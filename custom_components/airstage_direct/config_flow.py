"""Config flow: a system (one outdoor unit) with one subentry per indoor unit."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Any

import probatio
from homeassistant.components import network
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)

from .api import discovery
from .api.cycles import DEFAULT_OFF_W, DEFAULT_ON_W
from .api.estimates import DEFAULT_AIRFLOW_SCALE, DEFAULT_EFFECTIVENESS, FAN_CURVES
from .api.transport import (
    AirstageConnectionError,
    AirstageError,
    AirstageResponseError,
    Transport,
    arp_mac,
    format_device_id,
)
from .const import (
    CLIMATE_TEMPERATURE_INTAKE,
    CLIMATE_TEMPERATURE_ROOM,
    CONF_CURVE_INTERCEPT,
    CONF_CURVE_SLOPE,
    CONF_DEVICE_ID,
    CONF_HUMIDITY_ENTITY,
    CONF_MODEL,
    CONF_PORT,
    CONF_RATED_AIRFLOW,
    CONF_ROOM_SENSOR,
    DOMAIN,
    GUARD_DEFAULT_BAND,
    GUARD_DEFAULT_FAN,
    GUARD_DEFAULT_HOLD_OFFSET,
    GUARD_DEFAULT_MAX_HOLD,
    GUARD_FAN_LEAVE,
    GUARD_FAN_OPTIONS,
    MODEL_CUSTOM,
    MODEL_NONE,
    OPT_AIRFLOW_SCALE,
    OPT_CLIMATE_TEMPERATURE,
    OPT_COMPRESSOR_OFF_W,
    OPT_COMPRESSOR_ON_W,
    OPT_EFFECTIVENESS,
    OPT_ESTIMATES,
    OPT_GUARD,
    OPT_GUARD_BAND,
    OPT_GUARD_FAN,
    OPT_GUARD_HOLD,
    OPT_GUARD_HOLD_OFFSET,
    OPT_GUARD_MAX_HOLD,
    OPT_METER,
    OPT_POWER_SENSOR,
    OPT_PREFERRED_UNIT,
    OPT_PUSH,
    SUBENTRY_INDOOR_UNIT,
)
from .hub import async_get_hub

_LOGGER = logging.getLogger(__name__)

ADAPTER_PORT = 80  # the adapters listen on 80; tests patch this
DEFAULT_SYSTEM_NAME = "Airco"
LOOKUP_TIMEOUT_S = 2.0


class ValidationError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def configured_device_ids(hass: HomeAssistant) -> dict[str, str]:
    """MAC -> host of every indoor unit in every entry."""
    return {
        sub.data[CONF_DEVICE_ID]: sub.data[CONF_HOST]
        for entry in hass.config_entries.async_entries(DOMAIN)
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_INDOOR_UNIT
    }


async def async_validate_unit(hass: HomeAssistant, host: str, mac: str | None) -> str:
    """Contact the adapter once and return its device_id (MAC). Raises ValidationError."""
    configured = configured_device_ids(hass)
    if host in configured.values():
        raise ValidationError("already_configured")
    device_id = ""
    if mac:
        try:
            device_id = format_device_id(mac)
        except ValueError as err:
            raise ValidationError("invalid_mac") from err
        if device_id in configured:
            raise ValidationError("already_configured")
    # Not set up yet, so no other transport talks to this adapter.
    transport = Transport(async_get_clientsession(hass), host, device_id, port=ADAPTER_PORT)
    try:
        device_id = await transport.resolve_device_id()
    except AirstageResponseError as err:
        raise ValidationError("mac_needed" if not mac else "wrong_mac") from err
    except AirstageConnectionError as err:
        raise ValidationError("cannot_connect") from err
    except AirstageError as err:
        raise ValidationError("unknown") from err
    if device_id in configured:
        raise ValidationError("already_configured")
    return device_id


async def async_scan_network(hass: HomeAssistant) -> list[str]:
    """Adapters in Home Assistant's own IPv4 networks, skipping configured ones."""
    skip = (
        set(configured_device_ids(hass).values()) | (await async_get_hub(hass)).configured_hosts()
    )
    hosts: list[str] = []
    for adapter in await network.async_get_adapters(hass):
        if not adapter["enabled"]:
            continue
        for ipv4 in adapter["ipv4"]:
            net = ipaddress.ip_network(f"{ipv4['address']}/{ipv4['network_prefix']}", strict=False)
            if net.is_loopback or net.num_addresses > discovery.MAX_SCAN_HOSTS + 2:
                continue
            hosts += discovery.hosts_in(str(net), skip=skip | {ipv4["address"]})
    return await discovery.scan(async_get_clientsession(hass), hosts, port=ADAPTER_PORT)


