"""Config, options and reconfigure flows.

The config flow is the one part of this integration with no device in it, and
Home Assistant's Bronze tier asks for it at full coverage, so these tests go
through every branch: the two steps of the happy path, both bind failures, the
single-entry abort, every options sub-step, and the reconfigure step that did
not exist until now.
"""

from __future__ import annotations

import asyncio
import errno
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.visionect.const import (
    CONF_PARTIAL_MAX_CONSECUTIVE,
    CONF_PARTIAL_UPDATES,
    CONF_PERSIST_PARAMS,
    CONF_TLS_CERTFILE,
    CONF_TLS_KEYFILE,
    DEFAULT_PARTIAL_MAX_CONSECUTIVE,
    DOMAIN,
)

from .conftest import DEVICE_UUID, FakeSign, free_port


# ------------------------------------------------------------- the user flow


async def test_user_flow(hass: HomeAssistant, port: int) -> None:
    """Bind a port, read the instructions, get an entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}
    # The detected address is the whole point of the form's prose.
    assert "detected_ip" in result["description_placeholders"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: port}
    )
    # The second step has no fields: it is the thing the user must go and do.
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "instructions"
    assert result["description_placeholders"]["port"] == str(port)

    with patch(
        "custom_components.visionect.async_setup_entry", return_value=True
    ) as setup:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Visionect listener (port {port})"
    assert result["data"] == {"host": "127.0.0.1", "port": port}
    assert result["options"] == {CONF_PERSIST_PARAMS: True}
    assert len(setup.mock_calls) == 1


async def test_user_flow_port_in_use(
    hass: HomeAssistant, port: int, socket_enabled: None
) -> None:
    """A port someone else holds is caught in the form, not at bind time."""
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", port)
    try:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: port}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"
        assert result["errors"] == {"base": "port_in_use"}
    finally:
        server.close()
        await server.wait_closed()

    # ... and the same form still works once the port is free.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: port}
    )
    assert result["step_id"] == "instructions"


async def test_user_flow_cannot_bind(hass: HomeAssistant) -> None:
    """Any other OSError is a different error key and different advice."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(
        "custom_components.visionect.config_flow._async_test_bind",
        side_effect=OSError(errno.EACCES, "nope"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "203.0.113.9", CONF_PORT: 11113}
        )
    assert result["errors"] == {"base": "cannot_bind"}


