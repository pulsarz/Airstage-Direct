"""the measurement tools, running on the API against the simulator."""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
from pathlib import Path

import aiohttp
import pytest

from custom_components.airstage_direct.api import transport as tr
from tests.simulator import Simulator
from tools.airstage import bench, dump, watch
from tools.airstage.__main__ import build_parser
from tools.airstage.common import Unit, parse_unit_arg
from tools.airstage.make_fixtures import Redactor

GAP = "0.1"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def unit(sim: Simulator, monkeypatch: pytest.MonkeyPatch) -> Unit:
    monkeypatch.setattr(tr, "arp_mac", lambda host: sim.mac)
    return Unit("Test", "127.0.0.1")


def args_for(cmd: list[str], sim: Simulator) -> argparse.Namespace:
    args = build_parser().parse_args(cmd)
    args.port = sim.port
    return args


# -- dump ---------------------------------------------------------------------------


async def test_dump_unit(sim: Simulator, session: aiohttp.ClientSession, unit: Unit) -> None:
    sim.cut_short = {"ou_four_way_vlv"}
    r = await dump.dump_unit(session, unit, functions=False, gap=0.1, timeout=2, port=sim.port)
    assert "error" not in r
    assert r["device_id"] == sim.mac
    assert r["indoor"]["values"]["iu_set_tmp"] == "220"
    assert "iu_has_watch" in r["indoor"]["unknown_names"]
    assert r["indoor"]["lost_in_batch"] == ["ou_four_way_vlv"]
    assert "iu_has_ai_eco" in r["indoor"]["summary"]["unsupported_65535"]
    assert "wla_info" in r["adapter"]["unknown_names"]
    assert r["requests"] == len(sim.requests)  # NG answers (the first 0002) are recorded too
    assert {q["endpoint"] for q in sim.requests} == {"GetParam"}
    assert all(q["body"]["set_level"] == "03" for q in sim.requests)
    sent = {n for lst in sim.get_lists() for n in lst}
    assert not sent & {"iu_fld_set_start", "iu_test_op"}
    assert not any(n.startswith("iu_fld_set") for n in sent)
    assert (sim.overlaps, sim.forbidden) == (0, [])


async def test_dump_functions_opt_in(
    sim: Simulator, session: aiohttp.ClientSession, unit: Unit
) -> None:
    sim.indoor["iu_fld_set11"] = "00"
    r = await dump.dump_unit(session, unit, functions=True, gap=0.1, timeout=2, port=sim.port)
    assert r["function_settings"]["values"] == {"iu_fld_set11": "00"}


async def test_dump_unreachable(session: aiohttp.ClientSession) -> None:
    r = await dump.dump_unit(
        session, Unit("Gone", "127.0.0.1"), functions=False, gap=0.1, timeout=0.5,
        port=free_port(),
    )  # fmt: skip
    assert "AirstageConnectionError" in r["error"]


