"""The opt-in sections of the summary: read only while an entity that needs them is enabled."""

from __future__ import annotations

from datetime import date

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from test_read_sensors import FULL_SUMMARY, VEHICLES, _entry, _start, _state

from custom_components.autoledger.const import DOMAIN

SECTIONS = {
    "tires": {
        "mounted": 4,
        "worst_wear_pct": 62.5,
        "worst_life_used_pct": 71.0,
        "smallest_remaining_distance": {"value": 18400, "unit": "km"},
        "nearest_replacement_date": "2027-08-14",
        "worst_condition": "WARNING",
    },
    "battery": {
        "latest_percent": 93.4,
        "latest_date": "2026-10-30",
        "change_points_12m": 2,
        "current_capacity_kwh": 74.1,
        "max_capacity_kwh": 79.4,
    },
    "ownership": {
        "full_cost_per_km": {"amount": "0.4210", "currency": "EUR"},
        "current_value": {"amount": "21500.00", "currency": "EUR"},
        "depreciation_known": True,
        "mileage": None,
    },
    "sources": {
        "teslamate_connected": True,
        "last_record": "2026-11-05T08:15:00Z",
        "last_sync": "2026-11-05T08:00:00Z",
        "pending_charges": 0,
    },
}
SUMMARY = {"vehicles": [{**FULL_SUMMARY["vehicles"][0], "powertrain": "EV", **SECTIONS}]}
ENTITIES = {
    "tires_wear": "family_tire_wear",
    "tires_remaining_distance": "family_tire_distance_left",
    "tires_replacement_date": "family_tire_replacement_date",
    "battery_health": "family_battery_health",
    "ownership_cost_per_km": "family_full_cost_per_km",
    "vehicle_value": "family_estimated_value",
    "last_record": "family_last_record",
    "teslamate_last_sync": "family_teslamate_last_sync",
}


def _register(hass: HomeAssistant, entry, keys, *, enabled: bool) -> None:
    registry = er.async_get(hass)
    for key in keys:
        registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{entry.entry_id}_v1_{key}",
            config_entry=entry,
            suggested_object_id=ENTITIES[key],
            disabled_by=None if enabled else er.RegistryEntryDisabler.INTEGRATION,
        )


async def _summary_calls(hass: HomeAssistant, keys, *, enabled: bool = True, summary=SUMMARY):
    entry = _entry(linked=False)
    entry.add_to_hass(hass)
    _register(hass, entry, keys, enabled=enabled)
    mocks, patches = await _start(hass, entry, VEHICLES, summary)
    for p in patches:
        p.stop()
    return entry, mocks["summary"].await_args_list


async def test_nothing_is_asked_while_every_group_is_disabled(hass: HomeAssistant) -> None:
    _, calls = await _summary_calls(hass, ENTITIES, enabled=False)
    assert [list(call.args[0]) for call in calls] == [[]]


async def test_only_the_groups_with_an_enabled_entity_are_asked(hass: HomeAssistant) -> None:
    _, calls = await _summary_calls(hass, ["tires_wear", "vehicle_value"])
    assert sorted(calls[0].args[0]) == ["ownership", "tires"]


async def test_a_fresh_install_creates_the_entities_disabled(hass: HomeAssistant) -> None:
    entry = _entry(linked=False)
    _, patches = await _start(hass, entry, VEHICLES, SUMMARY)
    try:
        registry = er.async_get(hass)
        for key in ENTITIES:
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_v1_{key}")
            assert registry.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    finally:
        for p in patches:
            p.stop()


async def test_the_sections_feed_the_entities(hass: HomeAssistant) -> None:
    entry = _entry(linked=False)
    entry.add_to_hass(hass)
    _register(hass, entry, ENTITIES, enabled=True)
    _, patches = await _start(hass, entry, VEHICLES, SUMMARY)
    try:
        wear = _state(hass, "tire_wear")
        assert float(wear.state) == 62.5
        assert wear.attributes["condition"] == "WARNING"
        assert wear.attributes["life_used_pct"] == 71.0
        assert float(_state(hass, "tire_distance_left").state) == 18400
        assert _state(hass, "tire_replacement_date").state == date(2027, 8, 14).isoformat()
        battery = _state(hass, "battery_health")
        assert float(battery.state) == 93.4
        assert battery.attributes["max_capacity_kwh"] == 79.4
        cost = _state(hass, "full_cost_per_km")
        assert float(cost.state) == 0.421
        assert cost.attributes["unit_of_measurement"] == "EUR/km"
        assert cost.attributes["depreciation_known"] is True
        assert _state(hass, "estimated_value").state == "21500.00"
        assert _state(hass, "last_record").state == "2026-11-05T08:15:00+00:00"
        assert _state(hass, "teslamate_last_sync").state == "2026-11-05T08:00:00+00:00"
    finally:
        for p in patches:
            p.stop()


async def test_a_server_without_the_sections_leaves_them_unknown(hass: HomeAssistant) -> None:
    entry = _entry(linked=False)
    entry.add_to_hass(hass)
    _register(hass, entry, ENTITIES, enabled=True)
    _, patches = await _start(hass, entry, VEHICLES, FULL_SUMMARY)
    try:
        for name in ("tire_wear", "battery_health", "full_cost_per_km", "last_record"):
            assert _state(hass, name).state == "unknown", name
    finally:
        for p in patches:
            p.stop()


async def test_a_vehicle_without_tires_has_an_unknown_wear(hass: HomeAssistant) -> None:
    summary = {
        "vehicles": [
            {
                **SUMMARY["vehicles"][0],
                "tires": {**SECTIONS["tires"], "mounted": 0, "worst_wear_pct": 0},
            }
        ]
    }
    entry = _entry(linked=False)
    entry.add_to_hass(hass)
    _register(hass, entry, ["tires_wear"], enabled=True)
    _, patches = await _start(hass, entry, VEHICLES, summary)
    try:
        assert _state(hass, "tire_wear").state == "unknown"
    finally:
        for p in patches:
            p.stop()


async def test_a_combustion_vehicle_has_no_battery_health(hass: HomeAssistant) -> None:
    summary = {"vehicles": [{**SUMMARY["vehicles"][0], "powertrain": "ICE"}]}
    entry = _entry(linked=False)
    _, patches = await _start(hass, entry, VEHICLES, summary)
    try:
        registry = er.async_get(hass)
        assert (
            registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_v1_battery_health")
            is None
        )
    finally:
        for p in patches:
            p.stop()
