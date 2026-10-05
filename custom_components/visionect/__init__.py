"""The Visionect integration: Home Assistant *is* the sign's server.

A Visionect sign is a TCP client.  It dials out to port 11113, speaks first,
and runs no listening service of its own -- so there is nothing to poll, no
discovery in either direction, and no way to reach it between contacts.  This
integration therefore hosts the listener and replaces the Visionect Software
Suite outright.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from pyvisionect.wire.errors import ListenError

from .const import DOMAIN, OPTIONS_NEEDING_RELOAD, PLATFORMS
from .runtime import VisionectRuntime
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

type VisionectConfigEntry = ConfigEntry[VisionectRuntime]

# errno values that will never fix themselves by waiting.
_PERMANENT_ERRNOS = {13, 99, 97}  # EACCES, EADDRNOTAVAIL, EAFNOSUPPORT


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the actions once, not per entry."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: VisionectConfigEntry) -> bool:
    """Bind the listener and restore everything a restart must not lose."""
    runtime = VisionectRuntime(hass, entry)
    await runtime.async_load()

    try:
        await runtime.async_start()
    except ListenError as err:
        # ListenError is NOT an OSError subclass; it carries host/port/errno.
        if err.errno in _PERMANENT_ERRNOS:
            raise ConfigEntryError(
                translation_domain=DOMAIN,
                translation_key="cannot_bind",
                translation_placeholders={
                    "host": runtime.host,
                    "port": str(runtime.port),
                    "error": str(err),
                },
            ) from err
        # Anything else -- EADDRINUSE above all -- is plausibly transient: a
        # reload race, a lingering socket, another instance shutting down.
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="port_in_use",
            translation_placeholders={"port": str(runtime.port), "error": str(err)},
        ) from err

    entry.runtime_data = runtime
    runtime.async_register_listener_device()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    runtime.note_applied_options()
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: VisionectConfigEntry) -> bool:
    """Stop accepting *before* tearing down the entities that consume the data.

    The socket is closed here rather than from ``async_on_unload`` on purpose:
    an unload callback is capped at 10 seconds, and a listener teardown that
    gets cut short leaves the port bound and the next bind failing.
    """
    await entry.runtime_data.async_shutdown()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(
    hass: HomeAssistant, entry: VisionectConfigEntry
) -> None:
    """Reload only when an option actually needs the socket rebound.

    The default "reload on any option change" is wrong here, and not mildly.
    Reloading closes the listener, which drops the sign's TCP session -- and
    this firmware re-dials on its own schedule, which can be the best part of
    an hour away. So an option the runtime reads live, such as which signs use
    partial updates, is applied in place and the kitchen display stays up.
    """
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:  # pragma: no cover - entry not loaded
        return
    changed = runtime.changed_options(dict(entry.options))
    runtime.note_applied_options()
    if changed & OPTIONS_NEEDING_RELOAD:
        await hass.config_entries.async_reload(entry.entry_id)
        return
    _LOGGER.debug("applying %s without a reload", sorted(changed) or "no change")
    runtime.async_notify_entities()


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: VisionectConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Let a user delete a sign.

    It will re-register on its next contact -- the protocol has no concept of
    an unwanted device -- so this is "forget what I know", not "ban".
    """
    runtime = entry.runtime_data
    for domain, identifier in device.identifiers:
        if domain == DOMAIN and not identifier.startswith("listener-"):
            await runtime.async_forget(identifier)
    return True
