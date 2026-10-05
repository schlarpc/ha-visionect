"""Shared fixtures.

The guiding choice here: **drive the integration with real captured bytes.**
``fixtures/golden.json.gz`` is verbatim traffic from the author's 31.2" sign,
server-side, carried over from ``pyvisionect``'s own test suite.  So the tests
below do not mock the protocol at all -- they open a real TCP connection to the
listener the config entry just bound and replay what the sign actually sent.
That is the difference between testing this integration and testing a mock of
it: entity creation, the status-field gating, the sync verdict and the
diagnostics payload all run off the same packets the hardware produces.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import socket
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.visionect.const import (
    CONF_PERSIST_PARAMS,
    DEFAULT_PERSIST_PARAMS,
    DOMAIN,
)

FIXTURE = Path(__file__).parent / "fixtures" / "golden.json.gz"

#: The UUID the golden capture's sign reports.  Documentation-range, same
#: length as the real one (see the fixture's own ``meta.note``).
DEVICE_UUID = "00112233-4455-6677-8899-aabb00000000"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: Any) -> None:
    """Make Home Assistant load ``custom_components/visionect``."""


@pytest.fixture(autouse=True)
def expected_lingering_timers() -> bool:
    """The listener's one-minute overdue tick is unsubscribed on unload.

    Tests that never unload still trip the lingering-timer check, which is
    noise rather than a finding.
    """
    return True


# --------------------------------------------------------------- the capture


@pytest.fixture(scope="session")
def golden() -> dict[str, Any]:
    with gzip.open(FIXTURE, "rt") as handle:
        return json.load(handle)


@pytest.fixture(scope="session")
def device_frames(golden: dict[str, Any]) -> list[bytes]:
    """Every device -> server frame in the capture, in order, as raw bytes."""
    return [
        base64.b64decode(frame["bytes"])
        for frame in golden["frames"]
        if frame["direction"] == "device->server" and "bytes" in frame
    ]


# ------------------------------------------------------------------ the port


def free_port() -> int:
    """A port nothing is listening on, as far as the kernel can say."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def port(socket_enabled: None) -> int:
    return free_port()


# ------------------------------------------------------------- the fake sign


class FakeSign:
    """A TCP client that speaks the capture back at the listener.

    The sign dials out and speaks first, with no handshake, so replaying its
    frames onto a socket is the whole of being a sign.  Server replies are
    drained and kept so a test can assert that an ack came back.
    """

    def __init__(self, host: str, port: int, frames: list[bytes]) -> None:
        self._host = host
        self._port = port
        self._frames = frames
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self.received = bytearray()

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(
            self._host, self._port
        )

    async def send_raw(self, payload: bytes) -> None:
        assert self._writer is not None
        self._writer.write(payload)
        await self._writer.drain()

    async def send_frame(self, index: int) -> None:
        """Replay captured device frame *index*."""
        await self.send_raw(self._frames[index])

    async def status(self, hass: HomeAssistant, index: int = 0) -> None:
        """Replay one status frame and let Home Assistant settle."""
        await self.send_frame(index)
        await self.drain(hass)

    async def drain(self, hass: HomeAssistant, rounds: int = 6) -> None:
        """Give the listener's read loop and the event bridge room to run."""
        for _ in range(rounds):
            await asyncio.sleep(0)
            await hass.async_block_till_done()

    async def read_available(self, timeout: float = 0.2) -> bytes:
        assert self._reader is not None
        try:
            chunk = await asyncio.wait_for(self._reader.read(65536), timeout)
        except (TimeoutError, asyncio.IncompleteReadError):
            return b""
        self.received.extend(chunk)
        return chunk

    async def close(self) -> None:
        if self._writer is None:
            return
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError):
            pass


@pytest.fixture
def config_entry(port: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Visionect listener (port {port})",
        data={CONF_HOST: "127.0.0.1", CONF_PORT: port},
        options={CONF_PERSIST_PARAMS: DEFAULT_PERSIST_PARAMS},
        entry_id="01JVISIONECTTESTENTRY000000",
    )


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    socket_enabled: None,
) -> AsyncIterator[MockConfigEntry]:
    """A loaded config entry with a bound listener."""
    assert await async_setup_component(hass, "homeassistant", {})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    yield config_entry
    if config_entry.state.recoverable:
        await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()


@pytest.fixture
async def sign(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    port: int,
    device_frames: list[bytes],
) -> AsyncIterator[FakeSign]:
    """A connected fake sign that has reported its first status packet."""
    client = FakeSign("127.0.0.1", port, device_frames)
    await client.connect()
    await client.status(hass)
    yield client
    await client.close()
    await hass.async_block_till_done()
