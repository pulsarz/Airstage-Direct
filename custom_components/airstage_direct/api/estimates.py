"""Estimated heat output, COP and the Carnot bound. Calculated, not measured.

Heat output of one indoor unit (heating, sensible):

    Q = V * rho * c_p * eps * (T_coil - T_intake)

- V, the airflow, comes from the measured fan speed through a per-model line:
  rpm against the rated airflow per fan setting from the KJCAL service manual
  fits a straight line within 1.6 % (docs/measurements.md).
- rho = 1.2 kg/m3, c_p = 1005 J/(kg K); an airflow scale (default 1.0) is a
  setting.
- eps, the coil effectiveness, is a setting at the High fan airflow (default
  0.85). With the model's rated High airflow known it follows the airflow like
  a coil at constant refrigerant temperature: eps = 1 - exp(-NTU), and since
  the air side carries most of the resistance (heat transfer ~ airflow^0.6),
  NTU ~ airflow^-0.4. Slower air leaves closer to the coil temperature: about
  0.90 at Medium and 0.93 at Quiet. The direction is textbook; the exponent is
  typical for finned coils (0.3-0.5 moves Quiet by +-0.015), not measured.
- T_intake is the raw intake (iu_suct_tmp), which coil heat warms a little, so
  the estimate leans low.
- 0 when the fan stands still, the unit is off, or it only runs the fan.
  Defrost has not been measured; heating output is never negative.

Expect +-20-25 % on any single value; hourly and longer averages are what these
are good for, since the inputs refresh only every 5 minutes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from .codecs import OpMode

AIR_DENSITY: Final = 1.2  # kg/m3
AIR_HEAT_CAPACITY: Final = 1005.0  # J/(kg K)
DEFAULT_EFFECTIVENESS: Final = 0.85
DEFAULT_AIRFLOW_SCALE: Final = 1.0
ACCURACY: Final = "±20–25 %"  # noqa: RUF001 - the plan's wording
MIN_CARNOT_DELTA_K: Final = 5.0
ATMOSPHERE_PA: Final = 101325.0
LATENT_HEAT_J_KG: Final = 2.45e6  # water, around 20 C
CARNOT_FACTOR: Final = 0.5  # a practical heat pump reaches about half of Carnot
NTU_AIRFLOW_EXPONENT: Final = 0.4  # NTU ~ airflow^-0.4 (air-side h ~ velocity^0.6)
MAX_EFFECTIVENESS: Final = 0.99


@dataclass(frozen=True, slots=True)
class FanCurve:
    """Airflow (m3/h) = slope * rpm + intercept, within the measured rpm range.

    rated_airflow is the rated airflow at High (m3/h); without it the coil
    effectiveness stays fixed."""

    slope: float
    intercept: float
    min_rpm: float
    max_rpm: float
    source: str
    rated_airflow: float | None = None

    def airflow(self, rpm: float) -> float:
        """m3/h. Clamped to the measured range on top, never below zero."""
        if rpm <= 0:
            return 0.0
        rpm = min(rpm, self.max_rpm * 1.1)  # do not extrapolate far beyond the data
        return max(0.0, self.slope * rpm + self.intercept)


# Measured 2026-11-15 (rpm per fan setting, paired with the manual's rated airflow).
FAN_CURVES: Final = {
    "ASEH07KJCAL": FanCurve(
        0.706, -141.0, 570, 960, "measured rpm, rated airflow (KJCAL manual)", 540.0
    ),
    "ASEH14KJCAL": FanCurve(
        0.683, -163.0, 610, 1290, "measured rpm, rated airflow (KJCAL manual)", 720.0
    ),
}


def coil_effectiveness(
    airflow_m3h: float, rated_airflow_m3h: float | None, at_high: float
) -> float:
    """The coil effectiveness at this airflow, from its value at the rated High
    airflow (see the module notes). Fixed at `at_high` without a rated airflow."""
    if rated_airflow_m3h is None or rated_airflow_m3h <= 0 or airflow_m3h <= 0:
        return at_high
    ref = min(max(at_high, 0.01), MAX_EFFECTIVENESS)
    ntu = -math.log(1 - ref) * (airflow_m3h / rated_airflow_m3h) ** -NTU_AIRFLOW_EXPONENT
    return 1 - math.exp(-ntu)


@dataclass(frozen=True, slots=True)
class HeatInputs:
    on: bool
    mode: OpMode | None
    fan_rpm: float
    coil: float  # deg C
    intake: float  # deg C


def heat_output_w(
    inputs: HeatInputs,
    curve: FanCurve,
    *,
    effectiveness: float = DEFAULT_EFFECTIVENESS,
    airflow_scale: float = DEFAULT_AIRFLOW_SCALE,
) -> float:
    """Estimated sensible heat output in W: positive heating, negative cooling.
    `effectiveness` is the coil effectiveness at High (see coil_effectiveness).

    In Auto the sign of (coil - intake) decides; heating never goes below 0 and
    cooling never above 0 (defrost and thermo-off show as 0, not as the reverse).
    """
    if not inputs.on or inputs.mode in (OpMode.FAN, None) or inputs.fan_rpm <= 0:
        return 0.0
    airflow = curve.airflow(inputs.fan_rpm)
    eps = coil_effectiveness(airflow, curve.rated_airflow, effectiveness)
    q = airflow * airflow_scale / 3600 * AIR_DENSITY * AIR_HEAT_CAPACITY * eps
    q *= inputs.coil - inputs.intake
    if inputs.mode is OpMode.HEAT:
        return max(0.0, q)
    if inputs.mode in (OpMode.COOL, OpMode.DRY):
        return min(0.0, q)
    return q  # Auto: whichever way the coil works


def saturation_pressure_pa(t_c: float) -> float:
    """Water vapour saturation pressure over water (Magnus), Pa."""
    return 610.94 * math.exp(17.625 * t_c / (t_c + 243.04))


def humidity_ratio(t_c: float, rh_percent: float) -> float:
    """kg water per kg dry air at sea-level pressure."""
    p_w = saturation_pressure_pa(t_c) * max(0.0, min(rh_percent, 100.0)) / 100
    return 0.622 * p_w / (ATMOSPHERE_PA - p_w)


def latent_cooling_w(
    airflow_m3h: float,
    intake_c: float,
    room_rh_percent: float,
    coil_c: float,
    *,
    effectiveness: float = DEFAULT_EFFECTIVENESS,
) -> float:
    """Latent cooling (moisture removed) in W, >= 0: the air leaves at most saturated
    at the coil temperature. Needs the room humidity; not checked on hardware."""
    w_in = humidity_ratio(intake_c, room_rh_percent)
    w_out = min(w_in, humidity_ratio(coil_c, 100.0))
    mass_flow = airflow_m3h / 3600 * AIR_DENSITY
    return max(0.0, mass_flow * (w_in - w_out) * LATENT_HEAT_J_KG * effectiveness)


def carnot_cop(t_hot_c: float, t_cold_c: float, factor: float = CARNOT_FACTOR) -> float | None:
    """A plausible upper bound for heating COP: factor * T_hot / (T_hot - T_cold), in K.

    None when the temperatures are too close for the bound to mean anything.
    """
    delta = t_hot_c - t_cold_c
    if delta < MIN_CARNOT_DELTA_K:
        return None
    return factor * (t_hot_c + 273.15) / delta


def cop(heat_kwh: float, electricity_kwh: float, min_electricity_kwh: float = 0.05) -> float | None:
    """Heat (or cooling) energy over electricity; None while too little was used."""
    if electricity_kwh < min_electricity_kwh:
        return None
    return abs(heat_kwh) / electricity_kwh
