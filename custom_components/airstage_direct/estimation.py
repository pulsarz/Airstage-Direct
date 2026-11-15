"""The estimates of one system: per-unit heat output, energy, COP, SCOP/SEER.

Calculated, not measured (api/estimates.py, api/energy.py). Inputs are
subscribed here, so they are read while the estimates are on even when their
own sensors are disabled. A unit that is on but has no fresh inputs makes the
system totals unknown rather than silently too low.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, Final

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfEnergy
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import estimates as est
from .api.codecs import OpMode
from .api.energy import EnergyTracker
from .const import (
    CONF_CURVE_INTERCEPT,
    CONF_CURVE_SLOPE,
    CONF_HUMIDITY_ENTITY,
    CONF_MODEL,
    CONF_RATED_AIRFLOW,
    DOMAIN,
    ESTIMATE_TICK,
    MAINS_VOLTAGE,
    MODEL_CUSTOM,
    OPT_AIRFLOW_SCALE,
    OPT_EFFECTIVENESS,
    OPT_METER,
)
from .coordinator import UnitCoordinator
from .outdoor import OutdoorSource

_LOGGER = logging.getLogger(__name__)

STORE_VERSION: Final = 1
SAVE_DELAY_S: Final = 60
UNIT_INPUTS: Final = ("iu_onoff", "iu_op_mode", "iu_fan_rot1", "iu_heat_ex_tmp", "iu_suct_tmp")
OUTDOOR_INPUTS: Final = ("ou_heat_ex_tmp", "ou_heat_ex_tmp_out")
METHOD_HEAT: Final = (
    "airflow from the measured fan speed (per-model line) x 1.2 kg/m3 x 1005 J/(kg K) "
    "x coil effectiveness (its value at High, rising as the airflow drops when the rated "
    "airflow is known) x (coil - intake temperature)"
)
METHOD_LATENT: Final = "+ moisture removed (room humidity, air leaves saturated at the coil)"


def unit_curve(data: dict[str, Any]) -> est.FanCurve | None:
    """The fan curve for a unit's model setting; None means no heat estimate."""
    model = data.get(CONF_MODEL) or ""
    if model == MODEL_CUSTOM:
        slope, intercept = data.get(CONF_CURVE_SLOPE), data.get(CONF_CURVE_INTERCEPT)
        if slope is None or intercept is None:
            return None
        rated = data.get(CONF_RATED_AIRFLOW)
        return est.FanCurve(
            float(slope), float(intercept), 0, 3000, "custom",
            float(rated) if rated else None,
        )  # fmt: skip
    return est.FAN_CURVES.get(model)


