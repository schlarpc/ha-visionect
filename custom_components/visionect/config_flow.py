"""Config flow.

A normal config flow asks for an address and connects. This one cannot: there
is nothing to connect to, nothing to discover, and the sign may be an hour from
saying hello. Its real job is to **bind a port and then teach the user how to
point the sign at it** -- a documentation problem wearing a config-flow costume,
which is better admitted than faked.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import ssl
from typing import Any

import voluptuous as vol
from homeassistant.components import network
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    ConfigEntry,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import selector

from pyvisionect.io.tcp import server_ssl_context

from .const import (
    CONF_PERSIST_PARAMS,
    CONF_TLS_CERTFILE,
    CONF_TLS_KEYFILE,
    DEFAULT_HOST,
    DEFAULT_PERSIST_PARAMS,
    DEFAULT_PORT,
    DOMAIN,
)


_LOGGER = logging.getLogger(__name__)


async def _async_test_bind(host: str, port: int) -> None:
    """Open and immediately close a listening socket.

    The only validation the protocol permits, and genuinely worth doing:
    "port 11113 is already in use" caught here is infinitely better than a
    ConfigEntryNotReady loop later.
    """
    server = await asyncio.start_server(lambda r, w: None, host, port)
    server.close()
    await server.wait_closed()


class VisionectConfigFlow(ConfigFlow, domain=DOMAIN):
    """One entry, one listener, N signs."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._detected_ip: str = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        try:
            self._detected_ip = await network.async_get_source_ip(self.hass)
        except Exception:  # noqa: BLE001 - best effort; it is only shown to the user
            self._detected_ip = "this host's LAN address"

        if user_input is not None:
            self._async_abort_entries_match(user_input)
            try:
                await _async_test_bind(user_input[CONF_HOST], user_input[CONF_PORT])
            except OSError as err:
                errors["base"] = (
                    "port_in_use" if err.errno == errno.EADDRINUSE else "cannot_bind"
                )
            else:
                self._data = dict(user_input)
                return await self.async_step_instructions()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=65535)
                    ),
                    vol.Required(CONF_HOST, default=DEFAULT_HOST): selector.TextSelector(),
                }
            ),
            description_placeholders={"detected_ip": self._detected_ip},
            errors=errors,
        )

    async def async_step_instructions(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The honest step: no fields, just the thing the user has to go and do."""
        if user_input is not None:
            return self.async_create_entry(
                title=f"Visionect listener (port {self._data[CONF_PORT]})",
                data={
                    "host": self._data[CONF_HOST],
                    "port": self._data[CONF_PORT],
                },
                options={CONF_PERSIST_PARAMS: DEFAULT_PERSIST_PARAMS},
            )
        return self.async_show_form(
            step_id="instructions",
            data_schema=vol.Schema({}),
            description_placeholders={
                "detected_ip": self._detected_ip,
                "port": str(self._data[CONF_PORT]),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> VisionectOptionsFlow:
        return VisionectOptionsFlow()


class VisionectOptionsFlow(OptionsFlow):
    """Entry-level behaviour that is not per sign."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            certfile = (user_input.get(CONF_TLS_CERTFILE) or "").strip()
            keyfile = (user_input.get(CONF_TLS_KEYFILE) or "").strip()
            if keyfile and not certfile:
                errors[CONF_TLS_CERTFILE] = "key_without_certificate"
            elif certfile:
                # Load it now, here, where the user is looking at the form.
                # The alternative is discovering a bad path at bind time, by
                # which point a sign with TCLV 145 set is already unreachable.
                try:
                    await self.hass.async_add_executor_job(
                        server_ssl_context, certfile, keyfile or None
                    )
                except (OSError, ssl.SSLError, ValueError) as err:
                    errors[CONF_TLS_CERTFILE] = "bad_certificate"
                    errors["base"] = "bad_certificate"
                    _LOGGER.debug("rejecting TLS certificate %s: %s", certfile, err)
            if not errors:
                return self.async_create_entry(
                    data={
                        CONF_PERSIST_PARAMS: user_input[CONF_PERSIST_PARAMS],
                        CONF_TLS_CERTFILE: certfile,
                        CONF_TLS_KEYFILE: keyfile,
                    }
                )

        options = self.config_entry.options
        suggested = user_input if user_input is not None else options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PERSIST_PARAMS,
                        default=options.get(CONF_PERSIST_PARAMS, DEFAULT_PERSIST_PARAMS),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_TLS_CERTFILE,
                        description={
                            "suggested_value": suggested.get(CONF_TLS_CERTFILE, "")
                        },
                    ): selector.TextSelector(),
                    vol.Optional(
                        CONF_TLS_KEYFILE,
                        description={
                            "suggested_value": suggested.get(CONF_TLS_KEYFILE, "")
                        },
                    ): selector.TextSelector(),
                }
            ),
            errors=errors,
        )
