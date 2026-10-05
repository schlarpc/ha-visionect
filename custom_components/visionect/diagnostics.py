"""Diagnostics -- the integration's primary support tool.

The dominant failure mode here is silent: the listener binds, the config entry
looks healthy, and the sign never connects, because nothing errored. So this
is deliberately generous, and leads with the listener counters that tell
"nothing reaches the port" apart from "something connects but is not a sign".
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util.package import is_docker_env

from . import VisionectConfigEntry
from .content import describe_source
from .runtime import uuid_to_bytes

# async_redact_data matches key names exactly and is case-sensitive, and it
# never redacts None or "" -- so an absent value still discloses absence.
TO_REDACT = {"BSSID", "GTIN", "uuid", "headers", "url", "access_point", "serial_number"}


def _listener(entry: VisionectConfigEntry) -> dict[str, Any]:
    runtime = entry.runtime_data
    stats = runtime.server.stats
    return {
        "bind_host": runtime.host,
        "bind_port": runtime.port,
        "advertised_address": runtime.advertised_address,
        "listener_running": runtime.listener_running,
        "accepted": stats.accepted,
        "identified": stats.identified,
        "silent_connections": stats.silent_connections,
        "rejected": stats.rejected,
        # tls_unsupported is the one that names a stranded sign: a sign whose
        # parameter 145 is set but whose server has no certificate.
        "tls_enabled": runtime.tls_enabled,
        "tls_accepted": stats.tls_accepted,
        "tls_failed": stats.tls_failed,
        "tls_unsupported": stats.tls_unsupported,
        "bytes_in": stats.bytes_in,
        "bytes_out": stats.bytes_out,
        "last_accept_at": stats.last_accept_at,
        "live_connections": [
            uuid for uuid in runtime.known_uuids() if runtime.socket_open(uuid)
        ],
        "is_docker_env": is_docker_env(),
        "connection_config": {
            "compressor": runtime.compression_mode,
            "allow_command_packets": runtime.config.allow_command_packets,
            "watchdog_enabled": runtime.config.watchdog_enabled,
            "packet_timeout": runtime.config.packet_timeout,
        },
    }


def _device(entry: VisionectConfigEntry, uuid: str) -> dict[str, Any]:
    runtime = entry.runtime_data
    record = runtime.record(uuid)
    state = runtime.device_state(uuid)
    snapshot = runtime.coordinator.snapshot(uuid)
    work = runtime.store.queue(uuid_to_bytes(uuid))
    panel = runtime.panel(uuid)
    status = state.last_status
    return {
        # The UUID is the single most useful debugging field and also a
        # semi-secret, so keep a stable short hash for correlation.
        "uuid_short": uuid[:8],
        "fields": snapshot.fields if snapshot else {},
        "raw_tags": {str(k): v for k, v in (snapshot.raw if snapshot else {}).items()},
        "unknown_tags": (snapshot.fields.get("Unknown") if snapshot else None),
        "features": state.features,
        "panel": {
            "name": panel.name,
            "canvas": [panel.canvas_width, panel.canvas_height],
            "displays": panel.displays,
            "driver": str(panel.driver),
            "display_type": panel.display_type,
            "is_default_row": panel.is_default,
            "describe": panel.describe(),
        },
        "hardware_name_id": state.hardware_name_id,
        "supports_rectangles": state.supports_rectangles,
        "pushed_checksum": state.pushed_checksum,
        "display_state_crc": status.display_state_crc if status else None,
        "in_sync": state.in_sync,
        "has_pushed": state.has_pushed,
        "content_source": describe_source(record.source),
        "want_revision": record.want_revision,
        "pushed_revision": record.pushed_revision,
        "force_next": record.force_next,
        "encoding": record.encoding,
        "dither": record.dither,
        "fit": record.fit,
        "failed_pushes": record.failed_pushes,
        "last_error": record.last_error,
        "last_contact": record.last_contact.isoformat() if record.last_contact else None,
        "last_push": record.last_push.isoformat() if record.last_push else None,
        "next_contact": (
            expected.isoformat()
            if (expected := runtime.expected_next_contact(uuid))
            else None
        ),
        "overdue": runtime.is_overdue(uuid),
        "socket_open": runtime.socket_open(uuid),
        "connections": record.connections,
        "pending": {
            "slots": work.slots(),
            "detail": runtime.pending_descriptions(uuid),
            "snapshot": work.to_dict(),
        },
        "tclv_cache": runtime.tclv_cache.get(uuid, {}),
        "file_listing": runtime.file_listings.get(uuid, {}),
        "preview_bytes": len(runtime.previews.get(uuid, b"")),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: VisionectConfigEntry
) -> dict[str, Any]:
    runtime = entry.runtime_data
    return async_redact_data(
        {
            "entry": {"data": dict(entry.data), "options": dict(entry.options)},
            "listener": _listener(entry),
            "devices": {
                uuid[:8]: _device(entry, uuid) for uuid in runtime.known_uuids()
            },
        },
        TO_REDACT,
    )


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: VisionectConfigEntry, device: dr.DeviceEntry
) -> dict[str, Any]:
    runtime = entry.runtime_data
    payload: dict[str, Any] = {"listener": _listener(entry)}
    for domain, identifier in device.identifiers:
        if domain != "visionect" or identifier.startswith("listener-"):
            continue
        if identifier in runtime.known_uuids():
            payload["device"] = _device(entry, identifier)
    return async_redact_data(payload, TO_REDACT)
