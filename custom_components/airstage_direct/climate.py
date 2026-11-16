"""Climate entity: power, mode, setpoint, fan, swing, presets of one indoor unit."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    FAN_AUTO,
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    PRESET_BOOST,
    PRESET_ECO,
    PRESET_NONE,
    SWING_HORIZONTAL_OFF,
    SWING_HORIZONTAL_ON,
    SWING_OFF,
    SWING_ON,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AirstageConfigEntry
from .api.codecs import FanSpeed, OpMode
from .const import CLIMATE_TEMPERATURE_INTAKE, OPT_CLIMATE_TEMPERATURE
from .coordinator import UnitCoordinator
from .entity import AirstageEntity, run_writes
from .guard import UnitGuard

PARALLEL_UPDATES = 0  # writes are serialised per adapter by the transport

# Fujitsu Auto keeps one setpoint and decides between heating and cooling itself:
# heat_cool ("heat/cool to a target"), not auto (where the user cannot set it).
MODE_TO_HA = {
    OpMode.AUTO: HVACMode.HEAT_COOL,
    OpMode.COOL: HVACMode.COOL,
    OpMode.DRY: HVACMode.DRY,
    OpMode.FAN: HVACMode.FAN_ONLY,
    OpMode.HEAT: HVACMode.HEAT,
}
HA_TO_MODE = {ha: mode for mode, ha in MODE_TO_HA.items()}
FAN_TO_HA = {
    FanSpeed.AUTO: FAN_AUTO,
    FanSpeed.QUIET: "quiet",
    FanSpeed.LOW: FAN_LOW,
    FanSpeed.LOW_MEDIUM: "low_medium",
    FanSpeed.MEDIUM: FAN_MEDIUM,
    FanSpeed.MEDIUM_HIGH: "medium_high",
    FanSpeed.HIGH: FAN_HIGH,
}
HA_TO_FAN = {ha: fan for fan, ha in FAN_TO_HA.items()}
PRESET_MIN_HEAT = "min_heat"
PRESETS = {PRESET_ECO: "iu_economy", PRESET_BOOST: "iu_powerful", PRESET_MIN_HEAT: "iu_min_heat"}
DEFAULT_LIMITS = (16.0, 30.0)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AirstageConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for subentry_id, unit in entry.runtime_data.units.items():
        async_add_entities([AirstageClimate(unit)], config_subentry_id=subentry_id)


class AirstageClimate(AirstageEntity, ClimateEntity):
    _attr_name = None  # the device's own name
    _names = (
        "iu_onoff",
        "iu_op_mode",
        "iu_set_tmp",
        "iu_fan_spd",
        "iu_af_swg_vrt",
        "iu_af_swg_hrz",
        "iu_economy",
        "iu_powerful",
        "iu_min_heat",
        "iu_indoor_tmp",
    )
    _attr_translation_key = "airstage"
    _attr_target_temperature_step = 0.5
    _attr_precision = 0.5

    def __init__(self, coordinator: UnitCoordinator) -> None:
        super().__init__(coordinator, "climate")
        # Experimental option: show the corrected intake (what the unit regulates
        # on, refreshed every 5 minutes) instead of the room temperature.
        source = coordinator.config_entry.options.get(OPT_CLIMATE_TEMPERATURE)
        self._temperature_object = (
            "iu_indoor_tmp_crct" if source == CLIMATE_TEMPERATURE_INTAKE else "iu_indoor_tmp"
        )
        if self._temperature_object != "iu_indoor_tmp":
            self._names = (*self._names[:-1], self._temperature_object)
        self._attr_hvac_modes = [HVACMode.OFF, *MODE_TO_HA.values()]
        self._attr_fan_modes = list(FAN_TO_HA.values())
        device = coordinator.device
        features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )
        if device.supports("iu_af_swg_vrt"):
            features |= ClimateEntityFeature.SWING_MODE
            self._attr_swing_modes = [SWING_OFF, SWING_ON]
        if device.supports("iu_af_swg_hrz"):
            features |= ClimateEntityFeature.SWING_HORIZONTAL_MODE
            self._attr_swing_horizontal_modes = [SWING_HORIZONTAL_OFF, SWING_HORIZONTAL_ON]
        presets = [p for p, name in PRESETS.items() if device.supports(name)]
        if presets:
            features |= ClimateEntityFeature.PRESET_MODE
            self._attr_preset_modes = [PRESET_NONE, *presets]
        self._attr_supported_features = features

    # -- state --------------------------------------------------------------------

    @property
    def hvac_mode(self) -> HVACMode | None:
        power = self.device.value("iu_onoff")
        if power is False:
            return HVACMode.OFF
        mode = self.device.value("iu_op_mode")
        if power is True and isinstance(mode, OpMode):
            return MODE_TO_HA.get(mode)
        return None

    def _room_temperature(self) -> float | None:
        value = self.device.value(self._temperature_object)  # None/Missing while 65535
        return value if isinstance(value, float) else None

    def _unit_guard(self) -> UnitGuard | None:
        guard = self.coordinator.config_entry.runtime_data.guard
        if guard is None or not guard.enabled:
            return None
        return guard.units.get(self.coordinator.subentry.subentry_id)

    def _setpoint(self) -> float | None:
        unit_guard = self._unit_guard()
        if unit_guard is not None and unit_guard.stored.user_setpoint is not None:
            return unit_guard.stored.user_setpoint  # the user's, not the guard's raise
        value = self.device.value("iu_set_tmp")
        return value if isinstance(value, float) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        unit_guard = self._unit_guard()
        return unit_guard.attributes() if unit_guard is not None else None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        guard = self.coordinator.config_entry.runtime_data.guard
        if guard is not None and guard.enabled:
            self.async_on_remove(guard.add_listener(self.async_write_ha_state))

    def _limits(self) -> tuple[float, float]:
        caps = self.device.capabilities
        mode = self.device.value("iu_op_mode")
        if caps is None or not isinstance(mode, OpMode):
            return DEFAULT_LIMITS
        if mode is OpMode.FAN:
            mode = OpMode.COOL
        return caps.limits.get(mode, DEFAULT_LIMITS)

    @property
    def min_temp(self) -> float:
        return self._limits()[0]

    @property
    def max_temp(self) -> float:
        return self._limits()[1]

    @property
    def fan_mode(self) -> str | None:
        fan = self.device.value("iu_fan_spd")
        return FAN_TO_HA.get(fan) if isinstance(fan, FanSpeed) else None

    @property
    def swing_mode(self) -> str | None:
        on = self.device.value("iu_af_swg_vrt")
        return None if not isinstance(on, bool) else SWING_ON if on else SWING_OFF

    @property
    def swing_horizontal_mode(self) -> str | None:
        on = self.device.value("iu_af_swg_hrz")
        return (
            None
            if not isinstance(on, bool)
            else SWING_HORIZONTAL_ON
            if on
            else SWING_HORIZONTAL_OFF
        )

    @property
    def preset_mode(self) -> str | None:
        for preset, name in PRESETS.items():
            if self.device.value(name) is True:
                return preset
        return PRESET_NONE

    # -- actions --------------------------------------------------------------------

    async def _run(self, *writes: Awaitable[None]) -> None:
        await run_writes(*writes)

    async def async_turn_on(self) -> None:
        await self._run(self.device.set_power(True))

    async def async_turn_off(self) -> None:
        await self._run(self.device.set_power(False))

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode is HVACMode.OFF:
            await self._run(self.device.set_power(False))
            return
        writes = [self.device.set_mode(HA_TO_MODE[hvac_mode])]
        if self.device.value("iu_onoff") is not True:
            writes.insert(0, self.device.set_power(True))
        await self._run(*writes)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        writes: list[Awaitable[None]] = []
        if (mode := kwargs.get(ATTR_HVAC_MODE)) is not None:
            if mode == HVACMode.OFF:
                writes.append(self.device.set_power(False))
            else:
                writes.append(self.device.set_mode(HA_TO_MODE[HVACMode(mode)]))
                if self.device.value("iu_onoff") is not True:
                    writes.append(self.device.set_power(True))
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is not None:
            if (unit_guard := self._unit_guard()) is not None and unit_guard.raised:
                unit_guard.end_raise_for_user()  # the user's new setpoint wins
            writes.append(self.device.set_setpoint(float(temperature)))
        await self._run(*writes)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        await self._run(self.device.set_fan_speed(HA_TO_FAN[fan_mode]))

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        await self._run(self.device.set_swing_vertical(swing_mode == SWING_ON))

    async def async_set_swing_horizontal_mode(self, swing_horizontal_mode: str) -> None:
        await self._run(
            self.device.set_swing_horizontal(swing_horizontal_mode == SWING_HORIZONTAL_ON)
        )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        writes = [
            self.device.set_switch(name, preset == preset_mode)
            for preset, name in PRESETS.items()
            if self.device.supports(name)
            and (preset == preset_mode or self.device.value(name) is True)
        ]
        await self._run(*writes)


def install_temperature_api(cls: type[ClimateEntity], native: bool) -> None:
    """Attach the temperature unit and readings under the names this Home Assistant
    expects: 2026.11 added native_* names and deprecates the old ones (they stop
    working in 2027.11); 2026.10 only knows the old ones. Attached after the class
    is defined, so 2026.11 does not flag the old names on the class."""
    prefix = "native_" if native else ""
    setattr(cls, f"_attr_{prefix}temperature_unit", UnitOfTemperature.CELSIUS)
    setattr(cls, f"{prefix}current_temperature", property(AirstageClimate._room_temperature))
    setattr(cls, f"{prefix}target_temperature", property(AirstageClimate._setpoint))


install_temperature_api(AirstageClimate, hasattr(ClimateEntity, "native_current_temperature"))
