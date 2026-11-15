"""HTTP transport to one Airstage adapter.

This is the only code that sends anything to an adapter, so the safety rules
live here:

- Endpoints: GetParam, SetParam, Register, Unregister, and SetPollingList and
  SetTime only through their own methods. Nothing else (no SetUpdateFile, no
  debug_*). SetTime only sends the current time, never from a read-only
  transport.
- GetParam always at set_level "03". SetParam only at "02" ("04"/"05" put the
  unit in function-setting mode and stop it), only for names on the write
  allow-list, never in read-only mode.
- One request in flight per adapter. Writes go before queued reads, never in
  parallel. A minimum gap between the end of one request and the next.
- Every request closes its connection (the adapter's own pushes do; all the
  measurements used fresh connections), and a dropped connection is retried
  once.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import aiohttp

from . import objects

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT: Final = 3.0  # slowest answer measured: 2.0 s
DEFAULT_GAP: Final = 1.0
SETUP_GAP: Final = 0.25  # measured safe for one-off work
MIN_GAP: Final = 0.1
ERROR_INVALID_PARAMETER: Final = "0002"
ERROR_NO_KNOWN_NAME: Final = "0013"
PRIORITY_WRITE: Final = 0
PRIORITY_READ: Final = 1
POLLING_LIST_KEYS: Final = frozenset({"init_ntfy", "change_monitor", "cycle_monitor"})
SET_TIME_KEYS: Final = frozenset(
    {"device_id", "device_sub_id", "req_id", "modified_by", "unix_time"}
)
SET_TIME_TOLERANCE_S: Final = 300
ARP_TABLE: Final = Path("/proc/net/arp")


class AirstageError(Exception):
    """Base error."""


class AirstageConnectionError(AirstageError):
    """Timeout, refused or dropped connection, or an answer that is not JSON."""


class AirstageResponseError(AirstageError):
    """The adapter answered result NG."""

    def __init__(self, endpoint: str, code: str | None) -> None:
        super().__init__(f"{endpoint}: result NG, error {code}")
        self.endpoint = endpoint
        self.code = code


class AirstageWriteError(AirstageError):
    """SetParam was not accepted: write_res nack, timeout or unknown."""

    def __init__(self, write_res: object) -> None:
        super().__init__(f"write not accepted: {write_res!r}")
        self.write_res = write_res


class AirstageSafetyError(AirstageError):
    """A request this package must never send."""


@dataclass(slots=True)
class Answer:
    endpoint: str
    body: dict[str, Any]
    payload: dict[str, Any]
    started: float  # unix time
    elapsed: float  # seconds

    @property
    def values(self) -> dict[str, Any]:
        value = self.payload.get("value")
        return dict(value) if isinstance(value, dict) else {}


@dataclass(slots=True)
class ReadResult:
    values: dict[str, Any] = field(default_factory=dict)  # raw values
    unknown: set[str] = field(default_factory=set)  # names the firmware does not know
    cut_short: set[str] = field(default_factory=set)  # known names a batch left out
    requests: int = 0


class _Slot:
    """One request at a time, lowest priority number first, FIFO within a priority."""

    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._busy = False
        self._queue: list[tuple[int, int]] = []
        self._seq = itertools.count()

    @asynccontextmanager
    async def acquire(self, priority: int) -> AsyncIterator[None]:
        ticket = (priority, next(self._seq))
        async with self._cond:
            heapq.heappush(self._queue, ticket)
            try:
                await self._cond.wait_for(lambda: not self._busy and self._queue[0] == ticket)
            except BaseException:
                self._queue.remove(ticket)
                heapq.heapify(self._queue)
                self._cond.notify_all()
                raise
            heapq.heappop(self._queue)
            self._busy = True
        try:
            yield
        finally:
            async with self._cond:
                self._busy = False
                self._cond.notify_all()


class Transport:
    """All requests to one adapter. Create exactly one per adapter."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        device_id: str = "",
        *,
        port: int = 80,
        timeout: float = DEFAULT_TIMEOUT,
        gap: float = DEFAULT_GAP,
        read_only: bool = False,
    ) -> None:
        if gap < MIN_GAP:
            raise ValueError(f"gap must be at least {MIN_GAP} s")
        self._session = session
        self.host = host
        self.port = port
        self.device_id = device_id
        self.timeout = timeout
        self.gap = gap
        self.read_only = read_only
        self._slot = _Slot()
        self._last_end = 0.0
        self._inflight: dict[tuple[int, tuple[str, ...]], asyncio.Future[Answer]] = {}
        # Called with every answer the adapter gives, including NG ones
        # (diagnostics, the measurement tools' raw logs).
        self.observer: Callable[[Answer], None] | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}" if self.port == 80 else f"http://{self.host}:{self.port}"

    # -- low level -----------------------------------------------------------

    def _check_safe(self, endpoint: str, body: Mapping[str, Any]) -> None:
        if endpoint == "GetParam":
            if body.get("set_level") != "03":
                raise AirstageSafetyError("GetParam must use set_level 03")
            names = body.get("list") or []
            if len(names) > objects.MAX_NAMES:
                raise AirstageSafetyError(f"at most {objects.MAX_NAMES} names per request")
            if bad := objects.NEVER_TOUCH.intersection(names):
                raise AirstageSafetyError(f"never read {sorted(bad)}")
        elif endpoint == "SetParam":
            if self.read_only:
                raise AirstageSafetyError("this transport is read-only")
            if body.get("set_level") != "02":
                raise AirstageSafetyError("SetParam only at set_level 02")
            if body.get("device_sub_id") != 0:
                raise AirstageSafetyError("SetParam only to the indoor unit (sub-id 0)")
            values = body.get("value")
            if not isinstance(values, dict) or not values:
                raise AirstageSafetyError("SetParam needs values")
            if not_allowed := set(values) - objects.WRITABLE:
                raise AirstageSafetyError(f"not on the write allow-list: {sorted(not_allowed)}")
        elif endpoint == "SetTime":
            if self.read_only:
                raise AirstageSafetyError("this transport is read-only")
            if set(body) != SET_TIME_KEYS:
                raise AirstageSafetyError("SetTime carries exactly the documented keys")
            unix_time = body.get("unix_time")
            if isinstance(unix_time, bool) or not isinstance(unix_time, int):
                raise AirstageSafetyError("unix_time must be an integer")
            if abs(unix_time - time.time()) > SET_TIME_TOLERANCE_S:
                raise AirstageSafetyError("SetTime only sends this machine's current time")
        elif endpoint == "SetPollingList":
            if not set(body) <= POLLING_LIST_KEYS | {"device_id"}:
                raise AirstageSafetyError("SetPollingList carries only device_id and the lists")
        elif endpoint not in ("Register", "Unregister"):
            raise AirstageSafetyError(f"endpoint {endpoint} is not allowed")

    async def _post(
        self, endpoint: str, body: dict[str, Any], *, priority: int = PRIORITY_READ
    ) -> Answer:
        self._check_safe(endpoint, body)
        loop = asyncio.get_running_loop()
        async with self._slot.acquire(priority):
            wait = self._last_end + self.gap - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await self._send(endpoint, body)
            finally:
                self._last_end = loop.time()

    async def _send(self, endpoint: str, body: dict[str, Any]) -> Answer:
        url = f"{self.base_url}/{endpoint}"
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        for attempt in (1, 2):
            started = time.time()
            t0 = time.perf_counter()
            try:
                async with self._session.post(
                    url, json=body, timeout=timeout, headers={"Connection": "close"}
                ) as resp:
                    text = await resp.text()
                    status = resp.status
                break
            except (aiohttp.ServerDisconnectedError, aiohttp.ClientOSError) as err:
                if attempt == 2:
                    raise AirstageConnectionError(f"{self.host}: {err!r}") from err
                _LOGGER.debug("%s: connection dropped, retrying once: %r", self.host, err)
            except TimeoutError as err:
                raise AirstageConnectionError(
                    f"{self.host}: timeout after {self.timeout} s"
                ) from err
            except aiohttp.ClientError as err:
                raise AirstageConnectionError(f"{self.host}: {err!r}") from err
        elapsed = time.perf_counter() - t0
        if status != 200:
            raise AirstageConnectionError(f"{self.host}: {endpoint} HTTP {status}")
        try:
            payload = json.loads(text)
        except ValueError as err:
            raise AirstageConnectionError(f"{self.host}: answer is not JSON") from err
        if not isinstance(payload, dict):
            raise AirstageConnectionError(f"{self.host}: answer is not a JSON object")
        answer = Answer(endpoint, body, payload, started, elapsed)
        if self.observer is not None:
            self.observer(answer)
        if payload.get("result") != "OK":
            raise AirstageResponseError(endpoint, _str_or_none(payload.get("error")))
        return answer

    def _base(self, sub_id: int, set_level: str) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "device_sub_id": sub_id,
            "req_id": "",
            "modified_by": "",
            "set_level": set_level,
        }

    # -- requests --------------------------------------------------------------

    async def get(self, names: Iterable[str], sub_id: int = 0) -> Answer:
        """One GetParam. Identical concurrent requests share one answer."""
        names_t = tuple(names)
        if not names_t:
            raise ValueError("no names")
        key = (sub_id, names_t)
        if (pending := self._inflight.get(key)) is not None:
            return await asyncio.shield(pending)
        future: asyncio.Future[Answer] = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            answer = await self._post(
                "GetParam", {**self._base(sub_id, "03"), "list": list(names_t)}
            )
        except asyncio.CancelledError:
            future.cancel()
            raise
        except Exception as err:
            future.set_exception(err)
            future.exception()  # retrieved here, so no warning when nobody else waits
            raise
        else:
            future.set_result(answer)
            return answer
        finally:
            self._inflight.pop(key, None)

    async def read(
        self, names: Iterable[str], sub_id: int = 0, *, skip: Iterable[str] = ()
    ) -> ReadResult:
        """Read any number of names, batched, and check the answers are complete.

        Batches are sorted by object id (monitor-only batches came back complete
        in every measurement poll) and carry an anchor; long strings are read alone.
        A known name missing from an answer is asked again once in a batch of the
        missing names, then once alone, where the adapter tells unknown (error 0013
        on sub-id 0, OK with no value on sub-id 1) from a cut-short answer.
        Names in `skip` (already known to be unknown) are not requested.
        """
        anchor = objects.ANCHOR_INDOOR if sub_id == 0 else objects.ANCHOR_ADAPTER
        skip_set = set(skip)
        wanted = [n for n in dict.fromkeys(names) if n not in skip_set]
        result = ReadResult()
        if not wanted:
            return result
        long = [n for n in wanted if (o := objects.get(n, sub_id)) and o.long_string]
        rest = sorted((n for n in wanted if n not in long), key=lambda n: _sort_key(n, sub_id))
        raw: dict[str, Any] = {}
        for batch in _batches(rest, anchor):
            raw.update((await self.get(batch, sub_id)).values)
            result.requests += 1
        missing = [n for n in rest if n not in raw]
        if missing:
            for batch in _batches(missing, anchor):
                raw.update((await self.get(batch, sub_id)).values)
                result.requests += 1
            for name in missing:
                if name in raw:
                    result.cut_short.add(name)
        for name in [n for n in rest if n not in raw] + long:
            result.requests += 1
            try:
                got = (await self.get([name], sub_id)).values
            except AirstageResponseError as err:
                if err.code == ERROR_NO_KNOWN_NAME:
                    result.unknown.add(name)
                    continue
                raise
            if name in got:
                raw[name] = got[name]
                if name not in long:
                    result.cut_short.add(name)
            else:
                result.unknown.add(name)
        result.values = {n: raw[n] for n in wanted if n in raw}
        return result

    async def set_values(self, raw_values: Mapping[str, str]) -> Answer:
        """One SetParam at set_level "02". Raw values must already be encoded strings.

        Body and answer as in docs/protocol.md. "result" only says the
        request was well formed; "write_res" says whether the indoor unit took it
        ("unknown" is a refusal in practice); the echoed "value" is checked too.
        Only one name per request has been confirmed on a real adapter.
        """
        body = {**self._base(0, "02"), "value": dict(raw_values)}
        answer = await self._post("SetParam", body, priority=PRIORITY_WRITE)
        write_res = answer.payload.get("write_res")
        results = write_res.values() if isinstance(write_res, dict) else [write_res]
        if not all(r == "ack" for r in results):
            raise AirstageWriteError(write_res)
        echoed = answer.values
        if wrong := {
            n: echoed[n] for n in raw_values if n in echoed and str(echoed[n]) != raw_values[n]
        }:
            raise AirstageWriteError({"ack_but_value": wrong})
        return answer

    async def set_time(self) -> Answer:
        """Set the adapter's clock to now (docs/protocol.md).

        After a power cycle the adapter has no clock and its service monitor stays
        on placeholders until this is sent. It moves the monitor's refresh offset
        and wla_op_time, so callers learn the offset again afterwards.
        """
        body = {
            "device_id": self.device_id,
            "device_sub_id": 0,
            "req_id": "",
            "modified_by": "",
            "unix_time": int(time.time()),
        }
        return await self._post("SetTime", body, priority=PRIORITY_WRITE)

    async def register(self, key: str, ip: str) -> Answer:
        if not key or len(key) > 36 or not ip:
            raise ValueError("Register needs a key of 1..36 characters and an IP")
        return await self._post("Register", {"Type": "1", "Key": key, "IP": ip})

    async def unregister(self, key: str) -> Answer:
        return await self._post("Unregister", {"Key": key})

    async def set_polling_list(self, lists: Mapping[str, list[str]]) -> Answer:
        """Change the adapter's push list. Shared with the cloud and the app; the
        last writer wins, so the integration does not call this by default."""
        if not lists or not set(lists) <= POLLING_LIST_KEYS:
            raise ValueError(f"keys must be among {sorted(POLLING_LIST_KEYS)}")
        for key, names in lists.items():
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                raise ValueError(f"{key} must be a list of names")
        return await self._post("SetPollingList", {"device_id": self.device_id, **lists})

    async def resolve_device_id(self) -> str:
        """Find a device_id the adapter accepts: the current one, else the MAC from ARP.

        Firmware L45-2 rejects an empty device_id with 0002; the MAC must be 12
        uppercase hex digits. The ARP entry exists once we have talked to the host.
        """
        try:
            await self.get([objects.ANCHOR_INDOOR])
            return self.device_id
        except AirstageResponseError as err:
            if err.code != ERROR_INVALID_PARAMETER:
                raise
        mac = await asyncio.to_thread(arp_mac, self.host)
        if mac is None:
            raise AirstageResponseError("GetParam", ERROR_INVALID_PARAMETER)
        self.device_id = mac
        await self.get([objects.ANCHOR_INDOOR])
        return mac


