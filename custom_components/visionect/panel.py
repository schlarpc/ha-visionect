"""The Web Serial commissioning panel.

Commissioning a sign means telling it where Home Assistant is, over its USB
cable, once.  There is no way around the cable: the sign is a TCP client that
dials out and runs no listening service, so nothing can find it, and the eight
TCLV fields a bootstrap needs are ``canWrite: false`` over the network, so
nothing can configure it remotely either.  A sign has to be *pointed at* a
server, by hand, before it will ever speak to one.

What this panel changes is *which machine* needs the cable.  Before it, that
machine needed Python, pyserial and a checkout on it.  Now it is whatever
laptop the user already has Home Assistant open on, because Chrome can open a
serial port.

Three constraints shape the implementation:

**No binaries, no subprocesses.**  The integration ships Python and static
files and nothing else.  The browser owns the serial port; Home Assistant only
serves the page.

**No build step.**  ``frontend/`` is plain ES modules served as they are
written.  There is no npm, no bundler and no transpile, so what is in the
repository is what runs, and a stack trace from a user's browser points at a
real line.

**Web Serial needs a secure context.**  HTTPS or a loopback origin, and a
Chromium browser.  On a plain ``http://`` LAN address -- which is a great many
Home Assistant installs -- ``navigator.serial`` does not exist at all.  The
panel is registered anyway and says so on its own first screen, because a
panel that silently does nothing is worse than no panel; see
``frontend/lib/support.js``.  Nothing on this side can detect it: the origin
is a property of how the browser reached the page, not of how the page was
served.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PANEL_URL_PATH = "visionect-commissioning"
"""Where the panel lives in the frontend: ``/visionect-commissioning``."""

PANEL_WEBCOMPONENT = "visionect-commissioning-panel"
"""The custom element ``visionect-panel.js`` defines."""

PANEL_TITLE = "Visionect signs"
PANEL_ICON = "mdi:usb-flash-drive"

STATIC_URL = f"/{DOMAIN}_frontend"
"""Where the static assets are served.

Not under ``/api/``: Home Assistant's auth middleware treats that prefix as
authenticated API surface, and a static route registered there is a raw aiohttp
route that does not take part in that scheme.  A plain top-level path is what
every other integration with a custom panel uses, and a static path in Home
Assistant is unauthenticated wherever it lives -- which is fine here, because
these files are the same for every install and carry nothing install-specific.
Everything the page needs to know about *this* install arrives through the
panel config, over the authenticated websocket.
"""

STATIC_DIR = Path(__file__).parent / "frontend"

_STATIC_REGISTERED = f"{DOMAIN}_panel_static"
_PANEL_REGISTERED = f"{DOMAIN}_panel"


def _module_url(version: str) -> str:
    """The panel entry point, with a cache-busting version.

    Static assets are served without immutable cache headers, but a browser
    will still reuse a module it already has, and an ES module graph is not
    revalidated per import.  The version query is what makes an integration
    upgrade actually land in the browser.
    """
    return f"{STATIC_URL}/visionect-panel.js?v={version}"


async def async_register_panel(
    hass: HomeAssistant, *, version: str, config: dict[str, Any]
) -> None:
    """Serve ``frontend/`` and put the panel in the sidebar.

    Split in two because the two halves have different lifetimes.  The static
    route is registered once per Home Assistant run and never removed -- aiohttp
    has no route removal, and re-registering the same path on a config entry
    reload would raise.  The panel itself *is* removed on unload, so unloading
    the integration takes its sidebar entry with it.
    """
    if not hass.data.get(_STATIC_REGISTERED):
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    STATIC_URL,
                    str(STATIC_DIR),
                    # False on purpose: these files are hand-edited and the
                    # version query above is the only cache key that is honest
                    # about when they changed.
                    cache_headers=False,
                )
            ]
        )
        hass.data[_STATIC_REGISTERED] = True

    # Imported here rather than at module scope so that a Home Assistant
    # without panel_custom set up fails on this call, with a log line naming
    # the panel, instead of on importing the integration.
    from homeassistant.components import panel_custom

    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=PANEL_WEBCOMPONENT,
        module_url=_module_url(version),
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        # Commissioning writes a sign's flash. That is not a thing for a guest
        # account on the household dashboard.
        require_admin=True,
        embed_iframe=False,
        config=config,
    )
    hass.data[_PANEL_REGISTERED] = True
    _LOGGER.debug("registered the commissioning panel at /%s", PANEL_URL_PATH)


@callback
def async_remove_panel(hass: HomeAssistant) -> None:
    """Take the panel out of the sidebar. The static route stays."""
    if not hass.data.pop(_PANEL_REGISTERED, False):
        return
    from homeassistant.components import frontend

    frontend.async_remove_panel(hass, PANEL_URL_PATH)


@callback
def async_panel_config(runtime: Any) -> dict[str, Any]:
    """What the page needs to know that only this side can answer.

    One field matters: the address to point a sign at.  The page could guess it
    from ``window.location``, and does as a fallback, but that is where the
    *browser* reached Home Assistant -- which may be a Nabu Casa relay, a
    reverse proxy name that does not resolve on the sign's network, or
    ``localhost``.  ``localhost`` is the worst case and the most likely one,
    because a loopback origin is the one plain-HTTP address Web Serial accepts:
    the panel is most likely to be open on exactly the URL that must never be
    written into a sign.

    The listener knows better.  ``runtime.advertised_address`` is what the
    integration resolved its own bind address to, with ``0.0.0.0`` already
    replaced by the source IP of the default route.
    """
    host = ""
    port: int | None = None
    advertised = getattr(runtime, "advertised_address", "") or ""
    if advertised:
        host, _, raw_port = advertised.rpartition(":")
        if raw_port.isdigit():
            port = int(raw_port)
        else:  # pragma: no cover - advertised_address is always host:port
            host, port = advertised, None
    if port is None:
        port = getattr(runtime, "port", None)
    return {
        "listener_host": host,
        "listener_port": port,
        # What the user configured, which is not necessarily reachable: shown
        # so a 0.0.0.0 bind does not look like the panel invented an address.
        "configured_host": getattr(runtime, "host", ""),
    }
