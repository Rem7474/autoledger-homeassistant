"""Config flow for AutoLedger integration."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import SubentryFlowResult
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_entry_oauth2_flow, selector
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError

from .api import (
    AutoLedgerApiClient,
    AutoLedgerAuthError,
    AutoLedgerConnectionError,
    AutoLedgerScopeError,
    AutoLedgerTimeoutError,
)
from .const import (
    ASSIGNMENT_MODE_FIXED,
    ASSIGNMENT_MODE_INPUT_SELECT,
    ASSIGNMENT_MODES,
    AUTH_METHOD_OAUTH,
    AUTH_METHOD_TOKEN,
    CONF_API_KEY,
    CONF_ASSIGNMENT_MODE,
    CONF_AUTH_METHOD,
    CONF_AUTHORIZE_URL,
    CONF_BATTERY_SOC_ENTITY,
    CONF_CHARGER_ID,
    CONF_CHARGER_NAME,
    CONF_CHARGING_LOCATION,
    CONF_CHARGING_STATUS_ENTITY,
    CONF_DEBOUNCE_SECONDS,
    CONF_DEVICE_ID,
    CONF_ENERGY_METER_ENTITY,
    CONF_ENERGY_METER_TYPE,
    CONF_HOST,
    CONF_LINKED_VEHICLE_ID,
    CONF_ODOMETER_ENTITY,
    CONF_TOKEN_URL,
    CONF_TRIP_LOCATION_ENTITY,
    CONF_TRIP_MOVING_ENTITY,
    CONF_VEHICLE_ID,
    CONF_VEHICLE_NAME,
    CONF_VEHICLE_SELECT_ENTITY,
    CONF_VERIFY_SSL,
    DEFAULT_ASSIGNMENT_MODE,
    DEFAULT_CHARGING_LOCATION,
    DEFAULT_DEBOUNCE_SECONDS,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    ENERGY_METER_TYPE_SESSION,
    ENERGY_METER_TYPE_TOTAL_INCREASING,
    ENERGY_METER_TYPES,
    MAX_DEBOUNCE_SECONDS,
    MIN_DEBOUNCE_SECONDS,
)
from .oauth import (
    AutoLedgerOAuth2Implementation,
    OAuthUnavailableError,
    async_discover,
    async_register_client,
    build_client,
    oauth_entry_data,
)
from .subentries import (
    SUBENTRY_CHARGER,
    SUBENTRY_VEHICLE,
    chargers_config,
    subentry_unique_id,
    vehicles_config,
)

_LOGGER = logging.getLogger(__name__)


def _slugify(text: str) -> str:
    """Generate a clean slug identifier."""
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", text).strip("_").lower()
    return slug or "charger"


def _inspect_entity(hass: Any, entity_id: str | None) -> dict[str, Any]:
    """Inspect entity state, unit, and return formatted preview."""
    if not entity_id:
        return {
            "exists": False,
            "state": None,
            "unit": None,
            "formatted": "—",
        }

    state_obj = hass.states.get(entity_id) if hass else None
    if state_obj is None:
        return {
            "exists": False,
            "state": None,
            "unit": None,
            "formatted": f"{entity_id} (not found)",
        }

    val = state_obj.state
    unit = state_obj.attributes.get("unit_of_measurement") or ""

    if val in ("unavailable", "unknown"):
        formatted = f"{val} ({unit})" if unit else val
    else:
        try:
            f_val = float(val)
            if f_val.is_integer():
                formatted_num = f"{int(f_val):,}".replace(",", " ")
            else:
                formatted_num = f"{f_val:,.1f}".replace(",", " ")
            formatted = f"{formatted_num} {unit}".strip()
        except ValueError:
            formatted = f"{val} {unit}".strip()

    return {
        "exists": True,
        "state": val,
        "unit": unit,
        "formatted": formatted,
    }


def _validate_battery_sensor(hass: Any, entity_id: str | None) -> str | None:
    """Validate battery sensor unit and state. Returns error key or None."""
    if not entity_id:
        return None
    state_obj = hass.states.get(entity_id) if hass else None
    if not state_obj:
        return None

    unit = state_obj.attributes.get("unit_of_measurement")
    if unit and unit != "%":
        return "invalid_battery_unit"

    val = state_obj.state
    if val not in ("unavailable", "unknown"):
        try:
            num = float(val)
            if num < 0 or num > 100:
                return "invalid_battery_range"
        except ValueError:
            return "invalid_numeric_state"
    return None


def _validate_odometer_sensor(hass: Any, entity_id: str | None) -> str | None:
    """Validate odometer sensor unit and state. Returns error key or None."""
    if not entity_id:
        return None
    state_obj = hass.states.get(entity_id) if hass else None
    if not state_obj:
        return None

    unit = state_obj.attributes.get("unit_of_measurement")
    valid_distance_units = {"km", "mi", "m"}
    if unit and unit.lower() not in valid_distance_units:
        return "invalid_odometer_unit"

    val = state_obj.state
    if val not in ("unavailable", "unknown"):
        try:
            num = float(val)
            if num < 0:
                return "invalid_odometer_range"
        except ValueError:
            return "invalid_numeric_state"
    return None


def _validate_energy_sensor(hass: Any, entity_id: str | None) -> str | None:
    """Validate energy meter sensor unit and state. Returns error key or None."""
    if not entity_id:
        return None
    state_obj = hass.states.get(entity_id) if hass else None
    if not state_obj:
        return None

    unit = state_obj.attributes.get("unit_of_measurement")
    valid_energy_units = {"kwh", "wh", "mwh"}
    if unit and unit.lower() not in valid_energy_units:
        return "invalid_energy_unit"

    val = state_obj.state
    if val not in ("unavailable", "unknown"):
        try:
            num = float(val)
            if num < 0:
                return "invalid_energy_range"
        except ValueError:
            return "invalid_numeric_state"
    return None


async def _async_validate_credentials(
    hass: Any, host: str, api_key: str, verify_ssl: bool
) -> tuple[str | None, dict[str, Any]]:
    """Check a host/token pair. Returns (error key or None, account description)."""
    client = AutoLedgerApiClient(
        host=host,
        api_key=api_key,
        session=async_get_clientsession(hass),
        verify_ssl=verify_ssl,
    )
    try:
        account = await client.async_validate()
    except AutoLedgerAuthError:
        return "invalid_auth", {}
    except AutoLedgerScopeError:
        return "invalid_scope", {}
    except (AutoLedgerConnectionError, AutoLedgerTimeoutError):
        return "cannot_connect", {}
    except Exception as err:
        _LOGGER.exception("Unexpected exception while checking the AutoLedger credentials: %s", err)
        return "unknown", {}
    return None, account


def _connection_schema(
    host: str | None = None, api_key: str | None = None, verify_ssl: bool = DEFAULT_VERIFY_SSL
) -> vol.Schema:
    """Form schema of the connection step, prefilled with the current values."""
    host_key = vol.Required(CONF_HOST, default=host) if host else vol.Required(CONF_HOST)
    key_key = vol.Required(CONF_API_KEY, default=api_key) if api_key else vol.Required(CONF_API_KEY)
    return vol.Schema(
        {
            host_key: selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
            ),
            key_key: selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Optional(CONF_VERIFY_SSL, default=verify_ssl): selector.BooleanSelector(),
        }
    )


class AutoLedgerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for AutoLedger."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize the flow."""
        self._oauth: dict[str, Any] = {}
        self._external_data: dict[str, Any] = {}

    # ------------------------------------------------------------------ first connection

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Choose how to connect: authorize in the browser, or paste an API token."""
        return self.async_show_menu(step_id="user", menu_options=["oauth", "token"])

    async def async_step_token(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Connect with an API token."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].rstrip("/")
            api_key = user_input[CONF_API_KEY]
            verify_ssl = user_input.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)

            await self.async_set_unique_id(host.lower())
            self._abort_if_unique_id_configured()

            error, _account = await _async_validate_credentials(
                self.hass, host, api_key, verify_ssl
            )
            if error is None:
                return self.async_create_entry(
                    title=f"AutoLedger ({host})",
                    data={
                        CONF_HOST: host,
                        CONF_API_KEY: api_key,
                        CONF_VERIFY_SSL: verify_ssl,
                        CONF_AUTH_METHOD: AUTH_METHOD_TOKEN,
                    },
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="token",
            data_schema=_connection_schema(),
            errors=errors,
        )

    # ------------------------------------------------------------------ OAuth connection

    async def async_step_oauth(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Ask for the server, then send the user to AutoLedger to authorize Home Assistant."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].rstrip("/")
            verify_ssl = user_input.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)

            if self.source == config_entries.SOURCE_USER:
                await self.async_set_unique_id(host.lower())
                self._abort_if_unique_id_configured()
            elif self._another_entry_uses(host, self._current_entry()):
                return self.async_abort(reason="already_configured")

            error = await self._async_prepare_oauth(host, verify_ssl)
            if error is None:
                return await self.async_step_auth()
            errors["base"] = error

        entry = self._current_entry()
        return self.async_show_form(
            step_id="oauth",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOST,
                        **({"default": entry.data[CONF_HOST]} if entry else {}),
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
                    ),
                    vol.Optional(
                        CONF_VERIFY_SSL,
                        default=entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
                        if entry
                        else DEFAULT_VERIFY_SSL,
                    ): selector.BooleanSelector(),
                }
            ),
            errors=errors,
        )

    def _current_entry(self) -> config_entries.ConfigEntry | None:
        """The entry being reconfigured or re-authenticated, if any."""
        if self.source == config_entries.SOURCE_RECONFIGURE:
            return self._get_reconfigure_entry()
        if self.source == config_entries.SOURCE_REAUTH:
            return self._get_reauth_entry()
        return None

    def _another_entry_uses(self, host: str, current: config_entries.ConfigEntry | None) -> bool:
        """Whether an entry other than the one being edited already uses this server."""
        return any(
            (current is None or other.entry_id != current.entry_id)
            and other.unique_id == host.lower()
            for other in self._async_current_entries()
        )

    async def _async_prepare_oauth(self, host: str, verify_ssl: bool) -> str | None:
        """Discover the server and register Home Assistant. Returns an error key, or None."""
        try:
            endpoints = await async_discover(self.hass, host, verify_ssl)
            redirect_uri = config_entry_oauth2_flow.async_get_redirect_uri(self.hass)
            client_id = await async_register_client(
                self.hass, endpoints["registration_endpoint"], redirect_uri, verify_ssl
            )
        except AutoLedgerConnectionError:
            return "cannot_connect"
        except OAuthUnavailableError as err:
            _LOGGER.warning("OAuth is not available on %s: %s", host, err)
            return "oauth_unavailable"
        except (NoURLAvailableError, RuntimeError):
            return "no_url_available"

        self._oauth = {
            "host": host,
            "verify_ssl": verify_ssl,
            "endpoints": endpoints,
            "client_id": client_id,
            "implementation": AutoLedgerOAuth2Implementation(
                self.hass,
                client_id,
                endpoints[CONF_AUTHORIZE_URL],
                endpoints[CONF_TOKEN_URL],
                verify_ssl,
            ),
        }
        return None

    async def async_step_auth(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Send the user to AutoLedger, then take the answer of the redirect."""
        if user_input is not None:
            self._external_data = user_input
            next_step = "authorize_rejected" if "error" in user_input else "creation"
            return self.async_external_step_done(next_step_id=next_step)

        url = await self._oauth["implementation"].async_generate_authorize_url(self.flow_id)
        return self.async_external_step(step_id="auth", url=url)

    async def async_step_authorize_rejected(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """The user refused, or AutoLedger refused the request."""
        return self.async_abort(
            reason="user_rejected_authorize",
            description_placeholders={"error": str(self._external_data.get("error", ""))},
        )

    async def async_step_creation(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Trade the authorization code for tokens, check them, and store the connection."""
        oauth = self._oauth
        try:
            token = await oauth["implementation"].async_resolve_external_data(self._external_data)
        except AutoLedgerConnectionError:
            return self.async_abort(reason="cannot_connect")
        except AutoLedgerAuthError:
            return self.async_abort(reason="oauth_failed")

        try:
            token["expires_in"] = int(token["expires_in"])
        except (KeyError, TypeError, ValueError):
            return self.async_abort(reason="oauth_failed")
        token["expires_at"] = time.time() + token["expires_in"]

        error, _account = await _async_validate_credentials(
            self.hass, oauth["host"], token["access_token"], oauth["verify_ssl"]
        )
        if error == "invalid_scope":
            # The consent page grants the scope only when its box is ticked.
            return self.async_abort(reason="oauth_scope_not_granted")
        if error is not None:
            return self.async_abort(reason=error)

        data = {
            **oauth_entry_data(
                oauth["host"], oauth["verify_ssl"], oauth["endpoints"], oauth["client_id"], token
            ),
            CONF_AUTH_METHOD: AUTH_METHOD_OAUTH,
        }
        entry = self._current_entry()
        if entry is None:
            return self.async_create_entry(title=f"AutoLedger ({oauth['host']})", data=data)

        # The static token, if the entry had one, is replaced by the OAuth connection.
        return self.async_update_reload_and_abort(
            entry,
            unique_id=oauth["host"].lower(),
            data={k: v for k, v in entry.data.items() if k != CONF_API_KEY} | data,
        )

    # ------------------------------------------------------------------ reconfigure

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Change the connection: its server and token, or switch to OAuth."""
        entry = self._get_reconfigure_entry()
        if entry.data.get(CONF_AUTH_METHOD) == AUTH_METHOD_OAUTH:
            # Nothing to type for an OAuth connection but the server: authorize again.
            return await self.async_step_oauth()
        return self.async_show_menu(
            step_id="reconfigure", menu_options=["reconfigure_token", "oauth"]
        )

    async def async_step_reconfigure_token(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle reconfiguration of the integration connection."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            host = user_input[CONF_HOST].rstrip("/")
            api_key = user_input[CONF_API_KEY]
            verify_ssl = user_input.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
            unique_id = host.lower()

            if self._another_entry_uses(host, entry):
                return self.async_abort(reason="already_configured")

            error, _account = await _async_validate_credentials(
                self.hass, host, api_key, verify_ssl
            )
            if error is None:
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=unique_id,
                    data_updates={
                        CONF_HOST: host,
                        CONF_API_KEY: api_key,
                        CONF_VERIFY_SSL: verify_ssl,
                    },
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="reconfigure_token",
            data_schema=_connection_schema(
                entry.data.get(CONF_HOST),
                entry.data.get(CONF_API_KEY),
                entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------ re-authentication

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> FlowResult:
        """Start re-authentication when the server stops accepting the credentials."""
        if entry_data.get(CONF_AUTH_METHOD) == AUTH_METHOD_OAUTH:
            return await self.async_step_reauth_oauth()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_oauth(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Authorize Home Assistant again after AutoLedger ended the grant."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        host = entry.data[CONF_HOST]

        if user_input is not None:
            error = await self._async_prepare_oauth(
                host, entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
            )
            if error is None:
                return await self.async_step_auth()
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_oauth",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"host": host},
        )

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask for a new token for the server of the entry."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        host = entry.data[CONF_HOST]

        if user_input is not None:
            api_key = user_input[CONF_API_KEY]
            error, _account = await _async_validate_credentials(
                self.hass, host, api_key, entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
            )
            if error is None:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_KEY: api_key}
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"host": host},
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: config_entries.ConfigEntry
    ) -> dict[str, type[config_entries.ConfigSubentryFlow]]:
        """Vehicles and charging stations are subentries of the connection."""
        return {SUBENTRY_VEHICLE: VehicleSubentryFlow, SUBENTRY_CHARGER: ChargerSubentryFlow}


def _vehicle_label(vehicle: Mapping[str, Any]) -> str:
    """Name an AutoLedger vehicle the way the server lists it."""
    parts = [vehicle.get("make"), vehicle.get("model"), vehicle.get("name")]
    return " ".join(str(p) for p in parts if p) or f"Vehicle {vehicle.get('id')}"


def _entities_of_device(hass: Any, device_id: str | None) -> dict[str, str]:
    """Guess which entities of a device carry the battery, odometer and charge status."""
    found: dict[str, str] = {}
    if not device_id:
        return found
    for reg_entry in er.async_entries_for_device(er.async_get(hass), device_id):
        dev_class = reg_entry.device_class or reg_entry.original_device_class
        ent_id = reg_entry.entity_id
        if CONF_BATTERY_SOC_ENTITY not in found and (
            dev_class == SensorDeviceClass.BATTERY or "battery" in ent_id or "soc" in ent_id
        ):
            found[CONF_BATTERY_SOC_ENTITY] = ent_id
        if CONF_ODOMETER_ENTITY not in found and (
            dev_class == SensorDeviceClass.DISTANCE
            or "odometer" in ent_id
            or "mileage" in ent_id
            or "distance" in ent_id
        ):
            found[CONF_ODOMETER_ENTITY] = ent_id
        if CONF_CHARGING_STATUS_ENTITY not in found and (
            dev_class in (BinarySensorDeviceClass.BATTERY_CHARGING, BinarySensorDeviceClass.PLUG)
            or "charging" in ent_id
            or "plug" in ent_id
        ):
            found[CONF_CHARGING_STATUS_ENTITY] = ent_id
    return found


def _preview(hass: Any, *entity_ids: str | None) -> str:
    """One line per entity with its current value, for the form description."""
    lines = []
    for entity_id in entity_ids:
        if entity_id:
            lines.append(f"• {entity_id} : {_inspect_entity(hass, entity_id)['formatted']}")
    return "\n".join(lines)


def _energy_meter_type(hass: Any, entity_id: str | None) -> str:
    """Tell a lifetime counter from a per-session one with the state class of the sensor."""
    state = hass.states.get(entity_id) if entity_id else None
    if state is not None and state.attributes.get("state_class") == "measurement":
        return ENERGY_METER_TYPE_SESSION
    return ENERGY_METER_TYPE_TOTAL_INCREASING


class VehicleSubentryFlow(config_entries.ConfigSubentryFlow):
    """Add or reconfigure a vehicle: which AutoLedger vehicle, and its Home Assistant entities."""

    def __init__(self) -> None:
        """Initialize the flow."""
        self._draft: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Pick the AutoLedger vehicle to add and, optionally, its Home Assistant device."""
        entry = self._get_entry()
        try:
            vehicles = await build_client(self.hass, entry).async_get_vehicles()
        except AutoLedgerAuthError:
            return self.async_abort(reason="invalid_auth")
        except Exception as err:  # noqa: BLE001 - any failure means the server is unusable now
            _LOGGER.warning("Could not fetch vehicles from AutoLedger: %s", err)
            return self.async_abort(reason="cannot_connect")

        configured = set(vehicles_config(entry))
        available = [
            v for v in vehicles if str(v.get("id", "")) and str(v.get("id")) not in configured
        ]
        if not available:
            return self.async_abort(reason="no_vehicle_available")

        if user_input is not None:
            vehicle_id = str(user_input[CONF_VEHICLE_ID])
            match = next((v for v in available if str(v.get("id")) == vehicle_id), None)
            self._draft = {
                CONF_VEHICLE_ID: vehicle_id,
                CONF_VEHICLE_NAME: _vehicle_label(match) if match else vehicle_id,
                CONF_DEVICE_ID: user_input.get(CONF_DEVICE_ID),
            }
            return await self.async_step_mapping()

        options = [
            selector.SelectOptionDict(value=str(v["id"]), label=_vehicle_label(v))
            for v in available
        ]
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_VEHICLE_ID): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options, mode=selector.SelectSelectorMode.DROPDOWN
                        )
                    ),
                    vol.Optional(CONF_DEVICE_ID): selector.DeviceSelector(),
                }
            ),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit the entities of an existing vehicle."""
        self._draft = dict(self._get_reconfigure_subentry().data)
        return await self.async_step_mapping()

    async def async_step_mapping(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Map the Home Assistant entities that describe the vehicle."""
        errors: dict[str, str] = {}

        if user_input is not None:
            battery_err = _validate_battery_sensor(
                self.hass, user_input.get(CONF_BATTERY_SOC_ENTITY)
            )
            if battery_err:
                errors[CONF_BATTERY_SOC_ENTITY] = battery_err
            odometer_err = _validate_odometer_sensor(
                self.hass, user_input.get(CONF_ODOMETER_ENTITY)
            )
            if odometer_err:
                errors[CONF_ODOMETER_ENTITY] = odometer_err

            if not errors:
                data = {
                    **self._draft,
                    **{key: user_input.get(key) for key in _VEHICLE_ENTITY_KEYS},
                }
                title = str(data.get(CONF_VEHICLE_NAME) or data[CONF_VEHICLE_ID])
                if self.source == config_entries.SOURCE_RECONFIGURE:
                    return self.async_update_and_abort(
                        self._get_entry(),
                        self._get_reconfigure_subentry(),
                        title=title,
                        data=data,
                    )
                return self.async_create_entry(
                    title=title,
                    data=data,
                    unique_id=subentry_unique_id(SUBENTRY_VEHICLE, str(data[CONF_VEHICLE_ID])),
                )

        guessed = _entities_of_device(self.hass, self._draft.get(CONF_DEVICE_ID))
        current = {key: self._draft.get(key) or guessed.get(key) for key in _VEHICLE_ENTITY_KEYS}

        def suggested(key: str) -> dict[str, Any]:
            return {"suggested_value": current[key]}

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_BATTERY_SOC_ENTITY, description=suggested(CONF_BATTERY_SOC_ENTITY)
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor", device_class=SensorDeviceClass.BATTERY
                    )
                ),
                vol.Optional(
                    CONF_ODOMETER_ENTITY, description=suggested(CONF_ODOMETER_ENTITY)
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor", device_class=SensorDeviceClass.DISTANCE
                    )
                ),
                vol.Optional(
                    CONF_CHARGING_STATUS_ENTITY, description=suggested(CONF_CHARGING_STATUS_ENTITY)
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["binary_sensor", "sensor"])
                ),
                vol.Optional(
                    CONF_TRIP_LOCATION_ENTITY, description=suggested(CONF_TRIP_LOCATION_ENTITY)
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["device_tracker", "person"])
                ),
                vol.Optional(
                    CONF_TRIP_MOVING_ENTITY, description=suggested(CONF_TRIP_MOVING_ENTITY)
                ): selector.EntitySelector(selector.EntitySelectorConfig(domain="binary_sensor")),
            }
        )
        return self.async_show_form(
            step_id="mapping",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "vehicle_name": str(self._draft.get(CONF_VEHICLE_NAME) or ""),
                "preview_section": _preview(
                    self.hass,
                    current[CONF_BATTERY_SOC_ENTITY],
                    current[CONF_ODOMETER_ENTITY],
                    current[CONF_CHARGING_STATUS_ENTITY],
                ),
            },
        )


_VEHICLE_ENTITY_KEYS = (
    CONF_BATTERY_SOC_ENTITY,
    CONF_ODOMETER_ENTITY,
    CONF_CHARGING_STATUS_ENTITY,
    CONF_TRIP_LOCATION_ENTITY,
    CONF_TRIP_MOVING_ENTITY,
)


class ChargerSubentryFlow(config_entries.ConfigSubentryFlow):
    """Add or reconfigure a charging station and say which vehicle its charges belong to."""

    def __init__(self) -> None:
        """Initialize the flow."""
        self._draft: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Add a charging station."""
        self._draft = {}
        return await self.async_step_station()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit an existing charging station."""
        self._draft = dict(self._get_reconfigure_subentry().data)
        return await self.async_step_station()

    async def async_step_station(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Name the station and point at its status sensor and energy meter."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if not str(user_input.get(CONF_CHARGER_NAME, "")).strip():
                errors["base"] = "invalid_name"
            energy_entity = user_input.get(CONF_ENERGY_METER_ENTITY)
            energy_err = _validate_energy_sensor(self.hass, energy_entity)
            if energy_err:
                errors[CONF_ENERGY_METER_ENTITY] = energy_err

            if not errors:
                charger_id = self._draft.get(CONF_CHARGER_ID) or self._new_charger_id(
                    user_input[CONF_CHARGER_NAME]
                )
                self._draft = {
                    **self._draft,
                    **user_input,
                    CONF_CHARGER_ID: charger_id,
                    CONF_ENERGY_METER_TYPE: user_input.get(CONF_ENERGY_METER_TYPE)
                    or _energy_meter_type(self.hass, energy_entity),
                }
                return await self.async_step_assignment()

        draft = self._draft
        status = draft.get(CONF_CHARGING_STATUS_ENTITY)
        status_key = (
            vol.Required(CONF_CHARGING_STATUS_ENTITY, default=status)
            if status
            else vol.Required(CONF_CHARGING_STATUS_ENTITY)
        )
        name = draft.get(CONF_CHARGER_NAME)
        name_key = (
            vol.Required(CONF_CHARGER_NAME, default=name)
            if name
            else vol.Required(CONF_CHARGER_NAME)
        )
        schema = vol.Schema(
            {
                name_key: selector.TextSelector(),
                status_key: selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["binary_sensor", "sensor", "switch"])
                ),
                vol.Optional(
                    CONF_ENERGY_METER_ENTITY,
                    description={"suggested_value": draft.get(CONF_ENERGY_METER_ENTITY)},
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor", device_class=SensorDeviceClass.ENERGY
                    )
                ),
                vol.Optional(
                    CONF_ENERGY_METER_TYPE,
                    description={"suggested_value": draft.get(CONF_ENERGY_METER_TYPE)},
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(ENERGY_METER_TYPES),
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        translation_key="energy_meter_type",
                    )
                ),
                vol.Optional(
                    CONF_DEBOUNCE_SECONDS,
                    default=draft.get(CONF_DEBOUNCE_SECONDS, DEFAULT_DEBOUNCE_SECONDS),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_DEBOUNCE_SECONDS,
                        max=MAX_DEBOUNCE_SECONDS,
                        step=5,
                        mode=selector.NumberSelectorMode.SLIDER,
                    )
                ),
                vol.Optional(
                    CONF_CHARGING_LOCATION,
                    default=draft.get(CONF_CHARGING_LOCATION, DEFAULT_CHARGING_LOCATION),
                ): selector.TextSelector(),
            }
        )
        return self.async_show_form(
            step_id="station",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "preview_section": _preview(
                    self.hass,
                    draft.get(CONF_CHARGING_STATUS_ENTITY),
                    draft.get(CONF_ENERGY_METER_ENTITY),
                )
            },
        )

    def _new_charger_id(self, name: str) -> str:
        """Derive a charger id from its name that no other station of the entry uses."""
        taken = set(chargers_config(self._get_entry()))
        base = _slugify(name)
        charger_id, counter = base, 1
        while charger_id in taken:
            charger_id = f"{base}_{counter}"
            counter += 1
        return charger_id

    async def async_step_assignment(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Choose how the charges of this station are attributed to vehicles."""
        if user_input is not None:
            mode = user_input[CONF_ASSIGNMENT_MODE]
            self._draft[CONF_ASSIGNMENT_MODE] = mode
            self._draft[CONF_LINKED_VEHICLE_ID] = None
            self._draft[CONF_VEHICLE_SELECT_ENTITY] = None
            if mode == ASSIGNMENT_MODE_FIXED:
                return await self.async_step_assignment_vehicle()
            if mode == ASSIGNMENT_MODE_INPUT_SELECT:
                return await self.async_step_assignment_entity()
            return self._finish()

        return self.async_show_form(
            step_id="assignment",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ASSIGNMENT_MODE,
                        default=self._draft.get(CONF_ASSIGNMENT_MODE, DEFAULT_ASSIGNMENT_MODE),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=list(ASSIGNMENT_MODES),
                            mode=selector.SelectSelectorMode.DROPDOWN,
                            translation_key="assignment_mode",
                        )
                    )
                }
            ),
        )

    async def async_step_assignment_vehicle(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Fixed assignment: the vehicle every charge of the station belongs to."""
        vehicles = vehicles_config(self._get_entry())
        if not vehicles:
            return self.async_abort(reason="no_vehicle_configured")

        if user_input is not None:
            self._draft[CONF_LINKED_VEHICLE_ID] = user_input[CONF_LINKED_VEHICLE_ID]
            return self._finish()

        options = [
            selector.SelectOptionDict(
                value=vid, label=str(conf.get(CONF_VEHICLE_NAME) or f"Vehicle {vid}")
            )
            for vid, conf in vehicles.items()
        ]
        linked = self._draft.get(CONF_LINKED_VEHICLE_ID)
        key = (
            vol.Required(CONF_LINKED_VEHICLE_ID, default=linked)
            if linked in vehicles
            else vol.Required(CONF_LINKED_VEHICLE_ID)
        )
        return self.async_show_form(
            step_id="assignment_vehicle",
            data_schema=vol.Schema(
                {
                    key: selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options, mode=selector.SelectSelectorMode.DROPDOWN
                        )
                    )
                }
            ),
        )

    async def async_step_assignment_entity(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Dynamic assignment: the entity that holds the name of the vehicle being charged."""
        if user_input is not None:
            self._draft[CONF_VEHICLE_SELECT_ENTITY] = user_input[CONF_VEHICLE_SELECT_ENTITY]
            return self._finish()

        selected = self._draft.get(CONF_VEHICLE_SELECT_ENTITY)
        key = (
            vol.Required(CONF_VEHICLE_SELECT_ENTITY, default=selected)
            if selected
            else vol.Required(CONF_VEHICLE_SELECT_ENTITY)
        )
        return self.async_show_form(
            step_id="assignment_entity",
            data_schema=vol.Schema(
                {
                    key: selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=["input_select", "sensor"])
                    )
                }
            ),
        )

    def _finish(self) -> SubentryFlowResult:
        """Store the station."""
        data = dict(self._draft)
        title = str(data[CONF_CHARGER_NAME]).strip()
        if self.source == config_entries.SOURCE_RECONFIGURE:
            return self.async_update_and_abort(
                self._get_entry(), self._get_reconfigure_subentry(), title=title, data=data
            )
        return self.async_create_entry(
            title=title,
            data=data,
            unique_id=subentry_unique_id(SUBENTRY_CHARGER, data[CONF_CHARGER_ID]),
        )
