"""Fixtures for the Home Assistant integration tests, against the simulator."""

from __future__ import annotations

import socket
from typing import Any

import pytest
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.airstage_direct import config_flow, hub
from custom_components.airstage_direct.api import transport as tr
from custom_components.airstage_direct.const import (
    CONF_CAPABILITIES,
    CONF_DEVICE_ID,
    CONF_PORT,
    DOMAIN,
    SUBENTRY_INDOOR_UNIT,
)
from tests.simulator import Simulator


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load custom_components/airstage_direct."""


@pytest.fixture
def push_port() -> int:
    return free_port()


@pytest.fixture(autouse=True)
def _local_adapter(sim: Simulator, push_port: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the integration at the simulator: its port, its MAC via "ARP", a fast
    pace, a free push port, and localhost as the address the adapter pushes to."""
    monkeypatch.setattr(config_flow, "ADAPTER_PORT", sim.port)
    monkeypatch.setattr(tr, "arp_mac", lambda host: sim.mac)
    monkeypatch.setattr(hub, "TRANSPORT_GAP", tr.MIN_GAP)
    monkeypatch.setattr(hub, "PUSH_LISTEN_HOST", "127.0.0.1")
    monkeypatch.setattr(hub, "PUSH_LISTEN_PORT", push_port)

    async def listen_ip(self: hub.AirstageHub, target: str) -> str:
        return "127.0.0.1"

    monkeypatch.setattr(hub.AirstageHub, "listen_ip", listen_ip)


def unit_subentry(
    sim: Simulator, title: str = "Living room", extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "subentry_type": SUBENTRY_INDOOR_UNIT,
        "data": {
            CONF_HOST: "127.0.0.1",
            CONF_DEVICE_ID: sim.mac,
            CONF_PORT: sim.port,
            **(extra or {}),
        },
        "title": title,
        "unique_id": sim.mac,
    }


@pytest.fixture
def capabilities() -> dict[str, Any]:
    """Stored capabilities as the probe found them on the 14KJ (skips the probe)."""
    return {
        "supported": ["iu_onoff", "iu_op_mode", "iu_set_tmp", "iu_fan_spd", "iu_af_swg_vrt",
                      "iu_af_swg_hrz", "iu_economy", "iu_powerful", "iu_min_heat",
                      "iu_indoor_tmp"],
        "unsupported": ["iu_has_ai_eco"],
        "unknown": ["iu_has_watch"],
        "limits": {"HEAT": [16.0, 30.0], "COOL": [18.0, 30.0], "DRY": [18.0, 30.0],
                   "AUTO": [18.0, 30.0]},
        "firmware": "E101V07P00L02-1",
    }  # fmt: skip


@pytest.fixture
async def setup_entry(
    hass: HomeAssistant, sim: Simulator, capabilities: dict[str, Any]
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Airco",
        data={},
        subentries_data=[unit_subentry(sim, extra={CONF_CAPABILITIES: capabilities})],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
