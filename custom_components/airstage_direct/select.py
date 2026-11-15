"""Louvre positions: vertical 1-4 (1 most horizontal, 4 straight down) and
horizontal 1-5 (1 far left, 3 middle, 5 far right, as the Airstage app draws it)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AirstageConfigEntry
from .api.codecs import VANE_HORIZONTAL_POSITIONS, VANE_VERTICAL_POSITIONS
from .api.device import Device
from .coordinator import UnitCoordinator
from .entity import AirstageEntity, run_writes

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class AirstageSelectDescription(SelectEntityDescription):
    name_object: str
    setter: Callable[[Device, int], Awaitable[None]]


SELECTS: tuple[AirstageSelectDescription, ...] = (
    AirstageSelectDescription(
        key="vertical_louvre",
        translation_key="vertical_louvre",
        name_object="iu_af_dir_vrt",
        options=[str(p) for p in VANE_VERTICAL_POSITIONS],
        setter=lambda device, position: device.set_vane_vertical(position),
    ),
    AirstageSelectDescription(
        key="horizontal_louvre",
        translation_key="horizontal_louvre",
        name_object="iu_af_dir_hrz",
        options=[str(p) for p in VANE_HORIZONTAL_POSITIONS],
        setter=lambda device, position: device.set_vane_horizontal(position),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AirstageConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for subentry_id, unit in entry.runtime_data.units.items():
        async_add_entities(
            [LouvreSelect(unit, d) for d in SELECTS if unit.device.supports(d.name_object)],
            config_subentry_id=subentry_id,
        )


class LouvreSelect(AirstageEntity, SelectEntity):
    entity_description: AirstageSelectDescription

    def __init__(self, coordinator: UnitCoordinator, desc: AirstageSelectDescription) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc
        self._names = (desc.name_object,)

    @property
    def current_option(self) -> str | None:
        value = self.device.value(self.entity_description.name_object)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        option = str(value)
        return option if option in (self.entity_description.options or []) else None

    async def async_select_option(self, option: str) -> None:
        await run_writes(self.entity_description.setter(self.device, int(option)))
