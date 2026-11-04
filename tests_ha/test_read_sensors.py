"""Sensors fed by the read API, set up through the real Home Assistant core."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.autoledger.const import (
    CONF_API_KEY,
    CONF_HOST,
    CONF_VEHICLE_ID,
    CONF_VEHICLE_NAME,
    CONF_VEHICLES,
    CONF_VERIFY_SSL,
    DOMAIN,
)

API = "custom_components.autoledger.api.AutoLedgerApiClient"
HOST = "http://autoledger.local:8080"

VEHICLES = [{"id": "v1", "make": "Tesla", "model": "Model Y", "name": "Family"}]
FLEET_REMINDERS = {
    "overdue": 1,
    "due_soon": 1,
    "total": 3,
    "vehicles": [
        {
            "vehicle_id": "v1",
            "overdue": 1,
            "due_soon": 1,
            "total": 3,
            "reminders": [
                {
                    "title": "Technical inspection",
                    "category": "inspection",
                    "status": "overdue",
                    "due_date": "2026-10-20",
                    "remaining_days": -15,
                    "remaining_km": None,
                },
                {
                    "title": "Oil change",
                    "category": "maintenance",
                    "status": "due_soon",
                    "due_date": "2026-12-01",
                    "remaining_days": 27,
                    "remaining_km": {"value": 850, "unit": "km"},
                },
                {
                    "title": "Tires rotation",
                    "category": "tires",
                    "status": "ok",
                    "due_date": None,
                    "remaining_days": None,
                    "remaining_km": {"value": 7200, "unit": "km"},
                },
            ],
        }
    ],
}
MONTHLY = {
    "vehicle_id": "v1",
    "currency": "EUR",
    "months": [
        {"month": "2026-10", "in_progress": False, "costs": {"total": {"amount": "99.00"}}},
        {
            "month": "2026-11",
            "in_progress": True,
            "costs": {
                "energy": {"amount": "41.20", "currency": "EUR"},
                "maintenance": {"amount": "120.00", "currency": "EUR"},
                "total": {"amount": "161.20", "currency": "EUR"},
            },
        },
    ],
}


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=HOST,
        data={CONF_HOST: HOST, CONF_API_KEY: "k", CONF_VERIFY_SSL: True},
        options={
            CONF_VEHICLES: {"v1": {CONF_VEHICLE_ID: "v1", CONF_VEHICLE_NAME: "Family"}},
        },
    )


async def _setup(hass: HomeAssistant, account: dict) -> MockConfigEntry:
    entry = _entry()
    entry.add_to_hass(hass)
    with (
        patch(f"{API}.async_get_vehicles", new=AsyncMock(return_value=VEHICLES)),
        patch(f"{API}.async_get_vehicle_metrics", new=AsyncMock(return_value={})),
        patch(f"{API}.async_get_account", new=AsyncMock(return_value=account)),
        patch(f"{API}.async_get_fleet_reminders", new=AsyncMock(return_value=FLEET_REMINDERS)),
        patch(f"{API}.async_get_monthly_costs", new=AsyncMock(return_value=MONTHLY)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _state(hass: HomeAssistant, name: str):
    return hass.states.get(f"sensor.family_{name}")


async def test_reminder_sensors(hass: HomeAssistant) -> None:
    await _setup(hass, {"scope": "homeassistant", "pending_charges": 2})

    overdue = _state(hass, "overdue_reminders")
    assert overdue.state == "1"
    assert overdue.attributes["reminders"][0]["title"] == "Technical inspection"
    assert _state(hass, "reminders_due_soon").state == "1"
    # the overdue inspection (a past date) is the closest one
    assert _state(hass, "next_service_date").state == "2026-10-20"
    assert _state(hass, "next_service_date").attributes["reminder"] == "Technical inspection"
    # smallest remaining distance among the reminders that have one
    assert float(_state(hass, "next_service_distance").state) == 850.0


async def test_month_cost_sensor(hass: HomeAssistant) -> None:
    await _setup(hass, {"scope": "homeassistant", "pending_charges": 0})

    cost = _state(hass, "cost_this_month")
    assert cost.state == "161.20"
    assert cost.attributes["unit_of_measurement"] == "EUR"
    assert cost.attributes["month"] == "2026-11"
    assert cost.attributes["in_progress"] is True
    assert cost.attributes["energy"] == "41.20"


async def test_pending_charges_sensor(hass: HomeAssistant) -> None:
    await _setup(hass, {"scope": "homeassistant", "pending_charges": 3})

    assert hass.states.get("sensor.autoledger_charges_to_qualify").state == "3"


async def test_no_read_sensors_for_an_ingest_only_token(hass: HomeAssistant) -> None:
    """An ingest token cannot read the figures: no sensor that would stay unavailable."""
    await _setup(hass, {"scope": "ingest"})

    assert _state(hass, "overdue_reminders") is None
    assert _state(hass, "cost_this_month") is None
    assert hass.states.get("sensor.autoledger_charges_to_qualify") is None
