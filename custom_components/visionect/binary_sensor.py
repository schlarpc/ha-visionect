"""Binary sensors, including the two that make the deferred model legible."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from pyvisionect.session import SyncStatus
from pyvisionect.session.device import CONVERGENCE_CONTACTS

from . import VisionectConfigEntry
from .const import SIGNAL_DEVICE_ADDED, SIGNAL_DEVICE_REMOVED
from .entity import VisionectEntity
from .runtime import VisionectRuntime

PARALLEL_UPDATES = 0

CHARGING_ON = {"pre charge", "fast charge"}


@dataclass(frozen=True, kw_only=True)
class VisionectBinaryDescription(BinarySensorEntityDescription):
    field: str | None = None
    is_on_fn: Callable[[VisionectRuntime, str], bool | None]
    live: bool = False
    """True for values that are properties of the connection, not readings."""

    attrs_fn: Callable[[VisionectRuntime, str], dict[str, Any]] | None = None


def _charging(runtime: VisionectRuntime, uuid: str) -> bool | None:
    snapshot = runtime.coordinator.snapshot(uuid)
    if snapshot is None:
        return None
    # Tag 11 is an enum, not a bool. Only the charging states are "on".
    value = snapshot.get("ChargingStatus")
    if not isinstance(value, dict):
        return None
    return value.get("name") in CHARGING_ON


def _out_of_sync(runtime: VisionectRuntime, uuid: str) -> bool | None:
    """The free in-sync test: the device echoes our own checksum back.

    This is a ``PROBLEM`` sensor, so the only thing that may turn it on is a
    real problem. The raw tri-state is not that: a push legitimately leaves
    the device disagreeing with us until it has drawn the frame and reported
    the new ``DisplayStateCRC``, measured at 48 s on this sign, so presenting
    ``in_sync is False`` directly raised a fault after **every normal
    update**.

    So the mapping is:

    ===============  =======  =========================================
    sync_status      is_on    meaning
    ===============  =======  =========================================
    ``unknown``      None     never pushed; a fresh install, not a fault
    ``in_sync``      False    showing what we sent
    ``converging``   False    pushed, not answered yet -- expected
    ``diverged``     True     it has had its say and still disagrees
    ===============  =======  =========================================
    """
    status = runtime.sync_status(uuid)
    if status is SyncStatus.UNKNOWN:
        return None
    return status is SyncStatus.DIVERGED


def _sync_attrs(runtime: VisionectRuntime, uuid: str) -> dict[str, Any]:
    """Enough to explain the state without reading the source.

    "Problem: on" with no reason is a bad sensor; so is "problem: off" while
    the checksums visibly differ. Both are answered here.
    """
    state = runtime.device_state(uuid)
    status = runtime.sync_status(uuid)
    return {
        "sync_status": status.value,
        "checksums_match": state.in_sync,
        "pushed_checksum": state.pushed_checksum,
        "device_checksum": (
            state.last_status.display_state_crc if state.last_status else None
        ),
        "contacts_since_push": state.contacts_since_push,
        "contacts_needed": CONVERGENCE_CONTACTS,
        # Two numbers because there are two ways out of "converging": the
        # device answers enough times after it could plausibly have drawn the
        # frame (settle), or it never answers at all (grace).
        "settle_seconds": round(state.settle_time()),
        "grace_seconds": round(state.convergence_grace()),
    }


def _pending_attrs(runtime: VisionectRuntime, uuid: str) -> dict[str, Any]:
    # The obvious home for "what is this screen waiting for" is the image
    # entity, but ImageEntity.state_attributes is @final -- an image entity can
    # carry no extra attributes at all. So it lives here.
    record = runtime.record(uuid)
    work = runtime.store.queue(bytes.fromhex(uuid.replace("-", "")))
    state = runtime.device_state(uuid)
    from .content import describe_source

    return {
        "pending": runtime.pending_descriptions(uuid),
        "queued_at": dict(work.queued_at),
        "attempts": dict(work.attempts),
        "content_source": describe_source(record.source),
        "want_revision": record.want_revision,
        "pushed_revision": record.pushed_revision,
        # Both of these, because they are different questions and they
        # disagreeing was how the false alarm was spotted in the first place:
        # in_sync is "do the checksums match right now", sync_status is "is
        # that a problem".
        "in_sync": state.in_sync,
        "sync_status": runtime.sync_status(uuid).value,
        "dither_mode": record.dither,
        "encoding": record.encoding,
        "fit_mode": record.fit,
        "last_error": record.last_error,
        "nack_charging": work.last_nack_charging,
    }


BINARY_SENSORS: tuple[VisionectBinaryDescription, ...] = (
    VisionectBinaryDescription(
        key="pending_changes",
        translation_key="pending_changes",
        is_on_fn=lambda rt, uuid: rt.has_pending(uuid),
        attrs_fn=_pending_attrs,
    ),
    VisionectBinaryDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        live=True,
        is_on_fn=lambda rt, uuid: rt.socket_open(uuid),
        # The honest, flappy "is it on the line right now". This is NOT
        # `available`: a sleeping sign is available and offline.
    ),
    VisionectBinaryDescription(
        key="charging",
        translation_key="charging",
        field="ChargingStatus",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=_charging,
    ),
    VisionectBinaryDescription(
        key="display_out_of_sync",
        translation_key="display_out_of_sync",
        field="DisplayStateCRC",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=_out_of_sync,
        attrs_fn=_sync_attrs,
    ),
    VisionectBinaryDescription(
        key="image_push_blocked",
        translation_key="image_push_blocked",
        field="ImagePushAllowed",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        is_on_fn=lambda rt, uuid: (
            None
            if (s := rt.coordinator.snapshot(uuid)) is None
            or s.get("ImagePushAllowed") is None
            else not s.get("ImagePushAllowed")
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VisionectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add(uuid: str) -> None:
        snapshot = runtime.coordinator.snapshot(uuid)
        seen = set(snapshot.fields) if snapshot else set()
        seen |= runtime.record(uuid).seen_fields
        new = []
        for desc in BINARY_SENSORS:
            key = f"{uuid}-{desc.key}"
            if key in known:
                continue
            if desc.field is not None and desc.field not in seen:
                continue
            known.add(key)
            new.append(VisionectBinarySensor(runtime, uuid, desc))
        if new:
            async_add_entities(new)

    @callback
    def _forget(uuid: str) -> None:
        """Drop this sign from the already-added set.

        A deleted sign re-registers on its next contact -- the protocol offers
        no way to refuse it -- and it must get its entities back when it does.
        """
        for key in [k for k in known if k.startswith(f"{uuid}-")]:
            known.discard(key)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_DEVICE_ADDED.format(entry.entry_id), _add)
    )
    entry.async_on_unload(
        async_dispatcher_connect(
            hass, SIGNAL_DEVICE_REMOVED.format(entry.entry_id), _forget
        )
    )
    for uuid in runtime.known_uuids():
        _add(uuid)


class VisionectBinarySensor(VisionectEntity, BinarySensorEntity):
    entity_description: VisionectBinaryDescription

    def __init__(
        self,
        runtime: VisionectRuntime,
        uuid: str,
        description: VisionectBinaryDescription,
    ) -> None:
        super().__init__(runtime, uuid)
        self.entity_description = description
        self._attr_unique_id = f"{uuid}-{description.key}"

    @property
    def available(self) -> bool:
        if self.entity_description.live:
            return self.runtime.listener_running
        return super().available

    @property
    def assumed_state(self) -> bool:
        if self.entity_description.live:
            return False
        return super().assumed_state

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.is_on_fn(self.runtime, self._uuid)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.runtime, self._uuid)
