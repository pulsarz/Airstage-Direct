"""Push: parsing, routing, the listener, and registration per adapter."""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest

from custom_components.airstage_direct.api import push as pushmod
from custom_components.airstage_direct.api.device import Device
from custom_components.airstage_direct.api.push import (
    TOOLS_PUSH_KEY,
    Push,
    PushListener,
    PushListenerError,
    PushSession,
    integration_push_key,
    parse_push,
    split_values,
)
from custom_components.airstage_direct.api.transport import Transport
from tests.simulator import Simulator, load_fixture

KEY = integration_push_key("test-instance")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def listener() -> AsyncIterator[PushListener]:
    lst = PushListener("127.0.0.1", free_port())
    await lst.start()
    try:
        yield lst
    finally:
        await lst.stop()


async def post(listener: PushListener, body: Any, raw: bytes | None = None) -> int:
    url = f"http://127.0.0.1:{listener.port}/NtfyParam"
    async with aiohttp.ClientSession() as s:
        kwargs: dict[str, Any] = {"data": raw} if raw is not None else {"json": body}
        async with s.post(url, **kwargs) as resp:
            assert await resp.read() == b""
            return resp.status


# -- keys ----------------------------------------------------------------------------


def test_integration_key_is_stable_and_never_the_tools_key() -> None:
    assert integration_push_key("a") == integration_push_key("a")
    assert integration_push_key("a") != integration_push_key("b")
    assert len(KEY) == 12
    int(KEY, 16)
    for i in range(500):
        assert integration_push_key(str(i)) != TOOLS_PUSH_KEY


def test_session_refuses_the_tools_key(transport: Transport) -> None:
    device = Device(transport)
    with pytest.raises(ValueError, match="take over"):
        PushSession(device, PushListener(), TOOLS_PUSH_KEY, "192.0.2.5")
    PushSession(device, PushListener(), TOOLS_PUSH_KEY, "192.0.2.5", allow_tools_key=True)


# -- parsing -------------------------------------------------------------------------


def test_parse_push() -> None:
    body = {"value": {"iu_set_tmp": "220", "iu_monitor1": "0,0"}, "device_id": "aabbccddeeff",
            "device_sub_id": 0}  # fmt: skip
    push = parse_push(body, "192.0.2.5")
    assert push is not None
    assert (push.device_id, push.sub_id, push.values) == ("AABBCCDDEEFF", 0, {"iu_set_tmp": "220"})
    bad_bodies: list[object] = [
        None, [], {"value": {}}, {"device_id": "x"}, {"value": 1, "device_id": "x"},
        {"value": {}, "device_id": "x", "device_sub_id": "a"},
    ]  # fmt: skip
    for bad in bad_bodies:
        assert parse_push(bad) is None


def test_split_values() -> None:
    indoor, adapter = split_values({"wla_rssi": "-54", "iu_indoor_tmp": "7200"})
    assert indoor == {"iu_indoor_tmp": "7200"}
    assert adapter == {"wla_rssi": "-54"}


# -- listener --------------------------------------------------------------------------


async def test_listener_routes_by_device_id(listener: PushListener) -> None:
    seen: list[Push] = []
    raw: list[tuple[str, str, Any]] = []
    listener.raw_observer = lambda src, path, body: raw.append((src, path, body))
    remove = listener.add("aabbccddeeff", seen.append)
    body = {"value": {"iu_fan_spd": "5"}, "device_id": "AABBCCDDEEFF", "device_sub_id": 0}
    assert await post(listener, body) == 200
    assert [p.values for p in seen] == [{"iu_fan_spd": "5"}]
    assert raw[0][1:] == ("/NtfyParam", body)
    assert await post(listener, {**body, "device_id": "000000000000"}) == 200
    assert listener.unrouted == 1
    assert await post(listener, None, raw=b"not json") == 200
    remove()
    assert await post(listener, body) == 200
    assert len(seen) == 1
    assert listener.unrouted == 2


async def test_listener_survives_a_failing_handler(listener: PushListener) -> None:
    def boom(push: Push) -> None:
        raise RuntimeError("boom")

    listener.add("AABBCCDDEEFF", boom)
    body = {"value": {"iu_fan_spd": "5"}, "device_id": "AABBCCDDEEFF"}
    assert await post(listener, body) == 200


