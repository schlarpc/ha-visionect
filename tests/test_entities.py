"""Entities are created from the status packet, not from a static list.

That is a deliberate design choice and it is the thing most likely to be
"tidied" into a static list by someone who does not know why. A sign that
never reports humidity must not get a permanently-unavailable humidity sensor,
and a field that only appears on a later heartbeat -- a touch count after the
first touch -- must still get its entity when it does.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from .conftest import DEVICE_UUID, FakeSign

#: Status tags, from pyvisionect's own table.
TAG_BATTERY_LEVEL = 10
TAG_CHARGING_STATUS = 11
TAG_EPD_TEMPERATURE = 45
TAG_IMAGE_PUSH_ALLOWED = 54
TAG_BSSID = 91

PREFIX = "visionect_sign_00112233"


def entity_ids(hass: HomeAssistant, entry: MockConfigEntry) -> set[str]:
    registry = er.async_get(hass)
    return {
        entity.entity_id
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
    }


async def test_no_sign_means_only_the_listener(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Before any sign has spoken there is nothing to describe but the listener."""
    ids = entity_ids(hass, setup_integration)
    assert ids == {
        "sensor.visionect_listener_accepted_connections",
        "sensor.visionect_listener_identified_signs",
        "sensor.visionect_listener_known_signs",
        "sensor.visionect_listener_bind_address",
    }


