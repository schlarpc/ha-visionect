"""The screen entity: a faithful, post-dither picture of what the panel shows.

We are the server, so we know exactly what we sent, and the device confirms the
match for free by echoing our own checksum back as ``DisplayStateCRC``. So this
needs no readback and costs nothing.

It renders the **post-dither** image, quantised with the same dither the
encoder used, not the pre-dither 8-bit state model. A prettier-than-reality
preview is worse than none, because it hides exactly the dither-mode mistakes
the user is adjusting `select.dither_mode` to fix.

``ImageEntity``, not ``Camera``: the frame changes a few times a day, there is
nothing to stream, and ``image_last_updated`` is precisely the right metadata.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.image import ImageEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import VisionectConfigEntry
from .const import (
    SIGNAL_DEVICE_ADDED,
    SIGNAL_DEVICE_FILE_READ,
    SIGNAL_DEVICE_REMOVED,
    SIGNAL_SCREEN_UPDATED,
)
from .entity import VisionectEntity
from .runtime import VisionectRuntime

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VisionectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add(uuid: str) -> None:
        if uuid in known:
            return
        known.add(uuid)
        async_add_entities([
            VisionectScreen(hass, runtime, entry, uuid),
            VisionectDeviceFile(hass, runtime, entry, uuid),
        ])

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_DEVICE_ADDED.format(entry.entry_id), _add)
    )
    entry.async_on_unload(
        async_dispatcher_connect(
            hass, SIGNAL_DEVICE_REMOVED.format(entry.entry_id), known.discard
        )
    )
    for uuid in runtime.known_uuids():
        _add(uuid)


class VisionectScreen(VisionectEntity, ImageEntity):
    """What this sign is showing, as far as the device has confirmed."""

    _attr_translation_key = "screen"
    _attr_content_type = "image/png"

    def __init__(
        self,
        hass: HomeAssistant,
        runtime: VisionectRuntime,
        entry: VisionectConfigEntry,
        uuid: str,
    ) -> None:
        VisionectEntity.__init__(self, runtime, uuid)
        ImageEntity.__init__(self, hass)
        self._attr_unique_id = f"{uuid}-screen"
        self._entry_id = entry.entry_id
        record = runtime.record(uuid)
        self._attr_image_last_updated = record.last_push

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_SCREEN_UPDATED.format(self._entry_id),
                self._handle_screen_updated,
            )
        )

    @callback
    def _handle_screen_updated(self, uuid: str) -> None:
        if uuid != self._uuid:
            return
        self._cached_image = None
        self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()

    @property
    def image_last_updated(self) -> datetime | None:
        return self.runtime.record(self._uuid).last_push or self._attr_image_last_updated

    async def async_image(self) -> bytes | None:
        return self.runtime.previews.get(self._uuid)


class VisionectDeviceFile(VisionectEntity, ImageEntity):
    """The last file pulled off the device's own flash, decoded.

    Disabled by default, and empty until ``visionect.read_device_file`` is
    called, because filling it costs one to nine minutes of 1 KiB round trips
    at ~2.3 KiB/s.

    This is **not** a live framebuffer, however much the name invites that
    reading. The six ``/imageN.pv2`` files on firmware 7.4.4407 are Visionect's
    shipped demo screens: they do not change when we push, they carry a zero
    ``ImageHeader.Checksum`` and a zero UUID, and re-listing them across a push
    shows not one byte different. What the panel is showing is answered by
    ``DisplayStateCRC``, which the sign volunteers for free in every status --
    that is what ``binary_sensor.display_out_of_sync`` reads.

    The entity exists because the decode is real and the device genuinely holds
    these frames, and because a firmware that did cache the live frame would
    land here with no further work.
    """

    _attr_translation_key = "device_file"
    _attr_content_type = "image/png"
    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        hass: HomeAssistant,
        runtime: VisionectRuntime,
        entry: VisionectConfigEntry,
        uuid: str,
    ) -> None:
        VisionectEntity.__init__(self, runtime, uuid)
        ImageEntity.__init__(self, hass)
        self._attr_unique_id = f"{uuid}-device-file"
        self._entry_id = entry.entry_id

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_DEVICE_FILE_READ.format(self._entry_id),
                self._handle_file_read,
            )
        )

    @callback
    def _handle_file_read(self, uuid: str) -> None:
        if uuid != self._uuid:
            return
        self._cached_image = None
        self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return super().available and self._uuid in self.runtime.device_files

    async def async_image(self) -> bytes | None:
        return self.runtime.device_files.get(self._uuid)