async def test_port_taken(listener: PushListener) -> None:
    second = PushListener("127.0.0.1", listener.port)
    with pytest.raises(PushListenerError):
        await second.start()
    await second.stop()  # harmless when not started


# -- sessions ---------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_session_registers_feeds_the_device_and_unregisters(
    transport: Transport, sim: Simulator, listener: PushListener
) -> None:
    device = Device(transport)
    session = PushSession(device, listener, KEY, "192.0.2.5")
    events: list[str] = []
    session.on_event = lambda kind, data: events.append(kind)
    await session.start()
    assert sim.requests[-1]["body"] == {"Type": "1", "Key": KEY, "IP": "192.0.2.5"}
    body = {"value": {"wla_rssi": "-54", "iu_indoor_tmp": "7200", "iu_monitor1": "0,1"},
            "device_id": sim.mac, "device_sub_id": 0}  # fmt: skip
    await post(listener, body)
    assert device.value("wla_rssi") == -54  # decoded as the adapter value, a number
    assert device.value("iu_indoor_tmp") == 22.0
    assert "iu_monitor1" not in device.values
    assert session.pushes == 1
    await session.stop()
    assert sim.requests[-1]["body"] == {"Key": KEY}
    assert events == ["registered", "push", "unregistered"]
    await post(listener, body)
    assert session.pushes == 1  # no longer routed
    await session.stop()  # second stop: nothing to undo
    assert [r["endpoint"] for r in sim.requests].count("Unregister") == 1


async def test_session_needs_a_device_id(sim: Simulator, session: aiohttp.ClientSession) -> None:
    t = Transport(session, "127.0.0.1", "", port=sim.port, gap=0.1)
    with pytest.raises(ValueError, match="device_id"):
        await PushSession(Device(t), PushListener(), KEY, "192.0.2.5").start()


async def test_silence_triggers_a_new_registration(
    transport: Transport, sim: Simulator, listener: PushListener
) -> None:
    clock = Clock()
    session = PushSession(Device(transport), listener, KEY, "192.0.2.5", clock=clock)
    await session.start()
    clock.now += 400
    assert not await session.check()
    await post(listener, {"value": {"iu_fan_spd": "5"}, "device_id": sim.mac})
    clock.now += 400
    assert not await session.check()  # a push 400 s ago resets the silence
    clock.now += 30
    assert await session.check()
    assert [r["endpoint"] for r in sim.requests].count("Register") == 2
    sim.raw_reply = (500, "")
    clock.now += 500
    assert not await session.check()  # failure is reported, not raised
    sim.raw_reply = None
    await session.stop()


async def test_session_run_checks_until_stopped(
    transport: Transport, sim: Simulator, listener: PushListener
) -> None:
    import asyncio

    clock = Clock()
    session = PushSession(Device(transport), listener, KEY, "192.0.2.5", clock=clock, silence=1)
    await session.start()
    clock.now += 5
    stop = asyncio.Event()
    task = asyncio.create_task(session.run(stop, interval=0.05))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert [r["endpoint"] for r in sim.requests].count("Register") >= 2
    await session.stop()


async def test_unregister_failure_is_reported(
    transport: Transport, sim: Simulator, listener: PushListener
) -> None:
    session = PushSession(Device(transport), listener, KEY, "192.0.2.5")
    events: list[tuple[str, dict[str, Any]]] = []
    session.on_event = lambda kind, data: events.append((kind, data))
    await session.start()
    sim.raw_reply = (500, "")
    await session.stop()
    assert events[-1][0] == "unregister_failed"
    assert session.registered_at is None


async def test_recorded_pushes_apply_cleanly(
    transport: Transport, sim: Simulator, listener: PushListener
) -> None:
    device = Device(transport)
    session = PushSession(device, listener, KEY, "192.0.2.5")
    await session.start()
    for sample in load_fixture("pushes.json"):
        body = dict(sample["body"])
        body["device_id"] = sim.mac
        assert await post(listener, body) == 200
    assert isinstance(device.value("wla_rssi"), int)
    assert "iu_monitor1" not in device.values
    assert session.pushes == len(load_fixture("pushes.json"))
    await session.stop()


def test_constants() -> None:
    assert pushmod.PUSH_PORT == 10275
    assert pushmod.PUSH_SILENCE_S > 300
