"""Setup, unload and the failure modes that matter.

Unload gets the most attention here on purpose. The hazard in this integration
is not a failed setup -- it is a *half-finished teardown*: Home Assistant caps
an unload callback at ten seconds, and a listener teardown that gets cut short
leaves the port bound and the next bind failing. The real bug this suite was
written after was exactly that, a hang in ``close_clients()`` with a sign
holding its socket open, so the sign is connected for the unload tests rather
than politely absent.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
from datetime import timedelta
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from pyvisionect.wire.errors import ListenError

from custom_components.visionect.const import (
    DOMAIN,
    ISSUE_NO_DEVICE,
    NO_DEVICE_GRACE,
)

from .conftest import DEVICE_UUID, FakeSign, free_port


async def test_setup_binds_and_registers_the_listener(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    entry = setup_integration
    assert entry.state is ConfigEntryState.LOADED
    runtime = entry.runtime_data
    assert runtime.listener_running is True

    # Something is really listening on the port.
    reader, writer = await asyncio.open_connection("127.0.0.1", entry.data[CONF_PORT])
    writer.close()
    await writer.wait_closed()

    # The listener is its own service device, so its counters have somewhere
    # to live before any sign exists.
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert [d.name for d in devices] == ["Visionect listener"]
    assert hass.states.get("sensor.visionect_listener_bind_address").state == (
        f"127.0.0.1:{entry.data[CONF_PORT]}"
    )


async def test_services_are_registered_once(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Registered from async_setup, so they exist per integration, not per entry."""
    for name in (
        "display_image",
        "display_text",
        "set_content_source",
        "clear_content",
        "update_now",
        "refresh",
        "clear_screen",
        "ghost_clear",
        "read_parameters",
        "write_parameters",
        "list_device_files",
        "read_device_file",
    ):
        assert hass.services.has_service(DOMAIN, name), name


