"""Pytest configuration and Home Assistant mocks for AutoLedger tests."""

from __future__ import annotations

import asyncio
import inspect
import sys
from datetime import UTC, datetime
from enum import StrEnum
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import pytest

# Mock voluptuous if not installed
if "voluptuous" not in sys.modules:
    try:
        import voluptuous  # noqa: F401
    except ImportError:
        vol = ModuleType("voluptuous")

        class Schema:
            def __init__(self, schema):
                self.schema = schema

            def __call__(self, val):
                return val

        class Marker:
            def __init__(self, schema, msg=None, default=None, description=None):
                self.schema = schema
                self.default = default
                self.description = description

            def __hash__(self):
                return hash(self.schema)

            def __eq__(self, other):
                return self.schema == getattr(other, "schema", other)

        class Required(Marker):
            pass

        class Optional(Marker):
            pass

        def Coerce(t):
            return t

        def Any(*validators, **kwargs):
            return lambda v: v

        class Invalid(Exception):
            pass

        class MultipleInvalid(Invalid):
            pass

        vol.Schema = Schema
        vol.Required = Required
        vol.Optional = Optional
        vol.Coerce = Coerce
        vol.Any = Any
        vol.Invalid = Invalid
        vol.MultipleInvalid = MultipleInvalid
        sys.modules["voluptuous"] = vol

