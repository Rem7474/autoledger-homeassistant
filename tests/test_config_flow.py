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
async def test_options_flow_init_menu(mock_hass):
    """Test options flow init menu presenting manage_vehicles and manage_chargers."""
    entry = ConfigEntry(
        entry_id="entry_1",
        data={CONF_HOST: "http://autoledger.local", CONF_API_KEY: "key"},
        options={},
    )
    handler = AutoLedgerOptionsFlowHandler(entry)
    handler.hass = mock_hass

    res = await handler.async_step_init()
    assert res["type"] == "menu"
    assert res["menu_options"] == ["manage_vehicles", "manage_chargers"]


@pytest.mark.asyncio
async def test_options_flow_manage_vehicles_menu(mock_hass):
    """Test manage_vehicles menu depending on configured vehicles."""
    entry_empty = ConfigEntry(
        entry_id="entry_empty",
        data={CONF_HOST: "http://autoledger.local", CONF_API_KEY: "key"},
        options={},
    )
    handler_empty = AutoLedgerOptionsFlowHandler(entry_empty)
    handler_empty.hass = mock_hass

    res_empty = await handler_empty.async_step_manage_vehicles()
    assert res_empty["type"] == "menu"
    assert res_empty["menu_options"] == ["add_vehicle"]

    entry_with_v = ConfigEntry(
        entry_id="entry_v",
        data={CONF_HOST: "http://autoledger.local", CONF_API_KEY: "key"},
        options={CONF_VEHICLES: {"v-1": {CONF_VEHICLE_NAME: "Tesla"}}},
    )
    handler_v = AutoLedgerOptionsFlowHandler(entry_with_v)
    handler_v.hass = mock_hass

    res_v = await handler_v.async_step_manage_vehicles()
    assert res_v["type"] == "menu"
    assert res_v["menu_options"] == ["add_vehicle", "edit_vehicle", "remove_vehicle"]


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

    vehicles_api = [{"id": "v-uuid-1", "make": "Tesla", "model": "Model Y", "name": "Family Car"}]

    with patch.object(handler, "_get_api_client") as mock_client_factory:
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

        # Step 2: Configure vehicle entities
        step_2_submit = await handler.async_step_vehicle_mapping(
            user_input={
                CONF_BATTERY_SOC_ENTITY: "sensor.tesla_battery",
                CONF_ODOMETER_ENTITY: "sensor.tesla_odometer",
                CONF_CHARGING_STATUS_ENTITY: "binary_sensor.tesla_charging",
            }
        )
        assert step_2_submit["type"] == "create_entry"
        saved_vehicles = step_2_submit["data"][CONF_VEHICLES]
        assert "v-uuid-1" in saved_vehicles
        assert saved_vehicles["v-uuid-1"][CONF_BATTERY_SOC_ENTITY] == "sensor.tesla_battery"
        assert saved_vehicles["v-uuid-1"][CONF_ODOMETER_ENTITY] == "sensor.tesla_odometer"
        assert (
            saved_vehicles["v-uuid-1"][CONF_CHARGING_STATUS_ENTITY]
            == "binary_sensor.tesla_charging"
        )


@pytest.mark.asyncio
async def test_options_flow_edit_vehicle(mock_hass):
    """Test editing an existing vehicle mapping."""
    entry = ConfigEntry(
        entry_id="entry_test",
        data={CONF_HOST: "http://autoledger.local", CONF_API_KEY: "key"},
        options={
            CONF_VEHICLES: {
                "v-1": {
                    CONF_VEHICLE_ID: "v-1",
                    CONF_VEHICLE_NAME: "Old Name",
                    CONF_BATTERY_SOC_ENTITY: "sensor.old_soc",
                }
            }
        },
    )
    handler = AutoLedgerOptionsFlowHandler(entry)
    handler.hass = mock_hass

    # Select vehicle to edit
    step_select = await handler.async_step_edit_vehicle(user_input={CONF_VEHICLE_ID: "v-1"})
    assert step_select["type"] == "form"
    assert step_select["step_id"] == "vehicle_mapping"

    # Submit updated mapping
    step_update = await handler.async_step_vehicle_mapping(
        user_input={
            CONF_BATTERY_SOC_ENTITY: "sensor.new_soc",
            CONF_ODOMETER_ENTITY: "sensor.new_odo",
            CONF_CHARGING_STATUS_ENTITY: "sensor.new_status",
        }
    )
    assert step_update["type"] == "create_entry"
    saved = step_update["data"][CONF_VEHICLES]["v-1"]
    assert saved[CONF_BATTERY_SOC_ENTITY] == "sensor.new_soc"
    assert saved[CONF_ODOMETER_ENTITY] == "sensor.new_odo"


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

    result = await handler.async_step_remove_vehicle(user_input={CONF_VEHICLE_ID: "v-1"})
    assert result["type"] == "create_entry"
    remaining = result["data"][CONF_VEHICLES]
    assert "v-1" not in remaining
    assert "v-2" in remaining


