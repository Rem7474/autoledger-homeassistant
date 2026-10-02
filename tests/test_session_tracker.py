"""Tests for AutoLedgerChargerTracker and solar anti-bounce debounce engine."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.autoledger.api import AutoLedgerApiError, AutoLedgerConnectionError
from custom_components.autoledger.const import (
    ASSIGNMENT_MODE_CORRELATION,
    ASSIGNMENT_MODE_FIXED,
    ASSIGNMENT_MODE_INPUT_SELECT,
    ASSIGNMENT_MODE_UNASSIGNED,
    CONF_ASSIGNMENT_MODE,
    CONF_BATTERY_SOC_ENTITY,
    CONF_CHARGER_ID,
    CONF_CHARGER_NAME,
    CONF_CHARGING_LOCATION,
    CONF_CHARGING_STATUS_ENTITY,
    CONF_DEBOUNCE_SECONDS,
    CONF_ENERGY_METER_ENTITY,
    CONF_ENERGY_METER_TYPE,
    CONF_LINKED_VEHICLE_ID,
    CONF_ODOMETER_ENTITY,
    CONF_VEHICLE_ID,
    CONF_VEHICLE_NAME,
    CONF_VEHICLE_SELECT_ENTITY,
    ENERGY_METER_TYPE_SESSION,
    ENERGY_METER_TYPE_TOTAL_INCREASING,
    STATE_CHARGING,
    STATE_COOLING_DOWN,
    STATE_IDLE,
    SYNC_STATUS_ERROR,
    SYNC_STATUS_OK,
)
from custom_components.autoledger.session_tracker import (
    AutoLedgerChargerTracker,
)


@pytest.fixture
def mock_client():
    """Mock AutoLedgerApiClient."""
    client = MagicMock()
    client.async_post_event = AsyncMock(return_value={"status": "recorded"})
    client.async_submit_charge = AsyncMock(return_value={"status": "created"})
    return client


def create_state_event(old_val: str, new_val: str):
    """Helper to create a state change event mock."""
    event = MagicMock()
    event.data = {
        "old_state": MagicMock(state=old_val),
        "new_state": MagicMock(state=new_val),
    }
    return event


@pytest.mark.asyncio
async def test_session_start_idle_to_charging(mock_hass, mock_client):
    """Test transition from IDLE to CHARGING captures initial state."""
    mock_hass.states.set("sensor.car_battery", "25")
    mock_hass.states.set("sensor.car_odometer", "45120.0")
    mock_hass.states.set("sensor.wallbox_energy", "1250.0")

    config = {
        CONF_CHARGING_STATUS_ENTITY: "binary_sensor.car_charging",
        CONF_BATTERY_SOC_ENTITY: "sensor.car_battery",
        CONF_ODOMETER_ENTITY: "sensor.car_odometer",
        CONF_ENERGY_METER_ENTITY: "sensor.wallbox_energy",
        CONF_ENERGY_METER_TYPE: ENERGY_METER_TYPE_TOTAL_INCREASING,
        CONF_DEBOUNCE_SECONDS: 60,
    }

    tracker = AutoLedgerChargerTracker(
        hass=mock_hass,
        client=mock_client,
        charger_id="v-1",
        config=config,
    )

    listener_called = False

    def listener():
        nonlocal listener_called
        listener_called = True

    tracker.register_listener(listener)

    # Trigger charge started
    event = create_state_event("off", "on")
    await tracker._async_on_charging_status_changed(event)

    assert tracker.state == STATE_CHARGING
    assert tracker.soc_start == 25
    assert tracker.odometer_start == 45120.0
    assert tracker.energy_start == 1250.0
    assert tracker.session_start_time is not None
    assert listener_called is True


@pytest.mark.asyncio
async def test_solar_pause_and_resumed_interruption(mock_hass, mock_client):
    """Test solar drop: charge pause arms debounce timer; resuming charge cancels timer."""
    config = {
        CONF_CHARGING_STATUS_ENTITY: "binary_sensor.car_charging",
        CONF_DEBOUNCE_SECONDS: 60,
    }
    tracker = AutoLedgerChargerTracker(mock_hass, mock_client, "v-1", config)

    # 1. Start charging
    await tracker._async_on_charging_status_changed(create_state_event("off", "on"))
    assert tracker.state == STATE_CHARGING
    orig_start_time = tracker.session_start_time

    # 2. Solar cloud passes: charging goes off -> arm timer
    await tracker._async_on_charging_status_changed(create_state_event("on", "off"))
    assert tracker.state == STATE_COOLING_DOWN
    assert tracker._debounce_unsub is not None

    # 3. Sun comes back before timer expires: charging goes on again -> cancel timer & resume
    await tracker._async_on_charging_status_changed(create_state_event("off", "on"))
    assert tracker.state == STATE_CHARGING
    assert tracker._debounce_unsub is None
    # Original start time is preserved
    assert tracker.session_start_time == orig_start_time
    assert mock_client.async_post_event.call_count == 0


@pytest.mark.asyncio
async def test_debounce_expired_finalizes_session_total_increasing(mock_hass, mock_client):
    """Test session completes after debounce expiration with total increasing energy."""
    mock_hass.states.set("sensor.car_battery", "20")
    mock_hass.states.set("sensor.car_odometer", "10000.0")
    mock_hass.states.set("sensor.wallbox_energy", "500.0")

    config = {
        CONF_CHARGER_NAME: "Home Wallbox",
        CONF_CHARGING_STATUS_ENTITY: "binary_sensor.car_charging",
        CONF_BATTERY_SOC_ENTITY: "sensor.car_battery",
        CONF_ODOMETER_ENTITY: "sensor.car_odometer",
        CONF_ENERGY_METER_ENTITY: "sensor.wallbox_energy",
        CONF_ENERGY_METER_TYPE: ENERGY_METER_TYPE_TOTAL_INCREASING,
        CONF_CHARGING_LOCATION: "home",
        CONF_DEBOUNCE_SECONDS: 60,
    }
    tracker = AutoLedgerChargerTracker(mock_hass, mock_client, "v-1", config)

    # 1. Start charge
    await tracker._async_on_charging_status_changed(create_state_event("off", "on"))

    # 2. Charging completes, update sensors to final values
    mock_hass.states.set("sensor.car_battery", "80")
    mock_hass.states.set("sensor.wallbox_energy", "532.45")

    # 3. Charger turns off
    await tracker._async_on_charging_status_changed(create_state_event("on", "off"))
    assert tracker.state == STATE_COOLING_DOWN

    # 4. Debounce timer triggers
    await tracker._async_handle_debounce_expired(None)

    assert tracker.state == STATE_IDLE
    assert tracker.sync_status == SYNC_STATUS_OK
    assert tracker.last_successful_sync is not None
    assert tracker.last_energy_kwh == 32.45
    assert mock_client.async_post_event.call_count == 1

    payload = mock_client.async_post_event.call_args[0][0]
    assert payload["vehicle_id"] == "v-1"
    assert payload["event_type"] == "charging_session_end"
    assert payload["data"]["charger_name"] == "Home Wallbox"
    assert payload["data"]["energy_added_kwh"] == 32.45
    assert payload["data"]["soc_start"] == 20
    assert payload["data"]["soc_end"] == 80
    assert payload["data"]["odometer_km"] == 10000.0
    assert payload["data"]["location"] == "home"


@pytest.mark.asyncio
async def test_session_energy_meter_mode(mock_hass, mock_client):
    """Test session energy meter mode where sensor directly represents session kWh."""
    mock_hass.states.set("sensor.session_energy", "15.8")

    config = {
        CONF_CHARGING_STATUS_ENTITY: "binary_sensor.car_charging",
        CONF_ENERGY_METER_ENTITY: "sensor.session_energy",
        CONF_ENERGY_METER_TYPE: ENERGY_METER_TYPE_SESSION,
    }
    tracker = AutoLedgerChargerTracker(mock_hass, mock_client, "v-1", config)

    await tracker._async_on_charging_status_changed(create_state_event("off", "on"))
    await tracker._async_on_charging_status_changed(create_state_event("on", "off"))
    await tracker._async_handle_debounce_expired(None)

    payload = mock_client.async_post_event.call_args[0][0]
    assert payload["data"]["energy_added_kwh"] == 15.8
    assert tracker.last_energy_kwh == 15.8


@pytest.mark.asyncio
async def test_power_sensor_activation_threshold(mock_hass, mock_client):
    """Test power sensor activation with > 500W and unit kW support."""
    config = {
        CONF_CHARGER_NAME: "Power Wallbox",
        CONF_CHARGING_STATUS_ENTITY: "sensor.wallbox_power",
        CONF_ENERGY_METER_ENTITY: "sensor.wb_kwh",
        CONF_ENERGY_METER_TYPE: ENERGY_METER_TYPE_SESSION,
    }
    tracker = AutoLedgerChargerTracker(mock_hass, mock_client, "wb-power", config)

    # 1. Low standby power (e.g. 15W) -> not charging
    mock_hass.states.set("sensor.wallbox_power", "15.0")
    await tracker._async_on_charging_status_changed(create_state_event("0", "15.0"))
    assert tracker.state == STATE_IDLE

    # 2. Power jumps to 3500W (> 500W) -> charging started
    mock_hass.states.set("sensor.wallbox_power", "3500.0")
    mock_hass.states.set("sensor.wb_kwh", "5.0")
    await tracker._async_on_charging_status_changed(create_state_event("15.0", "3500.0"))
    assert tracker.state == STATE_CHARGING

    # 3. Power drops back to 10W -> cooling down
    mock_hass.states.set("sensor.wallbox_power", "10.0")
    mock_hass.states.set("sensor.wb_kwh", "12.5")
    await tracker._async_on_charging_status_changed(create_state_event("3500.0", "10.0"))
    assert tracker.state == STATE_COOLING_DOWN

    # 4. Timer expires
    await tracker._async_handle_debounce_expired(None)
    assert tracker.state == STATE_IDLE
    payload = mock_client.async_post_event.call_args[0][0]
    assert payload["data"]["energy_added_kwh"] == 12.5


@pytest.mark.asyncio
async def test_assignment_mode_fixed(mock_hass, mock_client):
    """Test Strategy 1: Fixed vehicle assignment."""
    mock_hass.states.set("sensor.tesla_soc", "40")
    mock_hass.states.set("sensor.tesla_odo", "25000.0")
    mock_hass.states.set("sensor.wb_kwh", "100.0")

    vehicles_config = {
        "v-tesla": {
            CONF_VEHICLE_ID: "v-tesla",
            CONF_VEHICLE_NAME: "Model Y",
            CONF_BATTERY_SOC_ENTITY: "sensor.tesla_soc",
            CONF_ODOMETER_ENTITY: "sensor.tesla_odo",
        }
    }

    charger_config = {
        CONF_CHARGER_ID: "wb_garage",
        CONF_CHARGER_NAME: "Garage Wallbox",
        CONF_CHARGING_STATUS_ENTITY: "binary_sensor.wb_charging",
        CONF_ENERGY_METER_ENTITY: "sensor.wb_kwh",
        CONF_ENERGY_METER_TYPE: ENERGY_METER_TYPE_TOTAL_INCREASING,
        CONF_ASSIGNMENT_MODE: ASSIGNMENT_MODE_FIXED,
        CONF_LINKED_VEHICLE_ID: "v-tesla",
    }

    tracker = AutoLedgerChargerTracker(
        hass=mock_hass,
        client=mock_client,
        charger_id="wb_garage",
        config=charger_config,
        vehicles_config=vehicles_config,
    )

    # Start charge
    await tracker._async_on_charging_status_changed(create_state_event("off", "on"))
    assert tracker.state == STATE_CHARGING

    # End charge with new readings
    mock_hass.states.set("sensor.tesla_soc", "85")
    mock_hass.states.set("sensor.wb_kwh", "125.0")
    await tracker._async_on_charging_status_changed(create_state_event("on", "off"))
    await tracker._async_handle_debounce_expired(None)

    payload = mock_client.async_post_event.call_args[0][0]
    assert payload["vehicle_id"] == "v-tesla"
    assert payload["data"]["soc_start"] == 40
    assert payload["data"]["soc_end"] == 85
    assert payload["data"]["odometer_km"] == 25000.0
    assert payload["data"]["energy_added_kwh"] == 25.0


@pytest.mark.asyncio
async def test_assignment_mode_input_select(mock_hass, mock_client):
    """Test Strategy 2: Dynamic input_select vehicle assignment."""
    mock_hass.states.set("sensor.mg4_soc", "30")
    mock_hass.states.set("sensor.mg4_odo", "12000.0")
    mock_hass.states.set("sensor.zoe_soc", "50")
    mock_hass.states.set("sensor.zoe_odo", "45000.0")
    mock_hass.states.set("sensor.wb_kwh", "20.0")

    # input_select holds the vehicle name or ID currently plugged
    mock_hass.states.set("input_select.active_car", "MG4 Electric")

    vehicles_config = {
        "v-mg4": {
            CONF_VEHICLE_ID: "v-mg4",
            CONF_VEHICLE_NAME: "MG4 Electric",
            CONF_BATTERY_SOC_ENTITY: "sensor.mg4_soc",
            CONF_ODOMETER_ENTITY: "sensor.mg4_odo",
        },
        "v-zoe": {
            CONF_VEHICLE_ID: "v-zoe",
            CONF_VEHICLE_NAME: "Renault Zoe",
            CONF_BATTERY_SOC_ENTITY: "sensor.zoe_soc",
            CONF_ODOMETER_ENTITY: "sensor.zoe_odo",
        },
    }

    charger_config = {
        CONF_CHARGER_ID: "shared_wb",
        CONF_CHARGER_NAME: "Courtyard Charger",
        CONF_CHARGING_STATUS_ENTITY: "binary_sensor.wb_charging",
        CONF_ENERGY_METER_ENTITY: "sensor.wb_kwh",
        CONF_ENERGY_METER_TYPE: ENERGY_METER_TYPE_SESSION,
        CONF_ASSIGNMENT_MODE: ASSIGNMENT_MODE_INPUT_SELECT,
        CONF_VEHICLE_SELECT_ENTITY: "input_select.active_car",
    }

    tracker = AutoLedgerChargerTracker(
        hass=mock_hass,
        client=mock_client,
        charger_id="shared_wb",
        config=charger_config,
        vehicles_config=vehicles_config,
    )

    # Start and finish charge
    await tracker._async_on_charging_status_changed(create_state_event("off", "on"))
    mock_hass.states.set("sensor.mg4_soc", "80")
    mock_hass.states.set("sensor.wb_kwh", "28.5")

    await tracker._async_on_charging_status_changed(create_state_event("on", "off"))
    await tracker._async_handle_debounce_expired(None)

    payload = mock_client.async_post_event.call_args[0][0]
    assert payload["vehicle_id"] == "v-mg4"
    assert payload["data"]["soc_start"] == 30
    assert payload["data"]["soc_end"] == 80
    assert payload["data"]["odometer_km"] == 12000.0
    assert payload["data"]["energy_added_kwh"] == 28.5


@pytest.mark.asyncio
async def test_assignment_mode_correlation(mock_hass, mock_client):
    """Test Strategy 3: Automatic correlation based on car charging state."""
    # 2 vehicles configured, but only Tesla reports charging state
    mock_hass.states.set("binary_sensor.tesla_charging", "on")
    mock_hass.states.set("sensor.tesla_soc", "55")
    mock_hass.states.set("sensor.tesla_odo", "32000.0")

    mock_hass.states.set("binary_sensor.mg4_charging", "off")
    mock_hass.states.set("sensor.mg4_soc", "90")
    mock_hass.states.set("sensor.mg4_odo", "8000.0")

    mock_hass.states.set("sensor.wb_kwh", "10.0")

    vehicles_config = {
        "v-tesla": {
            CONF_VEHICLE_ID: "v-tesla",
            CONF_VEHICLE_NAME: "Tesla Model Y",
            CONF_CHARGING_STATUS_ENTITY: "binary_sensor.tesla_charging",
            CONF_BATTERY_SOC_ENTITY: "sensor.tesla_soc",
            CONF_ODOMETER_ENTITY: "sensor.tesla_odo",
        },
        "v-mg4": {
            CONF_VEHICLE_ID: "v-mg4",
            CONF_VEHICLE_NAME: "MG4",
            CONF_CHARGING_STATUS_ENTITY: "binary_sensor.mg4_charging",
            CONF_BATTERY_SOC_ENTITY: "sensor.mg4_soc",
            CONF_ODOMETER_ENTITY: "sensor.mg4_odo",
        },
    }

    charger_config = {
        CONF_CHARGER_ID: "smart_wb",
        CONF_CHARGER_NAME: "Smart Wallbox",
        CONF_CHARGING_STATUS_ENTITY: "binary_sensor.wb_charging",
        CONF_ENERGY_METER_ENTITY: "sensor.wb_kwh",
        CONF_ENERGY_METER_TYPE: ENERGY_METER_TYPE_SESSION,
        CONF_ASSIGNMENT_MODE: ASSIGNMENT_MODE_CORRELATION,
    }

    tracker = AutoLedgerChargerTracker(
        hass=mock_hass,
        client=mock_client,
        charger_id="smart_wb",
        config=charger_config,
        vehicles_config=vehicles_config,
    )

    await tracker._async_on_charging_status_changed(create_state_event("off", "on"))
    mock_hass.states.set("sensor.tesla_soc", "90")
    mock_hass.states.set("sensor.wb_kwh", "22.0")

    await tracker._async_on_charging_status_changed(create_state_event("on", "off"))
    await tracker._async_handle_debounce_expired(None)

    payload = mock_client.async_post_event.call_args[0][0]
    assert payload["vehicle_id"] == "v-tesla"
    assert payload["data"]["soc_start"] == 55
    assert payload["data"]["soc_end"] == 90
    assert payload["data"]["odometer_km"] == 32000.0


@pytest.mark.asyncio
async def test_assignment_mode_unassigned(mock_hass, mock_client):
    """Test Strategy 4: Unassigned session sends vehicle_id=None."""
    mock_hass.states.set("sensor.wb_kwh", "14.2")

    charger_config = {
        CONF_CHARGER_ID: "blind_wb",
        CONF_CHARGER_NAME: "Guest Charger",
        CONF_CHARGING_STATUS_ENTITY: "binary_sensor.wb_charging",
        CONF_ENERGY_METER_ENTITY: "sensor.wb_kwh",
        CONF_ENERGY_METER_TYPE: ENERGY_METER_TYPE_SESSION,
        CONF_ASSIGNMENT_MODE: ASSIGNMENT_MODE_UNASSIGNED,
    }

    tracker = AutoLedgerChargerTracker(
        hass=mock_hass,
        client=mock_client,
        charger_id="blind_wb",
        config=charger_config,
        vehicles_config={},
    )

    await tracker._async_on_charging_status_changed(create_state_event("off", "on"))
    await tracker._async_on_charging_status_changed(create_state_event("on", "off"))
    await tracker._async_handle_debounce_expired(None)

    payload = mock_client.async_post_event.call_args[0][0]
    assert payload["vehicle_id"] is None
    assert payload["data"]["charger_name"] == "Guest Charger"
    assert payload["data"]["energy_added_kwh"] == 14.2
    assert payload["data"]["soc_start"] is None
    assert payload["data"]["soc_end"] is None
    assert payload["data"]["odometer_km"] is None


@pytest.mark.asyncio
async def test_session_error_handling(mock_hass, mock_client):
    """Test sync status is set to error if post_event raises an exception."""
    mock_client.async_post_event.side_effect = RuntimeError("Server Down")
    mock_hass.states.set("sensor.wallbox_energy", "100.0")

    config = {
        CONF_CHARGING_STATUS_ENTITY: "binary_sensor.car_charging",
        CONF_ENERGY_METER_ENTITY: "sensor.wallbox_energy",
    }
    tracker = AutoLedgerChargerTracker(mock_hass, mock_client, "v-1", config)

    await tracker._async_on_charging_status_changed(create_state_event("off", "on"))
    mock_hass.states.set("sensor.wallbox_energy", "110.0")
    await tracker._async_on_charging_status_changed(create_state_event("on", "off"))
    await tracker._async_handle_debounce_expired(None)

    assert tracker.sync_status == SYNC_STATUS_ERROR
    assert "Server Down" in tracker.last_error_message


@pytest.mark.asyncio
async def test_manual_charge_submission(mock_hass, mock_client):
    """Test manual charge submission helper."""
    config = {CONF_CHARGING_LOCATION: "office"}
    tracker = AutoLedgerChargerTracker(mock_hass, mock_client, "v-1", config)

    res = await tracker.async_submit_manual_charge(
        kwh=40.0,
        cost=10.0,
        odometer_km=50000.0,
        soc_start=10,
        soc_end=90,
    )
    assert res == {"status": "created"}
    assert tracker.sync_status == SYNC_STATUS_OK
    assert mock_client.async_submit_charge.call_args[1]["location"] == "office"


@pytest.mark.asyncio
async def test_tracker_unload(mock_hass, mock_client):
    """Test unloading tracker cancels debounce timer."""
    tracker = AutoLedgerChargerTracker(
        mock_hass,
        mock_client,
        "v-1",
        {CONF_CHARGING_STATUS_ENTITY: "binary_sensor.car_charging"},
    )
    await tracker._async_on_charging_status_changed(create_state_event("off", "on"))
    await tracker._async_on_charging_status_changed(create_state_event("on", "off"))
    assert tracker._debounce_unsub is not None

    await tracker.async_unload()
    assert tracker._debounce_unsub is None


@pytest.mark.asyncio
async def test_session_payload_identifies_the_session_and_ends_at_the_stop(mock_hass, mock_client):
    """The event carries a stable identifier and ends when charging stopped, not after the debounce."""
    mock_hass.states.set("sensor.wallbox_energy", "500.0")
    config = {
        CONF_CHARGING_STATUS_ENTITY: "binary_sensor.car_charging",
        CONF_ENERGY_METER_ENTITY: "sensor.wallbox_energy",
        CONF_ENERGY_METER_TYPE: ENERGY_METER_TYPE_TOTAL_INCREASING,
    }
    tracker = AutoLedgerChargerTracker(mock_hass, mock_client, "garage", config)

    await tracker._async_on_charging_status_changed(create_state_event("off", "on"))
    start_time = tracker.session_start_time
    mock_hass.states.set("sensor.wallbox_energy", "512.0")
    await tracker._async_on_charging_status_changed(create_state_event("on", "off"))
    stop_time = tracker.session_stop_time
    assert stop_time is not None
    await tracker._async_handle_debounce_expired(None)

    payload = mock_client.async_post_event.call_args[0][0]
    assert payload["event_id"] == f"garage:{start_time}"
    assert payload["data"]["start_time"] == start_time
    assert payload["data"]["end_time"] == stop_time


@pytest.mark.asyncio
async def test_unavailable_charger_does_not_end_the_session(mock_hass, mock_client):
    """A charger dropping off the network is not a stop."""
    config = {CONF_CHARGING_STATUS_ENTITY: "binary_sensor.car_charging"}
    tracker = AutoLedgerChargerTracker(mock_hass, mock_client, "v-1", config)

    await tracker._async_on_charging_status_changed(create_state_event("off", "on"))
    start_time = tracker.session_start_time
    for offline in ("unavailable", "unknown"):
        await tracker._async_on_charging_status_changed(create_state_event("on", offline))
        assert tracker.state == STATE_CHARGING
        assert tracker._debounce_unsub is None

    await tracker._async_on_charging_status_changed(create_state_event("unavailable", "on"))
    assert tracker.state == STATE_CHARGING
    assert tracker.session_start_time == start_time
    assert mock_client.async_post_event.call_count == 0


@pytest.mark.asyncio
async def test_session_without_energy_is_not_sent(mock_hass, mock_client):
    """Plugged in without charging: nothing is sent and the tracker is ready for the next session."""
    mock_hass.states.set("sensor.wallbox_energy", "700.0")
    config = {
        CONF_CHARGING_STATUS_ENTITY: "binary_sensor.car_charging",
        CONF_ENERGY_METER_ENTITY: "sensor.wallbox_energy",
    }
    tracker = AutoLedgerChargerTracker(mock_hass, mock_client, "v-1", config)

    await tracker._async_on_charging_status_changed(create_state_event("off", "on"))
    await tracker._async_on_charging_status_changed(create_state_event("on", "off"))
    await tracker._async_handle_debounce_expired(None)

    assert mock_client.async_post_event.call_count == 0
    assert tracker.state == STATE_IDLE
    assert tracker.session_start_time is None


class MemoryStore:
    """Stand-in for homeassistant.helpers.storage.Store."""

    def __init__(self, data=None):
        self.data = data

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = data


CHARGER_CONFIG = {
    CONF_CHARGING_STATUS_ENTITY: "binary_sensor.car_charging",
    CONF_ENERGY_METER_ENTITY: "sensor.wallbox_energy",
}


async def run_session(tracker, hass, start="100.0", end="110.0"):
    hass.states.set("sensor.wallbox_energy", start)
    await tracker._async_on_charging_status_changed(create_state_event("off", "on"))
    hass.states.set("sensor.wallbox_energy", end)
    await tracker._async_on_charging_status_changed(create_state_event("on", "off"))
    await tracker._async_handle_debounce_expired(None)


@pytest.mark.asyncio
async def test_running_session_survives_a_restart(mock_hass, mock_client):
    store = MemoryStore()
    mock_hass.states.set("sensor.wallbox_energy", "100.0")
    mock_hass.states.set("binary_sensor.car_charging", "on")
    first = AutoLedgerChargerTracker(mock_hass, mock_client, "v-1", CHARGER_CONFIG, store=store)
    await first._async_on_charging_status_changed(create_state_event("off", "on"))
    started_at = first.session_start_time

    second = AutoLedgerChargerTracker(mock_hass, mock_client, "v-1", CHARGER_CONFIG, store=store)
    await second.async_setup()

    assert second.state == STATE_CHARGING
    assert second.session_start_time == started_at
    assert second.energy_start == 100.0

    mock_hass.states.set("sensor.wallbox_energy", "112.5")
    await second._async_on_charging_status_changed(create_state_event("on", "off"))
    await second._async_handle_debounce_expired(None)

    payload = mock_client.async_post_event.call_args[0][0]
    assert payload["data"]["energy_added_kwh"] == 12.5
    assert payload["data"]["start_time"] == started_at
    assert store.data["state"] == STATE_IDLE


@pytest.mark.asyncio
async def test_session_ended_while_home_assistant_was_down_is_finalized(mock_hass, mock_client):
    store = MemoryStore()
    mock_hass.states.set("sensor.wallbox_energy", "100.0")
    first = AutoLedgerChargerTracker(mock_hass, mock_client, "v-1", CHARGER_CONFIG, store=store)
    await first._async_on_charging_status_changed(create_state_event("off", "on"))

    mock_hass.states.set("sensor.wallbox_energy", "108.0")
    mock_hass.states.set("binary_sensor.car_charging", "off")
    second = AutoLedgerChargerTracker(mock_hass, mock_client, "v-1", CHARGER_CONFIG, store=store)
    await second.async_setup()

    assert second.state == STATE_COOLING_DOWN
    await second._async_handle_debounce_expired(None)
    assert mock_client.async_post_event.call_args[0][0]["data"]["energy_added_kwh"] == 8.0


@pytest.mark.asyncio
async def test_unreachable_server_queues_the_session_then_resends_it(mock_hass, mock_client):
    store = MemoryStore()
    mock_client.async_post_event.side_effect = AutoLedgerConnectionError("down")
    tracker = AutoLedgerChargerTracker(mock_hass, mock_client, "v-1", CHARGER_CONFIG, store=store)
    await run_session(tracker, mock_hass)

    assert tracker.sync_status == SYNC_STATUS_ERROR
    assert tracker.pending_events_count == 1
    queued = store.data["retry_queue"][0]

    mock_client.async_post_event.side_effect = None
    await tracker._async_retry_tick(None)

    assert tracker.sync_status == SYNC_STATUS_OK
    assert tracker.pending_events_count == 0
    assert store.data["retry_queue"] == []
    assert mock_client.async_post_event.call_args[0][0] == queued
    assert queued["event_id"].startswith("v-1:")


@pytest.mark.asyncio
async def test_queue_survives_a_restart(mock_hass, mock_client):
    store = MemoryStore()
    mock_client.async_post_event.side_effect = AutoLedgerApiError("boom", status_code=503)
    first = AutoLedgerChargerTracker(mock_hass, mock_client, "v-1", CHARGER_CONFIG, store=store)
    await run_session(first, mock_hass)

    mock_client.async_post_event.side_effect = None
    second = AutoLedgerChargerTracker(mock_hass, mock_client, "v-1", CHARGER_CONFIG, store=store)
    await second.async_setup()

    assert second.pending_events_count == 0
    assert store.data["retry_queue"] == []
    assert mock_client.async_post_event.call_count == 2


@pytest.mark.asyncio
async def test_server_refusal_is_not_retried(mock_hass, mock_client):
    store = MemoryStore()
    mock_client.async_post_event.side_effect = AutoLedgerApiError("bad", status_code=400)
    tracker = AutoLedgerChargerTracker(mock_hass, mock_client, "v-1", CHARGER_CONFIG, store=store)
    await run_session(tracker, mock_hass)

    assert tracker.sync_status == SYNC_STATUS_ERROR
    assert tracker.pending_events_count == 0
    assert store.data["retry_queue"] == []


@pytest.mark.asyncio
async def test_flush_stops_at_the_first_failure_and_keeps_order(mock_hass, mock_client):
    tracker = AutoLedgerChargerTracker(mock_hass, mock_client, "v-1", CHARGER_CONFIG)
    tracker._retry_queue = [{"event_id": "a"}, {"event_id": "b"}]
    mock_client.async_post_event.side_effect = [None, AutoLedgerConnectionError("down")]

    await tracker.async_flush_retry_queue()

    assert [q["event_id"] for q in tracker._retry_queue] == ["b"]
