"""Reading the device's own flash.

The sign's filesystem has no list opcode -- a listing is a read of ``"."`` --
and no seek, so a lost reply costs the whole transfer. It answers about 1 KiB
at a time at roughly 2.3 KiB/s, which makes one of its stored frames a one- to
nine-minute conversation. That shape is why the integration drives it with its
own sequencer rather than through ``apply_pending``, and why only one read per
sign is allowed at a time: two readers would interleave their 1 KiB chunks into
each other's buffers.

The fake sign here answers that conversation for real rather than having
``read_device_file`` patched out, because the sequencing is the part worth
testing.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.visionect.const import DOMAIN, EVENT_FILES_LISTED
from custom_components.visionect.runtime import uuid_to_bytes

from .conftest import DEVICE_UUID, FakeSign

#: Not real frames -- the decode path is tested by what it does with bytes
#: that are not a frame, which is the realistic failure.
FILES = {
    "/image0.pv2": bytes(range(256)) * 10,
    "/image1.pv2": b"x" * 3000,
}


@pytest.fixture
async def filesystem(
    hass: HomeAssistant, sign: FakeSign
) -> AsyncIterator[FakeSign]:
    task = await sign.serve_files(hass, FILES)
    try:
        yield sign
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_a_listing_is_a_read_of_a_dot(
    hass: HomeAssistant, setup_integration: MockConfigEntry, filesystem: FakeSign
) -> None:
    from pytest_homeassistant_custom_component.common import async_capture_events

    runtime = setup_integration.runtime_data
    events = async_capture_events(hass, EVENT_FILES_LISTED)

    listing = await runtime.async_list_device_files(DEVICE_UUID)
    assert set(listing) == set(FILES)
    assert listing["/image0.pv2"]["size"] == len(FILES["/image0.pv2"])
    # The checksum column reads 0 for every file on this firmware, so it
    # identifies nothing and the integration must not build on it.
    assert listing["/image0.pv2"]["checksum"] == "0"

    assert len(events) == 1
    assert runtime.file_listings[DEVICE_UUID] == listing


async def test_the_list_service_answers_now_when_the_sign_is_here(
    hass: HomeAssistant, setup_integration: MockConfigEntry, filesystem: FakeSign
) -> None:
    """One round trip, well under a second, so do it rather than report queued."""
    from homeassistant.helpers import device_registry as dr

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, DEVICE_UUID), setup_integration.entry_id
    )
    response = await hass.services.async_call(
        DOMAIN,
        "list_device_files",
        {"device_id": device.id},
        blocking=True,
        return_response=True,
    )
    result = response["results"][0]
    assert result["queued"] is False
    assert set(result["files"]) == set(FILES)


async def test_a_file_is_read_a_kilobyte_at_a_time(
    hass: HomeAssistant, setup_integration: MockConfigEntry, filesystem: FakeSign
) -> None:
    runtime = setup_integration.runtime_data
    result = await runtime.async_read_device_file(
        DEVICE_UUID, "/image0.pv2", decode=False, timeout=5.0, attempts=1
    )
    assert result["filename"] == "/image0.pv2"
    assert result["size"] == len(FILES["/image0.pv2"])
    assert result["bytes_read"] == result["size"]
    # The measured throughput is reported, because "this took four minutes"
    # is the thing a caller needs to be able to say.
    assert result["rate_kib_s"] > 0
    assert "duration" in result


async def test_a_file_that_is_not_a_frame_is_reported_not_raised(
    hass: HomeAssistant, setup_integration: MockConfigEntry, filesystem: FakeSign
) -> None:
    """On this firmware the six .pv2 files are the vendor's demo screens.

    They are real files and they are not our frames, so a decode failure is
    information rather than an error.
    """
    runtime = setup_integration.runtime_data
    result = await runtime.async_read_device_file(
        DEVICE_UUID, "/image1.pv2", timeout=5.0, attempts=1
    )
    assert result["decoded"] is False
    assert result["decode_error"]
    assert result["bytes_read"] == len(FILES["/image1.pv2"])


async def test_an_unknown_filename_is_a_validation_error_listing_the_real_ones(
    hass: HomeAssistant, setup_integration: MockConfigEntry, filesystem: FakeSign
) -> None:
    runtime = setup_integration.runtime_data
    with pytest.raises(ServiceValidationError, match="/image0.pv2"):
        await runtime.async_read_device_file(
            DEVICE_UUID, "/nope.pv2", timeout=5.0, attempts=1
        )


async def test_a_refused_open_surfaces_the_refusal(
    hass: HomeAssistant, setup_integration: MockConfigEntry, filesystem: FakeSign
) -> None:
    """The sign NACKs an open of a file it does not have.

    Reached by lying to the integration about the listing, which is what a
    stale listing would do in the wild.
    """
    runtime = setup_integration.runtime_data
    runtime.file_listings[DEVICE_UUID] = {
        "/gone.pv2": {"size": 1024, "checksum": "0"}
    }
    with pytest.raises(HomeAssistantError, match="refused the open"):
        await runtime.async_read_device_file(
            DEVICE_UUID, "/gone.pv2", timeout=5.0, attempts=1
        )


async def test_only_one_read_per_sign_at_a_time(
    hass: HomeAssistant, setup_integration: MockConfigEntry, filesystem: FakeSign
) -> None:
    """Two readers would interleave their chunks into each other's buffers."""
    runtime = setup_integration.runtime_data
    await runtime.async_list_device_files(DEVICE_UUID)

    first = asyncio.ensure_future(
        runtime.async_read_device_file(
            DEVICE_UUID, "/image0.pv2", decode=False, timeout=5.0, attempts=1
        )
    )
    await asyncio.sleep(0)
    with pytest.raises(HomeAssistantError, match="already reading"):
        await runtime.async_read_device_file(
            DEVICE_UUID, "/image1.pv2", decode=False, timeout=5.0, attempts=1
        )
    assert (await first)["bytes_read"] == len(FILES["/image0.pv2"])


