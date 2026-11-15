"""dump: read every named object from each adapter and save the raw answers."""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp

from .common import OUT_DIR, Unit, answer_record, slug, stamp, write_json

if TYPE_CHECKING:
    from custom_components.airstage_direct import api
else:
    from .apiload import load_api

    api = load_api()


def classify(values: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"value": [], "unsupported_65535": [], "empty": []}
    for name, raw in values.items():
        missing = api.codecs.sentinel(raw)
        if missing is api.codecs.Missing.UNSUPPORTED:
            out["unsupported_65535"].append(name)
        elif missing is api.codecs.Missing.EMPTY:
            out["empty"].append(name)
        else:
            out["value"].append(name)
    return out


def _part(result: Any) -> dict[str, Any]:
    return {
        "values": result.values,
        "unknown_names": sorted(result.unknown),
        "lost_in_batch": sorted(result.cut_short),
        "requests": result.requests,
        "summary": classify(result.values),
    }


async def dump_unit(
    session: aiohttp.ClientSession,
    unit: Unit,
    *,
    functions: bool,
    gap: float,
    timeout: float,
    port: int = 80,
) -> dict[str, Any]:
    t = api.transport.Transport(
        session, unit.host, unit.mac, port=port, timeout=timeout, gap=gap, read_only=True
    )
    exchanges: list[dict[str, Any]] = []
    t.observer = lambda answer: exchanges.append(answer_record(answer))
    t0 = time.time()
    result: dict[str, Any] = {
        "tool": "airstage dump",
        "unit": unit.name,
        "model": unit.model,
        "host": unit.host,
        "started": t0,
        "started_local": time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(t0)),
        "gap_s": gap,
    }
    try:
        result["device_id"] = await t.resolve_device_id()
        indoor = [o.name for o in api.objects.INDOOR if o.name not in api.objects.NEVER_TOUCH]
        result["indoor"] = _part(await t.read(indoor))
        adapter = [o.name for o in api.objects.ADAPTER]
        result["adapter"] = _part(await t.read(adapter, sub_id=1))
        if functions:
            names = [api.objects.function_setting_name(n) for n in range(100)]
            result["function_settings"] = _part(await t.read(names))
    except api.transport.AirstageError as err:
        result["error"] = f"{type(err).__name__}: {err}"
    result["finished"] = time.time()
    result["requests"] = len(exchanges)
    result["exchanges"] = exchanges
    return result


def print_summary(r: dict[str, Any]) -> None:
    print(f"== {r['unit']} ({r['host']}) ==")
    if "error" in r:
        print(f"   ERROR: {r['error']}")
    if "device_id" not in r:
        return
    print(f"   {r['requests']} requests in {r['finished'] - r['started']:.1f} s")
    for part in ("indoor", "adapter", "function_settings"):
        if part not in r:
            continue
        p = r[part]
        s = p["summary"]
        print(
            f"   {part:18} values={len(s['value'])} 65535={len(s['unsupported_65535'])}"
            f" empty={len(s['empty'])} unknown={len(p['unknown_names'])}"
            f" lost_in_batch={len(p['lost_in_batch'])}"
        )
        if p["unknown_names"]:
            print(f"   {'':18} unknown names: {', '.join(p['unknown_names'])}")
        if p["lost_in_batch"]:
            print(f"   {'':18} lost in batch: {', '.join(p['lost_in_batch'])}")


async def run(args: argparse.Namespace, units: list[Unit]) -> int:
    out_dir = Path(args.out) if args.out else OUT_DIR
    rc = 0
    async with aiohttp.ClientSession() as session:
        for unit in units:
            r = await dump_unit(
                session,
                unit,
                functions=args.functions,
                gap=args.gap,
                timeout=args.timeout,
                port=args.port,
            )
            path = write_json(out_dir / f"dump-{slug(unit.name)}-{stamp()}.json", r)
            print_summary(r)
            print(f"   saved {path}")
            rc |= 1 if "error" in r else 0
    return rc
