"""Estimate sensors. Every name starts with "Estimated" (except metered
electricity), and every entity says so in its attributes: estimate, method,
accuracy. The changing inputs are not recorded."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.helpers.device_registry import DeviceInfo

from .api.energy import EnergyTracker
from .api.estimates import ACCURACY
from .entity import outdoor_device_info, unit_device_info
from .estimation import METHOD_HEAT, METHOD_LATENT, SystemEstimates, UnitEstimate

ESTIMATE_ATTRS = {"estimate": True, "accuracy": ACCURACY}


class _EstimateBase(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _unrecorded_attributes = frozenset({"inputs", "fan_curve", "window_minutes", "provisional"})

    def __init__(self, system: SystemEstimates, unique_id: str, device_info: DeviceInfo) -> None:
        self.system = system
        self._attr_unique_id = unique_id
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.system.add_listener(self.async_write_ha_state))


class UnitOutputSensor(_EstimateBase):
    """Estimated heat (or cooling) output of one indoor unit, W."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, system: SystemEstimates, estimate: UnitEstimate, cooling: bool) -> None:
        device_id = estimate.unit.device_id
        super().__init__(
            system,
            f"{device_id}_estimated_{'cooling' if cooling else 'heat'}_output",
            unit_device_info(estimate.unit),
        )
        self.estimate = estimate
        self.cooling = cooling
        if cooling:
            sensible_only = not estimate.humidity_entity
            self._attr_translation_key = (
                "estimated_cooling_output_sensible" if sensible_only else "estimated_cooling_output"
            )
        else:
            self._attr_translation_key = "estimated_heat_output"

    @property
    def native_value(self) -> float | None:
        output = self.estimate.output_w()
        if output is None:
            return None
        return round(output[1] if self.cooling else output[0])

    @property
    def available(self) -> bool:
        return self.estimate.unit.last_update_success

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        method = METHOD_HEAT
        if self.cooling and self.estimate.humidity_entity:
            method = f"{METHOD_HEAT} {METHOD_LATENT}"
        return {**ESTIMATE_ATTRS, "method": method, **self.estimate.attributes()}


@dataclass(frozen=True, kw_only=True)
class SystemSensorDescription(SensorEntityDescription):
    value: Callable[[SystemEstimates], float | None]
    method: str
    is_estimate: bool = True


def _per(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _energy(
    key: str, value: Callable[[EnergyTracker], float], method: str
) -> SystemSensorDescription:
    return SystemSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value=lambda s: round(value(s.tracker), 3),
        method=method,
    )


def _ratio(
    key: str, value: Callable[[SystemEstimates], float | None], method: str
) -> SystemSensorDescription:
    return SystemSensorDescription(
        key=key,
        translation_key=key,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value=lambda s: _per(value(s), 2),
        method=method,
    )


SYSTEM_SENSORS: tuple[SystemSensorDescription, ...] = (
    SystemSensorDescription(
        key="estimated_heat_output_total",
        translation_key="estimated_heat_output_total",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=lambda s: _per(s.heating_w, 0),
        method="sum of the indoor units' estimated heat output",
    ),
    SystemSensorDescription(
        key="estimated_cooling_output_total",
        translation_key="estimated_cooling_output_total",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=lambda s: _per(s.cooling_w, 0),
        method="sum of the indoor units' estimated cooling output",
    ),
    _energy(
        "estimated_heat_energy", lambda t: t.lifetime.heat_kwh, "estimated heat output over time"
    ),
    _energy(
        "estimated_cooling_energy",
        lambda t: t.lifetime.cool_kwh,
        "estimated cooling output over time",
    ),
    _ratio(
        "estimated_cop_1h",
        lambda s: s.cop_1h(),
        "estimated heat (or cooling) energy / electricity over the last hour, standby included",
    ),
    _ratio(
        "estimated_scop_month",
        lambda s: EnergyTracker.scop(s.tracker.month),
        "estimated heat energy / heating electricity, this calendar month",
    ),
    _ratio(
        "estimated_scop_year",
        lambda s: EnergyTracker.scop(s.tracker.year),
        "estimated heat energy / heating electricity, this calendar year",
    ),
    _ratio(
        "estimated_seer_month",
        lambda s: EnergyTracker.seer(s.tracker.month),
        "estimated cooling energy / cooling electricity, this calendar month",
    ),
    _ratio(
        "estimated_seer_year",
        lambda s: EnergyTracker.seer(s.tracker.year),
        "estimated cooling energy / cooling electricity, this calendar year",
    ),
    _ratio(
        "carnot_cop",
        lambda s: s.carnot(),
        "0.5 x T_hot / (T_hot - T_cold) in K, warmest indoor coil and coldest outdoor coil "
        "(the other way round when cooling): a plausible upper bound for the COP",
    ),
)


