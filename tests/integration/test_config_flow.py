"""Config flow, subentries, setup paths and diagnostics."""

from __future__ import annotations

import socket
from typing import Any

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.airstage_direct import config_flow
from custom_components.airstage_direct.api import transport as tr
from custom_components.airstage_direct.const import (
    CONF_CAPABILITIES,
    CONF_DEVICE_ID,
    DOMAIN,
    ISSUE_PUSH_UNAVAILABLE,
    STATE_POLL_NO_PUSH_S,
    SUBENTRY_INDOOR_UNIT,
)
from custom_components.airstage_direct.diagnostics import async_get_config_entry_diagnostics
from tests.integration.conftest import free_port, unit_subentry
from tests.simulator import Simulator


async def start_flow(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_NAME: "Airco"})
    assert result["type"] is FlowResultType.MENU
    flow_id: str = result["flow_id"]
    return flow_id


async def test_manual_setup_creates_system_and_unit(hass: HomeAssistant, sim: Simulator) -> None:
    flow_id = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "manual"})
    assert result["step_id"] == "manual"
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_HOST: "127.0.0.1", CONF_NAME: "Living room"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Airco"
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    (sub,) = entry.subentries.values()
    assert (sub.title, sub.unique_id, sub.data[CONF_DEVICE_ID]) == ("Living room", sim.mac, sim.mac)
    # The probe ran at setup and its result is stored with the unit.
    assert "iu_has_watch" in sub.data[CONF_CAPABILITIES]["unknown"]
    assert hass.states.get("climate.living_room") is not None
    endpoints = [r["endpoint"] for r in sim.requests]
    assert "Register" in endpoints
    assert "SetParam" not in endpoints
    assert sim.overlaps == 0


async def test_search_and_pick(
    hass: HomeAssistant, sim: Simulator, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scan(hass: HomeAssistant) -> list[str]:
        return ["127.0.0.1"]

    async def describe(hass: HomeAssistant, host: str) -> str:
        return f"{host} (adapter.lan, ec:3a:56:00:00:01)"

    monkeypatch.setattr(config_flow, "async_scan_network", scan)
    monkeypatch.setattr(config_flow, "async_describe_host", describe)
    flow_id = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "search"})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "searching"
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["step_id"] == "pick"
    schema = result["data_schema"]
    assert schema is not None
    (option,) = schema.schema[CONF_HOST].config["options"]
    assert option == {"value": "127.0.0.1", "label": "127.0.0.1 (adapter.lan, ec:3a:56:00:00:01)"}
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_HOST: "127.0.0.1", CONF_NAME: "Bedroom"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_search_finds_nothing(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scan(hass: HomeAssistant) -> list[str]:
        return []

    monkeypatch.setattr(config_flow, "async_scan_network", scan)
    flow_id = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "search"})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert (result["step_id"], result["errors"]) == ("manual", {"base": "none_found"})