def _str_or_none(value: object) -> str | None:
    return None if value is None else str(value)


def _sort_key(name: str, sub_id: int) -> tuple[int, str]:
    obj = objects.get(name, sub_id)
    return (obj.order if obj and obj.order is not None else 10_000, name)


def _batches(names: list[str], anchor: str) -> list[list[str]]:
    rest = [n for n in names if n != anchor]
    per = objects.MAX_NAMES - 1
    out = [[anchor, *rest[i : i + per]] for i in range(0, len(rest), per)]
    if anchor in names and not out:
        out = [[anchor]]
    return out


def format_device_id(mac: str) -> str:
    """'ec:3a:..' -> 'EC3A..': 12 uppercase hex digits, as the adapter wants it."""
    digits = "".join(c for c in mac if c in "0123456789abcdefABCDEF")
    if len(digits) != 12:
        raise ValueError(f"not a MAC address: {mac!r}")
    return digits.upper()


def arp_mac(host: str, table: Path = ARP_TABLE) -> str | None:
    """The MAC of `host` from the Linux ARP table, or None. Blocking file I/O."""
    try:
        lines = table.read_text().splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        parts = line.split()
        if len(parts) >= 4 and parts[0] == host and parts[3] != "00:00:00:00:00:00":
            try:
                return format_device_id(parts[3])
            except ValueError:
                return None
    return None
