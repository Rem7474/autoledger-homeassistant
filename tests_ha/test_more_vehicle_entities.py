"""The optional per-vehicle entities fed by figures the integration already fetches."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from test_read_sensors import (
    FULL_SUMMARY,
    VEHICLES,
    _entry,
    _start,
    _state,
)

from custom_components.autoledger.const import DOMAIN
from custom_components.autoledger.vehicles import is_vehicle_figure

OPTIONAL = {
    "completeness": ("sensor", "family_data_completeness"),
    "month_distance": ("sensor", "family_distance_this_month"),
    "month_net": ("sensor", "family_net_cost_this_month"),
    "last_12_months_cost": ("sensor", "family_cost_last_12_months"),
    "last_charge_price_per_kwh": ("sensor", "family_last_charge_price_per_kwh"),
}
SUMMARY_WITH_NEW_FIELDS = {
    "vehicles": [
        {
            **FULL_SUMMARY["vehicles"][0],
            "powertrain": "EV",
            "month_distance": {"value": 412.5, "unit": "km"},
            "carpool_revenue_month": {"amount": "30.00", "currency": "EUR"},
            "month_net": {"amount": "131.20", "currency": "EUR"},
            "last_12_months_cost": {"amount": "2140.00", "currency": "EUR"},
            "last_charge": {
                **FULL_SUMMARY["vehicles"][0]["last_charge"],
                "price_per_kwh": {"amount": "0.2333", "currency": "EUR"},
            },
        }
    ]
}
WITH_ODOMETER = [{**VEHICLES[0], "current_odometer": 48210.4}]


def _enable(hass: HomeAssistant, entry, keys) -> None:
    registry = er.async_get(hass)
    for key in keys:
        domain, object_id = OPTIONAL[key]
        registry.async_get_or_create(
            domain,
            DOMAIN,
            f"{entry.entry_id}_v1_{key}",
            config_entry=entry,
            suggested_object_id=object_id,
        )


async def test_the_odometer_and_the_overdue_maintenance_are_on_and_the_rest_is_off(
    hass: HomeAssistant,
) -> None:
    entry = _entry(linked=False)
    _, patches = await _start(hass, entry, WITH_ODOMETER, SUMMARY_WITH_NEW_FIELDS)
    try:
        odometer = _state(hass, "odometer")
        assert float(odometer.state) == 48210.4
        assert odometer.attributes["unit_of_measurement"] == "km"
        assert odometer.attributes["state_class"] == "total_increasing"

        overdue = hass.states.get("binary_sensor.family_maintenance_overdue")
        assert overdue.state == "on"
        assert overdue.attributes["device_class"] == "problem"
        assert [r["title"] for r in overdue.attributes["reminders"]] == ["Technical inspection"]

        registry = er.async_get(hass)
        for key, (domain, _) in OPTIONAL.items():
            reg = registry.async_get(
                registry.async_get_entity_id(domain, DOMAIN, f"{entry.entry_id}_v1_{key}")
            )
            assert reg.disabled_by is er.RegistryEntryDisabler.INTEGRATION, key
        for key in ("odometer",):
            reg = registry.async_get(
                registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_v1_{key}")
            )
            assert reg.disabled_by is None
    finally:
        for p in patches:
            p.stop()


async def test_the_optional_figures_come_from_the_summary(hass: HomeAssistant) -> None:
    entry = _entry(linked=False)
    _enable(hass, entry.add_to_hass(hass) or entry, OPTIONAL)
    _, patches = await _start(hass, entry, WITH_ODOMETER, SUMMARY_WITH_NEW_FIELDS)
    try:
        assert float(_state(hass, "distance_this_month").state) == 412.5
        net = _state(hass, "net_cost_this_month")
        assert net.state == "131.20"
        assert net.attributes["unit_of_measurement"] == "EUR"
        assert net.attributes["carpool_revenue"] == "30.00"
        assert _state(hass, "cost_last_12_months").state == "2140.00"
        price = _state(hass, "last_charge_price_per_kwh")
        assert float(price.state) == 0.2333
        assert price.attributes["unit_of_measurement"] == "EUR/kWh"
        assert _state(hass, "data_completeness").state == "80"
    finally:
        for p in patches:
            p.stop()


async def test_a_server_without_the_new_fields_leaves_them_unknown(hass: HomeAssistant) -> None:
    entry = _entry(linked=False)
    _enable(hass, entry.add_to_hass(hass) or entry, OPTIONAL)
    _, patches = await _start(hass, entry, WITH_ODOMETER, FULL_SUMMARY)
    try:
        for name in (
            "distance_this_month",
            "net_cost_this_month",
            "cost_last_12_months",
            "last_charge_price_per_kwh",
        ):
            assert _state(hass, name).state == "unknown", name
    finally:
        for p in patches:
            p.stop()


async def test_a_combustion_vehicle_has_no_price_per_kwh(hass: HomeAssistant) -> None:
    entry = _entry(linked=False)
    summary = {"vehicles": [{**SUMMARY_WITH_NEW_FIELDS["vehicles"][0], "powertrain": "ICE"}]}
    _, patches = await _start(hass, entry, WITH_ODOMETER, summary)
    try:
        registry = er.async_get(hass)
        assert (
            registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_v1_last_charge_price_per_kwh"
            )
            is None
        )
    finally:
        for p in patches:
            p.stop()


def test_only_the_server_fed_unique_ids_are_vehicle_figures() -> None:
    assert is_vehicle_figure("e_v1_odometer")
    assert is_vehicle_figure("e_v1_month_net")
    assert not is_vehicle_figure("e_v1_send_odometer")
    assert not is_vehicle_figure("e_v1_odometer_last_sync")
    assert not is_vehicle_figure(None)