async def test_a_queued_listing_is_drained_by_the_reconciler(
    hass: HomeAssistant, setup_integration: MockConfigEntry, filesystem: FakeSign
) -> None:
    """SLOT_FRAMEBUFFER_READ is a conversation, not one packet.

    ``apply_pending`` would only ever send the opening ``open``, which is why
    the file listing silently came back empty before and why the reconciler
    drives this slot itself. With the sign on the line the queue is drained
    immediately, which is the common case on a mains-powered sign.
    """
    runtime = setup_integration.runtime_data
    runtime.async_queue_file_list(DEVICE_UUID)
    # Real sleeps, not bare yields: the fake sign's reader waits on a real
    # timeout, so a tight yield loop would starve it rather than let it answer.
    for _ in range(60):
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()
        if runtime.file_listings.get(DEVICE_UUID):
            break
    assert set(runtime.file_listings[DEVICE_UUID]) == set(FILES)
    assert runtime.store.queue(uuid_to_bytes(DEVICE_UUID)).framebuffer_read is None


async def test_a_listing_asked_for_while_the_sign_is_away_waits_in_the_queue(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """Nothing can wake the sign, so the request is deferred and said to be."""
    runtime = setup_integration.runtime_data
    await sign.disconnect(hass, runtime)

    runtime.async_queue_file_list(DEVICE_UUID)
    work = runtime.store.queue(uuid_to_bytes(DEVICE_UUID))
    assert work.framebuffer_read == "list"
    assert "list device files" in runtime.pending_descriptions(DEVICE_UUID)
    assert runtime.has_pending(DEVICE_UUID) is True
    assert (
        hass.states.get("binary_sensor.visionect_sign_00112233_pending_changes").state
        == "on"
    )


async def test_the_device_file_image_entity_is_fed_by_a_decode(
    hass: HomeAssistant, setup_integration: MockConfigEntry, filesystem: FakeSign
) -> None:
    """It stays empty until a read decodes, which is the honest default.

    What comes off this firmware is not a live framebuffer anyway: the six
    .pv2 files are the vendor's shipped demo screens and do not change when we
    push, so DisplayStateCRC remains the only way to ask the sign what it is
    actually displaying.
    """
    runtime = setup_integration.runtime_data
    assert DEVICE_UUID not in runtime.device_files
    await runtime.async_read_device_file(
        DEVICE_UUID, "/image1.pv2", timeout=5.0, attempts=1
    )
    assert DEVICE_UUID not in runtime.device_files
