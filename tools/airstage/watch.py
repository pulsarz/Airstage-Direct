"""watch: the API's push and monitor scheduling, live, with a timestamped log.

- One push listener (api.push.PushListener, port 10275) for all units; every
  request it receives is logged verbatim.
- With --register, one api.push.PushSession per unit registers with the tools'
  own key, feeds pushes into the unit's Device, registers again after 7 minutes
  of silence, and unregisters on exit: Ctrl-C, SIGTERM, the stop file, or the
  end of --duration. --polling-list also sends SetPollingList (shared with the
  cloud and the app; the last writer wins).
- One api.monitor.MonitorScheduler per unit learns the refresh offset (polling
  every --interval seconds) and then reads the monitor once per 5 minutes at
  offset + 60 s. Every scheduled read is logged with its phase and raw values.
- --state-interval also polls state objects at a fixed interval, for sweeps.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import signal
import socket
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

import aiohttp

from .common import OUT_DIR, Unit, stamp

if TYPE_CHECKING:
    from custom_components.airstage_direct import api
else:
    from .apiload import load_api

    api = load_api()

MONITOR_PERIOD = 300.0
MIN_INTERVAL = 5.0
MIN_STATE_INTERVAL = 3.0
DEFAULT_KEY = api.push.TOOLS_PUSH_KEY

# State and settings objects for code sweeps (set from the app or remote, read
# back). Polled because the adapter's push list may not cover all of them.
STATE_NAMES = [
    "iu_onoff", "iu_op_mode", "iu_set_tmp", "iu_fan_spd",
    "iu_af_dir_vrt", "iu_af_swg_vrt", "iu_af_dir_hrz", "iu_af_swg_hrz",
    "iu_economy", "iu_powerful", "iu_min_heat", "iu_fan_ctrl", "ou_low_noise",
    "iu_wifi_led", "iu_hmn_det_auto_save", "iu_hmn_det_auto_off",
]  # fmt: skip
MONITOR_NAMES = ["iu_onoff", *api.objects.MONITOR]


def push_indoor_tmp(raw: Any) -> float | None:
    if raw is None:
        return None
    value = api.codecs.decode(api.objects.Codec.TEMP_OFFSET, raw)
    return value if isinstance(value, float) else None


def local_ip_towards(host: str) -> str:
    """The address of this machine as the adapter would see it (no packet sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((host, 80))
        return str(s.getsockname()[0])


def load_polling_list(arg: str) -> dict[str, list[str]]:
    """JSON object, or @path to a JSON file, with init_ntfy/change_monitor/cycle_monitor."""
    if not arg:
        return {}
    text = Path(arg[1:]).read_text() if arg.startswith("@") else arg
    data = json.loads(text)
    if not isinstance(data, dict):
        raise SystemExit("--polling-list must be a JSON object")
    return {str(k): [str(n) for n in v] for k, v in data.items() if not k.startswith("_")}


def circular_phase(times: list[float], period: float = MONITOR_PERIOD) -> float | None:
    """Mean of t mod period on a circle, so 299 s and 1 s average to 0 s."""
    if not times:
        return None
    ang = [2 * math.pi * (t % period) / period for t in times]
    x = sum(math.cos(a) for a in ang)
    y = sum(math.sin(a) for a in ang)
    if math.hypot(x, y) < 1e-9:
        return None
    return (math.atan2(y, x) / (2 * math.pi) * period) % period


class Log:
    def __init__(self, path: Path, echo: bool = True) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fh: TextIO = path.open("a", encoding="utf-8")
        self.echo = echo

    def write(self, kind: str, **data: Any) -> None:
        rec = {"t": round(time.time(), 3), "kind": kind, **data}
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._fh.flush()
        if self.echo:
            text = json.dumps(data, ensure_ascii=False)[:300]
            print(time.strftime("%H:%M:%S"), kind, text, flush=True)

    def close(self) -> None:
        self._fh.close()


class Poller:
    """Polls a set of names at a fixed interval and logs every change."""

    def __init__(
        self,
        t: api.transport.Transport,
        unit: Unit,
        log: Log,
        interval: float,
        names: list[str],
        kind: str = "state",
    ) -> None:
        self.t, self.unit, self.log, self.interval = t, unit, log, interval
        self.names = names
        self.kind = kind
        self.last: dict[str, Any] = {}
        self.last_t: float | None = None
        self.changes: dict[str, list[float]] = {}

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            t_start = time.time()
            values: dict[str, Any] = {}
            try:
                values = (await self.t.read(self.names)).values
            except api.transport.AirstageError as err:
                self.log.write("poll_error", unit=self.unit.name, error=str(err))
            self.record(values, time.time())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    stop.wait(), max(0.0, self.interval - (time.time() - t_start))
                )

    def record(self, values: dict[str, Any], t: float) -> None:
        if not values:
            return
        if not self.last:
            self.log.write(f"{self.kind}_initial", unit=self.unit.name, values=values)
        else:
            changed = {
                k: [self.last[k], v]
                for k, v in values.items()
                if k in self.last and self.last[k] != v
            }
            if changed:
                lo = self.last_t or t
                self.log.write(
                    f"{self.kind}_change",
                    unit=self.unit.name,
                    changed=changed,
                    after=round(lo, 3),
                    before=round(t, 3),
                    phase_s=round(((lo + t) / 2) % MONITOR_PERIOD, 1),
                )
                for k in changed:
                    self.changes.setdefault(k, []).append((lo + t) / 2)
        self.last, self.last_t = {**self.last, **values}, t

    def summary(self) -> dict[str, Any]:
        return {
            name: {
                "changes": len(ts),
                "phase_s": (round(p, 1) if (p := circular_phase(ts)) is not None else None),
            }
            for name, ts in sorted(self.changes.items())
        }