@pytest.mark.asyncio
async def test_options_flow_manage_chargers_menu(mock_hass):
    """Test manage_chargers menu depending on configured chargers."""
    entry_empty = ConfigEntry(
        entry_id="entry_c_empty",
        data={CONF_HOST: "http://autoledger.local", CONF_API_KEY: "key"},
        options={},
    )
    handler_empty = AutoLedgerOptionsFlowHandler(entry_empty)
    handler_empty.hass = mock_hass

    res_empty = await handler_empty.async_step_manage_chargers()
    assert res_empty["type"] == "menu"
    assert res_empty["menu_options"] == ["add_charger"]

    entry_with_c = ConfigEntry(
        entry_id="entry_c",
        data={CONF_HOST: "http://autoledger.local", CONF_API_KEY: "key"},
        options={CONF_CHARGERS: {"wallbox_1": {CONF_CHARGER_NAME: "Wallbox"}}},
    )
    handler_c = AutoLedgerOptionsFlowHandler(entry_with_c)
    handler_c.hass = mock_hass

    res_c = await handler_c.async_step_manage_chargers()
    assert res_c["type"] == "menu"
    assert res_c["menu_options"] == ["add_charger", "edit_charger", "remove_charger"]


@pytest.mark.asyncio
async def test_options_flow_add_charger_fixed_mode(mock_hass):
    """Test adding a charger with fixed vehicle assignment mode."""
    entry = ConfigEntry(
        entry_id="entry_c_test",
        data={CONF_HOST: "http://autoledger.local", CONF_API_KEY: "key"},
        options={
            CONF_VEHICLES: {
                "v-tesla": {CONF_VEHICLE_ID: "v-tesla", CONF_VEHICLE_NAME: "Tesla Model 3"}
            }
        },
    )
    handler = AutoLedgerOptionsFlowHandler(entry)
    handler.hass = mock_hass

    # Step 1: Charger info
    res1 = await handler.async_step_charger_step1(
        user_input={
            CONF_CHARGER_NAME: "Garage Wallbox",
            CONF_CHARGING_STATUS_ENTITY: "binary_sensor.wallbox_charging",
            CONF_ENERGY_METER_ENTITY: "sensor.wallbox_energy",
            CONF_ENERGY_METER_TYPE: ENERGY_METER_TYPE_TOTAL_INCREASING,
            CONF_DEBOUNCE_SECONDS: 90,
            CONF_CHARGING_LOCATION: "home",
        }
    )
    assert res1["type"] == "form"
    assert res1["step_id"] == "charger_step2"

    # Step 2: Fixed assignment mode
    res2 = await handler.async_step_charger_step2(
        user_input={
            CONF_ASSIGNMENT_MODE: ASSIGNMENT_MODE_FIXED,
            CONF_LINKED_VEHICLE_ID: "v-tesla",
        }
    )
    assert res2["type"] == "create_entry"
    chargers = res2["data"][CONF_CHARGERS]
    assert "garage_wallbox" in chargers
    saved = chargers["garage_wallbox"]
    assert saved[CONF_CHARGER_NAME] == "Garage Wallbox"
    assert saved[CONF_ASSIGNMENT_MODE] == ASSIGNMENT_MODE_FIXED
    assert saved[CONF_LINKED_VEHICLE_ID] == "v-tesla"
    assert saved[CONF_DEBOUNCE_SECONDS] == 90


@pytest.mark.asyncio
async def test_options_flow_add_charger_fixed_mode_error(mock_hass):
    """Test fixed mode requires linked_vehicle_id."""
    entry = ConfigEntry(
        entry_id="entry_c_test",
        data={CONF_HOST: "http://autoledger.local", CONF_API_KEY: "key"},
        options={},
    )
    handler = AutoLedgerOptionsFlowHandler(entry)
    handler.hass = mock_hass

    await handler.async_step_charger_step1(
        user_input={
            CONF_CHARGER_NAME: "Wallbox",
            CONF_CHARGING_STATUS_ENTITY: "binary_sensor.wb_state",
        }
    )

    res2 = await handler.async_step_charger_step2(
        user_input={
            CONF_ASSIGNMENT_MODE: ASSIGNMENT_MODE_FIXED,
            CONF_LINKED_VEHICLE_ID: None,
        }
    )
    assert res2["type"] == "form"
    assert res2["errors"]["base"] == "select_vehicle"


@pytest.mark.asyncio
async def test_options_flow_add_charger_input_select_mode(mock_hass):
    """Test adding a charger with input_select dynamic assignment mode."""
    entry = ConfigEntry(
        entry_id="entry_c_test",
        data={CONF_HOST: "http://autoledger.local", CONF_API_KEY: "key"},
        options={},
    )
    handler = AutoLedgerOptionsFlowHandler(entry)
    handler.hass = mock_hass

    await handler.async_step_charger_step1(
        user_input={
            CONF_CHARGER_NAME: "Smart Plug",
            CONF_CHARGING_STATUS_ENTITY: "switch.smart_plug",
        }
    )

    # Missing entity returns error
    err_res = await handler.async_step_charger_step2(
        user_input={
            CONF_ASSIGNMENT_MODE: ASSIGNMENT_MODE_INPUT_SELECT,
            CONF_VEHICLE_SELECT_ENTITY: None,
        }
    )
    assert err_res["errors"]["base"] == "select_entity"

    # Valid entity succeeds
    success_res = await handler.async_step_charger_step2(
        user_input={
            CONF_ASSIGNMENT_MODE: ASSIGNMENT_MODE_INPUT_SELECT,
            CONF_VEHICLE_SELECT_ENTITY: "input_select.active_vehicle",
        }
    )
    assert success_res["type"] == "create_entry"
    saved = success_res["data"][CONF_CHARGERS]["smart_plug"]
    assert saved[CONF_ASSIGNMENT_MODE] == ASSIGNMENT_MODE_INPUT_SELECT
    assert saved[CONF_VEHICLE_SELECT_ENTITY] == "input_select.active_vehicle"


