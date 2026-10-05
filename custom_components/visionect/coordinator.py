"""A push-only state container.

There is nothing to fetch: the sign dials us, speaks, and goes away.  So the
coordinator is constructed with ``update_interval=None`` -- the documented
pattern for a pushing API -- and fed from the library's event callback with
``async_set_updated_data``.  That buys ``CoordinatorEntity`` and the standard
"entity reads from coordinator.data" shape for free, with no polling.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import DOMAIN

if TYPE_CHECKING:
    from . import VisionectConfigEntry

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class DeviceSnapshot:
    """Everything the entity layer needs about one sign."""

    uuid: str
    fields: dict[str, Any] = field(default_factory=dict)
    raw: dict[int, int] = field(default_factory=dict)
    last_contact: datetime | None = None
    last_push: datetime | None = None
    connections: int = 0
    restored: bool = False
    """True until the device has reported in this run."""

    def get(self, name: str, default: Any = None) -> Any:
        return self.fields.get(name, default)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fields": self.fields,
            "raw": {str(k): v for k, v in self.raw.items()},
            "last_contact": self.last_contact.isoformat() if self.last_contact else None,
            "last_push": self.last_push.isoformat() if self.last_push else None,
            "connections": self.connections,
        }

    @classmethod
    def from_dict(cls, uuid: str, raw: dict[str, Any]) -> DeviceSnapshot:
        def _dt(value: Any) -> datetime | None:
            return dt_util.parse_datetime(value) if value else None

        return cls(
            uuid=uuid,
            fields=dict(raw.get("fields") or {}),
            raw={int(k): v for k, v in (raw.get("raw") or {}).items()},
            last_contact=_dt(raw.get("last_contact")),
            last_push=_dt(raw.get("last_push")),
            connections=int(raw.get("connections") or 0),
            restored=True,
        )


class VisionectCoordinator(DataUpdateCoordinator[dict[str, DeviceSnapshot]]):
    """One per config entry. Never polls."""

    def __init__(self, hass: HomeAssistant, entry: VisionectConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=None,  # arms no timer
        )
        # A never-refreshed coordinator starts last_update_success=True with
        # data=None, which would make every entity available with nothing in
        # it. snapcast fixes it here rather than in every entity.
        self.last_update_success = False
        self.data = {}

    @callback
    def async_set_snapshot(self, snapshot: DeviceSnapshot) -> None:
        """Publish a new snapshot for one sign. Already on the event loop."""
        self.async_set_updated_data({**self.data, snapshot.uuid: snapshot})

    @callback
    def async_seed(self, snapshots: dict[str, DeviceSnapshot]) -> None:
        """Install restored snapshots without claiming a successful update."""
        self.data = dict(snapshots)

    def snapshot(self, uuid: str) -> DeviceSnapshot | None:
        return (self.data or {}).get(uuid)