class UnitEstimate:
    """Heat and cooling output of one indoor unit."""

    def __init__(
        self,
        hass: HomeAssistant,
        unit: UnitCoordinator,
        curve: est.FanCurve,
        humidity_entity: str | None,
        effectiveness: float,
        airflow_scale: float,
    ) -> None:
        self.hass = hass
        self.unit = unit
        self.curve = curve
        self.humidity_entity = humidity_entity
        self.effectiveness = effectiveness
        self.airflow_scale = airflow_scale

    def inputs(self) -> est.HeatInputs | None:
        v = self.unit.device.value
        on, mode = v("iu_onoff"), v("iu_op_mode")
        rpm, coil, intake = v("iu_fan_rot1"), v("iu_heat_ex_tmp"), v("iu_suct_tmp")
        if not isinstance(on, bool):
            return None
        if not on:
            return est.HeatInputs(False, None, 0, 0, 0)
        if not (
            isinstance(mode, OpMode)
            and isinstance(rpm, int)
            and not isinstance(rpm, bool)
            and isinstance(coil, float)
            and isinstance(intake, float)
        ):
            return None
        return est.HeatInputs(True, mode, rpm, coil, intake)

    def effectiveness_at(self, rpm: float) -> float:
        """The coil effectiveness at this fan speed."""
        return est.coil_effectiveness(
            self.curve.airflow(rpm), self.curve.rated_airflow, self.effectiveness
        )

    def room_humidity(self) -> float | None:
        if not self.humidity_entity:
            return None
        state = self.hass.states.get(self.humidity_entity)
        try:
            return float(state.state) if state is not None else None
        except ValueError:
            return None

    def output_w(self) -> tuple[float, float] | None:
        """(heating W, cooling W), both >= 0; None while inputs are missing."""
        inputs = self.inputs()
        if inputs is None:
            return None
        q = est.heat_output_w(
            inputs, self.curve, effectiveness=self.effectiveness, airflow_scale=self.airflow_scale
        )
        heating, cooling = max(0.0, q), max(0.0, -q)
        rh = self.room_humidity()
        if cooling > 0 and rh is not None:
            airflow = self.curve.airflow(inputs.fan_rpm) * self.airflow_scale
            cooling += est.latent_cooling_w(
                airflow,
                inputs.intake,
                rh,
                inputs.coil,
                effectiveness=self.effectiveness_at(inputs.fan_rpm),
            )
        return heating, cooling

    def attributes(self) -> dict[str, Any]:
        inputs = self.inputs()
        return {
            "fan_curve": self.curve.source,
            "inputs": None
            if inputs is None
            else {
                "fan_rpm": inputs.fan_rpm,
                "airflow_m3h": round(self.curve.airflow(inputs.fan_rpm) * self.airflow_scale),
                "coil_c": inputs.coil,
                "intake_c": inputs.intake,
                "room_humidity_pct": self.room_humidity(),
                "coil_effectiveness": round(self.effectiveness_at(inputs.fan_rpm), 3),
                "coil_effectiveness_at_high": self.effectiveness,
            },
        }