@pytest.mark.parametrize(
    ("arp", "mac", "error"),
    [(None, "", "mac_needed"), (None, "not-a-mac", "invalid_mac"),
     (None, "0A1B2C3D4E5F", "wrong_mac")],
)  # fmt: skip
async def test_manual_errors(
    hass: HomeAssistant,
    sim: Simulator,
    monkeypatch: pytest.MonkeyPatch,
    arp: str | None,
    mac: str,
    error: str,
) -> None:
    monkeypatch.setattr(tr, "arp_mac", lambda host: arp)
    flow_id = await start_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "manual"})
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_HOST: "127.0.0.1", CONF_NAME: "X", CONF_DEVICE_ID: mac}
    )
    assert result["errors"] == {"base": error}
    # Entering the right MAC by hand works without ARP.
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_HOST: "127.0.0.1", CONF_NAME: "X", CONF_DEVICE_ID: sim.mac.lower()}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_describe_host(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    import socket as socket_mod

    monkeypatch.setattr(socket_mod, "gethostbyaddr", lambda host: ("adapter.lan", [], [host]))
    monkeypatch.setattr(config_flow, "arp_mac", lambda host: "0A1B2C3D4E5F")
    label = await config_flow.async_describe_host(hass, "192.0.2.7")
    assert label == "192.0.2.7 (adapter.lan, 0a:1b:2c:3d:4e:5f)"

    def no_name(host: str) -> tuple[str, list[str], list[str]]:
        raise OSError("no name")

    monkeypatch.setattr(socket_mod, "gethostbyaddr", no_name)
    monkeypatch.setattr(config_flow, "arp_mac", lambda host: None)
    assert await config_flow.async_describe_host(hass, "192.0.2.7") == "192.0.2.7"


async def test_cannot_connect(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_flow, "ADAPTER_PORT", free_port())
    flow_id = await start_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "manual"})
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_HOST: "127.0.0.1", CONF_NAME: "X"}
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_unit_added_twice_is_refused(
    hass: HomeAssistant, sim: Simulator, setup_entry: MockConfigEntry
) -> None:
    flow_id = await start_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "manual"})
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_HOST: "127.0.0.1", CONF_NAME: "Again"}
    )
    assert result == result | {"type": FlowResultType.ABORT, "reason": "already_configured"}
    # Also as a subentry of the existing system.
    sub = await hass.config_entries.subentries.async_init(
        (setup_entry.entry_id, SUBENTRY_INDOOR_UNIT),
        context={"source": config_entries.SOURCE_USER},
    )
    assert sub["type"] is FlowResultType.MENU
    sub = await hass.config_entries.subentries.async_configure(
        sub["flow_id"], {"next_step_id": "manual"}
    )
    sub = await hass.config_entries.subentries.async_configure(
        sub["flow_id"], {CONF_HOST: "127.0.0.1", CONF_NAME: "Again"}
    )
    assert sub["reason"] == "already_configured"


async def test_add_unit_as_subentry(hass: HomeAssistant, sim: Simulator) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="Airco", data={})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_INDOOR_UNIT), context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "manual"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_NAME: "Upstairs"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert hass.states.get("climate.upstairs") is not None  # the entry reloaded


async def test_reconfigure_ip(
    hass: HomeAssistant, sim: Simulator, setup_entry: MockConfigEntry
) -> None:
    (sub,) = setup_entry.subentries.values()
    result = await setup_entry.start_subentry_reconfigure_flow(hass, sub.subentry_id)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1"}
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert setup_entry.subentries[sub.subentry_id].data[CONF_HOST] == "127.0.0.1"


async def test_reconfigure_refuses_another_units_address(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> None:
    other = {**unit_subentry(sim, "Upstairs"), "unique_id": "AABBCCDDEE01"}
    other["data"] = {**other["data"], CONF_HOST: "192.0.2.50", CONF_DEVICE_ID: "AABBCCDDEE01"}
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Airco",
        data={},
        subentries_data=[unit_subentry(sim, extra={CONF_CAPABILITIES: capabilities}), other],
    )
    entry.add_to_hass(hass)
    sub = next(s for s in entry.subentries.values() if s.title == "Living room")
    result = await entry.start_subentry_reconfigure_flow(hass, sub.subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.50"}
    )
    assert result["errors"] == {"base": "host_in_use"}
    assert not [r for r in sim.requests if r["endpoint"] == "GetParam"]


async def test_diagnostics_are_redacted(
    hass: HomeAssistant, sim: Simulator, setup_entry: MockConfigEntry
) -> None:
    data = await async_get_config_entry_diagnostics(hass, setup_entry)
    text = str(data)
    assert sim.mac not in text
    assert "127.0.0.1" not in text
    unit = data["subentries"][0]["unit"]
    assert unit["values"]["iu_set_tmp"] == "220"
    assert unit["push"]["pushes"] == 0


async def test_push_port_taken_falls_back_to_polling(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any], push_port: int
) -> None:
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", push_port))
        blocker.listen()
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="Airco",
            data={},
            subentries_data=[unit_subentry(sim, extra={CONF_CAPABILITIES: capabilities})],
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        unit = next(iter(entry.runtime_data.units.values()))
        assert unit.push is None
        assert unit.update_interval is not None
        assert unit.update_interval.total_seconds() == STATE_POLL_NO_PUSH_S
        assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_PUSH_UNAVAILABLE) is not None
        assert "Register" not in [r["endpoint"] for r in sim.requests]
        assert await hass.config_entries.async_unload(entry.entry_id)
