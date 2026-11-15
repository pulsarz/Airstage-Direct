"""Push: the NtfyParam listener and one push registration per adapter.

See docs/protocol.md and docs/measurements.md:
- `Register` {"Type", "Key", "IP"} makes the adapter POST to
  http://<IP>:10275/NtfyParam; the port is fixed (Register has no port field).
- Registering again with the same Key reuses its slot. An adapter keeps only
  a few registrations (reportedly 5, a 6th replacing the oldest), and they are
  likely lost when the adapter restarts, so they are renewed when pushes stop.
- Pushes carry {"value": {...}, "device_id": <MAC>, "device_sub_id": 0 or 1}:
  state changes (only the changed objects, 1-8 s after the change) and a
  periodic report every 300 s. `Register` alone sends no initial dump.
- Adapter values (wla_*) arrive on sub-id 0 and as strings, unlike GetParam.
- An empty HTTP 200 is a sufficient reply.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from aiohttp import web

from .device import Device
from .transport import AirstageError, Transport

_LOGGER = logging.getLogger(__name__)

PUSH_PORT: Final = 10275
PUSH_SILENCE_S: Final = 420.0  # the periodic report comes every 300 s
CHECK_INTERVAL_S: Final = 60.0
# The measurement tools register with this key. The integration must use its own:
# registering again with a key takes over that key's slot, so sharing one would
# let a tools run take over (and on exit delete) Home Assistant's registration.
TOOLS_PUSH_KEY: Final = "02A1257A6E01"
IGNORED_PUSH_NAMES: Final = frozenset({"iu_monitor1"})  # an undocumented bundle, not used


class PushListenerError(AirstageError):
    """The listener could not start (port taken, no permission)."""


@dataclass(slots=True)
class Push:
    device_id: str
    sub_id: int
    values: dict[str, Any]
    source: str
    received: float = field(default_factory=time.time)


def integration_push_key(instance_id: str) -> str:
    """A stable push key for one Home Assistant instance, never the tools' key."""
    digest = hashlib.sha256(f"airstage_direct:{instance_id}".encode()).hexdigest().upper()
    key = "0A" + digest[:10]  # locally administered, MAC-like, 12 hex digits
    return key if key != TOOLS_PUSH_KEY else "0A" + digest[10:20]


def parse_push(body: object, source: str = "") -> Push | None:
    """A NtfyParam body as a Push, or None when it is not one."""
    if not isinstance(body, Mapping):
        return None
    values = body.get("value")
    device_id = body.get("device_id")
    if not isinstance(values, Mapping) or not isinstance(device_id, str) or not device_id:
        return None
    try:
        sub_id = int(body.get("device_sub_id", 0))
    except TypeError, ValueError:
        return None
    kept = {str(k): v for k, v in values.items() if k not in IGNORED_PUSH_NAMES}
    return Push(device_id.upper(), sub_id, kept, source)


