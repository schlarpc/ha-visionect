"""Content resolution: the declarative source, and the pixels it becomes.

All of this is pure except the fetch, so most of it is tested directly rather
than through Home Assistant. The fit modes matter more than they look: the sign
is 1440x2560, a portrait panel almost nothing renders for, so "my dashboard
came out tiny and letterboxed" is the single most likely user complaint and the
vocabulary here (``fit`` / ``stretch`` / ``crop``) is the answer to it.
"""

from __future__ import annotations

import io
from unittest.mock import patch

import pytest
from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from PIL import Image
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.visionect.content import (
    BlankSource,
    ContentError,
    EntitySource,
    StaticImage,
    UrlSource,
    classify_image_argument,
    decode_and_fit,
    describe_source,
    fetch_source_bytes,
    render_placeholder,
    render_text,
    source_from_dict,
    source_to_dict,
    white_frame,
)

CANVAS = (1440, 2560)


def png(size: tuple[int, int], colour: int | tuple = 0, mode: str = "L") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, colour).save(buf, format="PNG")
    return buf.getvalue()


# ----------------------------------------------------------------- the model


@pytest.mark.parametrize(
    "source",
    [
        BlankSource(),
        StaticImage(label="a label"),
        EntitySource(entity_id="camera.doorbell"),
        UrlSource(url="http://x.invalid/a.png", headers={"A": "b"}),
    ],
)
def test_sources_round_trip_through_storage(source) -> None:
    """The record is persisted, so a source that cannot round-trip is a bug."""
    assert source_from_dict(source_to_dict(source)) == source


def test_an_unknown_stored_source_degrades_to_blank() -> None:
    """A record written by a future version must not break setup."""
    assert source_from_dict({"kind": "something_new"}) == BlankSource()
    assert source_from_dict(None) == BlankSource()
    assert source_from_dict({}) == BlankSource()


def test_describe_source_is_one_line() -> None:
    assert describe_source(BlankSource()) == "nothing configured"
    assert describe_source(StaticImage(label="x")) == "static image (x)"
    assert describe_source(StaticImage()) == "static image"
    assert describe_source(EntitySource(entity_id="image.a")) == "entity image.a"
    assert describe_source(UrlSource(url="http://a/")) == "url http://a/"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://a.invalid/x.png", UrlSource(url="http://a.invalid/x.png")),
        ("https://a.invalid/x.png", UrlSource(url="https://a.invalid/x.png")),
        ("image.dashboard", EntitySource(entity_id="image.dashboard")),
        ("camera.doorbell", EntitySource(entity_id="camera.doorbell")),
        ("/media/x.png", "/media/x.png"),
    ],
)
def test_the_polymorphic_image_argument(value: str, expected) -> None:
    """One ``image:`` field that accepts a URL, an entity or a path."""
    assert classify_image_argument(value) == expected


# ------------------------------------------------------------------ the fetch


