"""Sensors fed by the read API, set up through the real Home Assistant core."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.autoledger.const import (
    CONF_API_KEY,
    CONF_HOST,
    CONF_VEHICLE_ID,
    CONF_VEHICLE_NAME,
    CONF_VERIFY_SSL,
    DOMAIN,
)
from custom_components.autoledger.subentries import SUBENTRY_VEHICLE

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


SUMMARY = {
    "vehicles": [
        {
            "vehicle_id": "v1",
            "powertrain": "PHEV",
            "currency": "EUR",
            "cost_per_km": {"amount": "0.1840", "currency": "EUR"},
            "consumption_kwh_100km": 17.5,
            "consumption_l_100km": None,
            "completeness_pct": 80,
            "data_gaps": 2,
        }
    ]
}


def _entry(*, linked: bool = True) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=HOST,
        version=2,
        data={CONF_HOST: HOST, CONF_API_KEY: "k", CONF_VERIFY_SSL: True},
        subentries_data=[
            ConfigSubentryData(
                data={CONF_VEHICLE_ID: "v1", CONF_VEHICLE_NAME: "Family"},
                subentry_type=SUBENTRY_VEHICLE,
                title="Family",
                unique_id="vehicle:v1",
            )
        ]
        if linked
        else [],
    )


# Entities created disabled; a registry entry made beforehand is enabled, like one a user enabled
SECONDARY = {
    "reminders_overdue": "family_overdue_reminders",
    "reminders_due_soon": "family_reminders_due_soon",
    "next_service_distance": "family_next_service_distance",
}


def _enable_secondary(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    registry = er.async_get(hass)
    for key, object_id in SECONDARY.items():
        registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{entry.entry_id}_v1_{key}",
            config_entry=entry,
            suggested_object_id=object_id,
        )


async def _setup(
    hass: HomeAssistant,
    account: dict,
    summary: dict = SUMMARY,
    *,
    entry: MockConfigEntry | None = None,
    enable_secondary: bool = True,
) -> MockConfigEntry:
    entry = entry or _entry()
    entry.add_to_hass(hass)
    if enable_secondary:
        _enable_secondary(hass, entry)
    with (
        patch(f"{API}.async_get_vehicles", new=AsyncMock(return_value=VEHICLES)),
        patch(f"{API}.async_get_vehicle_metrics", new=AsyncMock(return_value={})),
        patch(f"{API}.async_get_account", new=AsyncMock(return_value=account)),
        patch(f"{API}.async_get_fleet_reminders", new=AsyncMock(return_value=FLEET_REMINDERS)),
        patch(f"{API}.async_get_monthly_costs", new=AsyncMock(return_value=MONTHLY)),
        patch(f"{API}.async_get_summary", new=AsyncMock(return_value=summary)),
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


async def test_summary_sensors(hass: HomeAssistant) -> None:
    await _setup(hass, {"scope": "homeassistant"})

    cost = _state(hass, "cost_per_km")
    assert float(cost.state) == 0.184
    assert cost.attributes["unit_of_measurement"] == "EUR/km"
    assert float(_state(hass, "consumption").state) == 17.5
    assert _state(hass, "consumption").attributes["unit_of_measurement"] == "kWh/100 km"
    # the fuel figure is unknown (no two full tanks): the sensor exists and says so
    assert _state(hass, "fuel_consumption").state == "unknown"

    gaps = hass.states.get("binary_sensor.family_data_gaps")
    assert gaps.state == "on"
    assert gaps.attributes["data_gaps"] == 2
    assert gaps.attributes["completeness_pct"] == 80


async def test_an_electric_vehicle_has_no_fuel_sensor(hass: HomeAssistant) -> None:
    summary = {"vehicles": [{**SUMMARY["vehicles"][0], "powertrain": "EV", "data_gaps": 0}]}
    await _setup(hass, {"scope": "homeassistant"}, summary)

    assert _state(hass, "consumption") is not None
    assert _state(hass, "fuel_consumption") is None
    assert hass.states.get("binary_sensor.family_data_gaps").state == "off"


async def test_no_data_gaps_sensor_for_an_ingest_only_token(hass: HomeAssistant) -> None:
    await _setup(hass, {"scope": "ingest"})

    assert hass.states.get("binary_sensor.family_data_gaps") is None
    assert _state(hass, "cost_per_km") is None


async def test_an_old_server_raises_a_repair_issue_until_it_is_updated(hass: HomeAssistant) -> None:
    from homeassistant.helpers import issue_registry as ir

    entry = await _setup(hass, {"scope": "homeassistant", "server_version": "v1.59.1"})
    issue_id = f"server_too_old_{entry.entry_id}"

    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_placeholders == {"version": "v1.59.1", "minimum": "1.61.0"}

    # a build from the sources ("dev") or a recent release clears it
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    with (
        patch(f"{API}.async_get_vehicles", new=AsyncMock(return_value=VEHICLES)),
        patch(f"{API}.async_get_vehicle_metrics", new=AsyncMock(return_value={})),
        patch(
            f"{API}.async_get_account",
            new=AsyncMock(return_value={"scope": "homeassistant", "server_version": "v1.61.0"}),
        ),
        patch(f"{API}.async_get_fleet_reminders", new=AsyncMock(return_value=FLEET_REMINDERS)),
        patch(f"{API}.async_get_monthly_costs", new=AsyncMock(return_value=MONTHLY)),
        patch(f"{API}.async_get_summary", new=AsyncMock(return_value=SUMMARY)),
    ):
        await coordinator.async_refresh()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


# What a server with the one-call summary returns for a plug-in hybrid
FULL_SUMMARY = {
    "vehicles": [
        {
            **SUMMARY["vehicles"][0],
            "month": "2026-11",
            "month_cost": {"amount": "161.20", "currency": "EUR"},
            "month_costs": {
                "energy": {"amount": "41.20", "currency": "EUR"},
                "maintenance": {"amount": "120.00", "currency": "EUR"},
                "total": {"amount": "161.20", "currency": "EUR"},
            },
            "last_charge": {
                "date": "2026-11-03T18:00:00Z",
                "cost": {"amount": "7.35", "currency": "EUR"},
                "energy_kwh": 31.5,
                "duration_minutes": 95,
            },
            "cost_per_100km": {"amount": "4.20", "currency": "EUR"},
        }
    ]
}
WORK = {"id": "v2", "make": "Renault", "model": "Clio", "name": "Work"}


def _server(vehicles: list[dict], summary: dict, metrics: dict | None = None):
    """Patch the API; the returned dict holds the mocks to inspect which calls were made."""
    mocks = {
        "vehicles": AsyncMock(return_value=vehicles),
        "metrics": AsyncMock(return_value=metrics or {}),
        "account": AsyncMock(return_value={"scope": "homeassistant"}),
        "reminders": AsyncMock(return_value=FLEET_REMINDERS),
        "monthly": AsyncMock(return_value=MONTHLY),
        "summary": AsyncMock(return_value=summary),
    }
    names = {
        "vehicles": "async_get_vehicles",
        "metrics": "async_get_vehicle_metrics",
        "account": "async_get_account",
        "reminders": "async_get_fleet_reminders",
        "monthly": "async_get_monthly_costs",
        "summary": "async_get_summary",
    }
    patches = [patch(f"{API}.{names[key]}", new=mock) for key, mock in mocks.items()]
    return mocks, patches


async def _start(hass, entry, vehicles, summary, metrics=None):
    mocks, patches = _server(vehicles, summary, metrics)
    for p in patches:
        p.start()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return mocks, patches


async def _refresh(hass, entry) -> None:
    await hass.data[DOMAIN][entry.entry_id]["coordinator"].async_refresh()
    await hass.async_block_till_done()


async def test_a_vehicle_nobody_linked_still_gets_its_figures(hass: HomeAssistant) -> None:
    """No subentry: the server's vehicle is a device with its entities anyway."""
    from homeassistant.helpers import device_registry as dr

    entry = _entry(linked=False)
    _, patches = await _start(hass, entry, VEHICLES, FULL_SUMMARY)
    try:
        assert float(_state(hass, "cost_per_km").state) == 0.184
        assert _state(hass, "cost_this_month").state == "161.20"
        assert hass.states.get("binary_sensor.family_data_gaps") is not None
        device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, f"{entry.entry_id}_v1")})
        assert device is not None and device.name == "Family"
        assert device.manufacturer == "Tesla"
    finally:
        for p in patches:
            p.stop()