async def test_one_status_packet_creates_the_sign(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    ids = entity_ids(hass, setup_integration)
    # Every platform is represented, from one packet.
    for expected in (
        f"binary_sensor.{PREFIX}_connected",
        f"binary_sensor.{PREFIX}_charging",
        f"binary_sensor.{PREFIX}_display_out_of_sync",
        f"binary_sensor.{PREFIX}_pending_changes",
        f"button.{PREFIX}_update_now",
        f"image.{PREFIX}_screen",
        f"number.{PREFIX}_heartbeat_interval",
        f"select.{PREFIX}_dither_mode",
        f"sensor.{PREFIX}_battery",
        f"sensor.{PREFIX}_signal_strength",
        f"sensor.{PREFIX}_temperature",
        f"sensor.{PREFIX}_panel_temperature",
        f"sensor.{PREFIX}_access_point",
        f"sensor.{PREFIX}_next_contact",
    ):
        assert expected in ids, expected


async def test_values_come_from_the_captured_packet(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """Spot-check the decode, including the one that is easy to get backwards."""
    snapshot = setup_integration.runtime_data.coordinator.snapshot(DEVICE_UUID)
    assert snapshot is not None

    battery = hass.states.get(f"sensor.{PREFIX}_battery")
    assert battery.state == str(snapshot.fields["BatteryLevel"])

    # SignalStrength is a dBm *magnitude* on the wire and the library negates
    # it. A positive state here would be the bug.
    rssi = hass.states.get(f"sensor.{PREFIX}_signal_strength")
    assert float(rssi.state) == float(snapshot.fields["SignalStrength"])
    assert float(rssi.state) < 0

    connected = hass.states.get(f"binary_sensor.{PREFIX}_connected")
    assert connected.state == "on"


async def test_an_absent_field_creates_no_entity(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    port: int,
    device_frames: list[bytes],
) -> None:
    """The whole point of the design, pinned.

    The status packet is the captured one with five records removed, so this is
    a real packet from a sign that simply does not report those things.
    """
    entry = setup_integration
    client = FakeSign("127.0.0.1", port, device_frames)
    await client.connect()
    await client.send_raw(
        client.status_frame(
            drop={
                TAG_BATTERY_LEVEL,
                TAG_CHARGING_STATUS,
                TAG_EPD_TEMPERATURE,
                TAG_IMAGE_PUSH_ALLOWED,
                TAG_BSSID,
            }
        )
    )
    await client.drain(hass)
    try:
        snapshot = entry.runtime_data.coordinator.snapshot(DEVICE_UUID)
        assert snapshot is not None
        for absent in (
            "BatteryLevel",
            "ChargingStatus",
            "EPDTemperatureSensor",
            "ImagePushAllowed",
            "BSSID",
        ):
            assert absent not in snapshot.fields

        ids = entity_ids(hass, entry)
        for gone in (
            f"sensor.{PREFIX}_battery",
            f"sensor.{PREFIX}_panel_temperature",
            f"sensor.{PREFIX}_access_point",
            f"binary_sensor.{PREFIX}_charging",
            f"binary_sensor.{PREFIX}_image_push_blocked",
        ):
            assert gone not in ids, gone

        # The ungated ones are still there, so this is absence and not failure.
        assert f"sensor.{PREFIX}_signal_strength" in ids
        assert f"binary_sensor.{PREFIX}_connected" in ids
        assert f"button.{PREFIX}_refresh_display" in ids
    finally:
        await client.pump(hass)
        await client.close()
        await hass.async_block_till_done()


async def test_a_field_that_appears_later_still_gets_an_entity(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    port: int,
    device_frames: list[bytes],
) -> None:
    """Fields can turn up on a later heartbeat, so _add re-runs on every packet."""
    entry = setup_integration
    client = FakeSign("127.0.0.1", port, device_frames)
    await client.connect()
    await client.send_raw(client.status_frame(drop={TAG_BATTERY_LEVEL}))
    await client.drain(hass)
    try:
        assert f"sensor.{PREFIX}_battery" not in entity_ids(hass, entry)

        # The full packet, as captured. The battery is reported this time.
        await client.status(hass)
        assert f"sensor.{PREFIX}_battery" in entity_ids(hass, entry)
        assert hass.states.get(f"sensor.{PREFIX}_battery").state not in (
            None,
            STATE_UNAVAILABLE,
        )
    finally:
        await client.pump(hass)
        await client.close()
        await hass.async_block_till_done()


async def test_a_sign_stays_available_while_it_sleeps(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    sign: FakeSign,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Availability tracks the transport, not the clock.

    A sleeping sign is doing exactly what it was told to do. Hiding its last
    battery reading behind `unavailable` throws away the only data there is and
    fills the recorder with gaps; a one-hour sign would spend 59 minutes of
    every hour unavailable.
    """
    runtime = setup_integration.runtime_data
    await sign.disconnect(hass, runtime)
    assert runtime.socket_open(DEVICE_UUID) is False
    battery = hass.states.get(f"sensor.{PREFIX}_battery")
    assert battery.state != STATE_UNAVAILABLE

    # "No socket" is a value for a connection property, not missing data -- and
    # nothing in the protocol announces a closed socket, so the minute tick is
    # what notices. Without it the sensor would claim a live connection until
    # the sign next called, which on an hourly heartbeat is an hour.
    assert hass.states.get(f"binary_sensor.{PREFIX}_connected").state == "on"
    freezer.tick(timedelta(minutes=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(f"binary_sensor.{PREFIX}_connected").state == "off"


async def test_entities_go_unavailable_when_the_listener_stops(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    entry = setup_integration
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(f"sensor.{PREFIX}_battery").state == STATE_UNAVAILABLE


async def test_the_select_entities_are_optimistic_and_bump_the_revision(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    runtime = setup_integration.runtime_data
    before = runtime.record(DEVICE_UUID).want_revision

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": f"select.{PREFIX}_dither_mode", "option": "floyd_steinberg"},
        blocking=True,
    )
    assert runtime.record(DEVICE_UUID).dither == "floyd_steinberg"
    assert runtime.record(DEVICE_UUID).want_revision > before
    assert hass.states.get(f"select.{PREFIX}_dither_mode").state == "floyd_steinberg"


async def test_the_heartbeat_number_writes_and_reads_back(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """The write is queued, then read back, so the device has the last word."""
    from pyvisionect.packets import ParamPacket

    from custom_components.visionect.const import TCLV_HEARTBEAT

    sign.sent.clear()
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": f"number.{PREFIX}_heartbeat_interval", "value": 15},
        blocking=True,
    )
    await sign.pump(hass)

    params = sign.sent_of_type(ParamPacket)
    assert params, "the parameter write should have reached the sign"
    ids = {item.id for packet in params for item in packet.items}
    assert TCLV_HEARTBEAT in ids
    # The optimistic value shows immediately; the read-back is what corrects it.
    assert hass.states.get(f"number.{PREFIX}_heartbeat_interval").state == "15.0"


async def test_the_screen_image_entity_serves_the_preview(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """The preview is the post-dither image, which is the honest one."""
    runtime = setup_integration.runtime_data
    preview = runtime.previews.get(DEVICE_UUID)
    assert preview, "the first-contact placeholder push should have made one"
    assert preview.startswith(b"\x89PNG")

    state = hass.states.get(f"image.{PREFIX}_screen")
    assert state is not None
    assert state.state != STATE_UNAVAILABLE
