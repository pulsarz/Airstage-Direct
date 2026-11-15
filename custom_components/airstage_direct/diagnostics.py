"""Diagnostics, with the adapter's MAC, serial, addresses, SSID and push key redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from . import AirstageConfigEntry
from .const import CONF_DEVICE_ID, DOMAIN

TO_REDACT = {
    CONF_HOST,
    CONF_DEVICE_ID,
    "host",
    "listen_ip",
    "wla_serial",
    "wla_local_ip_addr",
    "wla_cnct_ssid",
    "unique_id",
    "push_key",
}


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: AirstageConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """The same as the entry's, limited to one indoor unit (or the outdoor unit)."""
    data = await async_get_config_entry_diagnostics(hass, entry)
    ids = {identifier for domain, identifier in device.identifiers if domain == DOMAIN}
    units = entry.runtime_data.units
    keep = [
        sub_id
        for sub_id, sub in entry.subentries.items()
        if sub.data.get(CONF_DEVICE_ID) in ids and sub_id in units
    ]
    if keep:
        titles = {entry.subentries[sub_id].title for sub_id in keep}
        data["subentries"] = [s for s in data["subentries"] if s["title"] in titles]
    return data


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AirstageConfigEntry
) -> dict[str, Any]:
    units = entry.runtime_data.units
    return {
        "subentries": [
            async_redact_data(
                {
                    "title": sub.title,
                    "unique_id": sub.unique_id,
                    "data": dict(sub.data),
                    "unit": units[sub_id].diagnostics if sub_id in units else None,
                },
                TO_REDACT,
            )
            for sub_id, sub in entry.subentries.items()
        ],
    }
