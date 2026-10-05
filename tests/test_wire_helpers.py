"""The one hand-built frame in this suite, checked against the capture.

An ack names the packet id the server has just invented, so no recording can
supply one and :meth:`FakeSign.control_frame` has to build it. That makes it
the only place where a test could drift away from the wire, so it is pinned
here: fed a captured ack's own packet id, the builder reproduces that captured
frame byte for byte.
"""

from __future__ import annotations

import base64
from typing import Any

from pyvisionect.packets import ControlPacket, decode_payload
from pyvisionect.wire import Direction, FrameDecoder

from .conftest import DEVICE_UUID, FakeSign


def _captured_acks(golden: dict[str, Any]) -> list[tuple[bytes, Any, Any]]:
    out = []
    for record in golden["frames"]:
        if record["direction"] != "device->server" or "bytes" not in record:
            continue
        raw = base64.b64decode(record["bytes"])
        for frame in FrameDecoder(Direction.DEVICE_TO_SERVER).feed(raw):
            packet = decode_payload(frame.data.type, frame.payload)
            if isinstance(packet, ControlPacket):
                out.append((raw, frame, packet))
    return out


def test_control_frame_is_byte_exact(golden: dict[str, Any]) -> None:
    captured = _captured_acks(golden)
    assert len(captured) >= 2, "the capture holds both an ack and a NACK"
    assert any(p.is_ack for _, _, p in captured)
    assert any(p.is_nack for _, _, p in captured)

    sign = FakeSign("127.0.0.1", 0, [])
    for raw, frame, packet in captured:
        sign.device_id = frame.data.device_id
        rebuilt = sign.control_frame(
            frame.data.id, ack=packet.is_ack, error_code=packet.error_code
        )
        assert rebuilt == raw, f"{packet} did not round-trip"


def test_device_uuid_matches_the_capture(golden: dict[str, Any]) -> None:
    """The fixture's UUID is what the tests key on; keep them together."""
    assert golden["meta"]["device_uuid"] == DEVICE_UUID
