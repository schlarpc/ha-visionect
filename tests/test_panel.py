"""The commissioning panel, from this side of the wire.

What can be proved here is that the panel is registered, that its assets are
served, that the address it prefills is the one the listener actually bound to,
and -- by reading the shipped JavaScript as text -- that the dangerous commands
are nowhere in it.

What cannot be proved here is anything involving an actual serial port: Web
Serial needs a real browser and a user gesture. The decisions the panel makes
are tested in ``tests/js/panel.test.mjs`` against captures from real hardware;
see ``tests/test_panel_js.py``, which runs that suite as part of pytest.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components import frontend
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.visionect.panel import (
    PANEL_URL_PATH,
    PANEL_WEBCOMPONENT,
    STATIC_DIR,
    STATIC_URL,
    async_panel_config,
)

FRONTEND = Path(__file__).parent.parent / "custom_components" / "visionect" / "frontend"


# --------------------------------------------------------------------- shipping


def test_every_asset_the_entry_point_imports_is_shipped() -> None:
    """Follow the import graph and check each file exists.

    There is no build step, so nothing resolves imports before a user's browser
    does. A typo in a relative path is a blank panel and a 404 in a console
    nobody is looking at.
    """
    seen: set[Path] = set()
    queue = [FRONTEND / "visionect-panel.js"]
    while queue:
        module = queue.pop()
        if module in seen:
            continue
        seen.add(module)
        assert module.is_file(), f"{module} is imported but not present"
        source = module.read_text()
        for match in re.finditer(r"""from\s+["'](\.[^"']+)["']""", source):
            queue.append((module.parent / match.group(1)).resolve())
        # new URL("./panel.css", import.meta.url) is an asset reference too.
        for match in re.finditer(r"""new URL\(\s*["'](\.[^"']+)["']""", source):
            target = (module.parent / match.group(1)).resolve()
            assert target.is_file(), f"{target} is referenced but not present"

    # The whole lib/ directory should be reachable; an orphan is either dead
    # code or a missing import.
    reachable = {p.name for p in seen}
    for path in (FRONTEND / "lib").glob("*.js"):
        assert path.name in reachable, f"lib/{path.name} is not imported by anything"


def test_no_build_step_and_no_dependencies() -> None:
    """No npm, no bundler, no CDN, nothing to install."""
    for name in ("package.json", "package-lock.json", "node_modules", "dist"):
        assert not (FRONTEND / name).exists(), f"frontend/{name} implies a build step"
    for path in FRONTEND.rglob("*.js"):
        source = path.read_text()
        assert "require(" not in source, f"{path.name} uses CommonJS"
        for match in re.finditer(r"""from\s+["']([^"'.][^"']*)["']""", source):
            pytest.fail(f"{path.name} imports the bare module {match.group(1)!r}")
        # Nothing is fetched from anywhere but this Home Assistant. A panel
        # that pulls a library off a CDN does not work on an offline LAN,
        # which is where a great many of these installs live.
        for match in re.finditer(r"""import\s*\(\s*["']https?://""", source):
            pytest.fail(f"{path.name} imports from a URL: {match.group(0)!r}")


def test_the_static_dir_is_only_what_it_should_be() -> None:
    """A static directory is served verbatim, so nothing stray may live in it."""
    allowed = {".js", ".css"}
    for path in FRONTEND.rglob("*"):
        if path.is_dir():
            continue
        assert path.suffix in allowed, f"{path} would be served to anyone"


# ------------------------------------------------------------------ the guard


def _forbidden_names() -> list[str]:
    """The commands the brief says must never be offered, anywhere."""
    return [
        "play_music",
        "sf_rdid",
        "sf_rdst",
        "fs_format",
        "cc3100_format",
        "cli_password_set",
        "sf_unprot",
        "sf_wrst",
        "display_conf_set",
        "app_sleep",
        "24aa256_test",
        "lms",
    ]


def test_no_forbidden_command_is_emittable_from_the_shipped_javascript() -> None:
    """Grep the actual files for the actual strings.

    The JavaScript suite proves the guard refuses these. This proves something
    different and dumber: that no file in ``frontend/`` contains a string that
    *is* one of these commands outside the deny list itself. A guard can be
    bypassed by a future contributor writing the command somewhere else; a
    grep catches that.
    """
    guard = FRONTEND / "lib" / "guard.js"
    for name in _forbidden_names():
        for path in FRONTEND.rglob("*.js"):
            if path == guard:
                continue
            # Match the command as a whole word in a string or template.
            for match in re.finditer(rf"""["'`]\s*{re.escape(name)}\b""", path.read_text()):
                pytest.fail(
                    f"{path.relative_to(FRONTEND)} contains the forbidden command "
                    f"{name!r} as a literal: {match.group(0)!r}"
                )


def test_the_guard_lists_every_forbidden_command_or_family() -> None:
    """Each name is either listed by name or caught by a pattern."""
    guard = (FRONTEND / "lib" / "guard.js").read_text()
    by_name = set(re.findall(r"^\s+\"?([a-z0-9_]+)\"?:", guard, re.M))
    patterns = [
        re.compile(p.replace("\\\\", "\\"))
        for p in re.findall(r"pattern:\s*/([^/]+)/", guard)
    ]
    extra_families = [
        "cc3100_fw_upgrade",
        "feat_enable",
        "feat_disable",
        "encryption_key_set",
        "encryption_mode_set",
        "dcmb",
        "dcmc",
        "dcmd",
        "dcmh",
        "dcms",
        "dcmt",
        "dcmu",
        "dcmw",
        "bsim",
        "bsimi",
        "bsimv",
    ]
    for name in _forbidden_names() + extra_families:
        assert name in by_name or any(p.search(name) for p in patterns), (
            f"{name} is neither in the deny list nor matched by a deny pattern"
        )


