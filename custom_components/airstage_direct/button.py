"""A button to start the estimate counters from zero."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AirstageConfigEntry
from .entity import outdoor_device_info
from .estimation import SystemEstimates

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AirstageConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    estimates = entry.runtime_data.estimates
    if estimates is not None and estimates.active:
        async_add_entities([ResetEstimates(estimates, entry.entry_id, entry.title)])


class ResetEstimates(ButtonEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "reset_estimates"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, system: SystemEstimates, entry_id: str, title: str) -> None:
        self.system = system
        self._attr_unique_id = f"{entry_id}_reset_estimates"
        self._attr_device_info = outdoor_device_info(entry_id, title)

    async def async_press(self) -> None:
        await self.system.async_reset()
