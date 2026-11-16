"""The short-cycle guard (experimental): fan floor, steering by a room sensor,
and the fail-safe rules. With the guard off, it never writes."""

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
    OPT_GUARD_STEER,
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


# -- steering by the room sensor ------------------------------------------------------


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

    async def push(self, sim: Simulator, **values: str) -> None:
        """A change made elsewhere (remote, app), arriving like a push."""
        sim.indoor.update(values)
        self.unit.device.apply(values)
        await self.hass.async_block_till_done()


async def steered(
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


async def test_a_room_below_the_setpoint_raises_the_units_setpoint(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    rig = await steered(hass, sim, capabilities)  # measured: room 21.8 at 22.0
    assert writes(sim) == [{"iu_set_tmp": "225"}]
    climate = state_of(hass, CLIMATE)
    assert climate.attributes["temperature"] == 22.0  # still shows the user's setpoint
    assert climate.attributes["guard"] == "raised"
    assert climate.attributes["guard_unit_setpoint"] == 22.5
    room(hass, 22.0)  # reached, but within 5 minutes of the raise: wait
    await hass.async_block_till_done()
    assert len(writes(sim)) == 1
    await rig.tick(300)
    assert writes(sim)[-1] == {"iu_set_tmp": "220"}
    record = state_of(hass, CLIMATE).attributes["guard_last_raise"]
    assert record["ended"] == "room reached the setpoint"
    assert record["room_at_start"] == 21.8
    assert record["room_at_end"] == 22.0


async def test_within_the_band_nothing_happens(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    await steered(hass, sim, capabilities, room_c=21.95)
    assert writes(sim) == []


async def test_changes_are_at_least_five_minutes_apart(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    rig = await steered(hass, sim, capabilities)
    await rig.tick(300)
    room(hass, 22.1)
    await rig.tick()
    assert writes(sim)[-1] == {"iu_set_tmp": "220"}
    room(hass, 21.8)  # below again at once: not before 5 minutes
    await rig.tick(60)
    assert len(writes(sim)) == 2
    await rig.tick(240)
    assert writes(sim)[-1] == {"iu_set_tmp": "225"}


async def test_no_room_sensor_no_steering(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    await steered(hass, sim, capabilities, room_sensor=False)
    assert writes(sim) == []
    assert state_of(hass, CLIMATE).attributes["guard_steering"] == "needs a room sensor"


async def test_steering_can_be_switched_off(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    await steered(hass, sim, capabilities, options={**GUARD_ON, OPT_GUARD_STEER: False})
    assert writes(sim) == []


async def test_a_user_setpoint_change_wins(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    rig = await steered(hass, sim, capabilities)
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 23}, blocking=True
    )
    assert writes(sim) == [{"iu_set_tmp": "225"}, {"iu_set_tmp": "230"}]
    assert not rig.unit_guard.raised
    room(hass, 23.5)
    await rig.tick(600)
    assert len(writes(sim)) == 2  # nothing put back over the user's choice


async def test_a_remote_setpoint_change_wins(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    rig = await steered(hass, sim, capabilities)
    await rig.push(sim, iu_set_tmp="210")
    assert not rig.unit_guard.raised
    room(hass, 22.5)
    await rig.tick(600)
    assert writes(sim) == [{"iu_set_tmp": "225"}]


async def test_switching_the_unit_off_restores_at_power_on(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    rig = await steered(hass, sim, capabilities)
    await rig.push(sim, iu_onoff="0")
    assert writes(sim) == [{"iu_set_tmp": "225"}]  # an off unit refuses: not even tried
    assert rig.unit_guard.stored.restore_pending == 22.0
    room(hass, 22.2)  # warm by the time it is switched on again
    await rig.push(sim, iu_onoff="1")
    assert writes(sim)[-1] == {"iu_set_tmp": "220"}
    assert rig.unit_guard.stored.restore_pending is None


async def test_a_user_change_cancels_a_pending_restore(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    rig = await steered(hass, sim, capabilities)
    await rig.push(sim, iu_onoff="0")
    await rig.push(sim, iu_set_tmp="240")  # with the remote while off
    assert rig.unit_guard.stored.restore_pending is None
    room(hass, 24.0)
    count = len(writes(sim))
    await rig.push(sim, iu_onoff="1")
    assert writes(sim)[count:] == []  # 24.0 stays


async def test_a_stale_room_sensor_restores(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    rig = await steered(hass, sim, capabilities)
    hass.states.async_set(ROOM, "unavailable")
    await rig.tick()
    assert writes(sim)[-1] == {"iu_set_tmp": "220"}
    assert state_of(hass, CLIMATE).attributes["guard_last_raise"]["ended"] == (
        "room sensor unavailable or stale"
    )


async def test_a_refused_raise_is_forgotten(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    sim.write_res = "unknown"
    rig = await steered(hass, sim, capabilities)
    assert not rig.unit_guard.raised
    assert rig.unit_guard.stored.restore_pending is None
    assert state_of(hass, CLIMATE).attributes["temperature"] == 22.0


async def test_unload_restores_and_a_crash_is_undone_at_start_up(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    rig = await steered(hass, sim, capabilities)
    assert await hass.config_entries.async_unload(rig.entry.entry_id)
    assert writes(sim)[-1] == {"iu_set_tmp": "220"}
    # A crash while raised: the stored raise is undone at the next start.
    sim.indoor["iu_set_tmp"] = "225"
    store: Store[dict[str, Any]] = Store(
        hass, guard_module.STORE_VERSION, f"{DOMAIN}.{rig.entry.entry_id}.guard"
    )
    (sub_id,) = rig.entry.subentries
    await store.async_save({sub_id: {"user_setpoint": 22.0, "raised_setpoint": 22.5}})
    room(hass, 22.3)  # warm now: nothing new to raise
    assert await hass.config_entries.async_setup(rig.entry.entry_id)
    await hass.async_block_till_done()
    assert writes(sim)[-1] == {"iu_set_tmp": "220"}


async def test_home_assistant_stopping_restores(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    rig = await steered(hass, sim, capabilities)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert writes(sim)[-1] == {"iu_set_tmp": "220"}
    assert not rig.unit_guard.raised


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
    entry = await setup(hass, sim, capabilities, GUARD_ON)  # fan Auto -> floor, raised
    rig = Rig(hass, entry)
    assert writes(sim) == [{"iu_set_tmp": "225"}, {"iu_fan_spd": "7"}]
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
    rig = await steered(hass, sim, capabilities, options={**GUARD_ON, OPT_POWER_SENSOR: POWER})
    compressor = rig.entry.runtime_data.compressor
    assert compressor is not None

    async def power(seconds: float, watts: float) -> None:
        for _ in range(10):
            rig.now += seconds / 10
            hass.states.async_set(POWER, str(watts), {"unit_of_measurement": "W"})
            compressor._tick(None)
            await hass.async_block_till_done()

    await power(30, 10)
    await power(600, 500)  # one start while raised
    room(hass, 22.0)
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
