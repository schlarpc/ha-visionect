"""Diagnostics, and what must not be in them.

This is the integration's primary support tool, because the dominant failure
mode here is silent -- the listener binds, the entry looks healthy, and the
sign never connects. So the payload is deliberately generous, which makes the
redaction worth pinning: ``async_redact_data`` matches key names exactly and
is case-sensitive, so a renamed field silently stops being redacted.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.visionect.const import DOMAIN
from custom_components.visionect.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)

from .conftest import DEVICE_UUID, FakeSign


def leaves(obj: Any, path: str = "") -> list[tuple[str, Any]]:
    if isinstance(obj, dict):
        out = []
        for key, value in obj.items():
            out.extend(leaves(value, f"{path}/{key}"))
        return out
    if isinstance(obj, list):
        out = []
        for index, value in enumerate(obj):
            out.extend(leaves(value, f"{path}[{index}]"))
        return out
    return [(path, obj)]


async def test_entry_diagnostics_lead_with_the_listener(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """The counters that tell the two silent failures apart come first."""
    data = await async_get_config_entry_diagnostics(hass, setup_integration)
    listener = data["listener"]
    assert listener["listener_running"] is True
    assert listener["accepted"] >= 1
    assert listener["identified"] >= 1
    assert listener["silent_connections"] == 0
    # Which compressor is in use, because the Alpine container often has no
    # lz4 and every block then goes out stored.
    assert listener["connection_config"]["compressor"] in ("lz4", "stored_only")
    assert listener["connection_config"]["allow_command_packets"] is False
    assert listener["connection_config"]["watchdog_enabled"] is False


async def test_entry_diagnostics_describe_the_sign(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    data = await async_get_config_entry_diagnostics(hass, setup_integration)
    device = data["devices"][DEVICE_UUID[:8]]
    assert device["uuid_short"] == DEVICE_UUID[:8]
    assert device["hardware_name_id"] == 8
    assert device["panel"]["canvas"] == [1440, 2560]
    assert device["socket_open"] is True
    assert device["fields"]["ErrorCode"]["name"] == "no error"
    assert device["pending"]["slots"] == []


async def test_the_secrets_are_redacted(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """Every key in TO_REDACT that is actually present must come back REDACTED."""
    data = await async_get_config_entry_diagnostics(hass, setup_integration)
    found = leaves(data)

    for path, value in found:
        key = path.rsplit("/", 1)[-1].split("[")[0]
        if key in TO_REDACT:
            assert value == REDACTED, f"{path} leaked {value!r}"

    # And the two that matter are genuinely in the payload at all, so this test
    # cannot pass by the fields having been renamed away.
    paths = [p for p, _ in found]
    assert any(p.endswith("/BSSID") for p in paths)
    assert any(p.endswith("/GTIN") for p in paths)

    # The full UUID is a semi-secret, and the short hash is what correlates.
    flat = repr(data)
    assert DEVICE_UUID not in flat
    assert DEVICE_UUID[:8] in flat


async def test_a_url_content_source_is_redacted(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """A renderer URL can carry a token in its query string."""
    from custom_components.visionect.content import UrlSource

    runtime = setup_integration.runtime_data
    runtime.async_set_source(
        DEVICE_UUID,
        UrlSource(
            url="http://renderer.invalid/dash?token=hunter2",
            headers={"Authorization": "Bearer hunter2"},
        ),
    )
    await hass.async_block_till_done()

    data = await async_get_config_entry_diagnostics(hass, setup_integration)
    assert "hunter2" not in repr(data)
    # The kind of source is still reported, which is the part support needs.
    device = data["devices"][DEVICE_UUID[:8]]
    assert device["content_source_kind"] == "UrlSource"


async def test_device_diagnostics_cover_one_sign(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    entry = setup_integration
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, DEVICE_UUID), entry.entry_id
    )
    data = await async_get_device_diagnostics(hass, entry, device)
    assert data["device"]["uuid_short"] == DEVICE_UUID[:8]
    assert data["listener"]["listener_running"] is True
    for path, value in leaves(data):
        key = path.rsplit("/", 1)[-1].split("[")[0]
        if key in TO_REDACT:
            assert value == REDACTED, f"{path} leaked {value!r}"


async def test_device_diagnostics_for_the_listener_device(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """The listener is a service device and has no sign behind it."""
    entry = setup_integration
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"listener-{entry.entry_id}"), entry.entry_id
    )
    data = await async_get_device_diagnostics(hass, entry, device)
    assert "device" not in data
    assert "listener" in data
