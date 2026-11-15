"""Sensors, the outdoor device, switches, selects, subscriptions and the clock."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiohttp.test_utils import TestServer
from homeassistant.const import ATTR_ENTITY_ID, STATE_ON
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.airstage_direct.api import objects
from custom_components.airstage_direct.climate import AirstageClimate
from custom_components.airstage_direct.const import CONF_CAPABILITIES, DOMAIN
from custom_components.airstage_direct.diagnostics import async_get_config_entry_diagnostics
from tests.integration.conftest import unit_subentry
from tests.simulator import Simulator

CLIMATE_NAMES = set(AirstageClimate._names)


def get_names(sim: Simulator) -> set[str]:
    return {n for r in sim.requests if r["endpoint"] == "GetParam" for n in r["body"]["list"]}


def state_of(hass: HomeAssistant, entity_id: str) -> State:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state


def unit_of(entry: MockConfigEntry) -> Any:
    return next(iter(entry.runtime_data.units.values()))


async def test_default_entities(
    hass: HomeAssistant, sim: Simulator, setup_entry: MockConfigEntry
) -> None:
    assert state_of(hass, "sensor.living_room_room_temperature").state == "22.0"
    assert state_of(hass, "sensor.living_room_error_code").state == "0"
    assert state_of(hass, "binary_sensor.living_room_malfunction").state == "off"
    assert state_of(hass, "switch.living_room_economy").state == "off"
    assert state_of(hass, "select.living_room_vertical_louvre").state == "4"
    assert state_of(hass, "select.living_room_horizontal_louvre").state == "2"
    assert hass.states.get("sensor.airco_outdoor_unit_compressor_frequency") is not None
    # Disabled by default: not set up, so not read.
    assert hass.states.get("sensor.living_room_wifi_signal") is None
    assert hass.states.get("sensor.airco_outdoor_unit_discharge_temperature") is None
    unit = unit_of(setup_entry)
    assert {"iu_suct_tmp", "iu_heat_ex_tmp", "iu_fan_rot1", "ou_cmp_freq"} <= unit.device.subscribed
    assert "ou_disc_tmp" not in unit.device.subscribed
    assert "wla_rssi" not in unit.device.subscribed_for(1)


async def test_unsupported_objects_get_no_entity(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    caps = {**capabilities, "unsupported": ["iu_af_dir_hrz", "iu_economy", "iu_fan_rot1"]}
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Airco",
        data={},
        subentries_data=[unit_subentry(sim, extra={CONF_CAPABILITIES: caps})],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("select.living_room_horizontal_louvre") is None
    assert hass.states.get("switch.living_room_economy") is None
    assert hass.states.get("sensor.living_room_fan_speed") is None
    assert hass.states.get("select.living_room_vertical_louvre") is not None
    assert "iu_fan_rot1" not in get_names(sim)


async def test_only_climate_objects_when_everything_else_is_disabled(
    hass: HomeAssistant, sim: Simulator, setup_entry: MockConfigEntry
) -> None:
    """With only the climate entity enabled, nothing else is read."""
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, setup_entry.entry_id):
        if entity.domain != "climate":
            registry.async_update_entity(
                entity.entity_id, disabled_by=er.RegistryEntryDisabler.USER
            )
    assert await hass.config_entries.async_reload(setup_entry.entry_id)
    await hass.async_block_till_done()
    sim.requests.clear()
    await unit_of(setup_entry).async_refresh()
    await asyncio.sleep(0.3)  # the monitor scheduler stays idle: no learning polls
    assert get_names(sim) <= CLIMATE_NAMES | {objects.ANCHOR_INDOOR}
    assert get_names(sim) >= {"iu_set_tmp", "iu_onoff"}
    assert not unit_of(setup_entry).monitor.active()
    assert unit_of(setup_entry).monitor.learn_reads == 0


async def test_enabling_an_adapter_sensor_reads_sub_id_1(
    hass: HomeAssistant, sim: Simulator, setup_entry: MockConfigEntry
) -> None:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{sim.mac}_wifi_signal")
    assert entity_id is not None
    registry.async_update_entity(entity_id, disabled_by=None)
    assert await hass.config_entries.async_reload(setup_entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == str(sim.adapter["wla_rssi"])
    sub1 = [
        r for r in sim.requests if r["endpoint"] == "GetParam" and r["body"]["device_sub_id"] == 1
    ]
    assert sub1
    assert all(n.startswith("wla_") for r in sub1 for n in r["body"]["list"])


async def test_outdoor_temperature_falls_back_when_outdoor_is_empty(
    hass: HomeAssistant, sim: Simulator, setup_entry: MockConfigEntry
) -> None:
    unit = unit_of(setup_entry)
    unit.device.apply({"ou_outdoor_tmp": "", "iu_outdoor_tmp": "5600"})
    unit.device.apply({"ou_outdoor_tmp": ""})  # past the one-cycle hold
    await hass.async_block_till_done()
    state = hass.states.get("sensor.airco_outdoor_unit_outdoor_temperature")
    assert state is not None
    assert state.state == "6.0"


async def test_outdoor_values_come_from_a_unit_that_is_on(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    upstairs_sim = Simulator("unit_upstairs_07kj.json")  # a second adapter
    server = TestServer(upstairs_sim.app, host="127.0.0.1")
    await server.start_server()
    assert server.port is not None
    upstairs_sim.port = server.port
    try:
        second = unit_subentry(sim, "Upstairs", extra={CONF_CAPABILITIES: capabilities})
        second["data"] = {**second["data"], "device_id": upstairs_sim.mac, "port": server.port}
        second["unique_id"] = upstairs_sim.mac
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="Airco",
            data={},
            subentries_data=[unit_subentry(sim, extra={CONF_CAPABILITIES: capabilities}), second],
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        outdoor = entry.runtime_data.outdoor
        living, upstairs = entry.runtime_data.units.values()
        assert outdoor.source is living  # both on; the first stays the source
        assert "ou_cmp_freq" in living.device.subscribed
        assert "ou_cmp_freq" not in upstairs.device.subscribed
        assert "ou_cmp_freq" not in {n for lst in upstairs_sim.get_lists() for n in lst}
        # The source turns off while the other unit is on: the subscriptions move.
        living.device.apply({"iu_onoff": "0"})
        await hass.async_block_till_done()
        assert outdoor.source is upstairs
        assert "ou_cmp_freq" in upstairs.device.subscribed
        assert "ou_cmp_freq" not in living.device.subscribed
        assert await hass.config_entries.async_unload(entry.entry_id)
    finally:
        await server.close()


async def test_switch_and_select_write(
    hass: HomeAssistant, sim: Simulator, setup_entry: MockConfigEntry
) -> None:
    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: "switch.living_room_powerful"}, blocking=True
    )
    await hass.services.async_call(
        "select",
        "select_option",
        {ATTR_ENTITY_ID: "select.living_room_vertical_louvre", "option": "2"},
        blocking=True,
    )
    writes = [r["body"]["value"] for r in sim.requests if r["endpoint"] == "SetParam"]
    assert writes == [{"iu_powerful": "1"}, {"iu_af_dir_vrt": "2"}]
    assert state_of(hass, "switch.living_room_powerful").state == STATE_ON


async def test_refused_switch_says_so(
    hass: HomeAssistant, sim: Simulator, setup_entry: MockConfigEntry
) -> None:
    sim.write_res = "unknown"
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "switch",
            "turn_on",
            {ATTR_ENTITY_ID: "switch.living_room_outdoor_low_noise"},
            blocking=True,
        )
    await hass.async_block_till_done()
    state = hass.states.get("switch.living_room_outdoor_low_noise")
    assert state is not None
    assert state.attributes["refused_by_unit"] is True
    sub = next(iter(setup_entry.subentries.values()))
    assert "ou_low_noise" in sub.data[CONF_CAPABILITIES]["refused"]  # kept across restarts
    sim.write_res = "ack"  # e.g. a single split, or DIP switch SET1-4 turned on
    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: "switch.living_room_outdoor_low_noise"}, blocking=True
    )
    state = hass.states.get("switch.living_room_outdoor_low_noise")
    assert state is not None
    assert state.attributes["refused_by_unit"] is False


async def test_lost_clock_is_set_once(
    hass: HomeAssistant, sim: Simulator, setup_entry: MockConfigEntry
) -> None:
    unit = unit_of(setup_entry)
    unit.monitor.offset = 100.0
    placeholders = {"iu_suct_tmp": "2000", "iu_heat_ex_tmp": "2000", "ou_cmp_freq": "",
                    "ou_outdoor_tmp": "", "ou_current": ""}  # fmt: skip
    for _ in range(3):
        unit.device.raw.update(placeholders)
        unit._check_clock()
    await hass.async_block_till_done()
    assert sim.clock_set is not None
    assert unit.monitor.offset is None  # learn the refresh again
    sim.clock_set = None
    unit.device.raw.update(placeholders)
    unit._check_clock()
    await hass.async_block_till_done()
    assert sim.clock_set is None  # once, not on every read


async def test_twenty_degrees_alone_is_no_lost_clock(
    hass: HomeAssistant, sim: Simulator, setup_entry: MockConfigEntry
) -> None:
    unit = unit_of(setup_entry)
    for _ in range(5):
        unit.device.raw.update(
            {"iu_suct_tmp": "2000", "iu_heat_ex_tmp": "2000", "ou_cmp_freq": "30"}
        )
        unit._check_clock()
    await hass.async_block_till_done()
    assert sim.clock_set is None


async def test_diagnostics_show_the_request_log(
    hass: HomeAssistant, sim: Simulator, setup_entry: MockConfigEntry
) -> None:
    data = await async_get_config_entry_diagnostics(hass, setup_entry)
    requests = data["subentries"][0]["unit"]["requests"]
    assert requests
    assert {"time", "endpoint", "result"} <= set(requests[0])
    assert any("iu_set_tmp" in r.get("names", []) for r in requests)
    assert sim.mac not in str(data)


async def test_room_temperature_appears_after_the_unit_is_switched_on(
    hass: HomeAssistant,
    sim: Simulator,
    capabilities: dict[str, Any],
    push_port: int,
) -> None:
    """Set up while off: room temperature answered 65535 and got no entity."""
    from datetime import timedelta

    import aiohttp
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    caps = {**capabilities, "unsupported": ["iu_indoor_tmp"]}
    sim.indoor["iu_onoff"] = "0"
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Airco",
        data={},
        subentries_data=[unit_subentry(sim, extra={CONF_CAPABILITIES: caps})],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.living_room_room_temperature") is None
    # Switched on with the remote: the push says so; room temperature now answers.
    sim.indoor["iu_onoff"] = "1"
    body = {"value": {"iu_onoff": "1"}, "device_id": sim.mac}
    async with (
        aiohttp.ClientSession() as session,
        session.post(f"http://127.0.0.1:{push_port}/NtfyParam", json=body) as resp,
    ):
        assert resp.status == 200
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=181))
    await hass.async_block_till_done()
    sub = next(iter(entry.subentries.values()))
    assert "iu_indoor_tmp" not in sub.data[CONF_CAPABILITIES]["unsupported"]
    assert state_of(hass, "sensor.living_room_room_temperature").state == "22.0"


async def test_device_diagnostics(
    hass: HomeAssistant, sim: Simulator, setup_entry: MockConfigEntry
) -> None:
    from homeassistant.helpers import device_registry as dr

    from custom_components.airstage_direct.diagnostics import async_get_device_diagnostics

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, sim.mac), setup_entry.entry_id
    )
    assert device is not None
    data = await async_get_device_diagnostics(hass, setup_entry, device)
    assert [s["title"] for s in data["subentries"]] == ["Living room"]
    assert sim.mac not in str(data)


async def test_human_sensor_switch(
    hass: HomeAssistant, sim: Simulator, setup_entry: MockConfigEntry
) -> None:
    assert state_of(hass, "switch.living_room_human_sensor").state == "off"
    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: "switch.living_room_human_sensor"}, blocking=True
    )
    writes = [r["body"]["value"] for r in sim.requests if r["endpoint"] == "SetParam"]
    assert writes == [{"iu_hmn_det_auto_save": "1"}]
    assert state_of(hass, "switch.living_room_human_sensor").state == STATE_ON
