"""Buttons -- every one of them built on a wire-verified mechanism.

``packet.Type 2`` (command) has never been observed on the wire in either
direction, and there is direct precedent in this protocol for the Go struct and
the wire format disagreeing (the param header is 8 bytes on the wire against a
12-byte struct).  So nothing here sends one.  ``refresh`` and ``clear_screen``
are image pushes, which were reproduced byte-exactly from a capture, and
``reboot`` is simply not offered.

Re-pushing is not a workaround for refresh -- it is the better mechanism. Every
push to this hardware is full-screen already, so re-pushing the same frame *is*
a full-screen refresh, and it needs no speculative packet type.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VisionectConfigEntry
from .const import SIGNAL_DEVICE_ADDED, SIGNAL_DEVICE_REMOVED
from .entity import VisionectEntity
from .runtime import VisionectRuntime
from .services import (
    async_clear_screen,
    async_ghost_clear,
    async_refresh,
    async_update_now,
)

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class VisionectButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[VisionectRuntime, str], Coroutine[Any, Any, Any]]


BUTTONS: tuple[VisionectButtonDescription, ...] = (
    VisionectButtonDescription(
        key="update_now",
        translation_key="update_now",
        press_fn=async_update_now,
    ),
    VisionectButtonDescription(
        key="refresh",
        translation_key="refresh",
        press_fn=async_refresh,
    ),
    VisionectButtonDescription(
        key="clear_screen",
        translation_key="clear_screen",
        entity_category=EntityCategory.CONFIG,
        press_fn=async_clear_screen,
    ),
    VisionectButtonDescription(
        key="ghost_clear",
        translation_key="ghost_clear",
        entity_registry_enabled_default=False,
        press_fn=async_ghost_clear,
        # Experimental: the server-side bit manipulation is verified, the
        # panel's reaction to it is inferred. Promises nothing.
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
        for desc in BUTTONS:
            key = f"{uuid}-{desc.key}"
            if key in known:
                continue
            known.add(key)
            new.append(VisionectButton(runtime, uuid, desc))
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


class VisionectButton(VisionectEntity, ButtonEntity):
    """A button's state is the timestamp of the last press, which paired with
    binary_sensor.pending_changes already says "pressed at 13:04, still
    waiting". No abuse of the state machine is needed."""

    entity_description: VisionectButtonDescription

    def __init__(
        self,
        runtime: VisionectRuntime,
        uuid: str,
        description: VisionectButtonDescription,
    ) -> None:
        super().__init__(runtime, uuid)
        self.entity_description = description
        self._attr_unique_id = f"{uuid}-{description.key}"

    async def async_press(self) -> None:
        await self.entity_description.press_fn(self.runtime, self._uuid)
