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

    # Mock coordinator and tracker
    coordinator = MagicMock()
    coordinator.async_request_refresh = AsyncMock()

    tracker = MagicMock()
    tracker.async_submit_manual_charge = AsyncMock(return_value={"status": "ok"})

    mock_hass.data[DOMAIN] = {
        "entry_1": {
            "coordinator": coordinator,
            "trackers": {"v-service-1": tracker},
        }
    }

    # Test sync service
    sync_handler = mock_hass.services._services[(DOMAIN, SERVICE_SYNC)]
    call_sync = MagicMock()
    call_sync.data = {}
    await sync_handler(call_sync)
    assert coordinator.async_request_refresh.call_count == 1

    # Test submit_charge service
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

    # Test unload
    mock_hass.data[DOMAIN].clear()
    await async_unload_services(mock_hass)
    assert not mock_hass.services.has_service(DOMAIN, SERVICE_SYNC)
    assert not mock_hass.services.has_service(DOMAIN, SERVICE_SUBMIT_CHARGE)
