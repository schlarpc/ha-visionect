"""Per-device partial (screen-space) updates.

What is being tested here is mostly *restraint*: the feature is off unless
asked for, unavailable unless the hardware was measured taking a rectangle,
and it cannot run indefinitely without a full-screen push to clear the panel.

The ghosting policy is the part that matters. Measured on the hardware, this
firmware clears nothing of its own accord -- across eight accepted partial
pushes and twelve rectangles it never once ran an unrequested clearing refresh
(pyvisionect ``OPEN-QUESTIONS.md`` A10/A12). So the forced full push is not a
precaution; it is the only thing that cleans the glass, and these tests pin it.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.visionect.const import (
    CONF_PARTIAL_MAX_CONSECUTIVE,
    CONF_PARTIAL_UPDATES,
    DEFAULT_PARTIAL_MAX_CONSECUTIVE,
)

from .conftest import DEVICE_UUID, FakeSign


def canvas(runtime: Any, boxes: list[tuple[int, int, int, int]]) -> Any:
    """A canvas-sized white image with black boxes drawn on it."""
    from PIL import Image, ImageDraw

    panel = runtime.panel(DEVICE_UUID)
    image = Image.new("L", (panel.canvas_width, panel.canvas_height), 255)
    draw = ImageDraw.Draw(image)
    for x, y, w, h in boxes:
        draw.rectangle([x, y, x + w - 1, y + h - 1], fill=0)
    return image


async def push(hass: HomeAssistant, sign: FakeSign, runtime: Any, image: Any,
               *, ack: bool = True) -> dict[str, Any]:
    result = await runtime.async_push_image_now(DEVICE_UUID, image)
    await sign.pump(hass, ack=ack)
    return result


async def baseline(hass: HomeAssistant, sign: FakeSign, runtime: Any) -> Any:
    """Get the sign to a known picture, full screen, with the budget at zero.

    Partials start working from the *first* user push, because the integration
    has already put a placeholder frame on the glass by then and so already
    holds a state image to diff against. These tests want a known starting
    point rather than that incidental one, so they force one full push.
    """
    image = canvas(runtime, [(8, 8, 64, 64)])
    runtime.record(DEVICE_UUID).force_next = True
    await push(hass, sign, runtime, image)
    record = runtime.record(DEVICE_UUID)
    assert record.last_frame_full is True
    assert record.consecutive_partials == 0
    return image


def enable_partials(
    hass: HomeAssistant, entry: MockConfigEntry, *, max_consecutive: int | None = None
) -> None:
    options = {**entry.options, CONF_PARTIAL_UPDATES: {DEVICE_UUID: True}}
    if max_consecutive is not None:
        options[CONF_PARTIAL_MAX_CONSECUTIVE] = max_consecutive
    hass.config_entries.async_update_entry(entry, options=options)


# ----------------------------------------------------------------- the gate


async def test_the_two_capability_questions_are_not_the_same(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """``supports_rectangles`` and ``accepts_screen_rectangles`` differ here.

    This is the single most likely way to get the feature wrong, so it is
    asserted rather than assumed: for this sign the vendor's own question
    ("would the Visionect server ever send a rectangle") answers False and the
    measured one ("does the hardware take one") answers True.
    """
    runtime = setup_integration.runtime_data
    state = runtime.device_state(DEVICE_UUID)
    assert state.hardware_name_id == 8
    assert state.supports_rectangles is False
    assert state.accepts_screen_rectangles is True
    assert runtime.partial_capable(DEVICE_UUID) is True


async def test_off_by_default(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    runtime = setup_integration.runtime_data
    assert CONF_PARTIAL_UPDATES not in setup_integration.options
    assert runtime.partial_requested(DEVICE_UUID) is False
    assert runtime.partial_enabled(DEVICE_UUID) is False
    assert runtime.partial_max_consecutive == DEFAULT_PARTIAL_MAX_CONSECUTIVE


async def test_unverified_hardware_is_refused_even_if_the_option_says_yes(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """The option is not the gate; the measured hardware set is.

    An option carried over from a sign that was replaced, or hand-edited into
    .storage, must not start sending rectangles to hardware nobody has tested.
    """
    runtime = setup_integration.runtime_data
    enable_partials(hass, setup_integration)
    await hass.async_block_till_done()
    assert runtime.partial_enabled(DEVICE_UUID) is True

    # Pretend the sign reports an id that was never measured.
    runtime.device_state(DEVICE_UUID).last_status.raw[42] = 1
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            type(runtime.device_state(DEVICE_UUID)),
            "accepts_screen_rectangles",
            property(lambda self: False),
        )
        assert runtime.partial_capable(DEVICE_UUID) is False
        assert runtime.partial_requested(DEVICE_UUID) is True
        assert runtime.partial_enabled(DEVICE_UUID) is False


async def test_turning_it_on_does_not_reload_the_entry(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """Reloading would drop the sign's socket, and it re-dials on its own clock.

    On this firmware that can be the best part of an hour, which for a kitchen
    display is the difference between a setting and an outage.
    """
    entry = setup_integration
    runtime = entry.runtime_data
    enable_partials(hass, entry)
    await hass.async_block_till_done()

    assert entry.runtime_data is runtime
    assert runtime.listener_running
    assert runtime.socket_open(DEVICE_UUID)
    assert runtime.partial_enabled(DEVICE_UUID) is True


# -------------------------------------------------------------- the frames


async def test_full_screen_path_is_untouched_when_off(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    runtime = setup_integration.runtime_data
    sign.sent.clear()
    await push(hass, sign, runtime, canvas(runtime, [(100, 100, 200, 100)]))

    record = runtime.record(DEVICE_UUID)
    assert record.last_frame_full is True
    assert record.last_fallback_reason is None
    assert record.consecutive_partials == 0


async def test_a_small_change_goes_out_as_one_small_rectangle(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    from pyvisionect.packets import ImagePacket

    entry = setup_integration
    runtime = entry.runtime_data
    enable_partials(hass, entry)
    await hass.async_block_till_done()

    await baseline(hass, sign, runtime)
    record = runtime.record(DEVICE_UUID)

    sign.sent.clear()
    await push(
        hass,
        sign,
        runtime,
        canvas(runtime, [(8, 8, 64, 64), (600, 700, 200, 100)]),
    )

    assert record.last_frame_full is False
    assert record.last_fallback_reason is None
    assert record.consecutive_partials == 1

    pushes = sign.sent_of_type(ImagePacket)
    assert len(pushes) == 1
    rects = pushes[0].rectangles
    panel = runtime.panel(DEVICE_UUID)
    full_area = panel.canvas_width * panel.canvas_height
    area = sum(r.width * r.height for r in rects)
    # A partial costs 2x the pixels it strictly needs -- the partner lane's
    # unchanged rows ride along, which is structural -- and must still be a
    # small fraction of the screen.
    assert 0 < area < full_area // 4
    # Every partial rectangle must carry the "normal update" bit, because the
    # firmware refuses a rectangle that also asks for the clearing waveform.
    assert all(r.options & 0x0002 for r in rects)


async def test_the_forced_refresh_fires_at_the_threshold(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """The ghosting budget, which is the whole reason this is safe to ship."""
    entry = setup_integration
    runtime = entry.runtime_data
    enable_partials(hass, entry, max_consecutive=2)
    await hass.async_block_till_done()
    assert runtime.partial_max_consecutive == 2

    await baseline(hass, sign, runtime)
    record = runtime.record(DEVICE_UUID)

    await push(hass, sign, runtime, canvas(runtime, [(8, 8, 64, 64), (40, 400, 8, 8)]))
    assert (record.last_frame_full, record.consecutive_partials) == (False, 1)
    assert runtime.partial_refresh_due(DEVICE_UUID) is False

    await push(hass, sign, runtime, canvas(runtime, [(8, 8, 64, 64), (80, 800, 8, 8)]))
    assert (record.last_frame_full, record.consecutive_partials) == (False, 2)
    assert runtime.partial_refresh_due(DEVICE_UUID) is True

    # Budget spent: the next frame is promoted to full screen, which is what
    # requests the inverse clearing waveform, and the counter resets.
    await push(hass, sign, runtime, canvas(runtime, [(8, 8, 64, 64), (120, 1200, 8, 8)]))
    assert record.last_frame_full is True
    assert record.last_fallback_reason == "ghosting-refresh-due"
    assert record.consecutive_partials == 0
    assert runtime.partial_refresh_due(DEVICE_UUID) is False


async def test_a_whole_new_image_is_not_worth_partialling(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """Many scattered rectangles cost more than one full push, and also clear."""
    entry = setup_integration
    runtime = entry.runtime_data
    enable_partials(hass, entry)
    await hass.async_block_till_done()

    await baseline(hass, sign, runtime)
    await push(
        hass,
        sign,
        runtime,
        canvas(runtime, [(x, y, 48, 48) for x in range(0, 1400, 160)
                         for y in range(0, 2500, 160)]),
    )
    record = runtime.record(DEVICE_UUID)
    assert record.last_frame_full is True
    assert record.last_fallback_reason in {"not-worth-it", "too-many-regions"}
    assert record.consecutive_partials == 0


# ------------------------------------------------------------- the recovery


async def test_a_refused_partial_is_rolled_back(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """A refused push did not draw, so the state image must not claim it did.

    On the full-screen path a wrong state costs one redundant redraw. On the
    partial path every later rectangle carries the partner lane's pixels read
    out of that state image, so a state that claims pixels the glass never drew
    poisons every subsequent partial and the device's echoed DisplayStateCRC can
    never agree again.
    """
    entry = setup_integration
    runtime = entry.runtime_data
    enable_partials(hass, entry)
    await hass.async_block_till_done()

    await baseline(hass, sign, runtime)
    record = runtime.record(DEVICE_UUID)
    await push(hass, sign, runtime, canvas(runtime, [(8, 8, 64, 64), (200, 900, 32, 32)]))
    assert record.consecutive_partials == 1
    good_state = runtime.device_state(DEVICE_UUID).imaging_state
    assert good_state is not None

    # The sign refuses the next one.
    await push(
        hass,
        sign,
        runtime,
        canvas(runtime, [(8, 8, 64, 64), (300, 1300, 64, 64)]),
        ack=False,
    )

    assert runtime.device_state(DEVICE_UUID).imaging_state is good_state
    assert record.consecutive_partials == 1
    # ... and the recovery is a full screen, which re-establishes every pixel
    # and the checksum from first principles.
    assert record.force_next is True
    assert record.failed_pushes == 1


async def test_a_charging_nack_also_rolls_back(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """A charging refusal is a retry-later, but it is still a refusal."""
    entry = setup_integration
    runtime = entry.runtime_data
    enable_partials(hass, entry)
    await hass.async_block_till_done()

    await baseline(hass, sign, runtime)
    good_state = runtime.device_state(DEVICE_UUID).imaging_state

    await runtime.async_push_image_now(
        DEVICE_UUID, canvas(runtime, [(8, 8, 64, 64), (500, 1500, 64, 64)])
    )
    await sign.pump(hass, ack=False, charging=True)

    assert runtime.device_state(DEVICE_UUID).imaging_state is good_state
    assert runtime.record(DEVICE_UUID).force_next is True


# -------------------------------------------------------------- the surface


async def test_the_counter_is_visible(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """A budget nobody can see is a budget nobody will notice being spent."""
    entry = setup_integration
    runtime = entry.runtime_data
    enable_partials(hass, entry, max_consecutive=5)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.visionect_sign_00112233_partials_since_refresh")
    assert state is not None
    assert state.state == "0"

    await baseline(hass, sign, runtime)
    await push(hass, sign, runtime, canvas(runtime, [(8, 8, 64, 64), (90, 900, 64, 64)]))

    state = hass.states.get("sensor.visionect_sign_00112233_partials_since_refresh")
    assert state.state == "1"
    assert state.attributes["enabled"] is True
    assert state.attributes["capable"] is True
    assert state.attributes["max_consecutive_partials"] == 5
    assert state.attributes["refresh_due"] is False
    assert state.attributes["last_frame_full"] is False


async def test_the_counter_sensor_needs_capable_hardware(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """No sign has reported in, so nothing is known to take a rectangle."""
    runtime = setup_integration.runtime_data
    assert runtime.known_uuids() == []
    assert not [
        entity_id
        for entity_id in hass.states.async_entity_ids("sensor")
        if "partials_since_refresh" in entity_id
    ]


async def test_diagnostics_separates_the_two_questions(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    from custom_components.visionect.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    data = await async_get_config_entry_diagnostics(hass, setup_integration)
    device = data["devices"][DEVICE_UUID[:8]]
    assert device["supports_rectangles"] is False
    assert device["accepts_screen_rectangles"] is True
    assert device["partial_updates"]["capable"] is True
    assert device["partial_updates"]["enabled"] is False
    assert device["partial_updates"]["consecutive_partials"] == 0
    assert (
        device["partial_updates"]["max_consecutive_partials"]
        == DEFAULT_PARTIAL_MAX_CONSECUTIVE
    )


# ------------------------------------------------------------- persistence


async def test_the_budget_survives_a_restart(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """The ghosting budget belongs to the glass, not to this process."""
    from custom_components.visionect.runtime import DeviceRecord

    record = setup_integration.runtime_data.record(DEVICE_UUID)
    record.consecutive_partials = 7
    record.last_frame_full = False
    record.last_fallback_reason = None

    restored = DeviceRecord.from_dict(DEVICE_UUID, record.to_dict())
    assert restored.consecutive_partials == 7
    assert restored.last_frame_full is False