async def test_dump_cli_writes_file(sim: Simulator, unit: Unit, tmp_path: Path) -> None:
    args = args_for(["dump", "--out", str(tmp_path), "--gap", GAP], sim)
    assert await dump.run(args, [Unit("Living room", "127.0.0.1")]) == 0
    files = list(tmp_path.glob("dump-living-room-*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["unit"] == "Living room"


# -- bench --------------------------------------------------------------------------


def test_stats() -> None:
    s = bench.stats([0.1] * 19 + [1.0])
    assert (s["n"], s["median_ms"], s["p95_ms"], s["max_ms"]) == (20, 100.0, 100.0, 1000.0)
    assert bench.stats([]) == {"n": 0}
    assert len(bench.BENCH_NAMES) == 20


def test_gap_guard() -> None:
    for gap, sweep, allow in ((0.5, [], False), (1.0, [0.5], False), (1.0, [0.05], True)):
        with pytest.raises(SystemExit):
            bench.check_gaps(gap, sweep, allow)
    bench.check_gaps(1.0, [0.5, 0.1], True)


async def test_bench_separates_cut_short_answers(sim: Simulator, transport: tr.Transport) -> None:
    sim.cut_short = {"iu_suct_tmp"}
    r = await bench.run_series(transport, 20, 3)
    assert r["latency"] == {"n": 0}
    assert r["latency_cut_short"]["n"] == 3
    assert r["cut_short"][0]["missing"] == ["iu_suct_tmp"]


async def test_bench_cli(sim: Simulator, unit: Unit, tmp_path: Path) -> None:
    args = args_for(
        ["bench", "--out", str(tmp_path), "--gap", GAP, "--allow-faster", "--sizes", "1,20",
         "--count", "2", "--sweep", "0.2"], sim,
    )  # fmt: skip
    assert await bench.run(args, [unit]) == 0
    r = json.loads(next(tmp_path.glob("bench-*.json")).read_text())
    assert [s["latency"]["n"] for s in r["latency_by_size"]] == [2, 2]
    assert r["rate_sweep"][0]["gap_s"] == 0.2
    assert sim.overlaps == 0


# -- watch --------------------------------------------------------------------------


def test_watch_helpers(tmp_path: Path) -> None:
    assert watch.push_indoor_tmp("7100") == 21.0
    assert watch.push_indoor_tmp("") is None
    p = watch.circular_phase([299.0, 301.0, 599.0, 601.0])
    assert p is not None
    assert min(p, 300 - p) < 0.01
    lists = tmp_path / "l.json"
    lists.write_text(json.dumps({"_note": "x", "cycle_monitor": ["ou_cmp_freq"]}))
    assert watch.load_polling_list(f"@{lists}") == {"cycle_monitor": ["ou_cmp_freq"]}
    assert watch.load_polling_list("") == {}


@pytest.mark.parametrize(
    "extra", [["--interval", "1"], ["--state-interval", "1"], ["--key", "x" * 37],
              ["--polling-list", '{"cycle_monitor": []}']],
)  # fmt: skip
def test_watch_argument_checks(extra: list[str]) -> None:
    with pytest.raises(SystemExit):
        watch.check_args(build_parser().parse_args(["watch", *extra]))


async def test_watch_register_push_and_unregister(
    sim: Simulator, unit: Unit, tmp_path: Path
) -> None:
    listen = free_port()
    stop = tmp_path / "STOP"
    lists = tmp_path / "l.json"
    lists.write_text(json.dumps({"change_monitor": ["iu_onoff"]}))
    args = args_for(
        ["watch", "--out", str(tmp_path), "--gap", GAP, "--interval", "5", "--state-interval",
         "3", "--listen-host", "127.0.0.1", "--listen-port", str(listen), "--register",
         "--my-ip", "192.0.2.5", "--polling-list", f"@{lists}", "--stop-file", str(stop)], sim,
    )  # fmt: skip
    task = asyncio.create_task(watch.run(args, [unit], signals=False))
    await asyncio.sleep(1.0)
    push = {"device_id": sim.mac, "value": {"iu_indoor_tmp": "7150", "iu_onoff": "1"}}
    async with (
        aiohttp.ClientSession() as s,
        s.post(f"http://127.0.0.1:{listen}/NtfyParam", json=push) as resp,
    ):
        assert resp.status == 200
    stop.touch()
    assert await asyncio.wait_for(task, 10) == 0
    eps = [q["endpoint"] for q in sim.requests if q["endpoint"] != "GetParam"]
    assert eps == ["Register", "SetPollingList", "Unregister"]
    assert sim.requests[-1]["endpoint"] == "Unregister"
    reg = next(q["body"] for q in sim.requests if q["endpoint"] == "Register")
    assert reg == {"Type": "1", "Key": watch.DEFAULT_KEY, "IP": "192.0.2.5"}
    log = [
        json.loads(x) for f in tmp_path.glob("watch-*.jsonl") for x in f.read_text().splitlines()
    ]
    kinds = {x["kind"] for x in log}
    for k in ("listening", "registered", "polling_list_set", "state_initial", "push",
              "stop_file_seen", "unregistered", "monitor_summary"):  # fmt: skip
        assert k in kinds, k
    pushed = next(x for x in log if x["kind"] == "push")
    assert (pushed["unit"], pushed["iu_indoor_tmp_c"]) == ("Test", 21.5)
    assert (sim.overlaps, sim.forbidden) == (0, [])


async def test_watch_without_register(sim: Simulator, unit: Unit, tmp_path: Path) -> None:
    args = args_for(
        ["watch", "--out", str(tmp_path), "--gap", GAP, "--duration", "0.5", "--no-listen"], sim
    )
    assert await watch.run(args, [unit], signals=False) == 0
    assert {q["endpoint"] for q in sim.requests} == {"GetParam"}


def test_poller_records_changes_not_absences(tmp_path: Path, transport: tr.Transport) -> None:
    log = watch.Log(tmp_path / "w.jsonl", echo=False)
    p = watch.Poller(transport, Unit("T", "x"), log, 5, ["a", "b"], "state")
    p.record({"a": "1", "b": "1"}, 100.0)
    p.record({"a": "1"}, 110.0)  # b missing: not a change
    p.record({"a": "2", "b": "1"}, 120.0)
    log.close()
    recs = [json.loads(x) for x in (tmp_path / "w.jsonl").read_text().splitlines()]
    assert [r["kind"] for r in recs] == ["state_initial", "state_change"]
    assert recs[1]["changed"] == {"a": ["1", "2"]}
    assert p.summary()["a"]["changes"] == 1


# -- common, fixtures --------------------------------------------------------


def test_parse_unit_arg() -> None:
    assert parse_unit_arg("Bed 1=192.0.2.2,0a:1b:2c:3d:4e:5f") == Unit(
        "Bed 1", "192.0.2.2", mac="0A1B2C3D4E5F"
    )
    with pytest.raises(ValueError, match="expected NAME"):
        parse_unit_arg("nope")


def test_redactor() -> None:
    red = Redactor()
    out = red.walk(
        {"device_id": "0A1B2C3D4E5F", "ip": "10.1.2.3", "wla_serial": "12345678",
         "wla_cnct_ssid": "home", "list": ["0A1B2C3D4E5F", 5]}
    )  # fmt: skip
    assert out == {
        "device_id": "AABBCCDDEEFF", "ip": "192.0.2.10", "wla_serial": "SERIAL00",
        "wla_cnct_ssid": "example-ssid", "list": ["AABBCCDDEEFF", 5],
    }  # fmt: skip