def unit_schema(host: str = "", name: str = "") -> probatio.Schema:
    return probatio.Schema(
        {
            probatio.Required(CONF_HOST, default=host): str,
            probatio.Required(CONF_NAME, default=name): str,
            probatio.Optional(CONF_DEVICE_ID, default=""): str,
        }
    )


def unit_data(host: str, device_id: str) -> dict[str, Any]:
    data: dict[str, Any] = {CONF_HOST: host, CONF_DEVICE_ID: device_id}
    if ADAPTER_PORT != 80:
        data[CONF_PORT] = ADAPTER_PORT
    return data


async def async_describe_host(hass: HomeAssistant, host: str) -> str:
    """An address with its reverse-DNS name and MAC where known, for the pick list."""
    parts: list[str] = []
    try:
        name = await asyncio.wait_for(
            hass.async_add_executor_job(socket.gethostbyaddr, host), LOOKUP_TIMEOUT_S
        )
        if name[0] and name[0] != host:
            parts.append(name[0])
    except OSError, TimeoutError:
        pass
    if mac := await hass.async_add_executor_job(arp_mac, host):
        parts.append(format_mac(mac))
    return f"{host} ({', '.join(parts)})" if parts else host


class _UnitSteps:
    """The "search network" / "enter manually" steps shared by both flows."""

    hass: HomeAssistant
    _found: list[SelectOptionDict]
    _search_task: asyncio.Task[None] | None

    def _menu(self) -> list[str]:
        return ["search", "manual"]

    def _start_search(self) -> asyncio.Task[None]:
        if self._search_task is None:
            self._search_task = self.hass.async_create_task(self._run_search())
        return self._search_task

    async def _run_search(self) -> None:
        try:
            hosts = await async_scan_network(self.hass)
        except (OSError, ValueError) as err:
            _LOGGER.debug("network scan failed: %s", err)
            hosts = []
        labels = await asyncio.gather(*(async_describe_host(self.hass, h) for h in hosts))
        self._found = [
            SelectOptionDict(value=h, label=label) for h, label in zip(hosts, labels, strict=True)
        ]

    def _search_finished(self) -> str | None:
        """The next step once the search task is done, else None."""
        task = self._search_task
        if task is None or not task.done():
            return None
        self._search_task = None
        return "pick" if self._found else "none_found"

    def _pick_schema(self) -> probatio.Schema:
        return probatio.Schema(
            {
                probatio.Required(CONF_HOST): SelectSelector(
                    SelectSelectorConfig(options=self._found)
                ),
                probatio.Required(CONF_NAME): str,
            }
        )


