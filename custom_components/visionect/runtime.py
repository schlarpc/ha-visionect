"""The runtime: the listener, the per-sign state, the queues and the reconciler.

Everything protocol-shaped lives in ``pyvisionect``.  What lives here is the
Home Assistant half: persistence across restarts, turning a declarative content
source into pixels at the moment the sign is reachable, and bridging library
events onto the coordinator and the dispatcher.

Two rules this module exists to enforce:

* **Encoding never runs on the event loop.**  A 1440x2560 frame is a few
  hundred milliseconds of dithering and packing; on the loop that presents as
  "Home Assistant freezes periodically".
* **Nothing is ever pushed from inside the library's read loop.**  A content
  push can involve an HTTP fetch to a renderer plus seconds of CPU, and
  blocking the read loop for that long stalls the acks the device is waiting
  for.  Reconciliation is a background task on the config entry.
"""

from __future__ import annotations

import contextlib
import logging
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from pyvisionect.devices import panel_for
from pyvisionect.imaging import FrameState, decode_image_packet, encode_frame
from pyvisionect.io import VisionectServer
from pyvisionect.io.tcp import FileReadError, server_ssl_context
from pyvisionect.session import (
    Acked,
    ButtonReceived,
    ConnectionConfig,
    DeviceConnected,
    DeviceState,
    DeviceStateStore,
    Event,
    FileEventReceived,
    HeartbeatOverdue,
    Nacked,
    ParamsReceived,
    ProtocolViolation,
    ReadTimeout,
    StatusReceived,
    SyncStatus,
    TouchReceived,
    SLOT_FLASH_SAVE,
    SLOT_FRAMEBUFFER_READ,
    SLOT_IMAGE,
    SLOT_PARAM_READS,
    SLOT_PARAM_WRITES,
)
from pyvisionect.packets.stored import parse_stored_frame
from pyvisionect.wire import STORED_ONLY

from .const import (
    CONF_PERSIST_PARAMS,
    CONF_TLS_CERTFILE,
    CONF_TLS_KEYFILE,
    DEFAULT_BACKGROUND,
    DEFAULT_DITHER,
    DEFAULT_ENCODING,
    DEFAULT_FIT,
    DEFAULT_PERSIST_PARAMS,
    DITHER_MODES,
    DOMAIN,
    ENCODINGS,
    EVENT_BUTTON,
    EVENT_COMMAND_NACKED,
    EVENT_DEVICE_CONNECTED,
    EVENT_FILES_LISTED,
    EVENT_FILE_READ,
    EVENT_PUSH_COMPLETED,
    EVENT_PUSH_FAILED,
    EVENT_TOUCH,
    FRAME_DIR,
    ISSUE_NO_DEVICE,
    NO_DEVICE_GRACE,
    OVERDUE_GRACE,
    SIGNAL_DEVICE_ADDED,
    SIGNAL_DEVICE_FILE_READ,
    SIGNAL_DEVICE_REMOVED,
    SIGNAL_LISTENER_STATE,
    SIGNAL_SCREEN_UPDATED,
    STORAGE_KEY,
    STORAGE_VERSION,
    TCLV_HEARTBEAT,
)
from .content import (
    BlankSource,
    ContentError,
    ContentSource,
    EntitySource,
    StaticImage,
    UrlSource,
    decode_and_fit,
    describe_source,
    fetch_source_bytes,
    render_placeholder,
    source_from_dict,
    source_to_dict,
)
from .coordinator import DeviceSnapshot, VisionectCoordinator

if TYPE_CHECKING:
    from . import VisionectConfigEntry

_LOGGER = logging.getLogger(__name__)

CHEAP_PHASES = (
    SLOT_PARAM_READS,
    SLOT_PARAM_WRITES,
    SLOT_FLASH_SAVE,
)
"""Slots ``apply_pending`` can put on the wire as one packet each.

``SLOT_FRAMEBUFFER_READ`` is deliberately **not** here. A file read is a
conversation -- open, then a read per 1024 bytes, then close -- and
``apply_pending`` would only ever send the opening ``open``, which is why the
file listing silently came back empty before. It is driven by
:meth:`VisionectRuntime._async_drain_file_read` instead.
"""

SAVE_DELAY = 5.0


def uuid_to_bytes(uuid: str) -> bytes:
    """``00112233-4455-...`` -> the 16 raw bytes the library keys on."""
    return bytes.fromhex(uuid.replace("-", ""))


def bytes_to_uuid(device_id: bytes) -> str:
    h = device_id.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


@dataclass
class DeviceRecord:
    """Our per-sign desired state. Persisted; the Store is the source of truth."""

    uuid: str
    source: ContentSource = field(default_factory=BlankSource)
    want_revision: int = 0
    """Bumped on any change of intent: a new source, a new dither, a service call."""
    pushed_revision: int = 0
    """The revision the device has acked. Equal to want_revision means settled."""
    force_next: bool = False
    encoding: str = DEFAULT_ENCODING
    dither: str = DEFAULT_DITHER
    fit: str = DEFAULT_FIT
    background: str = DEFAULT_BACKGROUND
    last_contact: datetime | None = None
    last_push: datetime | None = None
    connections: int = 0
    failed_pushes: int = 0
    last_error: str | None = None
    last_failure: datetime | None = None
    static_label: str = ""
    checked_crc_this_run: bool = False
    seen_fields: set[str] = field(default_factory=set)

    @property
    def needs_push(self) -> bool:
        return self.force_next or self.want_revision != self.pushed_revision

    def retry_due(self, now: datetime) -> bool:
        """Whether a previously-failed push may be attempted again.

        A sign on mains holds its connection open for hours, so "retry on the
        next contact" would mean retrying every heartbeat -- about 1440
        renderer requests a day while the renderer is down. Exponential
        backoff, capped at half an hour, keeps a broken source from becoming a
        denial of service against whatever is serving it.
        """
        if not self.failed_pushes or self.last_failure is None:
            return True
        minutes = min(2 ** min(self.failed_pushes, 5), 30)
        return now >= self.last_failure + timedelta(minutes=minutes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": source_to_dict(self.source),
            "want_revision": self.want_revision,
            "pushed_revision": self.pushed_revision,
            "force_next": self.force_next,
            "encoding": self.encoding,
            "dither": self.dither,
            "fit": self.fit,
            "background": self.background,
            "last_contact": self.last_contact.isoformat() if self.last_contact else None,
            "last_push": self.last_push.isoformat() if self.last_push else None,
            "connections": self.connections,
            "failed_pushes": self.failed_pushes,
            "last_error": self.last_error,
            "last_failure": self.last_failure.isoformat() if self.last_failure else None,
            "static_label": self.static_label,
            "seen_fields": sorted(self.seen_fields),
        }

    @classmethod
    def from_dict(cls, uuid: str, raw: dict[str, Any]) -> DeviceRecord:
        def _dt(value: Any) -> datetime | None:
            return dt_util.parse_datetime(value) if value else None

        return cls(
            uuid=uuid,
            source=source_from_dict(raw.get("source")),
            want_revision=int(raw.get("want_revision") or 0),
            pushed_revision=int(raw.get("pushed_revision") or 0),
            force_next=bool(raw.get("force_next")),
            encoding=raw.get("encoding") or DEFAULT_ENCODING,
            dither=raw.get("dither") or DEFAULT_DITHER,
            fit=raw.get("fit") or DEFAULT_FIT,
            background=raw.get("background") or DEFAULT_BACKGROUND,
            last_contact=_dt(raw.get("last_contact")),
            last_push=_dt(raw.get("last_push")),
            connections=int(raw.get("connections") or 0),
            failed_pushes=int(raw.get("failed_pushes") or 0),
            last_error=raw.get("last_error"),
            last_failure=_dt(raw.get("last_failure")),
            static_label=raw.get("static_label") or "",
            seen_fields=set(raw.get("seen_fields") or []),
        )


