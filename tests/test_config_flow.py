"""Tests for AutoLedger config flow and options flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntry

from custom_components.autoledger.api import (
    AutoLedgerAuthError,
    AutoLedgerConnectionError,
)
from custom_components.autoledger.config_flow import (
    AutoLedgerConfigFlow,
    AutoLedgerOptionsFlowHandler,
)
from custom_components.autoledger.const import (
    CONF_API_KEY,
    CONF_BATTERY_SOC_ENTITY,
    CONF_CHARGING_STATUS_ENTITY,
    CONF_DEBOUNCE_SECONDS,
    CONF_DEVICE_ID,
    CONF_ENERGY_METER_ENTITY,
    CONF_ENERGY_METER_TYPE,
    CONF_HOST,
    CONF_ODOMETER_ENTITY,
    CONF_VEHICLE_ID,
    CONF_VEHICLES,
    CONF_VERIFY_SSL,
    DOMAIN,
    ENERGY_METER_TYPE_TOTAL_INCREASING,
)


@pytest.mark.asyncio
async def test_flow_user_form_initial(mock_hass):
    """Test initial rendering of user config flow step."""
    flow = AutoLedgerConfigFlow()
    flow.hass = mock_hass

    result = await flow.async_step_user(user_input=None)
    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["errors"] == {}


@pytest.mark.asyncio
async def test_flow_user_success(mock_hass):
    """Test successful config flow submission."""
    flow = AutoLedgerConfigFlow()
    flow.hass = mock_hass

    user_input = {
        CONF_HOST: "http://192.168.1.100:8080",
        CONF_API_KEY: "secret_token_123",
        CONF_VERIFY_SSL: True,
    }

    with patch(
        "custom_components.autoledger.config_flow.AutoLedgerApiClient.async_test_connection",
        new_callable=AsyncMock,
        return_value=True,
    ):
        result = await flow.async_step_user(user_input=user_input)

    assert result["type"] == "create_entry"
    assert "192.168.1.100:8080" in result["title"]
    assert result["data"][CONF_HOST] == "http://192.168.1.100:8080"
    assert result["data"][CONF_API_KEY] == "secret_token_123"


@pytest.mark.asyncio
async def test_flow_user_invalid_auth(mock_hass):
    """Test config flow with authentication failure."""
    flow = AutoLedgerConfigFlow()
    flow.hass = mock_hass

    user_input = {
        CONF_HOST: "https://autoledger.domain.com",
        CONF_API_KEY: "wrong_token",
        CONF_VERIFY_SSL: True,
    }

    with patch(
        "custom_components.autoledger.config_flow.AutoLedgerApiClient.async_test_connection",
        side_effect=AutoLedgerAuthError("Invalid credentials"),
    ):
        result = await flow.async_step_user(user_input=user_input)

    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_auth"


@pytest.mark.asyncio
async def test_flow_user_cannot_connect(mock_hass):
    """Test config flow when server is unreachable."""
    flow = AutoLedgerConfigFlow()
    flow.hass = mock_hass

    user_input = {
        CONF_HOST: "http://unreachable.local",
        CONF_API_KEY: "token",
        CONF_VERIFY_SSL: True,
    }

    with patch(
        "custom_components.autoledger.config_flow.AutoLedgerApiClient.async_test_connection",
        side_effect=AutoLedgerConnectionError("Cannot connect"),
    ):
        result = await flow.async_step_user(user_input=user_input)

    assert result["type"] == "form"
    assert result["errors"]["base"] == "cannot_connect"


@pytest.mark.asyncio
async def test_options_flow_menu(mock_hass):
    """Test options flow menu display depending on configured vehicles."""
    # Entry with no vehicles
    entry_empty = ConfigEntry(
        entry_id="entry_1",
        data={CONF_HOST: "http://autoledger.local", CONF_API_KEY: "key"},
        options={},
    )
    handler_empty = AutoLedgerOptionsFlowHandler(entry_empty)
    handler_empty.hass = mock_hass

    res_empty = await handler_empty.async_step_init()
    assert res_empty["type"] == "menu"
    assert res_empty["menu_options"] == ["add_vehicle"]

    # Entry with 1 configured vehicle
    entry_with_v = ConfigEntry(
        entry_id="entry_2",
        data={CONF_HOST: "http://autoledger.local", CONF_API_KEY: "key"},
        options={CONF_VEHICLES: {"v-1": {"name": "Tesla"}}},
    )
    handler_v = AutoLedgerOptionsFlowHandler(entry_with_v)
    handler_v.hass = mock_hass

    res_v = await handler_v.async_step_init()
    assert res_v["type"] == "menu"
    assert "add_vehicle" in res_v["menu_options"]
    assert "edit_vehicle" in res_v["menu_options"]
    assert "remove_vehicle" in res_v["menu_options"]


@pytest.mark.asyncio
async def test_options_flow_add_vehicle_workflow(mock_hass):
    """Test full add vehicle options flow workflow."""
    entry = ConfigEntry(
        entry_id="entry_test",
        data={CONF_HOST: "http://autoledger.local", CONF_API_KEY: "key"},
        options={},
    )
    handler = AutoLedgerOptionsFlowHandler(entry)
    handler.hass = mock_hass

    # Mock fetching vehicles from API
    vehicles_api = [
        {"id": "v-uuid-1", "make": "Tesla", "model": "Model Y", "name": "Family Car"}
    ]

    with patch.object(
        handler, "_get_api_client"
    ) as mock_client_factory:
        mock_client = AsyncMock()
        mock_client.async_get_vehicles.return_value = vehicles_api
        mock_client_factory.return_value = mock_client

        # Step 1: Add vehicle selection
        step_1_res = await handler.async_step_add_vehicle(user_input=None)
        assert step_1_res["type"] == "form"

        # User selects vehicle and HA device
        step_1_submit = await handler.async_step_add_vehicle(
            user_input={
                CONF_VEHICLE_ID: "v-uuid-1",
                CONF_DEVICE_ID: "device_tesla_1",
            }
        )
        assert step_1_submit["type"] == "form"
        assert step_1_submit["step_id"] == "vehicle_mapping"

        # Step 2: Configure entities
        step_2_submit = await handler.async_step_vehicle_mapping(
            user_input={
                CONF_CHARGING_STATUS_ENTITY: "binary_sensor.tesla_charging",
                CONF_BATTERY_SOC_ENTITY: "sensor.tesla_battery",
                CONF_ODOMETER_ENTITY: "sensor.tesla_odometer",
                CONF_ENERGY_METER_ENTITY: "sensor.wallbox_kwh",
                CONF_ENERGY_METER_TYPE: ENERGY_METER_TYPE_TOTAL_INCREASING,
                CONF_DEBOUNCE_SECONDS: 45,
            }
        )
        assert step_2_submit["type"] == "create_entry"
        saved_vehicles = step_2_submit["data"][CONF_VEHICLES]
        assert "v-uuid-1" in saved_vehicles
        assert (
            saved_vehicles["v-uuid-1"][CONF_CHARGING_STATUS_ENTITY]
            == "binary_sensor.tesla_charging"
        )
        assert saved_vehicles["v-uuid-1"][CONF_DEBOUNCE_SECONDS] == 45


@pytest.mark.asyncio
async def test_options_flow_remove_vehicle(mock_hass):
    """Test removing a vehicle mapping."""
    entry = ConfigEntry(
        entry_id="entry_test",
        data={CONF_HOST: "http://autoledger.local", CONF_API_KEY: "key"},
        options={
            CONF_VEHICLES: {
                "v-1": {"vehicle_name": "Car 1"},
                "v-2": {"vehicle_name": "Car 2"},
            }
        },
    )
    handler = AutoLedgerOptionsFlowHandler(entry)
    handler.hass = mock_hass

    result = await handler.async_step_remove_vehicle(
        user_input={CONF_VEHICLE_ID: "v-1"}
    )
    assert result["type"] == "create_entry"
    remaining = result["data"][CONF_VEHICLES]
    assert "v-1" not in remaining
    assert "v-2" in remaining