async def test_user_flow_source_ip_failure(hass: HomeAssistant, port: int) -> None:
    """A failed address lookup is cosmetic and must not break the form."""
    with patch(
        "homeassistant.components.network.async_get_source_ip",
        side_effect=RuntimeError("no interfaces"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    assert result["type"] is FlowResultType.FORM
    assert (
        result["description_placeholders"]["detected_ip"] == "this host's LAN address"
    )


async def test_single_entry_only(hass: HomeAssistant, port: int) -> None:
    """One listener serves every sign, so a second entry is refused.

    ``single_config_entry`` in the manifest is what does it, and Home
    Assistant's own abort reason is what comes back -- which is why the flow
    does not need to reach the port check at all.
    """
    MockConfigEntry(
        domain=DOMAIN, data={CONF_HOST: "0.0.0.0", CONF_PORT: 11113}
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


# ------------------------------------------------------------- reconfigure


async def test_reconfigure_moves_the_port(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Gold asks for this, and without it the port cannot be changed at all."""
    entry = setup_integration
    new_port = free_port()

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["description_placeholders"]["port"] == str(entry.data[CONF_PORT])

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: new_port}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PORT] == new_port
    assert entry.title == f"Visionect listener (port {new_port})"
    # The entry reloaded onto the new port, and is actually listening there.
    assert entry.runtime_data.port == new_port
    assert entry.runtime_data.listener_running


async def test_reconfigure_unchanged_port_is_not_in_use(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Re-submitting the same port must not trip over our own listener.

    The entry holds the port already, so a naive test-bind fails with
    EADDRINUSE and the form refuses a no-op change. This is the regression.
    """
    entry = setup_integration
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: entry.data[CONF_HOST], CONF_PORT: entry.data[CONF_PORT]},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"


async def test_reconfigure_rejects_a_bound_port(
    hass: HomeAssistant, setup_integration: MockConfigEntry, socket_enabled: None
) -> None:
    entry = setup_integration
    other = free_port()
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", other)
    try:
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: other}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "port_in_use"}
    finally:
        server.close()
        await server.wait_closed()
    assert entry.data[CONF_PORT] != other


async def test_reconfigure_survives_a_failed_address_lookup(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The detected address is prose in the form; failing to find it is not fatal."""
    entry = setup_integration
    with patch(
        "homeassistant.components.network.async_get_source_ip",
        side_effect=RuntimeError("no interfaces"),
    ):
        result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert (
        result["description_placeholders"]["detected_ip"] == "this host's LAN address"
    )


async def test_reconfigure_cannot_bind(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    entry = setup_integration
    result = await entry.start_reconfigure_flow(hass)
    with patch(
        "custom_components.visionect.config_flow._async_test_bind",
        side_effect=OSError(errno.EACCES, "nope"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "203.0.113.9", CONF_PORT: free_port()}
        )
    assert result["errors"] == {"base": "cannot_bind"}


# ------------------------------------------------------------- options flow


async def test_options_menu(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(
        setup_integration.entry_id
    )
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {"settings", "partial_updates"}


async def test_options_settings(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    entry = setup_integration
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    assert result["step_id"] == "settings"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_PERSIST_PARAMS: False}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_PERSIST_PARAMS] is False
    assert entry.options[CONF_TLS_CERTFILE] == ""


async def test_options_key_without_certificate(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(
        setup_integration.entry_id
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_PERSIST_PARAMS: True, CONF_TLS_KEYFILE: "/tmp/key.pem"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_TLS_CERTFILE: "key_without_certificate"}


async def test_options_bad_certificate(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A certificate is loaded in the form, not discovered at bind time.

    The reason is in the integration's own comment: a sign with TLS turned on
    and a listener without a certificate can only be recovered over USB.
    """
    result = await hass.config_entries.options.async_init(
        setup_integration.entry_id
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_PERSIST_PARAMS: True, CONF_TLS_CERTFILE: "/nonexistent/cert.pem"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"][CONF_TLS_CERTFILE] == "bad_certificate"


async def test_options_partial_updates(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """The sign is offered, defaults to off, and turns on without a reload."""
    entry = setup_integration
    runtime = entry.runtime_data
    assert runtime.partial_capable(DEVICE_UUID)
    assert runtime.partial_requested(DEVICE_UUID) is False

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "partial_updates"}
    )
    assert result["step_id"] == "partial_updates"
    assert result["description_placeholders"]["count"] == "1"
    assert result["description_placeholders"]["default_max"] == str(
        DEFAULT_PARTIAL_MAX_CONSECUTIVE
    )

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_PARTIAL_UPDATES: [DEVICE_UUID],
            CONF_PARTIAL_MAX_CONSECUTIVE: 4,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_PARTIAL_UPDATES] == {DEVICE_UUID: True}
    assert entry.options[CONF_PARTIAL_MAX_CONSECUTIVE] == 4
    assert runtime.partial_enabled(DEVICE_UUID) is True
    assert runtime.partial_max_consecutive == 4
    # Applied in place: the same runtime object, still bound, still connected.
    assert entry.runtime_data is runtime
    assert runtime.listener_running


async def test_options_partial_updates_keeps_the_other_step(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """A menu-shaped options flow must merge, not replace."""
    entry = setup_integration
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_PERSIST_PARAMS: False}
    )
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "partial_updates"}
    )
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_PARTIAL_UPDATES: [], CONF_PARTIAL_MAX_CONSECUTIVE: 10},
    )
    await hass.async_block_till_done()
    assert entry.options[CONF_PERSIST_PARAMS] is False
    assert entry.options[CONF_PARTIAL_UPDATES] == {DEVICE_UUID: False}


async def test_options_partial_updates_no_capable_sign(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """With no sign that was ever measured taking a rectangle, say so."""
    result = await hass.config_entries.options.async_init(
        setup_integration.entry_id
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "partial_updates"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_partial_capable"


async def test_options_partial_updates_entry_not_loaded(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    flow = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "partial_updates"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_loaded"
