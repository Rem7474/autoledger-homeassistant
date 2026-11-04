"""An access that can only read: nothing is sent, and a repair issue says why."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.autoledger.const import (
    CONF_API_KEY,
    CONF_CHARGER_ID,
    CONF_CHARGER_NAME,
    CONF_CHARGING_STATUS_ENTITY,
    CONF_HOST,
    CONF_ODOMETER_ENTITY,
    CONF_VEHICLE_ID,
    CONF_VEHICLE_NAME,
    CONF_VERIFY_SSL,
    DOMAIN,
)
from custom_components.autoledger.subentries import SUBENTRY_CHARGER, SUBENTRY_VEHICLE

API = "custom_components.autoledger.api.AutoLedgerApiClient"
HOST = "http://autoledger.local:8080"


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=HOST,
        version=2,
        data={CONF_HOST: HOST, CONF_API_KEY: "k", CONF_VERIFY_SSL: True},
        subentries_data=[
            ConfigSubentryData(
                data={
                    CONF_VEHICLE_ID: "v1",
                    CONF_VEHICLE_NAME: "Family",
                    CONF_ODOMETER_ENTITY: "sensor.odometer",
                },
                subentry_type=SUBENTRY_VEHICLE,
                title="Family",
                unique_id="vehicle:v1",
            ),
            ConfigSubentryData(
                data={
                    CONF_CHARGER_ID: "wallbox",
                    CONF_CHARGER_NAME: "Wallbox",
                    CONF_CHARGING_STATUS_ENTITY: "binary_sensor.wallbox",
                },
                subentry_type=SUBENTRY_CHARGER,
                title="Wallbox",
                unique_id="charger:wallbox",
            ),
        ],
    )


async def _setup(hass: HomeAssistant, scope: str) -> MockConfigEntry:
    entry = _entry()
    entry.add_to_hass(hass)
    with (
        patch(f"{API}.async_get_vehicles", new=AsyncMock(return_value=[{"id": "v1"}])),
        patch(f"{API}.async_get_vehicle_metrics", new=AsyncMock(return_value={})),
        patch(f"{API}.async_get_account", new=AsyncMock(return_value={"scope": scope})),
        patch(f"{API}.async_get_fleet_reminders", new=AsyncMock(return_value={"vehicles": []})),
        patch(f"{API}.async_get_monthly_costs", new=AsyncMock(return_value={"months": []})),
        patch(f"{API}.async_get_summary", new=AsyncMock(return_value={"vehicles": []})),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_a_read_only_access_tracks_and_sends_nothing(hass: HomeAssistant) -> None:
    entry = await _setup(hass, "read")
    data = hass.data[DOMAIN][entry.entry_id]

    assert data["read_only"] is True
    assert data["trackers"] == {}
    assert data["odometer_tracker"] is None
    assert data["trip_tracker"] is None
    assert hass.states.get("sensor.wallbox_charging_state") is None
    assert hass.states.get("button.family_send_odometer") is None
    # the figures it can read are still there
    assert hass.states.get("sensor.family_cost_this_month") is not None

    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"read_only_access_{entry.entry_id}")
    assert issue is not None
    assert issue.translation_key == "read_only_access"


async def test_submit_charge_explains_why_it_cannot_send(hass: HomeAssistant) -> None:
    await _setup(hass, "read")

    with pytest.raises(HomeAssistantError, match="read-only"):
        await hass.services.async_call(DOMAIN, "submit_charge", {"energy_kwh": 10}, blocking=True)


async def test_a_token_that_can_send_has_trackers_and_no_issue(hass: HomeAssistant) -> None:
    entry = await _setup(hass, "homeassistant")
    data = hass.data[DOMAIN][entry.entry_id]

    assert data["read_only"] is False
    assert "wallbox" in data["trackers"]
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"read_only_access_{entry.entry_id}") is None
