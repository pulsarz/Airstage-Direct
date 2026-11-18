"""Compressor starts and stops from a power reading."""

from __future__ import annotations

import pytest

from custom_components.airstage_direct.api.cycles import CompressorTracker

STANDBY, RUN = 12.0, 900.0


def feed(t: CompressorTracker, start: float, end: float, watts: float, step: float = 3) -> float:
    """Readings every `step` seconds from start (inclusive) to end (exclusive)."""
    now = start
    while now < end:
        t.update(now, watts)
        now += step
    return end


def short_cycles(
    t: CompressorTracker, cycles: int, run_s: float = 270, pause_s: float = 216
) -> float:
    now = feed(t, 0, 60, STANDBY)
    for _ in range(cycles):
        now = feed(t, now, now + run_s, RUN)
        now = feed(t, now, now + pause_s, STANDBY)
    return now


def test_counts_short_cycles() -> None:
    t = CompressorTracker()
    now = short_cycles(t, 7)  # 4.5 min on, 3.6 min off: the logged Auto pattern
    assert t.starts == 7
    assert t.starts_last_hour(now) == 7
    assert t.last_run is not None
    assert t.last_run.seconds == pytest.approx(270, abs=4)
    assert t.last_pause is not None
    assert t.last_pause.seconds == pytest.approx(216, abs=4)
    assert t.running is False


def test_the_first_state_is_not_a_start() -> None:
    t = CompressorTracker()
    feed(t, 0, 600, RUN)  # already running when we begin
    assert t.running is True
    assert t.starts == 0
    feed(t, 600, 900, STANDBY)
    assert t.running is False
    assert t.last_run is None  # its start was never seen


def test_a_defrost_pause_is_not_a_stop() -> None:
    t = CompressorTracker()
    now = feed(t, 0, 60, STANDBY)
    now = feed(t, now, now + 600, RUN)
    now = feed(t, now, now + 60, STANDBY)  # a defrost pause of 60 s
    assert t.running is True
    now = feed(t, now, now + 600, RUN)
    assert t.starts == 1
    now = feed(t, now, now + 300, STANDBY)
    assert t.last_run is not None
    assert t.last_run.seconds == pytest.approx(1260, abs=4)  # one run, pause included


def test_blips_and_the_band_between_the_levels() -> None:
    t = CompressorTracker()
    now = feed(t, 0, 60, STANDBY)
    t.update(now, RUN)  # one high reading: not a start
    now = feed(t, now + 3, now + 60, STANDBY)
    assert t.starts == 0
    now = feed(t, now, now + 300, 100)  # between the levels: no change
    assert t.running is False
    now = feed(t, now, now + 300, RUN)
    now = feed(t, now, now + 300, 100)  # back into the band: still running
    assert t.running is True
    assert t.starts == 1


def test_unavailable_readings_are_ignored() -> None:
    t = CompressorTracker()
    now = feed(t, 0, 60, STANDBY)
    now = feed(t, now, now + 300, RUN)
    t.update(now, None)
    assert t.running is True


def test_a_meter_that_only_reports_changes() -> None:
    t = CompressorTracker()
    t.update(0, STANDBY)
    t.tick(30)
    t.update(30, RUN)
    t.tick(45)
    assert t.starts == 1
    t.update(300, STANDBY)
    t.tick(330)
    t.tick(500)  # no new reading, time passes
    assert t.running is False
    assert t.last_run is not None


def test_the_hour_window() -> None:
    t = CompressorTracker()
    now = short_cycles(t, 3)
    assert t.starts_last_hour(now + 3600) == 0
    assert t.starts == 3


def test_callbacks() -> None:
    t = CompressorTracker()
    seen: list[str] = []
    t.on_start = lambda at: seen.append("start")
    t.on_stop = lambda at: seen.append("stop")
    short_cycles(t, 2)
    assert seen == ["start", "stop", "start", "stop"]


def test_levels_must_make_sense() -> None:
    with pytest.raises(ValueError, match="below"):
        CompressorTracker(on_w=100, off_w=150)


def test_a_low_power_cooling_run_counts() -> None:
    t = CompressorTracker()
    now = feed(t, 0, 60, 25.0)
    now = feed(t, now, now + 1200, 170.0)  # cooling near its minimum
    assert t.running is True
    assert t.starts == 1
