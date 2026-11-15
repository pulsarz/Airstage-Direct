"""Switches for the indoor unit's on/off settings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AirstageConfigEntry
from .coordinator import UnitCoordinator
from .entity import AirstageEntity, run_writes

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class AirstageSwitchDescription(SwitchEntityDescription):
    name_object: str


SWITCHES: tuple[AirstageSwitchDescription, ...] = (
    AirstageSwitchDescription(key="economy", translation_key="economy", name_object="iu_economy"),
    AirstageSwitchDescription(
        key="powerful", translation_key="powerful", name_object="iu_powerful"
    ),
    AirstageSwitchDescription(
        key="minimum_heat", translation_key="minimum_heat", name_object="iu_min_heat"
    ),
    AirstageSwitchDescription(
        key="energy_saving_fan", translation_key="energy_saving_fan", name_object="iu_fan_ctrl"
    ),
    AirstageSwitchDescription(
        key="human_sensor", translation_key="human_sensor", name_object="iu_hmn_det_auto_save"
    ),
    # A multi-split outdoor unit refuses this unless DIP switch SET1-4 is on.
    AirstageSwitchDescription(
        key="outdoor_low_noise", translation_key="outdoor_low_noise", name_object="ou_low_noise"
    ),
    AirstageSwitchDescription(
        key="wifi_led",
        translation_key="wifi_led",
        name_object="iu_wifi_led",
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AirstageConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for subentry_id, unit in entry.runtime_data.units.items():
        async_add_entities(
            [AirstageSwitch(unit, d) for d in SWITCHES if unit.device.supports(d.name_object)],
            config_subentry_id=subentry_id,
        )


class AirstageSwitch(AirstageEntity, SwitchEntity):
    entity_description: AirstageSwitchDescription

    def __init__(self, coordinator: UnitCoordinator, desc: AirstageSwitchDescription) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc
        self._names = (desc.name_object,)

    @property
    def is_on(self) -> bool | None:
        value = self.device.value(self.entity_description.name_object)
        return value if isinstance(value, bool) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Whether this unit refused the last change (write_res "unknown")."""
        return {"refused_by_unit": self.entity_description.name_object in self.device.refused}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, on: bool) -> None:
        name = self.entity_description.name_object
        refused_before = name in self.device.refused
        try:
            await run_writes(self.device.set_switch(name, on))
        finally:
            if (name in self.device.refused) != refused_before:
                self.coordinator.save_capabilities()  # remembered across restarts
            self.async_write_ha_state()
