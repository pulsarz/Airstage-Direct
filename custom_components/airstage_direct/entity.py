"""Base entities (indoor unit, outdoor unit) and the shared write-error handling."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo, format_mac
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.codecs import CodecError
from .api.device import Device
from .api.transport import AirstageError, AirstageWriteError
from .const import DOMAIN, MANUFACTURER
from .coordinator import UnitCoordinator
from .outdoor import OutdoorSource


def unit_device_info(coordinator: UnitCoordinator) -> DeviceInfo:
    """The indoor unit's device, complete, whichever entity creates it first."""
    device_id = coordinator.device_id
    caps = coordinator.device.capabilities
    return DeviceInfo(
        identifiers={(DOMAIN, device_id)},
        connections={(CONNECTION_NETWORK_MAC, format_mac(device_id))},
        manufacturer=MANUFACTURER,
        model="Airstage indoor unit",
        name=coordinator.subentry.title,
        sw_version=caps.firmware if caps else None,
    )


def outdoor_device_info(entry_id: str, title: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_outdoor")},
        manufacturer=MANUFACTURER,
        model="Airstage outdoor unit",
        name=f"{title} outdoor unit",
    )


class AirstageEntity(CoordinatorEntity[UnitCoordinator]):
    """An indoor-unit entity. It declares the objects it needs; they are read only
    while the entity is enabled (Home Assistant never adds disabled entities)."""

    _attr_has_entity_name = True
    _names: tuple[str, ...] = ()
    _sub_id: int = 0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._names:
            self.async_on_remove(self.device.subscribe(self._names, self._sub_id))

    def __init__(self, coordinator: UnitCoordinator, key: str) -> None:
        super().__init__(coordinator)
        device_id = coordinator.device_id
        self._attr_unique_id = f"{device_id}_{key}"
        self._attr_device_info = unit_device_info(coordinator)

    @property
    def device(self) -> Device:
        return self.coordinator.device

    @property
    def available(self) -> bool:
        return super().available and bool(self.coordinator.device.values)


class OutdoorEntity(Entity):
    """An entity of the shared outdoor unit; its values come from the source unit."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _names: tuple[str, ...] = ()

    def __init__(self, entry_id: str, title: str, outdoor: OutdoorSource, key: str) -> None:
        self.outdoor = outdoor
        self._attr_unique_id = f"{entry_id}_outdoor_{key}"
        self._attr_device_info = outdoor_device_info(entry_id, title)

    @property
    def source_device(self) -> Device | None:
        source = self.outdoor.source
        return source.device if source is not None else None

    @property
    def available(self) -> bool:
        source = self.outdoor.source
        return source is not None and source.last_update_success

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.outdoor.subscribe(self._names))
        self.async_on_remove(self.outdoor.add_listener(self.async_write_ha_state))


async def run_writes(*writes: Awaitable[None]) -> None:
    """Run writes together (the device sends them one name per request) and turn
    failures into translated errors."""
    try:
        await asyncio.gather(*writes)
    except CodecError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_value",
            translation_placeholders={"error": str(err)},
        ) from err
    except AirstageWriteError as err:
        refused = err.write_res == "unknown"
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="write_refused" if refused else "write_failed",
            translation_placeholders={"result": str(err.write_res)},
        ) from err
    except AirstageError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="connection_failed",
            translation_placeholders={"error": str(err)},
        ) from err
