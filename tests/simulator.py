"""A simulated Airstage adapter (aiohttp), behaving the way the real ones did.

Measured on firmware L45-2 (docs/measurements.md):
- an empty or lowercase device_id gets {"result": "NG", "error": "0002"};
- unknown names are dropped silently; a request with no known name gets 0013
  on sub-id 0, and result OK with an empty value on sub-id 1;
- wla_* values are JSON numbers or strings as recorded, indoor values strings;
- answers can be cut short (known names left out, still OK): set `cut_short`
  (names left out of any multi-name answer) or `cut_after` (keep only the first
  N values of a multi-name answer).

It records every request, counts overlapping requests (the transport must never
cause any), and can delay answers, drop the connection, or refuse writes.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from aiohttp import web

FIXTURES = Path(__file__).parent / "fixtures"
MAC = "AABBCCDDEEFF"


def load_fixture(name: str) -> Any:
    """A JSON fixture: an object for units and answers, a list for pushes."""
    return json.loads((FIXTURES / name).read_text())


COUNTERS = ("iu_cmp_time", "ou_fan1_time", "ou_fan2_time")
SIGNAL = ("iu_suct_tmp", "iu_indoor_tmp_crct", "iu_heat_ex_tmp", "iu_fan_rot1")
OUTDOOR = (
    "ou_outdoor_tmp", "ou_disc_tmp", "ou_heat_ex_tmp", "ou_heat_ex_tmp_out", "ou_fan_rot1",
    "ou_cmp_freq", "ou_exp_vlv1", "ou_exp_vlv5", "ou_four_way_vlv", "ou_current",
)  # fmt: skip


class MonitorSchedule:
    """The service monitor on a fast clock, as measured: at each refresh the indoor
    values change and the outdoor values read "" for `blank` seconds; the run-time
    counters come back after `counters` seconds."""

    def __init__(
        self, period: float, offset: float, blank: float, counters: float, start: float = 0.0
    ) -> None:
        self.period, self.offset, self.blank, self.counters = period, offset, blank, counters
        self.start = start

    def values(self, now: float) -> dict[str, str]:
        elapsed = now - self.start - self.offset
        cycle = int(elapsed // self.period)
        since = elapsed - cycle * self.period
        out: dict[str, str] = {}
        for i, name in enumerate(SIGNAL):
            out[name] = str(2000 + 25 * cycle + i)
        for i, name in enumerate(OUTDOOR):
            out[name] = "" if since < self.blank else str(100 + cycle + i)
        for name in COUNTERS:
            out[name] = "" if since < self.counters else str(10 + cycle)
        return out


class Simulator:
    def __init__(self, fixture: str = "unit_downstairs_14kj.json") -> None:
        data = load_fixture(fixture)
        self.mac: str = data["device_id"]
        self.indoor: dict[str, Any] = dict(data["indoor"])
        self.adapter: dict[str, Any] = dict(data["adapter"])
        self.requests: list[dict[str, Any]] = []
        self.forbidden: list[str] = []
        self.cut_short: set[str] = set()
        self.cut_after: int | None = None
        self.write_res: Any = "ack"
        self.ignore_writes = False  # answer ack but keep the old value
        self.refuse_when_off = False  # refuse writes other than iu_onoff while off (real units)
        self.raw_reply: tuple[int, str] | None = None  # any status and body, verbatim
        self.single_error: str | None = None  # NG code for every one-name GetParam
        self.monitor: MonitorSchedule | None = None
        self.clock_set: int | None = None  # the last SetTime
        self.delay = 0.0
        self.drop_next = 0  # close the connection without answering, N times
        self.overlaps = 0
        self.port = 0  # set by the test server fixture
        self._busy = 0
        self.app = web.Application()
        self.app.router.add_post("/{endpoint}", self._handle)

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        self._busy += 1
        if self._busy > 1:
            self.overlaps += 1
        try:
            endpoint = request.match_info["endpoint"]
            body = await request.json()
            self.requests.append(
                {"endpoint": endpoint, "body": body, "t": asyncio.get_running_loop().time()}
            )
            if self.drop_next:
                self.drop_next -= 1
                if request.transport is not None:
                    request.transport.close()
                return web.Response(status=500)
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.raw_reply is not None:
                return web.Response(status=self.raw_reply[0], text=self.raw_reply[1])
            return web.json_response(self.answer(endpoint, body))
        finally:
            self._busy -= 1

    def answer(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        if endpoint == "Register":
            ok = bool(body.get("Key")) and bool(body.get("IP")) and bool(body.get("Type"))
            return {"result": "OK" if ok else "NG", "error": "" if ok else "0002"}
        if endpoint == "Unregister":
            ok = bool(body.get("Key"))
            return {"result": "OK" if ok else "NG", "error": "" if ok else "0002"}
        if endpoint == "SetTime":
            ok = body.get("device_id") == self.mac and isinstance(body.get("unix_time"), int)
            if ok:
                self.clock_set = int(body["unix_time"])
            return {**{k: body.get(k) for k in ("device_id", "device_sub_id", "req_id",
                       "modified_by")}, "result": "OK" if ok else "NG",
                    "error": "" if ok else "0002"}  # fmt: skip
        if endpoint not in ("GetParam", "SetParam", "SetPollingList"):
            self.forbidden.append(endpoint)
            return {"result": "NG", "error": "9999"}
        if body.get("device_id") != self.mac:
            return {"result": "NG", "error": "0002"}
        if endpoint == "SetPollingList":
            return {"result": "OK", "error": ""}
        base = {
            k: body.get(k)
            for k in ("device_id", "device_sub_id", "req_id", "modified_by", "set_level")
        }
        if endpoint == "SetParam":
            if body.get("set_level") != "02":
                self.forbidden.append(f"SetParam level {body.get('set_level')}")
            values = body.get("value") or {}
            write_res = self.write_res
            if (
                self.refuse_when_off
                and self.indoor.get("iu_onoff") == "0"
                and (set(values) - {"iu_onoff"})
            ):
                write_res = "unknown"
            if write_res == "ack" and not self.ignore_writes:
                self.indoor.update({k: str(v) for k, v in values.items()})
            echo = {k: self.indoor.get(k, "") for k in values}
            return {"value": echo, **base, "write_res": write_res, "read_res": "ack",
                    "cause": "", "result": "OK", "error": ""}  # fmt: skip
        names: list[str] = body.get("list") or []
        if self.monitor is not None:
            self.indoor.update(self.monitor.values(asyncio.get_running_loop().time()))
        if self.single_error and len(names) == 1:
            return {**base, "result": "NG", "error": self.single_error}
        sub_id = int(body.get("device_sub_id", 0))
        table = self.adapter if sub_id == 1 else self.indoor
        value = {n: table[n] for n in names if n in table}
        if len(names) > 1:
            value = {n: v for n, v in value.items() if n not in self.cut_short}
            if self.cut_after is not None:
                value = dict(list(value.items())[: self.cut_after])
        if not value and sub_id == 0:
            return {"value": {}, **base, "cause": "", "result": "NG", "error": "0013"}
        return {"value": value, **base, "read_res": "ack", "cause": "", "result": "OK", "error": ""}

    def get_lists(self) -> list[list[str]]:
        return [r["body"]["list"] for r in self.requests if r["endpoint"] == "GetParam"]