# Check if homeassistant is already installed; if not, register lightweight mocks
if "homeassistant" not in sys.modules:
    try:
        import homeassistant  # noqa: F401
    except ImportError:
        # Create mock module hierarchy
        ha = ModuleType("homeassistant")
        ha.const = ModuleType("homeassistant.const")
        ha.exceptions = ModuleType("homeassistant.exceptions")
        ha.core = ModuleType("homeassistant.core")
        ha.util = ModuleType("homeassistant.util")
        ha.util.dt = ModuleType("homeassistant.util.dt")
        ha.helpers = ModuleType("homeassistant.helpers")
        ha.helpers.event = ModuleType("homeassistant.helpers.event")
        ha.helpers.update_coordinator = ModuleType("homeassistant.helpers.update_coordinator")
        ha.helpers.device_registry = ModuleType("homeassistant.helpers.device_registry")
        ha.helpers.entity_platform = ModuleType("homeassistant.helpers.entity_platform")
        ha.helpers.entity_registry = ModuleType("homeassistant.helpers.entity_registry")
        ha.helpers.selector = ModuleType("homeassistant.helpers.selector")
        ha.helpers.config_validation = ModuleType("homeassistant.helpers.config_validation")
        ha.helpers.aiohttp_client = ModuleType("homeassistant.helpers.aiohttp_client")
        ha.config_entries = ModuleType("homeassistant.config_entries")
        ha.data_entry_flow = ModuleType("homeassistant.data_entry_flow")
        ha.components = ModuleType("homeassistant.components")
        ha.components.sensor = ModuleType("homeassistant.components.sensor")
        ha.components.binary_sensor = ModuleType("homeassistant.components.binary_sensor")
        ha.components.diagnostics = ModuleType("homeassistant.components.diagnostics")

        class Platform(StrEnum):
            SENSOR = "sensor"

        ha.const.Platform = Platform

        class HomeAssistantError(Exception):
            pass

        ha.exceptions.HomeAssistantError = HomeAssistantError

        def callback(func):
            return func

        ha.core.callback = callback
        ha.core.CALLBACK_TYPE = Any
        ha.core.HomeAssistant = MagicMock
        ha.core.Event = MagicMock
        ha.core.EventStateChangedData = MagicMock
        ha.core.ServiceCall = MagicMock

        def utcnow():
            return datetime.now(UTC)

        def slugify(text):
            import re

            return re.sub(r"[^a-zA-Z0-9_]+", "_", text).strip("_").lower()

        ha.util.dt.utcnow = utcnow
        ha.util.slugify = slugify

        def async_call_later(hass, delay, action):
            return MagicMock()

        def async_track_state_change_event(hass, entity_ids, action):
            return MagicMock()

        ha.helpers.event.async_call_later = async_call_later
        ha.helpers.event.async_track_state_change_event = async_track_state_change_event

        class UpdateFailed(HomeAssistantError):
            pass

        class DataUpdateCoordinator:
            def __init_subclass__(cls, **kwargs):
                super().__init_subclass__(**kwargs)

            def __class_getitem__(cls, item):
                return cls

            def __init__(self, hass, logger, name, update_interval=None):
                self.hass = hass
                self.logger = logger
                self.name = name
                self.update_interval = update_interval
                self.data = {}

            async def async_request_refresh(self):
                self.data = await self._async_update_data()

            async def async_config_entry_first_refresh(self):
                self.data = await self._async_update_data()

            async def _async_update_data(self):
                return {}

        class CoordinatorEntity:
            def __class_getitem__(cls, item):
                return cls

            def __init__(self, coordinator):
                self.coordinator = coordinator

        ha.helpers.update_coordinator.UpdateFailed = UpdateFailed
        ha.helpers.update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
        ha.helpers.update_coordinator.CoordinatorEntity = CoordinatorEntity

        class SensorDeviceClass(StrEnum):
            MONETARY = "monetary"
            BATTERY = "battery"
            DISTANCE = "distance"
            ENERGY = "energy"

        class BinarySensorDeviceClass(StrEnum):
            BATTERY_CHARGING = "battery_charging"
            PLUG = "plug"

        class SensorEntity:
            _attr_name = None
            _attr_unique_id = None
            _attr_device_info = None
            _attr_device_class = None
            _attr_icon = None

            def async_write_ha_state(self):
                pass

        ha.components.sensor.SensorDeviceClass = SensorDeviceClass
        ha.components.sensor.SensorEntity = SensorEntity
        ha.components.sensor.SensorEntityDescription = MagicMock
        ha.components.binary_sensor.BinarySensorDeviceClass = BinarySensorDeviceClass

        def DeviceInfo(**kwargs):
            return kwargs

        ha.helpers.device_registry.DeviceInfo = DeviceInfo
        ha.helpers.entity_platform.AddEntitiesCallback = Any

        class ConfigFlow:
            VERSION = 1

            def __init_subclass__(cls, domain=None, **kwargs):
                super().__init_subclass__(**kwargs)
                cls.domain = domain

            async def async_set_unique_id(self, unique_id):
                self._unique_id = unique_id

            def _abort_if_unique_id_configured(self):
                pass

            def _get_reconfigure_entry(self):
                return getattr(self, "_reconfigure_entry", None)

            def async_update_reload_and_abort(self, entry, data_updates=None):
                if entry and data_updates:
                    entry.data.update(data_updates)
                return {"type": "abort", "reason": "reconfigure_successful"}

            def async_show_form(
                self, step_id, data_schema=None, errors=None, description_placeholders=None
            ):
                return {
                    "type": "form",
                    "step_id": step_id,
                    "data_schema": data_schema,
                    "errors": errors or {},
                    "description_placeholders": description_placeholders or {},
                }

            def async_create_entry(self, title, data):
                return {"type": "create_entry", "title": title, "data": data}

            def async_abort(self, reason):
                return {"type": "abort", "reason": reason}

        class OptionsFlow:
            def async_show_menu(self, step_id, menu_options, description_placeholders=None):
                return {
                    "type": "menu",
                    "step_id": step_id,
                    "menu_options": menu_options,
                    "description_placeholders": description_placeholders or {},
                }

            def async_show_form(
                self, step_id, data_schema=None, errors=None, description_placeholders=None
            ):
                return {
                    "type": "form",
                    "step_id": step_id,
                    "data_schema": data_schema,
                    "errors": errors or {},
                    "description_placeholders": description_placeholders or {},
                }

            def async_create_entry(self, title, data):
                return {"type": "create_entry", "title": title, "data": data}

        class ConfigEntry:
            def __init__(
                self,
                entry_id="test_entry_id",
                domain="autoledger",
                title="AutoLedger",
                data=None,
                options=None,
            ):
                self.entry_id = entry_id
                self.domain = domain
                self.title = title
                self.version = 1
                self.data = data or {}
                self.options = options or {}
                self.runtime_data = None
                self._update_listeners = []

            def add_update_listener(self, listener):
                self._update_listeners.append(listener)
                return lambda: self._update_listeners.remove(listener)

            def async_on_unload(self, callback):
                pass

        ha.config_entries.ConfigFlow = ConfigFlow
        ha.config_entries.OptionsFlow = OptionsFlow
        ha.config_entries.ConfigEntry = ConfigEntry
        ha.data_entry_flow.FlowResult = dict[str, Any]

        # Selectors
        class TextSelectorType:
            URL = "url"
            PASSWORD = "password"
            TEXT = "text"

        class NumberSelectorMode:
            SLIDER = "slider"
            BOX = "box"

        class SelectSelectorMode:
            DROPDOWN = "dropdown"
            LIST = "list"

        def SelectOptionDict(value, label):
            return {"value": value, "label": label}

        def TextSelectorConfig(type=TextSelectorType.TEXT):
            return {"type": type}

        def TextSelector(config=None):
            return MagicMock()

        def BooleanSelector():
            return MagicMock()

        def DeviceSelector():
            return MagicMock()

        def EntitySelectorConfig(**kwargs):
            return kwargs

        def EntitySelector(config=None):
            return MagicMock()

        def SelectSelectorConfig(**kwargs):
            return kwargs

        def SelectSelector(config=None):
            return MagicMock()

        def NumberSelectorConfig(**kwargs):
            return kwargs

        def NumberSelector(config=None):
            return MagicMock()

        ha.helpers.selector.TextSelectorType = TextSelectorType
        ha.helpers.selector.TextSelectorConfig = TextSelectorConfig
        ha.helpers.selector.TextSelector = TextSelector
        ha.helpers.selector.BooleanSelector = BooleanSelector
        ha.helpers.selector.DeviceSelector = DeviceSelector
        ha.helpers.selector.EntitySelectorConfig = EntitySelectorConfig
        ha.helpers.selector.EntitySelector = EntitySelector
        ha.helpers.selector.SelectOptionDict = SelectOptionDict
        ha.helpers.selector.SelectSelectorConfig = SelectSelectorConfig
        ha.helpers.selector.SelectSelector = SelectSelector
        ha.helpers.selector.SelectSelectorMode = SelectSelectorMode
        ha.helpers.selector.NumberSelectorConfig = NumberSelectorConfig
        ha.helpers.selector.NumberSelector = NumberSelector
        ha.helpers.selector.NumberSelectorMode = NumberSelectorMode

        # Config validation
        def string(v):
            return str(v)

        def config_entry_only_config_schema(domain):
            return MagicMock()

        ha.helpers.config_validation.string = string
        ha.helpers.config_validation.config_entry_only_config_schema = (
            config_entry_only_config_schema
        )

        # aiohttp_client
        def async_get_clientsession(hass):
            return MagicMock()

        ha.helpers.aiohttp_client.async_get_clientsession = async_get_clientsession

        # entity registry
        def er_async_get(hass):
            return MagicMock()

        def er_async_entries_for_device(registry, device_id):
            return []

        ha.helpers.entity_registry.async_get = er_async_get
        ha.helpers.entity_registry.async_entries_for_device = er_async_entries_for_device

        # diagnostics
        def async_redact_data(data, to_redact):
            return {k: "***REDACTED***" if k in to_redact else v for k, v in data.items()}

        ha.components.diagnostics.async_redact_data = async_redact_data

        # Register all created mock modules into sys.modules
        sys.modules["homeassistant"] = ha
        sys.modules["homeassistant.const"] = ha.const
        sys.modules["homeassistant.exceptions"] = ha.exceptions
        sys.modules["homeassistant.core"] = ha.core
        sys.modules["homeassistant.util"] = ha.util
        sys.modules["homeassistant.util.dt"] = ha.util.dt
        sys.modules["homeassistant.helpers"] = ha.helpers
        sys.modules["homeassistant.helpers.event"] = ha.helpers.event
        sys.modules["homeassistant.helpers.update_coordinator"] = ha.helpers.update_coordinator
        sys.modules["homeassistant.helpers.device_registry"] = ha.helpers.device_registry
        sys.modules["homeassistant.helpers.entity_platform"] = ha.helpers.entity_platform
        sys.modules["homeassistant.helpers.entity_registry"] = ha.helpers.entity_registry
        sys.modules["homeassistant.helpers.selector"] = ha.helpers.selector
        sys.modules["homeassistant.helpers.config_validation"] = ha.helpers.config_validation
        sys.modules["homeassistant.helpers.aiohttp_client"] = ha.helpers.aiohttp_client
        sys.modules["homeassistant.config_entries"] = ha.config_entries
        sys.modules["homeassistant.data_entry_flow"] = ha.data_entry_flow
        sys.modules["homeassistant.components"] = ha.components
        sys.modules["homeassistant.components.sensor"] = ha.components.sensor
        sys.modules["homeassistant.components.binary_sensor"] = ha.components.binary_sensor
        sys.modules["homeassistant.components.diagnostics"] = ha.components.diagnostics


