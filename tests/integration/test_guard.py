"""The short-cycle guard (experimental): fan floor, the room thermostat, and
the fail-safe rules. With the guard off, it never writes."""

from __future__ import annotations

import time
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
    OPT_GUARD_THERMOSTAT,
    OPT_POWER_SENSOR,
)
from tests.integration.conftest import unit_subentry
from tests.simulator import Simulator

POWER = "sensor.airco_power"
ROOM = "sensor.living_room_temperature"
CLIMATE = "climate.living_room"
GUARD_ON = {OPT_GUARD: True}


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


# -- the room thermostat --------------------------------------------------------------


class Rig:
    """The guard's clock, ticks, and units."""

    def __init__(self, hass: HomeAssistant, entry: MockConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.now = time.time() + 1
        guard = entry.runtime_data.guard
        assert guard is not None
        self.guard = guard
        guard.clock = lambda: self.now
        if entry.runtime_data.compressor is not None:
            entry.runtime_data.compressor.clock = lambda: self.now
        (self.unit_guard,) = guard.units.values()
        (self.unit,) = entry.runtime_data.units.values()

    async def tick(self, seconds: float = 0) -> None:
        self.now += seconds
        self.guard._tick(None)
        await self.hass.async_block_till_done()

    async def room(self, celsius: float, seconds: float = 0) -> None:
        room(self.hass, celsius)
        await self.tick(seconds)

    async def push(self, sim: Simulator, **values: str) -> None:
        """A change made elsewhere (remote, app), arriving like a push."""
        sim.indoor.update(values)
        self.unit.device.apply(values)
        await self.hass.async_block_till_done()


async def thermostat(
    hass: HomeAssistant,
    sim: Simulator,
    capabilities: dict[str, Any],
    room_c: float = 21.8,
    options: dict[str, Any] | None = None,
    **kw: Any,
) -> Rig:
    room(hass, room_c)  # setpoint 22.0
    entry = await setup(hass, sim, capabilities, options or GUARD_ON, fan="7", **kw)
    return Rig(hass, entry)


async def test_runs_and_pauses_on_the_room_sensor(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    rig = await thermostat(hass, sim, capabilities)  # measured: room 21.8 at 22.0
    assert writes(sim) == [{"iu_set_tmp": "230"}]  # run: 22 + 1
    climate = state_of(hass, CLIMATE)
    assert climate.attributes["temperature"] == 22.0  # still shows the user's setpoint
    assert climate.attributes["guard"] == "run"
    assert climate.attributes["guard_unit_setpoint"] == 23.0
    await rig.room(22.3)  # top of the band, but within 5 minutes: wait
    assert len(writes(sim)) == 1
    await rig.tick(300)
    assert writes(sim)[-1] == {"iu_set_tmp": "190"}  # pause: 22 - 3
    assert state_of(hass, CLIMATE).attributes["guard"] == "pause"
    record = state_of(hass, CLIMATE).attributes["guard_last_phase"]
    assert record["phase"] == "run"
    assert record["ended"] == "room reached the top of the band"
    await rig.room(21.8, 600)  # not yet at the bottom
    assert len(writes(sim)) == 2
    await rig.room(21.7)
    assert writes(sim)[-1] == {"iu_set_tmp": "230"}  # the next run
    assert state_of(hass, CLIMATE).attributes["guard_last_phase"]["phase"] == "pause"


async def test_a_warm_room_starts_with_a_pause(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    await thermostat(hass, sim, capabilities, room_c=22.4)
    assert writes(sim) == [{"iu_set_tmp": "190"}]


async def test_no_room_sensor_no_thermostat(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    await thermostat(hass, sim, capabilities, room_sensor=False)
    assert writes(sim) == []
    assert state_of(hass, CLIMATE).attributes["guard_room_thermostat"] == "needs a room sensor"


async def test_the_thermostat_can_be_switched_off(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    await thermostat(hass, sim, capabilities, options={**GUARD_ON, OPT_GUARD_THERMOSTAT: False})
    assert writes(sim) == []


async def test_a_new_setpoint_in_home_assistant_becomes_the_target(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    rig = await thermostat(hass, sim, capabilities)
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 23}, blocking=True
    )
    await hass.async_block_till_done()
    assert writes(sim) == [{"iu_set_tmp": "230"}, {"iu_set_tmp": "240"}]  # run: 23 + 1
    assert rig.unit_guard.stored.user_setpoint == 23.0
    assert state_of(hass, CLIMATE).attributes["temperature"] == 23.0


async def test_a_change_with_the_remote_wins(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    rig = await thermostat(hass, sim, capabilities)
    await rig.push(sim, iu_set_tmp="210")  # with the remote
    assert not rig.unit_guard.active
    assert len(writes(sim)) == 1  # nothing put back over it
    await rig.tick(300)  # later the guard carries on, with 21.0 as the target
    assert rig.unit_guard.stored.user_setpoint == 21.0
    assert writes(sim)[-1] == {"iu_set_tmp": "180"}  # room 21.8: above 21.3, a pause


async def test_switching_the_unit_off_and_on(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    rig = await thermostat(hass, sim, capabilities)
    await rig.push(sim, iu_onoff="0")
    assert writes(sim) == [{"iu_set_tmp": "230"}]  # an off unit refuses: not even tried
    assert rig.unit_guard.stored.restore_pending == 22.0
    await rig.push(sim, iu_onoff="1")  # on again: the guard carries on with 22.0
    assert rig.unit_guard.stored.user_setpoint == 22.0
    assert rig.unit_guard.stored.restore_pending is None
    assert len(writes(sim)) == 1  # already at the run setpoint


async def test_off_without_the_room_sensor_restores_at_power_on(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    rig = await thermostat(hass, sim, capabilities)
    await rig.push(sim, iu_onoff="0")
    hass.states.async_set(ROOM, "unavailable")
    await rig.push(sim, iu_onoff="1")
    assert writes(sim)[-1] == {"iu_set_tmp": "220"}  # back to the user's


async def test_a_user_change_cancels_a_pending_restore(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    rig = await thermostat(hass, sim, capabilities)
    await rig.push(sim, iu_onoff="0")
    await rig.push(sim, iu_set_tmp="240")  # with the remote while off
    assert rig.unit_guard.stored.restore_pending is None
    await rig.push(sim, iu_onoff="1")
    await rig.tick(300)
    assert rig.unit_guard.stored.user_setpoint == 24.0  # the remote's value is the target


async def test_a_stale_room_sensor_hands_back(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    rig = await thermostat(hass, sim, capabilities)
    hass.states.async_set(ROOM, "unavailable")
    await rig.tick()
    assert writes(sim)[-1] == {"iu_set_tmp": "220"}
    assert state_of(hass, CLIMATE).attributes["guard_last_phase"]["ended"] == (
        "room sensor unavailable or stale"
    )


async def test_a_refused_setpoint_lets_go(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    sim.write_res = "unknown"
    rig = await thermostat(hass, sim, capabilities)
    assert not rig.unit_guard.active
    assert rig.unit_guard.stored.restore_pending is None
    assert state_of(hass, CLIMATE).attributes["temperature"] == 22.0


async def test_unload_restores_and_a_crash_is_undone_at_start_up(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    rig = await thermostat(hass, sim, capabilities)
    assert await hass.config_entries.async_unload(rig.entry.entry_id)
    assert writes(sim)[-1] == {"iu_set_tmp": "220"}
    # A crash in a pause: undone at the next start, then the guard carries on.
    sim.indoor["iu_set_tmp"] = "190"
    store: Store[dict[str, Any]] = Store(
        hass, guard_module.STORE_VERSION, f"{DOMAIN}.{rig.entry.entry_id}.guard"
    )
    (sub_id,) = rig.entry.subentries
    await store.async_save(
        {sub_id: {"user_setpoint": 22.0, "guard_setpoint": 19.0, "phase": "pause"}}
    )
    count = len(writes(sim))
    assert await hass.config_entries.async_setup(rig.entry.entry_id)
    await hass.async_block_till_done()
    assert writes(sim)[count:] == [{"iu_set_tmp": "220"}, {"iu_set_tmp": "230"}]


async def test_home_assistant_stopping_restores(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    rig = await thermostat(hass, sim, capabilities)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert writes(sim)[-1] == {"iu_set_tmp": "220"}
    assert not rig.unit_guard.active


async def test_stopping_with_the_fan_floor_puts_auto_back_for_good(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    await setup(hass, sim, capabilities, GUARD_ON, room_sensor=False)  # fan Auto -> floor
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert writes(sim) == [{"iu_fan_spd": "7"}, {"iu_fan_spd": "0"}]  # not floored again


async def test_switching_the_guard_off_while_the_unit_is_off(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    """The undo waits for the unit to be switched on, even with the guard off."""
    room(hass, 21.8)
    entry = await setup(hass, sim, capabilities, GUARD_ON)  # fan Auto -> floor, a run
    rig = Rig(hass, entry)
    assert writes(sim) == [{"iu_set_tmp": "230"}, {"iu_fan_spd": "7"}]
    await rig.push(sim, iu_onoff="0")
    hass.config_entries.async_update_entry(entry, options={})
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


async def test_records_count_compressor_starts_with_a_power_sensor(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    hass.states.async_set(POWER, "10", {"unit_of_measurement": "W"})
    rig = await thermostat(hass, sim, capabilities, options={**GUARD_ON, OPT_POWER_SENSOR: POWER})
    compressor = rig.entry.runtime_data.compressor
    assert compressor is not None

    async def power(seconds: float, watts: float) -> None:
        for _ in range(10):
            rig.now += seconds / 10
            hass.states.async_set(POWER, str(watts), {"unit_of_measurement": "W"})
            compressor._tick(None)
            await hass.async_block_till_done()

    await power(30, 10)
    await power(600, 500)  # one start in the run
    room(hass, 22.3)
    await rig.tick(300)
    assert rig.unit_guard.stored.records[-1]["compressor_starts"] == 1


async def test_too_many_writes_stop_the_guard_for_that_unit(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    entry = await setup(hass, sim, capabilities, GUARD_ON, room_sensor=False)
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
    room(hass, 22.3)
    hass.states.async_set(POWER, "10", {"unit_of_measurement": "W"})
    entry = await setup(hass, sim, capabilities, {**GUARD_ON, OPT_POWER_SENSOR: POWER})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    data_schema = result["data_schema"]
    assert data_schema is not None
    suggested: dict[str, Any] = {}
    for value in data_schema.schema.values():
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
