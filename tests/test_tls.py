"""Opportunistic TLS on the device port.

Worth testing carefully because the failure is one-way and expensive: a sign
whose TLS parameter (TCLV 145) is set to 1 and whose listener has no
certificate cannot be reached over the network at all and has to be recovered
over USB serial. Hence the order the integration enforces -- certificate first,
parameter second -- and hence a certificate that has gone missing since it was
configured must still leave a *plaintext listener up*, because an unreachable
server makes this firmware power-cycle itself roughly every twenty minutes.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.visionect.const import (
    CONF_PERSIST_PARAMS,
    CONF_TLS_CERTFILE,
    CONF_TLS_KEYFILE,
)


@pytest.fixture
def certificate(tmp_path: Path) -> tuple[Path, Path]:
    """A throwaway self-signed certificate and its key, as two PEM files."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "visionect-test")]
    )
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    certfile = tmp_path / "cert.pem"
    keyfile = tmp_path / "key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certfile, keyfile


async def test_the_options_flow_accepts_a_real_certificate(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    certificate: tuple[Path, Path],
) -> None:
    """Validated in the form, where the user is looking, not at bind time."""
    entry = setup_integration
    certfile, keyfile = certificate

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_PERSIST_PARAMS: True,
            CONF_TLS_CERTFILE: str(certfile),
            CONF_TLS_KEYFILE: str(keyfile),
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_TLS_CERTFILE] == str(certfile)

    # A TLS change is one of the few that does reload, because it decides how
    # the socket is wrapped.
    assert entry.runtime_data.tls_enabled is True
    assert entry.runtime_data.listener_running is True


async def test_whitespace_around_the_path_is_forgiven(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    certificate: tuple[Path, Path],
) -> None:
    entry = setup_integration
    certfile, keyfile = certificate
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_PERSIST_PARAMS: True,
            CONF_TLS_CERTFILE: f"  {certfile}  ",
            CONF_TLS_KEYFILE: f"  {keyfile}  ",
        },
    )
    await hass.async_block_till_done()
    assert entry.options[CONF_TLS_CERTFILE] == str(certfile)


async def test_a_certificate_that_vanished_still_leaves_a_listener_up(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    socket_enabled: None,
    certificate: tuple[Path, Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The config flow validated it, so reaching this means the file moved.

    Refusing to start would be strictly worse than plaintext: nothing
    answering on the port makes this firmware power-cycle itself.
    """
    from homeassistant.setup import async_setup_component

    certfile, keyfile = certificate
    assert await async_setup_component(hass, "homeassistant", {})
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        options={
            **config_entry.options,
            CONF_TLS_CERTFILE: str(certfile),
            CONF_TLS_KEYFILE: str(keyfile),
        },
    )
    certfile.unlink()

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    try:
        runtime = config_entry.runtime_data
        assert runtime.listener_running is True
        assert runtime.tls_enabled is False
        # And it says so loudly, naming the recovery.
        assert "could not load the TLS certificate" in caplog.text
        assert "USB serial" in caplog.text
    finally:
        await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()


async def test_no_certificate_means_no_tls_and_no_complaint(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Unset is the default, and that is not timidity.

    On the firmware this was developed against, a read of TCLV 145 comes back
    as a read error -- the same answer as a parameter that does not exist -- so
    TLS buys nothing there, while a listener that refused to start without a
    certificate would buy a reboot loop.
    """
    runtime = setup_integration.runtime_data
    assert runtime.tls_enabled is False
    assert runtime.listener_running is True
    assert runtime.server.stats.tls_accepted == 0