class AirstageConfigFlow(_UnitSteps, ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._system_name = DEFAULT_SYSTEM_NAME
        self._found = []
        self._search_task = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> AirstageOptionsFlow:
        return AirstageOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_INDOOR_UNIT: IndoorUnitSubentryFlow}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._system_name = user_input[CONF_NAME]
            return await self.async_step_first_unit()
        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema(
                {probatio.Required(CONF_NAME, default=DEFAULT_SYSTEM_NAME): str}
            ),
        )

    async def async_step_first_unit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="first_unit", menu_options=self._menu())

    async def async_step_search(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if (next_step := self._search_finished()) is not None:
            return self.async_show_progress_done(next_step_id=next_step)
        return self.async_show_progress(
            step_id="search", progress_action="searching", progress_task=self._start_search()
        )

    async def async_step_none_found(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="manual", data_schema=unit_schema(), errors={"base": "none_found"}
        )

    async def async_step_pick(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="pick", data_schema=self._pick_schema())
        return await self.async_step_manual(user_input)

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            try:
                device_id = await async_validate_unit(
                    self.hass, host, user_input.get(CONF_DEVICE_ID, "").strip() or None
                )
            except ValidationError as err:
                if err.key == "already_configured":
                    return self.async_abort(reason="already_configured")
                errors["base"] = err.key
            else:
                return self.async_create_entry(
                    title=self._system_name,
                    data={},
                    subentries=[
                        {
                            "subentry_type": SUBENTRY_INDOOR_UNIT,
                            "data": unit_data(host, device_id),
                            "title": user_input[CONF_NAME],
                            "unique_id": device_id,
                        }
                    ],
                )
        defaults = user_input or {}
        return self.async_show_form(
            step_id="manual",
            data_schema=unit_schema(defaults.get(CONF_HOST, ""), defaults.get(CONF_NAME, "")),
            errors=errors,
        )


class IndoorUnitSubentryFlow(_UnitSteps, ConfigSubentryFlow):
    """Add an indoor unit to a system, or change its IP address."""

    def __init__(self) -> None:
        self._found = []
        self._search_task = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return self.async_show_menu(step_id="user", menu_options=self._menu())

    async def async_step_search(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if (next_step := self._search_finished()) is not None:
            return self.async_show_progress_done(next_step_id=next_step)
        return self.async_show_progress(
            step_id="search", progress_action="searching", progress_task=self._start_search()
        )

    async def async_step_none_found(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return self.async_show_form(
            step_id="manual", data_schema=unit_schema(), errors={"base": "none_found"}
        )

    async def async_step_pick(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="pick", data_schema=self._pick_schema())
        return await self.async_step_manual(user_input)

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            try:
                device_id = await async_validate_unit(
                    self.hass, host, user_input.get(CONF_DEVICE_ID, "").strip() or None
                )
            except ValidationError as err:
                if err.key == "already_configured":
                    return self.async_abort(reason="already_configured")
                errors["base"] = err.key
            else:
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data=unit_data(host, device_id),
                    unique_id=device_id,
                )
        defaults = user_input or {}
        return self.async_show_form(
            step_id="manual",
            data_schema=unit_schema(defaults.get(CONF_HOST, ""), defaults.get(CONF_NAME, "")),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """The unit's settings: its IP address (checked by its MAC) and, for the
        estimates, its model and an optional room humidity sensor."""
        subentry = self._get_reconfigure_subentry()
        device_id: str = subentry.data[CONF_DEVICE_ID]
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            settings = unit_settings(user_input)
            if settings is None:
                errors["base"] = "curve_needed"
            elif host != subentry.data[CONF_HOST]:
                errors = await self._check_new_host(host, device_id)
            if not errors and settings is not None:
                data = {k: v for k, v in subentry.data.items() if k not in UNIT_SETTING_KEYS}
                return self.async_update_and_abort(
                    self._get_entry(), subentry, data={**data, **settings, CONF_HOST: host}
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                unit_settings_schema(),
                {
                    **subentry.data,
                    CONF_MODEL: model_option(subentry.data.get(CONF_MODEL)),
                    **(user_input or {}),
                },
            ),
            errors=errors,
        )

    async def _check_new_host(self, host: str, device_id: str) -> dict[str, str]:
        others = {h for mac, h in configured_device_ids(self.hass).items() if mac != device_id}
        if host in others:
            return {"base": "host_in_use"}  # another unit's adapter: never talk to it twice
        transport = Transport(
            async_get_clientsession(self.hass), host, device_id, port=ADAPTER_PORT
        )
        try:
            await transport.get(["iu_set_tmp"])
        except AirstageResponseError:
            return {"base": "wrong_mac"}  # answers, but not with this MAC
        except AirstageError:
            return {"base": "cannot_connect"}
        return {}


UNIT_SETTING_KEYS = (
    CONF_MODEL,
    CONF_CURVE_SLOPE,
    CONF_CURVE_INTERCEPT,
    CONF_RATED_AIRFLOW,
    CONF_HUMIDITY_ENTITY,
    CONF_ROOM_SENSOR,
)


MODEL_OPTION_NONE = "none"


def model_option(stored: str | None) -> str:
    """The selector option for a stored model. Option keys must be lowercase
    (translation keys), while the stored value stays the model's own name."""
    if stored == MODEL_CUSTOM:
        return MODEL_CUSTOM
    if stored in FAN_CURVES:
        return stored.lower()
    return MODEL_OPTION_NONE


def model_from_option(option: str | None) -> str:
    """The model to store for a selector option ("" for none)."""
    if option == MODEL_CUSTOM:
        return MODEL_CUSTOM
    for model in FAN_CURVES:
        if option and option.lower() == model.lower():
            return model
    return MODEL_NONE


def unit_settings_schema() -> probatio.Schema:
    models = [MODEL_OPTION_NONE, *(m.lower() for m in FAN_CURVES), MODEL_CUSTOM]
    return probatio.Schema(
        {
            probatio.Required(CONF_HOST): str,
            probatio.Optional(CONF_MODEL, default=MODEL_OPTION_NONE): SelectSelector(
                SelectSelectorConfig(options=models, translation_key="model")
            ),
            probatio.Optional(CONF_CURVE_SLOPE): NumberSelector(
                NumberSelectorConfig(min=0.05, max=5, step=0.001, mode=NumberSelectorMode.BOX)
            ),
            probatio.Optional(CONF_CURVE_INTERCEPT): NumberSelector(
                NumberSelectorConfig(min=-2000, max=2000, step=1, mode=NumberSelectorMode.BOX)
            ),
            probatio.Optional(CONF_RATED_AIRFLOW): NumberSelector(
                NumberSelectorConfig(min=50, max=5000, step=1, mode=NumberSelectorMode.BOX)
            ),
            probatio.Optional(CONF_HUMIDITY_ENTITY): EntitySelector(
                EntitySelectorConfig(domain="sensor", device_class="humidity")
            ),
            probatio.Optional(CONF_ROOM_SENSOR): EntitySelector(
                EntitySelectorConfig(domain="sensor", device_class="temperature")
            ),
        }
    )


def unit_settings(user_input: dict[str, Any]) -> dict[str, Any] | None:
    """The estimate settings to store, or None when "custom" lacks its curve."""
    model = model_from_option(user_input.get(CONF_MODEL))
    settings: dict[str, Any] = {CONF_MODEL: model}
    if model == MODEL_CUSTOM:
        slope, intercept = user_input.get(CONF_CURVE_SLOPE), user_input.get(CONF_CURVE_INTERCEPT)
        if slope is None or intercept is None:
            return None
        settings |= {CONF_CURVE_SLOPE: float(slope), CONF_CURVE_INTERCEPT: float(intercept)}
        if rated := user_input.get(CONF_RATED_AIRFLOW):
            settings[CONF_RATED_AIRFLOW] = float(rated)
    if humidity := user_input.get(CONF_HUMIDITY_ENTITY):
        settings[CONF_HUMIDITY_ENTITY] = humidity
    if room := user_input.get(CONF_ROOM_SENSOR):
        settings[CONF_ROOM_SENSOR] = room
    return settings


SECTION_COMPRESSOR = "compressor"  # experimental: power sensor and display
SECTION_GUARD = "short_cycle_guard"  # experimental: the guard
SECTIONS: dict[str, tuple[str, ...]] = {
    SECTION_COMPRESSOR: (
        OPT_POWER_SENSOR,
        OPT_COMPRESSOR_ON_W,
        OPT_COMPRESSOR_OFF_W,
        OPT_CLIMATE_TEMPERATURE,
    ),
    SECTION_GUARD: (
        OPT_GUARD,
        OPT_GUARD_FAN,
        OPT_GUARD_HOLD,
        OPT_GUARD_HOLD_OFFSET,
        OPT_GUARD_BAND,
        OPT_GUARD_MAX_HOLD,
    ),
}


def _number(lo: float, hi: float, step: float, unit: str) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=lo, max=hi, step=step, unit_of_measurement=unit, mode=NumberSelectorMode.BOX
        )
    )


