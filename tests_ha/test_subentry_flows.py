"""Adding and editing vehicles and charging stations, driven by the real core."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.autoledger.api import (
    AutoLedgerAuthError,
    AutoLedgerConnectionError,
    AutoLedgerError,
)
from custom_components.autoledger.const import (
    CONF_API_KEY,
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
GET_VEHICLES = f"{API}.async_get_vehicles"
HOST = "http://autoledger.local:8080"
SERVER_VEHICLES = [
    {"id": "v1", "make": "Tesla", "model": "Model Y", "name": "Family"},
    {"id": "v2", "make": "Renault", "model": "Zoe", "name": "Work"},
]


def _entry(*, with_vehicle: bool = True) -> MockConfigEntry:
    subentries = []
    if with_vehicle:
        subentries.append(
            ConfigSubentryData(
                data={"vehicle_id": "v1", "vehicle_name": "Tesla Model Y Family"},
                subentry_type=SUBENTRY_VEHICLE,
                title="Tesla Model Y Family",
                unique_id="vehicle:v1",
            )
        )
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=HOST,
        version=2,
        data={CONF_HOST: HOST, CONF_API_KEY: "k", CONF_VERIFY_SSL: True},
        subentries_data=subentries,
    )


async def _start(hass: HomeAssistant, entry: MockConfigEntry, subentry_type: str, **context):
    context.setdefault("source", SOURCE_USER)
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, subentry_type), **{"context": context}
    )


def _sensor(hass: HomeAssistant, entity_id: str, state: str, **attributes) -> None:
    hass.states.async_set(entity_id, state, attributes)


# ----------------------------------------------------------------------- vehicles


async def test_add_a_vehicle(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    _sensor(hass, "sensor.zoe_battery", "80", unit_of_measurement="%", device_class="battery")
    _sensor(hass, "sensor.zoe_odometer", "42000", unit_of_measurement="km", device_class="distance")

    with patch(GET_VEHICLES, new=AsyncMock(return_value=SERVER_VEHICLES)):
        result = await _start(hass, entry, SUBENTRY_VEHICLE)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"
        # v1 is already configured: only v2 is offered
        options = result["data_schema"].schema["vehicle_id"].config["options"]
        assert [o["value"] for o in options] == ["v2"]

        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"vehicle_id": "v2"}
        )
        assert result["step_id"] == "mapping"
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            {"battery_soc_entity": "sensor.zoe_battery", "odometer_entity": "sensor.zoe_odometer"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Renault Zoe Work"
    vehicles = vehicles_config(entry)
    assert set(vehicles) == {"v1", "v2"}
    assert vehicles["v2"]["odometer_entity"] == "sensor.zoe_odometer"
    assert vehicles["v2"]["vehicle_name"] == "Renault Zoe Work"
    assert {s.unique_id for s in entry.subentries.values()} == {"vehicle:v1", "vehicle:v2"}


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (AutoLedgerAuthError("revoked"), "invalid_auth"),
        (AutoLedgerConnectionError("down"), "cannot_connect"),
    ],
)
async def test_add_a_vehicle_aborts_when_the_server_is_not_usable(
    hass: HomeAssistant, error: Exception, reason: str
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)

    with patch(GET_VEHICLES, new=AsyncMock(side_effect=error)):
        result = await _start(hass, entry, SUBENTRY_VEHICLE)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


@pytest.mark.parametrize("server", [[], SERVER_VEHICLES[:1]])
async def test_add_a_vehicle_aborts_when_none_is_left(hass: HomeAssistant, server: list) -> None:
    entry = _entry()
    entry.add_to_hass(hass)

    with patch(GET_VEHICLES, new=AsyncMock(return_value=server)):
        result = await _start(hass, entry, SUBENTRY_VEHICLE)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_vehicle_available"


async def test_vehicle_sensors_are_checked(hass: HomeAssistant) -> None:
    entry = _entry(with_vehicle=False)
    entry.add_to_hass(hass)
    _sensor(hass, "sensor.zoe_battery", "80", unit_of_measurement="kWh")

    with patch(GET_VEHICLES, new=AsyncMock(return_value=SERVER_VEHICLES)):
        result = await _start(hass, entry, SUBENTRY_VEHICLE)
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"vehicle_id": "v1"}
        )
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"battery_soc_entity": "sensor.zoe_battery"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"battery_soc_entity": "invalid_battery_unit"}


async def test_the_sensors_of_the_device_are_suggested(hass: HomeAssistant) -> None:
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    entry = _entry(with_vehicle=False)
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("other", "car")}
    )
    er.async_get(hass).async_get_or_create(
        "sensor", "other", "car_odo", suggested_object_id="car_odometer", device_id=device.id
    )

    with patch(GET_VEHICLES, new=AsyncMock(return_value=SERVER_VEHICLES)):
        result = await _start(hass, entry, SUBENTRY_VEHICLE)
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"vehicle_id": "v1", "device_id": device.id}
        )

    assert result["step_id"] == "mapping"
    assert "sensor.car_odometer" in result["description_placeholders"]["preview_section"]
    suggested = {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description
    }
    assert suggested["odometer_entity"] == "sensor.car_odometer"


async def test_reconfigure_a_vehicle(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    subentry_id = next(iter(entry.subentries))
    _sensor(hass, "sensor.new_odometer", "100", unit_of_measurement="km", device_class="distance")

    result = await _start(
        hass, entry, SUBENTRY_VEHICLE, source=SOURCE_RECONFIGURE, subentry_id=subentry_id
    )
    assert result["step_id"] == "mapping"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"odometer_entity": "sensor.new_odometer"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert vehicles_config(entry)["v1"]["odometer_entity"] == "sensor.new_odometer"
    assert vehicles_config(entry)["v1"]["vehicle_name"] == "Tesla Model Y Family"
    assert len(entry.subentries) == 1


# ----------------------------------------------------------------------- charging stations


async def _add_charger(hass: HomeAssistant, entry: MockConfigEntry, name: str = "Home wallbox"):
    _sensor(hass, "binary_sensor.wallbox", "off")
    result = await _start(hass, entry, SUBENTRY_CHARGER)
    assert result["step_id"] == "station"
    return await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"charger_name": name, "charging_status_entity": "binary_sensor.wallbox"},
    )


async def _fixed_assignment(hass: HomeAssistant, entry: MockConfigEntry, server_vehicles: list):
    """Start a station and choose the fixed assignment, with the server listing these vehicles."""
    result = await _add_charger(hass, entry)
    assert result["step_id"] == "assignment"
    with patch(f"{API}.async_get_vehicles", new=AsyncMock(return_value=server_vehicles)):
        return await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"assignment_mode": "fixed"}
        )


async def test_add_a_charger_assigned_to_a_vehicle(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)

    result = await _fixed_assignment(hass, entry, SERVER_VEHICLES)
    assert result["step_id"] == "assignment_vehicle"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"linked_vehicle_id": "v1"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    charger = chargers_config(entry)["home_wallbox"]
    assert charger["assignment_mode"] == "fixed"
    assert charger["linked_vehicle_id"] == "v1"
    assert charger["debounce_seconds"] == 60
    assert "charger:home_wallbox" in {s.unique_id for s in entry.subentries.values()}


async def test_a_fixed_assignment_offers_every_vehicle_of_the_account(
    hass: HomeAssistant,
) -> None:
    """No vehicle has to be linked first: the server's vehicles are all offered."""
    entry = _entry(with_vehicle=False)
    entry.add_to_hass(hass)

    result = await _fixed_assignment(hass, entry, SERVER_VEHICLES)
    assert result["step_id"] == "assignment_vehicle"
    options = result["data_schema"].schema["linked_vehicle_id"].config["options"]
    assert {option["value"] for option in options} == {"v1", "v2"}

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"linked_vehicle_id": "v2"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert chargers_config(entry)["home_wallbox"]["linked_vehicle_id"] == "v2"