async def test_secondary_sensors_are_created_disabled(hass: HomeAssistant) -> None:
    entry = _entry(linked=False)
    _, patches = await _start(hass, entry, VEHICLES, FULL_SUMMARY)
    try:
        registry = er.async_get(hass)
        for key in SECONDARY:
            reg = registry.async_get(
                registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_v1_{key}")
            )
            assert reg.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        # the figures that matter stay on
        for name in ("cost_this_month", "cost_per_km", "next_service_date", "consumption"):
            assert _state(hass, name).state != "unavailable"
    finally:
        for p in patches:
            p.stop()


async def test_the_summary_carries_the_charge_and_the_month_without_other_calls(
    hass: HomeAssistant,
) -> None:
    entry = _entry(linked=False)
    mocks, patches = await _start(hass, entry, VEHICLES, FULL_SUMMARY)
    try:
        assert float(_state(hass, "last_charge_cost").state) == 7.35
        assert _state(hass, "last_charge_cost").attributes["energy_kwh"] == 31.5
        assert _state(hass, "last_charge_cost").attributes["duration_minutes"] == 95
        assert float(_state(hass, "cost_per_100km").state) == 4.2
        assert _state(hass, "cost_this_month").attributes["energy"] == "41.20"
        # one call per cycle covers the fleet
        mocks["metrics"].assert_not_called()
        mocks["monthly"].assert_not_called()
        mocks["summary"].assert_awaited_once()
    finally:
        for p in patches:
            p.stop()


