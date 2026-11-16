"""Compressor starts and stops from the system's power sensor (experimental).

Only set up when the user picks a power sensor for the outdoor unit in the
system settings; nothing is read from the adapters for it. The tracker
(api/cycles.py) does the counting; this module feeds it the sensor's readings
and a tick for meters that only report changes.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfPower
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval

from .api.cycles import DEFAULT_OFF_W, DEFAULT_ON_W, CompressorTracker
from .const import COMPRESSOR_TICK, OPT_COMPRESSOR_OFF_W, OPT_COMPRESSOR_ON_W, OPT_POWER_SENSOR


def power_watts(state: State | None) -> float | None:
    """A power sensor's state in W; None while it has no usable value."""
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None
    try:
        value = float(state.state)
    except ValueError:
        return None
    unit = state.attributes.get("unit_of_measurement")
    if unit == UnitOfPower.KILO_WATT:
        return value * 1000
    if unit == UnitOfPower.MILLIWATT:
        return value / 1000
    return value


class CompressorMonitor:
    def __init__(self, hass: HomeAssistant, options: dict[str, Any]) -> None:
        self.hass = hass
        self.entity_id: str = options[OPT_POWER_SENSOR]
        self.tracker = CompressorTracker(
            on_w=float(options.get(OPT_COMPRESSOR_ON_W, DEFAULT_ON_W)),
            off_w=float(options.get(OPT_COMPRESSOR_OFF_W, DEFAULT_OFF_W)),
        )
        self.clock: Callable[[], float] = time.time  # tests replace it
        self._listeners: list[Callable[[], None]] = []
        self._undo: list[Callable[[], None]] = []
        self._shown: tuple[Any, ...] | None = None

    @staticmethod
    def configured(options: dict[str, Any]) -> bool:
        return bool(options.get(OPT_POWER_SENSOR))

    def start(self) -> None:
        self._undo.append(
            async_track_state_change_event(self.hass, [self.entity_id], self._state_changed)
        )
        self._undo.append(async_track_time_interval(self.hass, self._tick, COMPRESSOR_TICK))
        self.tracker.update(self.clock(), power_watts(self.hass.states.get(self.entity_id)))

    def stop(self) -> None:
        for undo in self._undo:
            undo()
        self._undo.clear()

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def starts_last_hour(self) -> int:
        return self.tracker.starts_last_hour(self.clock())

    @callback
    def _state_changed(self, event: Event[EventStateChangedData]) -> None:
        self.tracker.update(self.clock(), power_watts(event.data["new_state"]))
        self._notify()

    @callback
    def _tick(self, _now: Any) -> None:
        self.tracker.tick(self.clock())
        self._notify()

    def _notify(self) -> None:
        """Tell the sensors only when what they show changes, not on every reading."""
        t = self.tracker
        shown = (t.running, t.starts, self.starts_last_hour(), t.last_run, t.last_pause)
        if shown == self._shown:
            return
        self._shown = shown
        for listener in list(self._listeners):
            listener()