@pytest.mark.asyncio
async def test_options_flow_add_charger_correlation_and_unassigned(mock_hass):
    """Test correlation and unassigned modes require no extra fields."""
    entry = ConfigEntry(
        entry_id="entry_c_test",
        data={CONF_HOST: "http://autoledger.local", CONF_API_KEY: "key"},
        options={},
    )
    handler = AutoLedgerOptionsFlowHandler(entry)
    handler.hass = mock_hass

    # Correlation mode
    await handler.async_step_charger_step1(
        user_input={
            CONF_CHARGER_NAME: "Borne Auto",
            CONF_CHARGING_STATUS_ENTITY: "binary_sensor.borne_active",
        }
    )
    res_corr = await handler.async_step_charger_step2(
        user_input={CONF_ASSIGNMENT_MODE: ASSIGNMENT_MODE_CORRELATION}
    )
    assert res_corr["type"] == "create_entry"
    assert (
        res_corr["data"][CONF_CHARGERS]["borne_auto"][CONF_ASSIGNMENT_MODE]
        == ASSIGNMENT_MODE_CORRELATION
    )

    # Unassigned mode
    handler2 = AutoLedgerOptionsFlowHandler(entry)
    handler2.hass = mock_hass
    await handler2.async_step_charger_step1(
        user_input={
            CONF_CHARGER_NAME: "Public Submeter",
            CONF_CHARGING_STATUS_ENTITY: "sensor.submeter_power",
        }
    )
    res_unassigned = await handler2.async_step_charger_step2(
        user_input={CONF_ASSIGNMENT_MODE: ASSIGNMENT_MODE_UNASSIGNED}
    )
    assert res_unassigned["type"] == "create_entry"
    assert (
        res_unassigned["data"][CONF_CHARGERS]["public_submeter"][CONF_ASSIGNMENT_MODE]
        == ASSIGNMENT_MODE_UNASSIGNED
    )


@pytest.mark.asyncio
async def test_options_flow_edit_and_remove_charger(mock_hass):
    """Test editing and removing a charger configuration."""
    entry = ConfigEntry(
        entry_id="entry_c_test",
        data={CONF_HOST: "http://autoledger.local", CONF_API_KEY: "key"},
        options={
            CONF_CHARGERS: {
                "wallbox": {
                    CONF_CHARGER_ID: "wallbox",
                    CONF_CHARGER_NAME: "Wallbox",
                    CONF_CHARGING_STATUS_ENTITY: "binary_sensor.wb",
                    CONF_DEBOUNCE_SECONDS: 60,
                    CONF_ASSIGNMENT_MODE: ASSIGNMENT_MODE_UNASSIGNED,
                }
            }
        },
    )
    handler = AutoLedgerOptionsFlowHandler(entry)
    handler.hass = mock_hass

    # Edit step
    step_edit = await handler.async_step_edit_charger(user_input={CONF_CHARGER_ID: "wallbox"})
    assert step_edit["type"] == "form"
    assert step_edit["step_id"] == "charger_step1"

    step_1 = await handler.async_step_charger_step1(
        user_input={
            CONF_CHARGER_NAME: "Wallbox Pro",
            CONF_CHARGING_STATUS_ENTITY: "binary_sensor.wb_new",
            CONF_DEBOUNCE_SECONDS: 120,
        }
    )
    assert step_1["type"] == "form"
    assert step_1["step_id"] == "charger_step2"

    step_2 = await handler.async_step_charger_step2(
        user_input={CONF_ASSIGNMENT_MODE: ASSIGNMENT_MODE_UNASSIGNED}
    )
    assert step_2["type"] == "create_entry"
    chargers = step_2["data"][CONF_CHARGERS]
    assert chargers["wallbox"][CONF_CHARGER_NAME] == "Wallbox Pro"
    assert chargers["wallbox"][CONF_DEBOUNCE_SECONDS] == 120

    # Remove step
    handler_remove = AutoLedgerOptionsFlowHandler(
        ConfigEntry(entry_id="e2", data={}, options={CONF_CHARGERS: {"wallbox": {}}})
    )
    handler_remove.hass = mock_hass
    step_rm = await handler_remove.async_step_remove_charger(
        user_input={CONF_CHARGER_ID: "wallbox"}
    )
    assert step_rm["type"] == "create_entry"
    assert "wallbox" not in step_rm["data"][CONF_CHARGERS]
