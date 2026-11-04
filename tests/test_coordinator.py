"""Tests for the AutoLedger data update coordinator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.autoledger.api import (
    AutoLedgerApiError,
    AutoLedgerAuthError,
    AutoLedgerConnectionError,
)
from custom_components.autoledger.coordinator import AutoLedgerDataUpdateCoordinator


def _coordinator(mock_hass, client):
    return AutoLedgerDataUpdateCoordinator(
        hass=mock_hass, client=client, entry_id="entry", vehicles_config={"v1": {}}
    )


def _client(**overrides):
    client = MagicMock()
    client.async_get_vehicles = AsyncMock(return_value=[{"id": "v1", "name": "Car"}])
    client.async_get_vehicle_metrics = AsyncMock(return_value={"last_charge_cost": 4.2})
    for name, value in overrides.items():
        setattr(client, name, value)
    return client


@pytest.mark.asyncio
async def test_update_returns_vehicles_and_metrics(mock_hass):
    data = await _coordinator(mock_hass, _client())._async_update_data()

    assert data["vehicles"]["v1"]["name"] == "Car"
    assert data["metrics"]["v1"] == {"last_charge_cost": 4.2}


@pytest.mark.asyncio
async def test_rejected_token_starts_reauthentication(mock_hass):
    """A refused token must reach Home Assistant as ConfigEntryAuthFailed, not UpdateFailed."""
    client = _client(async_get_vehicles=AsyncMock(side_effect=AutoLedgerAuthError("revoked")))

    with pytest.raises(ConfigEntryAuthFailed):
        await _coordinator(mock_hass, client)._async_update_data()


@pytest.mark.asyncio
async def test_rejected_token_on_metrics_starts_reauthentication(mock_hass):
    """The per-vehicle fetch must not swallow an authentication failure."""
    client = _client(
        async_get_vehicle_metrics=AsyncMock(side_effect=AutoLedgerAuthError("revoked"))
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await _coordinator(mock_hass, client)._async_update_data()


@pytest.mark.asyncio
async def test_unreachable_server_is_a_transient_failure(mock_hass):
    client = _client(async_get_vehicles=AsyncMock(side_effect=AutoLedgerConnectionError("down")))

    with pytest.raises(UpdateFailed):
        await _coordinator(mock_hass, client)._async_update_data()


@pytest.mark.asyncio
async def test_metrics_error_for_one_vehicle_keeps_the_others(mock_hass):
    client = _client(async_get_vehicle_metrics=AsyncMock(side_effect=AutoLedgerApiError("boom")))

    data = await _coordinator(mock_hass, client)._async_update_data()

    assert data["metrics"]["v1"] == {}
