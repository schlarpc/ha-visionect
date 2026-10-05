"""Numbers -- device-side TCLV settings, written optimistically and reconciled.

Setting one of these:

1. HA's state becomes the new value immediately and `pending_changes` goes on;
2. the write is coalesced into the device's pending TCLV map (last-write-wins);
3. on next contact the write goes out, followed by TCLV 53 `CMD_FLASH_SAVE`
   when the entry option says to persist -- without it the write is RAM-only
   and lost at the next reboot, which is a baffling failure for a user;
4. a read of the same id is queued behind it, so the *device* has the last word.

Note that step 3 is still partly unproven: nothing in the vendor server was
ever observed issuing id 53 after a parameter write, and no payload for it is
documented. We issue it with a value of 1 and read the parameter back, which is
the only honest way to find out.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntityDescription,
    NumberMode,
    RestoreNumber,
)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VisionectConfigEntry
from .const import SIGNAL_DEVICE_ADDED, SIGNAL_DEVICE_REMOVED, TCLV_HEARTBEAT
from .entity import VisionectEntity
from .runtime import VisionectRuntime

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class VisionectNumberDescription(NumberEntityDescription):
    tclv_id: int
    status_field: str | None = None
    """A status field that reports the same thing, used before any TCLV read."""


NUMBERS: tuple[VisionectNumberDescription, ...] = (
    VisionectNumberDescription(
        key="heartbeat_interval",
        translation_key="heartbeat_interval",
        tclv_id=TCLV_HEARTBEAT,
        status_field="NextStatus",
        native_min_value=1,
        native_max_value=1440,
        native_step=1,
        mode=NumberMode.BOX,
        device_class=NumberDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        entity_category=EntityCategory.CONFIG,
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
        new = []
        for desc in NUMBERS:
            key = f"{uuid}-{desc.key}"
            if key in known:
                continue
            known.add(key)
            new.append(VisionectNumber(runtime, uuid, desc))
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


class VisionectNumber(VisionectEntity, RestoreNumber):
    entity_description: VisionectNumberDescription

    def __init__(
        self,
        runtime: VisionectRuntime,
        uuid: str,
        description: VisionectNumberDescription,
    ) -> None:
        super().__init__(runtime, uuid)
        self.entity_description = description
        self._attr_unique_id = f"{uuid}-{description.key}"
        self._optimistic: float | None = None
        self._restored: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (data := await self.async_get_last_number_data()) is not None:
            self._restored = data.native_value

    @property
    def native_value(self) -> float | None:
        desc = self.entity_description
        # The device's own report wins over our optimism, once it arrives.
        cached = (self.runtime.tclv_cache.get(self._uuid) or {}).get(desc.tclv_id)
        if isinstance(cached, (int, float)):
            return float(cached)
        if self._optimistic is not None:
            return self._optimistic
        if desc.status_field and (snapshot := self.snapshot) is not None:
            value = snapshot.get(desc.status_field)
            if isinstance(value, (int, float)):
                return float(value)
        return self._restored

    async def async_set_native_value(self, value: float) -> None:
        self._optimistic = value
        self.runtime.async_queue_param_write(
            self._uuid, {self.entity_description.tclv_id: int(value)}
        )
        self.async_write_ha_state()