def test_the_allow_list_excludes_the_two_commands_with_better_alternatives() -> None:
    """``wifi_conf_set`` splits on whitespace; ``reboot`` blanks the glass."""
    guard = (FRONTEND / "lib" / "guard.js").read_text()
    allow = guard[guard.index("ALLOWED_COMMANDS") :]
    allow = allow[: allow.index("]")]
    names = set(re.findall(r'"([a-z0-9_]+)"', allow))
    assert "wifi_conf_set" not in names
    assert "reboot" not in names
    # The ones commissioning cannot do without.
    assert {"wifi_ssid_set", "wifi_psk_set", "wifi_security_set", "server_tcp_set",
            "flash_save", "cs", "uuid_get", "fw_version_get"} <= names


# ----------------------------------------------------------------- the prefill


class _Runtime:
    def __init__(self, advertised: str, host: str, port: int) -> None:
        self.advertised_address = advertised
        self.host = host
        self.port = port


def test_the_prefilled_address_comes_from_where_the_listener_bound() -> None:
    """The page's own URL is the fallback, not the source of truth.

    ``window.location`` is where the *browser* reached Home Assistant, which
    may be a cloud relay or ``localhost``. Writing ``localhost`` into a sign and
    committing it with ``flash_save`` leaves the cable as the only way back, so
    the address the integration resolved for itself wins.
    """
    config = async_panel_config(_Runtime("192.0.2.50:11113", "0.0.0.0", 11113))
    assert config["listener_host"] == "192.0.2.50"
    assert config["listener_port"] == 11113
    assert config["configured_host"] == "0.0.0.0"


def test_the_prefill_falls_back_to_the_configured_port() -> None:
    config = async_panel_config(_Runtime("", "192.168.1.10", 11113))
    assert config["listener_port"] == 11113
    assert config["listener_host"] == ""


# ------------------------------------------------------------- live in hass


async def test_the_panel_is_registered_and_the_assets_are_served(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_client: Any,
) -> None:
    """Registered in the sidebar, and every asset fetchable over HTTP."""
    panels = hass.data[frontend.DATA_PANELS]
    assert PANEL_URL_PATH in panels
    panel = panels[PANEL_URL_PATH]
    assert panel.component_name == "custom"
    assert panel.require_admin is True, "commissioning writes a sign's flash"

    config = panel.config
    assert config["_panel_custom"]["name"] == PANEL_WEBCOMPONENT
    assert config["_panel_custom"]["embed_iframe"] is False
    module_url = config["_panel_custom"]["module_url"]
    assert module_url.startswith(f"{STATIC_URL}/visionect-panel.js?v=")

    # The address the page will prefill, from the listener rather than the URL.
    assert "listener_host" in config
    assert config["listener_port"] == setup_integration.data["port"]

    client = await hass_client()
    for path in sorted(STATIC_DIR.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(STATIC_DIR).as_posix()
        response = await client.get(f"{STATIC_URL}/{relative}")
        assert response.status == 200, f"{relative} is not served"
        assert await response.text() == path.read_text()


async def test_the_entry_point_is_served_with_its_version_query(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_client: Any,
) -> None:
    """The cache-buster is the manifest version, and the URL still resolves."""
    manifest = json.loads(
        (STATIC_DIR.parent / "manifest.json").read_text(),
    )
    panel = hass.data[frontend.DATA_PANELS][PANEL_URL_PATH]
    module_url = panel.config["_panel_custom"]["module_url"]
    assert module_url.endswith(f"?v={manifest['version']}")

    client = await hass_client()
    response = await client.get(module_url)
    assert response.status == 200
    body = await response.text()
    assert PANEL_WEBCOMPONENT in body


async def test_unloading_takes_the_panel_out_of_the_sidebar(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
) -> None:
    """The sidebar entry goes; the static route cannot, and stays."""
    assert PANEL_URL_PATH in hass.data[frontend.DATA_PANELS]
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert PANEL_URL_PATH not in hass.data[frontend.DATA_PANELS]


async def test_a_reload_re_registers_without_tripping_over_the_static_route(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_client: Any,
) -> None:
    """aiohttp has no route removal, so the second registration must be skipped.

    This is the failure mode a reload would otherwise hit: a duplicate static
    route raises, and the raise would come out of ``async_setup_entry``.
    """
    await hass.config_entries.async_reload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert PANEL_URL_PATH in hass.data[frontend.DATA_PANELS]
    client = await hass_client()
    response = await client.get(f"{STATIC_URL}/visionect-panel.js")
    assert response.status == 200


async def test_a_failed_panel_registration_does_not_fail_setup(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    socket_enabled: None,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sidebar entry is not worth losing the listener over.

    Signs already pointed at this Home Assistant keep working whether or not
    the panel registered; the panel only matters for a sign that is not
    commissioned yet.
    """
    from custom_components.visionect import panel as panel_module

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("no frontend here")

    monkeypatch.setattr(panel_module, "async_register_panel", boom)
    monkeypatch.setattr("custom_components.visionect.async_register_panel", boom)

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert "could not register the commissioning panel" in caplog.text
    assert PANEL_URL_PATH not in hass.data.get(frontend.DATA_PANELS, {})