def electricity_sensors(system: SystemEstimates) -> tuple[SystemSensorDescription, ...]:
    metered = system.meter is not None
    method = (
        "the electricity meter's kWh, split by season (any unit heating: heating)"
        if metered
        else "outdoor current x 230 V over time, split by season"
    )
    suffix = "" if metered else "_estimated"
    prefix = "" if metered else "estimated_"
    return (
        SystemSensorDescription(
            key=f"{prefix}electricity_heating",
            translation_key=f"electricity_heating{suffix}",
            device_class=SensorDeviceClass.ENERGY,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            state_class=SensorStateClass.TOTAL_INCREASING,
            suggested_display_precision=2,
            value=lambda s: round(s.tracker.lifetime.electricity_heat_kwh, 3),
            method=method,
            is_estimate=not metered,
        ),
        SystemSensorDescription(
            key=f"{prefix}electricity_cooling",
            translation_key=f"electricity_cooling{suffix}",
            device_class=SensorDeviceClass.ENERGY,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            state_class=SensorStateClass.TOTAL_INCREASING,
            suggested_display_precision=2,
            value=lambda s: round(s.tracker.lifetime.electricity_cool_kwh, 3),
            method=method,
            is_estimate=not metered,
        ),
    )


class SystemSensor(_EstimateBase):
    entity_description: SystemSensorDescription

    def __init__(
        self, system: SystemEstimates, entry_id: str, title: str, desc: SystemSensorDescription
    ) -> None:
        super().__init__(system, f"{entry_id}_{desc.key}", outdoor_device_info(entry_id, title))
        self.entity_description = desc

    @property
    def native_value(self) -> float | None:
        return self.entity_description.value(self.system)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        desc = self.entity_description
        attrs: dict[str, Any] = {"method": desc.method}
        if desc.is_estimate:
            attrs |= ESTIMATE_ATTRS
        if desc.key == "estimated_cop_1h":
            _, coverage = self.system.cop_window()
            attrs["window_minutes"] = round(coverage / 60)
            attrs["provisional"] = EnergyTracker.is_provisional(coverage)
        return attrs


def unit_sensors(system: SystemEstimates) -> dict[str, list[SensorEntity]]:
    """Per subentry id: the estimate sensors of that indoor unit."""
    return {
        sub_id: [
            UnitOutputSensor(system, e, cooling=False),
            UnitOutputSensor(system, e, cooling=True),
        ]
        for sub_id, e in system.units.items()
    }


def system_sensors(system: SystemEstimates, entry_id: str, title: str) -> list[SensorEntity]:
    return [
        SystemSensor(system, entry_id, title, d)
        for d in (*SYSTEM_SENSORS, *electricity_sensors(system))
    ]


ESTIMATE_KEYS = frozenset(
    {"estimated_heat_output", "estimated_cooling_output"}
    | {d.key for d in SYSTEM_SENSORS}
    | {"electricity_heating", "electricity_cooling"}
    | {"estimated_electricity_heating", "estimated_electricity_cooling"}
    | {"reset_estimates"}
)


def is_estimate_unique_id(unique_id: str) -> bool:
    return any(unique_id.endswith(f"_{key}") for key in ESTIMATE_KEYS)


def estimate_entity_ids(system: SystemEstimates, entry_id: str) -> list[SensorEntity]:
    """The estimate entities this configuration provides (for cleaning up others)."""
    return [e for es in unit_sensors(system).values() for e in es] + system_sensors(
        system, entry_id, ""
    )