class VisionectRuntime:
    """Owns the listener, the device records, the queues and the coordinator."""

    def __init__(self, hass: HomeAssistant, entry: VisionectConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.host: str = entry.data["host"]
        self.port: int = entry.data["port"]
        self.store = DeviceStateStore()
        self.records: dict[str, DeviceRecord] = {}
        self.coordinator = VisionectCoordinator(hass, entry)
        self.listener_running = False
        self.tls_enabled = False
        """Whether a certificate loaded. Opportunistic either way: a plaintext
        sign keeps working on the same port."""
        self.previews: dict[str, bytes] = {}
        self.tclv_cache: dict[str, dict[int, Any]] = {}
        self.file_listings: dict[str, dict[str, Any]] = {}
        self.device_files: dict[str, bytes] = {}
        """uuid -> the last device file we decoded, as PNG. Feeds image.device_file."""
        self.device_file_meta: dict[str, dict[str, Any]] = {}
        self._file_reads: set[str] = set()
        """UUIDs with a file read in flight. One at a time per sign: the
        device answers 1 KiB at a time and two readers would interleave
        their chunks into each other's buffers."""
        self._sync_status: dict[str, str] = {}

        self._persist: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._frame_dir = Path(hass.config.path(".storage")) / FRAME_DIR
        self._inflight_push: dict[int, tuple[str, int]] = {}
        self._reconciling: set[str] = set()
        self._save_unsub: Any = None
        self._no_device_unsub: Any = None
        self._overdue_unsub: Any = None
        self._overdue: dict[str, bool] = {}
        self._forgotten: set[str] = set()
        self.advertised_address = f"{self.host}:{self.port}"
        """What to tell a human the sign should be dialling.

        The bind address is 0.0.0.0 by default, which is correct to bind and
        useless to read, so this resolves to Home Assistant's own LAN address
        once the listener is up.
        """
        self._clock = time.monotonic

        # Compression: prefer real LZ4, because that is what the vendor's
        # server emits and therefore the only path proven against this
        # firmware's decompressor. But lz4 ships no musllinux wheels and the
        # official Home Assistant container is Alpine, so it is frequently
        # absent -- in which case every block goes out Stored=1, which the
        # block format explicitly provides for and which costs essentially
        # nothing on dithered halftone data. Falling back silently is right;
        # refusing to run would be worse, and the choice is in diagnostics.
        self.compression_mode = "lz4"
        config_kwargs: dict[str, Any] = {}
        try:
            import lz4.block  # noqa: F401
        except ImportError:
            self.compression_mode = "stored_only"
            config_kwargs["compressor"] = STORED_ONLY
            _LOGGER.info(
                "lz4 is not installed, so outbound frames will be sent with "
                "every block uncompressed (Stored=1). This is a conforming "
                "mode and costs almost nothing on dithered image data, but it "
                "is not the mode the vendor's own server uses."
            )

        # No packet type 2, ever: it has never been observed on the wire, in
        # either direction, in any capture. allow_command_packets=False makes
        # any attempt raise instead of putting a possibly-malformed frame on
        # the wire, and watchdog_enabled=False is mandatory alongside it
        # because the watchdog's own expiry action is a type-2 status request.
        self.config = ConnectionConfig(
            allow_command_packets=False,
            watchdog_enabled=False,
            **config_kwargs,
        )
        self.server = VisionectServer(
            on_events=self._on_events,
            store=self.store,
            config=self.config,
            host=self.host,
            port=self.port,
        )

    # ------------------------------------------------------------- lifecycle

    async def async_load(self) -> None:
        """Restore everything a restart must not lose."""
        data = await self._persist.async_load() or {}
        if raw_store := data.get("store"):
            try:
                restored = DeviceStateStore.from_dict(raw_store)
            except ValueError as err:
                _LOGGER.warning("discarding unreadable device store: %s", err)
            else:
                for state in restored.known():
                    self.store.restore(state)

        snapshots: dict[str, DeviceSnapshot] = {}
        for uuid, raw in (data.get("devices") or {}).items():
            self.records[uuid] = DeviceRecord.from_dict(uuid, raw)
            snapshots[uuid] = DeviceSnapshot.from_dict(uuid, raw.get("snapshot") or {})
            self.tclv_cache[uuid] = {
                int(k): v for k, v in (raw.get("tclv") or {}).items()
            }
        self.coordinator.async_seed(snapshots)

        await self.hass.async_add_executor_job(self._load_frames)

    def _load_frames(self) -> None:
        """Rebuild each sign's FrameState so an in-sync restart costs no push."""
        self._frame_dir.mkdir(parents=True, exist_ok=True)
        for uuid in list(self.records):
            path = self._frame_dir / f"{uuid}.vnfs"
            if not path.exists():
                continue
            try:
                state = FrameState.from_bytes(path.read_bytes())
            except (OSError, ValueError) as err:
                _LOGGER.debug("could not restore frame state for %s: %s", uuid, err)
                continue
            self.store.get(uuid_to_bytes(uuid)).imaging_state = state
        for uuid in list(self.records):
            preview = self._frame_dir / f"{uuid}.preview.png"
            if preview.exists():
                with contextlib.suppress(OSError):
                    self.previews[uuid] = preview.read_bytes()

    async def _async_configure_tls(self) -> None:
        """Load the certificate, if one is configured, before binding.

        In the executor because ``load_cert_chain`` reads files, and before
        ``start()`` because a listener that is accepting with a half-configured
        context is worse than one that refused to come up.

        A bad certificate is **not** fatal here. The config flow already
        validated it, so reaching this is a file that moved or lost its
        permissions since -- and in that case a plaintext listener is strictly
        better than no listener: an unreachable server makes this firmware
        power-cycle itself roughly every twenty minutes.
        """
        certfile = (self.entry.options.get(CONF_TLS_CERTFILE) or "").strip()
        keyfile = (self.entry.options.get(CONF_TLS_KEYFILE) or "").strip()
        self.tls_enabled = False
        if not certfile:
            return
        try:
            context = await self.hass.async_add_executor_job(
                server_ssl_context, certfile, keyfile or None
            )
        except (OSError, ssl.SSLError, ValueError) as err:
            _LOGGER.error(
                "could not load the TLS certificate %s: %s. The listener will "
                "accept plaintext only. Any sign with TCLV 145 set to 1 will "
                "be unreachable until this is fixed or 145 is set back to 0 "
                "over USB serial.",
                certfile,
                err,
            )
            return
        self.server.ssl_context = context
        self.tls_enabled = True
        _LOGGER.info(
            "TLS 1.3 is available on port %s using %s. Plaintext signs are "
            "unaffected: the first six bytes of each connection decide which "
            "protocol it is.",
            self.port,
            certfile,
        )

    async def async_start(self) -> None:
        """Bind the listener. Raises ``ListenError`` on failure."""
        await self._async_configure_tls()
        await self.server.start()
        self.listener_running = True
        if self.host in ("0.0.0.0", "::", ""):
            try:
                from homeassistant.components import network

                source_ip = await network.async_get_source_ip(self.hass)
            except Exception:  # noqa: BLE001 - cosmetic only
                source_ip = ""
            if source_ip:
                self.advertised_address = f"{source_ip}:{self.port}"
        _LOGGER.info("Visionect listener bound to %s:%s", self.host, self.port)
        self._schedule_no_device_check()
        # assumed_state is a function of wall-clock time, so without a tick it
        # would never flip for a sign that simply stopped calling. The tick
        # only writes state on a *transition*, so a sleeping sign costs one
        # state write per sleep rather than one per minute.
        self._overdue_unsub = async_track_time_interval(
            self.hass, self._async_check_overdue, timedelta(minutes=1)
        )

    async def async_shutdown(self) -> None:
        """Stop accepting, drop live connections, persist, and go away."""
        self.listener_running = False
        if self._save_unsub is not None:
            self._save_unsub()
            self._save_unsub = None
        if self._no_device_unsub is not None:
            self._no_device_unsub()
            self._no_device_unsub = None
        if self._overdue_unsub is not None:
            self._overdue_unsub()
            self._overdue_unsub = None
        # abort=True: a half-written 1.84 MB frame is not worth waiting for on
        # a shutdown path. close() already does close()/close_clients()/
        # wait_closed() in the order Python's docs mandate, which is what stops
        # wait_closed() hanging forever on a sign that holds its socket open.
        await self.server.close(abort=True)
        await self.async_save()

    @callback
    def _async_check_overdue(self, _now: Any) -> None:
        """Flip assumed_state when a sign misses the window it announced.

        Logs once on the transition, never per heartbeat: a sign with a
        one-minute heartbeat would otherwise produce 1440 log lines a day,
        which is exactly what the "log when unavailable" rule asks you not
        to do.
        """
        changed = False
        for uuid in self.known_uuids():
            if not self.has_ever_seen(uuid):
                continue
            # The sync verdict can change with nothing but the clock: a sign
            # that stops calling altogether never produces the contact that
            # would otherwise settle it. Without this tick the problem sensor
            # would sit at "converging" forever.
            verdict = self.sync_status(uuid).value
            if self._sync_status.get(uuid) != verdict:
                self._sync_status[uuid] = verdict
                changed = True

            overdue = self.is_overdue(uuid)
            previous = self._overdue.get(uuid)
            if previous == overdue:
                continue
            self._overdue[uuid] = overdue
            changed = True
            if not overdue and previous is None:
                # First tick after a restart for a sign that is fine. Nothing
                # happened; do not announce a recovery that never occurred.
                continue
            if overdue:
                expected = self.expected_next_contact(uuid)
                _LOGGER.info(
                    "%s has not been in touch since %s; it announced it would "
                    "call back by %s. Its readings are now flagged as possibly "
                    "out of date, but the sign is not marked unavailable -- a "
                    "sleeping sign is doing what it was told.",
                    uuid,
                    self.record(uuid).last_contact,
                    expected,
                )
            else:
                _LOGGER.info("%s is back in touch", uuid)
        if changed:
            self.async_notify_entities()

    # ----------------------------------------------------------- persistence

    @callback
    def async_schedule_save(self) -> None:
        if self._save_unsub is not None:
            return
        self._save_unsub = async_call_later(self.hass, SAVE_DELAY, self._async_save_cb)

    async def _async_save_cb(self, _now: Any) -> None:
        self._save_unsub = None
        await self.async_save()

    async def async_save(self) -> None:
        devices: dict[str, Any] = {}
        for uuid, rec in self.records.items():
            payload = rec.to_dict()
            snapshot = self.coordinator.snapshot(uuid)
            payload["snapshot"] = snapshot.to_dict() if snapshot else {}
            payload["tclv"] = {
                str(k): v for k, v in (self.tclv_cache.get(uuid) or {}).items()
            }
            devices[uuid] = payload
        # Filter the library store to the signs we still know about, so a
        # deleted sign cannot come back from the protocol-level state map after
        # a restart.
        raw_store = {
            key: value
            for key, value in self.store.to_dict().items()
            if bytes_to_uuid(bytes.fromhex(key)) in self.records
        }
        await self._persist.async_save({"store": raw_store, "devices": devices})

    def _write_frame_state(self, uuid: str, state: Any, preview: bytes | None) -> None:
        self._frame_dir.mkdir(parents=True, exist_ok=True)
        if state is not None:
            with contextlib.suppress(OSError, ValueError):
                (self._frame_dir / f"{uuid}.vnfs").write_bytes(state.to_bytes())
        if preview is not None:
            with contextlib.suppress(OSError):
                (self._frame_dir / f"{uuid}.preview.png").write_bytes(preview)

    def _write_static_source(self, uuid: str, raw: bytes) -> None:
        self._frame_dir.mkdir(parents=True, exist_ok=True)
        (self._frame_dir / f"{uuid}.source.bin").write_bytes(raw)

    def _read_static_source(self, uuid: str) -> bytes:
        return (self._frame_dir / f"{uuid}.source.bin").read_bytes()

    # ------------------------------------------------------------- accessors

    def record(self, uuid: str) -> DeviceRecord:
        """This sign's desired state, created on demand.

        Auto-creating is what lets an entity read a record before the first
        save, but it also means a stray property read can resurrect a sign the
        user has just deleted -- the entity objects outlive the device registry
        entry by a moment. So a forgotten UUID gets a throwaway record that is
        never added to the map, and only a real status packet brings it back.
        """
        if uuid in self._forgotten:
            return DeviceRecord(uuid=uuid)
        if uuid not in self.records:
            self.records[uuid] = DeviceRecord(uuid=uuid)
        return self.records[uuid]

    def known_uuids(self) -> list[str]:
        return list(self.records)

    def device_state(self, uuid: str) -> DeviceState:
        return self.store.get(uuid_to_bytes(uuid))

    def panel(self, uuid: str):
        state = self.device_state(uuid)
        display_type = state.display_type
        return panel_for(display_type if display_type is not None else -1)

    def has_ever_seen(self, uuid: str) -> bool:
        snapshot = self.coordinator.snapshot(uuid)
        return snapshot is not None and bool(snapshot.fields)

    def socket_open(self, uuid: str) -> bool:
        return self.server.connection_for(uuid_to_bytes(uuid)) is not None

    def expected_next_contact(self, uuid: str) -> datetime | None:
        """When the device said it would next be in touch.

        1. ``NextStatus`` (status tag 27, minutes) -- the device's own
           announcement, confirmed present on this hardware;
        2. the ``HEARTBEAT`` TCLV (29) value we last read, also minutes;
        3. ``None`` -- unknown, so the caller falls back to "is the socket open".
        """
        rec = self.records.get(uuid)
        snapshot = self.coordinator.snapshot(uuid)
        if rec is None or snapshot is None or rec.last_contact is None:
            return None
        minutes = snapshot.get("NextStatus")
        if minutes is None:
            minutes = (self.tclv_cache.get(uuid) or {}).get(TCLV_HEARTBEAT)
        if not minutes:
            return None
        return rec.last_contact + timedelta(minutes=int(minutes))

    def sync_status(self, uuid: str) -> SyncStatus:
        """Whether this sign is showing what we sent -- with the clock applied.

        ``DeviceState.in_sync`` alone says False for the whole of the device's
        draw-and-report cycle after every push (48 s measured on this sign),
        which as a ``BinarySensorDeviceClass.PROBLEM`` is a fault indicator
        after every normal update. The library's four-state verdict separates
        "has not answered yet" from "is showing the wrong thing"; this passes
        it the same clock the push was recorded against.
        """
        return self.device_state(uuid).sync_status(self._clock())

    def is_overdue(self, uuid: str) -> bool:
        expected = self.expected_next_contact(uuid)
        if expected is None:
            return not self.socket_open(uuid)
        return dt_util.utcnow() > expected + OVERDUE_GRACE

    def pending_descriptions(self, uuid: str) -> list[str]:
        """A human list for ``binary_sensor.pending_changes``."""
        rec = self.records.get(uuid)
        work = self.store.queue(uuid_to_bytes(uuid))
        out: list[str] = []
        if rec is not None and rec.needs_push:
            out.append("content update")
        for param_id, value in sorted(work.params.items()):
            name = {TCLV_HEARTBEAT: "heartbeat interval"}.get(param_id, f"parameter {param_id}")
            out.append(f"{name} -> {value}")
        if work.param_reads:
            out.append("read parameters " + ", ".join(str(i) for i in sorted(work.param_reads)))
        if work.framebuffer_read == "list":
            out.append("list device files")
        elif work.framebuffer_read:
            out.append(f"read {work.framebuffer_read}")
        return out

    def has_pending(self, uuid: str) -> bool:
        rec = self.records.get(uuid)
        if rec is not None and rec.needs_push:
            return True
        return not self.store.queue(uuid_to_bytes(uuid)).is_empty

    # ---------------------------------------------------------- intent (API)

    @callback
    def async_bump(self, uuid: str, *, reason: str, force: bool = False) -> None:
        """Record that this sign's content should change on next contact.

        Any explicit change of intent clears the failure backoff. A user who
        has just corrected a broken renderer URL means "try this now", and
        making them wait out the backoff on the *old* URL's failures would be
        baffling.
        """
        rec = self.record(uuid)
        rec.want_revision += 1
        rec.failed_pushes = 0
        rec.last_failure = None
        rec.last_error = None
        if force:
            rec.force_next = True
        _LOGGER.debug("%s: revision -> %s (%s)", uuid, rec.want_revision, reason)
        self.async_schedule_save()
        self.async_notify_entities(uuid)
        self.async_kick(uuid)

    @callback
    def async_set_source(self, uuid: str, source: ContentSource) -> None:
        rec = self.record(uuid)
        rec.source = source
        if isinstance(source, StaticImage):
            rec.static_label = source.label
        self.async_bump(uuid, reason=f"source -> {describe_source(source)}")

    async def async_set_static_image(
        self, uuid: str, raw: bytes, *, label: str = ""
    ) -> None:
        await self.hass.async_add_executor_job(
            self._write_static_source, uuid, raw
        )
        self.async_set_source(uuid, StaticImage(label=label))

    @callback
    def async_queue_param_write(
        self, uuid: str, values: dict[int, Any], *, persist: bool | None = None
    ) -> None:
        work = self.store.queue(uuid_to_bytes(uuid))
        if persist is None:
            persist = self.entry.options.get(CONF_PERSIST_PARAMS, DEFAULT_PERSIST_PARAMS)
        work.persist_params = bool(persist)
        work.write_params(values, now=self._clock())
        # Read them back so the device, not our optimism, has the last word.
        work.read_params(values, now=self._clock())
        self.async_schedule_save()
        self.async_notify_entities(uuid)
        self.async_kick(uuid)

    @callback
    def async_queue_param_read(self, uuid: str, ids: list[int]) -> None:
        self.store.queue(uuid_to_bytes(uuid)).read_params(ids, now=self._clock())
        self.async_schedule_save()
        self.async_notify_entities(uuid)
        self.async_kick(uuid)

    @callback
    def async_queue_file_list(self, uuid: str) -> None:
        """Ask for the device's directory, now or on its next contact."""
        self.store.queue(uuid_to_bytes(uuid)).read_framebuffer("list", now=self._clock())
        self.async_schedule_save()
        self.async_notify_entities(uuid)
        self.async_kick(uuid)

    # ------------------------------------------------------- device files

    async def async_list_device_files(self, uuid: str) -> dict[str, Any]:
        """Read the device's directory. Cheap -- one round trip, well under a second.

        Raises:
            HomeAssistantError: if the sign is not on the line, or refused.
        """
        device_id = uuid_to_bytes(uuid)
        if self.server.connection_for(device_id) is None:
            raise HomeAssistantError(
                f"{self._friendly_name(uuid)} is not connected right now, so "
                "its filesystem cannot be read. The request has been queued "
                "for its next contact."
            )
        try:
            entries = await self.server.list_device_files(device_id)
        except (FileReadError, KeyError) as err:
            raise HomeAssistantError(f"could not list the device files: {err}") from err
        listing = {
            name: {"size": entry.size, "checksum": entry.checksum}
            for name, entry in entries.items()
        }
        self.file_listings[uuid] = listing
        self.store.queue(device_id).framebuffer_read = None
        self.hass.bus.async_fire(EVENT_FILES_LISTED, {"uuid": uuid, "files": listing})
        self.async_schedule_save()
        self.async_notify_entities(uuid)
        return listing

    async def async_read_device_file(
        self,
        uuid: str,
        filename: str,
        *,
        decode: bool = True,
        timeout: float = 30.0,
        attempts: int = 3,
    ) -> dict[str, Any]:
        """Pull one file off the device's flash and, if it is a frame, decode it.

        **This takes minutes.** The device answers at about 2.3 KiB/s in 1 KiB
        replies and offers no seek, so its 134 KB-1.2 MB stored frames take
        one to nine minutes and a lost reply restarts the transfer. The result
        carries the measured duration so a caller can say so.

        What comes back is *not* a live framebuffer. On firmware 7.4.4407 the
        six ``/imageN.pv2`` files are Visionect's shipped demo screens and do
        not change when we push; ``DisplayStateCRC`` remains the only way to
        ask the sign what it is actually displaying. The decode is still real,
        and would read a cached frame on a firmware that kept one.
        """
        device_id = uuid_to_bytes(uuid)
        if self.server.connection_for(device_id) is None:
            raise HomeAssistantError(
                f"{self._friendly_name(uuid)} is not connected right now."
            )
        if uuid in self._file_reads:
            raise HomeAssistantError(
                f"{self._friendly_name(uuid)} is already reading a file. The "
                "device answers one 1 KiB chunk at a time, so two reads would "
                "interleave into each other."
            )
        listing = self.file_listings.get(uuid) or await self.async_list_device_files(uuid)
        entry = listing.get(filename)
        if entry is None:
            raise ServiceValidationError(
                f"{filename!r} is not on the device. It holds: "
                + ", ".join(sorted(listing))
            )
        size = int(entry["size"])

        self._file_reads.add(uuid)
        started = time.monotonic()
        try:
            raw = await self.server.read_device_file(
                device_id, filename, size, timeout=timeout, attempts=attempts
            )
        except (FileReadError, KeyError) as err:
            raise HomeAssistantError(f"reading {filename} failed: {err}") from err
        finally:
            self._file_reads.discard(uuid)
        duration = time.monotonic() - started

        result: dict[str, Any] = {
            "uuid": uuid,
            "filename": filename,
            "size": size,
            "bytes_read": len(raw),
            "duration": round(duration, 1),
            "rate_kib_s": round(len(raw) / max(duration, 1e-3) / 1024, 2),
        }
        if not decode:
            return result

        try:
            frame, png = await self.hass.async_add_executor_job(
                _decode_device_file, raw, self.panel(uuid)
            )
        except Exception as err:  # noqa: BLE001 - any malformed file
            _LOGGER.warning("%s: %s did not decode as a frame: %s", uuid, filename, err)
            result["decoded"] = False
            result["decode_error"] = str(err)
            return result

        state = self.device_state(uuid)
        result.update(
            decoded=True,
            frame_version=frame.header.version,
            blocks=frame.header.nblocks,
            image_checksum=frame.checksum,
            rectangles=[
                {
                    "screen_id": r.screen_id,
                    "x": r.x,
                    "y": r.y,
                    "width": r.width,
                    "height": r.height,
                    "encoding": r.encoding,
                }
                for r in frame.image.rectangles
            ],
            written_by_us=frame.looks_like_our_push,
            matches_last_push=(
                frame.checksum == state.pushed_checksum
                and state.pushed_checksum is not None
            ),
        )
        self.device_files[uuid] = png
        self.device_file_meta[uuid] = result
        async_dispatcher_send(
            self.hass, SIGNAL_DEVICE_FILE_READ.format(self.entry.entry_id), uuid
        )
        self.hass.bus.async_fire(EVENT_FILE_READ, result)
        self.async_notify_entities(uuid)
        return result

    async def _async_drain_file_read(self, uuid: str) -> None:
        """Honour a queued ``read_framebuffer`` slot now that the sign is here."""
        work = self.store.queue(uuid_to_bytes(uuid))
        what = work.framebuffer_read
        if not what:
            return
        work.framebuffer_read = None
        try:
            if what == "list":
                await self.async_list_device_files(uuid)
            else:
                await self.async_read_device_file(uuid, what)
        except (HomeAssistantError, ServiceValidationError) as err:
            _LOGGER.warning("%s: queued file read failed: %s", uuid, err)

    @callback
    def async_kick(self, uuid: str) -> None:
        """Try to reconcile now, if the sign happens to be on the line."""
        if not self.socket_open(uuid):
            return
        self.entry.async_create_background_task(
            self.hass, self._reconcile(uuid), f"visionect-reconcile-{uuid}"
        )

    @callback
    def async_notify_entities(self, uuid: str | None = None) -> None:
        self.coordinator.async_update_listeners()

    # ------------------------------------------------------- event bridging

    async def _on_events(self, conn: Any, events: list[Event]) -> None:
        """The single bridge from library events to Home Assistant state.

        Runs on the event loop inside the library's read loop, so dispatcher
        sends and ``async_set_updated_data`` are safe here with no
        ``call_soon_threadsafe``.  Exceptions are *not* caught by the library,
        so the whole body is guarded.
        """
        try:
            for event in events:
                await self._handle_event(conn, event)
        except Exception:  # noqa: BLE001 - the library would not log this
            _LOGGER.exception("error handling Visionect events")

    async def _handle_event(self, conn: Any, event: Event) -> None:
        uuid = event.uuid or (bytes_to_uuid(conn.device_id) if conn.device_id else "")
        match event:
            case DeviceConnected():
                # Register the device BEFORE publishing the status snapshot:
                # the snapshot is what makes the platforms create entities, and
                # an entity added before its device has a name gets its
                # entity_id from the config entry title instead.
                await self._async_register_device(uuid)
                self._note_status(uuid, event.status, first=True)
                self.hass.bus.async_fire(
                    EVENT_DEVICE_CONNECTED,
                    {"uuid": uuid, "connect_reason": event.status.connect_reason_name},
                )
                self._clear_no_device_issue()
                self.entry.async_create_background_task(
                    self.hass, self._reconcile(uuid), f"visionect-reconcile-{uuid}"
                )
            case StatusReceived():
                self._note_status(uuid, event.status)
                # A mains sign holds one connection for hours, so DeviceConnected
                # fires once and then never again. Without this, work queued (or
                # a push that failed) mid-connection would wait for a reconnect
                # that may not come today.
                if self.has_pending(uuid):
                    self.entry.async_create_background_task(
                        self.hass, self._reconcile(uuid), f"visionect-reconcile-{uuid}"
                    )
            case Acked():
                self._on_acked(uuid, event)
            case Nacked():
                self._on_nacked(uuid, event)
            case ParamsReceived():
                self._on_params(uuid, event)
            case FileEventReceived():
                self._on_file_event(uuid, event)
            case TouchReceived():
                self.hass.bus.async_fire(EVENT_TOUCH, {"uuid": uuid})
            case ButtonReceived():
                self.hass.bus.async_fire(EVENT_BUTTON, {"uuid": uuid})
            case HeartbeatOverdue():
                self.async_notify_entities(uuid)
            case ReadTimeout():
                _LOGGER.debug("%s: read timeout after %.0fs", uuid, event.quiet_for)
            case ProtocolViolation(reason=reason):
                _LOGGER.warning("protocol violation from %s: %s", uuid or "?", reason)

    def _note_status(self, uuid: str, status: Any, *, first: bool = False) -> None:
        # A sign that was deleted and has now dialled in again is a real sign
        # again. The protocol gives us no way to turn one away, which the
        # removal dialog says out loud.
        self._forgotten.discard(uuid)
        rec = self.record(uuid)
        now = dt_util.utcnow()
        rec.last_contact = now
        if first:
            rec.connections += 1

        fields = status.fields()
        new_fields = set(fields) - rec.seen_fields
        rec.seen_fields |= set(fields)

        previous = self.coordinator.snapshot(uuid)
        snapshot = DeviceSnapshot(
            uuid=uuid,
            fields=fields,
            raw=dict(status.raw),
            last_contact=now,
            last_push=rec.last_push,
            connections=rec.connections,
            restored=False,
        )
        self.coordinator.async_set_snapshot(snapshot)

        if new_fields or previous is None or previous.restored:
            # Entity creation is driven by the status packet, not a static
            # list: a sign that never reports humidity must not get a
            # permanently-unavailable humidity sensor. Fields can also appear
            # for the first time on a later heartbeat.
            async_dispatcher_send(
                self.hass, SIGNAL_DEVICE_ADDED.format(self.entry.entry_id), uuid
            )

        # The e-ink free-restart check, done exactly once per run: if the sign
        # is showing something other than what we last pushed, something else
        # changed it, so re-assert our content. Once only, so a device that
        # never confirms cannot put us in a push loop.
        state = self.device_state(uuid)

        # A sign we have never pushed to gets one push, so `image.screen` is
        # not blank forever and so the panel says out loud that it is connected
        # but has no content source. Guarded on the persisted revision, so it
        # happens exactly once in the sign's life with this integration.
        if (
            first
            and rec.want_revision == 0
            and rec.pushed_revision == 0
            and state.pushed_checksum is None
        ):
            _LOGGER.info("%s: first contact, pushing the placeholder frame", uuid)
            rec.want_revision += 1

        if not rec.checked_crc_this_run:
            rec.checked_crc_this_run = True
            reported = status.display_state_crc
            if (
                state.pushed_checksum is not None
                and reported is not None
                and reported != state.pushed_checksum
                and not rec.needs_push
            ):
                _LOGGER.info(
                    "%s reports DisplayStateCRC %s but we last pushed %s; "
                    "re-asserting content",
                    uuid,
                    reported,
                    state.pushed_checksum,
                )
                rec.want_revision += 1
        self.async_schedule_save()

    def _on_acked(self, uuid: str, event: Acked) -> None:
        work = self.store.queue(uuid_to_bytes(uuid))
        slot = work.on_acked(event.packet_id)
        pushed = self._inflight_push.pop(event.packet_id, None)
        if pushed is not None:
            _uuid, revision = pushed
            rec = self.record(uuid)
            rec.pushed_revision = revision
            rec.force_next = False
            rec.last_push = dt_util.utcnow()
            rec.failed_pushes = 0
            rec.last_error = None
            _LOGGER.info("%s acked image push (revision %s)", uuid, revision)
            self.hass.bus.async_fire(
                EVENT_PUSH_COMPLETED,
                {
                    "uuid": uuid,
                    "revision": revision,
                    "checksum": self.device_state(uuid).pushed_checksum,
                },
            )
        elif slot:
            _LOGGER.debug("%s acked %s", uuid, slot)
        self.async_schedule_save()
        self.async_notify_entities(uuid)

    def _on_nacked(self, uuid: str, event: Nacked) -> None:
        work = self.store.queue(uuid_to_bytes(uuid))
        slot = work.on_nacked(event.packet_id, charging=event.charging)
        pushed = self._inflight_push.pop(event.packet_id, None)
        rec = self.record(uuid)
        detail = "charging" if event.charging else f"error {event.error_code}"
        if pushed is not None:
            rec.failed_pushes += 1
            rec.last_error = f"push refused ({detail})"
            rec.last_failure = dt_util.utcnow()
            self.hass.bus.async_fire(
                EVENT_PUSH_FAILED, {"uuid": uuid, "reason": detail}
            )
        # A charging NACK is a retry-later, not a failure. Either way the work
        # stays queued, because a NACK means the device did not do it.
        log = _LOGGER.debug if event.charging else _LOGGER.warning
        log("%s refused %s (%s)", uuid, slot or "a request", detail)
        self.hass.bus.async_fire(
            EVENT_COMMAND_NACKED,
            {"uuid": uuid, "slot": slot, "charging": event.charging,
             "error_code": event.error_code},
        )
        self.async_schedule_save()
        self.async_notify_entities(uuid)

    def _on_params(self, uuid: str, event: ParamsReceived) -> None:
        """Correct our view of the device's TCLV settings from the device."""
        cache = self.tclv_cache.setdefault(uuid, {})
        for item in event.params.items:
            if item.is_error:
                # error_name separates "this firmware has no such setting" from
                # "the value was the wrong width", which otherwise look the
                # same and have opposite fixes.
                _LOGGER.warning(
                    "%s refused parameter %s (%s): %s",
                    uuid,
                    item.id,
                    item.name,
                    item.error_name,
                )
                continue
            cache[int(item.id)] = _decode_tclv(item.value)
        _LOGGER.debug("%s reported parameters %s", uuid, cache)
        self.async_schedule_save()
        self.async_notify_entities(uuid)

    def _on_file_event(self, uuid: str, event: FileEventReceived) -> None:
        """A file reply, in passing.

        File traffic is driven by :meth:`async_list_device_files` and
        :meth:`async_read_device_file`, which attach their own listener to the
        library server for the duration of the transfer and reassemble the
        chunks there. Every chunk also reaches this handler, so it stays quiet
        and does not try to read a 1024-byte slice of LZ4 as a directory.
        """
        if event.file is not None:
            _LOGGER.debug(
                "%s: file reply %s, %d bytes",
                uuid,
                event.file.op_name,
                len(event.file.raw),
            )

    # --------------------------------------------------------- device registry

    async def _async_register_device(self, uuid: str) -> None:
        state = self.device_state(uuid)
        status = state.last_status
        panel = self.panel(uuid)
        registry = dr.async_get(self.hass)

        def _version(name: str) -> str | None:
            raw = status.get(name) if status else None
            if not isinstance(raw, dict):
                return None
            parts = [raw.get("major"), raw.get("minor"), raw.get("revision")]
            if parts[0] is None:
                return None
            return ".".join(str(p) for p in parts if p is not None)

        model = panel.name if not panel.is_default else "Visionect sign"
        if panel.is_default:
            model = f'Visionect sign ({panel.canvas_width}x{panel.canvas_height})'

        registry.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, uuid)},
            manufacturer="Visionect",
            # A default name rather than None: with None, Home Assistant falls
            # back to the config entry title ("Visionect listener (port
            # 11113)"), which then prefixes every entity id on the sign. The
            # user can still rename it, and renaming is the point.
            name=f"Visionect sign {uuid.split('-')[0]}",
            model=model,
            model_id=str(state.hardware_name_id) if state.hardware_name_id is not None else None,
            sw_version=_version("Firmware"),
            hw_version=_version("Hardware"),
            serial_number=(status.get("GTIN") if status else None) or None,
        )
        # Deliberately no `connections`: BSSID is the access point's MAC, not
        # the sign's, so using it would merge every sign on an AP into one
        # device. The sign never tells us its own MAC over this protocol.

    @callback
    def async_register_listener_device(self) -> None:
        dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, f"listener-{self.entry.entry_id}")},
            manufacturer="Visionect",
            model="Device listener",
            name="Visionect listener",
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    # ------------------------------------------------------- the silent failure

    def _schedule_no_device_check(self) -> None:
        self._no_device_unsub = async_call_later(
            self.hass, NO_DEVICE_GRACE.total_seconds(), self._async_no_device_check
        )

    async def _async_no_device_check(self, _now: Any) -> None:
        self._no_device_unsub = None
        stats = self.server.stats
        if stats.identified:
            return
        # Nothing has ever identified itself. The two cases need completely
        # different advice, so say which one it is.
        if stats.accepted:
            key = "connected_but_not_a_sign"
        else:
            key = "nothing_reached_the_port"
        _LOGGER.warning(
            "Visionect: listening on %s:%s for %s but no sign has connected "
            "(accepted=%s, identified=%s, silent=%s). The sign must be pointed "
            "at this address explicitly -- it cannot be discovered.",
            self.host,
            self.port,
            NO_DEVICE_GRACE,
            stats.accepted,
            stats.identified,
            stats.silent_connections,
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_NO_DEVICE}_{self.entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_NO_DEVICE,
            translation_placeholders={
                "host": self.host,
                "port": str(self.port),
                "accepted": str(stats.accepted),
                "detail": key,
            },
        )

    def _clear_no_device_issue(self) -> None:
        ir.async_delete_issue(
            self.hass, DOMAIN, f"{ISSUE_NO_DEVICE}_{self.entry.entry_id}"
        )

    # ------------------------------------------------------------- reconciler

    async def _reconcile(self, uuid: str) -> None:
        """Run the device's deferred work. One at a time per sign."""
        if uuid in self._reconciling:
            return
        self._reconciling.add(uuid)
        try:
            await self._reconcile_inner(uuid)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("reconcile failed for %s", uuid)
        finally:
            self._reconciling.discard(uuid)
            self.async_notify_entities(uuid)

    async def _reconcile_inner(self, uuid: str) -> None:
        device_id = uuid_to_bytes(uuid)
        conn = self.server.connection_for(device_id)
        if conn is None:
            return
        work = self.store.queue(device_id)
        rec = self.record(uuid)

        # 1-4: the cheap, small work first, so entity states are corrected
        # from the device before anything acts on them.
        if any(slot in CHEAP_PHASES for slot in work.slots()):
            sent = conn.apply_pending(phases=CHEAP_PHASES, now=self._clock())
            if sent:
                _LOGGER.debug("%s: queued %s", uuid, sent)
                with contextlib.suppress(KeyError):
                    await self.server.flush(device_id)

        # 4: a queued file read, which is a conversation and not one packet.
        if work.framebuffer_read:
            await self._async_drain_file_read(uuid)
            conn = self.server.connection_for(device_id)
            if conn is None:
                return

        # 5: the image push.
        if not rec.needs_push:
            return
        if not rec.retry_due(dt_util.utcnow()):
            _LOGGER.debug(
                "%s: holding off a retry; %s consecutive failures since %s",
                uuid,
                rec.failed_pushes,
                rec.last_failure,
            )
            return

        revision = rec.want_revision
        try:
            frame, preview = await self._async_build_frame(uuid, rec)
        except ContentError as err:
            rec.failed_pushes += 1
            rec.last_error = str(err)
            rec.last_failure = dt_util.utcnow()
            _LOGGER.warning("%s: could not resolve content: %s", uuid, err)
            self.hass.bus.async_fire(
                EVENT_PUSH_FAILED, {"uuid": uuid, "reason": str(err)}
            )
            # Deliberately do NOT consume the revision: the next contact
            # retries automatically, and the old frame stays on the glass.
            self.async_schedule_save()
            return

        if not frame.rectangles:
            # Nothing changed: the restored FrameState already matches. A free
            # restart, which is the whole point of persisting it.
            _LOGGER.info("%s: content unchanged, no push needed", uuid)
            rec.pushed_revision = revision
            rec.force_next = False
            self.async_schedule_save()
            return

        work.set_image(frame, force=rec.force_next, now=self._clock())
        conn = self.server.connection_for(device_id)
        if conn is None:
            _LOGGER.info("%s: window closed before the push; it stays queued", uuid)
            return
        sent = conn.apply_pending(phases=(SLOT_IMAGE,), now=self._clock())
        packet_id = sent.get(SLOT_IMAGE)
        if packet_id is None:
            return
        self._inflight_push[packet_id] = (uuid, revision)
        # Log the checksum the device will actually echo back, not the raw one:
        # a forced push deliberately perturbs ImageHeader.Checksum (it flips the
        # top bit) to buy one guaranteed full-screen redraw, and send_image
        # stores the perturbed value as pushed_checksum so in_sync stays
        # coherent. Logging the unperturbed value makes that look like a bug.
        _LOGGER.info(
            "%s: pushing %s rectangles, checksum %s, packet id %s",
            uuid,
            len(frame.rectangles),
            self.device_state(uuid).pushed_checksum,
            packet_id,
        )
        try:
            await self.server.flush(device_id)
        except KeyError:
            _LOGGER.info("%s: connection went away mid-push", uuid)
            return

        self.previews[uuid] = preview
        await self.hass.async_add_executor_job(
            self._write_frame_state, uuid, frame.state, preview
        )
        async_dispatcher_send(
            self.hass, SIGNAL_SCREEN_UPDATED.format(self.entry.entry_id), uuid
        )
        self.async_schedule_save()

    # ------------------------------------------------------------- the pixels

    async def _async_resolve_image(self, uuid: str, rec: DeviceRecord) -> Any:
        """Turn this sign's content source into a canvas-sized PIL image."""
        panel = self.panel(uuid)
        width, height = panel.canvas_width, panel.canvas_height

        if isinstance(rec.source, BlankSource):
            return await self.hass.async_add_executor_job(
                partial(
                    render_placeholder,
                    width=width,
                    height=height,
                    name=self._friendly_name(uuid),
                    address=self.advertised_address,
                )
            )

        if isinstance(rec.source, StaticImage):
            try:
                raw = await self.hass.async_add_executor_job(
                    self._read_static_source, uuid
                )
            except OSError as err:
                raise ContentError(f"stored image is gone: {err}") from err
        else:
            raw = await fetch_source_bytes(
                self.hass, rec.source, width=width, height=height
            )

        return await self.hass.async_add_executor_job(
            partial(
                decode_and_fit,
                raw,
                width=width,
                height=height,
                fit=rec.fit,
                background=rec.background,
            )
        )

    async def _async_build_frame(
        self, uuid: str, rec: DeviceRecord, *, image: Any = None
    ) -> tuple[Any, bytes]:
        """Resolve, encode and render a preview. All CPU work is off the loop."""
        if image is None:
            image = await self._async_resolve_image(uuid, rec)
        panel = self.panel(uuid)
        state = self.device_state(uuid)
        encoding = ENCODINGS.get(rec.encoding, 4)
        dithering = DITHER_MODES.get(rec.dither, 4)
        if encoding == 4 and dithering == DITHER_MODES["bayer"]:
            # GraphicsMagick's ordered dither takes no level parameter, so
            # bayer is bi-level only. Blue noise is the right ordered dither
            # at 4 bpp.
            _LOGGER.debug("%s: bayer is bi-level only; using blue noise", uuid)
            dithering = DITHER_MODES["blue_noise"]
        prev = state.imaging_state
        if prev is not None and (
            getattr(prev, "encoding", None) != encoding
            or getattr(prev, "dithering", None) != dithering
        ):
            # A dither or depth change must force a full re-encode.
            prev = None
        return await self.hass.async_add_executor_job(
            partial(
                _encode_and_preview,
                image,
                panel=panel,
                encoding=encoding,
                dithering=dithering,
                prev_state=prev,
                force_full=rec.force_next,
            )
        )

    async def async_push_image_now(
        self, uuid: str, image: Any, *, force: bool = False, label: str = ""
    ) -> dict[str, Any]:
        """Encode *image* and push it if the sign is on the line right now.

        The image is also stored as this sign's static content source before
        anything is encoded. That is not an optimisation: without it, a Home
        Assistant restart between the service call and the device's next
        contact would lose the picture, because ``PendingWork`` deliberately
        does not persist the frame (it is megabytes of pixels and cheaper to
        re-render). Persisting the *source* and re-rendering is the model.
        """
        rec = self.record(uuid)
        raw = await self.hass.async_add_executor_job(_png_bytes, image)
        await self.hass.async_add_executor_job(
            self._write_static_source, uuid, raw
        )
        rec.source = StaticImage(label=label)
        rec.static_label = label
        rec.force_next = rec.force_next or force
        rec.want_revision += 1
        revision = rec.want_revision
        frame, preview = await self._async_build_frame(uuid, rec, image=image)
        device_id = uuid_to_bytes(uuid)
        work = self.store.queue(device_id)
        if not frame.rectangles:
            rec.pushed_revision = revision
            rec.force_next = False
            self.async_schedule_save()
            return {"queued": False, "applied_immediately": False, "unchanged": True}
        work.set_image(frame, force=rec.force_next, now=self._clock())
        self.previews[uuid] = preview
        await self.hass.async_add_executor_job(
            self._write_frame_state, uuid, frame.state, preview
        )
        async_dispatcher_send(
            self.hass, SIGNAL_SCREEN_UPDATED.format(self.entry.entry_id), uuid
        )

        conn = self.server.connection_for(device_id)
        if conn is None:
            # The sign is away. The frame stays in the register and the source
            # is on disk, so this survives a restart as well as a long sleep.
            self.async_schedule_save()
            self.async_notify_entities(uuid)
            expected = self.expected_next_contact(uuid)
            return {
                "queued": True,
                "applied_immediately": False,
                "expected_at": expected.isoformat() if expected else None,
            }
        sent = conn.apply_pending(phases=(SLOT_IMAGE,), now=self._clock())
        packet_id = sent.get(SLOT_IMAGE)
        if packet_id is not None:
            self._inflight_push[packet_id] = (uuid, revision)
            with contextlib.suppress(KeyError):
                await self.server.flush(device_id)
        self.async_schedule_save()
        self.async_notify_entities(uuid)
        return {
            "queued": True,
            "applied_immediately": True,
            "packet_id": packet_id,
            "checksum": frame.state_checksum,
        }

    def _friendly_name(self, uuid: str) -> str:
        registry = dr.async_get(self.hass)
        device = registry.async_get_device_by_identifier(
            (DOMAIN, uuid), self.entry.entry_id
        )
        if device is not None:
            return device.name_by_user or device.name or uuid
        return uuid

    # -------------------------------------------------------------- removal

    async def async_forget(self, uuid: str) -> None:
        """Drop everything we know about a sign.

        It will re-register on its next contact -- the protocol has no concept
        of an unwanted device -- so this is "forget what I know", not "ban".
        """
        self._forgotten.add(uuid)
        self._overdue.pop(uuid, None)
        self.records.pop(uuid, None)
        self.previews.pop(uuid, None)
        self.tclv_cache.pop(uuid, None)
        self.file_listings.pop(uuid, None)
        self.device_files.pop(uuid, None)
        self.device_file_meta.pop(uuid, None)
        self._sync_status.pop(uuid, None)
        data = dict(self.coordinator.data or {})
        data.pop(uuid, None)
        self.coordinator.async_set_updated_data(data)
        # Tell the platforms, so that a sign which is deleted and then dials
        # in again gets its entities built afresh. Without this they would see
        # the UUID in their "already added" set and silently create nothing.
        async_dispatcher_send(
            self.hass, SIGNAL_DEVICE_REMOVED.format(self.entry.entry_id), uuid
        )
        for suffix in (".vnfs", ".preview.png", ".source.bin"):
            with contextlib.suppress(OSError):
                (self._frame_dir / f"{uuid}{suffix}").unlink()
        await self.async_save()


