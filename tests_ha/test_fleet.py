"""The household entities: the fleet cost of the month and the monthly budget."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from test_read_sensors import FULL_SUMMARY, VEHICLES, _entry, _start

from custom_components.autoledger.api import AutoLedgerApiError
from custom_components.autoledger.const import DOMAIN

API = "custom_components.autoledger.api.AutoLedgerApiClient"
ENTITIES = (
    ("sensor", "fleet_month_cost", "autoledger_household_cost_this_month"),
    ("sensor", "fleet_budget_used", "autoledger_budget_used"),
    ("binary_sensor", "fleet_over_budget", "autoledger_over_budget"),
)


def _fleet(used_pct: float | None = 62.5) -> dict:
    return {
        "fleet_currency": "EUR",
        "current_month_totals": [
            {
                "currency": "EUR",
                "vehicles": 2,
                "current_month_cost": {"amount": "250.00", "currency": "EUR"},
            },
            {
                "currency": "CHF",
                "vehicles": 1,
                "current_month_cost": {"amount": "40.00", "currency": "CHF"},
            },
        ],
        "budget": (
            None
            if used_pct is None
            else {
                "monthly": {"amount": "400.00", "currency": "EUR"},
                "spent_this_month": {"amount": "250.00", "currency": "EUR"},
                "remaining": {"amount": "150.00", "currency": "EUR"},
                "used_pct": used_pct,
            }
        ),
    }


async def _start_fleet(hass: HomeAssistant, fleet, *, enable: bool = True):
    entry = _entry(linked=False)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for domain, key, object_id in ENTITIES:
        registry.async_get_or_create(
            domain,
            DOMAIN,
            f"{entry.entry_id}_{key}",
            config_entry=entry,
            suggested_object_id=object_id,
            disabled_by=None if enable else er.RegistryEntryDisabler.INTEGRATION,
        )
    mock = (
        AsyncMock(side_effect=fleet)
        if isinstance(fleet, Exception)
        else AsyncMock(return_value=fleet)
    )
    patcher = patch(f"{API}.async_get_fleet", new=mock)
    patcher.start()
    _, patches = await _start(hass, entry, VEHICLES, FULL_SUMMARY)
    return mock, [patcher, *patches]


def _stop(patches) -> None:
    for p in patches:
        p.stop()


async def test_nothing_is_read_while_the_household_entities_are_disabled(
    hass: HomeAssistant,
) -> None:
    mock, patches = await _start_fleet(hass, _fleet(), enable=False)
    try:
        assert mock.await_count == 0
    finally:
        _stop(patches)


async def test_a_fresh_install_creates_them_disabled(hass: HomeAssistant) -> None:
    entry = _entry(linked=False)
    _, patches = await _start(hass, entry, VEHICLES, FULL_SUMMARY)
    try:
        registry = er.async_get(hass)
        for domain, key, _ in ENTITIES:
            entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{entry.entry_id}_{key}")
            assert registry.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    finally:
        _stop(patches)


async def test_the_month_cost_and_the_budget_are_mapped(hass: HomeAssistant) -> None:
    _, patches = await _start_fleet(hass, _fleet())
    try:
        cost = hass.states.get("sensor.autoledger_household_cost_this_month")
        assert cost.state == "250.00"
        assert cost.attributes["unit_of_measurement"] == "EUR"
        assert cost.attributes["vehicles"] == 2
        assert cost.attributes["excluded_currencies"] == ["CHF"]
        used = hass.states.get("sensor.autoledger_budget_used")
        assert float(used.state) == 62.5
        assert used.attributes["monthly"] == "400.00"
        assert used.attributes["remaining"] == "150.00"
        assert hass.states.get("binary_sensor.autoledger_over_budget").state == "off"
    finally:
        _stop(patches)


async def test_the_budget_is_over_once_more_than_all_of_it_is_spent(
    hass: HomeAssistant,
) -> None:
    _, patches = await _start_fleet(hass, _fleet(used_pct=112.0))
    try:
        assert hass.states.get("binary_sensor.autoledger_over_budget").state == "on"
    finally:
        _stop(patches)


async def test_without_a_budget_the_budget_entities_are_unknown(hass: HomeAssistant) -> None:
    _, patches = await _start_fleet(hass, _fleet(used_pct=None))
    try:
        assert hass.states.get("sensor.autoledger_budget_used").state == "unknown"
        assert hass.states.get("binary_sensor.autoledger_over_budget").state == "unknown"
        assert hass.states.get("sensor.autoledger_household_cost_this_month").state == "250.00"
    finally:
        _stop(patches)


async def test_a_failing_fleet_route_leaves_the_rest_available(hass: HomeAssistant) -> None:
    _, patches = await _start_fleet(hass, AutoLedgerApiError("down", status_code=500))
    try:
        assert hass.states.get("sensor.autoledger_household_cost_this_month").state == "unknown"
        assert hass.states.get("sensor.family_cost_per_km").state != "unavailable"
    finally:
        _stop(patches)
