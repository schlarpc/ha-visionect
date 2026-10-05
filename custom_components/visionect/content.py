"""The content model: a declarative source, resolved at push time.

The sign is asleep most of the time, so "show X" cannot mean "push bytes now".
It means "this sign displays X", and the source is re-read at the moment the
device is actually reachable.  That is why a camera entity set once keeps
showing a fresh snapshot forever with no automation at all.

Every resolve path ends in ``_decode_and_fit``, which runs in an executor: PIL
decoding plus a resample onto a 1440x2560 canvas is far too much work for the
event loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from aiohttp import ClientTimeout
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.template import Template

from .const import DEFAULT_BACKGROUND, DEFAULT_FIT

_LOGGER = logging.getLogger(__name__)

# PIL is a hard dependency of pyvisionect.imaging, which we need anyway.
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
)


def load_font(size: int) -> Any:
    """A bundled-ish sans face at *size*, or PIL's bitmap default."""
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:  # Pillow < 10.1 has no size argument
        return ImageFont.load_default()


# --------------------------------------------------------------------------
# the source union
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BlankSource:
    """Nothing configured. Renders a placeholder naming the sign."""


@dataclass(frozen=True, slots=True)
class StaticImage:
    """A literal frame. The bytes live in a file beside the Store."""

    label: str = ""


@dataclass(frozen=True, slots=True)
class EntitySource:
    """An ``image.*`` or ``camera.*`` entity, re-read at every push."""

    entity_id: str = ""


@dataclass(frozen=True, slots=True)
class UrlSource:
    """An HTTP GET at push time. The dashboard-rendering answer."""

    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0


ContentSource = BlankSource | StaticImage | EntitySource | UrlSource


def source_to_dict(src: ContentSource) -> dict[str, Any]:
    """JSON-safe form for the Store."""
    match src:
        case StaticImage(label=label):
            return {"type": "static", "label": label}
        case EntitySource(entity_id=eid):
            return {"type": "entity", "entity_id": eid}
        case UrlSource(url=url, headers=hdrs, timeout=timeout):
            return {"type": "url", "url": url, "headers": dict(hdrs), "timeout": timeout}
        case _:
            return {"type": "blank"}


def source_from_dict(raw: dict[str, Any] | None) -> ContentSource:
    """Inverse of :func:`source_to_dict`; unknown shapes degrade to blank."""
    if not raw:
        return BlankSource()
    match raw.get("type"):
        case "static":
            return StaticImage(label=raw.get("label", ""))
        case "entity":
            return EntitySource(entity_id=raw.get("entity_id", ""))
        case "url":
            return UrlSource(
                url=raw.get("url", ""),
                headers=dict(raw.get("headers") or {}),
                timeout=float(raw.get("timeout", 30.0)),
            )
        case _:
            return BlankSource()


def describe_source(src: ContentSource) -> str:
    """One line for the UI and for diagnostics."""
    match src:
        case StaticImage(label=label):
            return f"static image ({label})" if label else "static image"
        case EntitySource(entity_id=eid):
            return f"entity {eid}"
        case UrlSource(url=url):
            return f"url {url}"
        case _:
            return "nothing configured"


# --------------------------------------------------------------------------
# classifying what the user typed into visionect.display_image
# --------------------------------------------------------------------------


def classify_image_argument(value: str) -> ContentSource | str:
    """Resolve the polymorphic ``image:`` field by shape.

    Returns a :class:`ContentSource` for the live and remote cases, or a plain
    local path string for the "read this file now" case.
    """
    text = value.strip()
    if text.startswith(("http://", "https://")):
        return UrlSource(url=text)
    if "." in text and text.split(".", 1)[0] in ("image", "camera"):
        return EntitySource(entity_id=text)
    return text


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------


class ContentError(Exception):
    """A source could not be turned into pixels."""


async def fetch_source_bytes(
    hass: HomeAssistant, src: ContentSource, *, width: int, height: int
) -> bytes:
    """Get the raw encoded bytes for *src*. Does no decoding."""
    match src:
        case EntitySource(entity_id=eid) if eid.startswith("image."):
            from homeassistant.components import image as image_component

            try:
                return (await image_component.async_get_image(hass, eid)).content
            except Exception as err:  # noqa: BLE001 - surfaced to the user
                raise ContentError(f"could not read {eid}: {err}") from err
        case EntitySource(entity_id=eid) if eid.startswith("camera."):
            from homeassistant.components import camera as camera_component

            try:
                # "width and height will be passed to the underlying camera",
                # so a camera that can render natively avoids a resample.
                return (
                    await camera_component.async_get_image(
                        hass, eid, width=width, height=height
                    )
                ).content
            except Exception as err:  # noqa: BLE001
                raise ContentError(f"could not read {eid}: {err}") from err
        case EntitySource(entity_id=eid):
            raise ContentError(
                f"{eid} is not an image.* or camera.* entity"
            )
        case UrlSource(url=url, headers=hdrs, timeout=timeout):
            rendered = Template(url, hass).async_render(parse_result=False)
            session = async_get_clientsession(hass)
            try:
                async with session.get(
                    rendered, headers=hdrs, timeout=ClientTimeout(total=timeout)
                ) as resp:
                    resp.raise_for_status()
                    return await resp.read()
            except Exception as err:  # noqa: BLE001
                raise ContentError(f"could not fetch {rendered}: {err}") from err
        case _:
            raise ContentError("this source has no bytes to fetch")


