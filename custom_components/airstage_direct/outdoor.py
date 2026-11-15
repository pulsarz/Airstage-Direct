"""The shared outdoor unit: one device per system, its values from one indoor unit.

Every adapter reports the same outdoor unit, but two adapters read a cycle
apart can disagree (measured), and a unit that is off may not report outdoor
values. So one indoor unit that is on is the source; only it subscribes to the
outdoor objects, and the subscriptions move when the source changes. With every
unit off, outdoor temperature falls back to the source's own iu_outdoor_tmp.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable

from homeassistant.core import CALLBACK_TYPE, callback

from .coordinator import UnitCoordinator

Listener = Callable[[], None]


class OutdoorSource:
    def __init__(
        self, units: list[UnitCoordinator], preferred: UnitCoordinator | None = None
    ) -> None:
        self.units = units
        self.preferred = preferred
        self.source: UnitCoordinator | None = preferred or (units[0] if units else None)
        self._wanted: Counter[str] = Counter()
        self._undo: Callable[[], None] | None = None
        self._listeners: list[Listener] = []
        self._unsubs: list[CALLBACK_TYPE] = []

    def start(self) -> None:
        for unit in self.units:
            self._unsubs.append(unit.async_add_listener(self._units_updated))
        self._pick()

    def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._undo is not None:
            self._undo()
            self._undo = None

    def supports(self, name: str) -> bool:
        return any(unit.device.supports(name) for unit in self.units)

    def subscribe(self, names: tuple[str, ...]) -> Callable[[], None]:
        self._wanted.update(names)
        self._resubscribe()

        def undo() -> None:
            self._wanted.subtract(names)
            self._wanted += Counter()
            self._resubscribe()

        return undo

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def _units_updated(self) -> None:
        self._pick()
        for listener in list(self._listeners):
            listener()

    def _pick(self) -> None:
        """The preferred unit when it is on; else keep the current source while it
        is on; else the first unit that is on; with every unit off, keep it."""
        current = self.source
        preferred = self.preferred
        if preferred is not None and preferred.device.value("iu_onoff") is True:
            if preferred is not current:
                self.source = preferred
                self._resubscribe()
            return
        if current is not None and current.device.value("iu_onoff") is True:
            return
        on = [u for u in self.units if u.device.value("iu_onoff") is True]
        if on and on[0] is not current:
            self.source = on[0]
            self._resubscribe()

    def _resubscribe(self) -> None:
        if self._undo is not None:
            self._undo()
            self._undo = None
        if self.source is not None and self._wanted:
            self._undo = self.source.device.subscribe(tuple(self._wanted))
