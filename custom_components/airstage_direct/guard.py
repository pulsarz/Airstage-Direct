"""Short-cycle guard (experimental, off unless switched on in the system settings).

Measured on a short-cycling unit (docs/measurements.md, 2026-11-16): the unit
switches its compressor off when its own corrected intake reaches setpoint
+ 1 C and restarts as soon as that reading dips, after a 3.6-minute minimum
pause. The reading follows the unit's own warm air: it swung from 1.5 C above
the room to 1 C below it within minutes while the room did not move. So the
unit makes short runs (stopped early by false warmth) and short pauses
(restarted by a false dip). Two levers, each optional:

- **Fan floor.** While a unit heats with its fan on Auto, the guard sets a
  fixed speed instead (Low-medium by default). Any other fan choice is left
  alone. When the guard stops, or the unit leaves heat, a fan the guard set
  goes back to Auto.
- **Room thermostat.** With a room temperature sensor for the unit, that
  sensor decides when the unit heats, with a band around the user's
  setpoint (0.3 C by default), like a central-heating thermostat:
  - *run*: the unit's setpoint is raised (1 C by default), so its own sensor
    does not stop it early; it runs, slowing down as the room warms, until
    the room reaches the setpoint plus the band;
  - *pause*: the unit's setpoint is lowered (3 C by default), so it cannot
    restart on its own sensor; it stays off until the room has dropped to
    the setpoint minus the band, then a new run starts.
  When the house needs more heat than the compressor's minimum, the room
  never reaches the top of the band and the unit simply keeps running.
  Changes are at least 5 minutes apart.

Fail-safe rules: the user's setpoint is stored before the first write and
restored on every way out (sensor stale or unavailable, unit switched off or
out of heat, guard switched off, unload, Home Assistant stopping, start-up
after a crash). A switched-off unit refuses writes, so a restore waits for it
to be switched on. A setpoint change made in Home Assistant becomes the new
target; one made elsewhere (app, remote) ends the guard's change at once,
and a fan change made elsewhere wins too. Too many guard writes for one unit
in an hour switch the guard off for that unit.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Final

from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util.unit_conversion import TemperatureConverter

from .api.codecs import FanSpeed, OpMode
from .api.transport import AirstageError
from .compressor import CompressorMonitor
from .const import (
    CONF_ROOM_SENSOR,
    DOMAIN,
    GUARD_DEFAULT_BAND,
    GUARD_DEFAULT_FAN,
    GUARD_DEFAULT_PAUSE_DROP,
    GUARD_DEFAULT_RUN_RAISE,
    GUARD_FAN_LEAVE,
    GUARD_TICK,
    OPT_GUARD,
    OPT_GUARD_BAND,
    OPT_GUARD_FAN,
    OPT_GUARD_PAUSE_DROP,
    OPT_GUARD_RUN_RAISE,
    OPT_GUARD_THERMOSTAT,
)
from .coordinator import UnitCoordinator

_LOGGER = logging.getLogger(__name__)

STORE_VERSION: Final = 1
STALE_S: Final = 300.0  # a room reading older than this hands control back
MIN_CHANGE_S: Final = 300.0  # between two phase changes of one unit
MAX_WRITES_PER_HOUR: Final = 20  # runaway protection, e.g. fighting an automation
RECORDS_KEPT: Final = 30
RUN, PAUSE = "run", "pause"
FAN_OPTIONS: Final = {
    "quiet": FanSpeed.QUIET,
    "low": FanSpeed.LOW,
    "low_medium": FanSpeed.LOW_MEDIUM,
    "medium": FanSpeed.MEDIUM,
    "medium_high": FanSpeed.MEDIUM_HIGH,
    "high": FanSpeed.HIGH,
}


@dataclass
class Stored:
    """What survives a restart, per unit."""

    user_setpoint: float | None = None  # set while the guard controls the setpoint
    guard_setpoint: float | None = None  # what the guard wrote
    phase: str | None = None  # RUN or PAUSE
    fan_set: bool = False  # the guard set the fan (Auto goes back when it stops)
    fan_floor: int | None = None  # the speed the guard set (to know it is still ours)
    restore_pending: float | None = None  # a setpoint to put back when the unit allows
    records: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Phase:
    started: float
    room_at_start: float
    starts_at_start: int | None  # compressor starts counted so far (power sensor)


class UnitGuard:
    """The guard for one indoor unit."""

    def __init__(self, system: CycleGuard, unit: UnitCoordinator, room_sensor: str | None) -> None:
        self.system = system
        self.unit = unit
        self.device = unit.device
        self.room_sensor = room_sensor
        self.stored = Stored()
        self.current: Phase | None = None
        self.stopped_reason: str | None = None  # the guard gave up on this unit
        self._expect: dict[str, Any] = {}  # values the guard wrote and awaits
        self._writes: deque[float] = deque()
        self._last_change: float | None = None

    # -- state ---------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self.unit.subentry.title

    @property
    def heating(self) -> bool:
        return self.device.value("iu_onoff") is True and self.device.value("iu_op_mode") is (
            OpMode.HEAT
        )

    @property
    def active(self) -> bool:
        """The guard controls the unit's setpoint (the user's is stored)."""
        return self.stored.user_setpoint is not None

    def state(self) -> str:
        if self.stopped_reason:
            return "stopped"
        if self.active:
            return self.stored.phase or RUN
        if self.stored.fan_set:
            return "fan_floor"
        return "watching" if self.heating else "idle"

    def attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {"guard": self.state()}
        if self.stopped_reason:
            attrs["guard_stopped_because"] = self.stopped_reason
        if self.active:
            attrs["guard_user_setpoint"] = self.stored.user_setpoint
            attrs["guard_unit_setpoint"] = self.setpoint()
        if self.system.thermostat and not self.room_sensor:
            attrs["guard_room_thermostat"] = "needs a room sensor"
        if self.stored.records:
            attrs["guard_last_phase"] = self.stored.records[-1]
        return attrs

    def room(self) -> float | None:
        """The room sensor in deg C, or None when it is missing or stale."""
        if not self.room_sensor:
            return None
        state = self.system.hass.states.get(self.room_sensor)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        if self.system.clock() - state.last_reported.timestamp() > STALE_S:
            return None
        try:
            value = float(state.state)
        except ValueError:
            return None
        if state.attributes.get("unit_of_measurement") == UnitOfTemperature.FAHRENHEIT:
            value = TemperatureConverter.convert(
                value, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS
            )
        return value

    def setpoint(self) -> float | None:
        value = self.device.value("iu_set_tmp")
        return value if isinstance(value, float) else None

    # -- reacting to the unit ------------------------------------------------------------

    @callback
    def changed(self, names: set[str]) -> None:
        """Values changed on the unit (a read, a push, or the echo of a write)."""
        if "iu_set_tmp" in names:
            self._setpoint_changed()
        if "iu_fan_spd" in names:
            self._fan_changed()
        if names & {"iu_onoff", "iu_op_mode", "iu_fan_spd", "iu_set_tmp"}:
            self.evaluate()

    def _ours(self, name: str) -> bool:
        """Was the new value of `name` the guard's own write? Matched on value."""
        if name in self._expect and self.device.value(name) == self._expect[name]:
            del self._expect[name]
            return True
        return False

    def _setpoint_changed(self) -> None:
        if self._ours("iu_set_tmp"):
            return
        if self.stored.restore_pending is not None:
            self.stored.restore_pending = None  # the user set a setpoint: theirs wins
            self.system.save()
        if self.active and self.setpoint() != self.stored.guard_setpoint:
            _LOGGER.debug("%s: setpoint changed by someone else: hands off", self.name)
            self._end("setpoint changed elsewhere", restore=False)

    def _fan_changed(self) -> None:
        if self._ours("iu_fan_spd"):
            return
        if self.stored.fan_set and self.device.value("iu_fan_spd") != self.stored.fan_floor:
            self.stored.fan_set = False  # the user chose another speed
            self.system.save()

    # -- the decisions -------------------------------------------------------------------

    def _thermostat_applies(self) -> bool:
        return self.system.thermostat and bool(self.room_sensor)

    @callback
    def evaluate(self) -> None:
        if self.stopped_reason:
            return
        on, mode = self.device.value("iu_onoff"), self.device.value("iu_op_mode")
        pending = self.stored.restore_pending
        if pending is not None and on is True and "iu_set_tmp" not in self._expect:
            self.stored.restore_pending = None
            self.system.save()
            if self._thermostat_applies() and self.heating and self.room() is not None:
                self._take_over(pending)  # back on: the guard continues with that target
            elif self.setpoint() != pending:
                self._write_setpoint(pending, "restore after an earlier failed write", restore=True)
        self._thermostat()
        self._fan_floor(on, mode)

    def _fan_floor(self, on: Any, mode: Any) -> None:
        floor = self.system.fan
        fan = self.device.value("iu_fan_spd")
        if floor is None or "iu_fan_spd" in self._expect:
            return
        if self.heating and fan is FanSpeed.AUTO:
            self.stored.fan_set, self.stored.fan_floor = True, int(floor)
            self.system.save()
            self._write_fan(floor, "fan floor")
        elif (
            on is True
            and isinstance(mode, OpMode)
            and mode is not OpMode.HEAT
            and self.stored.fan_set
            and fan == self.stored.fan_floor
        ):
            self.stored.fan_set = False
            self.system.save()
            self._write_fan(FanSpeed.AUTO, "left heat: fan back to Auto")

    def _thermostat(self) -> None:
        if not self._thermostat_applies():
            return
        if self.active:
            if not self.heating:
                self._end("unit off or not heating")
                return
            room = self.room()
            if room is None:
                self._end("room sensor unavailable or stale")
                return
            target = self.stored.user_setpoint
            assert target is not None
            band = self.system.band
            if not self._may_change():
                return
            if self.stored.phase == RUN and room >= target + band:
                self._switch(PAUSE, room, "room reached the top of the band")
            elif self.stored.phase == PAUSE and room <= target - band:
                self._switch(RUN, room, "room reached the bottom of the band")
            return
        if not self.heating or self.stored.restore_pending is not None:
            return
        if "iu_set_tmp" in self._expect or not self._may_change():
            return  # the current setpoint may be the guard's own, or too soon
        setpoint = self.setpoint()
        if setpoint is not None and self.room() is not None:
            self._take_over(setpoint)

    def _take_over(self, target: float) -> None:
        """Start controlling the setpoint, with `target` as the user's."""
        room = self.room()
        if room is None:
            return
        phase = PAUSE if room >= target + self.system.band else RUN
        self.stored.user_setpoint = target
        self._enter(phase, room, "start")

    def _switch(self, phase: str, room: float, why: str) -> None:
        self._close_record(why, room)
        self._enter(phase, room, why)

    def _enter(self, phase: str, room: float, why: str) -> None:
        target = self.stored.user_setpoint
        assert target is not None
        unit_setpoint = self._phase_setpoint(target, phase)
        self.stored.phase, self.stored.guard_setpoint = phase, unit_setpoint
        self.system.save()  # stored before the write
        compressor = self.system.compressor
        now = self.system.clock()
        self.current = Phase(now, room, compressor.tracker.starts if compressor else None)
        self._last_change = now
        _LOGGER.debug("%s: %s (%s), room %.2f C, target %.1f: setpoint %.1f", self.name,
                      phase, why, room, target, unit_setpoint)  # fmt: skip
        if self.setpoint() != unit_setpoint:
            self._write_setpoint(unit_setpoint, f"{phase}: {why}")

    def _phase_setpoint(self, target: float, phase: str) -> float:
        lo, hi = self._limits()
        if phase == RUN:
            return min(hi, target + self.system.run_raise)
        return max(lo, target - self.system.pause_drop)

    def _may_change(self) -> bool:
        last = self._last_change
        return last is None or self.system.clock() - last >= MIN_CHANGE_S

    def user_target(self, celsius: float) -> bool:
        """A new setpoint from Home Assistant while the guard is in control: it
        becomes the target, and the unit gets the matching setpoint. Returns
        whether the guard handled it (otherwise the caller writes it)."""
        if not self.active or self.stopped_reason:
            return False
        self.stored.user_setpoint = celsius
        unit_setpoint = self._phase_setpoint(celsius, self.stored.phase or RUN)
        self.stored.guard_setpoint = unit_setpoint
        self.system.save()
        if self.setpoint() != unit_setpoint:
            self._write_setpoint(unit_setpoint, "new target")
        return True

    def _close_record(self, why: str, room: float | None) -> None:
        current = self.current
        if current is None:
            return
        record: dict[str, Any] = {
            "phase": self.stored.phase,
            "ended": why,
            "minutes": round((self.system.clock() - current.started) / 60, 1),
            "room_at_start": round(current.room_at_start, 2),
            "room_at_end": None if room is None else round(room, 2),
        }
        compressor = self.system.compressor
        if compressor is not None and current.starts_at_start is not None:
            record["compressor_starts"] = compressor.tracker.starts - current.starts_at_start
        self.stored.records = [*self.stored.records, record][-RECORDS_KEPT:]
        self.current = None

    def _end(self, reason: str, *, restore: bool = True) -> None:
        user = self.stored.user_setpoint
        self._close_record(reason, self.room())
        self._last_change = self.system.clock()
        self.stored.user_setpoint = self.stored.guard_setpoint = self.stored.phase = None
        self.system.save()
        _LOGGER.debug("%s: hands off (%s)", self.name, reason)
        if restore and user is not None and self.setpoint() != user:
            if self.device.value("iu_onoff") is False:
                # A switched-off unit refuses setpoint writes: put it back at power-on.
                self.stored.restore_pending = user
                self.system.save()
            else:
                self._write_setpoint(user, f"hands off: {reason}", restore=True)

    def _limits(self) -> tuple[float, float]:
        caps = self.device.capabilities
        if caps is not None and OpMode.HEAT in caps.limits:
            return caps.limits[OpMode.HEAT]
        return 16.0, 30.0

    # -- writing ---------------------------------------------------------------------------

    def _allowed(self) -> bool:
        now = self.system.clock()
        while self._writes and self._writes[0] <= now - 3600:
            self._writes.popleft()
        if len(self._writes) >= MAX_WRITES_PER_HOUR:
            _LOGGER.warning(
                "%s: the short-cycle guard wrote %d times within an hour; it stops for this "
                "unit until the integration is reloaded",
                self.name, len(self._writes),
            )  # fmt: skip
            self.stopped_reason = "too many writes in an hour"
            user = self.stored.user_setpoint
            self.stored.user_setpoint = self.stored.guard_setpoint = self.stored.phase = None
            self.current = None
            self.system.save()
            if user is not None:
                self._schedule(self.device.set_setpoint(user), "iu_set_tmp", user, "give up",
                               restore=True)  # fmt: skip
            return False
        self._writes.append(now)
        return True

    def _write_fan(self, speed: FanSpeed, why: str) -> None:
        if self._allowed():
            self._schedule(self.device.set_fan_speed(speed), "iu_fan_spd", speed, why)

    def _write_setpoint(self, celsius: float, why: str, *, restore: bool = False) -> None:
        if self._allowed():
            self._schedule(
                self.device.set_setpoint(celsius), "iu_set_tmp", celsius, why, restore=restore
            )

    def _schedule(
        self, write: Any, name: str, value: Any, why: str, *, restore: bool = False
    ) -> None:
        self._expect[name] = value

        async def run() -> None:
            try:
                await write
                _LOGGER.debug("%s: %s=%s (%s)", self.name, name, value, why)
            except (AirstageError, ValueError) as err:
                self._expect.pop(name, None)
                _LOGGER.warning("%s: the guard could not write %s=%s (%s): %s",
                                self.name, name, value, why, err)  # fmt: skip
                if restore:  # e.g. the unit is off: put it back when it is on again
                    self.stored.restore_pending = value
                    self.system.save()
                elif name == "iu_set_tmp" and self.active:
                    # The unit did not take it: the guard lets go (nothing to undo
                    # if this was its first write; otherwise put the user's back).
                    self._end("the unit refused the setpoint")
            self.system.notify()

        self.system.entry_task(run(), f"{DOMAIN} guard {self.name} {name}")

    async def async_restore(self, reason: str) -> None:
        """Undo the guard's changes now (unload, guard off, Home Assistant stopping)."""
        user = self.stored.user_setpoint or self.stored.restore_pending
        self.current = None
        self.stored.user_setpoint = self.stored.guard_setpoint = self.stored.phase = None
        self.stored.restore_pending = None
        if user is not None and self.setpoint() == user:
            user = None
        if self.device.value("iu_onoff") is False:
            # A switched-off unit refuses writes: both go back when it is switched on.
            self.stored.restore_pending = user
            return
        try:
            if user is not None:
                await self.device.set_setpoint(user)
            fan = self.device.value("iu_fan_spd")
            if self.stored.fan_set and fan is not None and fan == self.stored.fan_floor:
                await self.device.set_fan_speed(FanSpeed.AUTO)
            self.stored.fan_set, self.stored.fan_floor = False, None
        except (AirstageError, ValueError) as err:
            _LOGGER.warning("%s: could not undo the guard's changes (%s): %s",
                            self.name, reason, err)  # fmt: skip
            if user is not None:
                self.stored.restore_pending = user  # retried later or at the next start-up


