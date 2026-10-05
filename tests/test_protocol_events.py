"""Library events, replayed from the capture rather than constructed.

Every frame used here is one the sign really sent: a TCLV parameter reply, a
NACK, and the two connect reasons the capture contains. The point of going
through the socket for these is that the mapping from bytes to Home Assistant
state is exactly where a mock would be most tempting and least useful.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.visionect.const import (
    EVENT_COMMAND_NACKED,
    EVENT_DEVICE_CONNECTED,
    EVENT_PUSH_COMPLETED,
    TCLV_HEARTBEAT,
)

from .conftest import DEVICE_UUID, FakeSign

#: Indices into the capture's device -> server frames.
FRAME_STATUS = 0
FRAME_PARAM_REPLY = 5
FRAME_NACK = 9
FRAME_FIRST_CONTACT = 10  # ConnectReason 5 rather than 3
PREFIX = "visionect_sign_00112233"


async def test_a_captured_parameter_reply_corrects_our_view(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """The device, not our optimism, has the last word on a TCLV value."""
    runtime = setup_integration.runtime_data

    # Ask for something else first, so there is optimism to overrule.
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": f"number.{PREFIX}_heartbeat_interval", "value": 42},
        blocking=True,
    )
    await sign.pump(hass)
    assert hass.states.get(f"number.{PREFIX}_heartbeat_interval").state == "42.0"

    # The sign replies with what it really holds: one minute.
    await sign.status(hass, FRAME_PARAM_REPLY)
    assert runtime.tclv_cache[DEVICE_UUID][TCLV_HEARTBEAT] == 1
    assert hass.states.get(f"number.{PREFIX}_heartbeat_interval").state == "1.0"


async def test_a_captured_nack_is_reported_without_a_push_in_flight(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """The capture's NACK names no packet of ours, and must not blow up."""
    events = async_capture_events(hass, EVENT_COMMAND_NACKED)
    await sign.status(hass, FRAME_NACK)
    assert len(events) == 1
    assert events[0].data["uuid"] == DEVICE_UUID
    assert events[0].data["charging"] is False
    assert events[0].data["error_code"] == 0x008A0000
    assert setup_integration.runtime_data.record(DEVICE_UUID).failed_pushes == 0


async def test_a_push_fires_its_event_with_the_checksum_the_sign_will_echo(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """Logging the raw checksum would make a forced push look like a bug.

    A forced push perturbs ImageHeader.Checksum deliberately, to buy one
    guaranteed full-screen redraw, and it is the perturbed value the device
    echoes back as DisplayStateCRC.
    """
    events = async_capture_events(hass, EVENT_PUSH_COMPLETED)
    runtime = setup_integration.runtime_data

    await hass.services.async_call(
        "visionect",
        "refresh",
        {"entity_id": f"image.{PREFIX}_screen"},
        blocking=True,
    )
    await sign.pump(hass)

    assert events, "a push that was acked should announce itself"
    assert events[-1].data["uuid"] == DEVICE_UUID
    assert events[-1].data["checksum"] == (
        runtime.device_state(DEVICE_UUID).pushed_checksum
    )


async def test_connect_reason_comes_from_the_packet(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    port: int,
    device_frames: list[bytes],
) -> None:
    """The capture holds two reasons; the event carries whichever arrived."""
    events = async_capture_events(hass, EVENT_DEVICE_CONNECTED)
    client = FakeSign("127.0.0.1", port, device_frames)
    await client.connect()
    await client.status(hass, FRAME_FIRST_CONTACT)
    try:
        assert len(events) == 1
        reason = events[0].data["connect_reason"]
        assert reason and reason != "heartbeat"
        # sensor.connect_reason exists but is disabled by default, so the
        # decoded field is what to assert on.
        snapshot = setup_integration.runtime_data.coordinator.snapshot(DEVICE_UUID)
        assert snapshot.fields["ConnectReason"]["name"] == reason
    finally:
        await client.pump(hass)
        await client.close()
        await hass.async_block_till_done()


async def test_the_free_restart_check_re_asserts_a_hijacked_screen(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    port: int,
    device_frames: list[bytes],
) -> None:
    """Done once per run: if the sign shows something we did not send, re-send.

    "Something else changed it" is a real case -- the vendor's own server, or a
    previous install. Once only, so a sign that never confirms cannot put the
    integration into a push loop.
    """
    entry = setup_integration
    runtime = entry.runtime_data

    client = FakeSign("127.0.0.1", port, device_frames)
    await client.connect()
    await client.status(hass)
    await client.pump(hass)
    try:
        record = runtime.record(DEVICE_UUID)
        state = runtime.device_state(DEVICE_UUID)
        assert state.pushed_checksum is not None
        assert record.needs_push is False

        # Next run: the sign reports a checksum that is not ours.
        record.checked_crc_this_run = False
        before = record.want_revision
        await client.status(hass, 1)
        assert runtime.record(DEVICE_UUID).want_revision == before + 1

        # And exactly once -- the flag is now set for the rest of the run.
        after = runtime.record(DEVICE_UUID).want_revision
        await client.status(hass, 2)
        assert runtime.record(DEVICE_UUID).want_revision in (after, after)
    finally:
        await client.pump(hass)
        await client.close()
        await hass.async_block_till_done()


async def test_a_deleted_sign_that_dials_in_again_is_a_sign_again(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """The protocol gives no way to turn a device away, and the UI says so."""
    entry = setup_integration
    runtime = entry.runtime_data
    await runtime.async_forget(DEVICE_UUID)
    await hass.async_block_till_done()
    assert runtime.known_uuids() == []
    # A stray property read must not resurrect it.
    assert runtime.record(DEVICE_UUID).uuid == DEVICE_UUID
    assert runtime.known_uuids() == []

    await sign.status(hass, 1)
    assert runtime.known_uuids() == [DEVICE_UUID]


async def test_a_protocol_violation_does_not_take_the_listener_down(
    hass: HomeAssistant, setup_integration: MockConfigEntry, port: int
) -> None:
    """Rubbish on the port is a thing that happens; it must stay a log line."""
    import asyncio

    runtime = setup_integration.runtime_data
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    await writer.drain()
    for _ in range(20):
        await asyncio.sleep(0)
        await hass.async_block_till_done()
    writer.close()
    import contextlib

    with contextlib.suppress(Exception):
        await asyncio.wait_for(writer.wait_closed(), 2)
    await hass.async_block_till_done()

    assert runtime.listener_running is True
    assert runtime.server.stats.accepted >= 1
