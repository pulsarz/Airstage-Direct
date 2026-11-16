"""Airstage Direct for Fujitsu: local control of Fujitsu Airstage Wi-Fi adapters.

One config entry per outdoor system; one subentry per indoor unit (adapter).
Not affiliated with Fujitsu.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .compressor import CompressorMonitor
from .compressor_sensors import is_compressor_unique_id
from .const import OPT_ESTIMATES, OPT_PREFERRED_UNIT, STORED_ONLY, SUBENTRY_INDOOR_UNIT
from .coordinator import UnitCoordinator
from .estimate_sensors import estimate_entity_ids, is_estimate_unique_id
from .estimation import SystemEstimates
from .hub import async_get_hub
from .outdoor import OutdoorSource

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


@dataclass
class AirstageRuntime:
    units: dict[str, UnitCoordinator]  # by subentry id
    outdoor: OutdoorSource
    estimates: SystemEstimates | None
    layout: str
    compressor: CompressorMonitor | None = None  # experimental: needs a power sensor


type AirstageConfigEntry = ConfigEntry[AirstageRuntime]


def _layout(entry: ConfigEntry) -> str:
    """What a reload must follow: the units and their settings, and the options;
    not what the integration stores itself (capabilities, the learned offset)."""
    units = {
        sub.subentry_id: (
            sub.title,
            {k: v for k, v in sorted(sub.data.items()) if k not in STORED_ONLY},
        )
        for sub in entry.get_subentries_of_type(SUBENTRY_INDOOR_UNIT)
    }
    return repr((sorted(units.items()), sorted(entry.options.items())))


async def async_setup_entry(hass: HomeAssistant, entry: AirstageConfigEntry) -> bool:
    hub = await async_get_hub(hass)
    units = {
        sub.subentry_id: UnitCoordinator(hass, entry, sub, hub)
        for sub in entry.get_subentries_of_type(SUBENTRY_INDOOR_UNIT)
    }
    outdoor = OutdoorSource(
        list(units.values()), units.get(entry.options.get(OPT_PREFERRED_UNIT) or "")
    )
    estimates = (
        SystemEstimates(hass, entry.entry_id, units, outdoor, dict(entry.options))
        if entry.options.get(OPT_ESTIMATES, True)
        else None
    )
    options = dict(entry.options)
    compressor = CompressorMonitor(hass, options) if CompressorMonitor.configured(options) else None
    entry.runtime_data = AirstageRuntime(units, outdoor, estimates, _layout(entry), compressor)
    # Different adapters: starting them in parallel keeps one request per adapter.
    await asyncio.gather(*(unit.async_start() for unit in units.values()))
    outdoor.start()
    if compressor is not None:
        compressor.start()
    if estimates is not None and estimates.active:
        await estimates.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # The first state read, now that the enabled entities have subscribed
    # (Register sends no initial dump).
    await asyncio.gather(*(unit.async_refresh() for unit in units.values()))
    # One monitor read now, so monitor values (and the estimates) need not wait
    # for the first scheduled read after learning; then once per refresh.
    await asyncio.gather(*(unit.async_read_monitor_now() for unit in units.values()))
    _remove_stale_entities(hass, entry)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


def _remove_stale_entities(hass: HomeAssistant, entry: AirstageConfigEntry) -> None:
    """Estimates that are switched off, units without a model, compressor sensors
    without a power sensor: remove their entities instead of leaving them
    behind as unavailable."""
    estimates = entry.runtime_data.estimates
    wanted = set()
    if estimates is not None and estimates.active:
        wanted = {e.unique_id for e in estimate_entity_ids(estimates, entry.entry_id)}
        wanted.add(f"{entry.entry_id}_reset_estimates")
    registry = er.async_get(hass)
    compressor = entry.runtime_data.compressor is not None
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        unique_id = entity.unique_id
        stale = (is_estimate_unique_id(unique_id) and unique_id not in wanted) or (
            is_compressor_unique_id(unique_id) and not compressor
        )
        if stale:
            registry.async_remove(entity.entity_id)


async def _async_update_listener(hass: HomeAssistant, entry: AirstageConfigEntry) -> None:
    """Reload when units change; stored capabilities alone are no reason."""
    if _layout(entry) != entry.runtime_data.layout:
        hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AirstageConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    entry.runtime_data.outdoor.stop()
    if (compressor := entry.runtime_data.compressor) is not None:
        compressor.stop()
    if (estimates := entry.runtime_data.estimates) is not None and estimates.active:
        await estimates.async_stop()
    await asyncio.gather(*(unit.async_stop() for unit in entry.runtime_data.units.values()))
    return unloaded
