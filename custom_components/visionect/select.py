"""Selects. All three are server-side: no device round trip, instant in HA.

The *visible* result still waits for the next contact, which is what
``sensor.next_contact`` and ``binary_sensor.pending_changes`` are for.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VisionectConfigEntry
from .const import (
    DEFAULT_DITHER,
    DEFAULT_ENCODING,
    DEFAULT_FIT,
    DITHER_MODES,
    ENCODINGS,
    FIT_MODES,
    SIGNAL_DEVICE_ADDED,
    SIGNAL_DEVICE_REMOVED,
)
from .entity import VisionectEntity
from .runtime import DeviceRecord, VisionectRuntime

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class VisionectSelectDescription(SelectEntityDescription):
    current_fn: Callable[[DeviceRecord], str]
    set_fn: Callable[[DeviceRecord, str], None]


def _set_dither(record: DeviceRecord, option: str) -> None:
    record.dither = option


def _set_encoding(record: DeviceRecord, option: str) -> None:
    record.encoding = option


def _set_fit(record: DeviceRecord, option: str) -> None:
    record.fit = option


SELECTS: tuple[VisionectSelectDescription, ...] = (
    VisionectSelectDescription(
        key="dither_mode",
        translation_key="dither_mode",
        options=list(DITHER_MODES),
        entity_category=EntityCategory.CONFIG,
        current_fn=lambda r: r.dither if r.dither in DITHER_MODES else DEFAULT_DITHER,
        set_fn=_set_dither,
        # `blue_noise` is this implementation's extension, and safe: the dither
        # mode never appears on the wire. It is also the only good *ordered*
        # dither at 4 bpp, so it is the default there.
    ),
    VisionectSelectDescription(
        key="encoding",
        translation_key="encoding",
        options=list(ENCODINGS),
        entity_category=EntityCategory.CONFIG,
        current_fn=lambda r: r.encoding if r.encoding in ENCODINGS else DEFAULT_ENCODING,
        set_fn=_set_encoding,
    ),
    VisionectSelectDescription(
        key="fit_mode",
        translation_key="fit_mode",
        options=list(FIT_MODES),
        entity_category=EntityCategory.CONFIG,
        current_fn=lambda r: r.fit if r.fit in FIT_MODES else DEFAULT_FIT,
        set_fn=_set_fit,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VisionectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add(uuid: str) -> None:
        new = []
        for desc in SELECTS:
            key = f"{uuid}-{desc.key}"
            if key in known:
                continue
            known.add(key)
            new.append(VisionectSelect(runtime, uuid, desc))
        if new:
            async_add_entities(new)

    @callback
    def _forget(uuid: str) -> None:
        """Drop this sign from the already-added set.

        A deleted sign re-registers on its next contact -- the protocol offers
        no way to refuse it -- and it must get its entities back when it does.
        """
        for key in [k for k in known if k.startswith(f"{uuid}-")]:
            known.discard(key)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_DEVICE_ADDED.format(entry.entry_id), _add)
    )
    entry.async_on_unload(
        async_dispatcher_connect(
            hass, SIGNAL_DEVICE_REMOVED.format(entry.entry_id), _forget
        )
    )
    for uuid in runtime.known_uuids():
        _add(uuid)


class VisionectSelect(VisionectEntity, SelectEntity):
    entity_description: VisionectSelectDescription

    def __init__(
        self,
        runtime: VisionectRuntime,
        uuid: str,
        description: VisionectSelectDescription,
    ) -> None:
        super().__init__(runtime, uuid)
        self.entity_description = description
        self._attr_unique_id = f"{uuid}-{description.key}"

    @property
    def available(self) -> bool:
        # A server-side setting is meaningful even before the sign reports.
        return self.runtime.listener_running

    @property
    def assumed_state(self) -> bool:
        return False

    @property
    def current_option(self) -> str:
        return self.entity_description.current_fn(self.record)

    async def async_select_option(self, option: str) -> None:
        self.entity_description.set_fn(self.record, option)
        # A change of dither or depth invalidates the cached FrameState, so the
        # next push is a full re-encode.
        self.runtime.async_bump(
            self._uuid, reason=f"{self.entity_description.key} -> {option}"
        )
        self.async_write_ha_state()