def split_values(values: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Indoor values and adapter (wla_*) values; pushes put both on sub-id 0."""
    indoor = {k: v for k, v in values.items() if not k.startswith("wla_")}
    adapter = {k: v for k, v in values.items() if k.startswith("wla_")}
    return indoor, adapter


PushHandler = Callable[[Push], None]
RawObserver = Callable[[str, str, Any], None]  # source, path, body (parsed or text)


class PushListener:
    """One HTTP listener for every adapter's pushes. The caller owns and stops it."""

    def __init__(self, host: str = "0.0.0.0", port: int = PUSH_PORT) -> None:
        self.host = host
        self.port = port
        self._handlers: dict[str, PushHandler] = {}
        self._runner: web.AppRunner | None = None
        self.unrouted = 0
        self.raw_observer: RawObserver | None = None

    def add(self, device_id: str, handler: PushHandler) -> Callable[[], None]:
        key = device_id.upper()
        self._handlers[key] = handler

        def remove() -> None:
            if self._handlers.get(key) is handler:
                del self._handlers[key]

        return remove

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            site = web.TCPSite(runner, self.host, self.port)
            await site.start()
        except OSError as err:
            await runner.cleanup()
            raise PushListenerError(f"cannot listen on {self.host}:{self.port}: {err}") from err
        self._runner = runner

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _handle(self, request: web.Request) -> web.Response:
        text = (await request.read()).decode("utf-8", "replace")
        try:
            body: Any = json.loads(text) if text else None
        except ValueError:
            body = None
        source = request.remote or ""
        if self.raw_observer is not None:
            self.raw_observer(source, request.path, body if body is not None else text)
        push = parse_push(body, source)
        if push is None:
            _LOGGER.debug("ignored a push from %s that is not NtfyParam JSON", source)
        elif (handler := self._handlers.get(push.device_id)) is None:
            self.unrouted += 1
            _LOGGER.debug("push from %s for an unknown adapter", source)
        else:
            try:
                handler(push)
            except Exception:  # a bad handler must not break the listener
                _LOGGER.exception("push handler failed")
        return web.Response(status=200)


EventHook = Callable[[str, dict[str, Any]], None]


class PushSession:
    """The push registration of one adapter, feeding its pushes into the Device."""

    def __init__(
        self,
        device: Device,
        listener: PushListener,
        key: str,
        listen_ip: str,
        *,
        silence: float = PUSH_SILENCE_S,
        allow_tools_key: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if key == TOOLS_PUSH_KEY and not allow_tools_key:
            raise ValueError("the tools' push key would take over another registration")
        self.device = device
        self.transport: Transport = device.transport
        self.listener = listener
        self.key = key
        self.listen_ip = listen_ip
        self.silence = silence
        self._clock = clock
        self.last_push: float | None = None
        self.registered_at: float | None = None
        self.pushes = 0
        self.on_event: EventHook | None = None
        self._remove: Callable[[], None] | None = None

    def _event(self, kind: str, **data: Any) -> None:
        if self.on_event is not None:
            self.on_event(kind, data)

    async def start(self) -> None:
        if not self.transport.device_id:
            raise ValueError("resolve the adapter's device_id before registering")
        self._remove = self.listener.add(self.transport.device_id, self._on_push)
        await self.register()

    async def register(self) -> None:
        await self.transport.register(self.key, self.listen_ip)
        self.registered_at = self._clock()
        self._event("registered", key=self.key, ip=self.listen_ip)

    def _on_push(self, push: Push) -> None:
        self.last_push = self._clock()
        self.pushes += 1
        indoor, adapter = split_values(push.values)
        changed: set[str] = set()
        if indoor:
            changed |= self.device.apply(indoor, 0)
        if adapter:
            changed |= self.device.apply(adapter, 1)
        self._event("push", values=push.values, changed=sorted(changed))

    @property
    def silent_for(self) -> float:
        since = self.last_push if self.last_push is not None else self.registered_at
        return 0.0 if since is None else self._clock() - since

    async def check(self) -> bool:
        """Register again after `silence` seconds without any push. True if it did."""
        if self.registered_at is None or self.silent_for < self.silence:
            return False
        self._event("silent", seconds=round(self.silent_for, 1))
        try:
            await self.register()
        except AirstageError as err:
            self._event("register_failed", error=str(err))
            return False
        return True

    async def run(self, stop: asyncio.Event, interval: float = CHECK_INTERVAL_S) -> None:
        """Check for silence every `interval` until `stop` is set."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), interval)
            except TimeoutError:
                await self.check()

    async def stop(self) -> None:
        """Stop routing pushes and unregister (the adapter keeps only 5)."""
        if self._remove is not None:
            self._remove()
            self._remove = None
        if self.registered_at is None:
            return
        try:
            await self.transport.unregister(self.key)
            self._event("unregistered", key=self.key)
        except AirstageError as err:
            self._event("unregister_failed", error=str(err))
        finally:
            self.registered_at = None
