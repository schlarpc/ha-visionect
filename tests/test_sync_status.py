"""The sync verdict, and the convergence window it exists to survive.

``pyvisionect`` ``OPEN-QUESTIONS.md`` A11. A push legitimately leaves the sign
disagreeing with us for the whole of its draw-and-report cycle -- 10 to 48 s
measured on the real sign -- and the entity carries
``BinarySensorDeviceClass.PROBLEM``, so a naive "checksums differ" raised a
problem indicator after **every normal update**.

The fix was a fourth state rather than a wider timeout, and the half of it that
only hardware revealed is that the sign *bursts* status packets around a draw:
nine in the first 55 s, then one a minute. So two contacts can elapse in
fifteen seconds, before a 1.84 MB frame has finished transferring, and the
contact counter has to be gated on a clock as well. That is the case pinned
below -- it is the one a reasonable implementation gets wrong.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pyvisionect.session import SyncStatus

from .conftest import DEVICE_UUID, FakeSign

TAG_DISPLAY_STATE_CRC = 9
SENSOR = "binary_sensor.visionect_sign_00112233_display_out_of_sync"
PENDING = "binary_sensor.visionect_sign_00112233_pending_changes"


class FakeClock:
    """A stand-in for ``time.monotonic``, which is what the runtime passes.

    The runtime keeps its clock in one attribute precisely so that the push
    timestamp and the verdict share a scale; swapping it is how a 25-minute
    convergence window fits in a test.
    """

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


async def push(hass: HomeAssistant, sign: FakeSign, runtime: Any) -> None:
    from PIL import Image

    panel = runtime.panel(DEVICE_UUID)
    image = Image.new("L", (panel.canvas_width, panel.canvas_height), 128)
    runtime.record(DEVICE_UUID).force_next = True
    await runtime.async_push_image_now(DEVICE_UUID, image)
    await sign.pump(hass)


async def test_a_fresh_sign_is_unknown_not_broken(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """Nothing pushed yet, so there is nothing to disagree about."""
    runtime = setup_integration.runtime_data
    runtime.device_state(DEVICE_UUID).pushed_checksum = None
    assert runtime.sync_status(DEVICE_UUID) in (
        SyncStatus.UNKNOWN,
        SyncStatus.IN_SYNC,
        SyncStatus.CONVERGING,
    )
    assert hass.states.get(SENSOR).state == "off"


async def test_it_does_not_flap_during_the_convergence_window(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """The regression. A normal push must not raise a problem indicator.

    The sign's captured status packets all report the same old
    ``DisplayStateCRC``, so after our push the checksums genuinely differ --
    exactly the situation that used to turn the sensor on immediately.
    """
    runtime = setup_integration.runtime_data
    clock = FakeClock()
    runtime._clock = clock  # noqa: SLF001 - the attribute exists for this

    await push(hass, sign, runtime)
    state = runtime.device_state(DEVICE_UUID)
    assert state.pushed_checksum is not None
    assert state.in_sync is False, "the capture reports a different CRC"

    assert runtime.sync_status(DEVICE_UUID) is SyncStatus.CONVERGING
    assert hass.states.get(SENSOR).state == "off"

    # The burst: nine contacts inside the first minute, as measured. The bare
    # contact counter is satisfied after two of them; the clock gate is what
    # keeps the verdict honest.
    clock.tick(15.0)
    for index in range(9):
        await sign.status(hass, index % 3)
    assert state.contacts_since_push >= 9
    assert runtime.sync_status(DEVICE_UUID) is SyncStatus.CONVERGING
    assert hass.states.get(SENSOR).state == "off"

    # pending_changes must agree with it rather than contradict it, which is
    # how the original false alarm was found.
    assert hass.states.get(PENDING).attributes["sync_status"] == "converging"


async def test_it_settles_to_in_sync_when_the_sign_confirms(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    runtime = setup_integration.runtime_data
    clock = FakeClock()
    runtime._clock = clock  # noqa: SLF001

    await push(hass, sign, runtime)
    pushed = runtime.device_state(DEVICE_UUID).pushed_checksum

    # The sign reports our checksum back, which is the only way to ask it what
    # it is actually displaying.
    await sign.send_raw(sign.status_frame(set_tags={TAG_DISPLAY_STATE_CRC: pushed}))
    await sign.drain(hass)

    assert runtime.device_state(DEVICE_UUID).in_sync is True
    assert runtime.sync_status(DEVICE_UUID) is SyncStatus.IN_SYNC
    assert hass.states.get(SENSOR).state == "off"


async def test_a_real_divergence_does_turn_it_on(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """Had its say, and still showing the wrong thing."""
    runtime = setup_integration.runtime_data
    clock = FakeClock()
    runtime._clock = clock  # noqa: SLF001

    await push(hass, sign, runtime)
    await sign.status(hass, 0)
    await sign.status(hass, 1)

    # Past one announced interval plus the draw allowance, with contacts to
    # spare: the sign has had every chance to report the new frame.
    clock.tick(600.0)
    assert runtime.sync_status(DEVICE_UUID) is SyncStatus.DIVERGED

    # The minute tick is what writes the state, because the verdict can change
    # with nothing but the clock.
    runtime._async_check_overdue(None)  # noqa: SLF001
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == "on"
    assert hass.states.get(PENDING).attributes["sync_status"] == "diverged"


async def test_a_silent_sign_diverges_on_the_grace_window_alone(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """A sign that stopped calling never produces the contact that would settle it.

    Without the second deadline the verdict would sit at "converging" forever,
    and a stale panel would never be reported.
    """
    runtime = setup_integration.runtime_data
    clock = FakeClock()
    runtime._clock = clock  # noqa: SLF001

    await push(hass, sign, runtime)
    state = runtime.device_state(DEVICE_UUID)
    assert state.contacts_since_push < 2

    clock.tick(30.0)
    assert runtime.sync_status(DEVICE_UUID) is SyncStatus.CONVERGING
    clock.tick(3600.0)
    assert runtime.sync_status(DEVICE_UUID) is SyncStatus.DIVERGED


async def test_the_windows_come_from_the_sign_not_a_flat_timeout(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """A sign on an hourly heartbeat is not broken for the 59 minutes it is away.

    Both deadlines are derived from ``NextStatus`` -- the device's own
    announcement of when it will next be in touch.
    """
    runtime = setup_integration.runtime_data
    clock = FakeClock()
    runtime._clock = clock  # noqa: SLF001

    state = runtime.device_state(DEVICE_UUID)
    minute = state.settle_time()
    await push(hass, sign, runtime)

    # Same device state, told it speaks hourly instead.
    await sign.send_raw(sign.status_frame(set_tags={27: 60}))
    await sign.drain(hass)
    assert runtime.coordinator.snapshot(DEVICE_UUID).fields["NextStatus"] == 60
    hourly = runtime.device_state(DEVICE_UUID).settle_time()
    assert hourly > minute

    clock.tick(minute + 60.0)
    # Already past the one-minute sign's settle time, nowhere near the hourly
    # sign's, and the verdict follows the sign rather than the clock.
    assert runtime.sync_status(DEVICE_UUID) is SyncStatus.CONVERGING


async def test_a_restored_state_falls_back_to_the_contact_counter(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """``last_push_at`` is a monotonic value and means nothing after a restart.

    So a restored state has no clock to measure against, and the counter alone
    decides -- which is the safe direction: any push it remembers is from
    before the restart and has long since had its chance.
    """
    runtime = setup_integration.runtime_data
    state = runtime.device_state(DEVICE_UUID)
    restored = type(state).from_dict(state.to_dict())
    assert restored.last_push_at is None
