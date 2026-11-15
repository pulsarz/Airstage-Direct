"""The climate entity in Home Assistant, against the simulator."""

from __future__ import annotations

from typing import Any

import aiohttp
import pytest
from homeassistant.components.climate.const import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_FAN_MODE,
    ATTR_HVAC_MODE,
    ATTR_PRESET_MODE,
    ATTR_SWING_HORIZONTAL_MODE,
    ATTR_SWING_MODE,
    HVACMode,
)
from homeassistant.components.climate.const import (
    DOMAIN as CLIMATE_DOMAIN,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.airstage_direct.api.push import TOOLS_PUSH_KEY
from tests.simulator import Simulator

ENTITY = "climate.living_room"


def writes(sim: Simulator) -> list[dict[str, Any]]:
    return [r["body"]["value"] for r in sim.requests if r["endpoint"] == "SetParam"]


async def call(hass: HomeAssistant, service: str, **data: Any) -> None:
    await hass.services.async_call(
        CLIMATE_DOMAIN, service, {ATTR_ENTITY_ID: ENTITY, **data}, blocking=True
    )


async def test_state_from_the_unit(
    hass: HomeAssistant, sim: Simulator, setup_entry: MockConfigEntry
) -> None:
    state = hass.states.get(ENTITY)
    assert state is not None
    assert state.state == HVACMode.HEAT  # iu_onoff 1, iu_op_mode 4
    assert state.attributes[ATTR_TEMPERATURE] == 22.0
    assert state.attributes[ATTR_CURRENT_TEMPERATURE] == 22.0  # iu_indoor_tmp 7200
    assert state.attributes[ATTR_FAN_MODE] == "medium"  # code 8
    assert state.attributes[ATTR_SWING_MODE] == "off"
    assert state.attributes[ATTR_PRESET_MODE] == "none"
    assert state.attributes["min_temp"] == 16.0
    assert state.attributes["max_temp"] == 30.0
    entity = er.async_get(hass).async_get(ENTITY)
    assert entity is not None
    assert entity.unique_id == f"{sim.mac}_climate"
    assert entity.config_subentry_id is not None


async def test_registers_with_its_own_key_and_unregisters(
    hass: HomeAssistant, sim: Simulator, setup_entry: MockConfigEntry
) -> None:
    register = [r["body"] for r in sim.requests if r["endpoint"] == "Register"]
    assert len(register) == 1
    assert register[0]["Key"] != TOOLS_PUSH_KEY
    assert register[0]["IP"] == "127.0.0.1"
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    unregister = [r["body"] for r in sim.requests if r["endpoint"] == "Unregister"]
    assert unregister == [{"Key": register[0]["Key"]}]
    assert sim.overlaps == 0


async def test_push_updates_the_entity(
    hass: HomeAssistant, sim: Simulator, setup_entry: MockConfigEntry, push_port: int
) -> None:
    body = {"value": {"iu_set_tmp": "235", "iu_fan_spd": "2"}, "device_id": sim.mac}
    async with (
        aiohttp.ClientSession() as session,
        session.post(f"http://127.0.0.1:{push_port}/NtfyParam", json=body) as resp,
    ):
        assert resp.status == 200
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert state is not None
    assert state.attributes[ATTR_TEMPERATURE] == 23.5
    assert state.attributes[ATTR_FAN_MODE] == "quiet"


async def test_controls_write_one_name_per_request(
    hass: HomeAssistant, sim: Simulator, setup_entry: MockConfigEntry
) -> None:
    await call(hass, "set_temperature", **{ATTR_TEMPERATURE: 21.5})
    await call(hass, "set_fan_mode", **{ATTR_FAN_MODE: "medium_high"})
    await call(hass, "set_swing_mode", **{ATTR_SWING_MODE: "on"})
    await call(hass, "set_swing_horizontal_mode", **{ATTR_SWING_HORIZONTAL_MODE: "on"})
    await call(hass, "set_preset_mode", **{ATTR_PRESET_MODE: "eco"})
    await call(hass, "set_hvac_mode", **{ATTR_HVAC_MODE: HVACMode.COOL})
    await call(hass, "turn_off")
    assert writes(sim) == [
        {"iu_set_tmp": "215"},
        {"iu_fan_spd": "9"},
        {"iu_af_swg_vrt": "1"},
        {"iu_af_swg_hrz": "1"},
        {"iu_economy": "1"},
        {"iu_op_mode": "1"},
        {"iu_onoff": "0"},
    ]
    state = hass.states.get(ENTITY)
    assert state is not None
    assert state.state == HVACMode.OFF
    assert state.attributes[ATTR_PRESET_MODE] == "eco"
    sim.requests.clear()
    await call(hass, "set_temperature", **{ATTR_TEMPERATURE: 24, ATTR_HVAC_MODE: HVACMode.HEAT})
    assert writes(sim) == [{"iu_onoff": "1"}, {"iu_op_mode": "4"}, {"iu_set_tmp": "240"}]
    sim.requests.clear()
    await call(hass, "set_preset_mode", **{ATTR_PRESET_MODE: "boost"})
    assert writes(sim) == [{"iu_economy": "0"}, {"iu_powerful": "1"}]
    assert sim.overlaps == 0


async def test_turn_on_keeps_the_mode(
    hass: HomeAssistant, sim: Simulator, setup_entry: MockConfigEntry
) -> None:
    await call(hass, "turn_off")
    await call(hass, "turn_on")
    assert writes(sim) == [{"iu_onoff": "0"}, {"iu_onoff": "1"}]


async def test_errors_are_translated(
    hass: HomeAssistant, sim: Simulator, setup_entry: MockConfigEntry
) -> None:
    with pytest.raises(ServiceValidationError) as out_of_range:  # Home Assistant's own check
        await call(hass, "set_temperature", **{ATTR_TEMPERATURE: 30.5})
    assert out_of_range.value.translation_key == "temp_out_of_range"
    with pytest.raises(ServiceValidationError) as not_a_step:  # ours: 0.5 C steps
        await call(hass, "set_temperature", **{ATTR_TEMPERATURE: 21.3})
    assert not_a_step.value.translation_key == "invalid_value"
    assert writes(sim) == []
    sim.write_res = "unknown"
    with pytest.raises(HomeAssistantError) as refused:
        await call(hass, "set_fan_mode", **{ATTR_FAN_MODE: "high"})
    assert refused.value.translation_key == "write_refused"
    sim.write_res = "nack"
    with pytest.raises(HomeAssistantError) as failed:
        await call(hass, "set_fan_mode", **{ATTR_FAN_MODE: "high"})
    assert failed.value.translation_key == "write_failed"
    sim.write_res = "ack"
    sim.raw_reply = (500, "")
    with pytest.raises(HomeAssistantError) as gone:
        await call(hass, "set_fan_mode", **{ATTR_FAN_MODE: "high"})
    assert gone.value.translation_key == "connection_failed"


async def test_unreachable_unit_is_unavailable(
    hass: HomeAssistant, sim: Simulator, setup_entry: MockConfigEntry
) -> None:
    sim.raw_reply = (500, "")
    unit = next(iter(setup_entry.runtime_data.units.values()))
    await unit.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE


def test_temperature_api_for_both_home_assistant_versions() -> None:
    from homeassistant.components.climate import ClimateEntity

    from custom_components.airstage_direct.climate import AirstageClimate, install_temperature_api

    for native in (True, False):
        cls = type(f"Probe{native}", (AirstageClimate,), {})
        install_temperature_api(cls, native)
        prefix = "native_" if native else ""
        assert cls.__dict__[f"_attr_{prefix}temperature_unit"] == "°C"
        assert isinstance(cls.__dict__[f"{prefix}current_temperature"], property)
        assert isinstance(cls.__dict__[f"{prefix}target_temperature"], property)
    assert ClimateEntity is not None
