"""One coordinator per indoor unit: push, subscribed reads, monitor schedule, clock."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import CONF_HOST
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import objects
from .api.device import Capabilities, Device
from .api.monitor import OUTDOOR, MonitorScheduler
from .api.push import PushSession
from .api.transport import AirstageError
from .const import (
    CONF_CAPABILITIES,
    CONF_DEVICE_ID,
    CONF_MONITOR_OFFSET,
    CONF_PORT,
    DOMAIN,
    ISSUE_CLOCK_LOST,
    OPT_PUSH,
    RECHECK_AFTER_ON_S,
    RECHECK_INTERVAL,
    RECHECK_UNSUPPORTED_S,
    STATE_POLL_NO_PUSH_S,
    STATE_POLL_S,
)
from .hub import AirstageHub

if TYPE_CHECKING:
    from . import AirstageConfigEntry

_LOGGER = logging.getLogger(__name__)

# After a power cycle the adapter has no clock and its monitor stays on these
# placeholders (docs/protocol.md). 20.00 C can also be a real reading,
# so it takes several scheduled reads in a row, and an empty outdoor block too.
PLACEHOLDER = "2000"
CLOCK_LOST_READS = 3


class UnitCoordinator(DataUpdateCoordinator[None]):
    """The state of one indoor unit. Entities read `device.values`.

    Nothing is read that no enabled entity has subscribed: the 60 s read covers
    subscribed state objects (sub-id 0) and adapter values (sub-id 1); the
    monitor scheduler reads subscribed monitor objects once per refresh.
    """

    config_entry: AirstageConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: AirstageConfigEntry,
        subentry: ConfigSubentry,
        hub: AirstageHub,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {subentry.title}",
            update_interval=timedelta(seconds=STATE_POLL_S),
        )
        self.subentry = subentry
        self.hub = hub
        data = subentry.data
        self.device_id: str = data[CONF_DEVICE_ID]
        self.transport = hub.transport(
            data[CONF_HOST], self.device_id, int(data.get(CONF_PORT, 80))
        )
        self.device = Device(self.transport)
        if stored := data.get(CONF_CAPABILITIES):
            self.device.capabilities = Capabilities.from_dict(stored)
        self.monitor = MonitorScheduler(
            self.device, self._monitor_names, offset=data.get(CONF_MONITOR_OFFSET)
        )
        self.monitor.on_event = self._on_monitor_event
        self.push: PushSession | None = None
        self._stop = asyncio.Event()
        self._refreshing = False
        self._listener_held = False
        self._placeholder_reads = 0
        self._cancel_recheck: CALLBACK_TYPE | None = None
        self._clock_set_sent = False
        self._unsub_change = self.device.add_listener(self._on_change)

    def _monitor_names(self) -> list[str]:
        return [n for n in self.device.subscribed if n in objects.MONITOR]

    # -- setup -----------------------------------------------------------------

    async def async_start(self) -> None:
        """Probe if needed, register for push, start the monitor schedule. Never
        raises: an unreachable unit becomes unavailable and is retried by the
        refresh. The first state read follows once the entities have subscribed."""
        try:
            await self._ensure_capabilities()
        except AirstageError as err:
            _LOGGER.warning("%s: capability probe failed, retrying later: %s", self.name, err)
        await self._start_push()
        self.config_entry.async_create_background_task(
            self.hass, self.monitor.run(self._stop), f"{self.name} monitor"
        )
        # A unit that was off at the probe answers 65535 for values it has (room
        # temperature, for one): look again after start, hourly, and after it is
        # switched on.
        self.config_entry.async_on_unload(self._cancel_pending_recheck)
        self.recheck_in(RECHECK_UNSUPPORTED_S)
        self.config_entry.async_on_unload(
            async_track_time_interval(self.hass, self._recheck_now, RECHECK_INTERVAL)
        )

    async def _ensure_capabilities(self) -> None:
        if self.device.capabilities is not None:
            return
        caps = await self.device.probe()
        self._store(caps=caps)

    @callback
    def recheck_in(self, seconds: float) -> None:
        """Read the "65535" objects again after `seconds` (replaces a pending one)."""
        if self._cancel_recheck is not None:
            self._cancel_recheck()
        self._cancel_recheck = async_call_later(self.hass, seconds, self._recheck_now)

    @callback
    def _cancel_pending_recheck(self) -> None:
        if self._cancel_recheck is not None:
            self._cancel_recheck()
            self._cancel_recheck = None

    @callback
    def _recheck_now(self, _now: Any) -> None:
        self._cancel_recheck = None
        caps = self.device.capabilities
        if caps is None or not caps.unsupported:
            return
        self.config_entry.async_create_task(
            self.hass, self._recheck(), f"{self.name} recheck unsupported"
        )

    async def _recheck(self) -> None:
        try:
            promoted = await self.device.recheck_unsupported()
        except AirstageError as err:
            _LOGGER.debug("%s: recheck failed: %s", self.name, err)
            return
        if promoted and self.device.capabilities is not None:
            _LOGGER.info("%s: now supported: %s", self.name, sorted(promoted))
            self._store(caps=self.device.capabilities)
            # Entities are created by capability: reload so the new ones appear.
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)

    def _store(self, *, caps: Capabilities | None = None, offset: float | None = None) -> None:
        """Keep capabilities and the learned monitor offset with the unit."""
        subentry = self.config_entry.subentries.get(self.subentry.subentry_id)
        if subentry is None:
            return
        data = dict(subentry.data)
        if caps is not None:
            data[CONF_CAPABILITIES] = caps.to_dict()
        if offset is not None:
            data[CONF_MONITOR_OFFSET] = round(offset, 1)
        if data != subentry.data:
            self.hass.config_entries.async_update_subentry(self.config_entry, subentry, data=data)

    @callback
    def save_capabilities(self) -> None:
        """Store the capabilities again, e.g. after the unit refused a write."""
        if self.device.capabilities is not None:
            self._store(caps=self.device.capabilities)

    async def _start_push(self) -> None:
        if not self.config_entry.options.get(OPT_PUSH, True):
            self.update_interval = timedelta(seconds=STATE_POLL_NO_PUSH_S)  # push off: poll
            return
        listener = await self.hub.acquire_listener()
        if listener is None:
            self.update_interval = timedelta(seconds=STATE_POLL_NO_PUSH_S)
            return
        self._listener_held = True
        try:
            ip = await self.hub.listen_ip(self.transport.host)
            self.push = PushSession(self.device, listener, self.hub.push_key, ip)
            await self.push.start()
        except AirstageError as err:
            _LOGGER.warning("%s: push registration failed, will retry: %s", self.name, err)
        if self.push is not None:
            self.config_entry.async_create_background_task(
                self.hass, self.push.run(self._stop), f"{self.name} push check"
            )

    async def async_read_monitor_now(self) -> None:
        """One read of the subscribed monitor objects (at start-up)."""
        if not self.monitor.active():
            return
        try:
            await self.monitor.read_once()
        except AirstageError as err:
            _LOGGER.debug("%s: first monitor read failed: %s", self.name, err)

    async def async_stop(self) -> None:
        """Stop the schedules, unregister, release the listener (5 slots only)."""
        self._stop.set()
        self._unsub_change()
        if self.push is not None:
            await self.push.stop()
            self.push = None
        if self._listener_held:
            self._listener_held = False
            await self.hub.release_listener()

    # -- updates -----------------------------------------------------------------

    async def _async_update_data(self) -> None:
        """The slow read: subscribed state objects and adapter values only."""
        self._refreshing = True
        try:
            await self._ensure_capabilities()
            state = [n for n in self.device.subscribed if n not in objects.MONITOR]
            if state:
                await self.device.refresh(state)
            if self.device.subscribed_for(1):
                await self.device.refresh(sub_id=1)
        except AirstageError as err:
            raise UpdateFailed(f"{self.transport.host}: {err}") from err
        finally:
            self._refreshing = False

    @callback
    def _on_change(self, changed: set[str]) -> None:
        """A push, a write or a monitor read changed values: update entities now."""
        if "iu_onoff" in changed and self.device.value("iu_onoff") is True:
            self.recheck_in(RECHECK_AFTER_ON_S)
        if not self._refreshing:
            self.async_set_updated_data(None)

    @callback
    def _on_monitor_event(self, kind: str, data: dict[str, Any]) -> None:
        if kind == "learned" and self.monitor.offset is not None:
            self._store(offset=self.monitor.offset)
        elif kind == "read":
            self._check_clock()

    @callback
    def _check_clock(self) -> None:
        """Placeholders in several scheduled reads in a row: the adapter lost its
        clock (power cycle). Send SetTime once, then learn the offset again."""
        raw = self.device.raw
        outdoor = [raw[n] for n in OUTDOOR if n in raw]
        lost = (
            raw.get("iu_suct_tmp") == PLACEHOLDER
            and raw.get("iu_heat_ex_tmp") == PLACEHOLDER
            and bool(outdoor)
            and all(v == "" for v in outdoor)
        )
        if not lost:
            if self._placeholder_reads or self._clock_set_sent:
                ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_CLOCK_LOST}_{self.device_id}")
            self._placeholder_reads = 0
            self._clock_set_sent = False
            return
        self._placeholder_reads += 1
        if self._placeholder_reads < CLOCK_LOST_READS:
            return
        if not self._clock_set_sent:
            self._clock_set_sent = True
            self.config_entry.async_create_task(
                self.hass, self._set_clock(), f"{self.name} set clock"
            )
        elif self._placeholder_reads >= 2 * CLOCK_LOST_READS:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"{ISSUE_CLOCK_LOST}_{self.device_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_CLOCK_LOST,
                translation_placeholders={"unit": self.subentry.title},
            )

    async def _set_clock(self) -> None:
        try:
            await self.transport.set_time()
        except AirstageError as err:
            _LOGGER.warning("%s: setting the adapter's clock failed: %s", self.name, err)
            return
        _LOGGER.info("%s: the adapter had lost its clock; time set", self.name)
        self.monitor.offset = None  # the refresh moves with the clock: learn it again

    @property
    def diagnostics(self) -> dict[str, Any]:
        caps = self.device.capabilities
        return {
            "host": self.transport.host,
            "values": dict(self.device.raw),
            "capabilities": caps.to_dict() if caps else None,
            "refused": sorted(self.device.refused),
            "subscribed": sorted(self.device.subscribed),
            "subscribed_adapter": sorted(self.device.subscribed_for(1)),
            "monitor": {
                "offset_s": self.monitor.offset,
                "reads": self.monitor.reads,
                "learn_reads": self.monitor.learn_reads,
            },
            "push": None
            if self.push is None
            else {
                "pushes": self.push.pushes,
                "silent_for_s": round(self.push.silent_for, 1),
                "listen_ip": self.push.listen_ip,
            },
            "polling_interval_s": self.update_interval.total_seconds()
            if self.update_interval
            else None,
            "last_update_success": self.last_update_success,
            "requests": self.hub.request_log(self.device_id),
        }
