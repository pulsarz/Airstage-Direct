"""State shared by every Airstage entry in one Home Assistant instance.

- One Transport per adapter (keyed by its MAC), used by everything: entries,
  the config flow and diagnostics. A second transport for the same adapter would
  send requests in parallel, which the adapters handle badly.
- One push listener on port 10275, started with the first unit that needs it
  and stopped with the last.
- The push key of this instance, derived from Home Assistant's instance ID and
  never the measurement tools' key.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from homeassistant.components import network
from homeassistant.core import HomeAssistant
from homeassistant.helpers import instance_id
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api.push import PUSH_PORT, PushListener, PushListenerError, integration_push_key
from .api.transport import DEFAULT_GAP, Answer, Transport
from .const import DOMAIN, ISSUE_PUSH_UNAVAILABLE, REQUEST_LOG_SIZE

_LOGGER = logging.getLogger(__name__)

PUSH_LISTEN_HOST: Final = "0.0.0.0"
PUSH_LISTEN_PORT = PUSH_PORT  # the adapters always push here; tests patch it
TRANSPORT_GAP = DEFAULT_GAP  # seconds between requests to one adapter; tests patch it
_HUB_KEY: Final = f"{DOMAIN}_hub"


@dataclass
class AirstageHub:
    hass: HomeAssistant
    push_key: str
    transports: dict[str, Transport] = field(default_factory=dict)
    listener: PushListener | None = None
    listener_users: int = 0
    listener_error: str | None = None
    _requests: dict[str, deque[dict[str, Any]]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def transport(self, host: str, device_id: str, port: int = 80) -> Transport:
        """The one transport for this adapter; follows a changed host or port."""
        key = device_id.upper()
        existing = self.transports.get(key)
        if existing is not None:
            existing.host = host
            existing.port = port
            return existing
        transport = Transport(
            async_get_clientsession(self.hass), host, key, port=port, gap=TRANSPORT_GAP
        )
        log = self._requests.setdefault(key, deque(maxlen=REQUEST_LOG_SIZE))
        transport.observer = lambda answer: log.append(_request_record(answer))
        self.transports[key] = transport
        return transport

    def request_log(self, device_id: str) -> list[dict[str, Any]]:
        """The last requests sent to this adapter (for diagnostics)."""
        return list(self._requests.get(device_id.upper(), ()))

    def configured_hosts(self) -> set[str]:
        return {t.host for t in self.transports.values()}

    async def acquire_listener(self) -> PushListener | None:
        """The shared push listener, started if needed; None if it cannot run."""
        async with self._lock:
            if self.listener is None:
                listener = PushListener(PUSH_LISTEN_HOST, PUSH_LISTEN_PORT)
                try:
                    await listener.start()
                except PushListenerError as err:
                    self.listener_error = str(err)
                    _LOGGER.warning("push listener unavailable, falling back to polling: %s", err)
                    ir.async_create_issue(
                        self.hass,
                        DOMAIN,
                        ISSUE_PUSH_UNAVAILABLE,
                        is_fixable=False,
                        severity=ir.IssueSeverity.WARNING,
                        translation_key=ISSUE_PUSH_UNAVAILABLE,
                        translation_placeholders={"port": str(PUSH_LISTEN_PORT)},
                    )
                    return None
                self.listener = listener
                self.listener_error = None
                ir.async_delete_issue(self.hass, DOMAIN, ISSUE_PUSH_UNAVAILABLE)
            self.listener_users += 1
            return self.listener

    async def release_listener(self) -> None:
        async with self._lock:
            if self.listener is None:
                return
            self.listener_users -= 1
            if self.listener_users <= 0:
                await self.listener.stop()
                self.listener = None
                self.listener_users = 0

    async def listen_ip(self, target: str) -> str:
        """The address of this Home Assistant as the adapter at `target` sees it."""
        return await network.async_get_source_ip(self.hass, target_ip=target)


def _request_record(answer: Answer) -> dict[str, Any]:
    """What was asked, without the MAC: endpoint, sub-id, names, result, time."""
    body = answer.body
    record: dict[str, Any] = {
        "time": datetime.fromtimestamp(answer.started, UTC).isoformat(timespec="seconds"),
        "endpoint": answer.endpoint,
        "result": answer.payload.get("result"),
        "elapsed_s": round(answer.elapsed, 3),
    }
    if "list" in body:
        record["sub_id"] = body.get("device_sub_id")
        record["names"] = body["list"]
    elif "value" in body:
        record["names"] = sorted(body["value"])
    return record


async def async_get_hub(hass: HomeAssistant) -> AirstageHub:
    hub: AirstageHub | None = hass.data.get(_HUB_KEY)
    if hub is None:
        key = integration_push_key(await instance_id.async_get(hass))
        hub = hass.data.setdefault(_HUB_KEY, AirstageHub(hass, key))
    return hub
