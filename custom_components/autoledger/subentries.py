"""Vehicles and charging stations, stored as config subentries.

Version 1 of the integration kept them in the options of the config entry
(``options["vehicles"]`` and ``options["chargers"]``). Version 2 stores each one as
a subentry, so Home Assistant lists, adds, reconfigures and deletes them itself.

The runtime still works on the same ``{id: settings}`` dictionaries: the helpers
below rebuild them from the subentries.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    ASSIGNMENT_MODE_FIXED,
    CONF_ASSIGNMENT_MODE,
    CONF_CHARGER_ID,
    CONF_CHARGER_NAME,
    CONF_CHARGERS,
    CONF_CHARGING_LOCATION,
    CONF_CHARGING_STATUS_ENTITY,
    CONF_DEBOUNCE_SECONDS,
    CONF_ENERGY_METER_TYPE,
    CONF_LINKED_VEHICLE_ID,
    CONF_VEHICLE_ID,
    CONF_VEHICLE_NAME,
    CONF_VEHICLES,
    DEFAULT_CHARGING_LOCATION,
    DEFAULT_DEBOUNCE_SECONDS,
    ENERGY_METER_TYPE_TOTAL_INCREASING,
)

SUBENTRY_VEHICLE = "vehicle"
SUBENTRY_CHARGER = "charger"


def subentry_unique_id(subentry_type: str, item_id: str) -> str:
    """Unique id of a subentry.

    Home Assistant requires it to be unique across every type of subentry of an
    entry, and a vehicle and a charging station may share an id (a charger named
    after its vehicle), so the type is part of it.
    """
    return f"{subentry_type}:{item_id}"


def _settings(entry: ConfigEntry, subentry_type: str, id_key: str) -> dict[str, dict[str, Any]]:
    """Return ``{item id: settings}`` for one type of subentry."""
    return {
        str(subentry.data[id_key]): dict(subentry.data)
        for subentry in entry.subentries.values()
        if subentry.subentry_type == subentry_type and id_key in subentry.data
    }


def vehicles_config(entry: ConfigEntry) -> dict[str, dict[str, Any]]:
    """Vehicles of the entry, keyed by their AutoLedger vehicle id."""
    return _settings(entry, SUBENTRY_VEHICLE, CONF_VEHICLE_ID)


def chargers_config(entry: ConfigEntry) -> dict[str, dict[str, Any]]:
    """Charging stations of the entry, keyed by their charger id."""
    return _settings(entry, SUBENTRY_CHARGER, CONF_CHARGER_ID)


def plan_subentries_from_options(
    options: Mapping[str, Any],
) -> list[tuple[str, str, str, dict[str, Any]]]:
    """Translate the version 1 options into ``(type, unique_id, title, data)`` subentries.

    A version 1 entry without any charging station but with a vehicle that has a
    charging status sensor used to get a charge tracker named after that vehicle:
    it becomes an explicit charging station with the same id, so the stored
    session of the tracker keeps its name.
    """
    vehicles: Mapping[str, Mapping[str, Any]] = options.get(CONF_VEHICLES) or {}
    chargers: Mapping[str, Mapping[str, Any]] = options.get(CONF_CHARGERS) or {}

    planned: list[tuple[str, str, str, dict[str, Any]]] = []
    for vehicle_id, settings in vehicles.items():
        data = {**settings, CONF_VEHICLE_ID: str(vehicle_id)}
        title = str(settings.get(CONF_VEHICLE_NAME) or vehicle_id)
        planned.append(
            (SUBENTRY_VEHICLE, subentry_unique_id(SUBENTRY_VEHICLE, str(vehicle_id)), title, data)
        )

    for charger_id, settings in chargers.items():
        data = {**settings, CONF_CHARGER_ID: str(charger_id)}
        title = str(settings.get(CONF_CHARGER_NAME) or charger_id)
        planned.append(
            (SUBENTRY_CHARGER, subentry_unique_id(SUBENTRY_CHARGER, str(charger_id)), title, data)
        )

    if not chargers:
        for vehicle_id, settings in vehicles.items():
            status_entity = settings.get(CONF_CHARGING_STATUS_ENTITY)
            if not status_entity:
                continue
            name = str(settings.get(CONF_VEHICLE_NAME) or vehicle_id)
            data = {
                CONF_CHARGER_ID: str(vehicle_id),
                CONF_CHARGER_NAME: name,
                CONF_CHARGING_STATUS_ENTITY: status_entity,
                CONF_ENERGY_METER_TYPE: ENERGY_METER_TYPE_TOTAL_INCREASING,
                CONF_DEBOUNCE_SECONDS: DEFAULT_DEBOUNCE_SECONDS,
                CONF_CHARGING_LOCATION: DEFAULT_CHARGING_LOCATION,
                CONF_ASSIGNMENT_MODE: ASSIGNMENT_MODE_FIXED,
                CONF_LINKED_VEHICLE_ID: str(vehicle_id),
            }
            planned.append(
                (
                    SUBENTRY_CHARGER,
                    subentry_unique_id(SUBENTRY_CHARGER, str(vehicle_id)),
                    name,
                    data,
                )
            )

    return planned
