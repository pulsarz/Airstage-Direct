"""Transport against the simulator: safety, pacing, batching, completeness, errors."""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from custom_components.airstage_direct.api import objects
from custom_components.airstage_direct.api import transport as tr
from custom_components.airstage_direct.api.transport import (
    AirstageConnectionError,
    AirstageResponseError,
    AirstageSafetyError,
    AirstageWriteError,
    Transport,
)
from tests.simulator import Simulator, load_fixture

# -- safety ----------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", ["SetUpdateFile", "debug_info", "SetTimer", "GetTimer"])
async def test_forbidden_endpoints(transport: Transport, endpoint: str, sim: Simulator) -> None:
    with pytest.raises(AirstageSafetyError):
        await transport._post(endpoint, {})
    assert sim.requests == []


@pytest.mark.parametrize(
    ("endpoint", "body"),
    [
        ("GetParam", {"set_level": "02", "list": ["iu_set_tmp"]}),
        ("GetParam", {"set_level": "03", "list": [f"n{i}" for i in range(21)]}),
        ("GetParam", {"set_level": "03", "list": ["iu_fld_set_start"]}),
        ("GetParam", {"set_level": "03", "list": ["iu_test_op"]}),
        ("SetParam", {"set_level": "05", "device_sub_id": 0, "value": {"iu_set_tmp": "220"}}),
        ("SetParam", {"set_level": "04", "device_sub_id": 0, "value": {"iu_set_tmp": "220"}}),
        ("SetParam", {"set_level": "02", "device_sub_id": 1, "value": {"iu_set_tmp": "220"}}),
        ("SetParam", {"set_level": "02", "device_sub_id": 0, "value": {"iu_indoor_tmp": "7000"}}),
        ("SetParam", {"set_level": "02", "device_sub_id": 0, "value": {"iu_fld_set05": "01"}}),
        ("SetParam", {"set_level": "02", "device_sub_id": 0, "value": {}}),
        ("SetPollingList", {"device_id": "x", "cycle_monitor": [], "extra": 1}),
    ],
)
async def test_unsafe_bodies_are_refused(
    transport: Transport, sim: Simulator, endpoint: str, body: dict[str, Any]
) -> None:
    with pytest.raises(AirstageSafetyError):
        await transport._post(endpoint, body)
    assert sim.requests == []


async def test_read_only_transport_refuses_writes(
    sim: Simulator, session: aiohttp.ClientSession
) -> None:
    ro = Transport(session, "127.0.0.1", sim.mac, port=sim.port, gap=0.1, read_only=True)
    with pytest.raises(AirstageSafetyError):
        await ro.set_values({"iu_set_tmp": "220"})
    assert sim.requests == []


def test_gap_floor(session: aiohttp.ClientSession) -> None:
    with pytest.raises(ValueError, match="gap must be"):
        Transport(session, "127.0.0.1", gap=0.01)


# -- pacing ----------------------------------------------------------------------


async def test_one_request_in_flight_and_gap(transport: Transport, sim: Simulator) -> None:
    sim.delay = 0.05
    names = ["iu_set_tmp", "iu_fan_spd", "iu_onoff", "iu_op_mode", "iu_economy", "iu_powerful"]
    await asyncio.gather(*(transport.get([n]) for n in names))
    assert sim.overlaps == 0
    times = [r["t"] for r in sim.requests]
    assert len(times) == 6
    for a, b in itertools.pairwise(times):
        assert b - a >= transport.gap + sim.delay - 0.02


async def test_writes_go_before_queued_reads(transport: Transport, sim: Simulator) -> None:
    sim.delay = 0.05
    first = asyncio.ensure_future(transport.get(["iu_set_tmp"]))
    await asyncio.sleep(0.01)  # first request is in flight
    reads = [asyncio.ensure_future(transport.get([n])) for n in ("iu_fan_spd", "iu_onoff")]
    await asyncio.sleep(0)
    write = asyncio.ensure_future(transport.set_values({"iu_set_tmp": "225"}))
    await asyncio.gather(first, write, *reads)
    order = [r["endpoint"] for r in sim.requests]
    assert order == ["GetParam", "SetParam", "GetParam", "GetParam"]
    assert sim.overlaps == 0


