"""Recorded fixtures must not contain private details (CLAUDE.md)."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
PLACEHOLDER_MACS = {"AABBCCDDEEFF", "AABBCCDDEE01", "AABBCCDDEE02", "AABBCCDDEE03"}
DOC_NET = ipaddress.ip_network("192.0.2.0/24")
MAC_HEX = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{12}(?![0-9A-Fa-f])")
MAC_COLON = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def test_fixtures_exist() -> None:
    assert list(FIXTURES.glob("*.json"))


def test_no_mac_addresses() -> None:
    for path in FIXTURES.glob("*.json"):
        text = path.read_text()
        bad = {m for m in MAC_HEX.findall(text) if m.upper() not in PLACEHOLDER_MACS}
        bad |= set(MAC_COLON.findall(text))
        assert not bad, f"{path.name}: {sorted(bad)}"


def test_only_documentation_ip_addresses() -> None:
    for path in FIXTURES.glob("*.json"):
        for found in IPV4.findall(path.read_text()):
            assert ipaddress.ip_address(found) in DOC_NET, f"{path.name}: {found}"


def test_serial_and_ssid_replaced() -> None:
    for path in FIXTURES.glob("unit_*.json"):
        text = path.read_text()
        assert '"wla_serial": "SERIAL00"' in text, path.name
        assert '"wla_cnct_ssid": "example-ssid"' in text, path.name