def check_args(args: argparse.Namespace) -> dict[str, list[str]]:
    if args.interval < MIN_INTERVAL:
        raise SystemExit(f"--interval must be at least {MIN_INTERVAL} s")
    if args.state_interval and args.state_interval < MIN_STATE_INTERVAL:
        raise SystemExit(f"--state-interval must be at least {MIN_STATE_INTERVAL} s")
    if len(args.key) > 36:
        raise SystemExit("--key must be at most 36 characters")
    polling_list = load_polling_list(args.polling_list)
    if polling_list and not args.register:
        raise SystemExit("--polling-list needs --register (it is sent after Register)")
    return polling_list


def monitor_logger(log: Log, unit: Unit, device: api.device.Device) -> api.monitor.EventHook:
    """Log scheduler events; for reads, the phase and the raw monitor values."""

    def on_event(kind: str, data: dict[str, Any]) -> None:
        if kind == "read":
            now = time.time()
            raw = {n: device.raw.get(n) for n in MONITOR_NAMES}
            data = {**data, "phase_s": round(now % MONITOR_PERIOD, 1), "raw": raw}
        log.write(f"monitor_{kind}", unit=unit.name, **data)

    return on_event


async def run(args: argparse.Namespace, units: list[Unit], *, signals: bool = True) -> int:
    polling_list = check_args(args)
    out = Path(args.out) if args.out else OUT_DIR
    log = Log(out / f"watch-{stamp()}.jsonl")
    print(f"logging to {log.path}")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM) if signals else ():
        # Not available outside the main thread (tests).
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)

    by_ip = {u.host: u.name for u in units}
    listener: api.push.PushListener | None = None
    sessions: list[api.push.PushSession] = []
    schedulers: list[tuple[Unit, api.monitor.MonitorScheduler]] = []
    pollers: list[Poller] = []
    tasks: list[asyncio.Task[None]] = []
    async with aiohttp.ClientSession() as session:
        try:
            if not args.no_listen:
                listener = api.push.PushListener(args.listen_host, args.listen_port)

                def on_raw(source: str, path: str, body: Any) -> None:
                    extra: dict[str, Any] = {}
                    value = body.get("value") if isinstance(body, dict) else None
                    if isinstance(value, dict) and "iu_indoor_tmp" in value:
                        extra["iu_indoor_tmp_c"] = push_indoor_tmp(value["iu_indoor_tmp"])
                    log.write("push", unit=by_ip.get(source), src=source, path=path, body=body,
                              **extra)  # fmt: skip

                listener.raw_observer = on_raw
                await listener.start()
                log.write("listening", host=args.listen_host, port=args.listen_port)
            for u in units:
                t = api.transport.Transport(
                    session, u.host, u.mac, port=args.port, timeout=args.timeout,
                    gap=args.gap, read_only=True,
                )  # fmt: skip
                try:
                    await t.resolve_device_id()
                except api.transport.AirstageError as err:
                    log.write("unit_error", unit=u.name, error=str(err))
                    continue
                device = api.device.Device(t)
                if args.register and listener is not None:
                    me = args.my_ip or local_ip_towards(u.host)
                    push_session = api.push.PushSession(
                        device, listener, args.key, me, allow_tools_key=True
                    )

                    def on_push_event(kind: str, data: dict[str, Any], name: str = u.name) -> None:
                        if kind != "push":  # the raw push is logged by the listener
                            log.write(kind, unit=name, **data)

                    push_session.on_event = on_push_event
                    try:
                        await push_session.start()
                        sessions.append(push_session)
                    except api.transport.AirstageError as err:
                        log.write("register_failed", unit=u.name, error=str(err))
                    if polling_list and push_session in sessions:
                        try:
                            answer = await t.set_polling_list(polling_list)
                            log.write("polling_list_set", unit=u.name, lists=polling_list,
                                      answer=answer.payload)  # fmt: skip
                        except api.transport.AirstageError as err:
                            log.write("polling_list_failed", unit=u.name, error=str(err))
                if not args.no_monitor:
                    sched = api.monitor.MonitorScheduler(
                        device, lambda: MONITOR_NAMES, learn_interval=args.interval
                    )
                    sched.on_event = monitor_logger(log, u, device)
                    schedulers.append((u, sched))
                if args.state_interval:
                    pollers.append(Poller(t, u, log, args.state_interval, STATE_NAMES))
            tasks = [asyncio.create_task(s.run(stop)) for s in sessions]
            tasks += [asyncio.create_task(s.run(stop)) for _, s in schedulers]
            tasks += [asyncio.create_task(p.run(stop)) for p in pollers]
            deadline = time.time() + args.duration if args.duration else None
            while not stop.is_set():
                if deadline and time.time() >= deadline:
                    break
                if args.stop_file and Path(args.stop_file).exists():
                    log.write("stop_file_seen", path=args.stop_file)
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), 0.5)
        finally:
            stop.set()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for push_session in sessions:
                await push_session.stop()  # logs "unregistered" or "unregister_failed"
            if listener is not None:
                await listener.stop()
            for u, sched in schedulers:
                log.write("monitor_summary", unit=u.name, offset_s=sched.offset,
                          reads=sched.reads, learn_reads=sched.learn_reads)  # fmt: skip
            for p in pollers:
                log.write(f"{p.kind}_phase_summary", unit=p.unit.name, objects=p.summary(),
                          resolution_s=p.interval)  # fmt: skip
            log.close()
    return 0
