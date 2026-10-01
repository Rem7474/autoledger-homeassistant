"""Config flow for AutoLedger integration."""

from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    AutoLedgerApiClient,
    AutoLedgerAuthError,
    AutoLedgerConnectionError,
    AutoLedgerTimeoutError,
)
from .const import (
    ASSIGNMENT_MODE_CORRELATION,
    ASSIGNMENT_MODE_FIXED,
    ASSIGNMENT_MODE_INPUT_SELECT,
    ASSIGNMENT_MODE_UNASSIGNED,
    CONF_API_KEY,
    CONF_ASSIGNMENT_MODE,
    CONF_BATTERY_SOC_ENTITY,
    CONF_CHARGER_ID,
    CONF_CHARGER_NAME,
    CONF_CHARGERS,
    CONF_CHARGING_LOCATION,
    CONF_CHARGING_STATUS_ENTITY,
    CONF_DEBOUNCE_SECONDS,
    CONF_DEVICE_ID,
    CONF_ENERGY_METER_ENTITY,
    CONF_ENERGY_METER_TYPE,
    CONF_HOST,
    CONF_LINKED_VEHICLE_ID,
    CONF_ODOMETER_ENTITY,
    CONF_VEHICLE_ID,
    CONF_VEHICLE_NAME,
    CONF_VEHICLE_SELECT_ENTITY,
    CONF_VEHICLES,
    CONF_VERIFY_SSL,
    DEFAULT_ASSIGNMENT_MODE,
    DEFAULT_CHARGING_LOCATION,
    DEFAULT_DEBOUNCE_SECONDS,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    ENERGY_METER_TYPE_SESSION,
    ENERGY_METER_TYPE_TOTAL_INCREASING,
    MAX_DEBOUNCE_SECONDS,
    MIN_DEBOUNCE_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


def _slugify(text: str) -> str:
    """Generate a clean slug identifier."""
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", text).strip("_").lower()
    return slug or "charger"


class AutoLedgerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for AutoLedger."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial user step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].rstrip("/")
            api_key = user_input[CONF_API_KEY]
            verify_ssl = user_input.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)

            normalized_host = host.lower()
            await self.async_set_unique_id(normalized_host)
            self._abort_if_unique_id_configured()

            session = async_get_clientsession(self.hass)
            client = AutoLedgerApiClient(
                host=host,
                api_key=api_key,
                session=session,
                verify_ssl=verify_ssl,
            )

            try:
                await client.async_test_connection()
            except AutoLedgerAuthError:
                errors["base"] = "invalid_auth"
            except (AutoLedgerConnectionError, AutoLedgerTimeoutError):
                errors["base"] = "cannot_connect"
            except Exception as err:
                _LOGGER.exception("Unexpected exception during connection test: %s", err)
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=f"AutoLedger ({host})",
                    data={
                        CONF_HOST: host,
                        CONF_API_KEY: api_key,
                        CONF_VERIFY_SSL: verify_ssl,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
                ),
                vol.Required(CONF_API_KEY): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Optional(
                    CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL
                ): selector.BooleanSelector(),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> AutoLedgerOptionsFlowHandler:
        """Get the options flow for this handler."""
        return AutoLedgerOptionsFlowHandler(config_entry)


class AutoLedgerOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for configuring AutoLedger vehicles and charging stations."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._temp_vehicle: dict[str, Any] = {}
        self._temp_charger: dict[str, Any] = {}
        self._vehicles_from_api: list[dict[str, Any]] = []

    def _get_api_client(self) -> AutoLedgerApiClient:
        """Get or initialize the API client."""
        host = self._config_entry.data[CONF_HOST]
        api_key = self._config_entry.data[CONF_API_KEY]
        verify_ssl = self._config_entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
        session = async_get_clientsession(self.hass)
        return AutoLedgerApiClient(
            host=host,
            api_key=api_key,
            session=session,
            verify_ssl=verify_ssl,
        )

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Manage the main options menu (two main panes: vehicles & chargers)."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["manage_vehicles", "manage_chargers"],
        )

    # -------------------------------------------------------------------------
    # VEHICLE MANAGEMENT
    # -------------------------------------------------------------------------
    async def async_step_manage_vehicles(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage vehicle list: add, edit, remove."""
        configured_vehicles = self._config_entry.options.get(CONF_VEHICLES, {})
        menu_options = ["add_vehicle"]
        if configured_vehicles:
            menu_options.extend(["edit_vehicle", "remove_vehicle"])

        return self.async_show_menu(
            step_id="manage_vehicles",
            menu_options=menu_options,
        )

    async def async_step_add_vehicle(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Step 1 of adding a vehicle: pick AutoLedger vehicle & HA device."""
        errors: dict[str, str] = {}

        if not self._vehicles_from_api:
            client = self._get_api_client()
            try:
                self._vehicles_from_api = await client.async_get_vehicles()
            except Exception as err:
                _LOGGER.warning("Could not fetch vehicles from AutoLedger: %s", err)
                self._vehicles_from_api = []

        if user_input is not None:
            vehicle_id = user_input[CONF_VEHICLE_ID]
            v_name = vehicle_id
            for v in self._vehicles_from_api:
                if str(v.get("id")) == str(vehicle_id):
                    name_parts = [v.get("make"), v.get("model"), v.get("name")]
                    v_name = " ".join([p for p in name_parts if p]) or vehicle_id
                    break

            self._temp_vehicle = {
                CONF_VEHICLE_ID: vehicle_id,
                CONF_VEHICLE_NAME: v_name,
                CONF_DEVICE_ID: user_input.get(CONF_DEVICE_ID),
            }
            return await self.async_step_vehicle_mapping()

        vehicle_options = []
        for v in self._vehicles_from_api:
            v_id = str(v.get("id", ""))
            if not v_id:
                continue
            name_parts = [v.get("make"), v.get("model"), v.get("name")]
            label = " ".join([p for p in name_parts if p]) or f"Vehicle {v_id}"
            vehicle_options.append(selector.SelectOptionDict(value=v_id, label=label))

        if not vehicle_options:
            vehicle_options.append(
                selector.SelectOptionDict(value="custom_vehicle", label="Default Vehicle")
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_VEHICLE_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=vehicle_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_DEVICE_ID): selector.DeviceSelector(),
            }
        )

        return self.async_show_form(
            step_id="add_vehicle",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_edit_vehicle(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Select a vehicle to edit."""
        configured_vehicles: dict[str, Any] = self._config_entry.options.get(CONF_VEHICLES, {})

        if not configured_vehicles:
            return await self.async_step_manage_vehicles()

        if user_input is not None:
            vehicle_id = user_input[CONF_VEHICLE_ID]
            v_data = configured_vehicles.get(vehicle_id, {})
            self._temp_vehicle = dict(v_data)
            self._temp_vehicle[CONF_VEHICLE_ID] = vehicle_id
            return await self.async_step_vehicle_mapping()

        options = [
            selector.SelectOptionDict(
                value=vid,
                label=data.get(CONF_VEHICLE_NAME, f"Vehicle {vid}"),
            )
            for vid, data in configured_vehicles.items()
        ]

        schema = vol.Schema(
            {
                vol.Required(CONF_VEHICLE_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="edit_vehicle",
            data_schema=schema,
        )

    async def async_step_remove_vehicle(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Select a vehicle to remove."""
        configured_vehicles: dict[str, Any] = dict(
            self._config_entry.options.get(CONF_VEHICLES, {})
        )

        if not configured_vehicles:
            return await self.async_step_manage_vehicles()

        if user_input is not None:
            vehicle_id = user_input[CONF_VEHICLE_ID]
            if vehicle_id in configured_vehicles:
                del configured_vehicles[vehicle_id]

            new_options = dict(self._config_entry.options)
            new_options[CONF_VEHICLES] = configured_vehicles
            return self.async_create_entry(title="", data=new_options)

        options = [
            selector.SelectOptionDict(
                value=vid,
                label=data.get(CONF_VEHICLE_NAME, f"Vehicle {vid}"),
            )
            for vid, data in configured_vehicles.items()
        ]

        schema = vol.Schema(
            {
                vol.Required(CONF_VEHICLE_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="remove_vehicle",
            data_schema=schema,
        )

    async def async_step_vehicle_mapping(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure vehicle telemetry entities."""
        errors: dict[str, str] = {}
        vehicle_id = self._temp_vehicle.get(CONF_VEHICLE_ID, "")

        if user_input is not None:
            configured_vehicles: dict[str, Any] = dict(
                self._config_entry.options.get(CONF_VEHICLES, {})
            )

            configured_vehicles[vehicle_id] = {
                CONF_VEHICLE_ID: vehicle_id,
                CONF_VEHICLE_NAME: self._temp_vehicle.get(CONF_VEHICLE_NAME, vehicle_id),
                CONF_DEVICE_ID: self._temp_vehicle.get(CONF_DEVICE_ID),
                CONF_BATTERY_SOC_ENTITY: user_input.get(CONF_BATTERY_SOC_ENTITY),
                CONF_ODOMETER_ENTITY: user_input.get(CONF_ODOMETER_ENTITY),
                CONF_CHARGING_STATUS_ENTITY: user_input.get(CONF_CHARGING_STATUS_ENTITY),
            }

            new_options = dict(self._config_entry.options)
            new_options[CONF_VEHICLES] = configured_vehicles
            return self.async_create_entry(title="", data=new_options)

        device_id = self._temp_vehicle.get(CONF_DEVICE_ID)
        prefill_battery = self._temp_vehicle.get(CONF_BATTERY_SOC_ENTITY)
        prefill_odometer = self._temp_vehicle.get(CONF_ODOMETER_ENTITY)
        prefill_charging = self._temp_vehicle.get(CONF_CHARGING_STATUS_ENTITY)

        if device_id and not prefill_battery:
            ent_reg = er.async_get(self.hass)
            entries = er.async_entries_for_device(ent_reg, device_id)
            for entry in entries:
                dev_class = entry.device_class or entry.original_device_class
                ent_id = entry.entity_id

                # Battery SoC
                if not prefill_battery and (
                    dev_class == SensorDeviceClass.BATTERY or "battery" in ent_id or "soc" in ent_id
                ):
                    prefill_battery = ent_id

                # Odometer
                if not prefill_odometer and (
                    dev_class == SensorDeviceClass.DISTANCE
                    or "odometer" in ent_id
                    or "mileage" in ent_id
                    or "distance" in ent_id
                ):
                    prefill_odometer = ent_id

                # Vehicle internal charging status
                if not prefill_charging and (
                    dev_class
                    in (
                        BinarySensorDeviceClass.BATTERY_CHARGING,
                        BinarySensorDeviceClass.PLUG,
                    )
                    or "charging" in ent_id
                    or "plug" in ent_id
                ):
                    prefill_charging = ent_id

        fields: dict[Any, Any] = {
            vol.Optional(
                CONF_BATTERY_SOC_ENTITY,
                description={"suggested_value": prefill_battery},
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor",
                    device_class=SensorDeviceClass.BATTERY,
                )
            ),
            vol.Optional(
                CONF_ODOMETER_ENTITY,
                description={"suggested_value": prefill_odometer},
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor",
                    device_class=SensorDeviceClass.DISTANCE,
                )
            ),
            vol.Optional(
                CONF_CHARGING_STATUS_ENTITY,
                description={"suggested_value": prefill_charging},
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["binary_sensor", "sensor"],
                )
            ),
        }

        return self.async_show_form(
            step_id="vehicle_mapping",
            data_schema=vol.Schema(fields),
            errors=errors,
        )

    # -------------------------------------------------------------------------
    # CHARGING STATION MANAGEMENT
    # -------------------------------------------------------------------------
    async def async_step_manage_chargers(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage charging stations: add, edit, remove."""
        configured_chargers = self._config_entry.options.get(CONF_CHARGERS, {})
        menu_options = ["add_charger"]
        if configured_chargers:
            menu_options.extend(["edit_charger", "remove_charger"])

        return self.async_show_menu(
            step_id="manage_chargers",
            menu_options=menu_options,
        )

    async def async_step_add_charger(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Start adding a charging station."""
        self._temp_charger = {}
        return await self.async_step_charger_step1()

    async def async_step_edit_charger(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Select a charging station to edit."""
        configured_chargers: dict[str, Any] = self._config_entry.options.get(CONF_CHARGERS, {})

        if not configured_chargers:
            return await self.async_step_manage_chargers()

        if user_input is not None:
            charger_id = user_input[CONF_CHARGER_ID]
            c_data = configured_chargers.get(charger_id, {})
            self._temp_charger = dict(c_data)
            self._temp_charger[CONF_CHARGER_ID] = charger_id
            return await self.async_step_charger_step1()

        options = [
            selector.SelectOptionDict(
                value=cid,
                label=data.get(CONF_CHARGER_NAME, f"Charger {cid}"),
            )
            for cid, data in configured_chargers.items()
        ]

        schema = vol.Schema(
            {
                vol.Required(CONF_CHARGER_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="edit_charger",
            data_schema=schema,
        )

    async def async_step_remove_charger(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Select a charging station to remove."""
        configured_chargers: dict[str, Any] = dict(
            self._config_entry.options.get(CONF_CHARGERS, {})
        )

        if not configured_chargers:
            return await self.async_step_manage_chargers()

        if user_input is not None:
            charger_id = user_input[CONF_CHARGER_ID]
            if charger_id in configured_chargers:
                del configured_chargers[charger_id]

            new_options = dict(self._config_entry.options)
            new_options[CONF_CHARGERS] = configured_chargers
            return self.async_create_entry(title="", data=new_options)

        options = [
            selector.SelectOptionDict(
                value=cid,
                label=data.get(CONF_CHARGER_NAME, f"Charger {cid}"),
            )
            for cid, data in configured_chargers.items()
        ]

        schema = vol.Schema(
            {
                vol.Required(CONF_CHARGER_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="remove_charger",
            data_schema=schema,
        )

    async def async_step_charger_step1(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: Charger info, charging status sensor, energy meter, debounce, location."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if not user_input.get(CONF_CHARGER_NAME, "").strip():
                errors["base"] = "invalid_name"

            if not errors:
                charger_id = self._temp_charger.get(CONF_CHARGER_ID)
                if not charger_id:
                    base_id = _slugify(user_input[CONF_CHARGER_NAME])
                    charger_id = base_id
                    configured = self._config_entry.options.get(CONF_CHARGERS, {})
                    counter = 1
                    while charger_id in configured:
                        charger_id = f"{base_id}_{counter}"
                        counter += 1

                self._temp_charger.update(user_input)
                self._temp_charger[CONF_CHARGER_ID] = charger_id
                return await self.async_step_charger_step2()

        prefill_name = self._temp_charger.get(CONF_CHARGER_NAME, "")
        prefill_status = self._temp_charger.get(CONF_CHARGING_STATUS_ENTITY)
        prefill_energy = self._temp_charger.get(CONF_ENERGY_METER_ENTITY)
        prefill_energy_type = self._temp_charger.get(
            CONF_ENERGY_METER_TYPE, ENERGY_METER_TYPE_TOTAL_INCREASING
        )
        prefill_debounce = self._temp_charger.get(CONF_DEBOUNCE_SECONDS, DEFAULT_DEBOUNCE_SECONDS)
        prefill_location = self._temp_charger.get(CONF_CHARGING_LOCATION, DEFAULT_CHARGING_LOCATION)

        fields: dict[Any, Any] = {}
        if prefill_name:
            fields[vol.Required(CONF_CHARGER_NAME, default=prefill_name)] = selector.TextSelector()
        else:
            fields[vol.Required(CONF_CHARGER_NAME)] = selector.TextSelector()

        if prefill_status:
            fields[vol.Required(CONF_CHARGING_STATUS_ENTITY, default=prefill_status)] = (
                selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain=["binary_sensor", "sensor", "switch"],
                    )
                )
            )
        else:
            fields[vol.Required(CONF_CHARGING_STATUS_ENTITY)] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["binary_sensor", "sensor", "switch"],
                )
            )

        fields[
            vol.Optional(
                CONF_ENERGY_METER_ENTITY,
                description={"suggested_value": prefill_energy},
            )
        ] = selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain="sensor",
                device_class=SensorDeviceClass.ENERGY,
            )
        )

        fields[vol.Optional(CONF_ENERGY_METER_TYPE, default=prefill_energy_type)] = (
            selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(
                            value=ENERGY_METER_TYPE_TOTAL_INCREASING,
                            label="Total Increasing (Cumulative kWh)",
                        ),
                        selector.SelectOptionDict(
                            value=ENERGY_METER_TYPE_SESSION,
                            label="Session Energy (Resets per charge)",
                        ),
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    translation_key="energy_meter_type",
                )
            )
        )

        fields[vol.Optional(CONF_DEBOUNCE_SECONDS, default=prefill_debounce)] = (
            selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_DEBOUNCE_SECONDS,
                    max=MAX_DEBOUNCE_SECONDS,
                    step=5,
                    mode=selector.NumberSelectorMode.SLIDER,
                )
            )
        )

        fields[vol.Optional(CONF_CHARGING_LOCATION, default=prefill_location)] = (
            selector.TextSelector()
        )

        return self.async_show_form(
            step_id="charger_step1",
            data_schema=vol.Schema(fields),
            errors=errors,
        )

    async def async_step_charger_step2(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: Choose vehicle assignment mode and required parameters."""
        errors: dict[str, str] = {}
        configured_vehicles: dict[str, Any] = self._config_entry.options.get(CONF_VEHICLES, {})

        if user_input is not None:
            mode = user_input[CONF_ASSIGNMENT_MODE]
            linked_vehicle = user_input.get(CONF_LINKED_VEHICLE_ID)
            vehicle_select = user_input.get(CONF_VEHICLE_SELECT_ENTITY)

            if mode == ASSIGNMENT_MODE_FIXED and not linked_vehicle:
                errors["base"] = "select_vehicle"
            elif mode == ASSIGNMENT_MODE_INPUT_SELECT and not vehicle_select:
                errors["base"] = "select_entity"

            if not errors:
                charger_id = self._temp_charger[CONF_CHARGER_ID]
                new_options = dict(self._config_entry.options)
                configured_chargers = dict(new_options.get(CONF_CHARGERS, {}))

                configured_chargers[charger_id] = {
                    CONF_CHARGER_ID: charger_id,
                    CONF_CHARGER_NAME: self._temp_charger[CONF_CHARGER_NAME],
                    CONF_CHARGING_STATUS_ENTITY: self._temp_charger[CONF_CHARGING_STATUS_ENTITY],
                    CONF_ENERGY_METER_ENTITY: self._temp_charger.get(CONF_ENERGY_METER_ENTITY),
                    CONF_ENERGY_METER_TYPE: self._temp_charger.get(
                        CONF_ENERGY_METER_TYPE, ENERGY_METER_TYPE_TOTAL_INCREASING
                    ),
                    CONF_DEBOUNCE_SECONDS: self._temp_charger.get(
                        CONF_DEBOUNCE_SECONDS, DEFAULT_DEBOUNCE_SECONDS
                    ),
                    CONF_CHARGING_LOCATION: self._temp_charger.get(
                        CONF_CHARGING_LOCATION, DEFAULT_CHARGING_LOCATION
                    ),
                    CONF_ASSIGNMENT_MODE: mode,
                    CONF_LINKED_VEHICLE_ID: linked_vehicle
                    if mode == ASSIGNMENT_MODE_FIXED
                    else None,
                    CONF_VEHICLE_SELECT_ENTITY: (
                        vehicle_select if mode == ASSIGNMENT_MODE_INPUT_SELECT else None
                    ),
                }

                new_options[CONF_CHARGERS] = configured_chargers
                return self.async_create_entry(title="", data=new_options)

        prefill_mode = self._temp_charger.get(CONF_ASSIGNMENT_MODE, DEFAULT_ASSIGNMENT_MODE)
        prefill_linked_vehicle = self._temp_charger.get(CONF_LINKED_VEHICLE_ID)
        prefill_select_entity = self._temp_charger.get(CONF_VEHICLE_SELECT_ENTITY)

        mode_options = [
            selector.SelectOptionDict(
                value=ASSIGNMENT_MODE_FIXED,
                label="Fixed Vehicle (Linked)",
            ),
            selector.SelectOptionDict(
                value=ASSIGNMENT_MODE_INPUT_SELECT,
                label="Dynamic Selector (input_select)",
            ),
            selector.SelectOptionDict(
                value=ASSIGNMENT_MODE_CORRELATION,
                label="Automatic Correlation",
            ),
            selector.SelectOptionDict(
                value=ASSIGNMENT_MODE_UNASSIGNED,
                label="Unassigned / Multi-vehicle (Qualify later)",
            ),
        ]

        vehicle_options = [
            selector.SelectOptionDict(
                value=vid,
                label=vdata.get(CONF_VEHICLE_NAME, f"Vehicle {vid}"),
            )
            for vid, vdata in configured_vehicles.items()
        ]
        if not vehicle_options:
            vehicle_options.append(
                selector.SelectOptionDict(value="none", label="No vehicles configured")
            )

        fields: dict[Any, Any] = {
            vol.Required(
                CONF_ASSIGNMENT_MODE,
                default=prefill_mode,
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=mode_options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    translation_key="assignment_mode",
                )
            ),
            vol.Optional(
                CONF_LINKED_VEHICLE_ID,
                description={"suggested_value": prefill_linked_vehicle},
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=vehicle_options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_VEHICLE_SELECT_ENTITY,
                description={"suggested_value": prefill_select_entity},
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["input_select", "sensor"],
                )
            ),
        }

        return self.async_show_form(
            step_id="charger_step2",
            data_schema=vol.Schema(fields),
            errors=errors,
        )
