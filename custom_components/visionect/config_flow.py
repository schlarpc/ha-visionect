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
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector

from pyvisionect.io.tcp import server_ssl_context

from .const import (
    CONF_PARTIAL_MAX_CONSECUTIVE,
    CONF_PARTIAL_UPDATES,
    CONF_PERSIST_PARAMS,
    CONF_TLS_CERTFILE,
    CONF_TLS_KEYFILE,
    DEFAULT_HOST,
    DEFAULT_PARTIAL_MAX_CONSECUTIVE,
    DEFAULT_PERSIST_PARAMS,
    DEFAULT_PORT,
    DOMAIN,
    PARTIAL_MAX_CONSECUTIVE_LIMIT,
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

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Move the listener to a different address or port.

        Without this the only way to change the port is to delete the entry,
        which throws away every sign's content source, dither choice and
        ghosting budget along with it.

        One wrinkle the obvious implementation gets wrong: the running entry is
        *already* bound to its own port, so test-binding an unchanged port
        fails with EADDRINUSE and the form refuses a no-op. So the bind check
        runs only for an address that actually changed.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        try:
            self._detected_ip = await network.async_get_source_ip(self.hass)
        except Exception:  # noqa: BLE001 - cosmetic only
            self._detected_ip = "this host's LAN address"

        if user_input is not None:
            unchanged = (
                user_input[CONF_HOST] == entry.data[CONF_HOST]
                and user_input[CONF_PORT] == entry.data[CONF_PORT]
            )
            if not unchanged:
                try:
                    await _async_test_bind(
                        user_input[CONF_HOST], user_input[CONF_PORT]
                    )
                except OSError as err:
                    errors["base"] = (
                        "port_in_use"
                        if err.errno == errno.EADDRINUSE
                        else "cannot_bind"
                    )
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    title=f"Visionect listener (port {user_input[CONF_PORT]})",
                    data_updates={
                        CONF_HOST: user_input[CONF_HOST],
                        CONF_PORT: user_input[CONF_PORT],
                    },
                )

        suggested = user_input if user_input is not None else entry.data
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PORT, default=suggested.get(CONF_PORT, DEFAULT_PORT)
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
                    vol.Required(
                        CONF_HOST, default=suggested.get(CONF_HOST, DEFAULT_HOST)
                    ): selector.TextSelector(),
                }
            ),
            description_placeholders={
                "detected_ip": self._detected_ip,
                "port": str(entry.data.get(CONF_PORT, DEFAULT_PORT)),
            },
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> VisionectOptionsFlow:
        return VisionectOptionsFlow()


class VisionectOptionsFlow(OptionsFlow):
    """Entry-level behaviour that is not per sign -- plus the one thing that is.

    Partial updates are deliberately *not* a per-sign entity. The owner wants
    to turn them on knowing what they are turning on, once, and then watch the
    ghosting counter; a config toggle under Configure says that, and a switch
    sitting among the sign's other entities does not.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init", menu_options=["settings", "partial_updates"]
        )

    async def async_step_settings(
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
                return self._save(
                    {
                        CONF_PERSIST_PARAMS: user_input[CONF_PERSIST_PARAMS],
                        CONF_TLS_CERTFILE: certfile,
                        CONF_TLS_KEYFILE: keyfile,
                    }
                )

        options = self.config_entry.options
        suggested = user_input if user_input is not None else options
        return self.async_show_form(
            step_id="settings",
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

    async def async_step_partial_updates(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose which signs send screen-space partial rectangles.

        Only signs whose ``HardwareNameID`` is in the library's verified set
        are offered at all. That set is a record of an experiment on physical
        hardware, not a capability a device advertises -- there is no way to
        ask a sign this question -- so an unverified sign is simply not on the
        list rather than being offered a switch that might damage its panel.
        """
        runtime = getattr(self.config_entry, "runtime_data", None)
        if runtime is None:
            return self.async_abort(reason="not_loaded")

        capable = [
            uuid for uuid in runtime.known_uuids() if runtime.partial_capable(uuid)
        ]
        if not capable:
            return self.async_abort(reason="no_partial_capable")

        if user_input is not None:
            chosen = set(user_input.get(CONF_PARTIAL_UPDATES) or [])
            # Signs that are not on the list keep whatever they had, so a sign
            # that happens to be asleep and absent from known_uuids() is not
            # silently switched off.
            existing = dict(self.config_entry.options.get(CONF_PARTIAL_UPDATES) or {})
            for uuid in capable:
                existing[uuid] = uuid in chosen
            return self._save(
                {
                    CONF_PARTIAL_UPDATES: existing,
                    CONF_PARTIAL_MAX_CONSECUTIVE: int(
                        user_input[CONF_PARTIAL_MAX_CONSECUTIVE]
                    ),
                }
            )

        enabled = dict(self.config_entry.options.get(CONF_PARTIAL_UPDATES) or {})
        registry = dr.async_get(self.hass)

        def _label(uuid: str) -> str:
            device = registry.async_get_device_by_identifier(
                (DOMAIN, uuid), self.config_entry.entry_id
            )
            name = (device.name_by_user or device.name) if device else None
            return f"{name} ({uuid[:8]})" if name else uuid

        return self.async_show_form(
            step_id="partial_updates",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_PARTIAL_UPDATES,
                        default=[u for u in capable if enabled.get(u)],
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=uuid, label=_label(uuid)
                                )
                                for uuid in capable
                            ],
                            multiple=True,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Required(
                        CONF_PARTIAL_MAX_CONSECUTIVE,
                        default=self.config_entry.options.get(
                            CONF_PARTIAL_MAX_CONSECUTIVE,
                            DEFAULT_PARTIAL_MAX_CONSECUTIVE,
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1,
                            max=PARTIAL_MAX_CONSECUTIVE_LIMIT,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
            description_placeholders={
                "count": str(len(capable)),
                "default_max": str(DEFAULT_PARTIAL_MAX_CONSECUTIVE),
            },
        )

    @callback
    def _save(self, updates: dict[str, Any]) -> ConfigFlowResult:
        """Merge one step's answers into the options, keeping the other step's.

        A menu-shaped options flow has to do this by hand: ``create_entry``
        replaces the whole options mapping, so returning only the step's own
        keys would silently wipe the other step's settings.
        """
        return self.async_create_entry(
            data={**self.config_entry.options, **updates}
        )
