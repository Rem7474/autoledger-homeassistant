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
    client.async_get_account = AsyncMock(return_value={"scope": "ingest"})
    client.async_get_fleet_reminders = AsyncMock(return_value={"vehicles": []})
    client.async_get_monthly_costs = AsyncMock(return_value={"months": []})
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


@pytest.mark.asyncio
async def test_ingest_token_never_calls_the_read_api(mock_hass):
    """A token that only pushes cannot read the figures: no read call, no read sensors."""
    client = _client()

    data = await _coordinator(mock_hass, client)._async_update_data()

    assert data["read_access"] is False
    assert "reminders" not in data
    client.async_get_fleet_reminders.assert_not_awaited()
    client.async_get_monthly_costs.assert_not_awaited()


@pytest.mark.asyncio
async def test_homeassistant_token_reads_reminders_and_the_month_in_progress(mock_hass):
    client = _client(
        async_get_account=AsyncMock(return_value={"scope": "homeassistant", "pending_charges": 2}),
        async_get_fleet_reminders=AsyncMock(
            return_value={"vehicles": [{"vehicle_id": "v1", "overdue": 1}, {"overdue": 9}]}
        ),
        async_get_monthly_costs=AsyncMock(
            return_value={"months": [{"month": "2026-10"}, {"month": "2026-11"}]}
        ),
    )

    data = await _coordinator(mock_hass, client)._async_update_data()

    assert data["read_access"] is True
    assert data["account"]["pending_charges"] == 2
    assert data["reminders"] == {"v1": {"vehicle_id": "v1", "overdue": 1}}
    assert data["monthly"] == {"v1": {"month": "2026-11"}}
    client.async_get_monthly_costs.assert_awaited_once_with("v1")


@pytest.mark.asyncio
async def test_transient_read_error_keeps_the_previous_figures(mock_hass):
    client = _client(
        async_get_account=AsyncMock(return_value={"scope": "homeassistant"}),
        async_get_fleet_reminders=AsyncMock(
            return_value={"vehicles": [{"vehicle_id": "v1", "overdue": 1}]}
        ),
    )
    coordinator = _coordinator(mock_hass, client)
    await coordinator._async_update_data()

    client.async_get_fleet_reminders.side_effect = AutoLedgerConnectionError("down")
    data = await coordinator._async_update_data()

    assert data["read_access"] is True
    assert data["reminders"]["v1"]["overdue"] == 1


@pytest.mark.asyncio
async def test_monthly_error_of_one_vehicle_does_not_hide_the_reminders(mock_hass):
    client = _client(
        async_get_account=AsyncMock(return_value={"scope": "read"}),
        async_get_fleet_reminders=AsyncMock(
            return_value={"vehicles": [{"vehicle_id": "v1", "overdue": 0}]}
        ),
        async_get_monthly_costs=AsyncMock(side_effect=AutoLedgerApiError("boom")),
    )

    data = await _coordinator(mock_hass, client)._async_update_data()

    assert data["reminders"]["v1"]["overdue"] == 0
    assert data["monthly"] == {}


@pytest.mark.asyncio
async def test_token_revoked_while_reading_starts_reauthentication(mock_hass):
    client = _client(
        async_get_account=AsyncMock(return_value={"scope": "homeassistant"}),
        async_get_fleet_reminders=AsyncMock(side_effect=AutoLedgerAuthError("revoked")),
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await _coordinator(mock_hass, client)._async_update_data()
