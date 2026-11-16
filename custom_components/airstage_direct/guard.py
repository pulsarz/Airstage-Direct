"""Short-cycle guard (experimental, off unless switched on in the system settings).

Measured on a short-cycling unit (docs/measurements.md, 2026-11-16): on Auto
the indoor fan runs slowly at low load, the intake reads the unit's own warm
air, and the compressor stops and restarts every few minutes; every restart
is a burst. Two levers, each optional:

- **Fan floor.** While a unit heats with its fan on Auto, the guard sets a
  fixed speed instead (Low-medium by default). Any other fan choice is left
  alone. When the guard stops, or the unit leaves heat, a fan the guard set
  goes back to Auto.
- **Restart delay.** Needs a power sensor (to see the compressor stop) and a
  room temperature sensor for the unit. After a stop, a unit whose room is
  really warm (at or above its setpoint) gets its setpoint lowered
  a little, so the unit does not restart at the first moment it may. When the
  room has cooled to the setpoint minus the band, the setpoint goes back and
  the unit starts with real demand. A cold room is never held.

Fail-safe rules: the user's setpoint is stored before the first write and
restored on every way out (room cooled, sensor stale or unavailable, maximum
hold time, unit switched off or out of heat, guard switched off, unload,
Home Assistant stopping, start-up after a crash). A setpoint or fan change
that is not the guard's (app, remote, Home Assistant) wins at once. Too many
guard writes for one unit in an hour switch the guard off for that unit.
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
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
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
    GUARD_DEFAULT_HOLD_OFFSET,
    GUARD_DEFAULT_MAX_HOLD,
    GUARD_FAN_LEAVE,
    GUARD_TICK,
    OPT_GUARD,
    OPT_GUARD_BAND,
    OPT_GUARD_FAN,
    OPT_GUARD_HOLD,
    OPT_GUARD_HOLD_OFFSET,
    OPT_GUARD_MAX_HOLD,
)
from .coordinator import UnitCoordinator

_LOGGER = logging.getLogger(__name__)

STORE_VERSION: Final = 1
STALE_S: Final = 300.0  # a room reading older than this ends a hold
MAX_WRITES_PER_HOUR: Final = 20  # runaway protection, e.g. fighting an automation
OUTCOMES_KEPT: Final = 20
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

    user_setpoint: float | None = None  # set while a hold is active
    hold_setpoint: float | None = None  # what the guard wrote
    fan_set: bool = False  # the guard set the fan (Auto goes back when it stops)
    restore_pending: float | None = None  # a setpoint to put back when the unit allows
    fan_floor: int | None = None  # the speed the guard set (to know it is still ours)
    outcomes: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Hold:
    started: float
    room_at_start: float
    stopped: float  # when the compressor stopped (the pause began)
    pause_ended: float | None = None  # when the compressor started again


class UnitGuard:
    """The guard for one indoor unit."""

    def __init__(self, system: CycleGuard, unit: UnitCoordinator, room_sensor: str | None) -> None:
        self.system = system
        self.unit = unit
        self.device = unit.device
        self.room_sensor = room_sensor
        self.stored = Stored()
        self.hold: Hold | None = None
        self.stopped_reason: str | None = None  # the guard gave up on this unit
        self._expect: dict[str, Any] = {}  # values the guard wrote and awaits
        self._writes: deque[float] = deque()
        self._last_release: dict[str, Any] | None = None  # waiting for the next run's length

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
    def holding(self) -> bool:
        return self.stored.user_setpoint is not None

    def state(self) -> str:
        if self.stopped_reason:
            return "stopped"
        if self.holding:
            return "holding"
        if self.stored.fan_set:
            return "fan_floor"
        return "watching" if self.heating else "idle"

    def attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {"guard": self.state()}
        if self.stopped_reason:
            attrs["guard_stopped_because"] = self.stopped_reason
        if self.holding:
            attrs["guard_user_setpoint"] = self.stored.user_setpoint
        if self.system.hold_enabled and not self.can_hold():
            attrs["guard_restart_delay"] = self.hold_prerequisite()
        if self.stored.outcomes:
            attrs["guard_last_hold"] = self.stored.outcomes[-1]
        return attrs

    def hold_prerequisite(self) -> str:
        if self.system.compressor is None:
            return "needs a power sensor"
        if not self.room_sensor:
            return "needs a room sensor"
        return "ok"

    def can_hold(self) -> bool:
        return self.system.hold_enabled and self.hold_prerequisite() == "ok"

    def room(self) -> float | None:
        """The room sensor in deg C, or None when it is missing or stale."""
        if not self.room_sensor:
            return None
        state = self.system.hass.states.get(self.room_sensor)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        reported = state.last_reported.timestamp()
        if self.system.clock() - reported > STALE_S:
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
        if names & {"iu_onoff", "iu_op_mode", "iu_fan_spd"}:
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
        value = self.setpoint()
        if self.stored.restore_pending is not None:
            self.stored.restore_pending = None  # the user set a setpoint: theirs wins
            self.system.save()
        if self.holding and value != self.stored.hold_setpoint:
            _LOGGER.debug("%s: setpoint changed to %s by someone else: hold ends", self.name, value)
            self._end_hold("setpoint changed by the user", restore=False)

    def _fan_changed(self) -> None:
        if self._ours("iu_fan_spd"):
            return
        if self.stored.fan_set and self.device.value("iu_fan_spd") != self.system.fan:
            self.stored.fan_set = False  # the user chose another speed
            self.system.save()

    # -- the decisions -------------------------------------------------------------------

    @callback
    def evaluate(self) -> None:
        if self.stopped_reason:
            return
        on, mode = self.device.value("iu_onoff"), self.device.value("iu_op_mode")
        fan = self.device.value("iu_fan_spd")
        if self.holding:
            self._check_hold()
        pending = self.stored.restore_pending
        if pending is not None and on is True and "iu_set_tmp" not in self._expect:
            self.stored.restore_pending = None
            self.system.save()
            if self.setpoint() != pending:
                self._write_setpoint(pending, "restore after an earlier failed write", restore=True)
        floor = self.system.fan
        if floor is None:
            return
        if self.heating and fan is FanSpeed.AUTO and "iu_fan_spd" not in self._expect:
            self.stored.fan_set, self.stored.fan_floor = True, int(floor)
            self.system.save()
            self._write_fan(floor, "fan floor")
        elif (
            on is True
            and isinstance(mode, OpMode)
            and mode is not OpMode.HEAT
            and self.stored.fan_set
            and fan == floor
        ):
            self.stored.fan_set = False
            self.system.save()
            self._write_fan(FanSpeed.AUTO, "left heat: fan back to Auto")

    def compressor_stopped(self, at: float) -> None:
        """The compressor stopped (confirmed after the shortest real pause)."""
        if not self.can_hold() or self.holding or self.stopped_reason or not self.heating:
            return
        if self.stored.restore_pending is not None or "iu_set_tmp" in self._expect:
            return  # the current setpoint may be the guard's own, not the user's
        compressor = self.system.compressor
        if compressor is not None and compressor.tracker.running:
            return  # already running again: too late for this pause
        room, setpoint = self.room(), self.setpoint()
        if room is None or setpoint is None:
            return
        if room < setpoint:
            # Below the setpoint the room needs heat: a stop there is the unit's
            # own sensor reading too warm (measured: room 21.8 C at setpoint 22.0
            # while the corrected intake read 23.25 C), not a warm room.
            return
        lo = self._lowest()
        target = max(lo, setpoint - self.system.hold_offset)
        if target >= setpoint:
            return
        self.stored.user_setpoint, self.stored.hold_setpoint = setpoint, target
        self.system.save()  # stored before the write
        self.hold = Hold(started=self.system.clock(), room_at_start=room, stopped=at)
        _LOGGER.debug(
            "%s: compressor stopped with the room at %.2f C: setpoint %.1f -> %.1f",
            self.name, room, setpoint, target,
        )  # fmt: skip
        self._write_setpoint(target, "hold")

    def compressor_started(self, at: float) -> None:
        if self.hold is not None and self.hold.pause_ended is None:
            self.hold.pause_ended = at
        record = self._last_release
        if record is not None and "run_started" not in record:
            record["run_started"] = at
            if record.get("pause_minutes") is None and "pause_began" in record:
                record["pause_minutes"] = round((at - record.pop("pause_began")) / 60, 1)
                self.system.save()

    def compressor_stopped_for_outcome(self, at: float) -> None:
        """Completes the last release's record with the length of the run after it."""
        record = self._last_release
        if record is not None and "run_started" in record:
            record["next_run_minutes"] = round((at - record.pop("run_started")) / 60, 1)
            self._last_release = None
            self.system.save()

    def _check_hold(self) -> None:
        if not self.heating:
            self._end_hold("unit off or not heating")
            return
        room = self.room()
        if room is None:
            self._end_hold("room sensor unavailable or stale")
            return
        user = self.stored.user_setpoint
        assert user is not None
        if room <= user - self.system.band:
            self._end_hold("room cooled")
            return
        started = self.hold.started if self.hold else self.system.clock()
        if self.system.clock() - started >= self.system.max_hold_s:
            self._end_hold("maximum hold time")

    def end_hold_for_user(self) -> None:
        """The user sets a new setpoint (from Home Assistant): the hold ends without
        restoring, since their write follows."""
        self._end_hold("setpoint changed by the user", restore=False)

    def _end_hold(self, reason: str, *, restore: bool = True) -> None:
        user = self.stored.user_setpoint
        hold = self.hold
        now = self.system.clock()
        if hold is not None:
            record: dict[str, Any] = {
                "ended": reason,
                "held_minutes": round((now - hold.started) / 60, 1),
                "room_at_start": round(hold.room_at_start, 2),
                "room_at_end": None if (room := self.room()) is None else round(room, 2),
                "pause_minutes": None,
            }
            if hold.pause_ended is not None:
                record["pause_minutes"] = round((hold.pause_ended - hold.stopped) / 60, 1)
            else:
                record["pause_began"] = hold.stopped  # filled in at the next start
            self.stored.outcomes = [*self.stored.outcomes, record][-OUTCOMES_KEPT:]
            self._last_release = record
        self.hold = None
        self.stored.user_setpoint = self.stored.hold_setpoint = None
        self.system.save()
        _LOGGER.debug("%s: hold ends (%s)", self.name, reason)
        if restore and user is not None and self.setpoint() != user:
            if self.device.value("iu_onoff") is False:
                # A switched-off unit refuses setpoint writes: put it back at power-on.
                self.stored.restore_pending = user
                self.system.save()
            else:
                self._write_setpoint(user, f"hold ends: {reason}", restore=True)

    def _lowest(self) -> float:
        caps = self.device.capabilities
        if caps is not None and OpMode.HEAT in caps.limits:
            return caps.limits[OpMode.HEAT][0]
        return 16.0

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
            self.stored.user_setpoint = self.stored.hold_setpoint = None
            self.hold = None
            self.system.save()
            if user is not None:
                self._schedule(self.device.set_setpoint(user), "iu_set_tmp", user, "give up")
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
            self.system.notify()

        self.system.entry_task(run(), f"{DOMAIN} guard {self.name} {name}")

    async def async_restore(self, reason: str) -> None:
        """Undo the guard's changes now (unload, guard off, Home Assistant stopping)."""
        user = self.stored.user_setpoint or self.stored.restore_pending
        self.hold = None
        self.stored.user_setpoint = self.stored.hold_setpoint = None
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
        self.compressor = compressor
        self.entry_task = create_task
        self.enabled = bool(options.get(OPT_GUARD, False))
        fan = options.get(OPT_GUARD_FAN, GUARD_DEFAULT_FAN)
        self.fan: FanSpeed | None = None if fan == GUARD_FAN_LEAVE else FAN_OPTIONS.get(fan)
        self.hold_enabled = bool(options.get(OPT_GUARD_HOLD, True))
        self.hold_offset = float(options.get(OPT_GUARD_HOLD_OFFSET, GUARD_DEFAULT_HOLD_OFFSET))
        self.band = float(options.get(OPT_GUARD_BAND, GUARD_DEFAULT_BAND))
        self.max_hold_s = float(options.get(OPT_GUARD_MAX_HOLD, GUARD_DEFAULT_MAX_HOLD)) * 60
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
        """Load what a previous run left (undoing a hold it could not end), then,
        if the guard is on, start watching."""
        stored = await self._store.async_load() or {}
        if not self.enabled and not stored:
            return  # never used: nothing to undo, nothing to write
        for sub_id, unit in self.units.items():
            data = stored.get(sub_id) or {}
            unit.stored = Stored(**{k: v for k, v in data.items() if k in Stored.__annotations__})
            leftover = unit.holding or unit.stored.restore_pending is not None
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
        if self.compressor is not None:
            self._undo.append(self.compressor.add_event_listener(self._compressor_event))
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
            u.holding or u.stored.fan_set or u.stored.restore_pending is not None
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
    def _compressor_event(self, kind: str, at: float) -> None:
        for unit in self.units.values():
            if kind == "stop":
                unit.compressor_stopped_for_outcome(at)
                unit.compressor_stopped(at)
            else:
                unit.compressor_started(at)
        self.notify()

    @callback
    def _tick(self, _now: Any) -> None:
        for unit in self.units.values():
            unit.evaluate()
        self.notify()
