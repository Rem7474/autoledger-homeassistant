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
    """Test config flow with network connection failure."""
    flow = AutoLedgerConfigFlow()
    flow.hass = mock_hass

    user_input = {
        CONF_HOST: "https://unreachable.host",
        CONF_API_KEY: "any_token",
        CONF_VERIFY_SSL: False,
    }

    with patch(
        "custom_components.autoledger.config_flow.AutoLedgerApiClient.async_test_connection",
        side_effect=AutoLedgerConnectionError("Unreachable"),
    ):
        result = await flow.async_step_user(user_input=user_input)

    assert result["type"] == "form"
    assert result["errors"]["base"] == "cannot_connect"


@pytest.mark.asyncio
async def test_flow_reconfigure(mock_hass):
    """Test reconfigure flow successfully updating credentials."""
    entry = ConfigEntry(
        entry_id="test_reconfig",
        data={CONF_HOST: "http://old.local", CONF_API_KEY: "old_key"},
    )
    flow = AutoLedgerConfigFlow()
    flow.hass = mock_hass
    flow._reconfigure_entry = entry

    # Initial form
    form = await flow.async_step_reconfigure(user_input=None)
    assert form["type"] == "form"
    assert form["step_id"] == "reconfigure"

    # Submit valid update
    with patch(
        "custom_components.autoledger.config_flow.AutoLedgerApiClient.async_test_connection",
        new_callable=AsyncMock,
        return_value=True,
    ):
        res = await flow.async_step_reconfigure(
            user_input={
                CONF_HOST: "http://new.local",
                CONF_API_KEY: "new_key",
                CONF_VERIFY_SSL: True,
            }
        )

    assert res["type"] == "abort"
    assert res["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == "http://new.local"
    assert entry.data[CONF_API_KEY] == "new_key"


@pytest.mark.asyncio
async def test_options_flow_init_menu(mock_hass):
    """Test direct options flow menu depending on configured entities."""
    # When empty
    entry_empty = ConfigEntry(entry_id="e1", data={}, options={})
    handler_empty = AutoLedgerOptionsFlowHandler(entry_empty)
    handler_empty.hass = mock_hass
    res_empty = await handler_empty.async_step_init()
    assert res_empty["type"] == "menu"
    assert res_empty["menu_options"] == ["add_vehicle", "add_charger"]

    # When vehicle and charger configured
    entry_configured = ConfigEntry(
        entry_id="e2",
        data={},
        options={
            CONF_VEHICLES: {"v1": {CONF_VEHICLE_NAME: "Car"}},
            CONF_CHARGERS: {"c1": {CONF_CHARGER_NAME: "Wallbox"}},
        },
    )
    handler_cfg = AutoLedgerOptionsFlowHandler(entry_configured)
    handler_cfg.hass = mock_hass
    res_cfg = await handler_cfg.async_step_init()
    assert res_cfg["type"] == "menu"
    assert res_cfg["menu_options"] == [
        "edit_vehicle",
        "add_vehicle",
        "edit_charger",
        "add_charger",
        "remove_item",
    ]


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
    """Test full add vehicle options flow workflow with preview and validation."""
    entry = ConfigEntry(
        entry_id="entry_test",
        data={CONF_HOST: "http://autoledger.local", CONF_API_KEY: "key"},
        options={},
    )
    handler = AutoLedgerOptionsFlowHandler(entry)
    handler.hass = mock_hass

    # Setup valid mock states with appropriate units
    mock_hass.states.set("sensor.tesla_battery", "82", {"unit_of_measurement": "%"})
    mock_hass.states.set("sensor.tesla_odometer", "15420", {"unit_of_measurement": "km"})
    mock_hass.states.set("binary_sensor.tesla_charging", "on")

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

        # Step 2: Configure vehicle entities -> moves to preview
        step_2_submit = await handler.async_step_vehicle_mapping(
            user_input={
                CONF_BATTERY_SOC_ENTITY: "sensor.tesla_battery",
                CONF_ODOMETER_ENTITY: "sensor.tesla_odometer",
                CONF_CHARGING_STATUS_ENTITY: "binary_sensor.tesla_charging",
            }
        )
        assert step_2_submit["type"] == "form"
        assert step_2_submit["step_id"] == "vehicle_preview"
        assert "82 %" in step_2_submit["description_placeholders"]["battery_preview"]
        assert "15 420 km" in step_2_submit["description_placeholders"]["odometer_preview"]

        # Step 3: Confirm preview -> creates entry
        step_3_submit = await handler.async_step_vehicle_preview(user_input={})
        assert step_3_submit["type"] == "create_entry"
        saved_vehicles = step_3_submit["data"][CONF_VEHICLES]
        assert "v-uuid-1" in saved_vehicles
        assert saved_vehicles["v-uuid-1"][CONF_BATTERY_SOC_ENTITY] == "sensor.tesla_battery"
        assert saved_vehicles["v-uuid-1"][CONF_ODOMETER_ENTITY] == "sensor.tesla_odometer"
        assert (
            saved_vehicles["v-uuid-1"][CONF_CHARGING_STATUS_ENTITY]
            == "binary_sensor.tesla_charging"
        )


@pytest.mark.asyncio
async def test_options_flow_vehicle_unit_validation(mock_hass):
    """Test vehicle telemetry unit validation (rejects invalid units and negative values)."""
    entry = ConfigEntry(entry_id="e_val", data={}, options={})
    handler = AutoLedgerOptionsFlowHandler(entry)
    handler.hass = mock_hass
    handler._temp_vehicle = {CONF_VEHICLE_ID: "v1", CONF_VEHICLE_NAME: "Car"}

    # Mock invalid battery unit (km instead of %)
    mock_hass.states.set("sensor.wrong_battery", "50", {"unit_of_measurement": "km"})
    # Mock invalid odometer unit (% instead of km/mi)
    mock_hass.states.set("sensor.wrong_odo", "1000", {"unit_of_measurement": "%"})

    res_err = await handler.async_step_vehicle_mapping(
        user_input={
            CONF_BATTERY_SOC_ENTITY: "sensor.wrong_battery",
            CONF_ODOMETER_ENTITY: "sensor.wrong_odo",
        }
    )
    assert res_err["type"] == "form"
    assert res_err["errors"][CONF_BATTERY_SOC_ENTITY] == "invalid_battery_unit"
    assert res_err["errors"][CONF_ODOMETER_ENTITY] == "invalid_odometer_unit"

    # Mock negative odometer
    mock_hass.states.set("sensor.neg_odo", "-50", {"unit_of_measurement": "km"})
    res_neg = await handler.async_step_vehicle_mapping(
        user_input={
            CONF_BATTERY_SOC_ENTITY: None,
            CONF_ODOMETER_ENTITY: "sensor.neg_odo",
        }
    )
    assert res_neg["type"] == "form"
    assert res_neg["errors"][CONF_ODOMETER_ENTITY] == "invalid_odometer_range"


@pytest.mark.asyncio
async def test_options_flow_edit_vehicle_1click_shortcut(mock_hass):
    """Test 1-click edit shortcut: when only 1 vehicle exists, skips selector dropdown."""
    entry = ConfigEntry(
        entry_id="entry_test",
        data={CONF_HOST: "http://autoledger.local", CONF_API_KEY: "key"},
        options={
            CONF_VEHICLES: {
                "v-1": {
                    CONF_VEHICLE_ID: "v-1",
                    CONF_VEHICLE_NAME: "My Car",
                    CONF_BATTERY_SOC_ENTITY: "sensor.soc",
                }
            }
        },
    )
    handler = AutoLedgerOptionsFlowHandler(entry)
    handler.hass = mock_hass

    # Single vehicle: directly jumps to vehicle_mapping without asking to choose vehicle
    step_direct = await handler.async_step_edit_vehicle()
    assert step_direct["type"] == "form"
    assert step_direct["step_id"] == "vehicle_mapping"

    # Submit updated mapping -> goes to preview
    step_update = await handler.async_step_vehicle_mapping(
        user_input={
            CONF_BATTERY_SOC_ENTITY: "sensor.new_soc",
            CONF_ODOMETER_ENTITY: "sensor.new_odo",
            CONF_CHARGING_STATUS_ENTITY: "sensor.new_status",
        }
    )
    assert step_update["type"] == "form"
    assert step_update["step_id"] == "vehicle_preview"

    # Confirm preview -> saved
    step_save = await handler.async_step_vehicle_preview(user_input={})
    assert step_save["type"] == "create_entry"
    saved = step_save["data"][CONF_VEHICLES]["v-1"]
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
    """Test adding a charger with fixed vehicle assignment mode and preview."""
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

    mock_hass.states.set("sensor.wallbox_energy", "1250", {"unit_of_measurement": "kWh"})

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

    # Step 2: Fixed assignment mode -> moves to preview
    res2 = await handler.async_step_charger_step2(
        user_input={
            CONF_ASSIGNMENT_MODE: ASSIGNMENT_MODE_FIXED,
            CONF_LINKED_VEHICLE_ID: "v-tesla",
        }
    )
    assert res2["type"] == "form"
    assert res2["step_id"] == "charger_preview"

    # Step 3: Confirm preview -> creates entry
    res3 = await handler.async_step_charger_preview(user_input={})
    assert res3["type"] == "create_entry"
    chargers = res3["data"][CONF_CHARGERS]
    assert "garage_wallbox" in chargers
    saved = chargers["garage_wallbox"]
    assert saved[CONF_CHARGER_NAME] == "Garage Wallbox"
    assert saved[CONF_ASSIGNMENT_MODE] == ASSIGNMENT_MODE_FIXED
    assert saved[CONF_LINKED_VEHICLE_ID] == "v-tesla"
    assert saved[CONF_DEBOUNCE_SECONDS] == 90


@pytest.mark.asyncio
async def test_options_flow_charger_energy_unit_validation(mock_hass):
    """Test charger energy meter unit validation (rejects power kW/W)."""
    entry = ConfigEntry(entry_id="e_c", data={}, options={})
    handler = AutoLedgerOptionsFlowHandler(entry)
    handler.hass = mock_hass

    # Instantaneous power kW instead of energy kWh
    mock_hass.states.set("sensor.power_meter", "7.4", {"unit_of_measurement": "kW"})

    res_err = await handler.async_step_charger_step1(
        user_input={
            CONF_CHARGER_NAME: "Wallbox",
            CONF_CHARGING_STATUS_ENTITY: "binary_sensor.wb",
            CONF_ENERGY_METER_ENTITY: "sensor.power_meter",
        }
    )
    assert res_err["type"] == "form"
    assert res_err["errors"][CONF_ENERGY_METER_ENTITY] == "invalid_energy_unit"


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

    # Valid entity succeeds to preview
    success_res = await handler.async_step_charger_step2(
        user_input={
            CONF_ASSIGNMENT_MODE: ASSIGNMENT_MODE_INPUT_SELECT,
            CONF_VEHICLE_SELECT_ENTITY: "input_select.active_vehicle",
        }
    )
    assert success_res["type"] == "form"
    assert success_res["step_id"] == "charger_preview"

    confirm_res = await handler.async_step_charger_preview(user_input={})
    assert confirm_res["type"] == "create_entry"
    saved = confirm_res["data"][CONF_CHARGERS]["smart_plug"]
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
    assert res_corr["type"] == "form"
    assert res_corr["step_id"] == "charger_preview"
    save_corr = await handler.async_step_charger_preview(user_input={})
    assert (
        save_corr["data"][CONF_CHARGERS]["borne_auto"][CONF_ASSIGNMENT_MODE]
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
    assert res_unassigned["type"] == "form"
    save_unassigned = await handler2.async_step_charger_preview(user_input={})
    assert (
        save_unassigned["data"][CONF_CHARGERS]["public_submeter"][CONF_ASSIGNMENT_MODE]
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

    # Single charger: 1-click edit shortcut directly opens charger_step1
    step_edit = await handler.async_step_edit_charger()
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
    assert step_2["type"] == "form"
    assert step_2["step_id"] == "charger_preview"

    step_save = await handler.async_step_charger_preview(user_input={})
    assert step_save["type"] == "create_entry"
    chargers = step_save["data"][CONF_CHARGERS]
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


@pytest.mark.asyncio
async def test_options_flow_remove_item_unified(mock_hass):
    """Test unified remove_item step removing vehicles or chargers."""
    entry = ConfigEntry(
        entry_id="entry_unified_rm",
        data={},
        options={
            CONF_VEHICLES: {"v1": {CONF_VEHICLE_NAME: "Car 1"}},
            CONF_CHARGERS: {"c1": {CONF_CHARGER_NAME: "Wallbox 1"}},
        },
    )
    handler = AutoLedgerOptionsFlowHandler(entry)
    handler.hass = mock_hass

    # Show form
    form = await handler.async_step_remove_item(user_input=None)
    assert form["type"] == "form"
    assert form["step_id"] == "remove_item"

    # Remove vehicle
    rm_veh = await handler.async_step_remove_item(user_input={"item_to_remove": "vehicle:v1"})
    assert rm_veh["type"] == "create_entry"
    assert "v1" not in rm_veh["data"][CONF_VEHICLES]
    assert "c1" in rm_veh["data"][CONF_CHARGERS]

    # Remove charger
    entry2 = ConfigEntry(entry_id="entry_unified_rm2", data={}, options=rm_veh["data"])
    handler2 = AutoLedgerOptionsFlowHandler(entry2)
    handler2.hass = mock_hass
    rm_chg = await handler2.async_step_remove_item(user_input={"item_to_remove": "charger:c1"})
    assert rm_chg["type"] == "create_entry"
    assert "c1" not in rm_chg["data"][CONF_CHARGERS]
