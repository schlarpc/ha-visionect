"""Actions.

Every action that cannot complete now returns ``{"queued": true, ...}`` rather
than nothing, because "queued, expected by 14:32" is the whole user experience
for a device that is unreachable between contacts.  Lying about completion is
how you get bug reports about a working integration.

When the sign *does* happen to be connected -- which, on a mains-powered sign,
is always -- ``applied_immediately`` comes back true and the frame lands in
seconds.  The deferred machinery is invisible in the common case and correct in
the hard one.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Any

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

# Area / floor / label expansion moved from helpers.service to helpers.target,
# and the newer function takes a TargetSelection rather than a ServiceCall.
# Both are supported rather than pinning a core version, because getting this
# wrong costs the whole integration: an ImportError at module scope means
# "Setup failed for custom integration", not a degraded target picker.
try:
    from homeassistant.helpers.target import (
        TargetSelection,
        async_extract_referenced_entity_ids,
    )
except ImportError:  # pragma: no cover - older cores
    TargetSelection = None  # type: ignore[assignment]
    try:
        from homeassistant.helpers.service import (
            async_extract_referenced_entity_ids,
        )
    except ImportError:  # pragma: no cover - no expansion available at all
        async_extract_referenced_entity_ids = None  # type: ignore[assignment]

from pyvisionect.wire.errors import ReadOnlyParameter

from pyvisionect.packets.file import DEVICE_IMAGE_FILES

from .const import (
    DEVICE_FILE_ATTEMPTS,
    DEVICE_FILE_TIMEOUT,
    DITHER_MODES,
    DOMAIN,
    ENCODINGS,
    FIT_MODES,
)
from .content import (
    BlankSource,
    ContentError,
    EntitySource,
    UrlSource,
    classify_image_argument,
    decode_and_fit,
    fetch_source_bytes,
    render_text,
    white_frame,
)

_LOGGER = logging.getLogger(__name__)

ATTR_DEVICE_ID = "device_id"
ATTR_ENTITY_ID = "entity_id"
ATTR_AREA_ID = "area_id"
ATTR_FLOOR_ID = "floor_id"
ATTR_LABEL_ID = "label_id"
ATTR_TARGET = "target"

SERVICE_DISPLAY_IMAGE = "display_image"
SERVICE_DISPLAY_TEXT = "display_text"
SERVICE_SET_CONTENT_SOURCE = "set_content_source"
SERVICE_CLEAR_CONTENT = "clear_content"
SERVICE_UPDATE_NOW = "update_now"
SERVICE_REFRESH = "refresh"
SERVICE_CLEAR_SCREEN = "clear_screen"
SERVICE_GHOST_CLEAR = "ghost_clear"
SERVICE_READ_PARAMETERS = "read_parameters"
SERVICE_WRITE_PARAMETERS = "write_parameters"
SERVICE_LIST_DEVICE_FILES = "list_device_files"
SERVICE_READ_DEVICE_FILE = "read_device_file"

_TARGET = {
    # The sign is the unit of meaning, so a device target is the natural one.
    # Every other standard target form is accepted too, and the reason is not
    # tidiness: the obvious first call is `entity_id`, naming the image entity
    # the user can see, and a service that answers that with a bare 400 reads
    # as broken rather than as particular.
    vol.Optional(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
    vol.Optional(ATTR_AREA_ID): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(ATTR_FLOOR_ID): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(ATTR_LABEL_ID): vol.All(cv.ensure_list, [cv.string]),
    # A REST or script caller that copies the YAML shape sends
    # {"target": {"entity_id": ...}} as the *data*, because the REST API hands
    # the whole body to async_call unchanged. Without this key that is "extra
    # keys not allowed" -- a 400 with no message, which is exactly how this
    # was found.
    vol.Optional(ATTR_TARGET): vol.Schema(
        {
            vol.Optional(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
            vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
            vol.Optional(ATTR_AREA_ID): vol.All(cv.ensure_list, [cv.string]),
            vol.Optional(ATTR_FLOOR_ID): vol.All(cv.ensure_list, [cv.string]),
            vol.Optional(ATTR_LABEL_ID): vol.All(cv.ensure_list, [cv.string]),
        }
    ),
}

_TARGET_KEYS = (
    ATTR_DEVICE_ID,
    ATTR_ENTITY_ID,
    ATTR_AREA_ID,
    ATTR_FLOOR_ID,
    ATTR_LABEL_ID,
    ATTR_TARGET,
)


def _targeted(schema: dict) -> Any:
    """Require at least one target key, as a schema rather than at run time.

    A ``ServiceValidationError`` raised inside the handler comes back over the
    REST API as a bare ``500 Internal Server Error`` with no message at all,
    which is a miserable thing to debug. A schema failure comes back as a 400
    carrying the text. So "you did not say which sign" is a schema rule.
    """
    return vol.All(vol.Schema(schema), cv.has_at_least_one_key(*_TARGET_KEYS))


TARGET_ONLY_SCHEMA = _targeted(_TARGET)

DISPLAY_IMAGE_SCHEMA = _targeted(
    {
        **_TARGET,
        vol.Required("image"): cv.string,
        vol.Optional("snapshot"): cv.boolean,
        vol.Optional("fit"): vol.In(FIT_MODES),
        vol.Optional("background"): cv.string,
        vol.Optional("dither"): vol.In(list(DITHER_MODES)),
        vol.Optional("encoding"): vol.In(list(ENCODINGS)),
    }
)

DISPLAY_TEXT_SCHEMA = _targeted(
    {
        **_TARGET,
        vol.Required("message"): cv.string,
        vol.Optional("size", default=110): vol.All(vol.Coerce(int), vol.Range(12, 400)),
        vol.Optional("align", default="center"): vol.In(["left", "center", "right"]),
        vol.Optional("invert", default=False): cv.boolean,
    }
)

SET_CONTENT_SOURCE_SCHEMA = _targeted(
    {
        **_TARGET,
        vol.Required("source"): vol.In(["entity", "url", "blank"]),
        vol.Optional("entity_id_source"): cv.entity_id,
        vol.Optional("url"): cv.string,
        vol.Optional("headers"): dict,
    }
)


READ_DEVICE_FILE_SCHEMA = _targeted(
    {
        **_TARGET,
        vol.Optional("filename", default=DEVICE_IMAGE_FILES[0]): cv.string,
        vol.Optional("decode", default=True): cv.boolean,
        vol.Optional("timeout", default=DEVICE_FILE_TIMEOUT): vol.All(
            vol.Coerce(float), vol.Range(min=1.0, max=300.0)
        ),
        vol.Optional("attempts", default=DEVICE_FILE_ATTEMPTS): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=10)
        ),
    }
)

READ_PARAMETERS_SCHEMA = _targeted(
    {**_TARGET, vol.Required("ids"): vol.All(cv.ensure_list, [vol.Coerce(int)])}
)

WRITE_PARAMETERS_SCHEMA = _targeted(
    {
        **_TARGET,
        vol.Required("values"): dict,
        vol.Optional("persist"): cv.boolean,
    }
)


# ---------------------------------------------------------------- targeting


def _runtime(hass: HomeAssistant) -> Any:
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        if hasattr(entry, "runtime_data"):
            return entry.runtime_data
    raise HomeAssistantError(
        "The Visionect integration is not set up, so there is nothing to act on."
    )


def _uuid_of_device(device: dr.DeviceEntry) -> str | None:
    for domain, identifier in device.identifiers:
        if domain == DOMAIN and not identifier.startswith("listener-"):
            return identifier
    return None


def _merged_target(call: ServiceCall) -> dict[str, list[str]]:
    """Flatten the target, whichever shape the caller used.

    An automation's ``target:`` block is merged into the service data by Home
    Assistant before the handler sees it, but the REST API passes the request
    body through verbatim, so both shapes turn up in the wild.
    """
    out: dict[str, list[str]] = {}
    nested = call.data.get(ATTR_TARGET) or {}
    for key in (ATTR_DEVICE_ID, ATTR_ENTITY_ID, ATTR_AREA_ID, ATTR_FLOOR_ID,
                ATTR_LABEL_ID):
        values: list[str] = []
        for source in (call.data.get(key), nested.get(key)):
            if not source:
                continue
            values.extend([source] if isinstance(source, str) else list(source))
        if values:
            out[key] = list(dict.fromkeys(values))
    return out


def _uuids_from_call(hass: HomeAssistant, call: ServiceCall) -> list[str]:
    """Resolve whatever the caller targeted onto sign UUIDs.

    The sign is the unit of meaning, so a UUID is what every handler wants.
    Getting there accepts anything Home Assistant calls a target:

    * ``device_id`` -- the direct form;
    * ``entity_id`` -- any entity belonging to a sign, which is the obvious
      first thing to reach for because it is the thing with a visible name;
    * ``area_id`` / ``floor_id`` / ``label_id`` -- resolved through the
      registries, so "every sign in the kitchen" works;
    * a nested ``target:`` mapping of any of the above.

    An entity that is not a Visionect entity is ignored rather than refused:
    targeting an area that contains a sign and a lamp should act on the sign.
    """
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    target = _merged_target(call)
    found: list[str] = []

    def _from_device(device_id: str | None) -> None:
        if not device_id:
            return
        device = devices.async_get(device_id)
        if device is None:
            return
        if (uuid := _uuid_of_device(device)) is not None:
            found.append(uuid)

    for device_id in target.get(ATTR_DEVICE_ID, []):
        _from_device(device_id)

    entity_ids = list(target.get(ATTR_ENTITY_ID, []))
    wide = any(key in target for key in (ATTR_AREA_ID, ATTR_FLOOR_ID, ATTR_LABEL_ID))
    if wide and async_extract_referenced_entity_ids is not None:
        # Area/floor/label expansion is registry work with several edge cases
        # (an entity in an area, a device in an area, an entity inheriting its
        # device's area), so use Home Assistant's own resolver rather than a
        # second implementation of it.
        #
        # primary_entities_only=False matters here: every entity a sign has
        # except the screen image is a diagnostic or config entity, so the
        # default would expand an area containing a sign to nothing at all.
        if TargetSelection is not None:
            selected = async_extract_referenced_entity_ids(
                hass,
                TargetSelection(dict(target)),
                expand_group=False,
                primary_entities_only=False,
            )
        else:  # pragma: no cover - older cores
            selected = async_extract_referenced_entity_ids(
                hass,
                ServiceCall(hass, DOMAIN, call.service, dict(target)),
                expand_group=False,
            )
        entity_ids.extend(selected.referenced | selected.indirectly_referenced)
        for device_id in selected.referenced_devices:
            _from_device(device_id)
    elif wide:  # pragma: no cover - no expansion helper on this core
        raise ServiceValidationError(
            "This Home Assistant version offers no area/floor/label target "
            "expansion here. Target the sign by device or entity instead."
        )

    for entity_id in dict.fromkeys(entity_ids):
        entry = entities.async_get(entity_id)
        if entry is None:
            continue
        _from_device(entry.device_id)

    unique = list(dict.fromkeys(found))
    if not unique:
        raise ServiceValidationError(
            "No Visionect sign was targeted. Name a sign with device_id, or "
            "any entity belonging to one with entity_id -- "
            "image.<sign>_screen is the easy one -- or an area it is in."
        )
    return unique


def _queued_response(runtime: Any, uuid: str, result: dict[str, Any]) -> dict[str, Any]:
    expected = runtime.expected_next_contact(uuid)
    out = {
        "uuid": uuid,
        "queued": True,
        "applied_immediately": runtime.socket_open(uuid),
        "expected_at": expected.isoformat() if expected else None,
    }
    out.update(result)
    return out


# ------------------------------------------------------- shared button logic


async def async_update_now(runtime: Any, uuid: str) -> None:
    """Re-resolve the content source and push on next contact."""
    runtime.async_bump(uuid, reason="update_now")


async def async_refresh(runtime: Any, uuid: str) -> None:
    """Re-push the current frame, bypassing the in-sync check.

    Every push to this hardware is full-screen, so re-pushing *is* a
    full-screen refresh. No command packet is involved.
    """
    runtime.async_bump(uuid, reason="refresh", force=True)


async def async_clear_screen(runtime: Any, uuid: str) -> None:
    """Push an all-white frame. White is the paper colour."""
    panel = runtime.panel(uuid)
    image = await runtime.hass.async_add_executor_job(
        partial(white_frame, width=panel.canvas_width, height=panel.canvas_height)
    )
    await runtime.async_push_image_now(
        uuid, image, force=True, label="clear_screen"
    )


async def async_ghost_clear(runtime: Any, uuid: str) -> None:
    """Queue an inverse / anti-ghosting full-screen pass.

    Experimental. The server-side bit manipulation is verified; the firmware's
    reaction to it is inferred, and on a stock configuration the signalling bit
    is already clear, so this may do nothing at all beyond a second push.
    """
    device_id = bytes.fromhex(uuid.replace("-", ""))
    runtime.store.queue(device_id).ghost_clear()
    runtime.async_bump(uuid, reason="ghost_clear", force=True)


# ------------------------------------------------------------- registration


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register every action once, from async_setup."""

    async def _display_image(call: ServiceCall) -> ServiceResponse:
        runtime = _runtime(hass)
        results = []
        for uuid in _uuids_from_call(hass, call):
            record = runtime.record(uuid)
            if fit := call.data.get("fit"):
                record.fit = fit
            if background := call.data.get("background"):
                record.background = background
            if dither := call.data.get("dither"):
                record.dither = dither
            if encoding := call.data.get("encoding"):
                record.encoding = encoding

            classified = classify_image_argument(call.data["image"])
            snapshot = call.data.get("snapshot", False)
            panel = runtime.panel(uuid)

            try:
                if isinstance(classified, EntitySource) and not snapshot:
                    # An entity id sets a *live* source: the sign re-reads it at
                    # every wake, which is what someone typing camera.doorbell
                    # actually wants.
                    runtime.async_set_source(uuid, classified)
                    results.append(_queued_response(runtime, uuid, {"source": "entity"}))
                    continue
                if isinstance(classified, (EntitySource, UrlSource)):
                    raw = await fetch_source_bytes(
                        hass,
                        classified,
                        width=panel.canvas_width,
                        height=panel.canvas_height,
                    )
                    label = getattr(classified, "entity_id", None) or getattr(
                        classified, "url", ""
                    )
                else:
                    path = str(classified)
                    if not hass.config.is_allowed_path(path):
                        raise ServiceValidationError(
                            f"{path} is not in allowlist_external_dirs, so Home "
                            "Assistant will not read it."
                        )
                    raw = await hass.async_add_executor_job(
                        lambda: open(path, "rb").read()  # noqa: SIM115
                    )
                    label = path
            except ContentError as err:
                raise HomeAssistantError(str(err)) from err
            except OSError as err:
                raise HomeAssistantError(f"could not read {classified}: {err}") from err

            await runtime.async_set_static_image(uuid, raw, label=str(label))
            results.append(_queued_response(runtime, uuid, {"source": "static"}))
        return {"results": results}

    async def _display_text(call: ServiceCall) -> ServiceResponse:
        runtime = _runtime(hass)
        results = []
        for uuid in _uuids_from_call(hass, call):
            panel = runtime.panel(uuid)
            image = await hass.async_add_executor_job(
                partial(
                    render_text,
                    call.data["message"],
                    width=panel.canvas_width,
                    height=panel.canvas_height,
                    size=call.data["size"],
                    align=call.data["align"],
                    invert=call.data["invert"],
                )
            )
            result = await runtime.async_push_image_now(
                uuid, image, label="display_text"
            )
            results.append(_queued_response(runtime, uuid, result))
        return {"results": results}

    async def _set_content_source(call: ServiceCall) -> ServiceResponse:
        runtime = _runtime(hass)
        kind = call.data["source"]
        results = []
        for uuid in _uuids_from_call(hass, call):
            if kind == "entity":
                entity_id = call.data.get("entity_id_source")
                if not entity_id:
                    raise ServiceValidationError(
                        "source: entity needs entity_id_source to name an "
                        "image.* or camera.* entity."
                    )
                runtime.async_set_source(uuid, EntitySource(entity_id=entity_id))
            elif kind == "url":
                url = call.data.get("url")
                if not url:
                    raise ServiceValidationError("source: url needs a url.")
                runtime.async_set_source(
                    uuid,
                    UrlSource(url=url, headers=dict(call.data.get("headers") or {})),
                )
            else:
                runtime.async_set_source(uuid, BlankSource())
            results.append(_queued_response(runtime, uuid, {"source": kind}))
        return {"results": results}

    async def _clear_content(call: ServiceCall) -> ServiceResponse:
        runtime = _runtime(hass)
        results = []
        for uuid in _uuids_from_call(hass, call):
            runtime.async_set_source(uuid, BlankSource())
            results.append(_queued_response(runtime, uuid, {}))
        return {"results": results}

    def _simple(handler) -> Any:
        async def _run(call: ServiceCall) -> ServiceResponse:
            runtime = _runtime(hass)
            results = []
            for uuid in _uuids_from_call(hass, call):
                await handler(runtime, uuid)
                results.append(_queued_response(runtime, uuid, {}))
            return {"results": results}

        return _run

    async def _read_parameters(call: ServiceCall) -> ServiceResponse:
        runtime = _runtime(hass)
        results = []
        for uuid in _uuids_from_call(hass, call):
            runtime.async_queue_param_read(uuid, list(call.data["ids"]))
            results.append(
                _queued_response(
                    runtime,
                    uuid,
                    {"known": dict(runtime.tclv_cache.get(uuid) or {})},
                )
            )
        return {"results": results}

    async def _write_parameters(call: ServiceCall) -> ServiceResponse:
        runtime = _runtime(hass)
        try:
            values = {int(k): int(v) for k, v in call.data["values"].items()}
        except (TypeError, ValueError) as err:
            raise ServiceValidationError(
                "values must be a mapping of TCLV id to integer, e.g. {29: 30}"
            ) from err
        results = []
        for uuid in _uuids_from_call(hass, call):
            try:
                runtime.async_queue_param_write(
                    uuid, values, persist=call.data.get("persist")
                )
            except ReadOnlyParameter as err:
                # The library's message already names the USB command that can
                # do it. Surface it rather than swallowing it.
                raise ServiceValidationError(str(err)) from err
            results.append(_queued_response(runtime, uuid, {"values": values}))
        return {"results": results}

    async def _list_device_files(call: ServiceCall) -> ServiceResponse:
        runtime = _runtime(hass)
        results = []
        for uuid in _uuids_from_call(hass, call):
            if runtime.socket_open(uuid):
                # A listing is one round trip and comes back in well under a
                # second, so do it now rather than reporting "queued" and
                # handing back a stale (or empty) dict.
                files = await runtime.async_list_device_files(uuid)
                results.append(
                    _queued_response(
                        runtime, uuid, {"queued": False, "files": files}
                    )
                )
                continue
            runtime.async_queue_file_list(uuid)
            results.append(
                _queued_response(
                    runtime, uuid, {"files": runtime.file_listings.get(uuid, {})}
                )
            )
        return {"results": results}

    async def _read_device_file(call: ServiceCall) -> ServiceResponse:
        runtime = _runtime(hass)
        results = []
        for uuid in _uuids_from_call(hass, call):
            results.append(
                await runtime.async_read_device_file(
                    uuid,
                    call.data["filename"],
                    decode=call.data["decode"],
                    timeout=call.data["timeout"],
                    attempts=call.data["attempts"],
                )
            )
        return {"results": results}

    registrations: list[tuple[str, Any, Any]] = [
        (SERVICE_DISPLAY_IMAGE, _display_image, DISPLAY_IMAGE_SCHEMA),
        (SERVICE_DISPLAY_TEXT, _display_text, DISPLAY_TEXT_SCHEMA),
        (SERVICE_SET_CONTENT_SOURCE, _set_content_source, SET_CONTENT_SOURCE_SCHEMA),
        (SERVICE_CLEAR_CONTENT, _clear_content, TARGET_ONLY_SCHEMA),
        (SERVICE_UPDATE_NOW, _simple(async_update_now), TARGET_ONLY_SCHEMA),
        (SERVICE_REFRESH, _simple(async_refresh), TARGET_ONLY_SCHEMA),
        (SERVICE_CLEAR_SCREEN, _simple(async_clear_screen), TARGET_ONLY_SCHEMA),
        (SERVICE_GHOST_CLEAR, _simple(async_ghost_clear), TARGET_ONLY_SCHEMA),
        (SERVICE_READ_PARAMETERS, _read_parameters, READ_PARAMETERS_SCHEMA),
        (SERVICE_WRITE_PARAMETERS, _write_parameters, WRITE_PARAMETERS_SCHEMA),
        (SERVICE_LIST_DEVICE_FILES, _list_device_files, TARGET_ONLY_SCHEMA),
        (SERVICE_READ_DEVICE_FILE, _read_device_file, READ_DEVICE_FILE_SCHEMA),
    ]
    for name, handler, schema in registrations:
        if hass.services.has_service(DOMAIN, name):
            continue
        hass.services.async_register(
            DOMAIN,
            name,
            handler,
            schema=schema,
            supports_response=SupportsResponse.OPTIONAL,
        )
