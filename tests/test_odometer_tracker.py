"""Tests for AutoLedgerOdometerTracker and trip-end stabilization."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import Event, HomeAssistant

from custom_components.autoledger.const import (
    CONF_ODOMETER_ENTITY,
    CONF_VEHICLE_ID,
    CONF_VEHICLE_NAME,
)
from custom_components.autoledger.odometer_tracker import (
    AutoLedgerOdometerTracker,
    get_entity_odometer_km,
)
from tests.conftest import MockState as State


@pytest.fixture
def mock_hass():
    """Mock HomeAssistant instance."""
    hass = MagicMock(spec=HomeAssistant)
    hass.states = MagicMock()
    tasks = []

    def fake_create_task(coro):
        task = MagicMock()
        tasks.append(coro)
        return task

    hass.async_create_task = MagicMock(side_effect=fake_create_task)
    hass._created_tasks = tasks
    return hass


@pytest.fixture
def mock_client():
    """Mock AutoLedgerApiClient."""
    client = MagicMock()
    client.async_update_odometer = AsyncMock(return_value={"status": "recorded"})
    return client


def test_get_entity_odometer_km(mock_hass):
    """Test get_entity_odometer_km with various units and edge cases."""
    # None entity
    assert get_entity_odometer_km(mock_hass, None) is None

    # Unknown / unavailable
    mock_hass.states.get.return_value = State("sensor.car_odo", "unknown")
    assert get_entity_odometer_km(mock_hass, "sensor.car_odo") is None

    mock_hass.states.get.return_value = State("sensor.car_odo", "unavailable")
    assert get_entity_odometer_km(mock_hass, "sensor.car_odo") is None

    # Invalid string
    mock_hass.states.get.return_value = State("sensor.car_odo", "foo")
    assert get_entity_odometer_km(mock_hass, "sensor.car_odo") is None

    # Negative number
    mock_hass.states.get.return_value = State("sensor.car_odo", "-10")
    assert get_entity_odometer_km(mock_hass, "sensor.car_odo") is None

    # km unit (standard)
    mock_hass.states.get.return_value = State(
        "sensor.car_odo", "45200.6", attributes={"unit_of_measurement": "km"}
    )
    assert get_entity_odometer_km(mock_hass, "sensor.car_odo") == 45200.6

    # mi unit (miles converted to km)
    mock_hass.states.get.return_value = State(
        "sensor.car_odo", "100.0", attributes={"unit_of_measurement": "mi"}
    )
    # 100 * 1.609344 = 160.9344 -> 160.9
    assert get_entity_odometer_km(mock_hass, "sensor.car_odo") == 160.9

    # m unit (meters converted to km)
    mock_hass.states.get.return_value = State(
        "sensor.car_odo", "50000", attributes={"unit_of_measurement": "m"}
    )
    assert get_entity_odometer_km(mock_hass, "sensor.car_odo") == 50.0


@pytest.mark.asyncio
async def test_odometer_tracker_initial_sync(mock_hass, mock_client):
    """Test initial sync upon setup when sensor is higher than backend."""
    vehicles_config = {
        "veh-1": {
            CONF_VEHICLE_ID: "veh-1",
            CONF_VEHICLE_NAME: "Model 3",
            CONF_ODOMETER_ENTITY: "sensor.car_odometer",
        }
    }

    mock_coordinator = MagicMock()
    mock_coordinator.data = {"vehicles": {"veh-1": {"id": "veh-1", "current_odometer": 40000.0}}}

    # Case 1: HA sensor shows 45000.0 km > 40000.0 km -> syncs immediately
    mock_hass.states.get.return_value = State(
        "sensor.car_odometer", "45000.0", attributes={"unit_of_measurement": "km"}
    )

    with patch("custom_components.autoledger.odometer_tracker.async_track_state_change_event"):
        tracker = AutoLedgerOdometerTracker(
            hass=mock_hass,
            client=mock_client,
            vehicles_config=vehicles_config,
            coordinator=mock_coordinator,
            debounce_seconds=300,
        )
        await tracker.async_setup()

    mock_client.async_update_odometer.assert_awaited_once_with(
        vehicle_id="veh-1",
        odometer_km=45000.0,
    )
    assert tracker._last_synced_odometer["veh-1"] == 45000.0


@pytest.mark.asyncio
async def test_odometer_tracker_no_initial_sync_if_already_up_to_date(mock_hass, mock_client):
    """Test no initial sync if backend is already equal or newer."""
    vehicles_config = {
        "veh-1": {
            CONF_VEHICLE_ID: "veh-1",
            CONF_VEHICLE_NAME: "Model 3",
            CONF_ODOMETER_ENTITY: "sensor.car_odometer",
        }
    }

    mock_coordinator = MagicMock()
    mock_coordinator.data = {"vehicles": {"veh-1": {"id": "veh-1", "current_odometer": 50000.0}}}

    # HA sensor matches backend
    mock_hass.states.get.return_value = State(
        "sensor.car_odometer", "50000.0", attributes={"unit_of_measurement": "km"}
    )

    with patch("custom_components.autoledger.odometer_tracker.async_track_state_change_event"):
        tracker = AutoLedgerOdometerTracker(
            hass=mock_hass,
            client=mock_client,
            vehicles_config=vehicles_config,
            coordinator=mock_coordinator,
            debounce_seconds=300,
        )
        await tracker.async_setup()

    mock_client.async_update_odometer.assert_not_called()


@pytest.mark.asyncio
async def test_odometer_tracker_trip_end_debounce(mock_hass, mock_client):
    """Test trip-end debounce prevents requests while driving and sends 1 request upon parking."""
    vehicles_config = {
        "veh-1": {
            CONF_VEHICLE_ID: "veh-1",
            CONF_VEHICLE_NAME: "Model 3",
            CONF_ODOMETER_ENTITY: "sensor.car_odometer",
        }
    }

    mock_coordinator = MagicMock()
    mock_coordinator.async_request_refresh = AsyncMock()
    mock_coordinator.data = {"vehicles": {"veh-1": {"id": "veh-1", "current_odometer": 50000.0}}}

    mock_hass.states.get.return_value = State(
        "sensor.car_odometer", "50000.0", attributes={"unit_of_measurement": "km"}
    )

    tracked_callback = None

    def fake_track(hass, entities, callback):
        nonlocal tracked_callback
        tracked_callback = callback
        return MagicMock()

    scheduled_timer_callback = None
    timer_unsub = MagicMock()

    def fake_call_later(hass, delay, callback):
        nonlocal scheduled_timer_callback
        scheduled_timer_callback = callback
        return timer_unsub

    with (
        patch(
            "custom_components.autoledger.odometer_tracker.async_track_state_change_event",
            side_effect=fake_track,
        ),
        patch(
            "custom_components.autoledger.odometer_tracker.async_call_later",
            side_effect=fake_call_later,
        ),
    ):
        tracker = AutoLedgerOdometerTracker(
            hass=mock_hass,
            client=mock_client,
            vehicles_config=vehicles_config,
            coordinator=mock_coordinator,
            debounce_seconds=300,
        )
        await tracker.async_setup()

        assert tracker._last_synced_odometer["veh-1"] == 50000.0
        mock_client.async_update_odometer.assert_not_called()

        # Step 1: Vehicle starts driving, odometer changes to 50010 km
        mock_hass.states.get.return_value = State(
            "sensor.car_odometer", "50010.0", attributes={"unit_of_measurement": "km"}
        )
        event1 = MagicMock(spec=Event)
        event1.data = {"entity_id": "sensor.car_odometer"}
        await tracker._async_on_odometer_changed(event1)

        # NO API call should have been made! Only a timer should have been scheduled
        mock_client.async_update_odometer.assert_not_called()
        assert scheduled_timer_callback is not None

        # Step 2: Vehicle continues driving, odometer changes to 50025 km
        mock_hass.states.get.return_value = State(
            "sensor.car_odometer", "50025.0", attributes={"unit_of_measurement": "km"}
        )
        event2 = MagicMock(spec=Event)
        event2.data = {"entity_id": "sensor.car_odometer"}
        await tracker._async_on_odometer_changed(event2)

        # Previous timer was canceled, new timer scheduled, STILL NO API call
        timer_unsub.assert_called_once()
        mock_client.async_update_odometer.assert_not_called()

        # Step 3: Vehicle parks. Timer expires (5 minutes of silence).
        mock_hass.states.get.return_value = State(
            "sensor.car_odometer", "50025.0", attributes={"unit_of_measurement": "km"}
        )
        # Simulate timer firing
        scheduled_timer_callback(None)
        # The callback spawns an async task on mock_hass
        assert len(mock_hass._created_tasks) == 1
        # Await the created task
        await mock_hass._created_tasks[0]

        # Exactly 1 single API call made with the final trip odometer!
        mock_client.async_update_odometer.assert_awaited_once_with(
            vehicle_id="veh-1",
            odometer_km=50025.0,
        )
        assert tracker._last_synced_odometer["veh-1"] == 50025.0
        mock_coordinator.async_request_refresh.assert_awaited_once()
