"""Experimental options: compressor starts from a power sensor, and the
temperature on the thermostat card. Without them nothing changes."""

from __future__ import annotations

import time
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.airstage_direct.const import (
    CONF_CAPABILITIES,
    DOMAIN,
    OPT_CLIMATE_TEMPERATURE,
    OPT_COMPRESSOR_OFF_W,
    OPT_COMPRESSOR_ON_W,
    OPT_POWER_SENSOR,
)
from tests.integration.conftest import unit_subentry
from tests.simulator import Simulator

POWER = "sensor.airco_power"
STARTS = "sensor.airco_outdoor_unit_compressor_starts"
LAST_HOUR = "sensor.airco_outdoor_unit_compressor_starts_last_hour"
CLIMATE = "climate.living_room"


def state_of(hass: HomeAssistant, entity_id: str) -> State:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state


async def setup(
    hass: HomeAssistant,
    sim: Simulator,
    capabilities: dict[str, Any],
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Airco",
        data={},
        options=options or {},
        subentries_data=[unit_subentry(sim, extra={CONF_CAPABILITIES: capabilities})],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_nothing_changes_without_the_options(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    entry = await setup(hass, sim, capabilities)
    assert entry.runtime_data.compressor is None
    assert hass.states.get(STARTS) is None
    assert hass.states.get(LAST_HOUR) is None
    assert state_of(hass, CLIMATE).attributes["current_temperature"] == 22.0  # iu_indoor_tmp
    assert not [r for r in sim.requests if r["endpoint"] == "SetParam"]


def set_power(hass: HomeAssistant, watts: float, unit: str = "W") -> None:
    hass.states.async_set(POWER, str(watts), {"unit_of_measurement": unit})


async def test_compressor_starts_from_the_power_sensor(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    set_power(hass, 12)
    entry = await setup(hass, sim, capabilities, {OPT_POWER_SENSOR: POWER})
    monitor = entry.runtime_data.compressor
    assert monitor is not None
    now = [time.time() + 1]  # after the reading taken at start-up
    monitor.clock = lambda: now[0]

    async def at(seconds: float, watts: float, unit: str = "W") -> None:
        now[0] += seconds
        set_power(hass, watts, unit)
        monitor._tick(None)  # the 10 s timer: an unchanged reading is no state change
        await hass.async_block_till_done()

    await at(30, 12)  # standby settles (the first state is not a start)
    for _ in range(2):  # two short cycles: 4.5 min on, 3.6 min off
        await at(1, 900)
        await at(15, 0.9, "kW")  # the same in kW: still running
        await at(255, 12)
        await at(30, 12)
        await at(200, 12)
    assert state_of(hass, STARTS).state == "2"
    last_hour = state_of(hass, LAST_HOUR)
    assert last_hour.state == "2"
    assert last_hour.attributes["compressor_running"] is False
    assert last_hour.attributes["last_run_minutes"] == pytest.approx(4.5, abs=0.2)
    assert last_hour.attributes["last_pause_minutes"] == pytest.approx(3.6, abs=0.3)
    hass.states.async_set(POWER, "unavailable")
    await hass.async_block_till_done()
    assert state_of(hass, STARTS).state == "2"  # unavailable is ignored

    # The total carries on across a reload; the sensors go when the option does.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert state_of(hass, STARTS).state == "2"
    hass.config_entries.async_update_entry(entry, options={})
    await hass.async_block_till_done()
    assert er.async_get(hass).async_get(STARTS) is None


async def test_the_thermostat_can_show_the_corrected_intake(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    await setup(hass, sim, capabilities, {OPT_CLIMATE_TEMPERATURE: "corrected_intake"})
    assert state_of(hass, CLIMATE).attributes["current_temperature"] == 22.5  # iu_indoor_tmp_crct


async def test_options_check_the_levels(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    entry = await setup(hass, sim, capabilities)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "compressor": {
                OPT_POWER_SENSOR: POWER,
                OPT_COMPRESSOR_ON_W: 100,
                OPT_COMPRESSOR_OFF_W: 150,
            }
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "compressor_levels"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "compressor": {
                OPT_POWER_SENSOR: POWER,
                OPT_COMPRESSOR_ON_W: 300,
                OPT_COMPRESSOR_OFF_W: 150,
            }
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.runtime_data.compressor is not None
    assert entry.options[OPT_POWER_SENSOR] == POWER  # stored flat
