"""Sensors. Created from the status packet, not from a static list."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfInformation,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from pyvisionect.devices import CONNECT_REASON, ERROR_CODE

from . import VisionectConfigEntry
from .const import DOMAIN, SIGNAL_DEVICE_ADDED, SIGNAL_DEVICE_REMOVED
from .coordinator import DeviceSnapshot
from .entity import VisionectEntity
from .runtime import VisionectRuntime

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class VisionectSensorDescription(SensorEntityDescription):
    """A sensor fed from one decoded status field, or derived."""

    field: str | None = None
    value_fn: Callable[[Any], StateType | datetime] = lambda v: v
    derived_fn: Callable[[VisionectRuntime, str], StateType | datetime] | None = None


def _enum_name(value: Any) -> StateType:
    if isinstance(value, dict):
        return value.get("name")
    return None


def _boot_time(minutes: Any) -> datetime | None:
    """Tag 15 is in minutes. SensorDeviceClass.UPTIME wants the boot moment.

    The class suppresses small drift between updates for you, which is a
    precise fit for a minute-granularity source that would otherwise jitter on
    every heartbeat.
    """
    if minutes is None:
        return None
    return dt_util.utcnow() - timedelta(minutes=int(minutes))


SENSORS: tuple[VisionectSensorDescription, ...] = (
    # ---- primary -----------------------------------------------------------
    VisionectSensorDescription(
        key="next_contact",
        translation_key="next_contact",
        device_class=SensorDeviceClass.TIMESTAMP,
        derived_fn=lambda rt, uuid: rt.expected_next_contact(uuid),
    ),
    # ---- diagnostic, enabled ----------------------------------------------
    VisionectSensorDescription(
        key="battery",
        field="BatteryLevel",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="%",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VisionectSensorDescription(
        key="battery_voltage",
        translation_key="battery_voltage",
        field="BatteryVoltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.MILLIVOLT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VisionectSensorDescription(
        key="battery_current",
        translation_key="battery_current",
        field="BatteryCurrent",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.MILLIAMPERE,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VisionectSensorDescription(
        key="signal_strength",
        field="SignalStrength",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        entity_category=EntityCategory.DIAGNOSTIC,
        # The wire value is a dBm *magnitude*; the library's RSSI codec already
        # negates it, so value_fn is identity. Getting this wrong is the single
        # most likely bug in the whole integration.
    ),
    VisionectSensorDescription(
        key="temperature",
        field="AverageTemperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VisionectSensorDescription(
        key="last_boot",
        translation_key="last_boot",
        field="DeviceUptime",
        device_class=SensorDeviceClass.UPTIME,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_boot_time,
    ),
    VisionectSensorDescription(
        key="display_updates",
        translation_key="display_updates",
        field="DisplayUpdateCount",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VisionectSensorDescription(
        key="status_reason",
        translation_key="status_reason",
        field="ErrorCode",
        device_class=SensorDeviceClass.ENUM,
        options=sorted(set(ERROR_CODE.values())),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_enum_name,
        # NOT a fault: this is the *reason the packet was sent*. 13 is the
        # device announcing a deep-sleep request, which is normal.
    ),
    VisionectSensorDescription(
        key="last_contact",
        translation_key="last_contact",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        derived_fn=lambda rt, uuid: rt.record(uuid).last_contact,
    ),
    VisionectSensorDescription(
        key="last_push",
        translation_key="last_push",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        derived_fn=lambda rt, uuid: rt.record(uuid).last_push,
    ),
    # ---- diagnostic, disabled by default ----------------------------------
    VisionectSensorDescription(
        key="panel_temperature",
        translation_key="panel_temperature",
        field="EPDTemperatureSensor",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    VisionectSensorDescription(
        key="connect_reason",
        translation_key="connect_reason",
        field="ConnectReason",
        device_class=SensorDeviceClass.ENUM,
        options=sorted(set(CONNECT_REASON.values())),
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_enum_name,
    ),
    VisionectSensorDescription(
        key="filesystem_free",
        translation_key="filesystem_free",
        field="FSFreeSize",
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    VisionectSensorDescription(
        key="mcu_awake_count",
        translation_key="mcu_awake_count",
        field="MCUAwakeCount",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    VisionectSensorDescription(
        key="network_errors",
        translation_key="network_errors",
        field="NetworkErrCount",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    VisionectSensorDescription(
        key="wifi_dtim",
        translation_key="wifi_dtim",
        field="WiFiDTIM",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    VisionectSensorDescription(
        key="access_point",
        translation_key="access_point",
        field="BSSID",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    VisionectSensorDescription(
        key="connections",
        translation_key="connections",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        derived_fn=lambda rt, uuid: rt.record(uuid).connections,
    ),
    VisionectSensorDescription(
        key="failed_pushes",
        translation_key="failed_pushes",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        derived_fn=lambda rt, uuid: rt.record(uuid).failed_pushes,
    ),
)


@dataclass(frozen=True, kw_only=True)
class ListenerSensorDescription(SensorEntityDescription):
    """A counter on the listener itself, not on any sign."""

    value_fn: Callable[[VisionectRuntime], StateType]


LISTENER_SENSORS: tuple[ListenerSensorDescription, ...] = (
    ListenerSensorDescription(
        key="accepted_connections",
        translation_key="accepted_connections",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda rt: rt.server.stats.accepted,
    ),
    ListenerSensorDescription(
        key="identified_signs",
        translation_key="identified_signs",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda rt: rt.server.stats.identified,
    ),
    ListenerSensorDescription(
        key="known_signs",
        translation_key="known_signs",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda rt: len(rt.known_uuids()),
    ),
    ListenerSensorDescription(
        key="bind_address",
        translation_key="bind_address",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda rt: f"{rt.host}:{rt.port}",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VisionectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    known: set[str] = set()

    async_add_entities(
        VisionectListenerSensor(runtime, entry, desc) for desc in LISTENER_SENSORS
    )

    @callback
    def _add(uuid: str) -> None:
        """Add whatever this sign has turned out to report.

        Re-run on every status packet so a field that only appears later (a
        touch panel after the first touch) still gets an entity. Entities are
        never removed.
        """
        snapshot = runtime.coordinator.snapshot(uuid)
        seen = set(snapshot.fields) if snapshot else set()
        seen |= runtime.record(uuid).seen_fields
        new = []
        for desc in SENSORS:
            key = f"{uuid}-{desc.key}"
            if key in known:
                continue
            if desc.field is not None and desc.field not in seen:
                continue
            known.add(key)
            new.append(VisionectSensor(runtime, uuid, desc))
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


class VisionectSensor(VisionectEntity, RestoreSensor):
    """A sensor whose value survives a restart.

    RestoreSensor rather than RestoreEntity: the latter stores the *state*,
    which the sensor base may have mangled, not the native value.
    """

    entity_description: VisionectSensorDescription

    def __init__(
        self, runtime: VisionectRuntime, uuid: str, description: VisionectSensorDescription
    ) -> None:
        super().__init__(runtime, uuid)
        self.entity_description = description
        self._attr_unique_id = f"{uuid}-{description.key}"
        self._restored: Any = None

    async def async_added_to_hass(self) -> None:
        # Mandatory on CoordinatorEntity: the base body is what registers the
        # listener. Omitting it produces an entity that never updates and logs
        # nothing.
        await super().async_added_to_hass()
        if (data := await self.async_get_last_sensor_data()) is not None:
            # None covers both "nothing stored" and "stored but unparseable".
            self._restored = data.native_value

    @property
    def native_value(self) -> StateType | datetime:
        desc = self.entity_description
        if desc.derived_fn is not None:
            return desc.derived_fn(self.runtime, self._uuid)
        snapshot: DeviceSnapshot | None = self.snapshot
        if snapshot is None or desc.field not in snapshot.fields:
            return self._restored
        return desc.value_fn(snapshot.fields.get(desc.field))


class VisionectListenerSensor(SensorEntity):
    """A counter on the listener service device."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: ListenerSensorDescription

    def __init__(
        self,
        runtime: VisionectRuntime,
        entry: VisionectConfigEntry,
        description: ListenerSensorDescription,
    ) -> None:
        self.runtime = runtime
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}-{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"listener-{entry.entry_id}")}
        )

    @property
    def available(self) -> bool:
        return self.runtime.listener_running

    @property
    def native_value(self) -> StateType:
        return self.entity_description.value_fn(self.runtime)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.runtime.coordinator.async_add_listener(self.async_write_ha_state)
        )