async def test_identical_reads_share_one_request(transport: Transport, sim: Simulator) -> None:
    sim.delay = 0.05
    results = await asyncio.gather(*(transport.get(["iu_set_tmp"]) for _ in range(3)))
    assert len(sim.requests) == 1
    assert all(r.values == {"iu_set_tmp": "220"} for r in results)


async def test_shared_read_failure_reaches_every_caller(
    transport: Transport, sim: Simulator
) -> None:
    sim.delay = 0.05
    results = await asyncio.gather(
        *(transport.get(["no_such_name"]) for _ in range(2)), return_exceptions=True
    )
    assert all(isinstance(r, AirstageResponseError) for r in results)
    assert len(sim.requests) == 1


# -- reading -----------------------------------------------------------------------


async def test_read_batches_with_anchor(transport: Transport, sim: Simulator) -> None:
    names = [o.name for o in objects.INDOOR if o.name not in objects.KNOWN_UNKNOWN_L45]
    result = await transport.read(names)
    lists = sim.get_lists()
    assert all(len(lst) <= objects.MAX_NAMES for lst in lists)
    multi = [lst for lst in lists if len(lst) > 1]
    assert all(lst[0] == objects.ANCHOR_INDOOR for lst in multi)
    for long in ("ou_model", "iu_main_ver", "iu_eep_ver"):
        assert [long] in lists, f"{long} must be read alone"
    assert result.unknown == set()
    assert result.cut_short == set()
    assert set(result.values) == set(names)


async def test_read_recovers_cut_short_names(transport: Transport, sim: Simulator) -> None:
    sim.cut_short = {"ou_four_way_vlv", "ou_exp_vlv5"}
    result = await transport.read(["ou_cmp_freq", "ou_exp_vlv5", "ou_four_way_vlv"])
    assert result.cut_short == {"ou_four_way_vlv", "ou_exp_vlv5"}
    assert result.values["ou_four_way_vlv"] == sim.indoor["ou_four_way_vlv"]
    assert result.unknown == set()


async def test_read_recovers_from_truncated_answers(transport: Transport, sim: Simulator) -> None:
    sim.cut_after = 5
    names = [o.name for o in objects.INDOOR if o.monitor]
    result = await transport.read(names)
    assert set(result.values) == set(names)
    assert result.cut_short
    assert result.requests < 40  # bounded: one retry batch, then singles


async def test_read_classifies_unknown_names(transport: Transport, sim: Simulator) -> None:
    result = await transport.read(["iu_set_tmp", "iu_has_watch", "iu_dry_cool_jp"])
    assert result.unknown == {"iu_has_watch", "iu_dry_cool_jp"}
    assert result.values == {"iu_set_tmp": "220"}


async def test_read_adapter_unknown_names(transport: Transport, sim: Simulator) -> None:
    result = await transport.read(["wla_rssi", "wla_model", "wla_info"], sub_id=1)
    assert result.unknown == {"wla_info"}
    assert result.values["wla_rssi"] == sim.adapter["wla_rssi"]


async def test_read_skips_known_unknown(transport: Transport, sim: Simulator) -> None:
    result = await transport.read(["iu_set_tmp", "iu_has_watch"], skip={"iu_has_watch"})
    assert result.values == {"iu_set_tmp": "220"}
    assert all("iu_has_watch" not in lst for lst in sim.get_lists())
    assert await transport.read([], skip=()) == tr.ReadResult()


async def test_recorded_cut_short_cases_are_recovered(transport: Transport, sim: Simulator) -> None:
    """Replay the batches that came back cut short on the real adapter."""
    for case in load_fixture("batch_cut_short.json").values():
        sim.cut_short = set(case["missing"])
        sim.requests.clear()
        names = [n for n in case["list"] if n != objects.ANCHOR_INDOOR]
        result = await transport.read(names)
        assert set(result.values) == set(names)


# -- device id, errors ---------------------------------------------------------------


