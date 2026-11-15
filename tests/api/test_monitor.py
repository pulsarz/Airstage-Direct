"""Monitor scheduling against the simulator on a fast clock.

The real cycle is 300 s with the refresh at about 100 s (Downstairs) or 298 s
(Upstairs), outdoor values blank for up to 50 s and the read at +60 s. Here the
cycle is 2 s and every time is scaled by 2/300.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Any

import pytest

from custom_components.airstage_direct.api import monitor, objects
from custom_components.airstage_direct.api.device import Device
from custom_components.airstage_direct.api.monitor import MonitorScheduler
from custom_components.airstage_direct.api.transport import Transport
from tests.simulator import COUNTERS, OUTDOOR, MonitorSchedule, Simulator

PERIOD = 2.0
SCALE = PERIOD / 300
BLANK = 45 * SCALE  # 0.30 s
COUNTERS_BACK = 50 * SCALE  # 0.33 s
READ_DELAY = 60 * SCALE  # 0.40 s
LEARN_INTERVAL = 0.1


def test_next_read_time_wraps() -> None:
    assert monitor.next_read_time(1000.0, 100.0, 60.0) == 1060.0
    assert monitor.next_read_time(1061.0, 100.0, 60.0) == 1360.0
    # Upstairs: refresh at 298 s, read at 58 s past the next mark
    assert monitor.next_read_time(1198.0, 298.0, 60.0) == 1258.0
    assert monitor.next_read_time(900.0, 0.0, 0.0) == 1200.0


def test_circular_mean() -> None:
    mean = monitor.circular_mean([299.0, 1.0])
    assert mean is not None
    assert min(mean, 300 - mean) < 1e-6
    assert monitor.circular_mean([]) is None
    assert monitor.circular_mean([0.0, 150.0]) is None  # opposite: no mean


def test_missed_window() -> None:
    assert monitor.missed_window({"ou_cmp_freq"}, {"ou_cmp_freq": ""})
    assert not monitor.missed_window({"ou_cmp_freq"}, {"ou_cmp_freq": "30"})
    assert not monitor.missed_window(set(), {"ou_cmp_freq": ""})
    assert not monitor.missed_window({"ou_cmp_freq"}, {})
    assert not monitor.missed_window({"iu_suct_tmp"}, {"iu_suct_tmp": ""})  # indoor
    filled = monitor.filled_outdoor({"ou_cmp_freq": "0", "ou_current": "", "iu_suct_tmp": "1"})
    assert filled == {"ou_cmp_freq"}


async def test_misses_do_not_count_while_the_unit_is_off(
    transport: Transport, sim: Simulator
) -> None:
    device = Device(transport)
    device.subscribe(["ou_cmp_freq"])
    sched = scheduler(device, offset=0.0)
    sched._seen_outdoor = {"ou_cmp_freq"}
    sim.indoor["ou_cmp_freq"] = ""
    device.apply({"iu_onoff": "0"})
    events: list[str] = []
    sched.on_event = lambda kind, data: events.append(kind)
    await sched.read_once()
    await sched.read_once()
    assert "missed_window" not in events
    device.apply({"iu_onoff": "1"})
    await sched.read_once()
    await sched.read_once()
    assert events.count("missed_window") == 2
    assert sched.offset is None  # learn again


def scheduler(device: Device, **kw: Any) -> MonitorScheduler:
    loop = asyncio.get_running_loop()
    return MonitorScheduler(
        device,
        period=PERIOD,
        read_delay=READ_DELAY,
        learn_interval=LEARN_INTERVAL,
        clock=loop.time,
        **kw,
    )


def phase(t: float, start: float) -> float:
    return (t - start) % PERIOD


@pytest.mark.parametrize("offset_s", [100.0, 298.0])  # Downstairs, Upstairs (wraps)
async def test_one_read_per_cycle_after_the_last_value(
    transport: Transport, sim: Simulator, offset_s: float
) -> None:
    loop = asyncio.get_running_loop()
    start = loop.time()
    sim.monitor = MonitorSchedule(PERIOD, offset_s * SCALE, BLANK, COUNTERS_BACK, start)
    device = Device(transport)
    device.subscribe(objects.MONITOR)
    sched = scheduler(device)
    reads: list[tuple[float, dict[str, Any]]] = []
    events: list[str] = []

    def on_event(kind: str, data: dict[str, Any]) -> None:
        events.append(kind)
        if kind == "read":
            reads.append((loop.time(), dict(device.raw)))

    sched.on_event = on_event
    stop = asyncio.Event()
    task = asyncio.create_task(sched.run(stop))
    await asyncio.sleep(4 * PERIOD)
    stop.set()
    await asyncio.wait_for(task, 2)

    assert events[0] == "learned"
    assert sched.offset is not None
    true_offset = offset_s * SCALE  # relative to `start`
    err = (sched.offset - (start + true_offset)) % PERIOD  # the scheduler uses the clock
    assert err < LEARN_INTERVAL + 0.15, f"learned {sched.offset}, true {true_offset}"
    assert "missed_window" not in events
    assert 2 <= len(reads) <= 4  # learning took up to a cycle, then one read per cycle
    for (a, _), (b, _) in itertools.pairwise(reads):
        assert abs((b - a) - PERIOD) < 0.2, "exactly one read per cycle"
    for t, raw in reads:
        since_refresh = (phase(t, start) - true_offset) % PERIOD
        assert COUNTERS_BACK <= since_refresh < PERIOD / 2, "read after the last value"
        assert all(raw[n] != "" for n in OUTDOOR), "no blank outdoor values"
        assert all(raw[n] != "" for n in COUNTERS), "counters are back"
    assert sim.overlaps == 0


async def test_relearns_when_the_refresh_moves(transport: Transport, sim: Simulator) -> None:
    loop = asyncio.get_running_loop()
    start = loop.time()
    schedule = MonitorSchedule(PERIOD, 100 * SCALE, BLANK, COUNTERS_BACK, start)
    sim.monitor = schedule
    device = Device(transport)
    device.subscribe(objects.MONITOR)
    sched = scheduler(device)
    events: list[str] = []
    sched.on_event = lambda kind, data: events.append(kind)
    stop = asyncio.Event()
    task = asyncio.create_task(sched.run(stop))
    while sched.offset is None:
        await asyncio.sleep(0.05)
    first = sched.offset
    # The adapter's clock jumps: its refresh now lands just before our read.
    schedule.offset = (first + READ_DELAY - 0.1 - start) % PERIOD
    await asyncio.sleep(5 * PERIOD)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert events.count("missed_window") >= 2
    assert events.count("learned") >= 2
    assert sched.offset is not None
    assert (sched.offset - (start + schedule.offset)) % PERIOD < LEARN_INTERVAL + 0.15


async def test_no_change_falls_back_to_unsynced_reads(transport: Transport, sim: Simulator) -> None:
    device = Device(transport)
    device.subscribe(["iu_suct_tmp"])
    sched = scheduler(device, learn_timeout=0.5)
    events: list[str] = []
    sched.on_event = lambda kind, data: events.append(kind)
    stop = asyncio.Event()
    task = asyncio.create_task(sched.run(stop))
    await asyncio.sleep(0.5 + READ_DELAY + 0.4)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert events[:2] == ["learn_timeout", "unsynced"]
    assert "read" in events


async def test_stops_promptly_while_learning(transport: Transport, sim: Simulator) -> None:
    sched = scheduler(Device(transport))
    stop = asyncio.Event()
    task = asyncio.create_task(sched.run(stop))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, 0.5)
    assert sched.offset is None


async def test_nothing_subscribed_means_no_reads(transport: Transport, sim: Simulator) -> None:
    sched = scheduler(Device(transport), offset=0.0)
    assert await sched.read_once() == set()
    assert sim.requests == []


async def test_learning_needs_signal_objects(transport: Transport, sim: Simulator) -> None:
    device = Device(transport)
    device.capabilities = None
    from custom_components.airstage_direct.api.device import Capabilities

    device.capabilities = Capabilities(unsupported=set(monitor.SIGNAL))
    sched = scheduler(device)
    assert await sched.learn() is None
    assert sim.requests == []


async def test_idle_without_subscriptions_then_starts(transport: Transport, sim: Simulator) -> None:
    loop = asyncio.get_running_loop()
    sim.monitor = MonitorSchedule(PERIOD, 100 * SCALE, BLANK, COUNTERS_BACK, loop.time())
    device = Device(transport)
    sched = scheduler(device, idle_interval=0.1)
    events: list[str] = []
    sched.on_event = lambda kind, data: events.append(kind)
    stop = asyncio.Event()
    task = asyncio.create_task(sched.run(stop))
    await asyncio.sleep(1.0)
    assert sim.requests == []  # nothing subscribed: not even learning polls
    assert not sched.active()
    device.subscribe(["ou_cmp_freq"])
    await asyncio.sleep(1.5 * PERIOD)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert "learned" in events
    assert "read" in events


async def test_a_stored_offset_skips_learning(transport: Transport, sim: Simulator) -> None:
    loop = asyncio.get_running_loop()
    sim.monitor = MonitorSchedule(PERIOD, 100 * SCALE, BLANK, COUNTERS_BACK, loop.time())
    device = Device(transport)
    device.subscribe(["ou_cmp_freq"])
    sched = scheduler(device, offset=(loop.time() + 0.1) % PERIOD)
    events: list[str] = []
    sched.on_event = lambda kind, data: events.append(kind)
    stop = asyncio.Event()
    task = asyncio.create_task(sched.run(stop))
    await asyncio.sleep(PERIOD)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert "learned" not in events
    assert sched.learn_reads == 0
    assert "read" in events
