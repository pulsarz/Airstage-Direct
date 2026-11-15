"""Subnet discovery against the simulator."""

from __future__ import annotations

import socket

import aiohttp
import pytest

from custom_components.airstage_direct.api import discovery
from tests.simulator import Simulator


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_looks_like_adapter() -> None:
    assert discovery.looks_like_adapter({"result": "NG", "error": "0002"})
    assert discovery.looks_like_adapter({"result": "OK", "value": {"iu_set_tmp": "220"}})
    others: list[object] = [None, [], {"result": "NG", "error": "0013"}, {"result": "OK"}, {"x": 1}]
    for other in others:
        assert not discovery.looks_like_adapter(other)


def test_hosts_in() -> None:
    hosts = discovery.hosts_in("192.0.2.0/29", skip=["192.0.2.3"])
    assert hosts == ["192.0.2.1", "192.0.2.2", "192.0.2.4", "192.0.2.5", "192.0.2.6"]
    assert len(discovery.hosts_in("192.0.2.17/24")) == 254
    with pytest.raises(ValueError, match="too large"):
        discovery.hosts_in("10.0.0.0/16")
    with pytest.raises(ValueError, match="IPv4"):
        discovery.hosts_in("2001:db8::/120")


async def test_scan_finds_the_adapter(sim: Simulator, session: aiohttp.ClientSession) -> None:
    found = await discovery.scan(session, ["127.0.0.1"], port=sim.port)
    assert found == ["127.0.0.1"]
    assert sim.requests[0]["body"] == discovery.PROBE_BODY
    assert len(sim.requests) == 1


async def test_scan_ignores_everything_else(sim: Simulator, session: aiohttp.ClientSession) -> None:
    assert await discovery.scan(session, ["127.0.0.1"], port=free_port()) == []  # refused
    sim.raw_reply = (404, "not found")
    assert await discovery.scan(session, ["127.0.0.1"], port=sim.port) == []
    sim.raw_reply = (200, "<html>")
    assert await discovery.scan(session, ["127.0.0.1"], port=sim.port) == []
    sim.raw_reply = (200, '{"result": "OK"}')
    assert await discovery.scan(session, ["127.0.0.1"], port=sim.port) == []
    sim.raw_reply = None
    sim.delay = 0.5
    assert await discovery.scan(session, ["127.0.0.1"], port=sim.port, timeout=0.1) == []


async def test_scan_asks_each_host_once(sim: Simulator, session: aiohttp.ClientSession) -> None:
    await discovery.scan(session, ["127.0.0.1", "127.0.0.1"], port=sim.port)
    assert len(sim.requests) == 1
