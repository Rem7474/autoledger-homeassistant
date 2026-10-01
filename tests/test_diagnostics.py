"""Tests for AutoLedger diagnostics support."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.config_entries import ConfigEntry

from custom_components.autoledger.const import (
    CONF_API_KEY,
    CONF_HOST,
    CONF_VEHICLE_NAME,
    CONF_VEHICLES,
    DOMAIN,
)
from custom_components.autoledger.diagnostics import async_get_config_entry_diagnostics
from custom_components.autoledger.session_tracker import AutoLedgerSessionTracker


@pytest.mark.asyncio
async def test_diagnostics_redacts_api_key(mock_hass):
    """Test diagnostics export redacts sensitive credentials."""
    vehicle_id = "v-diag-1"
    entry = ConfigEntry(
        entry_id="entry_diag_test",
        data={
            CONF_HOST: "https://my.autoledger.net",
            CONF_API_KEY: "super_secret_jwt_token",
        },
        options={CONF_VEHICLES: {vehicle_id: {CONF_VEHICLE_NAME: "Model 3"}}},
    )

    coordinator = MagicMock()
    coordinator.data = {"vehicles": {}, "metrics": {}}

    tracker = AutoLedgerSessionTracker(mock_hass, MagicMock(), vehicle_id, {})

    entry.runtime_data = {
        "coordinator": coordinator,
        "trackers": {vehicle_id: tracker},
    }
    mock_hass.data[DOMAIN] = {entry.entry_id: entry.runtime_data}

    diag = await async_get_config_entry_diagnostics(mock_hass, entry)

    assert diag["entry"]["data"][CONF_HOST] == "https://my.autoledger.net"
    assert diag["entry"]["data"][CONF_API_KEY] == "***REDACTED***"
    assert vehicle_id in diag["trackers"]
    assert diag["trackers"][vehicle_id]["state"] == tracker.state
