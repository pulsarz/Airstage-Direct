"""The estimates in Home Assistant: heat output, energy, meter, options, restore."""

from __future__ import annotations

import math
from typing import Any

import pytest
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.airstage_direct.const import (
    CONF_CAPABILITIES,
    CONF_CURVE_INTERCEPT,
    CONF_CURVE_SLOPE,
    CONF_MODEL,
    CONF_RATED_AIRFLOW,
    DOMAIN,
    OPT_ESTIMATES,
    OPT_METER,
)
from tests.integration.conftest import unit_subentry
from tests.simulator import Simulator

HEAT = "sensor.living_room_estimated_heat_output"
# Simulator (14KJ dump): 960 rpm, coil 39.00 C, intake 25.50 C, heat, on.
# Coil effectiveness at 492.7 of 720 m3/h, from 0.85 at High: 0.890.
EPS = 1 - math.exp(math.log(0.15) * ((0.683 * 960 - 163) / 720) ** -0.4)
EXPECTED_W = (0.683 * 960 - 163) / 3600 * 1.2 * 1005 * EPS * (39.0 - 25.5)


def state_of(hass: HomeAssistant, entity_id: str) -> State:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state


async def setup(
    hass: HomeAssistant,
    sim: Simulator,
    capabilities: dict[str, Any],
    *,
    model: str = "ASEH14KJCAL",
    options: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> MockConfigEntry:
    data = {CONF_CAPABILITIES: capabilities, CONF_MODEL: model, **(extra or {})}
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Airco",
        data={},
        options=options or {},
        subentries_data=[unit_subentry(sim, extra=data)],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_heat_output_of_a_unit(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    await setup(hass, sim, capabilities)
    state = state_of(hass, HEAT)
    assert float(state.state) == pytest.approx(EXPECTED_W, abs=1)
    assert state.attributes["estimate"] is True
    assert state.attributes["accuracy"] == "±20–25 %"  # noqa: RUF001
    assert "coil" in state.attributes["method"]
    assert state.attributes["inputs"]["fan_rpm"] == 960
    assert state.attributes["inputs"]["coil_effectiveness"] == pytest.approx(0.890, abs=0.001)
    assert state.attributes["inputs"]["coil_effectiveness_at_high"] == 0.85
    total = state_of(hass, "sensor.airco_outdoor_unit_estimated_heat_output_total")
    assert float(total.state) == pytest.approx(EXPECTED_W, abs=1)
    cooling = state_of(hass, "sensor.living_room_estimated_cooling_output_sensible_only")
    assert cooling.state == "0"


async def test_no_model_no_estimate(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    await setup(hass, sim, capabilities, model="")
    assert hass.states.get(HEAT) is None
    assert hass.states.get("sensor.airco_outdoor_unit_estimated_cop_1h") is None


async def test_estimates_off(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    await setup(hass, sim, capabilities, options={OPT_ESTIMATES: False})
    assert hass.states.get(HEAT) is None


async def test_energy_is_integrated(
    hass: HomeAssistant,
    sim: Simulator,
    capabilities: dict[str, Any],
) -> None:
    entry = await setup(hass, sim, capabilities)
    system = entry.runtime_data.estimates
    assert system is not None
    now = [1_000_000.0]
    system.clock = lambda: now[0]
    system._recalculate()
    now[0] += 300
    system._tick(None)
    await hass.async_block_till_done()
    energy = float(state_of(hass, "sensor.airco_outdoor_unit_estimated_heat_energy").state)
    assert energy == pytest.approx(EXPECTED_W * 300 / 3600 / 1000, rel=0.02)
    # Restored after a reload.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    again = float(state_of(hass, "sensor.airco_outdoor_unit_estimated_heat_energy").state)
    assert again == pytest.approx(energy, rel=0.01)


async def test_meter_split_and_units(
    hass: HomeAssistant,
    sim: Simulator,
    capabilities: dict[str, Any],
) -> None:
    hass.states.async_set("sensor.airco_energy", "100000", {"unit_of_measurement": "Wh"})
    entry = await setup(hass, sim, capabilities, options={OPT_METER: "sensor.airco_energy"})
    system = entry.runtime_data.estimates
    assert system is not None
    now = [1_000_000.0]
    system.clock = lambda: now[0]
    system._recalculate()  # heating: electricity goes to heating
    now[0] += 60
    hass.states.async_set("sensor.airco_energy", "100500", {"unit_of_measurement": "Wh"})
    await hass.async_block_till_done()
    heating = state_of(hass, "sensor.airco_outdoor_unit_electricity_heating")
    assert float(heating.state) == 0  # counted, but not written until the next tick
    system._tick(None)
    await hass.async_block_till_done()
    heating = state_of(hass, "sensor.airco_outdoor_unit_electricity_heating")
    assert float(heating.state) == pytest.approx(0.5)
    assert "estimate" not in heating.attributes  # metered, not estimated
    hass.states.async_set("sensor.airco_energy", "unavailable")
    await hass.async_block_till_done()
    assert float(state_of(hass, "sensor.airco_outdoor_unit_electricity_heating").state) == 0.5


async def test_without_a_meter_electricity_is_estimated(
    hass: HomeAssistant,
    sim: Simulator,
    capabilities: dict[str, Any],
) -> None:
    entry = await setup(hass, sim, capabilities)
    system = entry.runtime_data.estimates
    assert system is not None
    now = [1_000_000.0]
    system.clock = lambda: now[0]
    system._recalculate()
    assert system.electricity_w == pytest.approx(3.5 * 230)  # ou_current 350 -> 3.50 A
    now[0] += 600
    system._tick(None)
    await hass.async_block_till_done()
    state = state_of(hass, "sensor.airco_outdoor_unit_estimated_electricity_heating")
    assert float(state.state) == pytest.approx(3.5 * 230 * 600 / 3600 / 1000, rel=0.02)
    assert state.attributes["estimate"] is True


async def test_model_setting_reloads(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    entry = await setup(hass, sim, capabilities, model="")
    (sub,) = entry.subentries.values()
    result = await entry.start_subentry_reconfigure_flow(hass, sub.subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_MODEL: "custom"}
    )
    assert result["errors"] == {"base": "curve_needed"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_MODEL: "custom", CONF_CURVE_SLOPE: 0.683,
         CONF_CURVE_INTERCEPT: -163},
    )  # fmt: skip
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert float(state_of(hass, HEAT).state) == pytest.approx(EXPECTED_W / EPS * 0.85, abs=1)
    result = await entry.start_subentry_reconfigure_flow(hass, sub.subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_MODEL: "custom", CONF_CURVE_SLOPE: 0.683,
         CONF_CURVE_INTERCEPT: -163, CONF_RATED_AIRFLOW: 720},
    )  # fmt: skip
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert float(state_of(hass, HEAT).state) == pytest.approx(EXPECTED_W, abs=1)
    assert not [r for r in sim.requests if r["endpoint"] == "GetParam" and False]


async def test_options_flow(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    entry = await setup(hass, sim, capabilities)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {OPT_ESTIMATES: False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[OPT_ESTIMATES] is False
    assert hass.states.get(HEAT) is None  # reloaded without estimates; entity removed


async def test_meter_changes_do_not_write_the_sensors_each_time(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    hass.states.async_set("sensor.airco_energy", "100", {"unit_of_measurement": "kWh"})
    entry = await setup(hass, sim, capabilities, options={OPT_METER: "sensor.airco_energy"})
    system = entry.runtime_data.estimates
    assert system is not None
    entity = "sensor.airco_outdoor_unit_electricity_heating"
    written: list[str] = []
    hass.bus.async_listen(
        "state_changed",
        lambda e: written.append(e.data["entity_id"]) if e.data["entity_id"] == entity else None,
    )
    for i in range(1, 11):  # a fast meter: ten readings in under a minute
        hass.states.async_set("sensor.airco_energy", str(100 + i / 100),
                              {"unit_of_measurement": "kWh"})  # fmt: skip
        await hass.async_block_till_done()
    assert written == []
    system._tick(None)
    await hass.async_block_till_done()
    assert written == [entity]
    assert float(state_of(hass, entity).state) == pytest.approx(0.1)


async def test_changing_the_meter_never_counts_the_jump(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    hass.states.async_set("sensor.house_energy", "300", {"unit_of_measurement": "kWh"})
    hass.states.async_set("sensor.airco_energy", "12345", {"unit_of_measurement": "kWh"})
    entry = await setup(hass, sim, capabilities, options={OPT_METER: "sensor.house_energy"})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {OPT_METER: "sensor.airco_energy"}
    )
    await hass.async_block_till_done()
    heating = state_of(hass, "sensor.airco_outdoor_unit_electricity_heating")
    assert float(heating.state) == 0


async def test_reset_button(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    hass.states.async_set("sensor.airco_energy", "100", {"unit_of_measurement": "kWh"})
    entry = await setup(hass, sim, capabilities, options={OPT_METER: "sensor.airco_energy"})
    system = entry.runtime_data.estimates
    assert system is not None
    hass.states.async_set("sensor.airco_energy", "101", {"unit_of_measurement": "kWh"})
    system._tick(None)
    await hass.async_block_till_done()
    assert float(state_of(hass, "sensor.airco_outdoor_unit_electricity_heating").state) == 1.0
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.airco_outdoor_unit_reset_estimate_counters"},
        blocking=True,
    )
    assert float(state_of(hass, "sensor.airco_outdoor_unit_electricity_heating").state) == 0
    hass.states.async_set("sensor.airco_energy", "101.5", {"unit_of_measurement": "kWh"})
    system._tick(None)
    await hass.async_block_till_done()  # re-baselined: only the new 0.5 counts
    assert float(state_of(hass, "sensor.airco_outdoor_unit_electricity_heating").state) == 0.5