def _encode_and_preview(
    image: Any,
    *,
    panel: Any,
    encoding: int,
    dithering: int,
    prev_state: Any,
    force_full: bool,
) -> tuple[Any, bytes]:
    """Encode and render the post-dither preview in one executor hop.

    The preview is deliberately the *post-dither* image, reconstructed with the
    same quantiser the encoder used, not the pre-dither 8-bit state model. A
    prettier-than-reality preview hides exactly the dither-mode mistakes the
    user is adjusting the dither select to fix.
    """
    import io

    from PIL import Image as PILImage
    from pyvisionect.imaging import quantise, to_grey8

    frame = encode_frame(
        image,
        panel=panel,
        encoding=encoding,
        dithering=dithering,
        prev_state=prev_state,
        force_full_screen=force_full,
    )
    # quantise already returns expanded 8-bit grey -- {0, 17, 34, ... 255} at
    # 4 bpp, {0, 255} at 1 bpp -- not level indices. Scaling it again overflows
    # uint8 and produces a preview that is wrong in a plausible-looking way.
    preview = PILImage.fromarray(
        quantise(to_grey8(image), encoding, dithering), mode="L"
    )
    buf = io.BytesIO()
    preview.save(buf, format="PNG", optimize=False, compress_level=6)
    return frame, buf.getvalue()


def _decode_tclv(raw: bytes) -> Any:
    """A TCLV value as something JSON can hold.

    The library carries no per-id codec, so this is deliberately shallow: a
    value of four bytes or fewer is a little-endian integer (which covers every
    id this integration writes), anything longer is kept as hex.
    """
    if not raw:
        return None
    if len(raw) <= 4:
        return int.from_bytes(raw, "little")
    return raw.hex()


def _decode_device_file(raw: bytes, panel: Any):
    """A ``.pv2`` off the device -> (StoredFrame, PNG bytes). Executor only.

    Blocking: the block chain inflates to ~1.8 MB and the de-interlace plus
    nibble decode is numpy work over the whole canvas.
    """
    frame = parse_stored_frame(raw)
    decoded = decode_image_packet(frame.image, panel)
    return frame, _png_bytes(decoded.to_image())


def _png_bytes(image: Any) -> bytes:
    """A PIL image as PNG bytes. Blocking; executor only."""
    import io

    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=False, compress_level=6)
    return buf.getvalue()
