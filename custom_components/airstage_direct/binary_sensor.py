"""Binary sensors: malfunction per indoor unit; compressor running (outdoor)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AirstageConfigEntry
from .coordinator import UnitCoordinator
from .entity import AirstageEntity, OutdoorEntity
from .outdoor import OutdoorSource

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AirstageConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    for subentry_id, unit in runtime.units.items():
        if unit.device.supports("iu_malfnc_stat"):
            async_add_entities([Malfunction(unit)], config_subentry_id=subentry_id)
    if runtime.units and runtime.outdoor.supports("ou_cmp_freq"):
        async_add_entities([CompressorRunning(entry.entry_id, entry.title, runtime.outdoor)])


class Malfunction(AirstageEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "malfunction"
    _names = ("iu_malfnc_stat",)

    def __init__(self, coordinator: UnitCoordinator) -> None:
        super().__init__(coordinator, "malfunction")

    @property
    def is_on(self) -> bool | None:
        value = self.device.value("iu_malfnc_stat")
        return value != 0 if isinstance(value, int) and not isinstance(value, bool) else None


class CompressorRunning(OutdoorEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_translation_key = "compressor_running"
    _names = ("ou_cmp_freq",)

    def __init__(self, entry_id: str, title: str, outdoor: OutdoorSource) -> None:
        super().__init__(entry_id, title, outdoor, "compressor_running")

    @property
    def is_on(self) -> bool | None:
        device = self.source_device
        value = device.value("ou_cmp_freq") if device is not None else None
        return value > 0 if isinstance(value, int) and not isinstance(value, bool) else None
