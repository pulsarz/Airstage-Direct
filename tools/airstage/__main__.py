"""CLI: python -m tools.airstage {dump,bench,watch} ..."""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import TYPE_CHECKING

from . import bench, dump, watch
from .common import load_units

if TYPE_CHECKING:
    from custom_components.airstage_direct import api
else:
    from .apiload import load_api

    api = load_api()

DEFAULT_GAP = api.transport.DEFAULT_GAP
DEFAULT_TIMEOUT = api.transport.DEFAULT_TIMEOUT


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--unit",
        action="append",
        metavar="NAME=IP[,MAC]",
        help="adapter to use (repeatable); default: tools/airstage/units.local.json",
    )
    p.add_argument(
        "--only", action="append", metavar="NAME", help="only these units from the units file"
    )
    p.add_argument(
        "--gap",
        type=float,
        default=DEFAULT_GAP,
        help=f"minimum seconds between requests per adapter (default {DEFAULT_GAP})",
    )
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--port", type=int, default=80, help=argparse.SUPPRESS)
    p.add_argument("--out", help="output directory (default tools/airstage/out)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m tools.airstage", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("dump", help="read every named object, save raw answers")
    _common(p)
    p.add_argument(
        "--functions",
        action="store_true",
        help="also read function settings iu_fld_set00..99 (reads only)",
    )

    p = sub.add_parser("bench", help="latency by batch size; optional rate sweep")
    _common(p)
    p.add_argument("--sizes", default="1,5,10,20")
    p.add_argument("--count", type=int, default=200)
    p.add_argument("--sweep", default="", help="gaps to try with 20 names, e.g. 0.75,0.5")
    p.add_argument(
        "--allow-faster",
        action="store_true",
        help="allow gaps under 1 s (the sweep stops at the first error)",
    )

    p = sub.add_parser("watch", help="push listener + monitor change log")
    _common(p)
    p.add_argument(
        "--interval",
        type=float,
        default=10.0,
        help="seconds between polls while learning each adapter's refresh offset (min 5)",
    )
    p.add_argument("--duration", type=float, default=0, help="seconds; 0 = until Ctrl-C")
    p.add_argument(
        "--stop-file",
        default="",
        help="exit cleanly (unregistering) as soon as this file exists",
    )
    p.add_argument(
        "--state-interval",
        type=float,
        default=0,
        help="also poll state/settings objects every N s (min 3) for code sweeps; 0 = off",
    )
    p.add_argument("--listen-host", default="0.0.0.0")
    p.add_argument("--listen-port", type=int, default=10275)
    p.add_argument("--no-listen", action="store_true")
    p.add_argument("--no-monitor", action="store_true")
    p.add_argument(
        "--register",
        action="store_true",
        help="register for push (Type/Key/IP) and unregister on exit",
    )
    p.add_argument(
        "--polling-list",
        default="",
        help="SetPollingList lists after Register: JSON or @file "
        "(keys init_ntfy, change_monitor, cycle_monitor; keys starting with _ are ignored)",
    )
    p.add_argument(
        "--key",
        default=watch.DEFAULT_KEY,
        help="registration key: MAC-like, <=36 chars; reusing it reuses the slot",
    )
    p.add_argument("--my-ip", default="", help="address the adapters should push to")
    p.add_argument(
        "--reply",
        default="",
        help="body returned to the adapter for each push (default: empty, as tested)",
    )

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    units = load_units(args.unit)
    if args.only:
        units = [u for u in units if u.name in args.only]
    if not units:
        raise SystemExit("no units selected")
    runner = {"dump": dump.run, "bench": bench.run, "watch": watch.run}[args.cmd]
    return asyncio.run(runner(args, units))


if __name__ == "__main__":
    sys.exit(main())
