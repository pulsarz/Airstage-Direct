"""Compressor starts and stops from an electricity meter's power reading.

The adapter only reports the compressor every 5 minutes, which hides short
cycles (docs/measurements.md: runs of 3-5 minutes with 3.6-minute pauses).
A power meter for the outdoor unit sees each start and stop within seconds.

Pure Python, with time passed in:

- Above `on_w` for `on_s` seconds is running; below `off_w` for `off_s`
  seconds is stopped; in between keeps the current state.
- A stop shorter than `min_pause_s` (a defrost pauses the compressor for
  30-70 s, meters blip) is not a stop: the run simply continues.
- The first state seen is not a start or stop: when it began is unknown.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal

DEFAULT_ON_W: Final = 250.0
DEFAULT_OFF_W: Final = 150.0
ON_S: Final = 10.0
OFF_S: Final = 20.0
MIN_PAUSE_S: Final = 120.0
HOUR_S: Final = 3600.0

State = Literal["running", "stopped"]


@dataclass(frozen=True, slots=True)
class Cycle:
    """A finished run or pause, in seconds."""

    started: float
    ended: float

    @property
    def seconds(self) -> float:
        return self.ended - self.started


class CompressorTracker:
    def __init__(
        self,
        *,
        on_w: float = DEFAULT_ON_W,
        off_w: float = DEFAULT_OFF_W,
        on_s: float = ON_S,
        off_s: float = OFF_S,
        min_pause_s: float = MIN_PAUSE_S,
    ) -> None:
        if off_w >= on_w:
            raise ValueError("the off level must be below the on level")
        self.on_w, self.off_w = on_w, off_w
        self.on_s, self.off_s, self.min_pause_s = on_s, off_s, min_pause_s
        self.state: State | None = None
        self.since: float | None = None  # when the current state began (None: unknown)
        self.starts = 0  # counted starts, since this tracker began
        self.last_run: Cycle | None = None
        self.last_pause: Cycle | None = None
        self.on_start: Callable[[float], None] | None = None
        self.on_stop: Callable[[float], None] | None = None
        self._recent: deque[float] = deque()  # start times, for the last hour
        self._candidate: tuple[State, float] | None = None  # (state, since)
        self._pause_from: float | None = None  # a stop not yet long enough to count

    # -- input -------------------------------------------------------------------

    def update(self, now: float, watts: float | None) -> None:
        """A power reading (W). None (unavailable) is ignored."""
        if watts is not None:
            level: State | None = None
            if watts > self.on_w:
                level = "running"
            elif watts < self.off_w:
                level = "stopped"
            if level is None or level == self._settled_level():
                self._candidate = None
            elif self._candidate is None or self._candidate[0] != level:
                self._candidate = (level, now)
        self.tick(now)

    def tick(self, now: float) -> None:
        """Let time pass without a reading (meters that only report changes)."""
        if self._candidate is not None:
            level, since = self._candidate
            hold = self.on_s if level == "running" else self.off_s
            if now - since >= hold:
                self._candidate = None
                self._settle(level, since)
        if self._pause_from is not None and now - self._pause_from >= self.min_pause_s:
            self._confirm_stop()
        self._trim(now)

    # -- results -----------------------------------------------------------------

    @property
    def running(self) -> bool | None:
        if self.state is None:
            return None
        return self.state == "running" or self._pause_from is not None

    def starts_last_hour(self, now: float) -> int:
        self._trim(now)
        return len(self._recent)

    # -- internals ----------------------------------------------------------------

    def _settled_level(self) -> State | None:
        """The level the tracker is at, counting a pending short pause as stopped."""
        if self._pause_from is not None:
            return "stopped"
        return self.state

    def _settle(self, level: State, at: float) -> None:
        if self.state is None:
            self.state, self.since = level, None  # first state: its start is unknown
            return
        if level == "stopped":
            if self.state == "running":
                self._pause_from = at  # counts once it lasts min_pause_s
            return
        if self._pause_from is not None:
            self._pause_from = None  # a short pause (defrost, blip): the run goes on
            return
        if self.state == "stopped":
            if self.since is not None:
                self.last_pause = Cycle(self.since, at)
            self.state, self.since = "running", at
            self.starts += 1
            self._recent.append(at)
            if self.on_start:
                self.on_start(at)

    def _confirm_stop(self) -> None:
        at = self._pause_from
        assert at is not None
        self._pause_from = None
        if self.since is not None:
            self.last_run = Cycle(self.since, at)
        self.state, self.since = "stopped", at
        if self.on_stop:
            self.on_stop(at)

    def _trim(self, now: float) -> None:
        while self._recent and self._recent[0] <= now - HOUR_S:
            self._recent.popleft()