async def test_resolve_device_id_from_arp(
    sim: Simulator, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = Transport(session, "127.0.0.1", "", port=sim.port, gap=0.1)
    monkeypatch.setattr(tr, "arp_mac", lambda host: sim.mac)
    assert await t.resolve_device_id() == sim.mac
    assert [r["body"]["device_id"] for r in sim.requests] == ["", sim.mac]


async def test_resolve_device_id_keeps_a_working_one(transport: Transport, sim: Simulator) -> None:
    assert await transport.resolve_device_id() == sim.mac
    assert len(sim.requests) == 1


async def test_resolve_device_id_without_arp(
    sim: Simulator, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = Transport(session, "127.0.0.1", "aabbccddeeff", port=sim.port, gap=0.1)
    monkeypatch.setattr(tr, "arp_mac", lambda host: None)
    with pytest.raises(AirstageResponseError) as err:
        await t.resolve_device_id()
    assert err.value.code == "0002"


async def test_timeout(transport: Transport, sim: Simulator) -> None:
    sim.delay = 0.5
    transport.timeout = 0.1
    with pytest.raises(AirstageConnectionError):
        await transport.get(["iu_set_tmp"])


async def test_dropped_connection_is_retried_once(transport: Transport, sim: Simulator) -> None:
    sim.drop_next = 1
    answer = await transport.get(["iu_set_tmp"])
    assert answer.values == {"iu_set_tmp": "220"}
    sim.drop_next = 2
    with pytest.raises(AirstageConnectionError):
        await transport.get(["iu_fan_spd"])


async def test_refused_connection(session: aiohttp.ClientSession) -> None:
    t = Transport(session, "127.0.0.1", port=1, gap=0.1, timeout=1.0)
    with pytest.raises(AirstageConnectionError):
        await t.get(["iu_set_tmp"])


# -- writing and push registration ---------------------------------------------------


async def test_set_values(transport: Transport, sim: Simulator) -> None:
    await transport.set_values({"iu_set_tmp": "225"})
    body = sim.requests[-1]["body"]
    assert body["set_level"] == "02"
    assert body["device_sub_id"] == 0
    assert body["value"] == {"iu_set_tmp": "225"}
    assert sim.indoor["iu_set_tmp"] == "225"


async def test_write_ack_but_value_unchanged(transport: Transport, sim: Simulator) -> None:
    sim.ignore_writes = True
    with pytest.raises(AirstageWriteError) as err:
        await transport.set_values({"iu_set_tmp": "225"})
    assert err.value.write_res == {"ack_but_value": {"iu_set_tmp": "220"}}


@pytest.mark.parametrize("write_res", ["nack", "timeout", "unknown", {"iu_set_tmp": "unknown"}])
async def test_write_not_accepted(transport: Transport, sim: Simulator, write_res: Any) -> None:
    sim.write_res = write_res
    with pytest.raises(AirstageWriteError):
        await transport.set_values({"iu_set_tmp": "225"})


async def test_register_and_unregister(transport: Transport, sim: Simulator) -> None:
    await transport.register("02A1257A6E01", "192.0.2.5")
    await transport.unregister("02A1257A6E01")
    assert [r["body"] for r in sim.requests] == [
        {"Type": "1", "Key": "02A1257A6E01", "IP": "192.0.2.5"},
        {"Key": "02A1257A6E01"},
    ]
    with pytest.raises(ValueError, match="Register needs"):
        await transport.register("x" * 37, "192.0.2.5")


async def test_set_polling_list(transport: Transport, sim: Simulator) -> None:
    await transport.set_polling_list({"change_monitor": ["iu_onoff"]})
    assert sim.requests[-1]["body"] == {"device_id": sim.mac, "change_monitor": ["iu_onoff"]}
    with pytest.raises(ValueError, match="keys must be"):
        await transport.set_polling_list({"list": ["x"]})
    with pytest.raises(ValueError, match="must be a list"):
        await transport.set_polling_list({"cycle_monitor": "x"})  # type: ignore[dict-item]


# -- helpers ---------------------------------------------------------------------------


def test_format_device_id() -> None:
    assert tr.format_device_id("0a:1b:2c:3d:4e:5f") == "0A1B2C3D4E5F"
    with pytest.raises(ValueError, match="not a MAC"):
        tr.format_device_id("0a:1b")


def test_arp_mac(tmp_path: Path) -> None:
    table = tmp_path / "arp"
    table.write_text(
        "IP address HW type Flags HW address Mask Device\n"
        "192.0.2.5 0x1 0x2 0a:1b:2c:3d:4e:5f * eth0\n"
        "192.0.2.6 0x1 0x0 00:00:00:00:00:00 * eth0\n"
    )
    assert tr.arp_mac("192.0.2.5", table) == "0A1B2C3D4E5F"
    assert tr.arp_mac("192.0.2.6", table) is None
    assert tr.arp_mac("192.0.2.7", table) is None
    assert tr.arp_mac("192.0.2.5", tmp_path / "missing") is None


def test_base_url(session: aiohttp.ClientSession) -> None:
    assert Transport(session, "192.0.2.5").base_url == "http://192.0.2.5"
    assert Transport(session, "192.0.2.5", port=8080).base_url == "http://192.0.2.5:8080"


# -- error paths -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "text", "message"),
    [(500, "{}", "HTTP 500"), (200, "not json", "not JSON"), (200, "[1, 2]", "not a JSON object")],
)
async def test_bad_answers(
    transport: Transport, sim: Simulator, status: int, text: str, message: str
) -> None:
    sim.raw_reply = (status, text)
    with pytest.raises(AirstageConnectionError, match=message):
        await transport.get(["iu_set_tmp"])


async def test_cancelled_request_frees_its_place(transport: Transport, sim: Simulator) -> None:
    sim.delay = 0.2
    first = asyncio.ensure_future(transport.get(["iu_set_tmp"]))
    await asyncio.sleep(0.05)
    queued = asyncio.ensure_future(transport.get(["iu_fan_spd"]))
    await asyncio.sleep(0.01)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    await first
    sim.delay = 0
    assert (await transport.get(["iu_onoff"])).values == {"iu_onoff": sim.indoor["iu_onoff"]}
    assert [r["body"]["list"] for r in sim.requests] == [["iu_set_tmp"], ["iu_onoff"]]


async def test_cancelled_while_sending(transport: Transport, sim: Simulator) -> None:
    sim.delay = 0.3
    task = asyncio.ensure_future(transport.get(["iu_set_tmp"]))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    sim.delay = 0
    assert (await transport.get(["iu_set_tmp"])).values == {"iu_set_tmp": "220"}


async def test_get_needs_names(transport: Transport) -> None:
    with pytest.raises(ValueError, match="no names"):
        await transport.get([])


async def test_read_raises_other_errors_on_single_reads(
    transport: Transport, sim: Simulator
) -> None:
    sim.cut_short = {"iu_fan_spd"}
    sim.single_error = "0002"
    with pytest.raises(AirstageResponseError) as err:
        await transport.read(["iu_set_tmp", "iu_fan_spd"])
    assert err.value.code == "0002"


async def test_resolve_device_id_raises_other_errors(transport: Transport, sim: Simulator) -> None:
    sim.single_error = "0013"
    with pytest.raises(AirstageResponseError) as err:
        await transport.resolve_device_id()
    assert err.value.code == "0013"


def test_arp_mac_malformed_entry(tmp_path: Path) -> None:
    table = tmp_path / "arp"
    table.write_text("header\n192.0.2.5 0x1 0x2 zz:zz:zz * eth0\n")
    assert tr.arp_mac("192.0.2.5", table) is None


# -- SetTime --------------------------------------------------------------------------


async def test_set_time_sends_now(transport: Transport, sim: Simulator) -> None:
    import time

    await transport.set_time()
    body = sim.requests[-1]["body"]
    assert set(body) == tr.SET_TIME_KEYS
    assert isinstance(body["unix_time"], int)
    assert abs(body["unix_time"] - time.time()) < 5
    assert sim.clock_set == body["unix_time"]


@pytest.mark.parametrize(
    "body",
    [
        {"device_id": "", "device_sub_id": 0, "req_id": "", "modified_by": "", "unix_time": 0},
        {"device_id": "", "device_sub_id": 0, "req_id": "", "modified_by": "", "unix_time": "1"},
        {"device_id": "", "device_sub_id": 0, "req_id": "", "modified_by": "", "unix_time": True},
        {"device_id": "", "unix_time": 1},
    ],
)
async def test_set_time_refuses_anything_else(
    transport: Transport, sim: Simulator, body: dict[str, Any]
) -> None:
    with pytest.raises(AirstageSafetyError):
        await transport._post("SetTime", body)
    assert sim.requests == []


async def test_read_only_transport_never_sets_the_clock(
    sim: Simulator, session: aiohttp.ClientSession
) -> None:
    ro = Transport(session, "127.0.0.1", sim.mac, port=sim.port, gap=0.1, read_only=True)
    with pytest.raises(AirstageSafetyError):
        await ro.set_time()
    assert sim.requests == []
