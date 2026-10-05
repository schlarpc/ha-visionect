"""The entity base: where the availability model lives."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import DeviceSnapshot, VisionectCoordinator

if TYPE_CHECKING:
    from .runtime import DeviceRecord, VisionectRuntime


class VisionectEntity(CoordinatorEntity[VisionectCoordinator]):
    """One sign's entity. Availability tracks the transport, not the clock."""

    _attr_has_entity_name = True

    def __init__(self, runtime: VisionectRuntime, uuid: str) -> None:
        super().__init__(runtime.coordinator)
        self.runtime = runtime
        self._uuid = uuid
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, uuid)})

    @property
    def snapshot(self) -> DeviceSnapshot | None:
        return self.coordinator.snapshot(self._uuid)

    @property
    def record(self) -> DeviceRecord:
        return self.runtime.record(self._uuid)

    @property
    def available(self) -> bool:
        """Available once we have ever heard from this sign and the listener is up.

        Deliberately **not** a function of time since the last contact. A sign
        that is asleep is doing exactly what it was told to do; hiding its last
        known battery reading behind `unavailable` throws away the only data we
        have and fills the recorder with gaps. A 1-hour sign would otherwise
        spend 59 minutes of every hour unavailable.

        Precedent: zwave_js (an asleep node is available, a *dead* node is
        available), matter, oralb, xiaomi_ble, shelly. All of them surface
        lifecycle state as a diagnostic sensor instead.
        """
        return self.runtime.listener_running and self.runtime.has_ever_seen(self._uuid)

    @property
    def assumed_state(self) -> bool:
        """True when the sign has missed the window it announced.

        The honest signal: the value is real, but it is older than the device
        itself said it would be. The frontend already renders `assumed_state`
        as "this value may be out of date", which is exactly right. Pairs with
        sensor.last_contact and sensor.next_contact for the actual numbers.
        """
        return self.runtime.is_overdue(self._uuid)


class VisionectLiveEntity(VisionectEntity):
    """For values that are properties of the connection, not device readings.

    "No socket" is a valid *value* for these, not missing data, so they are
    always available and never assumed.
    """

    @property
    def available(self) -> bool:
        return self.runtime.listener_running

    @property
    def assumed_state(self) -> bool:
        return False
