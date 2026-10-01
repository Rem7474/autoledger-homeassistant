"""Tests for AutoLedger sensor platform."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.config_entries import ConfigEntry

from custom_components.autoledger.const import (
    ATTR_CURRENCY,
    ATTR_DATE,
    ATTR_DEBOUNCE_ACTIVE,
    ATTR_DURATION_MINUTES,
    ATTR_ENERGY_KWH,
    ATTR_LAST_ERROR_MESSAGE,
    CONF_HOST,
    CONF_VEHICLE_NAME,
    CONF_VEHICLES,
    DOMAIN,
    STATE_COOLING_DOWN,
    STATE_IDLE,
    SYNC_STATUS_ERROR,
    SYNC_STATUS_OK,
)
from custom_components.autoledger.sensor import (
    AutoLedgerChargingStateSensor,
    AutoLedgerCostPer100KmSensor,
    AutoLedgerLastChargeCostSensor,
    AutoLedgerSyncStatusSensor,
    async_setup_entry,
)
from custom_components.autoledger.session_tracker import AutoLedgerSessionTracker


@pytest.mark.asyncio
async def test_sensors_setup_entry(mock_hass):
    """Test full sensor platform setup for configured vehicles."""
    vehicle_id = "v-test-1"
    entry = ConfigEntry(
        entry_id="entry_sensor_test",
        data={CONF_HOST: "http://autoledger.local"},
        options={
            CONF_VEHICLES: {
                vehicle_id: {
                    CONF_VEHICLE_NAME: "Tesla Model Y",
                }
            }
        },
    )

    coordinator = MagicMock()
    coordinator.data = {
        "vehicles": {vehicle_id: {"make": "Tesla", "model": "Model Y"}},
        "metrics": {
            vehicle_id: {
                "last_charge_cost": 11.20,
                "cost_per_100km": 3.75,
                "currency": "EUR",
                "energy_kwh": 40.5,
                "duration_minutes": 240,
                "date": "2026-10-01T08:00:00Z",
            }
        },
    }

    client = MagicMock()
    tracker = AutoLedgerSessionTracker(mock_hass, client, vehicle_id, {})
    tracker.sync_status = SYNC_STATUS_OK
    tracker.state = STATE_IDLE

    entry.runtime_data = {
        "coordinator": coordinator,
        "trackers": {vehicle_id: tracker},
    }
    mock_hass.data[DOMAIN] = {entry.entry_id: entry.runtime_data}

    created_entities = []

    def add_entities(entities):
        created_entities.extend(entities)

    await async_setup_entry(mock_hass, entry, add_entities)

    assert len(created_entities) == 4

    cost_sensor = next(e for e in created_entities if isinstance(e, AutoLedgerLastChargeCostSensor))
    efficiency_sensor = next(
        e for e in created_entities if isinstance(e, AutoLedgerCostPer100KmSensor)
    )
    sync_sensor = next(e for e in created_entities if isinstance(e, AutoLedgerSyncStatusSensor))
    state_sensor = next(e for e in created_entities if isinstance(e, AutoLedgerChargingStateSensor))

    # Test Cost Sensor
    assert cost_sensor.native_value == 11.20
    assert cost_sensor.native_unit_of_measurement == "EUR"
    attrs = cost_sensor.extra_state_attributes
    assert attrs[ATTR_ENERGY_KWH] == 40.5
    assert attrs[ATTR_DURATION_MINUTES] == 240
    assert attrs[ATTR_DATE] == "2026-10-01T08:00:00Z"
    assert attrs[ATTR_CURRENCY] == "EUR"

    # Test Efficiency Sensor
    assert efficiency_sensor.native_value == 3.75
    assert efficiency_sensor.native_unit_of_measurement == "EUR/100km"

    # Test Sync Sensor
    assert sync_sensor.native_value == SYNC_STATUS_OK

    # Test Charging State Sensor
    assert state_sensor.native_value == STATE_IDLE
    assert state_sensor.extra_state_attributes[ATTR_DEBOUNCE_ACTIVE] is False


@pytest.mark.asyncio
async def test_sensor_tracker_live_updates(mock_hass):
    """Test that tracker sensors dynamically reflect tracker state changes."""
    vehicle_id = "v-test-2"
    entry = ConfigEntry(
        entry_id="entry_live_test",
        data={CONF_HOST: "http://autoledger.local"},
        options={},
    )
    client = MagicMock()
    tracker = AutoLedgerSessionTracker(mock_hass, client, vehicle_id, {})

    sync_sensor = AutoLedgerSyncStatusSensor(
        entry=entry,
        tracker=tracker,
        vehicle_id=vehicle_id,
        vehicle_name="Car 2",
    )
    state_sensor = AutoLedgerChargingStateSensor(
        entry=entry,
        tracker=tracker,
        vehicle_id=vehicle_id,
        vehicle_name="Car 2",
    )

    # Attach listeners
    await sync_sensor.async_added_to_hass()
    await state_sensor.async_added_to_hass()

    assert sync_sensor.native_value == SYNC_STATUS_OK
    assert state_sensor.native_value == STATE_IDLE

    # Update tracker state
    tracker.state = STATE_COOLING_DOWN
    tracker.sync_status = SYNC_STATUS_ERROR
    tracker.last_error_message = "Connection refused"
    tracker._notify_listeners()

    assert state_sensor.native_value == STATE_COOLING_DOWN
    assert state_sensor.extra_state_attributes[ATTR_DEBOUNCE_ACTIVE] is True

    assert sync_sensor.native_value == SYNC_STATUS_ERROR
    assert sync_sensor.extra_state_attributes[ATTR_LAST_ERROR_MESSAGE] == "Connection refused"

    # Cleanup
    await sync_sensor.async_will_remove_from_hass()
    await state_sensor.async_will_remove_from_hass()


@pytest.mark.asyncio
async def test_decoupled_sensors_setup_entry(mock_hass):
    """Test decoupled setup with vehicle metrics sensors and charger sensors."""
    from custom_components.autoledger.const import CONF_CHARGER_NAME, CONF_CHARGERS
    from custom_components.autoledger.sensor import (
        AutoLedgerChargerChargingStateSensor,
        AutoLedgerChargerLastEnergySensor,
        AutoLedgerChargerSyncStatusSensor,
    )
    from custom_components.autoledger.session_tracker import AutoLedgerChargerTracker

    vehicle_id = "v-tesla"
    charger_id = "wb-garage"

    entry = ConfigEntry(
        entry_id="entry_decoupled_test",
        data={CONF_HOST: "http://autoledger.local"},
        options={
            CONF_VEHICLES: {
                vehicle_id: {CONF_VEHICLE_NAME: "Model Y"},
            },
            CONF_CHARGERS: {
                charger_id: {CONF_CHARGER_NAME: "Wallbox Garage"},
            },
        },
    )

    coordinator = MagicMock()
    coordinator.data = {
        "vehicles": {vehicle_id: {"make": "Tesla", "model": "Model Y"}},
        "metrics": {
            vehicle_id: {
                "last_charge_cost": 8.40,
                "cost_per_100km": 3.10,
                "currency": "EUR",
                "energy_kwh": 30.0,
                "duration_minutes": 180,
                "date": "2026-10-01T08:00:00Z",
            }
        },
    }

    client = MagicMock()
    tracker = AutoLedgerChargerTracker(
        mock_hass, client, charger_id=charger_id, config={CONF_CHARGER_NAME: "Wallbox Garage"}
    )
    tracker.state = STATE_IDLE
    tracker.sync_status = SYNC_STATUS_OK
    tracker.last_energy_kwh = 28.45

    entry.runtime_data = {
        "coordinator": coordinator,
        "trackers": {charger_id: tracker},
    }
    mock_hass.data[DOMAIN] = {entry.entry_id: entry.runtime_data}

    created_entities = []

    def add_entities(entities):
        created_entities.extend(entities)

    await async_setup_entry(mock_hass, entry, add_entities)

    # 2 vehicle sensors + 3 charger sensors = 5 sensors total
    assert len(created_entities) == 5

    cost_sensor = next(e for e in created_entities if isinstance(e, AutoLedgerLastChargeCostSensor))
    efficiency_sensor = next(
        e for e in created_entities if isinstance(e, AutoLedgerCostPer100KmSensor)
    )
    charger_state_sensor = next(
        e for e in created_entities if isinstance(e, AutoLedgerChargerChargingStateSensor)
    )
    charger_energy_sensor = next(
        e for e in created_entities if isinstance(e, AutoLedgerChargerLastEnergySensor)
    )
    charger_sync_sensor = next(
        e for e in created_entities if isinstance(e, AutoLedgerChargerSyncStatusSensor)
    )

    assert cost_sensor.native_value == 8.40
    assert efficiency_sensor.native_value == 3.10
    assert charger_state_sensor.native_value == STATE_IDLE
    assert charger_energy_sensor.native_value == 28.45
    assert charger_sync_sensor.native_value == SYNC_STATUS_OK
