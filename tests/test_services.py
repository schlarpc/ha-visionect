"""The actions, and above all the targeting.

Targeting gets most of the attention because it is where this integration has
already drawn blood twice. The sign is the unit of meaning, so every handler
wants a UUID -- but the obvious first call names the one thing with a visible
name, which is an entity, and a service that answers that with a bare 400
reads as broken rather than as particular. And a REST caller that copies the
YAML shape sends ``{"target": {...}}`` as the *data*, because the REST API
hands the request body to ``async_call`` unchanged.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.visionect.const import DOMAIN

from .conftest import DEVICE_UUID, FakeSign

PREFIX = "visionect_sign_00112233"


def device_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, DEVICE_UUID), entry.entry_id
    )
    assert device is not None
    return device.id


async def call(hass: HomeAssistant, service: str, data: dict) -> dict:
    return await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=True
    )


# ---------------------------------------------------------------- targeting


async def test_target_by_device_id(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    response = await call(
        hass, "update_now", {"device_id": device_id(hass, setup_integration)}
    )
    assert [r["uuid"] for r in response["results"]] == [DEVICE_UUID]
    assert response["results"][0]["applied_immediately"] is True


async def test_target_by_entity_id(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """The obvious first call: name the image entity you can see."""
    response = await call(
        hass, "update_now", {"entity_id": f"image.{PREFIX}_screen"}
    )
    assert [r["uuid"] for r in response["results"]] == [DEVICE_UUID]


async def test_target_by_a_diagnostic_entity(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """Any entity belonging to a sign resolves to that sign."""
    response = await call(
        hass, "update_now", {"entity_id": f"sensor.{PREFIX}_battery"}
    )
    assert [r["uuid"] for r in response["results"]] == [DEVICE_UUID]


async def test_target_by_area_id(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """"Every sign in the kitchen" has to work, and nearly did not.

    Every entity a sign has except the screen image is a diagnostic or config
    entity, so Home Assistant's default "primary entities only" expansion
    resolves an area containing a sign to nothing at all.
    """
    entry = setup_integration
    area = ar.async_get(hass).async_get_or_create("Kitchen")
    dr.async_get(hass).async_update_device(
        device_id(hass, entry), area_id=area.id
    )
    await hass.async_block_till_done()

    response = await call(hass, "update_now", {"area_id": area.id})
    assert [r["uuid"] for r in response["results"]] == [DEVICE_UUID]


async def test_target_by_an_area_with_no_sign_in_it(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    area = ar.async_get(hass).async_get_or_create("Garage")
    with pytest.raises(ServiceValidationError, match="No Visionect sign"):
        await call(hass, "update_now", {"area_id": area.id})


async def test_target_nested_the_way_the_rest_api_sends_it(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    response = await call(
        hass, "update_now", {"target": {"entity_id": [f"image.{PREFIX}_screen"]}}
    )
    assert [r["uuid"] for r in response["results"]] == [DEVICE_UUID]


async def test_no_target_at_all_is_a_schema_error(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """A schema failure, not a handler error, and the reason is practical.

    A ServiceValidationError raised inside a handler comes back over the REST
    API as a bare 500 with no message; a schema failure comes back as a 400
    carrying the text.
    """
    with pytest.raises(vol_error := __import__("voluptuous").MultipleInvalid):
        await call(hass, "update_now", {})
    assert vol_error is not None


async def test_an_unknown_device_targets_nothing(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    with pytest.raises(ServiceValidationError, match="No Visionect sign"):
        await call(hass, "update_now", {"device_id": "not-a-device"})


async def test_a_non_visionect_entity_is_ignored_not_refused(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """Targeting an area with a sign and a lamp in it should act on the sign."""
    hass.states.async_set("light.kitchen", "on")
    response = await call(
        hass,
        "update_now",
        {"entity_id": [f"image.{PREFIX}_screen", "light.kitchen"]},
    )
    assert [r["uuid"] for r in response["results"]] == [DEVICE_UUID]


async def test_the_listener_device_is_not_a_sign(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    entry = setup_integration
    listener = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"listener-{entry.entry_id}"), entry.entry_id
    )
    with pytest.raises(ServiceValidationError, match="No Visionect sign"):
        await call(hass, "update_now", {"device_id": listener.id})


async def test_services_without_a_loaded_entry(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    entry = setup_integration
    await sign.close()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(HomeAssistantError, match="is not set up"):
        await call(hass, "update_now", {"device_id": "anything"})


# ----------------------------------------------------------------- the work


async def test_display_text(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    from pyvisionect.packets import ImagePacket

    sign.sent.clear()
    response = await call(
        hass,
        "display_text",
        {"device_id": device_id(hass, setup_integration), "message": "Dinner at 7"},
    )
    await sign.pump(hass)
    result = response["results"][0]
    assert result["queued"] is True
    assert result["applied_immediately"] is True
    assert sign.sent_of_type(ImagePacket), "the frame should have gone out"


async def test_display_text_while_the_sign_is_away(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """Nothing can wake the sign, so "queued, expected by X" is the honest answer."""
    entry = setup_integration
    target = device_id(hass, entry)
    await sign.disconnect(hass, entry.runtime_data)

    response = await call(
        hass, "display_text", {"device_id": target, "message": "later"}
    )
    result = response["results"][0]
    assert result["queued"] is True
    assert result["applied_immediately"] is False
    # The sign announced a one-minute heartbeat in the capture, so there is a
    # real time to report rather than a shrug.
    assert result["expected_at"] is not None


async def test_set_content_source_and_clear(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    runtime = setup_integration.runtime_data
    target = device_id(hass, setup_integration)

    await call(
        hass,
        "set_content_source",
        {"device_id": target, "source": "url", "url": "http://example.invalid/x.png"},
    )
    from custom_components.visionect.content import BlankSource, UrlSource

    assert isinstance(runtime.record(DEVICE_UUID).source, UrlSource)

    await call(hass, "clear_content", {"device_id": target})
    assert isinstance(runtime.record(DEVICE_UUID).source, BlankSource)


async def test_set_content_source_needs_its_argument(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    target = device_id(hass, setup_integration)
    with pytest.raises(ServiceValidationError, match="needs a url"):
        await call(hass, "set_content_source", {"device_id": target, "source": "url"})
    with pytest.raises(ServiceValidationError, match="entity_id_source"):
        await call(
            hass, "set_content_source", {"device_id": target, "source": "entity"}
        )


async def test_refresh_forces_a_full_screen_push(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """Every push to this hardware is full screen, so re-pushing *is* a refresh."""
    runtime = setup_integration.runtime_data
    await call(hass, "refresh", {"device_id": device_id(hass, setup_integration)})
    # force_next is consumed by the push that it forced.
    await sign.pump(hass)
    assert runtime.record(DEVICE_UUID).last_frame_full is True


async def test_clear_screen_pushes_white(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    from pyvisionect.packets import ImagePacket

    sign.sent.clear()
    await call(hass, "clear_screen", {"device_id": device_id(hass, setup_integration)})
    await sign.pump(hass)
    assert sign.sent_of_type(ImagePacket)
    assert setup_integration.runtime_data.record(DEVICE_UUID).static_label == (
        "clear_screen"
    )


async def test_read_and_write_parameters(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    from pyvisionect.packets import ParamPacket

    target = device_id(hass, setup_integration)
    sign.sent.clear()
    await call(hass, "write_parameters", {"device_id": target, "values": {"29": 30}})
    await sign.pump(hass)
    ids = {item.id for p in sign.sent_of_type(ParamPacket) for item in p.items}
    assert 29 in ids

    sign.sent.clear()
    response = await call(hass, "read_parameters", {"device_id": target, "ids": [29]})
    await sign.pump(hass)
    assert "known" in response["results"][0]


async def test_write_parameters_rejects_junk(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    target = device_id(hass, setup_integration)
    with pytest.raises(ServiceValidationError, match="mapping of TCLV id"):
        await call(
            hass, "write_parameters", {"device_id": target, "values": {"29": "soon"}}
        )


async def test_write_parameters_refuses_a_read_only_id(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """The library names the USB command that can do it; surface that."""
    # 2, 18, 19, 65-68 and 70 are the eight the network cannot write.
    target = device_id(hass, setup_integration)
    with pytest.raises(ServiceValidationError):
        await call(
            hass, "write_parameters", {"device_id": target, "values": {"2": 1}}
        )


async def test_display_image_refuses_a_path_outside_the_allowlist(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    target = device_id(hass, setup_integration)
    with pytest.raises(ServiceValidationError, match="allowlist_external_dirs"):
        await call(
            hass, "display_image", {"device_id": target, "image": "/etc/passwd"}
        )


async def test_display_image_sets_a_live_entity_source(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """An entity id means "re-read it at every wake", which is what was meant."""
    from custom_components.visionect.content import EntitySource

    runtime = setup_integration.runtime_data
    response = await call(
        hass,
        "display_image",
        {
            "device_id": device_id(hass, setup_integration),
            "image": "camera.doorbell",
        },
    )
    assert response["results"][0]["source"] == "entity"
    assert isinstance(runtime.record(DEVICE_UUID).source, EntitySource)


async def test_buttons_run_the_same_code_as_the_services(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    runtime = setup_integration.runtime_data
    before = runtime.record(DEVICE_UUID).want_revision
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": f"button.{PREFIX}_update_now"},
        blocking=True,
    )
    await sign.pump(hass)
    assert runtime.record(DEVICE_UUID).want_revision > before


async def test_list_device_files_needs_a_connection(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    """Away: the request is queued rather than answered with a stale dict."""
    entry = setup_integration
    target = device_id(hass, entry)
    await sign.disconnect(hass, entry.runtime_data)

    response = await call(hass, "list_device_files", {"device_id": target})
    assert response["results"][0]["queued"] is True
    assert response["results"][0]["files"] == {}


async def test_read_device_file_needs_a_connection(
    hass: HomeAssistant, setup_integration: MockConfigEntry, sign: FakeSign
) -> None:
    entry = setup_integration
    target = device_id(hass, entry)
    await sign.disconnect(hass, entry.runtime_data)
    with pytest.raises(HomeAssistantError, match="not connected"):
        await call(hass, "read_device_file", {"device_id": target})
