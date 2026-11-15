"""bench: GetParam latency by batch size, and (opt-in) the fastest safe rate.

Strictly sequential: one request in flight per adapter. Gaps under 1 s are
refused unless --allow-faster is given; a rate sweep stops at the first error.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp

from .common import OUT_DIR, Unit, slug, stamp, write_json

if TYPE_CHECKING:
    from custom_components.airstage_direct import api
else:
    from .apiload import load_api

    api = load_api()

# The 20-name monitor batch: anchor, iu_onoff, the indoor monitor and the first
# outdoor monitor objects. It came back complete in every measurement poll.
BENCH_NAMES = [api.objects.ANCHOR_INDOOR, "iu_onoff", *api.objects.MONITOR][: api.objects.MAX_NAMES]
SAFE_GAP = 1.0
MIN_GAP = 0.1


def stats(lat: list[float]) -> dict[str, float | int]:
    if not lat:
        return {"n": 0}
    s = sorted(lat)
    p95 = s[min(len(s) - 1, max(0, round(0.95 * len(s)) - 1))]
    return {
        "n": len(s),
        "median_ms": round(statistics.median(s) * 1000, 1),
        "p95_ms": round(p95 * 1000, 1),
        "min_ms": round(s[0] * 1000, 1),
        "max_ms": round(s[-1] * 1000, 1),
    }


async def run_series(t: api.transport.Transport, size: int, count: int) -> dict[str, Any]:
    names = BENCH_NAMES[:size]
    lat: list[float] = []
    lat_short: list[float] = []
    samples: list[list[float]] = []
    short: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for i in range(count):
        t0 = time.perf_counter()
        try:
            answer = await t.get(names)
        except api.transport.AirstageError as err:
            errors.append(
                {
                    "i": i,
                    "error": f"{type(err).__name__}: {err}",
                    "elapsed_s": round(time.perf_counter() - t0, 4),
                }
            )
            continue
        # An answer cut short is not a clean sample of a full request.
        missing = [n for n in names if n not in answer.values]
        samples.append([round(answer.started, 3), round(answer.elapsed, 4), len(missing)])
        if missing:
            lat_short.append(answer.elapsed)
            short.append({"i": i, "missing": missing, "elapsed_s": round(answer.elapsed, 4)})
        else:
            lat.append(answer.elapsed)
    return {
        "size": size,
        "count": count,
        "gap_s": t.gap,
        "latency": stats(lat),
        "errors": errors,
        "cut_short": short,
        "latency_cut_short": stats(lat_short),
        "samples": samples,  # [start unix time, elapsed s, names missing]
        "samples_fields": ["started", "elapsed_s", "missing"],
    }


async def bench_unit(
    session: aiohttp.ClientSession,
    unit: Unit,
    *,
    sizes: list[int],
    count: int,
    gap: float,
    sweep: list[float],
    timeout: float,
    port: int = 80,
) -> dict[str, Any]:
    t = api.transport.Transport(
        session, unit.host, unit.mac, port=port, timeout=timeout, gap=gap, read_only=True
    )
    r: dict[str, Any] = {
        "tool": "airstage bench",
        "unit": unit.name,
        "host": unit.host,
        "started": time.time(),
        "timeout_s": timeout,
    }
    try:
        r["device_id"] = await t.resolve_device_id()
    except api.transport.AirstageError as err:
        r["error"] = str(err)
        return r
    r["latency_by_size"] = []
    for size in sizes:
        print(f"   {unit.name}: {count} x {size} names, gap {gap:.2f} s", flush=True)
        r["latency_by_size"].append(await run_series(t, size, count))
    r["rate_sweep"] = []
    for g in sweep:
        t.gap = g
        print(f"   {unit.name}: rate sweep, gap {g:.2f} s, {count} x 20 names", flush=True)
        series = await run_series(t, api.objects.MAX_NAMES, count)
        r["rate_sweep"].append(series)
        if series["errors"]:
            print(f"   {unit.name}: errors at gap {g:.2f} s, stopping the sweep")
            await asyncio.sleep(10)  # let the adapter settle
            break
    r["finished"] = time.time()
    return r


def check_gaps(gap: float, sweep: list[float], allow_faster: bool) -> None:
    gaps = [gap, *sweep]
    too_fast = [g for g in gaps if g < SAFE_GAP]
    if too_fast and not allow_faster:
        raise SystemExit(
            f"gap {min(too_fast)} s is under the {SAFE_GAP} s safety limit; "
            "pass --allow-faster to run it deliberately"
        )
    if any(g < MIN_GAP for g in gaps):
        raise SystemExit(f"gaps under {MIN_GAP} s are never allowed")


async def run(args: argparse.Namespace, units: list[Unit]) -> int:
    sizes = [int(s) for s in args.sizes.split(",")]
    if any(not 1 <= s <= api.objects.MAX_NAMES for s in sizes):
        raise SystemExit(f"sizes must be 1..{api.objects.MAX_NAMES}")
    sweep = [float(g) for g in args.sweep.split(",")] if args.sweep else []
    check_gaps(args.gap, sweep, args.allow_faster)
    out_dir = Path(args.out) if args.out else OUT_DIR
    rc = 0
    async with aiohttp.ClientSession() as session:
        for unit in units:
            r = await bench_unit(
                session,
                unit,
                sizes=sizes,
                count=args.count,
                gap=args.gap,
                sweep=sweep,
                timeout=args.timeout,
                port=args.port,
            )
            path = write_json(out_dir / f"bench-{slug(unit.name)}-{stamp()}.json", r)
            print(f"== {unit.name} ==")
            if "error" in r:
                print(f"   ERROR: {r['error']}")
                rc = 1
                continue
            for s in r["latency_by_size"] + r["rate_sweep"]:
                print(
                    f"   size {s['size']:2} gap {s['gap_s']:.2f}s  {s['latency']}"
                    f"  errors={len(s['errors'])} cut_short={len(s['cut_short'])}"
                )
            print(f"   saved {path}")
    return rc