async def test_a_fixed_assignment_still_offers_the_linked_vehicles_when_the_server_is_down(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    result = await _add_charger(hass, entry)
    with patch(f"{API}.async_get_vehicles", new=AsyncMock(side_effect=AutoLedgerError("down"))):
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"assignment_mode": "fixed"}
        )

    options = result["data_schema"].schema["linked_vehicle_id"].config["options"]
    assert [option["value"] for option in options] == ["v1"]


async def test_a_fixed_assignment_needs_a_vehicle_in_the_account(hass: HomeAssistant) -> None:
    entry = _entry(with_vehicle=False)
    entry.add_to_hass(hass)

    result = await _fixed_assignment(hass, entry, [])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_vehicle_configured"


async def test_add_a_charger_with_a_vehicle_selector(hass: HomeAssistant) -> None:
    entry = _entry(with_vehicle=False)
    entry.add_to_hass(hass)

    result = await _add_charger(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"assignment_mode": "input_select"}
    )
    assert result["step_id"] == "assignment_entity"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"vehicle_select_entity": "input_select.car"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert chargers_config(entry)["home_wallbox"]["vehicle_select_entity"] == "input_select.car"
    assert chargers_config(entry)["home_wallbox"]["linked_vehicle_id"] is None


@pytest.mark.parametrize("mode", ["correlation", "unassigned"])
async def test_add_a_charger_without_a_fixed_vehicle(hass: HomeAssistant, mode: str) -> None:
    entry = _entry(with_vehicle=False)
    entry.add_to_hass(hass)

    result = await _add_charger(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"assignment_mode": mode}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert chargers_config(entry)["home_wallbox"]["assignment_mode"] == mode


