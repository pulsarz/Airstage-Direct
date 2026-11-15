"""Build tests/fixtures/ from raw tool output, with private details redacted.

Usage: python -m tools.airstage.make_fixtures

Reads the newest dump per unit and the watch logs in tools/airstage/out/ (not
committed) and writes redacted JSON to tests/fixtures/. Every MAC becomes a
placeholder, IP addresses move to the documentation range 192.0.2.0/24, and
the adapter serial and SSID are replaced.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "tools" / "airstage" / "out"
FIXTURES = ROOT / "tests" / "fixtures"

PLACEHOLDER_MACS = ["AABBCCDDEEFF", "AABBCCDDEE01", "AABBCCDDEE02", "AABBCCDDEE03"]
_MAC = re.compile(r"\b[0-9A-F]{12}\b")
_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


class Redactor:
    def __init__(self) -> None:
        self.macs: dict[str, str] = {}
        self.ips: dict[str, str] = {}

    def mac(self, value: str) -> str:
        if value in PLACEHOLDER_MACS:
            return value
        if value not in self.macs:
            self.macs[value] = PLACEHOLDER_MACS[len(self.macs)]
        return self.macs[value]

    def ip(self, value: str) -> str:
        if value.startswith("192.0.2."):
            return value
        if value not in self.ips:
            self.ips[value] = f"192.0.2.{10 + len(self.ips)}"
        return self.ips[value]

    def text(self, value: str) -> str:
        value = _MAC.sub(lambda m: self.mac(m.group(0)), value)
        return _IP.sub(lambda m: self.ip(m.group(0)), value)

    def walk(self, data: Any) -> Any:
        if isinstance(data, dict):
            out = {}
            for key, val in data.items():
                if key == "wla_serial":
                    out[key] = "SERIAL00"
                elif key == "wla_cnct_ssid":
                    out[key] = "example-ssid"
                else:
                    out[key] = self.walk(val)
            return out
        if isinstance(data, list):
            return [self.walk(v) for v in data]
        if isinstance(data, str):
            return self.text(data)
        return data


def newest(pattern: str) -> Path:
    files = sorted(OUT.glob(pattern))
    if not files:
        raise SystemExit(f"no {pattern} in {OUT}")
    return files[-1]


def unit_fixture(dump: dict[str, Any], model: str) -> dict[str, Any]:
    return {
        "model": model,
        "recorded": dump["started_local"][:10],
        "device_id": dump["device_id"],
        "indoor": dump["indoor"]["values"],
        "adapter": dump["adapter"]["values"],
        "unknown_indoor": sorted(dump["indoor"].get("unknown_names", [])),
        "unknown_adapter": sorted(dump["adapter"].get("unknown_names", [])),
        "cut_short_in_dump": sorted(dump["indoor"].get("lost_in_batch", [])),
    }


def push_samples() -> list[dict[str, Any]]:
    """A few pushes of each kind from the watch logs."""
    seen: dict[tuple[str, ...], dict[str, Any]] = {}
    for log in sorted(OUT.glob("watch-2026*.jsonl")):
        for line in log.read_text().splitlines():
            rec = json.loads(line)
            body = rec.get("body")
            if rec.get("kind") != "push" or not isinstance(body, dict):
                continue
            key = (str(rec.get("unit")), *sorted(body.get("value", {})))
            seen.setdefault(key, {"unit": rec.get("unit"), "body": body})
    return list(seen.values())


def main() -> int:
    red = Redactor()
    FIXTURES.mkdir(parents=True, exist_ok=True)
    units = {
        "downstairs_14kj": ("dump-downstairs-*.json", "ASEH14KJCAL"),
        "upstairs_07kj": ("dump-upstairs-*.json", "ASEH07KJCAL"),
    }
    for name, (pattern, model) in units.items():
        dump = json.loads(newest(pattern).read_text())
        write(FIXTURES / f"unit_{name}.json", red.walk(unit_fixture(dump, model)))
    batchloss = json.loads((OUT / "batchloss-downstairs.json").read_text())
    write(
        FIXTURES / "batch_cut_short.json",
        red.walk({k: {"list": v["list"], "missing": [n for _, n in v["missing"]]}
                  for k, v in batchloss.items()}),
    )  # fmt: skip
    write(FIXTURES / "pushes.json", red.walk(push_samples()))
    write(
        FIXTURES / "answers.json",
        {
            "empty_device_id": {"result": "NG", "error": "0002"},
            "no_known_name": {
                "value": {}, "device_id": "AABBCCDDEEFF", "device_sub_id": 0, "req_id": "",
                "modified_by": "", "set_level": "03", "cause": "", "result": "NG",
                "error": "0013",
            },
            "adapter_unknown_name": {
                "value": {}, "device_id": "AABBCCDDEEFF", "device_sub_id": 1, "req_id": "",
                "modified_by": "", "set_level": "03", "read_res": "ack", "cause": "",
                "result": "OK", "error": "",
            },
            "register_ok": {"result": "OK", "error": ""},
        },
    )  # fmt: skip
    print("wrote", ", ".join(sorted(p.name for p in FIXTURES.glob("*.json"))))
    return 0


def write(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


if __name__ == "__main__":
    sys.exit(main())
