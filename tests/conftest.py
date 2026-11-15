"""Shared pytest fixtures: a simulated adapter and a transport pointing at it."""

from __future__ import annotations

from collections.abc import AsyncIterator

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from custom_components.airstage_direct.api.transport import Transport
from tests.simulator import Simulator


@pytest.fixture(autouse=True)
def _real_sockets(socket_enabled: None) -> None:
    """Every test talks to the simulator, and the integration's push listener binds
    a port, over real localhost sockets. Home Assistant's test plugin blocks sockets
    for every test; pytest-socket's socket_enabled fixture switches them back on."""


@pytest.fixture
async def sim() -> AsyncIterator[Simulator]:
    simulator = Simulator()
    server = TestServer(simulator.app, host="127.0.0.1")
    await server.start_server()
    assert server.port is not None
    simulator.port = server.port
    try:
        yield simulator
    finally:
        await server.close()


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as client:
        yield client


@pytest.fixture
def transport(sim: Simulator, session: aiohttp.ClientSession) -> Transport:
    return Transport(session, "127.0.0.1", sim.mac, port=sim.port, gap=0.1, timeout=2.0)
