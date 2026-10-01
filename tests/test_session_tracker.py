"""Tests for AutoLedgerSessionTracker and solar anti-bounce debounce engine."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.autoledger.const import (
    CONF_BATTERY_SOC_ENTITY,
    CONF_CHARGING_LOCATION,
    CONF_CHARGING_STATUS_ENTITY,
    CONF_DEBOUNCE_SECONDS,
    CONF_ENERGY_METER_ENTITY,
    CONF_ENERGY_METER_TYPE,
    CONF_ODOMETER_ENTITY,
    ENERGY_METER_TYPE_SESSION,
    ENERGY_METER_TYPE_TOTAL_INCREASING,
    STATE_CHARGING,
    STATE_COOLING_DOWN,
    STATE_IDLE,
    SYNC_STATUS_ERROR,
    SYNC_STATUS_OK,
)
from custom_components.autoledger.session_tracker import AutoLedgerSessionTracker


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

    tracker = AutoLedgerSessionTracker(
        hass=mock_hass,
        client=mock_client,
        vehicle_id="v-1",
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
    tracker = AutoLedgerSessionTracker(mock_hass, mock_client, "v-1", config)

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
        CONF_CHARGING_STATUS_ENTITY: "binary_sensor.car_charging",
        CONF_BATTERY_SOC_ENTITY: "sensor.car_battery",
        CONF_ODOMETER_ENTITY: "sensor.car_odometer",
        CONF_ENERGY_METER_ENTITY: "sensor.wallbox_energy",
        CONF_ENERGY_METER_TYPE: ENERGY_METER_TYPE_TOTAL_INCREASING,
        CONF_CHARGING_LOCATION: "home",
        CONF_DEBOUNCE_SECONDS: 60,
    }
    tracker = AutoLedgerSessionTracker(mock_hass, mock_client, "v-1", config)

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
    assert mock_client.async_post_event.call_count == 1

    payload = mock_client.async_post_event.call_args[0][0]
    assert payload["vehicle_id"] == "v-1"
    assert payload["event_type"] == "charging_session_end"
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
    tracker = AutoLedgerSessionTracker(mock_hass, mock_client, "v-1", config)

    await tracker._async_on_charging_status_changed(create_state_event("off", "on"))
    await tracker._async_on_charging_status_changed(create_state_event("on", "off"))
    await tracker._async_handle_debounce_expired(None)

    payload = mock_client.async_post_event.call_args[0][0]
    assert payload["data"]["energy_added_kwh"] == 15.8


@pytest.mark.asyncio
async def test_session_error_handling(mock_hass, mock_client):
    """Test sync status is set to error if post_event raises an exception."""
    mock_client.async_post_event.side_effect = RuntimeError("Server Down")

    config = {
        CONF_CHARGING_STATUS_ENTITY: "binary_sensor.car_charging",
    }
    tracker = AutoLedgerSessionTracker(mock_hass, mock_client, "v-1", config)

    await tracker._async_on_charging_status_changed(create_state_event("off", "on"))
    await tracker._async_on_charging_status_changed(create_state_event("on", "off"))
    await tracker._async_handle_debounce_expired(None)

    assert tracker.sync_status == SYNC_STATUS_ERROR
    assert "Server Down" in tracker.last_error_message


@pytest.mark.asyncio
async def test_manual_charge_submission(mock_hass, mock_client):
    """Test manual charge submission helper."""
    config = {CONF_CHARGING_LOCATION: "office"}
    tracker = AutoLedgerSessionTracker(mock_hass, mock_client, "v-1", config)

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
    tracker = AutoLedgerSessionTracker(
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