async def test_fetch_a_url(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    body = png((100, 100))
    aioclient_mock.get("http://renderer.invalid/d.png", content=body)
    source = UrlSource(url="http://renderer.invalid/d.png")
    got = await fetch_source_bytes(hass, source, width=1440, height=2560)
    assert got == body


async def test_a_url_may_be_a_template(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Which is how a renderer gets told the panel's size, or today's date."""
    body = png((10, 10))
    aioclient_mock.get("http://renderer.invalid/1440.png", content=body)
    source = UrlSource(url="http://renderer.invalid/{{ 1440 }}.png")
    assert await fetch_source_bytes(hass, source, width=1440, height=2560) == body


async def test_a_failed_fetch_is_a_content_error_naming_the_url(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """ContentError is what the reconciler catches to arm its backoff.

    Anything else would escape as an unhandled exception and the sign would
    keep its old frame with no explanation anywhere.
    """
    aioclient_mock.get("http://renderer.invalid/d.png", exc=ClientError("boom"))
    source = UrlSource(url="http://renderer.invalid/d.png")
    with pytest.raises(ContentError, match="http://renderer.invalid/d.png"):
        await fetch_source_bytes(hass, source, width=1, height=1)


async def test_an_http_error_is_a_content_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get("http://renderer.invalid/d.png", status=503)
    with pytest.raises(ContentError):
        await fetch_source_bytes(
            hass, UrlSource(url="http://renderer.invalid/d.png"), width=1, height=1
        )


async def test_a_non_image_entity_is_refused_with_advice(
    hass: HomeAssistant
) -> None:
    with pytest.raises(ContentError, match="not an image.* or camera.*"):
        await fetch_source_bytes(
            hass, EntitySource(entity_id="sensor.cpu"), width=1, height=1
        )


async def test_a_missing_image_entity_names_itself(hass: HomeAssistant) -> None:
    with pytest.raises(ContentError, match="image.nope"):
        await fetch_source_bytes(
            hass, EntitySource(entity_id="image.nope"), width=1, height=1
        )


async def test_a_camera_failure_names_the_entity(hass: HomeAssistant) -> None:
    """Patched rather than left to fail by itself: the real failure in this
    environment is an ImportError inside the camera component, which would be
    testing the test rig rather than this code."""
    with patch(
        "homeassistant.components.camera.async_get_image",
        side_effect=RuntimeError("no stream"),
    ):
        with pytest.raises(ContentError, match="camera.doorbell"):
            await fetch_source_bytes(
                hass, EntitySource(entity_id="camera.doorbell"), width=1, height=1
            )


async def test_a_camera_is_asked_to_render_at_the_panel_size(
    hass: HomeAssistant,
) -> None:
    """So a camera that can render natively avoids a resample."""
    from homeassistant.components.camera import Image as CameraImage

    body = png((10, 10))
    with patch(
        "homeassistant.components.camera.async_get_image",
        return_value=CameraImage(content_type="image/png", content=body),
    ) as get_image:
        got = await fetch_source_bytes(
            hass, EntitySource(entity_id="camera.doorbell"), width=1440, height=2560
        )
    assert got == body
    assert get_image.call_args.kwargs == {"width": 1440, "height": 2560}


async def test_an_image_entity_is_read_through_its_component(
    hass: HomeAssistant,
) -> None:
    from homeassistant.components.image import Image as HaImage

    body = png((10, 10))
    with patch(
        "homeassistant.components.image.async_get_image",
        return_value=HaImage(content_type="image/png", content=body),
    ):
        got = await fetch_source_bytes(
            hass, EntitySource(entity_id="image.dash"), width=1440, height=2560
        )
    assert got == body


async def test_blank_and_static_have_nothing_to_fetch(hass: HomeAssistant) -> None:
    """Their bytes come from the placeholder renderer and from disk."""
    for source in (BlankSource(), StaticImage()):
        with pytest.raises(ContentError, match="no bytes to fetch"):
            await fetch_source_bytes(hass, source, width=1, height=1)


# ------------------------------------------------------------------ the pixels


def test_fit_letterboxes_and_keeps_the_aspect() -> None:
    out = decode_and_fit(png((400, 200)), width=1440, height=2560, fit="fit")
    assert out.size == CANVAS
    assert out.mode == "L"
    # A 2:1 source on a 0.56:1 panel: full width, and white above and below.
    assert out.getpixel((720, 0)) == 255
    assert out.getpixel((720, 2559)) == 255
    assert out.getpixel((720, 1280)) == 0


def test_stretch_ignores_the_aspect() -> None:
    out = decode_and_fit(png((400, 200)), width=1440, height=2560, fit="stretch")
    assert out.size == CANVAS
    # Nothing is left over, so the corners are the image.
    assert out.getpixel((0, 0)) == 0
    assert out.getpixel((1439, 2559)) == 0


def test_crop_fills_and_clips() -> None:
    out = decode_and_fit(png((400, 200)), width=1440, height=2560, fit="crop")
    assert out.size == CANVAS
    assert out.getpixel((0, 0)) == 0
    assert out.getpixel((720, 1280)) == 0


def test_the_background_is_white_because_the_paper_is() -> None:
    """Letterboxing to white is invisible on e-ink; black would be a frame."""
    out = decode_and_fit(png((400, 200)), width=1440, height=2560)
    assert out.getpixel((720, 5)) == 255


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("#000000", 0),
        ("#ffffff", 255),
        ("#80", 128),
        ("not a colour", 255),
        ("", 255),
        ("#zzzzzz", 255),
    ],
)
def test_the_background_colour_grammar(spec: str, expected: int) -> None:
    # A wide source on a square canvas, so there is a letterbox to look at.
    out = decode_and_fit(png((64, 16)), width=64, height=64, background=spec)
    assert out.getpixel((0, 0)) == expected


def test_transparency_is_flattened_onto_the_paper_not_onto_black() -> None:
    """The bug this guards: alpha becoming black, which on e-ink is a solid slab."""
    buf = io.BytesIO()
    Image.new("RGBA", (64, 64), (0, 0, 0, 0)).save(buf, format="PNG")
    out = decode_and_fit(buf.getvalue(), width=64, height=64, fit="stretch")
    assert out.getpixel((32, 32)) == 255


def test_a_palette_image_is_flattened_too() -> None:
    buf = io.BytesIO()
    Image.new("P", (32, 32), 0).save(buf, format="PNG")
    out = decode_and_fit(buf.getvalue(), width=64, height=64)
    assert out.mode == "L"


def test_undecodable_bytes_are_a_content_error() -> None:
    with pytest.raises(ContentError, match="not a decodable image"):
        decode_and_fit(b"this is not a png", width=64, height=64)


def test_render_text_fills_the_canvas() -> None:
    out = render_text("Dinner at 7", width=720, height=1280, size=90)
    assert out.size == (720, 1280)
    assert out.mode == "L"
    histogram = out.histogram()
    assert histogram[255] > 0, "white paper"
    assert sum(histogram[:128]) > 0, "and some ink"


def test_render_text_can_be_inverted() -> None:
    normal = render_text("x", width=200, height=200, size=40)
    inverted = render_text("x", width=200, height=200, size=40, invert=True)
    assert normal.getpixel((0, 0)) != inverted.getpixel((0, 0))


@pytest.mark.parametrize("align", ["left", "center", "right"])
def test_render_text_alignment(align: str) -> None:
    out = render_text("hello", width=400, height=200, size=40, align=align)
    assert out.size == (400, 200)


def test_render_text_wraps_rather_than_overflowing() -> None:
    out = render_text("word " * 60, width=400, height=400, size=40)
    assert out.size == (400, 400)


def test_the_placeholder_says_what_to_do_next() -> None:
    """The first thing on the glass, on a sign with no content source."""
    out = render_placeholder(
        width=480, height=800, name="Kitchen sign", address="10.0.0.5:11113"
    )
    assert out.size == (480, 800)
    assert sum(out.histogram()[:128]) > 0


def test_white_frame_is_the_paper_colour() -> None:
    out = white_frame(width=64, height=64)
    assert out.getpixel((0, 0)) == 255
    assert out.histogram()[255] == 64 * 64
