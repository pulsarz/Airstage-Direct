"""Shared helpers: unit list, output files."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from custom_components.airstage_direct import api
else:
    from .apiload import load_api

    api = load_api()

PKG_DIR = Path(__file__).resolve().parent
OUT_DIR = PKG_DIR / "out"
UNITS_FILE = PKG_DIR / "units.local.json"  # not committed (.gitignore)


@dataclass(frozen=True)
class Unit:
    name: str
    host: str
    model: str = ""
    mac: str = ""


def _mac(value: str) -> str:
    value = value.strip()
    return api.transport.format_device_id(value) if value else ""


def parse_unit_arg(arg: str) -> Unit:
    """'Downstairs=192.0.2.10' or 'Downstairs=192.0.2.10,0A1B2C3D4E5F'."""
    name, sep, rest = arg.partition("=")
    if not sep or not name or not rest:
        raise ValueError(f"expected NAME=IP[,MAC], got {arg!r}")
    host, _, mac = rest.partition(",")
    return Unit(name=name.strip(), host=host.strip(), mac=_mac(mac))


def load_units(args: list[str] | None, path: Path = UNITS_FILE) -> list[Unit]:
    if args:
        return [parse_unit_arg(a) for a in args]
    if not path.exists():
        raise SystemExit(
            f"No units given. Pass --unit NAME=IP or create {path} with "
            '[{"name": "Downstairs", "host": "192.0.2.10", "model": "..."}]'
        )
    rows = json.loads(path.read_text())
    return [
        Unit(
            name=str(r["name"]),
            host=str(r["host"]),
            model=str(r.get("model") or ""),
            mac=_mac(str(r.get("mac") or "")),
        )
        for r in rows
        if r.get("host")
    ]


def answer_record(answer: Any) -> dict[str, Any]:
    """An api.transport.Answer as JSON for the raw output files."""
    return {
        "endpoint": answer.endpoint,
        "body": answer.body,
        "started": answer.started,
        "elapsed_s": round(answer.elapsed, 4),
        "answer": answer.payload,
    }


def slug(name: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-") or "unit"


def stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%S")


def write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return path
