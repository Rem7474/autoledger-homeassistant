"""Tests for AutoLedger custom services."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.autoledger.const import DOMAIN
from custom_components.autoledger.services import (
    SERVICE_SUBMIT_CHARGE,
    SERVICE_SYNC,
    async_setup_services,
    async_unload_services,
)


@pytest.mark.asyncio
async def test_services_registration_and_calls(mock_hass):
    """Test registering and triggering autoledger services."""
    await async_setup_services(mock_hass)

    assert mock_hass.services.has_service(DOMAIN, SERVICE_SYNC)
    assert mock_hass.services.has_service(DOMAIN, SERVICE_SUBMIT_CHARGE)
    assert mock_hass.services.has_service(DOMAIN, "sync_odometer")
    assert mock_hass.services.has_service(DOMAIN, "log_trip")

    # Mock coordinator, tracker, odometer_tracker, and client
    coordinator = MagicMock()
    coordinator.async_request_refresh = AsyncMock()

    tracker = MagicMock()
    tracker.async_submit_manual_charge = AsyncMock(return_value={"status": "ok"})

    odometer_tracker = MagicMock()
    odometer_tracker.vehicles_config = {"v-service-1": {}}
    odometer_tracker.async_sync_vehicle = AsyncMock(return_value=True)

    client = MagicMock()
    client.async_submit_charge = AsyncMock(return_value={"status": "ok"})

    mock_hass.data[DOMAIN] = {
        "entry_1": {
            "coordinator": coordinator,
            "trackers": {"v-service-1": tracker},
            "odometer_tracker": odometer_tracker,
            "client": client,
        }
    }

    # Test sync service
    sync_handler = mock_hass.services._services[(DOMAIN, SERVICE_SYNC)]
    call_sync = MagicMock()
    call_sync.data = {}
    await sync_handler(call_sync)
    assert coordinator.async_request_refresh.call_count == 1

    # Test sync_odometer service
    sync_odo_handler = mock_hass.services._services[(DOMAIN, "sync_odometer")]
    call_sync_odo = MagicMock()
    call_sync_odo.data = {"vehicle_id": "v-service-1"}
    await sync_odo_handler(call_sync_odo)
    assert odometer_tracker.async_sync_vehicle.call_count == 1
    assert odometer_tracker.async_sync_vehicle.call_args[0][0] == "v-service-1"

    # Test submit_charge service with tracked vehicle
    submit_handler = mock_hass.services._services[(DOMAIN, SERVICE_SUBMIT_CHARGE)]
    call_submit = MagicMock()
    call_submit.data = {
        "vehicle_id": "v-service-1",
        "energy_kwh": 35.5,
        "cost": 8.50,
        "odometer_km": 52000.0,
        "soc_start": 15,
        "soc_end": 85,
        "location": "home",
    }
    await submit_handler(call_submit)
    assert tracker.async_submit_manual_charge.call_count == 1
    assert tracker.async_submit_manual_charge.call_args[1]["kwh"] == 35.5

    # Test submit_charge service with unassigned vehicle (vehicle_id=None)
    call_unassigned = MagicMock()
    call_unassigned.data = {
        "vehicle_id": None,
        "energy_kwh": 22.0,
        "location": "home",
    }
    await submit_handler(call_unassigned)
    assert client.async_submit_charge.call_count == 1
    assert client.async_submit_charge.call_args[1]["vehicle_id"] is None
    assert client.async_submit_charge.call_args[1]["kwh"] == 22.0

    # Test unload
    mock_hass.data[DOMAIN].clear()
    await async_unload_services(mock_hass)
    assert not mock_hass.services.has_service(DOMAIN, SERVICE_SYNC)
    assert not mock_hass.services.has_service(DOMAIN, SERVICE_SUBMIT_CHARGE)
    assert not mock_hass.services.has_service(DOMAIN, "sync_odometer")
    assert not mock_hass.services.has_service(DOMAIN, "log_trip")


@pytest.mark.asyncio
async def test_log_trip_service(mock_hass):
    from datetime import UTC, datetime

    await async_setup_services(mock_hass)
    handler = mock_hass.services._services[(DOMAIN, "log_trip")]
    call = MagicMock()
    start = datetime(2026, 1, 1, 8, tzinfo=UTC)
    end = datetime(2026, 1, 1, 9, tzinfo=UTC)
    call.data = {
        "vehicle_id": "v1",
        "start_time": start,
        "end_time": end,
        "start_latitude": 45.0,
        "start_longitude": 6.0,
        "end_latitude": 46.0,
        "start_odometer_km": 10.0,
    }

    # No entry yet: nothing to call, no crash
    await handler(call)

    tracker = MagicMock(async_log_trip=AsyncMock(return_value=True))
    mock_hass.data[DOMAIN] = {"e1": {"trip_tracker": tracker}}
    await handler(call)
    kwargs = tracker.async_log_trip.call_args.kwargs
    assert kwargs["start"] == (45.0, 6.0)
    assert kwargs["end"] is None  # a latitude alone is not a position
    assert kwargs["start_odometer_km"] == 10.0

    call.data = {**call.data, "start_time": end, "end_time": start}
    tracker.async_log_trip.reset_mock()
    await handler(call)
    tracker.async_log_trip.assert_not_called()
