"""Moving an entry from version 1 (options) to version 2 (subentries), on the real core.

A version 1 entry keeps its vehicles and charging stations in ``options``. What the
user can see (entity ids, history) and what the integration stored (the sessions in
progress) must survive the move untouched.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.autoledger.const import (
    AUTH_METHOD_TOKEN,
    CONF_API_KEY,
    CONF_AUTH_METHOD,
    CONF_HOST,
    CONF_VERIFY_SSL,
    DOMAIN,
)
from custom_components.autoledger.subentries import (
    SUBENTRY_CHARGER,
    SUBENTRY_VEHICLE,
    chargers_config,
    vehicles_config,
)

API = "custom_components.autoledger.api.AutoLedgerApiClient"
HOST = "http://autoledger.local:8080"
SESSION = {
    "state": "charging",
    "session_start_time": "2026-11-03T18:00:00+00:00",
    "energy_start": 1200.5,
    "retry_queue": [],
}


def _v1_entry(options: dict) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=HOST,
        version=1,
        data={CONF_HOST: HOST, CONF_API_KEY: "k", CONF_VERIFY_SSL: True},
        options=options,
    )


def _family_and_work() -> dict:
    """Two vehicles and a wallbox, the way version 1 stored them."""
    return {
        "vehicles": {
            "v1": {
                "vehicle_id": "v1",
                "vehicle_name": "Family",
                "device_id": None,
                "battery_soc_entity": "sensor.family_battery",
                "odometer_entity": "sensor.family_odometer",
                "charging_status_entity": None,
                "trip_location_entity": None,
                "trip_moving_entity": None,
            },
            "v2": {"vehicle_id": "v2", "vehicle_name": "Work"},
        },
        "chargers": {
            "home_wallbox": {
                "charger_id": "home_wallbox",
                "charger_name": "Home wallbox",
                "charging_status_entity": "binary_sensor.wallbox_charging",
                "energy_meter_entity": "sensor.wallbox_energy",
                "energy_meter_type": "total_increasing",
                "debounce_seconds": 90,
                "charging_location": "home",
                "assignment_mode": "fixed",
                "linked_vehicle_id": "v1",
                "vehicle_select_entity": None,
            }
        },
    }


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set the entry up; add_to_hass is idempotent for an entry already added."""
    if entry.entry_id not in {e.entry_id for e in hass.config_entries.async_entries(DOMAIN)}:
        entry.add_to_hass(hass)
    with (
        patch(f"{API}.async_get_vehicles", new=AsyncMock(return_value=[])),
        patch(f"{API}.async_get_vehicle_metrics", new=AsyncMock(return_value={})),
        patch(f"{API}.async_get_account", new=AsyncMock(return_value={"scope": "ingest"})),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def test_vehicles_and_chargers_become_subentries(hass: HomeAssistant) -> None:
    entry = _v1_entry(_family_and_work())
    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.version == 2
    assert entry.data[CONF_AUTH_METHOD] == AUTH_METHOD_TOKEN
    assert entry.data[CONF_API_KEY] == "k"
    assert dict(entry.options) == {}

    vehicles = vehicles_config(entry)
    assert set(vehicles) == {"v1", "v2"}
    assert vehicles["v1"]["odometer_entity"] == "sensor.family_odometer"
    assert chargers_config(entry)["home_wallbox"]["debounce_seconds"] == 90
    titles = {sub.unique_id: sub.title for sub in entry.subentries.values()}
    assert titles == {
        "vehicle:v1": "Family",
        "vehicle:v2": "Work",
        "charger:home_wallbox": "Home wallbox",
    }


async def test_entities_keep_their_ids_through_the_migration(hass: HomeAssistant) -> None:
    entry = _v1_entry(_family_and_work())
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    # entities created by version 1: <entry_id>_<vehicle or charger>_<key>
    old = {
        "sensor": f"{entry.entry_id}_v1_last_charge_cost",
        "button": f"{entry.entry_id}_v1_send_odometer",
    }
    seeded = {
        domain: registry.async_get_or_create(
            domain,
            DOMAIN,
            unique_id,
            config_entry=entry,
            suggested_object_id=f"kept_{domain}",
        ).entity_id
        for domain, unique_id in old.items()
    }

    await _setup(hass, entry)

    for domain, unique_id in old.items():
        entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
        assert entity_id == seeded[domain]
        state = hass.states.get(entity_id)
        assert state is not None and not state.attributes.get("restored")


async def test_a_running_session_is_still_picked_up(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    entry = _v1_entry(_family_and_work())
    entry.add_to_hass(hass)
    hass.states.async_set("binary_sensor.wallbox_charging", "on")
    hass_storage[f"{DOMAIN}.{entry.entry_id}.charger.home_wallbox"] = {
        "version": 1,
        "key": f"{DOMAIN}.{entry.entry_id}.charger.home_wallbox",
        "data": SESSION,
    }

    await _setup(hass, entry)

    tracker = hass.data[DOMAIN][entry.entry_id]["trackers"]["home_wallbox"]
    assert tracker.state == "charging"
    assert tracker.session_start_time == SESSION["session_start_time"]


async def test_the_implicit_charger_of_version_1_becomes_an_explicit_one(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """Without any charging station, a vehicle with a status sensor charged by itself."""
    entry = _v1_entry(
        {
            "vehicles": {
                "v1": {
                    "vehicle_id": "v1",
                    "vehicle_name": "Family",
                    "charging_status_entity": "binary_sensor.family_charging",
                }
            }
        }
    )
    entry.add_to_hass(hass)
    hass.states.async_set("binary_sensor.family_charging", "on")
    hass_storage[f"{DOMAIN}.{entry.entry_id}.charger.v1"] = {
        "version": 1,
        "key": f"{DOMAIN}.{entry.entry_id}.charger.v1",
        "data": SESSION,
    }

    await _setup(hass, entry)

    charger = chargers_config(entry)["v1"]
    assert charger["charging_status_entity"] == "binary_sensor.family_charging"
    assert charger["assignment_mode"] == "fixed"
    assert charger["linked_vehicle_id"] == "v1"
    tracker = hass.data[DOMAIN][entry.entry_id]["trackers"]["v1"]
    assert tracker.state == "charging"


async def test_an_interrupted_migration_is_replayed_without_duplicates(
    hass: HomeAssistant,
) -> None:
    """The version is raised last: a crash after the first subentry leaves version 1."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=HOST,
        version=1,
        data={CONF_HOST: HOST, CONF_API_KEY: "k", CONF_VERIFY_SSL: True},
        options=_family_and_work(),
        subentries_data=[
            ConfigSubentryData(
                data={"vehicle_id": "v1", "vehicle_name": "Family"},
                subentry_type=SUBENTRY_VEHICLE,
                title="Family",
                unique_id="vehicle:v1",
            )
        ],
    )
    await _setup(hass, entry)

    assert entry.version == 2
    keys = sorted((sub.subentry_type, sub.unique_id) for sub in entry.subentries.values())
    assert keys == [
        (SUBENTRY_CHARGER, "charger:home_wallbox"),
        (SUBENTRY_VEHICLE, "vehicle:v1"),
        (SUBENTRY_VEHICLE, "vehicle:v2"),
    ]


async def test_an_entry_without_vehicles_migrates_cleanly(hass: HomeAssistant) -> None:
    entry = _v1_entry({})
    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.version == 2
    assert not entry.subentries


async def test_an_entry_from_a_newer_version_is_left_alone(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=HOST,
        version=3,
        data={CONF_HOST: HOST, CONF_API_KEY: "k"},
    )
    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.MIGRATION_ERROR
    assert entry.version == 3
