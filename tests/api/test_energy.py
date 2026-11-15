"""Energy bookkeeping: integration, gaps, meter resets, seasons, periods, COP."""

from __future__ import annotations

import pytest

from custom_components.airstage_direct.api.energy import EnergyTracker

M, Y = "2026-11", "2026"


def test_heat_is_integrated_over_time() -> None:
    t = EnergyTracker()
    t.set_power(0, heating_w=2000, cooling_w=0, month=M, year=Y)
    t.tick(300, month=M, year=Y)  # 2 kW for 5 minutes
    t.set_power(600, heating_w=1000, cooling_w=0, month=M, year=Y)  # held 2 kW until now
    t.tick(900, month=M, year=Y)
    assert t.lifetime.heat_kwh == pytest.approx(2 * 600 / 3600 + 1 * 300 / 3600)
    assert t.month.totals.heat_kwh == t.lifetime.heat_kwh
    assert t.lifetime.cool_kwh == 0


def test_long_gaps_add_nothing() -> None:
    t = EnergyTracker(max_gap_s=600)
    t.set_power(0, heating_w=2000, cooling_w=0, month=M, year=Y)
    t.tick(3600, month=M, year=Y)  # an outage of an hour
    assert t.lifetime.heat_kwh == 0
    t.tick(3900, month=M, year=Y)
    assert t.lifetime.heat_kwh == pytest.approx(2 * 300 / 3600)


def test_meter_split_by_season_and_resets() -> None:
    t = EnergyTracker()
    t.meter_reading(0, 100.0, month=M, year=Y)  # baseline
    t.set_power(10, heating_w=1500, cooling_w=0, month=M, year=Y)
    t.meter_reading(20, 100.5, month=M, year=Y)
    assert t.lifetime.electricity_heat_kwh == pytest.approx(0.5)
    t.meter_reading(30, 3.0, month=M, year=Y)  # meter replaced: new baseline
    t.meter_reading(40, 3.25, month=M, year=Y)
    assert t.lifetime.electricity_heat_kwh == pytest.approx(0.75)
    t.set_power(50, heating_w=0, cooling_w=1200, month=M, year=Y)
    t.meter_reading(60, 3.5, month=M, year=Y)
    assert t.lifetime.electricity_cool_kwh == pytest.approx(0.25)
    t.set_power(70, heating_w=0, cooling_w=0, month=M, year=Y)  # all off: last season
    t.meter_reading(80, 3.6, month=M, year=Y)
    assert t.lifetime.electricity_cool_kwh == pytest.approx(0.35)


def test_estimated_electricity_without_a_meter() -> None:
    t = EnergyTracker()
    t.set_power(0, heating_w=2000, cooling_w=0, electricity_w=600, month=M, year=Y)
    t.tick(600, month=M, year=Y)
    assert t.lifetime.electricity_heat_kwh == pytest.approx(0.6 * 600 / 3600)


def test_periods_roll_over() -> None:
    t = EnergyTracker()
    t.set_power(0, heating_w=3600, cooling_w=0, month=M, year=Y)
    t.tick(500, month=M, year=Y)
    t.tick(1000, month="2026-12", year=Y)
    assert t.month.id == "2026-12"
    assert t.month.totals.heat_kwh == pytest.approx(0.5)  # since the month started
    assert t.year.totals.heat_kwh == pytest.approx(1.0)
    t.tick(1500, month="2027-01", year="2027")
    assert t.year.totals.heat_kwh == pytest.approx(0.5)
    assert t.lifetime.heat_kwh == pytest.approx(1.5)


def test_cop_1h_needs_an_hour_and_electricity() -> None:
    t = EnergyTracker()
    t.meter_reading(0, 0.0, month=M, year=Y)
    t.set_power(0, heating_w=3000, cooling_w=0, month=M, year=Y)
    for minute in range(1, 61):
        now = minute * 60
        t.meter_reading(now, minute / 60, month=M, year=Y)  # 1 kW electric
        if minute == 10:
            assert t.cop_1h(now) is None  # under 15 minutes: nothing yet
        if minute == 30:
            cop, coverage = t.cop_window(now)
            assert cop == pytest.approx(3.0, rel=0.05)  # provisional
            assert t.is_provisional(coverage)
    cop, coverage = t.cop_window(3600)
    assert cop == pytest.approx(3.0, rel=0.05)
    assert not t.is_provisional(coverage)


def test_the_hour_window_survives_a_restart() -> None:
    t = EnergyTracker()
    t.meter_reading(0, 0.0, month=M, year=Y)
    t.set_power(0, heating_w=3000, cooling_w=0, month=M, year=Y)
    for minute in range(1, 41):
        t.meter_reading(minute * 60, minute / 60, month=M, year=Y)
    again = EnergyTracker()
    again.load(t.to_dict())
    assert again.cop_window(2400) == t.cop_window(2400)
    assert again.cop_1h(2400) == pytest.approx(3.0, rel=0.05)


def test_scop_and_seer() -> None:
    t = EnergyTracker()
    t.month.totals.heat_kwh, t.month.totals.electricity_heat_kwh = 30.0, 10.0
    assert EnergyTracker.scop(t.month) == 3.0
    assert EnergyTracker.seer(t.month) is None  # no cooling electricity


def test_round_trip() -> None:
    t = EnergyTracker()
    t.meter_reading(0, 50.0, month=M, year=Y)
    t.set_power(0, heating_w=0, cooling_w=900, month=M, year=Y)
    t.tick(300, month=M, year=Y)
    again = EnergyTracker()
    again.load(t.to_dict())
    assert again.to_dict() == t.to_dict()
    assert again.season == "cool"


def test_a_different_meter_starts_a_new_baseline() -> None:
    t = EnergyTracker()
    t.use_meter("sensor.house")
    t.meter_reading(0, 300.0, month=M, year=Y)
    saved = t.to_dict()
    again = EnergyTracker()
    again.load(saved)
    again.use_meter("sensor.airco")  # the user picks another meter
    again.meter_reading(10, 12345.0, month=M, year=Y)
    assert again.lifetime.electricity_heat_kwh == 0  # not 12045 kWh
    again.meter_reading(20, 12345.5, month=M, year=Y)
    assert again.lifetime.electricity_heat_kwh == pytest.approx(0.5)
    same = EnergyTracker()
    same.load(again.to_dict())
    same.use_meter("sensor.airco")  # same meter after a restart: keep the baseline
    same.meter_reading(30, 12346.0, month=M, year=Y)
    assert same.lifetime.electricity_heat_kwh == pytest.approx(1.0)
