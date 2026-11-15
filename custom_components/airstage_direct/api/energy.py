"""Energy bookkeeping for the estimates: heat, cooling and electricity counters.

All of it is calculated, not measured (see estimates.py). Pure Python, with
time passed in, so it can be tested without Home Assistant.

- Heat and cooling power (W) is held between readings and integrated over
  wall-clock time; a gap longer than `max_gap_s` (two monitor cycles) is not
  integrated, so an outage never invents energy.
- Electricity comes from a kWh meter reading (a decrease starts a new baseline
  rather than counting as negative) or, without a meter, from an estimated
  power integrated like heat.
- Every kWh of electricity goes to the season at the time: heating when a unit
  heats, cooling when one cools, otherwise the last season.
- Month and year totals roll over on the period ids the caller passes (local
  calendar), for SCOP and SEER; a one-hour window gives the hourly COP.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Final, Literal

Season = Literal["heat", "cool"]

MAX_GAP_S: Final = 600.0
WINDOW_S: Final = 3600.0
MIN_WINDOW_COVERAGE_S: Final = 3300.0  # a full hourly COP needs nearly an hour of data
PROVISIONAL_COVERAGE_S: Final = 900.0  # from 15 minutes on, shown as provisional
MIN_ELECTRICITY_KWH: Final = 0.05


@dataclass
class Totals:
    heat_kwh: float = 0.0
    cool_kwh: float = 0.0
    electricity_heat_kwh: float = 0.0
    electricity_cool_kwh: float = 0.0

    def add(self, other: Totals) -> None:
        self.heat_kwh += other.heat_kwh
        self.cool_kwh += other.cool_kwh
        self.electricity_heat_kwh += other.electricity_heat_kwh
        self.electricity_cool_kwh += other.electricity_cool_kwh


@dataclass
class Period:
    id: str = ""
    totals: Totals = field(default_factory=Totals)


class EnergyTracker:
    def __init__(self, *, max_gap_s: float = MAX_GAP_S, window_s: float = WINDOW_S) -> None:
        self.max_gap_s = max_gap_s
        self.window_s = window_s
        self.lifetime = Totals()
        self.month = Period()
        self.year = Period()
        self.season: Season = "heat"
        self._heat_w: float | None = None  # current heating power
        self._cool_w: float | None = None  # current cooling power (positive)
        self._elec_w: float | None = None  # estimated electric power, no meter
        self._last_t: float | None = None
        self._meter_kwh: float | None = None
        self.meter_id: str | None = None  # which meter _meter_kwh came from
        self._window: deque[tuple[float, Totals]] = deque()

    # -- inputs ------------------------------------------------------------------

    def set_power(
        self,
        now: float,
        *,
        heating_w: float | None,
        cooling_w: float | None,
        electricity_w: float | None = None,
        month: str,
        year: str,
    ) -> None:
        """New power readings. Integrates the previous ones up to `now` first."""
        self._integrate(now, month, year)
        self._heat_w, self._cool_w, self._elec_w = heating_w, cooling_w, electricity_w
        if heating_w and heating_w > 0:
            self.season = "heat"
        elif cooling_w and cooling_w > 0:
            self.season = "cool"

    def meter_reading(self, now: float, kwh: float, *, month: str, year: str) -> None:
        """A reading of the electricity meter (kWh, increasing)."""
        self._integrate(now, month, year)
        last, self._meter_kwh = self._meter_kwh, kwh
        if last is None or kwh < last:
            return  # first reading, or the meter was reset/replaced: new baseline
        self._add(now, self._electricity(kwh - last), month, year)

    def reset(self) -> None:
        """Start all counters from zero (keeps the meter, re-baselined)."""
        meter_id = self.meter_id
        self.__init__(max_gap_s=self.max_gap_s, window_s=self.window_s)  # type: ignore[misc]
        self.meter_id = meter_id

    def use_meter(self, meter_id: str | None) -> None:
        """The meter readings come from. A different meter (or none) starts a new
        baseline: the jump between two meters' readings is not electricity used."""
        if meter_id != self.meter_id:
            self.meter_id = meter_id
            self._meter_kwh = None

    def tick(self, now: float, *, month: str, year: str) -> None:
        """Integrate up to now without new readings (e.g. once a minute)."""
        self._integrate(now, month, year)

    # -- results ------------------------------------------------------------------

    def cop_1h(self, now: float) -> float | None:
        return self.cop_window(now)[0]

    def cop_window(self, now: float) -> tuple[float | None, float]:
        """(COP over the last hour, seconds of data it covers). The COP is None
        with less than 15 minutes of data or too little electricity; with less
        than 55 minutes it is provisional (see `is_provisional`)."""
        recent = [(t, d) for t, d in self._window if t >= now - self.window_s]
        coverage = now - recent[0][0] if recent else 0.0
        if coverage < PROVISIONAL_COVERAGE_S:
            return None, coverage
        heat = sum(d.heat_kwh + d.cool_kwh for _, d in recent)
        elec = sum(d.electricity_heat_kwh + d.electricity_cool_kwh for _, d in recent)
        return (heat / elec if elec >= MIN_ELECTRICITY_KWH else None), coverage

    @staticmethod
    def is_provisional(coverage_s: float) -> bool:
        return coverage_s < MIN_WINDOW_COVERAGE_S

    def window_started(self) -> float | None:
        return self._window[0][0] if self._window else None

    @staticmethod
    def scop(period: Period) -> float | None:
        t = period.totals
        return t.heat_kwh / t.electricity_heat_kwh if t.electricity_heat_kwh >= 0.5 else None

    @staticmethod
    def seer(period: Period) -> float | None:
        t = period.totals
        return t.cool_kwh / t.electricity_cool_kwh if t.electricity_cool_kwh >= 0.5 else None

    # -- internals ------------------------------------------------------------------

    def _electricity(self, kwh: float) -> Totals:
        if self.season == "heat":
            return Totals(electricity_heat_kwh=kwh)
        return Totals(electricity_cool_kwh=kwh)

    def _integrate(self, now: float, month: str, year: str) -> None:
        last, self._last_t = self._last_t, now
        if last is None:
            self._roll(month, year)
            return
        dt = now - last
        if dt <= 0 or dt > self.max_gap_s:
            self._roll(month, year)
            return
        hours = dt / 3600
        delta = Totals(
            heat_kwh=max(0.0, self._heat_w or 0.0) * hours / 1000,
            cool_kwh=max(0.0, self._cool_w or 0.0) * hours / 1000,
        )
        if self._elec_w is not None and self._meter_kwh is None:
            delta.add(self._electricity(max(0.0, self._elec_w) * hours / 1000))
        self._add(now, delta, month, year)

    def _roll(self, month: str, year: str) -> None:
        if self.month.id != month:
            self.month = Period(month)
        if self.year.id != year:
            self.year = Period(year)

    def _add(self, now: float, delta: Totals, month: str, year: str) -> None:
        self._roll(month, year)
        for totals in (self.lifetime, self.month.totals, self.year.totals):
            totals.add(delta)
        self._window.append((now, delta))
        while self._window and self._window[0][0] < now - self.window_s:
            self._window.popleft()

    # -- storage ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "lifetime": asdict(self.lifetime),
            "month": {"id": self.month.id, "totals": asdict(self.month.totals)},
            "year": {"id": self.year.id, "totals": asdict(self.year.totals)},
            "season": self.season,
            "meter_kwh": self._meter_kwh,
            "meter_id": self.meter_id,
            "window": [[t, asdict(d)] for t, d in self._window],
        }

    def load(self, data: dict[str, Any]) -> None:
        self.lifetime = Totals(**data.get("lifetime", {}))
        for name in ("month", "year"):
            period = data.get(name) or {}
            setattr(self, name, Period(period.get("id", ""), Totals(**period.get("totals", {}))))
        self.season = "cool" if data.get("season") == "cool" else "heat"
        self._meter_kwh = data.get("meter_kwh")
        self.meter_id = data.get("meter_id")
        self._window = deque((float(t), Totals(**d)) for t, d in data.get("window", []))
