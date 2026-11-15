"""Estimates against hand calculations and recorded cycles."""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from custom_components.airstage_direct.api import estimates as est
from custom_components.airstage_direct.api.codecs import OpMode

C14 = est.FAN_CURVES["ASEH14KJCAL"]
C07 = est.FAN_CURVES["ASEH07KJCAL"]


def heat(mode: OpMode | None, rpm: float, coil: float, intake: float, on: bool = True) -> float:
    return est.heat_output_w(est.HeatInputs(on, mode, rpm, coil, intake), C14)


def test_curves_reproduce_the_rated_airflow() -> None:
    # Rated heating airflow (manual) at the measured rpm, within 2 %.
    for curve, points in (
        (C14, [(610, 250), (700, 320), (960, 490), (1290, 720)]),
        (C07, [(570, 260), (670, 330), (810, 430), (960, 540)]),
    ):
        for rpm, airflow in points:
            assert curve.airflow(rpm) == pytest.approx(airflow, rel=0.02)


def test_airflow_is_never_negative_nor_far_extrapolated() -> None:
    assert C14.airflow(0) == 0
    assert C14.airflow(100) == 0  # the line goes below zero there
    assert C14.airflow(5000) == C14.airflow(1290 * 1.1)


def test_recorded_downstairs_cycle() -> None:
    """13:21 on 2026-11-15: Medium, 960 rpm, coil 41.50 C, intake 24.25 C."""
    fixed = (0.683 * 960 - 163) / 3600 * 1.2 * 1005 * 0.85 * (41.5 - 24.25)
    assert fixed == pytest.approx(2420, abs=5)  # 492.7 m3/h, 17.25 K, eps 0.85
    # At Medium (492.7 of 720 m3/h): NTU = 1.897 * 0.684^-0.4 = 2.208, eps = 0.890.
    by_hand = fixed / 0.85 * (1 - math.exp(math.log(0.15) * (492.68 / 720) ** -0.4))
    assert by_hand == pytest.approx(2534, abs=5)
    assert heat(OpMode.HEAT, 960, 41.5, 24.25) == pytest.approx(by_hand, rel=1e-3)
    no_rated = replace(C14, rated_airflow=None)
    inputs = est.HeatInputs(True, OpMode.HEAT, 960, 41.5, 24.25)
    assert est.heat_output_w(inputs, no_rated) == pytest.approx(fixed)


def test_coil_effectiveness_follows_the_airflow() -> None:
    eps = est.coil_effectiveness
    assert eps(720, 720, 0.85) == pytest.approx(0.85)
    assert eps(576, 720, 0.85) == pytest.approx(0.874, abs=0.002)  # 80 %
    assert eps(432, 720, 0.85) == pytest.approx(0.903, abs=0.002)  # 60 %
    assert eps(288, 720, 0.85) == pytest.approx(0.935, abs=0.002)  # 40 %
    assert eps(800, 720, 0.85) < 0.85  # above the rated airflow: a little lower
    assert eps(10, 720, 0.85) < 1
    assert eps(432, None, 0.85) == 0.85  # no rated airflow: fixed
    assert eps(432, 720, 1.0) < 1  # a setting of 1.0 does not break the formula
    for curve in (C07, C14):  # the rated airflow is the curve's High point
        assert curve.rated_airflow == pytest.approx(curve.airflow(curve.max_rpm), rel=0.02)


def test_no_output_when_it_cannot_be() -> None:
    assert heat(OpMode.HEAT, 960, 41.5, 24.25, on=False) == 0
    assert heat(OpMode.HEAT, 0, 41.5, 24.25) == 0
    assert heat(OpMode.FAN, 960, 25, 24) == 0
    assert heat(None, 960, 41.5, 24.25) == 0


def test_signs_by_mode() -> None:
    assert heat(OpMode.HEAT, 960, 20, 24) == 0  # coil colder (e.g. defrost): not negative
    assert heat(OpMode.COOL, 960, 10, 25) < 0
    assert heat(OpMode.COOL, 960, 30, 25) == 0
    assert heat(OpMode.DRY, 960, 12, 25) < 0
    assert heat(OpMode.AUTO, 960, 40, 24) > 0
    assert heat(OpMode.AUTO, 960, 10, 24) < 0


def test_settings_scale_the_result() -> None:
    inputs = est.HeatInputs(True, OpMode.HEAT, 960, 41.5, 24.25)
    base = est.heat_output_w(inputs, C14)
    fixed = replace(C14, rated_airflow=None)
    assert est.heat_output_w(inputs, fixed, effectiveness=0.425) == pytest.approx(
        est.heat_output_w(inputs, fixed) / 2
    )
    assert est.heat_output_w(inputs, C14, effectiveness=0.7) < base
    assert est.heat_output_w(inputs, C14, airflow_scale=2) == pytest.approx(base * 2)


def test_carnot_bound() -> None:
    bound = est.carnot_cop(45, 0)
    assert bound == pytest.approx(0.5 * 318.15 / 45)
    assert est.carnot_cop(25, 22) is None


def test_cop() -> None:
    assert est.cop(3.0, 1.0) == 3.0
    assert est.cop(-2.5, 1.0) == 2.5  # cooling energy counts as its size
    assert est.cop(1.0, 0.01) is None


def test_humidity_ratio() -> None:
    # 20 C, 50 %: about 7.3 g/kg (psychrometric tables)
    assert est.humidity_ratio(20, 50) == pytest.approx(0.00726, rel=0.02)
    assert est.humidity_ratio(20, 0) == 0


def test_latent_cooling() -> None:
    # 500 m3/h, 26 C / 60 % in (12.6 g/kg), coil 10 C (saturated 7.66 g/kg).
    w_in, w_out = est.humidity_ratio(26, 60), est.humidity_ratio(10, 100)
    by_hand = 500 / 3600 * 1.2 * (w_in - w_out) * 2.45e6 * 0.85
    assert est.latent_cooling_w(500, 26, 60, 10) == pytest.approx(by_hand)
    assert by_hand == pytest.approx(1720, rel=0.03)  # 0.167 kg/s * 4.96 g/kg * 2.45 MJ/kg * 0.85
    assert est.latent_cooling_w(500, 26, 30, 15) == 0  # dry air: nothing condenses