def _flatten(user_input: dict[str, Any]) -> dict[str, Any]:
    """Options are stored flat; the form groups the experimental ones in sections."""
    flat = {k: v for k, v in user_input.items() if k not in SECTIONS}
    for key in SECTIONS:
        flat.update(user_input.get(key) or {})
    return flat


def _grouped(options: dict[str, Any]) -> dict[str, Any]:
    grouped = {k: v for k, v in options.items() if not any(k in ks for ks in SECTIONS.values())}
    for section_key, keys in SECTIONS.items():
        grouped[section_key] = {k: options[k] for k in keys if k in options}
    return grouped


class AirstageOptionsFlow(OptionsFlow):
    """System settings: estimates, the electricity meter, the outdoor source, push,
    and (experimental, folded away) compressor starts and the short-cycle guard."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            flat = _flatten(user_input)
            on_w = flat.get(OPT_COMPRESSOR_ON_W, DEFAULT_ON_W)
            off_w = flat.get(OPT_COMPRESSOR_OFF_W, DEFAULT_OFF_W)
            if off_w >= on_w:
                errors["base"] = "compressor_levels"
            else:
                return self.async_create_entry(data=flat)
        units = [
            SelectOptionDict(value=sub.subentry_id, label=sub.title)
            for sub in self.config_entry.get_subentries_of_type(SUBENTRY_INDOOR_UNIT)
        ]
        compressor = probatio.Schema(
            {
                probatio.Optional(OPT_POWER_SENSOR): EntitySelector(
                    EntitySelectorConfig(domain="sensor", device_class="power")
                ),
                probatio.Optional(OPT_COMPRESSOR_ON_W, default=DEFAULT_ON_W): _number(
                    20, 3000, 10, "W"
                ),
                probatio.Optional(OPT_COMPRESSOR_OFF_W, default=DEFAULT_OFF_W): _number(
                    10, 2000, 10, "W"
                ),
                probatio.Optional(
                    OPT_CLIMATE_TEMPERATURE, default=CLIMATE_TEMPERATURE_ROOM
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[CLIMATE_TEMPERATURE_ROOM, CLIMATE_TEMPERATURE_INTAKE],
                        translation_key=OPT_CLIMATE_TEMPERATURE,
                    )
                ),
            }
        )
        guard = probatio.Schema(
            {
                probatio.Optional(OPT_GUARD, default=False): BooleanSelector(),
                probatio.Optional(OPT_GUARD_FAN, default=GUARD_DEFAULT_FAN): SelectSelector(
                    SelectSelectorConfig(
                        options=[GUARD_FAN_LEAVE, *GUARD_FAN_OPTIONS],
                        translation_key=OPT_GUARD_FAN,
                    )
                ),
                probatio.Optional(OPT_GUARD_HOLD, default=True): BooleanSelector(),
                probatio.Optional(
                    OPT_GUARD_HOLD_OFFSET, default=GUARD_DEFAULT_HOLD_OFFSET
                ): _number(0.5, 2.0, 0.5, "°C"),
                probatio.Optional(OPT_GUARD_BAND, default=GUARD_DEFAULT_BAND): _number(
                    0.1, 1.0, 0.1, "°C"
                ),
                probatio.Optional(OPT_GUARD_MAX_HOLD, default=GUARD_DEFAULT_MAX_HOLD): _number(
                    10, 120, 5, "min"
                ),
            }
        )
        schema = probatio.Schema(
            {
                probatio.Optional(OPT_ESTIMATES, default=True): BooleanSelector(),
                probatio.Optional(OPT_EFFECTIVENESS, default=DEFAULT_EFFECTIVENESS): NumberSelector(
                    NumberSelectorConfig(min=0.3, max=0.99, step=0.01, mode=NumberSelectorMode.BOX)
                ),
                probatio.Optional(OPT_AIRFLOW_SCALE, default=DEFAULT_AIRFLOW_SCALE): NumberSelector(
                    NumberSelectorConfig(min=0.5, max=1.5, step=0.01, mode=NumberSelectorMode.BOX)
                ),
                probatio.Optional(OPT_METER): EntitySelector(
                    EntitySelectorConfig(domain="sensor", device_class="energy")
                ),
                probatio.Optional(OPT_PREFERRED_UNIT): SelectSelector(
                    SelectSelectorConfig(options=units)
                ),
                probatio.Optional(OPT_PUSH, default=True): BooleanSelector(),
                probatio.Optional(SECTION_COMPRESSOR): section(compressor, {"collapsed": True}),
                probatio.Optional(SECTION_GUARD): section(guard, {"collapsed": True}),
            }
        )  # fmt: skip
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema, user_input or _grouped(dict(self.config_entry.options))
            ),
            errors=errors,
        )
