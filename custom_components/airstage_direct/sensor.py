"""Sensors: indoor-unit values per unit, and the shared outdoor unit."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    REVOLUTIONS_PER_MINUTE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfFrequency,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AirstageConfigEntry
from .api.device import Device
from .coordinator import UnitCoordinator
from .entity import AirstageEntity, OutdoorEntity
from .estimate_sensors import system_sensors, unit_sensors
from .outdoor import OutdoorSource

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class AirstageSensorDescription(SensorEntityDescription):
    name_object: str  # the adapter object this sensor shows
    fallback: str | None = None  # read when name_object has no value
    sub_id: int = 0


def _temperature(key: str, obj: str, *, on: bool = True, **kw: object) -> AirstageSensorDescription:
    return AirstageSensorDescription(
        key=key,
        translation_key=key,
        name_object=obj,
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=on,
        **kw,  # type: ignore[arg-type]
    )


def _counter(key: str, obj: str) -> AirstageSensorDescription:
    """Run-time counters; their unit is not known yet, so none is claimed."""
    return AirstageSensorDescription(
        key=key,
        translation_key=key,
        name_object=obj,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    )


INDOOR: tuple[AirstageSensorDescription, ...] = (
    _temperature("room_temperature", "iu_indoor_tmp"),
    _temperature("intake_temperature", "iu_suct_tmp"),
    _temperature("corrected_intake_temperature", "iu_indoor_tmp_crct"),
    _temperature("coil_temperature", "iu_heat_ex_tmp"),
    AirstageSensorDescription(
        key="fan_speed",
        translation_key="fan_speed",
        name_object="iu_fan_rot1",
        native_unit_of_measurement=REVOLUTIONS_PER_MINUTE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    _counter("operating_time", "iu_eng_time"),
    _counter("filter_time", "iu_fltr_time"),
    _counter("fan_operating_time", "iu_fan1_time"),
    AirstageSensorDescription(
        key="error_code",
        translation_key="error_code",
        name_object="iu_err_code",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    AirstageSensorDescription(
        key="wifi_signal",
        translation_key="wifi_signal",
        name_object="wla_rssi",
        sub_id=1,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)

OUTDOOR: tuple[AirstageSensorDescription, ...] = (
    _temperature("outdoor_temperature", "ou_outdoor_tmp", fallback="iu_outdoor_tmp"),
    AirstageSensorDescription(
        key="compressor_frequency",
        translation_key="compressor_frequency",
        name_object="ou_cmp_freq",
        device_class=SensorDeviceClass.FREQUENCY,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    AirstageSensorDescription(
        key="outdoor_current",
        translation_key="outdoor_current",
        name_object="ou_current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    _temperature("discharge_temperature", "ou_disc_tmp", on=False),
    _temperature("outdoor_coil_temperature", "ou_heat_ex_tmp", on=False),
    _temperature("outdoor_coil_outlet_temperature", "ou_heat_ex_tmp_out", on=False),
    AirstageSensorDescription(
        key="outdoor_fan_speed",
        translation_key="outdoor_fan_speed",
        name_object="ou_fan_rot1",
        native_unit_of_measurement=REVOLUTIONS_PER_MINUTE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    *(
        AirstageSensorDescription(
            key=f"expansion_valve_{i}",
            translation_key="expansion_valve",
            translation_placeholders={"number": str(i)},
            name_object=f"ou_exp_vlv{i}",
            state_class=SensorStateClass.MEASUREMENT,
            entity_registry_enabled_default=False,
        )
        for i in range(1, 6)
    ),
    AirstageSensorDescription(
        key="four_way_valve",
        translation_key="four_way_valve",
        name_object="ou_four_way_vlv",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    _counter("compressor_operating_time", "iu_cmp_time"),
    _counter("outdoor_fan_operating_time", "ou_fan1_time"),
)


def number(device: Device | None, desc: AirstageSensorDescription) -> float | int | str | None:
    """The value to show, or None while the adapter has none (65535, "")."""
    if device is None:
        return None
    for name in (desc.name_object, desc.fallback):
        if name is None:
            continue
        value = device.value(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float | str):
            return value
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AirstageConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    for subentry_id, unit in runtime.units.items():
        async_add_entities(
            [
                IndoorSensor(unit, desc)
                for desc in INDOOR
                if unit.device.supports(desc.name_object, desc.sub_id)
            ],
            config_subentry_id=subentry_id,
        )
    estimates = runtime.estimates
    if estimates is not None and estimates.active:
        for subentry_id, entities in unit_sensors(estimates).items():
            async_add_entities(entities, config_subentry_id=subentry_id)
        async_add_entities(system_sensors(estimates, entry.entry_id, entry.title))
    outdoor = runtime.outdoor
    if runtime.units:
        async_add_entities(
            [
                OutdoorSensor(entry.entry_id, entry.title, outdoor, desc)
                for desc in OUTDOOR
                if outdoor.supports(desc.name_object)
            ]
        )


class IndoorSensor(AirstageEntity, SensorEntity):
    entity_description: AirstageSensorDescription

    def __init__(self, coordinator: UnitCoordinator, desc: AirstageSensorDescription) -> None:
        super().__init__(coordinator, desc.key)
        self.entity_description = desc
        self._names = (desc.name_object,)
        self._sub_id = desc.sub_id

    @property
    def native_value(self) -> float | int | str | None:
        return number(self.device, self.entity_description)


class OutdoorSensor(OutdoorEntity, SensorEntity):
    entity_description: AirstageSensorDescription

    def __init__(
        self, entry_id: str, title: str, outdoor: OutdoorSource, desc: AirstageSensorDescription
    ) -> None:
        super().__init__(entry_id, title, outdoor, desc.key)
        self.entity_description = desc
        self._names = tuple(n for n in (desc.name_object, desc.fallback) if n)

    @property
    def native_value(self) -> float | int | str | None:
        return number(self.source_device, self.entity_description)