class SystemEstimates:
    """Totals, energy counters and COP figures for one system (config entry)."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        units: dict[str, UnitCoordinator],
        outdoor: OutdoorSource,
        options: dict[str, Any],
    ) -> None:
        self.hass = hass
        self.outdoor = outdoor
        self.meter: str | None = options.get(OPT_METER) or None
        effectiveness = float(options.get(OPT_EFFECTIVENESS, est.DEFAULT_EFFECTIVENESS))
        scale = float(options.get(OPT_AIRFLOW_SCALE, est.DEFAULT_AIRFLOW_SCALE))
        self.units: dict[str, UnitEstimate] = {}
        for sub_id, unit in units.items():
            data = dict(unit.subentry.data)
            curve = unit_curve(data)
            if curve is not None:
                self.units[sub_id] = UnitEstimate(
                    hass, unit, curve, data.get(CONF_HUMIDITY_ENTITY), effectiveness, scale
                )
        self.tracker = EnergyTracker()
        self.heating_w: float | None = None
        self.cooling_w: float | None = None
        self.electricity_w: float | None = None
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry_id}.energy"
        )
        self._listeners: list[Callable[[], None]] = []
        self._undo: list[Callable[[], None]] = []
        self.clock: Callable[[], float] = time.time  # tests replace it

    @property
    def active(self) -> bool:
        return bool(self.units)

    async def async_start(self) -> None:
        if (stored := await self._store.async_load()) is not None:
            self.tracker.load(stored)
        self.tracker.use_meter(self.meter)  # a new or changed meter: new baseline
        for estimate in self.units.values():
            self._undo.append(estimate.unit.device.subscribe(UNIT_INPUTS))
            self._undo.append(estimate.unit.async_add_listener(self._recalculate))
        outdoor_names = OUTDOOR_INPUTS if self.meter else (*OUTDOOR_INPUTS, "ou_current")
        self._undo.append(self.outdoor.subscribe(outdoor_names))
        if self.meter:
            self._undo.append(
                async_track_state_change_event(self.hass, [self.meter], self._meter_changed)
            )
            if (state := self.hass.states.get(self.meter)) is not None:
                self._meter_state(state)
        self._undo.append(async_track_time_interval(self.hass, self._tick, ESTIMATE_TICK))

    async def async_stop(self) -> None:
        for undo in self._undo:
            undo()
        self._undo.clear()
        await self._store.async_save(self.tracker.to_dict())

    async def async_reset(self) -> None:
        """Zero every counter (energy, electricity, SCOP/SEER, COP window)."""
        self.tracker.reset()
        if self.meter and (state := self.hass.states.get(self.meter)) is not None:
            self._meter_state(state)  # baseline: the meter's reading right now
        self._recalculate()
        await self._store.async_save(self.tracker.to_dict())

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    # -- inputs ------------------------------------------------------------------

    @staticmethod
    def _periods() -> dict[str, str]:
        now = dt_util.now()
        return {"month": now.strftime("%Y-%m"), "year": now.strftime("%Y")}

    @callback
    def _recalculate(self) -> None:
        heating = cooling = 0.0
        known = True
        for estimate in self.units.values():
            output = estimate.output_w()
            if output is None:
                known = False
                continue
            heating += output[0]
            cooling += output[1]
        self.heating_w = heating if known else None
        self.cooling_w = cooling if known else None
        self.electricity_w = None if self.meter else self._estimated_electricity()
        self.tracker.set_power(
            self.clock(),
            heating_w=self.heating_w,
            cooling_w=self.cooling_w,
            electricity_w=self.electricity_w,
            **self._periods(),
        )
        self._changed()

    def _estimated_electricity(self) -> float | None:
        """Outdoor current x 230 V, for systems without a meter (an estimate)."""
        source = self.outdoor.source
        current = source.device.value("ou_current") if source else None
        if isinstance(current, float):
            return current * MAINS_VOLTAGE
        if all(u.unit.device.value("iu_onoff") is False for u in self.units.values()):
            return 0.0
        return None

    @callback
    def _meter_changed(self, event: Event[EventStateChangedData]) -> None:
        """Every reading counts right away, but the sensors are only written on
        the minute tick and after a unit's update: a meter that updates every
        few seconds would otherwise fill the recorder with near-identical rows."""
        if (state := event.data["new_state"]) is not None:
            self._meter_state(state)
            self._save()

    def _meter_state(self, state: State) -> None:
        if state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return
        try:
            value = float(state.state)
        except ValueError:
            return
        unit = state.attributes.get("unit_of_measurement")
        if unit == UnitOfEnergy.WATT_HOUR:
            value /= 1000
        elif unit == UnitOfEnergy.MEGA_WATT_HOUR:
            value *= 1000
        self.tracker.meter_reading(self.clock(), value, **self._periods())

    @callback
    def _tick(self, _now: Any) -> None:
        self.tracker.tick(self.clock(), **self._periods())
        self._changed()

    def _save(self) -> None:
        self._store.async_delay_save(self.tracker.to_dict, SAVE_DELAY_S)

    @callback
    def _changed(self) -> None:
        self._save()
        for listener in list(self._listeners):
            listener()

    # -- results ------------------------------------------------------------------

    def cop_1h(self) -> float | None:
        return self.tracker.cop_1h(self.clock())

    def cop_window(self) -> tuple[float | None, float]:
        return self.tracker.cop_window(self.clock())

    def carnot(self) -> float | None:
        """0.5 x T_hot / (T_hot - T_cold) from the warmest indoor coil and the
        coldest outdoor coil (heating), the other way round when cooling."""
        indoor = [
            c
            for e in self.units.values()
            if e.unit.device.value("iu_onoff") is True
            and isinstance(c := e.unit.device.value("iu_heat_ex_tmp"), float)
        ]
        source = self.outdoor.source
        outdoor = [
            c
            for name in OUTDOOR_INPUTS
            if source is not None and isinstance(c := source.device.value(name), float)
        ]
        if not indoor or not outdoor:
            return None
        if self.tracker.season == "heat":
            return est.carnot_cop(max(indoor), min(outdoor))
        hot, cold = max(outdoor), min(indoor)
        bound = est.carnot_cop(hot, cold)
        return None if bound is None else bound - est.CARNOT_FACTOR  # cooling: T_cold/dT