class CycleGuard:
    """The guard for one system (config entry)."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        units: dict[str, UnitCoordinator],
        compressor: CompressorMonitor | None,
        options: dict[str, Any],
        create_task: Callable[[Any, str], Any],
    ) -> None:
        self.hass = hass
        self.compressor = compressor  # optional: only for the records
        self.entry_task = create_task
        self.enabled = bool(options.get(OPT_GUARD, False))
        fan = options.get(OPT_GUARD_FAN, GUARD_DEFAULT_FAN)
        self.fan: FanSpeed | None = None if fan == GUARD_FAN_LEAVE else FAN_OPTIONS.get(fan)
        self.thermostat = bool(options.get(OPT_GUARD_THERMOSTAT, True))
        self.run_raise = float(options.get(OPT_GUARD_RUN_RAISE, GUARD_DEFAULT_RUN_RAISE))
        self.pause_drop = float(options.get(OPT_GUARD_PAUSE_DROP, GUARD_DEFAULT_PAUSE_DROP))
        self.band = float(options.get(OPT_GUARD_BAND, GUARD_DEFAULT_BAND))
        self.units = {
            sub_id: UnitGuard(self, unit, unit.subentry.data.get(CONF_ROOM_SENSOR) or None)
            for sub_id, unit in units.items()
        }
        self.clock: Callable[[], float] = time.time  # tests replace it
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry_id}.guard"
        )
        self._listeners: list[Callable[[], None]] = []
        self._undo: list[Callable[[], None]] = []
        self._stop_undo: Callable[[], None] | None = None

    async def async_start(self) -> None:
        """Load what a previous run left (undoing a change it could not end), then,
        if the guard is on, start watching."""
        stored = await self._store.async_load() or {}
        if not self.enabled and not stored:
            return  # never used: nothing to undo, nothing to write
        for sub_id, unit in self.units.items():
            data = stored.get(sub_id) or {}
            unit.stored = Stored(**{k: v for k, v in data.items() if k in Stored.__annotations__})
            leftover = unit.active or unit.stored.restore_pending is not None
            if leftover or (not self.enabled and unit.stored.fan_set):
                await unit.async_restore("start-up")
        await self._async_save()
        if not self.enabled:
            # Switched off with something still to undo on a unit that was off:
            # undo it when that unit is switched on.
            for unit in self.units.values():
                if unit.stored.restore_pending is not None or unit.stored.fan_set:
                    self._undo.append(unit.device.add_listener(self._retry_listener(unit)))
            return
        for unit in self.units.values():
            self._undo.append(unit.device.add_listener(unit.changed))
        rooms = {u.room_sensor: u for u in self.units.values() if u.room_sensor}
        if rooms and self.thermostat:
            self._undo.append(
                async_track_state_change_event(self.hass, list(rooms), self._room_changed)
            )
        self._undo.append(async_track_time_interval(self.hass, self._tick, GUARD_TICK))
        self._stop_undo = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._stop)
        for unit in self.units.values():
            unit.evaluate()

    def _retry_listener(self, unit: UnitGuard) -> Callable[[set[str]], None]:
        @callback
        def changed(names: set[str]) -> None:
            pending = unit.stored.restore_pending is not None or unit.stored.fan_set
            if "iu_onoff" in names and unit.device.value("iu_onoff") is True and pending:
                self.entry_task(self._retry(unit), f"{DOMAIN} guard {unit.name} undo")

        return changed

    async def _retry(self, unit: UnitGuard) -> None:
        await unit.async_restore("switched on")
        await self._async_save()

    def _detach(self) -> None:
        """Stop reacting to the units, so restoring cannot trigger the guard again."""
        for undo in self._undo:
            undo()
        self._undo.clear()

    async def async_stop(self) -> None:
        self._detach()
        if self._stop_undo is not None:
            self._stop_undo()
            self._stop_undo = None
        if not self.enabled and not any(
            u.active or u.stored.fan_set or u.stored.restore_pending is not None
            for u in self.units.values()
        ):
            return  # never used: nothing to undo, nothing to write
        for unit in self.units.values():
            await unit.async_restore("unload")
        await self._async_save()

    async def _stop(self, _event: Event) -> None:
        self._stop_undo = None  # a once-listener removes itself
        self._detach()
        for unit in self.units.values():
            await unit.async_restore("Home Assistant stopping")
        await self._async_save()

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    def save(self) -> None:
        self._store.async_delay_save(self._data, 1)
        self.notify()

    async def _async_save(self) -> None:
        await self._store.async_save(self._data())

    def _data(self) -> dict[str, Any]:
        return {sub_id: asdict(unit.stored) for sub_id, unit in self.units.items()}

    @callback
    def _room_changed(self, event: Event[EventStateChangedData]) -> None:
        for unit in self.units.values():
            if unit.room_sensor == event.data["entity_id"]:
                unit.evaluate()
        self.notify()

    @callback
    def _tick(self, _now: Any) -> None:
        for unit in self.units.values():
            unit.evaluate()
        self.notify()
