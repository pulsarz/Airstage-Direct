"""The short-cycle guard (experimental): fan floor, restart delay, and the
fail-safe rules. With the guard off, it never writes."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.airstage_direct import guard as guard_module
from custom_components.airstage_direct.const import (
    CONF_CAPABILITIES,
    CONF_ROOM_SENSOR,
    DOMAIN,
    OPT_GUARD,
    OPT_GUARD_FAN,
    OPT_GUARD_HOLD,
    OPT_POWER_SENSOR,
)
from tests.integration.conftest import unit_subentry
from tests.simulator import Simulator

POWER = "sensor.airco_power"
ROOM = "sensor.living_room_temperature"
CLIMATE = "climate.living_room"
GUARD_ON = {OPT_GUARD: True}
FULL = {OPT_GUARD: True, OPT_POWER_SENSOR: POWER}


def writes(sim: Simulator) -> list[dict[str, str]]:
    return [r["body"]["value"] for r in sim.requests if r["endpoint"] == "SetParam"]


def state_of(hass: HomeAssistant, entity_id: str) -> State:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state


@pytest.fixture(autouse=True)
def _never_stale(monkeypatch: pytest.MonkeyPatch, sim: Simulator) -> None:
    """Test clocks run ahead of the room sensor's real timestamps; and like the
    real units, a switched-off unit refuses setpoint and fan writes."""
    monkeypatch.setattr(guard_module, "STALE_S", 1e9)
    sim.refuse_when_off = True


async def setup(
    hass: HomeAssistant,
    sim: Simulator,
    capabilities: dict[str, Any],
    options: dict[str, Any] | None = None,
    *,
    fan: str = "0",
    room_sensor: bool = True,
) -> MockConfigEntry:
    sim.indoor["iu_fan_spd"] = fan
    extra: dict[str, Any] = {CONF_CAPABILITIES: capabilities}
    if room_sensor:
        extra[CONF_ROOM_SENSOR] = ROOM
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Airco",
        data={},
        options=options or {},
        subentries_data=[unit_subentry(sim, extra=extra)],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def room(hass: HomeAssistant, celsius: float) -> None:
    hass.states.async_set(ROOM, str(celsius), {"unit_of_measurement": "°C"})


async def test_off_by_default_and_never_writes(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    room(hass, 22.4)
    entry = await setup(hass, sim, capabilities)
    guard = entry.runtime_data.guard
    assert guard is not None
    assert not guard.enabled
    assert writes(sim) == []
    assert "guard" not in state_of(hass, CLIMATE).attributes
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert writes(sim) == []


async def test_fan_floor_replaces_auto_while_heating(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    entry = await setup(hass, sim, capabilities, GUARD_ON)
    assert writes(sim) == [{"iu_fan_spd": "7"}]  # Auto -> Low-medium
    assert state_of(hass, CLIMATE).attributes["fan_mode"] == "low_medium"
    assert state_of(hass, CLIMATE).attributes["guard"] == "fan_floor"
    # Unloading (or switching the guard off) puts Auto back.
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert writes(sim)[-1] == {"iu_fan_spd": "0"}


async def test_fan_floor_leaves_other_choices_alone(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    entry = await setup(hass, sim, capabilities, GUARD_ON, fan="8")  # Medium, the user's
    assert writes(sim) == []
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert writes(sim) == []  # never set by the guard: nothing to undo


async def test_a_user_fan_change_wins(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    entry = await setup(hass, sim, capabilities, GUARD_ON)
    (unit,) = entry.runtime_data.units.values()
    sim.indoor["iu_fan_spd"] = "11"
    unit.device.apply({"iu_fan_spd": "11"})  # High, from the remote (a push)
    await hass.async_block_till_done()
    assert entry.runtime_data.guard is not None
    (unit_guard,) = entry.runtime_data.guard.units.values()
    assert unit_guard.stored.fan_set is False
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert writes(sim) == [{"iu_fan_spd": "7"}]  # High stays


async def test_leave_the_fan_alone(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    await setup(hass, sim, capabilities, {**GUARD_ON, OPT_GUARD_FAN: "leave"})
    assert writes(sim) == []


# -- the restart delay ---------------------------------------------------------------


class Rig:
    """Drives the power sensor and both clocks."""

    def __init__(self, hass: HomeAssistant, entry: MockConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.now = time.time() + 1
        compressor = entry.runtime_data.compressor
        guard = entry.runtime_data.guard
        assert compressor is not None
        assert guard is not None
        self.compressor, self.guard = compressor, guard
        compressor.clock = guard.clock = lambda: self.now
        (self.unit_guard,) = guard.units.values()

    async def power(self, seconds: float, watts: float, steps: int = 1) -> None:
        for _ in range(steps):
            self.now += seconds / steps
            self.hass.states.async_set(POWER, str(watts), {"unit_of_measurement": "W"})
            self.compressor._tick(None)
            await self.hass.async_block_till_done()

    async def tick(self) -> None:
        self.guard._tick(None)
        await self.hass.async_block_till_done()

    async def run_then_stop(self) -> None:
        await self.power(30, 10, 3)  # standby: the first state
        await self.power(30, 600, 3)  # a start
        await self.power(600, 450, 10)  # running near the minimum
        await self.power(150, 10, 10)  # stopped long enough to count


Setup = Callable[..., Awaitable[MockConfigEntry]]


async def full_setup(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any], **kw: Any
) -> Rig:
    hass.states.async_set(POWER, "10", {"unit_of_measurement": "W"})
    entry = await setup(hass, sim, capabilities, FULL, fan="7", **kw)
    return Rig(hass, entry)


async def test_a_warm_room_delays_the_restart(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    room(hass, 22.3)  # setpoint 22.0
    rig = await full_setup(hass, sim, capabilities)
    await rig.run_then_stop()
    assert writes(sim) == [{"iu_set_tmp": "215"}]  # 22.0 -> 21.5
    climate = state_of(hass, CLIMATE)
    assert climate.attributes["temperature"] == 22.0  # still shows the user's setpoint
    assert climate.attributes["guard"] == "holding"
    room(hass, 21.8)  # not yet 0.3 below
    await rig.tick()
    assert len(writes(sim)) == 1
    room(hass, 21.7)  # cooled: the setpoint goes back
    await rig.tick()
    assert writes(sim)[-1] == {"iu_set_tmp": "220"}
    outcome = state_of(hass, CLIMATE).attributes["guard_last_hold"]
    assert outcome["ended"] == "room cooled"
    assert outcome["room_at_start"] == 22.3


async def test_a_cold_room_is_never_held(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    room(hass, 21.5)
    rig = await full_setup(hass, sim, capabilities)
    await rig.run_then_stop()
    assert writes(sim) == []


async def test_no_room_sensor_no_hold(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    rig = await full_setup(hass, sim, capabilities, room_sensor=False)
    await rig.run_then_stop()
    assert writes(sim) == []
    assert state_of(hass, CLIMATE).attributes["guard_restart_delay"] == "needs a room sensor"


async def test_hold_can_be_switched_off(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    room(hass, 22.3)
    hass.states.async_set(POWER, "10", {"unit_of_measurement": "W"})
    entry = await setup(hass, sim, capabilities, {**FULL, OPT_GUARD_HOLD: False}, fan="7")
    await Rig(hass, entry).run_then_stop()
    assert writes(sim) == []


async def test_a_user_setpoint_change_ends_the_hold(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    room(hass, 22.3)
    rig = await full_setup(hass, sim, capabilities)
    await rig.run_then_stop()
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 23}, blocking=True
    )
    assert writes(sim) == [{"iu_set_tmp": "215"}, {"iu_set_tmp": "230"}]
    assert not rig.unit_guard.holding
    room(hass, 21.0)
    await rig.tick()
    assert len(writes(sim)) == 2  # nothing restored over the user's choice


async def test_a_remote_setpoint_change_ends_the_hold(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    room(hass, 22.3)
    rig = await full_setup(hass, sim, capabilities)
    await rig.run_then_stop()
    rig.unit_guard.device.apply({"iu_set_tmp": "210"})  # from the remote, via push
    await hass.async_block_till_done()
    assert not rig.unit_guard.holding
    await rig.tick()
    assert writes(sim) == [{"iu_set_tmp": "215"}]


async def test_switching_the_unit_off_ends_the_hold(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    room(hass, 22.3)
    rig = await full_setup(hass, sim, capabilities)
    await rig.run_then_stop()
    sim.indoor["iu_onoff"] = "0"
    rig.unit_guard.device.apply({"iu_onoff": "0"})
    await hass.async_block_till_done()
    assert writes(sim) == [{"iu_set_tmp": "215"}]  # an off unit refuses: not even tried
    assert not rig.unit_guard.holding
    assert rig.unit_guard.stored.restore_pending == 22.0
    sim.indoor["iu_onoff"] = "1"
    rig.unit_guard.device.apply({"iu_onoff": "1"})  # switched on: put back at once
    await hass.async_block_till_done()
    assert writes(sim)[-1] == {"iu_set_tmp": "220"}
    assert rig.unit_guard.stored.restore_pending is None


async def test_the_longest_delay(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    room(hass, 22.3)
    rig = await full_setup(hass, sim, capabilities)
    await rig.run_then_stop()
    rig.now += 45 * 60
    await rig.tick()
    assert writes(sim)[-1] == {"iu_set_tmp": "220"}


async def test_a_stale_room_sensor_ends_the_hold(
    hass: HomeAssistant,
    sim: Simulator,
    capabilities: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    room(hass, 22.3)
    rig = await full_setup(hass, sim, capabilities)
    await rig.run_then_stop()
    hass.states.async_set(ROOM, "unavailable")
    await rig.tick()
    assert writes(sim)[-1] == {"iu_set_tmp": "220"}


async def test_unload_restores_and_a_crash_is_undone_at_start_up(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    room(hass, 22.3)
    rig = await full_setup(hass, sim, capabilities)
    await rig.run_then_stop()
    assert await hass.config_entries.async_unload(rig.entry.entry_id)
    assert writes(sim)[-1] == {"iu_set_tmp": "220"}
    # A crash in the middle of a hold: the stored hold is undone at the next start.
    sim.indoor["iu_set_tmp"] = "215"
    store: Store[dict[str, Any]] = Store(
        hass, guard_module.STORE_VERSION, f"{DOMAIN}.{rig.entry.entry_id}.guard"
    )
    (sub_id,) = rig.entry.subentries
    await store.async_save({sub_id: {"user_setpoint": 22.0, "hold_setpoint": 21.5}})
    assert await hass.config_entries.async_setup(rig.entry.entry_id)
    await hass.async_block_till_done()
    assert writes(sim)[-1] == {"iu_set_tmp": "220"}


async def test_too_many_writes_stop_the_guard_for_that_unit(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    entry = await setup(hass, sim, capabilities, GUARD_ON)
    assert entry.runtime_data.guard is not None
    (unit_guard,) = entry.runtime_data.guard.units.values()
    (unit,) = entry.runtime_data.units.values()
    for _ in range(25):  # something keeps putting the fan back on Auto
        sim.indoor["iu_fan_spd"] = "0"
        unit.device.apply({"iu_fan_spd": "0"})
        await hass.async_block_till_done()
    assert unit_guard.stopped_reason == "too many writes in an hour"
    assert len(writes(sim)) <= guard_module.MAX_WRITES_PER_HOUR
    assert state_of(hass, CLIMATE).attributes["guard"] == "stopped"


async def test_home_assistant_stopping_restores(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    room(hass, 22.3)
    rig = await full_setup(hass, sim, capabilities)
    await rig.run_then_stop()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert writes(sim)[-1] == {"iu_set_tmp": "220"}
    assert not rig.unit_guard.holding


async def test_stopping_with_the_fan_floor_puts_auto_back_for_good(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    await setup(hass, sim, capabilities, GUARD_ON)  # fan Auto -> floor
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert writes(sim) == [{"iu_fan_spd": "7"}, {"iu_fan_spd": "0"}]  # not floored again


async def test_a_failed_restore_is_retried_and_a_user_change_wins(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    room(hass, 22.3)
    rig = await full_setup(hass, sim, capabilities)
    await rig.run_then_stop()
    unit_guard = rig.unit_guard
    sim.write_res = "unknown"  # the unit refuses the restore (say, while off)
    sim.indoor["iu_onoff"] = "0"
    unit_guard.device.apply({"iu_onoff": "0"})
    await hass.async_block_till_done()
    assert unit_guard.stored.restore_pending == 22.0
    # A compressor stop now must not take the lowered 21.5 as the user's setpoint.
    sim.write_res = "ack"
    sim.indoor["iu_onoff"] = "1"
    count = len(writes(sim))
    unit_guard.device.apply({"iu_onoff": "1"})  # back on: the restore is retried
    await hass.async_block_till_done()
    assert writes(sim)[count:] == [{"iu_set_tmp": "220"}]
    assert unit_guard.stored.restore_pending is None


async def test_a_user_change_cancels_a_pending_restore(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    room(hass, 22.3)
    rig = await full_setup(hass, sim, capabilities)
    await rig.run_then_stop()
    unit_guard = rig.unit_guard
    sim.write_res = "unknown"
    sim.indoor["iu_onoff"] = "0"
    unit_guard.device.apply({"iu_onoff": "0"})
    await hass.async_block_till_done()
    assert unit_guard.stored.restore_pending == 22.0
    sim.write_res = "ack"
    unit_guard.device.apply({"iu_set_tmp": "240"})  # set with the remote while off
    await hass.async_block_till_done()
    assert unit_guard.stored.restore_pending is None
    count = len(writes(sim))
    sim.indoor["iu_onoff"] = "1"
    unit_guard.device.apply({"iu_onoff": "1"})
    await hass.async_block_till_done()
    assert writes(sim)[count:] == []  # 24.0 stays


async def test_the_pause_is_recorded_when_the_hold_ends_first(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    room(hass, 22.3)
    rig = await full_setup(hass, sim, capabilities)
    await rig.run_then_stop()  # stopped about 150 s in total
    room(hass, 21.6)
    await rig.tick()  # the hold ends before the compressor starts again
    outcome = rig.unit_guard.stored.outcomes[-1]
    assert outcome["pause_minutes"] is None
    await rig.power(300, 600, 3)  # first reading 100 s in: started about 4 min after the stop
    assert outcome["pause_minutes"] == pytest.approx(4.0, abs=0.3)
    await rig.power(1200, 450, 4)
    await rig.power(150, 10, 10)
    assert outcome["next_run_minutes"] == pytest.approx(23.6, abs=0.5)


async def test_room_readings_go_stale(
    hass: HomeAssistant,
    sim: Simulator,
    capabilities: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(guard_module, "STALE_S", 300.0)
    room(hass, 22.3)
    entry = await setup(hass, sim, capabilities, GUARD_ON)
    assert entry.runtime_data.guard is not None
    (unit_guard,) = entry.runtime_data.guard.units.values()
    reported = state_of(hass, ROOM).last_reported.timestamp()
    entry.runtime_data.guard.clock = lambda: reported + 299
    assert unit_guard.room() == 22.3
    entry.runtime_data.guard.clock = lambda: reported + 301
    assert unit_guard.room() is None


async def test_the_settings_show_stored_values_inside_the_sections(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    entry = await setup(hass, sim, capabilities, FULL)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    data_schema = result["data_schema"]
    assert data_schema is not None
    schema = data_schema.schema
    suggested: dict[str, Any] = {}
    for value in schema.values():
        inner = getattr(value, "schema", None)
        if inner is None:
            continue
        for k in inner.schema:
            if (description := getattr(k, "description", None)) and "suggested_value" in (
                description
            ):
                suggested[str(k)] = description["suggested_value"]
    assert suggested[OPT_POWER_SENSOR] == POWER
    assert suggested[OPT_GUARD] is True


async def test_switching_the_guard_off_while_the_unit_is_off(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    """The undo waits for the unit to be switched on, even with the guard off."""
    room(hass, 22.3)
    hass.states.async_set(POWER, "10", {"unit_of_measurement": "W"})
    entry = await setup(hass, sim, capabilities, FULL)  # fan Auto -> floor
    rig = Rig(hass, entry)
    await rig.run_then_stop()
    assert writes(sim) == [{"iu_fan_spd": "7"}, {"iu_set_tmp": "215"}]
    sim.indoor["iu_onoff"] = "0"
    rig.unit_guard.device.apply({"iu_onoff": "0"})
    await hass.async_block_till_done()
    hass.config_entries.async_update_entry(entry, options={OPT_POWER_SENSOR: POWER})
    await hass.async_block_till_done()  # reloaded with the guard off
    assert len(writes(sim)) == 2  # nothing sent to the switched-off unit
    guard = entry.runtime_data.guard
    assert guard is not None
    assert not guard.enabled
    (unit,) = entry.runtime_data.units.values()
    sim.indoor["iu_onoff"] = "1"
    unit.device.apply({"iu_onoff": "1"})
    await hass.async_block_till_done()
    assert writes(sim)[2:] == [{"iu_set_tmp": "220"}, {"iu_fan_spd": "0"}]


async def test_a_room_below_the_setpoint_is_not_held(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    """Measured: room 21.8 C at setpoint 22.0 and the unit still stopped (its own
    sensor read 23.25 C). The room needs heat, so no hold."""
    room(hass, 21.8)
    rig = await full_setup(hass, sim, capabilities)
    await rig.run_then_stop()
    assert writes(sim) == []