async def test_two_chargers_with_the_same_name_get_distinct_ids(hass: HomeAssistant) -> None:
    entry = _entry(with_vehicle=False)
    entry.add_to_hass(hass)

    for _ in range(2):
        result = await _add_charger(hass, entry)
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"assignment_mode": "unassigned"}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY

    assert set(chargers_config(entry)) == {"home_wallbox", "home_wallbox_1"}


async def test_a_charger_may_share_the_id_of_a_vehicle(hass: HomeAssistant) -> None:
    """Home Assistant wants subentry unique ids to differ across types: the type prefixes them."""
    entry = _entry()
    entry.add_to_hass(hass)

    result = await _add_charger(hass, entry, name="v1")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"assignment_mode": "unassigned"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert set(vehicles_config(entry)) == {"v1"}
    assert set(chargers_config(entry)) == {"v1"}


async def test_the_energy_counter_mode_is_deduced_from_the_sensor(hass: HomeAssistant) -> None:
    entry = _entry(with_vehicle=False)
    entry.add_to_hass(hass)
    _sensor(hass, "binary_sensor.wallbox", "off")
    _sensor(
        hass,
        "sensor.wallbox_energy",
        "0",
        unit_of_measurement="kWh",
        device_class="energy",
        state_class="measurement",
    )

    result = await _start(hass, entry, SUBENTRY_CHARGER)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "charger_name": "Garage",
            "charging_status_entity": "binary_sensor.wallbox",
            "energy_meter_entity": "sensor.wallbox_energy",
        },
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"assignment_mode": "unassigned"}
    )

    assert chargers_config(entry)["garage"]["energy_meter_type"] == "session"


