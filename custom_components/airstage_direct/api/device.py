"""One adapter (one indoor unit): capabilities, cache, subscriptions, typed setters."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from . import codecs, objects
from .codecs import FanSpeed, Missing, OpMode, Value
from .transport import SETUP_GAP, AirstageWriteError, Transport

_LOGGER = logging.getLogger(__name__)

WRITE_COALESCE_S: Final = 0.3
_LIMIT_NAMES: Final = {
    OpMode.HEAT: ("iu_tmp_lo_heat", "iu_tmp_hi_heat"),
    OpMode.COOL: ("iu_tmp_lo_cool", "iu_tmp_hi_cool"),
    OpMode.DRY: ("iu_tmp_lo_cool", "iu_tmp_hi_cool"),
    OpMode.AUTO: ("iu_tmp_lo_auto", "iu_tmp_hi_auto"),
}

Listener = Callable[[set[str]], None]


@dataclass(slots=True)
class Capabilities:
    """What the unit answered at setup, stored in the config entry."""

    supported: set[str] = field(default_factory=set)  # answered a value, or ""
    unsupported: set[str] = field(default_factory=set)  # answered "65535"
    unknown: set[str] = field(default_factory=set)  # the firmware does not know the name
    unknown_adapter: set[str] = field(default_factory=set)  # same, for sub-id 1 (wla_*)
    refused: set[str] = field(default_factory=set)  # writes the unit refused ("unknown")
    limits: dict[OpMode, tuple[float, float]] = field(default_factory=dict)
    firmware: str | None = None  # iu_main_ver; re-probe when it changes

    def to_dict(self) -> dict[str, Any]:
        return {
            "supported": sorted(self.supported),
            "unsupported": sorted(self.unsupported),
            "unknown": sorted(self.unknown),
            "unknown_adapter": sorted(self.unknown_adapter),
            "refused": sorted(self.refused),
            "limits": {m.name: list(v) for m, v in self.limits.items()},
            "firmware": self.firmware,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Capabilities:
        return cls(
            supported=set(data.get("supported", ())),
            unsupported=set(data.get("unsupported", ())),
            unknown=set(data.get("unknown", ())),
            unknown_adapter=set(data.get("unknown_adapter", ())),
            refused=set(data.get("refused", ())),
            limits={
                OpMode[m]: (float(lo), float(hi)) for m, (lo, hi) in data.get("limits", {}).items()
            },
            firmware=data.get("firmware"),
        )


class Device:
    """The cached state of one indoor unit and the only way to change it."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self.capabilities: Capabilities | None = None
        self.raw: dict[str, Any] = {}
        self.values: dict[str, Value] = {}
        self._held_empty: set[str] = set()  # names showing a held value through one ""
        self._subscriptions: Counter[tuple[int, str]] = Counter()
        self._subscription_hooks: list[Callable[[], None]] = []
        self._listeners: list[Listener] = []
        self._pending: dict[str, str] = {}
        # Writable objects this unit refused (write_res "unknown"), e.g. outdoor
        # low noise on a multi-split; the UI can show them as not changeable.
        self._refused: set[str] = set()  # used until there are capabilities
        self._flush: asyncio.Future[None] | None = None

    # -- capabilities ----------------------------------------------------------

    async def probe(self) -> Capabilities:
        """Read every candidate object once and classify it. Used at setup.

        A "65535" right after power-on can mean "no value yet" (measured for
        iu_indoor_tmp), so callers should probe again later before hiding an entity.
        """
        names = [o.name for o in objects.INDOOR if o.name not in objects.NEVER_TOUCH]
        gap = self.transport.gap
        self.transport.gap = min(gap, SETUP_GAP)
        try:
            result = await self.transport.read(names)
        finally:
            self.transport.gap = gap
        caps = Capabilities(unknown=set(result.unknown))
        for name, raw in result.values.items():
            if codecs.sentinel(raw) is Missing.UNSUPPORTED:
                caps.unsupported.add(name)
            else:
                caps.supported.add(name)
        self.apply(result.values)
        for mode, (lo_name, hi_name) in _LIMIT_NAMES.items():
            lo, hi = self.values.get(lo_name), self.values.get(hi_name)
            if isinstance(lo, float) and isinstance(hi, float) and lo <= hi:
                caps.limits[mode] = (lo, hi)
        version = self.values.get("iu_main_ver")
        caps.firmware = version if isinstance(version, str) else None
        self.capabilities = caps
        return caps

    @property
    def refused(self) -> set[str]:
        """Writable objects this unit refused; kept with the capabilities (and so
        across restarts) once there are capabilities."""
        return self.capabilities.refused if self.capabilities is not None else self._refused

    async def recheck_unsupported(self) -> set[str]:
        """Read the "65535" objects again; any that now answer a value become supported.

        A unit that was just switched on can answer 65535 for a while (measured
        for iu_indoor_tmp), so run this a few minutes after the probe and at the
        next start before hiding an entity for good. Returns the promoted names.
        """
        caps = self.capabilities
        if caps is None or not caps.unsupported:
            return set()
        result = await self.transport.read(sorted(caps.unsupported), skip=caps.unknown)
        promoted = {
            name
            for name, raw in result.values.items()
            if codecs.sentinel(raw) is not Missing.UNSUPPORTED
        }
        caps.unsupported -= promoted
        caps.supported |= promoted
        caps.unknown |= result.unknown
        self.apply({n: result.values[n] for n in promoted})
        return promoted

    def supports(self, name: str, sub_id: int = 0) -> bool:
        """False only when the probe (or a read) proved there is no such value."""
        caps = self.capabilities
        if caps is None:
            return True
        if sub_id == 1:
            return name not in caps.unknown_adapter
        return name not in caps.unknown and name not in caps.unsupported

    # -- subscriptions -----------------------------------------------------------

    def subscribe(self, names: Iterable[str], sub_id: int = 0) -> Callable[[], None]:
        """Declare that an enabled entity needs these objects. Returns the undo."""
        wanted = [(sub_id, n) for n in names]
        self._subscriptions.update(wanted)
        for hook in list(self._subscription_hooks):
            hook()

        def unsubscribe() -> None:
            self._subscriptions.subtract(wanted)
            self._subscriptions += Counter()  # drop zero counts
            for hook in list(self._subscription_hooks):
                hook()

        return unsubscribe

    def subscribed_for(self, sub_id: int = 0) -> set[str]:
        return {
            n
            for (s, n), c in self._subscriptions.items()
            if s == sub_id and c > 0 and self.supports(n, sub_id)
        }

    @property
    def subscribed(self) -> set[str]:
        """Subscribed indoor-unit (sub-id 0) objects."""
        return self.subscribed_for(0)

    def on_subscriptions_changed(self, hook: Callable[[], None]) -> Callable[[], None]:
        self._subscription_hooks.append(hook)
        return lambda: self._subscription_hooks.remove(hook)

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    # -- reading -----------------------------------------------------------------

    async def refresh(self, names: Iterable[str] | None = None, sub_id: int = 0) -> set[str]:
        """Read the given names (default: everything subscribed) and update the cache."""
        source = self.subscribed_for(sub_id) if names is None else names
        wanted = [n for n in source if self.supports(n, sub_id)]
        if not wanted:
            return set()
        caps = self.capabilities
        skip = (caps.unknown_adapter if sub_id == 1 else caps.unknown) if caps else set()
        result = await self.transport.read(wanted, sub_id, skip=skip)
        if result.unknown and caps is not None:
            (caps.unknown_adapter if sub_id == 1 else caps.unknown).update(result.unknown)
        return self.apply(result.values, sub_id)

    def apply(self, raw_values: Mapping[str, Any], sub_id: int = 0) -> set[str]:
        """Merge raw values (from a read or a push) into the cache. Returns what changed.

        An empty value ("") after a real one keeps the real one for one more
        update instead of flashing to unknown: outdoor values read "" for
        20-50 s after every monitor refresh.
        """
        changed: set[str] = set()
        for name, raw in raw_values.items():
            obj = objects.get(name, sub_id)
            codec = obj.codec if obj else objects.Codec.RAW
            try:
                value = codecs.decode(codec, raw)
            except codecs.CodecError as err:
                _LOGGER.debug("%s: %s=%r: %s", self.transport.host, name, raw, err)
                value = codecs.decode(objects.Codec.RAW, raw)
            previous = self.values.get(name)
            if (
                value is Missing.EMPTY
                and previous is not None
                and not isinstance(previous, Missing)
                and name not in self._held_empty
            ):
                self._held_empty.add(name)
                self.raw[name] = raw
                continue
            self._held_empty.discard(name)
            self.raw[name] = raw
            if previous != value or name not in self.values:
                self.values[name] = value
                changed.add(name)
        if changed:
            for listener in list(self._listeners):
                listener(changed)
        return changed

    def value(self, name: str) -> Value | None:
        return self.values.get(name)

    # -- writing -----------------------------------------------------------------

    async def set_power(self, on: bool) -> None:
        await self._write("iu_onoff", on)

    async def set_mode(self, mode: OpMode) -> None:
        await self._write("iu_op_mode", mode)

    async def set_fan_speed(self, speed: FanSpeed) -> None:
        await self._write("iu_fan_spd", speed)

    async def set_vane_vertical(self, position: int) -> None:
        await self._write("iu_af_dir_vrt", position)

    async def set_vane_horizontal(self, position: int) -> None:
        await self._write("iu_af_dir_hrz", position)

    async def set_swing_vertical(self, on: bool) -> None:
        await self._write("iu_af_swg_vrt", on)

    async def set_swing_horizontal(self, on: bool) -> None:
        await self._write("iu_af_swg_hrz", on)

    async def set_switch(self, name: str, on: bool) -> None:
        """One of the on/off settings: Economy, Powerful, minimum heat, and so on."""
        obj = objects.get(name)
        if obj is None or obj.codec is not objects.Codec.BOOL or not obj.writable:
            raise ValueError(f"{name} is not a writable on/off setting")
        await self._write(name, on)

    async def set_setpoint(self, celsius: float) -> None:
        mode = self._target_mode()
        caps = self.capabilities
        if mode is not None and caps is not None and mode in caps.limits:
            lo, hi = caps.limits[mode]
            if not lo <= celsius <= hi:
                raise codecs.CodecError(f"setpoint {celsius} outside {lo}..{hi} for {mode.name}")
        await self._write("iu_set_tmp", celsius)

    def _target_mode(self) -> OpMode | None:
        if "iu_op_mode" in self._pending:
            return OpMode(int(self._pending["iu_op_mode"]))
        mode = self.values.get("iu_op_mode")
        return mode if isinstance(mode, OpMode) else None

    async def _write(self, name: str, value: Value) -> None:
        obj = objects.get(name)
        if obj is None or not obj.writable:
            raise ValueError(f"{name} is not writable")
        if not self.supports(name):
            raise ValueError(f"the unit does not support {name}")
        raw = codecs.encode(obj.codec, value)
        self._pending[name] = raw
        if self._flush is None:
            self._flush = asyncio.ensure_future(self._flush_later())
        await asyncio.shield(self._flush)

    async def _flush_later(self) -> None:
        """Send everything written within WRITE_COALESCE_S together.

        Only one name per SetParam has been confirmed on a real adapter, so the
        names go out one request each, power first, then mode, setpoint and fan
        (a mode change can change the valid setpoint range). A failure stops the
        rest and is raised to every caller of this flush.
        """
        await asyncio.sleep(WRITE_COALESCE_S)
        pending, self._pending, self._flush = self._pending, {}, None
        for name in sorted(pending, key=_write_order):
            try:
                await self.transport.set_values({name: pending[name]})
            except AirstageWriteError as err:
                if err.write_res == "unknown":
                    self.refused.add(name)
                raise
            self.refused.discard(name)
            self.apply({name: pending[name]})


_WRITE_ORDER: Final = ("iu_onoff", "iu_op_mode", "iu_set_tmp", "iu_fan_spd")


def _write_order(name: str) -> tuple[int, str]:
    return (_WRITE_ORDER.index(name) if name in _WRITE_ORDER else len(_WRITE_ORDER), name)