async def test_a_server_without_the_new_summary_fields_is_asked_vehicle_by_vehicle(
    hass: HomeAssistant,
) -> None:
    entry = _entry(linked=False)
    metrics = {"currency": "EUR", "last_charge_cost": 6.5, "cost_per_100km": 3.9}
    mocks, patches = await _start(hass, entry, VEHICLES, SUMMARY, metrics)
    try:
        assert float(_state(hass, "last_charge_cost").state) == 6.5
        assert float(_state(hass, "cost_per_100km").state) == 3.9
        assert _state(hass, "cost_this_month").state == "161.20"
        mocks["metrics"].assert_awaited_once_with("v1")
        mocks["monthly"].assert_awaited_once_with("v1")
    finally:
        for p in patches:
            p.stop()


async def test_a_vehicle_created_later_gets_its_entities_on_the_next_refresh(
    hass: HomeAssistant,
) -> None:
    entry = _entry(linked=False)
    mocks, patches = await _start(hass, entry, VEHICLES, FULL_SUMMARY)
    try:
        assert hass.states.get("sensor.work_cost_per_100km") is None

        mocks["vehicles"].return_value = [*VEHICLES, WORK]
        mocks["summary"].return_value = {
            "vehicles": [
                *FULL_SUMMARY["vehicles"],
                {
                    "vehicle_id": "v2",
                    "powertrain": "ICE",
                    "currency": "EUR",
                    "month": "2026-11",
                    "month_costs": {"total": {"amount": "60.00", "currency": "EUR"}},
                    "last_charge": None,
                    "cost_per_100km": {"amount": "8.10", "currency": "EUR"},
                    "consumption_l_100km": 5.6,
                    "data_gaps": 0,
                },
            ],
        }
        await _refresh(hass, entry)

        assert float(hass.states.get("sensor.work_cost_per_100km").state) == 8.1
        assert hass.states.get("sensor.work_cost_this_month").state == "60.00"
        assert hass.states.get("sensor.work_fuel_consumption").state == "5.6"
        # a combustion vehicle has no charge to report
        assert hass.states.get("sensor.work_last_charge_cost") is None
    finally:
        for p in patches:
            p.stop()


async def test_a_vehicle_the_server_stops_listing_keeps_its_entities_unavailable(
    hass: HomeAssistant,
) -> None:
    from homeassistant.helpers import device_registry as dr

    entry = _entry(linked=False)
    mocks, patches = await _start(hass, entry, [*VEHICLES, WORK], FULL_SUMMARY)
    try:
        assert hass.states.get("sensor.work_cost_per_100km") is not None

        mocks["vehicles"].return_value = VEHICLES  # a token limited to one vehicle, or a deletion
        await _refresh(hass, entry)

        assert hass.states.get("sensor.work_cost_per_100km").state == "unavailable"
        assert _state(hass, "cost_per_km").state != "unavailable"
        assert (
            dr.async_get(hass).async_get_device(identifiers={(DOMAIN, f"{entry.entry_id}_v2")})
            is not None
        )
    finally:
        for p in patches:
            p.stop()


async def test_a_limited_token_only_gets_the_vehicles_it_sees(hass: HomeAssistant) -> None:
    entry = _entry(linked=False)
    _, patches = await _start(hass, entry, VEHICLES, FULL_SUMMARY)  # the server hides "Work"
    try:
        assert hass.states.get("sensor.work_cost_per_100km") is None
        assert hass.states.get("sensor.work_cost_this_month") is None
    finally:
        for p in patches:
            p.stop()