async def test_a_charger_needs_a_name_and_a_real_energy_sensor(hass: HomeAssistant) -> None:
    entry = _entry(with_vehicle=False)
    entry.add_to_hass(hass)
    _sensor(hass, "binary_sensor.wallbox", "off")
    _sensor(hass, "sensor.power", "3.7", unit_of_measurement="kW")

    result = await _start(hass, entry, SUBENTRY_CHARGER)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "charger_name": "  ",
            "charging_status_entity": "binary_sensor.wallbox",
            "energy_meter_entity": "sensor.power",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {
        "base": "invalid_name",
        "energy_meter_entity": "invalid_energy_unit",
    }


async def test_reconfigure_a_charger_keeps_its_id(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    result = await _add_charger(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"assignment_mode": "unassigned"}
    )
    subentry_id = next(
        s.subentry_id for s in entry.subentries.values() if s.subentry_type == SUBENTRY_CHARGER
    )

    result = await _start(
        hass, entry, SUBENTRY_CHARGER, source=SOURCE_RECONFIGURE, subentry_id=subentry_id
    )
    assert result["step_id"] == "station"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "charger_name": "Renamed wallbox",
            "charging_status_entity": "binary_sensor.wallbox",
            "debounce_seconds": 120,
        },
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"assignment_mode": "unassigned"}
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    charger = chargers_config(entry)["home_wallbox"]
    assert charger["charger_name"] == "Renamed wallbox"
    assert charger["debounce_seconds"] == 120
    assert entry.subentries[subentry_id].title == "Renamed wallbox"
    assert entry.subentries[subentry_id].unique_id == "charger:home_wallbox"


async def test_a_charger_names_its_place_from_an_entity_that_can_be_removed(
    hass: HomeAssistant,
) -> None:
    """A new station has no location tag; the place entity is kept, then dropped when cleared."""
    entry = _entry()
    entry.add_to_hass(hass)
    _sensor(hass, "binary_sensor.wallbox", "off")
    hass.states.async_set("device_tracker.car", "home")

    result = await _start(hass, entry, SUBENTRY_CHARGER)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "charger_name": "Home wallbox",
            "charging_status_entity": "binary_sensor.wallbox",
            "location_entity": "device_tracker.car",
        },
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"assignment_mode": "unassigned"}
    )
    charger = chargers_config(entry)["home_wallbox"]
    assert charger["location_entity"] == "device_tracker.car"
    assert charger["charging_location"] == ""
    subentry_id = next(
        s.subentry_id for s in entry.subentries.values() if s.subentry_type == SUBENTRY_CHARGER
    )

    result = await _start(
        hass, entry, SUBENTRY_CHARGER, source=SOURCE_RECONFIGURE, subentry_id=subentry_id
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"charger_name": "Home wallbox", "charging_status_entity": "binary_sensor.wallbox"},
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"assignment_mode": "unassigned"}
    )
    await hass.async_block_till_done()

    assert "location_entity" not in chargers_config(entry)["home_wallbox"]


async def test_deleting_the_link_of_a_vehicle_keeps_its_figures(hass: HomeAssistant) -> None:
    """Removing a vehicle subentry stops the sending; the vehicle stays a device with its figures."""
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_subentry(
        entry,
        next(iter(entry.subentries.values())),
        data={"vehicle_id": "v1", "vehicle_name": "Family", "odometer_entity": "sensor.odo"},
    )
    hass.states.async_set("sensor.odo", "1000")
    with (
        patch(f"{API}.async_get_vehicles", new=AsyncMock(return_value=SERVER_VEHICLES)),
        patch(f"{API}.async_get_vehicle_metrics", new=AsyncMock(return_value={})),
        patch(f"{API}.async_get_account", new=AsyncMock(return_value={"scope": "ingest"})),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get("sensor.family_last_odometer_sync") is not None

        hass.config_entries.async_remove_subentry(entry, next(iter(entry.subentries)))
        await hass.async_block_till_done()

    assert hass.states.get("sensor.family_last_odometer_sync") is None
    # both vehicles of the server keep their figures, linked or not
    assert hass.states.get("sensor.family_cost_per_100km") is not None
    assert hass.states.get("sensor.work_cost_per_100km") is not None
