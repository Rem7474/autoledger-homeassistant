"""The lease mileage entities and the TeslaMate synchronization problem."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from test_data_groups import SECTIONS
from test_read_sensors import FULL_SUMMARY, VEHICLES, _entry, _start, _state

from custom_components.autoledger.const import DOMAIN

MILEAGE = {
    "allowance_per_year": {"value": 15000, "unit": "km"},
    "allowance_total": {"value": 45000, "unit": "km"},
    "allowance_to_date": {"value": 21000, "unit": "km"},
    "driven": {"value": 23400, "unit": "km"},
    "excess_price_per_km": {"amount": "0.1000", "currency": "EUR"},
    "excess_accrued": {"amount": "240.00", "currency": "EUR"},
    "excess_projected": {"amount": "910.50", "currency": "EUR"},
}
LEASE = {
    "vehicles": [
        {
            **FULL_SUMMARY["vehicles"][0],
            "powertrain": "EV",
            **SECTIONS,
            "ownership": {**SECTIONS["ownership"], "mileage": MILEAGE},
        }
    ]
}
ENTITIES = {
    "lease_mileage_driven": ("sensor", "family_lease_distance_driven"),
    "lease_excess_projected": ("sensor", "family_lease_projected_excess"),
    "teslamate_sync_problem": ("binary_sensor", "family_teslamate_sync_problem"),
}


def _enable(hass: HomeAssistant, entry, keys=ENTITIES) -> None:
    registry = er.async_get(hass)
    for key in keys:
        domain, object_id = ENTITIES[key]
        registry.async_get_or_create(
            domain,
            DOMAIN,
            f"{entry.entry_id}_v1_{key}",
            config_entry=entry,
            suggested_object_id=object_id,
        )


def _with_sources(**sources) -> dict:
    vehicle = {**LEASE["vehicles"][0], "sources": {**SECTIONS["sources"], **sources}}
    return {"vehicles": [vehicle]}


async def _run(hass: HomeAssistant, summary: dict, *, keys=ENTITIES):
    entry = _entry(linked=False)
    entry.add_to_hass(hass)
    _enable(hass, entry, keys)
    mocks, patches = await _start(hass, entry, VEHICLES, summary)
    for p in patches:
        p.stop()
    return entry, mocks


async def test_the_lease_figures_come_from_the_ownership_section(hass: HomeAssistant) -> None:
    await _run(hass, LEASE)

    driven = _state(hass, "lease_distance_driven")
    assert float(driven.state) == 23400
    assert driven.attributes["unit_of_measurement"] == "km"
    assert driven.attributes["allowance_to_date"] == 21000
    assert driven.attributes["allowance_total"] == 45000
    assert driven.attributes["over_allowance"] == 2400
    excess = _state(hass, "lease_projected_excess")
    assert excess.state == "910.50"
    assert excess.attributes["unit_of_measurement"] == "EUR"


async def test_a_vehicle_without_an_allowance_leaves_the_lease_figures_unknown(
    hass: HomeAssistant,
) -> None:
    summary = {"vehicles": [{**LEASE["vehicles"][0], "ownership": SECTIONS["ownership"]}]}
    await _run(hass, summary)

    assert _state(hass, "lease_distance_driven").state == "unknown"
    assert _state(hass, "lease_projected_excess").state == "unknown"


async def test_the_lease_and_sync_entities_are_created_disabled(hass: HomeAssistant) -> None:
    entry = _entry(linked=False)
    mocks, patches = await _start(hass, entry, VEHICLES, LEASE)
    for p in patches:
        p.stop()

    registry = er.async_get(hass)
    for key, (domain, _) in ENTITIES.items():
        entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{entry.entry_id}_v1_{key}")
        assert registry.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert [list(call.args[0]) for call in mocks["summary"].await_args_list] == [[]]


async def test_enabling_one_of_them_asks_for_its_section(hass: HomeAssistant) -> None:
    _, mocks = await _run(hass, LEASE, keys=["teslamate_sync_problem"])
    assert [list(call.args[0]) for call in mocks["summary"].await_args_list] == [["sources"]]


async def test_a_stale_synchronization_is_a_problem(hass: HomeAssistant) -> None:
    stale = (dt_util.utcnow() - timedelta(hours=5)).isoformat()
    await _run(hass, _with_sources(last_sync=stale))

    problem = hass.states.get("binary_sensor.family_teslamate_sync_problem")
    assert problem.state == "on"
    assert problem.attributes["device_class"] == "problem"
    assert problem.attributes["last_sync"] == stale


async def test_a_recent_synchronization_is_not_a_problem(hass: HomeAssistant) -> None:
    fresh = (dt_util.utcnow() - timedelta(minutes=40)).isoformat()
    await _run(hass, _with_sources(last_sync=fresh))
    assert hass.states.get("binary_sensor.family_teslamate_sync_problem").state == "off"


async def test_a_connected_vehicle_that_never_synchronized_is_a_problem(
    hass: HomeAssistant,
) -> None:
    await _run(hass, _with_sources(last_sync=None))
    assert hass.states.get("binary_sensor.family_teslamate_sync_problem").state == "on"


async def test_a_vehicle_not_connected_to_teslamate_has_no_verdict(hass: HomeAssistant) -> None:
    await _run(hass, _with_sources(teslamate_connected=False, last_sync=None))
    assert hass.states.get("binary_sensor.family_teslamate_sync_problem").state == "unknown"
