"""Finding adapters on the local network by asking each address once.

Every adapter answers a GetParam with an empty device_id with
{"result": "NG", "error": "0002"} (measured on firmware L45-2), which no
ordinary web server does. So a scan sends that one small request to each
address, a few at a time with a short timeout, and keeps the addresses that
answer like an adapter. Only on demand (the config flow's "search network"
step), never in the background, and never to an address that is already set
up, since that adapter's own transport must stay the only thing talking to it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
from collections.abc import Iterable
from typing import Any, Final

import aiohttp

from .transport import ERROR_INVALID_PARAMETER

SCAN_TIMEOUT_S: Final = 1.0
SCAN_CONCURRENCY: Final = 16
MAX_SCAN_HOSTS: Final = 1024
PROBE_BODY: Final = {
    "device_id": "",
    "device_sub_id": 0,
    "req_id": "",
    "modified_by": "",
    "set_level": "03",
    "list": ["iu_set_tmp"],
}


def looks_like_adapter(payload: Any) -> bool:
    """The answer an adapter gives to the probe: NG 0002 (or OK on firmware
    that accepts an empty device_id)."""
    if not isinstance(payload, dict):
        return False
    if payload.get("result") == "NG" and payload.get("error") == ERROR_INVALID_PARAMETER:
        return True
    return payload.get("result") == "OK" and isinstance(payload.get("value"), dict)


def hosts_in(network: str, skip: Iterable[str] = ()) -> list[str]:
    """Usable host addresses of an IPv4 network, minus `skip`. Refuses huge networks."""
    net = ipaddress.ip_network(network, strict=False)
    if net.version != 4:
        raise ValueError("only IPv4 networks can be scanned")
    if net.num_addresses > MAX_SCAN_HOSTS + 2:
        raise ValueError(f"{network} is too large to scan")
    skipped = set(skip)
    return [str(h) for h in net.hosts() if str(h) not in skipped]


async def probe_host(
    session: aiohttp.ClientSession, host: str, *, port: int = 80, timeout: float = SCAN_TIMEOUT_S
) -> bool:
    """True if `host` answers the probe like an adapter."""
    url = f"http://{host}/GetParam" if port == 80 else f"http://{host}:{port}/GetParam"
    try:
        async with session.post(
            url,
            json=PROBE_BODY,
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"Connection": "close"},
        ) as resp:
            if resp.status != 200:
                return False
            text = await resp.text()
    except aiohttp.ClientError, TimeoutError, OSError:
        return False
    try:
        return looks_like_adapter(json.loads(text))
    except ValueError:
        return False


async def scan(
    session: aiohttp.ClientSession,
    hosts: Iterable[str],
    *,
    port: int = 80,
    timeout: float = SCAN_TIMEOUT_S,
    concurrency: int = SCAN_CONCURRENCY,
) -> list[str]:
    """Probe each host once, `concurrency` at a time; return those that answered
    like an adapter, in the order given. Each host gets one request at most."""
    hosts_l = list(dict.fromkeys(hosts))
    limit = asyncio.Semaphore(concurrency)

    async def one(host: str) -> bool:
        async with limit:
            return await probe_host(session, host, port=port, timeout=timeout)

    found = await asyncio.gather(*(one(h) for h in hosts_l))
    return [h for h, ok in zip(hosts_l, found, strict=True) if ok]