async def test_unload_releases_the_port_with_a_sign_still_connected(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """The teardown path, with the thing that used to make it hang.

    A sign on mains holds one TCP session open for hours. ``wait_closed()`` on
    a server with a live client never returns on its own, which is why the
    shutdown does close/close_clients/wait_closed in that order and aborts
    rather than waiting for a half-written 1.84 MB frame.
    """
    entry = setup_integration
    port = entry.data[CONF_PORT]
    assert entry.runtime_data.socket_open(DEVICE_UUID)

    async with asyncio.timeout(10):
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED

    # The port is genuinely free again -- the assertion the old hang failed.
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", port)
    server.close()
    await server.wait_closed()


async def test_unload_then_setup_again(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    entry = setup_integration
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.listener_running


async def test_setup_retries_when_the_port_is_taken(
    hass: HomeAssistant, config_entry: MockConfigEntry, socket_enabled: None
) -> None:
    """EADDRINUSE must be ConfigEntryNotReady, not a traceback.

    It is plausibly transient -- a reload race, a lingering socket, another
    instance shutting down -- so Home Assistant should retry rather than ask
    the user to fix something.
    """
    assert await async_setup_component(hass, "homeassistant", {})
    port = config_entry.data[CONF_PORT]
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", port)
    try:
        config_entry.add_to_hass(hass)
        assert not await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        assert config_entry.state is ConfigEntryState.SETUP_RETRY
        assert config_entry.error_reason_translation_key == "port_in_use"
    finally:
        server.close()
        await server.wait_closed()

    # The retry succeeds now that the port is free.
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize("code", [errno.EACCES, errno.EADDRNOTAVAIL])
async def test_setup_fails_permanently_for_an_unfixable_bind(
    hass: HomeAssistant, config_entry: MockConfigEntry, code: int
) -> None:
    """Waiting will not fix a privileged port or an address that is not here."""
    assert await async_setup_component(hass, "homeassistant", {})
    config_entry.add_to_hass(hass)
    with patch(
        "pyvisionect.io.VisionectServer.start",
        side_effect=ListenError("127.0.0.1", 1, OSError(code, "no")),
    ):
        assert not await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert config_entry.error_reason_translation_key == "cannot_bind"


async def test_a_bind_error_with_no_errno_is_retried(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """asyncio discards the per-address errno when every candidate fails.

    ``errno is None`` is therefore not a bug, and the only safe reading of it
    is "retry" -- treating it as permanent would strand a listener on a
    transient failure.
    """
    assert await async_setup_component(hass, "homeassistant", {})
    config_entry.add_to_hass(hass)
    with patch(
        "pyvisionect.io.VisionectServer.start",
        side_effect=ListenError("::", 1, OSError("could not bind on any address")),
    ):
        assert not await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


# ----------------------------------------------------------- the silent failure


async def test_the_no_device_issue(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The dominant failure here is silent: nothing errors, nothing connects.

    So it is raised as a repair issue after a grace period, and the issue says
    which of the two cases it is -- "nothing reached the port" needs different
    advice from "something connected but was not a sign".
    """
    entry = setup_integration
    issue_id = f"{ISSUE_NO_DEVICE}_{entry.entry_id}"
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, issue_id) is None

    freezer.tick(NO_DEVICE_GRACE + timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_placeholders["detail"] == "nothing_reached_the_port"
    assert issue.translation_placeholders["port"] == str(entry.data[CONF_PORT])


@pytest.fixture
def instant_no_device_grace() -> None:
    """Fire the no-device check on the next loop iteration, not in ten minutes.

    Deliberately not freezegun. The fake sign drives real sockets with real
    ``asyncio.wait_for`` timeouts, and a frozen monotonic clock stops those
    expiring at all -- the test hangs rather than fails, which took a while to
    work out. Shortening the grace period keeps the real clock and still
    exercises the real timer.
    """
    with patch(
        "custom_components.visionect.runtime.NO_DEVICE_GRACE", timedelta(seconds=0)
    ):
        yield


async def test_the_no_device_issue_clears_when_a_sign_arrives(
    hass: HomeAssistant,
    instant_no_device_grace: None,
    setup_integration: MockConfigEntry,
    port: int,
    device_frames: list[bytes],
) -> None:
    entry = setup_integration
    issue_id = f"{ISSUE_NO_DEVICE}_{entry.entry_id}"
    await asyncio.sleep(0)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    client = FakeSign("127.0.0.1", port, device_frames)
    await client.connect()
    await client.status(hass)
    try:
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    finally:
        await client.pump(hass)
        await client.close()
        await hass.async_block_till_done()


async def test_a_silent_connection_is_reported_differently(
    hass: HomeAssistant,
    instant_no_device_grace: None,
    setup_integration: MockConfigEntry,
    port: int,
) -> None:
    """Something opened the port and said nothing -- a port scan, or a proxy.

    It needs different advice from "nothing reached the port at all", so the
    issue carries which case it is.
    """
    entry = setup_integration
    runtime = entry.runtime_data
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    for _ in range(10):
        await asyncio.sleep(0)
        await hass.async_block_till_done()
        if runtime.server.stats.accepted:
            break
    assert runtime.server.stats.accepted == 1
    assert runtime.server.stats.identified == 0

    # The grace timer was armed at setup with a zero delay, so re-arm it now
    # that there is something to report.
    runtime._schedule_no_device_check()  # noqa: SLF001
    await asyncio.sleep(0)
    await hass.async_block_till_done()

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_NO_DEVICE}_{entry.entry_id}"
    )
    assert issue is not None
    assert issue.translation_placeholders["detail"] == "connected_but_not_a_sign"

    writer.close()
    with contextlib.suppress(ConnectionResetError, BrokenPipeError, TimeoutError):
        await asyncio.wait_for(writer.wait_closed(), 2)
    await hass.async_block_till_done()


# ------------------------------------------------------------- device removal


async def test_removing_a_sign_forgets_it_rather_than_banning_it(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """The protocol has no concept of an unwanted device.

    A deleted sign dials in again and must get its entities back, which is the
    bit the "already added" set in each platform would otherwise prevent.
    """
    entry = setup_integration
    runtime = entry.runtime_data
    registry = dr.async_get(hass)
    device = registry.async_get_device_by_identifier((DOMAIN, DEVICE_UUID), entry.entry_id)
    assert device is not None

    from custom_components.visionect import async_remove_config_entry_device

    assert await async_remove_config_entry_device(hass, entry, device)
    await hass.async_block_till_done()
    assert runtime.known_uuids() == []
    assert runtime.coordinator.snapshot(DEVICE_UUID) is None

    # It comes back on its next contact.
    await sign.status(hass, 1)
    assert runtime.known_uuids() == [DEVICE_UUID]
    assert hass.states.get("binary_sensor.visionect_sign_00112233_connected") is not None


async def test_removing_the_listener_device_touches_no_sign(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    entry = setup_integration
    registry = dr.async_get(hass)
    listener = registry.async_get_device_by_identifier(
        (DOMAIN, f"listener-{entry.entry_id}"), entry.entry_id
    )
    from custom_components.visionect import async_remove_config_entry_device

    assert await async_remove_config_entry_device(hass, entry, listener)
    assert entry.runtime_data.known_uuids() == [DEVICE_UUID]


# ------------------------------------------------------------------ restore


async def test_a_restart_restores_the_sign_without_a_push(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """The point of persisting the frame state is a free restart."""
    entry = setup_integration
    runtime = entry.runtime_data
    await runtime.async_save()
    record = runtime.record(DEVICE_UUID)
    assert record.want_revision == record.pushed_revision

    await sign.close()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime = entry.runtime_data
    # Restored before the sign has said anything this run, and deliberately
    # not "available" yet in the coordinator's eyes.
    assert runtime.known_uuids() == [DEVICE_UUID]
    snapshot = runtime.coordinator.snapshot(DEVICE_UUID)
    assert snapshot is not None and snapshot.restored is True
    assert runtime.device_state(DEVICE_UUID).imaging_state is not None
    restored = runtime.record(DEVICE_UUID)
    assert restored.needs_push is False
