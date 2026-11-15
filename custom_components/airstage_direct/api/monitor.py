"""Phase-locked service-monitor reads: one read per adapter per refresh.

Measured (docs/measurements.md): each adapter refreshes its service
monitor every 300 s on its own clock, at its own offset (Downstairs about 100 s,
Upstairs about 298 s past each 5-minute mark of unix time). At the refresh the
indoor values update and every outdoor value reads "" for 20-50 s; the run-time
counters come back last, up to about 50 s after the refresh. Reads in between
return the adapter's cache, so reading more often gains nothing.

The scheduler first learns the offset by polling a few indoor monitor objects
until they change, then reads once per period at offset + READ_DELAY_S. It
learns again when two scheduled reads in a row show signs of a missed window:
outdoor values that have been seen filled before read "" (counted only while
the unit is on, since outdoor values may be empty when it is off). It learns
from raw answers, never from Device.values, whose empty-value hold would hide
both the blanking and the first change.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any, Final

from . import codecs, objects
from .codecs import Missing
from .device import Device
from .transport import AirstageError

_LOGGER = logging.getLogger(__name__)

PERIOD_S: Final = 300.0
READ_DELAY_S: Final = 60.0  # after the refresh: outdoor block and counters are back
LEARN_INTERVAL_S: Final = 10.0
LEARN_TIMEOUT_S: Final = 2 * PERIOD_S
MISSES_TO_RELEARN: Final = 2
IDLE_INTERVAL_S: Final = 10.0  # how often an idle scheduler checks for subscriptions
# Indoor monitor values that change at (nearly) every refresh, even with the unit off.
SIGNAL: Final = ("iu_suct_tmp", "iu_indoor_tmp_crct", "iu_heat_ex_tmp", "iu_fan_rot1")
OUTDOOR: Final = frozenset(o.name for o in objects.INDOOR if o.monitor and o.name.startswith("ou_"))


def circular_mean(phases: Iterable[float], period: float = PERIOD_S) -> float | None:
    """Mean of phases on a circle, so 299 s and 1 s average to 0 s."""
    angles = [2 * math.pi * (p % period) / period for p in phases]
    if not angles:
        return None
    x = sum(math.cos(a) for a in angles)
    y = sum(math.sin(a) for a in angles)
    if math.hypot(x, y) < 1e-9:
        return None
    return (math.atan2(y, x) / (2 * math.pi) * period) % period


def next_read_time(now: float, offset: float, delay: float, period: float = PERIOD_S) -> float:
    """The first time after `now` that is offset + delay past a period boundary."""
    target = (offset + delay) % period
    t = now - (now % period) + target
    while t <= now:
        t += period
    return t


def missed_window(seen: Iterable[str], current: Mapping[str, Any]) -> bool:
    """Signs a read landed inside the blanking window: outdoor values that have
    been seen filled before now read ""."""
    return any(
        name in OUTDOOR and name in current and codecs.sentinel(current[name]) is Missing.EMPTY
        for name in seen
    )


def filled_outdoor(values: Mapping[str, Any]) -> set[str]:
    return {n for n, v in values.items() if n in OUTDOOR and codecs.sentinel(v) is None}


EventHook = Callable[[str, dict[str, Any]], None]
NamesProvider = Callable[[], Iterable[str]]


class MonitorScheduler:
    """Reads one adapter's monitor objects once per refresh, after the last value."""

    def __init__(
        self,
        device: Device,
        names: NamesProvider | None = None,
        *,
        period: float = PERIOD_S,
        read_delay: float = READ_DELAY_S,
        learn_interval: float = LEARN_INTERVAL_S,
        learn_timeout: float | None = None,
        idle_interval: float = IDLE_INTERVAL_S,
        offset: float | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.device = device
        self._names = names or (lambda: (n for n in device.subscribed if n in objects.MONITOR))
        self.period = period
        self.read_delay = read_delay
        self.learn_interval = learn_interval
        self.learn_timeout = learn_timeout if learn_timeout is not None else 2 * period
        self.idle_interval = idle_interval
        self.offset = offset
        self._clock = clock
        self._sleep = sleep
        self._seen_outdoor: set[str] = set()
        self._misses = 0
        self.reads = 0
        self.learn_reads = 0
        self.on_event: EventHook | None = None

    def _event(self, kind: str, **data: Any) -> None:
        if self.on_event is not None:
            self.on_event(kind, data)

    async def learn(self, stop: asyncio.Event | None = None) -> float | None:
        """Poll the indoor signal objects until they change; return the offset.

        The offset is the time of the poll that first saw the change, i.e. the
        latest the refresh can have happened, so the scheduled read is never early.
        """
        names = [n for n in SIGNAL if self.device.supports(n)]
        if not names:
            return None
        if self.device.supports("ou_outdoor_tmp"):
            names.append("ou_outdoor_tmp")  # same request; tells us outdoor values exist
        signal = [n for n in names if n in SIGNAL]
        deadline = self._clock() + self.learn_timeout
        last: dict[str, Any] | None = None
        while self._clock() < deadline:
            if stop is not None and stop.is_set():
                return None
            t = self._clock()
            try:
                values = (await self.device.transport.read(names)).values
            except AirstageError as err:
                self._event("learn_error", error=str(err))
                values = {}
            self.learn_reads += 1
            self._seen_outdoor |= filled_outdoor(values)
            if values:
                if last is not None and any(values.get(n) != last.get(n) for n in signal):
                    seen = self._clock()
                    self.offset = seen % self.period
                    self._event("learned", offset=round(self.offset, 1), window=[t, seen])
                    return self.offset
                last = values
            pause = max(0.0, t + self.learn_interval - self._clock())
            if stop is not None:
                if await self._wait(stop, pause):
                    return None
            else:
                await self._sleep(pause)
        self._event("learn_timeout")
        return None

    async def read_once(self) -> set[str]:
        """One scheduled read of the monitor objects; returns the changed names."""
        names = [n for n in self._names() if self.device.supports(n)]
        if not names:
            return set()
        skip = self.device.capabilities.unknown if self.device.capabilities else set()
        result = await self.device.transport.read(names, skip=skip)
        self.reads += 1
        raw = result.values
        unit_on = self.device.values.get("iu_onoff") is not False
        if unit_on and missed_window(self._seen_outdoor, raw):
            self._misses += 1
            self._event("missed_window", misses=self._misses)
            if self._misses >= MISSES_TO_RELEARN:
                self.offset = None
                self._misses = 0
        else:
            self._misses = 0
        self._seen_outdoor |= filled_outdoor(raw)
        changed = self.device.apply(raw)
        self._event("read", names=len(names), changed=sorted(changed), requests=result.requests)
        return changed

    def active(self) -> bool:
        """True when something needs monitor values; otherwise nothing is read."""
        return any(self.device.supports(n) for n in self._names())

    async def run(self, stop: asyncio.Event) -> None:
        """Learn, then read once per period until `stop` is set. Idle (no requests
        at all, not even learning) while no monitor object is subscribed."""
        while not stop.is_set():
            if not self.active():
                if await self._wait(stop, self.idle_interval):
                    return
                continue
            if self.offset is None and await self.learn(stop) is None:
                if stop.is_set():
                    return
                # No change seen (or no signal objects): read now, try again later.
                self.offset = self._clock() % self.period
                self._event("unsynced", offset=round(self.offset, 1))
            assert self.offset is not None
            when = next_read_time(self._clock(), self.offset, self.read_delay, self.period)
            if await self._wait(stop, when - self._clock()):
                return
            try:
                await self.read_once()
            except AirstageError as err:
                self._event("read_error", error=str(err))

    async def _wait(self, stop: asyncio.Event, seconds: float) -> bool:
        """Sleep, waking early when `stop` is set. True if stopped."""
        if seconds <= 0:
            return stop.is_set()
        waiter = asyncio.ensure_future(stop.wait())
        sleeper = asyncio.ensure_future(self._sleep(seconds))
        try:
            await asyncio.wait({waiter, sleeper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (waiter, sleeper):
                task.cancel()
        return stop.is_set()
