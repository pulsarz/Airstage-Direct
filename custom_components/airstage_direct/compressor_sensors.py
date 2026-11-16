"""Compressor start sensors on the outdoor unit (experimental, needs a power sensor)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import RestoreSensor, SensorEntity, SensorStateClass
from homeassistant.helpers.device_registry import DeviceInfo

from .compressor import CompressorMonitor
from .entity import outdoor_device_info

COMPRESSOR_KEYS = frozenset({"compressor_starts", "compressor_starts_last_hour"})


def is_compressor_unique_id(unique_id: str) -> bool:
    return any(unique_id.endswith(f"_{key}") for key in COMPRESSOR_KEYS)


class _CompressorBase(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, monitor: CompressorMonitor, entry_id: str, title: str, key: str) -> None:
        self.monitor = monitor
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info: DeviceInfo = outdoor_device_info(entry_id, title)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.monitor.add_listener(self.async_write_ha_state))


class CompressorStarts(_CompressorBase, RestoreSensor):
    """Every compressor start seen, counted up across restarts."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, monitor: CompressorMonitor, entry_id: str, title: str) -> None:
        super().__init__(monitor, entry_id, title, "compressor_starts")
        self._base = 0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and isinstance(last.native_value, int | float):
            self._base = int(last.native_value)

    @property
    def native_value(self) -> int:
        return self._base + self.monitor.tracker.starts


class CompressorStartsLastHour(_CompressorBase):
    """Starts in the last 60 minutes, with the last run and pause."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"last_run_minutes", "last_pause_minutes"})

    def __init__(self, monitor: CompressorMonitor, entry_id: str, title: str) -> None:
        super().__init__(monitor, entry_id, title, "compressor_starts_last_hour")

    @property
    def native_value(self) -> int:
        return self.monitor.starts_last_hour()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        t = self.monitor.tracker
        return {
            "compressor_running": t.running,
            "last_run_minutes": round(t.last_run.seconds / 60, 1) if t.last_run else None,
            "last_pause_minutes": round(t.last_pause.seconds / 60, 1) if t.last_pause else None,
            "power_sensor": self.monitor.entity_id,
        }


def compressor_sensors(monitor: CompressorMonitor, entry_id: str, title: str) -> list[SensorEntity]:
    return [
        CompressorStarts(monitor, entry_id, title),
        CompressorStartsLastHour(monitor, entry_id, title),
    ]