class MockState:
    """Mock Home Assistant State object."""

    def __init__(
        self,
        entity_id_or_state: str,
        state_or_attributes: Any = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        if attributes is not None:
            self.entity_id = entity_id_or_state
            self.state = str(state_or_attributes)
            self.attributes = attributes
        elif isinstance(state_or_attributes, dict):
            self.entity_id = ""
            self.state = entity_id_or_state
            self.attributes = state_or_attributes
        elif state_or_attributes is not None:
            self.entity_id = entity_id_or_state
            self.state = str(state_or_attributes)
            self.attributes = {}
        else:
            self.entity_id = ""
            self.state = entity_id_or_state
            self.attributes = {}


if "homeassistant.core" in sys.modules:
    sys.modules["homeassistant.core"].State = MockState


class MockStates:
    """Mock Home Assistant State Registry."""

    def __init__(self) -> None:
        self._states: dict[str, MockState] = {}

    def set(self, entity_id: str, state: str, attributes: dict[str, Any] | None = None) -> None:
        self._states[entity_id] = MockState(state, attributes)

    def get(self, entity_id: str) -> MockState | None:
        return self._states.get(entity_id)


class MockServices:
    """Mock Home Assistant Services Registry."""

    def __init__(self) -> None:
        self._services: dict[tuple[str, str], Any] = {}

    def has_service(self, domain: str, service: str) -> bool:
        return (domain, service) in self._services

    def async_register(self, domain: str, service: str, handler: Any, schema: Any = None) -> None:
        self._services[(domain, service)] = handler

    def async_remove(self, domain: str, service: str) -> None:
        self._services.pop((domain, service), None)


class MockHass:
    """Mock HomeAssistant core instance."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.states = MockStates()
        self.services = MockServices()
        self.config_entries = MagicMock()


@pytest.fixture
def mock_hass() -> MockHass:
    """Provide a mock HomeAssistant instance."""
    return MockHass()


def pytest_pyfunc_call(pyfuncitem):
    """Run async test functions in an asyncio event loop."""
    if inspect.iscoroutinefunction(pyfuncitem.obj):
        testfunction = pyfuncitem.obj
        funcargs = pyfuncitem.funcargs
        testargs = {arg: funcargs[arg] for arg in pyfuncitem._fixtureinfo.argnames}
        asyncio.run(testfunction(**testargs))
        return True
    return None