# --------------------------------------------------------------------------
# the executor half -- all of this is CPU work
# --------------------------------------------------------------------------


def _parse_colour(value: str) -> int:
    """A ``#RRGGBB`` or ``#GG`` string as an 8-bit grey level."""
    text = (value or DEFAULT_BACKGROUND).lstrip("#")
    try:
        if len(text) == 2:
            return int(text, 16)
        if len(text) == 6:
            r, g, b = (int(text[i : i + 2], 16) for i in (0, 2, 4))
            return int(0.299 * r + 0.587 * g + 0.114 * b)
    except ValueError:
        pass
    return 255


def decode_and_fit(
    raw: bytes,
    *,
    width: int,
    height: int,
    fit: str = DEFAULT_FIT,
    background: str = DEFAULT_BACKGROUND,
) -> Image.Image:
    """Decode *raw* and place it on a ``width x height`` greyscale canvas.

    Blocking and CPU-bound; call via ``hass.async_add_executor_job``.

    The vendor's vocabulary, which is already proven against this hardware:
    ``fit`` letterboxes, ``stretch`` ignores aspect, ``crop`` fills and clips.
    The background defaults to white because the paper is white, so
    letterboxing to white is invisible.
    """
    import io

    try:
        src = Image.open(io.BytesIO(raw))
        src.load()
    except Exception as err:  # noqa: BLE001
        raise ContentError(f"not a decodable image: {err}") from err

    if src.mode in ("RGBA", "LA", "P"):
        # Flatten onto the paper colour rather than letting alpha become black.
        flat = Image.new("RGBA", src.size, (255, 255, 255, 255))
        flat.alpha_composite(src.convert("RGBA"))
        src = flat
    grey = src.convert("L")

    bg = _parse_colour(background)
    canvas = Image.new("L", (width, height), bg)

    if fit == "stretch":
        canvas.paste(grey.resize((width, height), Image.LANCZOS), (0, 0))
        return canvas

    sw, sh = grey.size
    if not sw or not sh:
        raise ContentError("image has a zero dimension")
    scale = max(width / sw, height / sh) if fit == "crop" else min(width / sw, height / sh)
    new = (max(1, round(sw * scale)), max(1, round(sh * scale)))
    resized = grey.resize(new, Image.LANCZOS)
    canvas.paste(resized, ((width - new[0]) // 2, (height - new[1]) // 2))
    return canvas


def render_text(
    message: str,
    *,
    width: int,
    height: int,
    size: int = 96,
    align: str = "center",
    invert: bool = False,
) -> Image.Image:
    """One font, one size, word-wrapped, centred vertically. Deliberately small.

    Anything more elaborate than this belongs in an HTML page behind a
    ``url`` content source, where there is a real layout engine.
    """
    fg, bg = (255, 0) if invert else (0, 255)
    img = Image.new("L", (width, height), bg)
    draw = ImageDraw.Draw(img)
    font = load_font(max(8, int(size)))
    margin = max(24, width // 24)
    usable = width - 2 * margin

    lines: list[str] = []
    for paragraph in message.replace("\r", "").split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        current = ""
        for word in paragraph.split():
            trial = f"{current} {word}".strip()
            if draw.textlength(trial, font=font) <= usable or not current:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)

    ascent, descent = font.getmetrics() if hasattr(font, "getmetrics") else (size, 0)
    line_h = int((ascent + descent) * 1.25) or size
    total = line_h * len(lines)
    y = max(margin, (height - total) // 2)
    for line in lines:
        w = draw.textlength(line, font=font)
        if align == "left":
            x = margin
        elif align == "right":
            x = width - margin - w
        else:
            x = (width - w) / 2
        draw.text((x, y), line, font=font, fill=fg)
        y += line_h
    return img


def render_placeholder(
    *, width: int, height: int, name: str, address: str
) -> Image.Image:
    """What a sign with no content source shows: who it is and where we are."""
    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, width - 1, height // 10], fill=0)
    draw.text((width // 24, height // 40), "Home Assistant", font=load_font(height // 34), fill=255)
    y = height // 7
    for text, px in (
        (name, height // 28),
        ("", 0),
        ("This sign is connected but no content", height // 50),
        ("source has been configured yet.", height // 50),
        ("", 0),
        (f"Listening on {address}", height // 60),
        ("", 0),
        ("Set one with the visionect.set_content_source", height // 64),
        ("action, or push a picture with", height // 64),
        ("visionect.display_image.", height // 64),
    ):
        if text:
            draw.text((width // 24, y), text, font=load_font(max(10, px)), fill=0)
        y += int(px * 1.6) + height // 90
    return img


def white_frame(*, width: int, height: int) -> Image.Image:
    """An all-white canvas -- how ``clear_screen`` is implemented."""
    return Image.new("L", (width, height), 255)
