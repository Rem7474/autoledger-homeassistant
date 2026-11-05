"""The vehicles of the account as Home Assistant devices.

Every vehicle the server lists is a device with its read entities: nothing has to be added by
hand, and a vehicle created later in AutoLedger shows up on the next refresh. A ``vehicle``
subentry only adds what Home Assistant sends to AutoLedger for that vehicle.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_VEHICLE_NAME, DOMAIN
from .coordinator import AutoLedgerDataUpdateCoordinator
from .subentries import vehicles_config

# Unique id suffixes of the entities fed by the server for a vehicle
VEHICLE_FIGURE_KEYS = (
    "last_charge_cost",
    "cost_per_100km",
    "reminders_overdue",
    "reminders_due_soon",
    "next_service_date",
    "next_service_distance",
    "month_cost",
    "cost_per_km",
    "consumption_kwh_100km",
    "consumption_l_100km",
    "data_gaps",
)


def is_vehicle_figure(unique_id: str | None) -> bool:
    """Whether a unique id is one of the server-fed entities of a vehicle."""
    return bool(unique_id) and unique_id.endswith(tuple(f"_{key}" for key in VEHICLE_FIGURE_KEYS))


def vehicle_device_info(
    entry: ConfigEntry,
    vehicle_id: str,
    vehicle_name: str,
    make: str | None = None,
    model: str | None = None,
) -> DeviceInfo:
    """Return the device of a vehicle; its identifier is the same whoever creates it first."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{vehicle_id}")},
        name=vehicle_name,
        manufacturer=make or "AutoLedger",
        model=model or "Vehicle",
    )


def listed_vehicles(coordinator: AutoLedgerDataUpdateCoordinator) -> dict[str, dict[str, Any]]:
    """Vehicles the server returned at the last refresh, by id."""
    return (coordinator.data or {}).get("vehicles", {})


def vehicle_name(entry: ConfigEntry, vehicle_id: str, meta: dict[str, Any]) -> str:
    """Name of a vehicle: the one it has in AutoLedger, else the one given when linking it."""
    if meta.get("name"):
        return str(meta["name"])
    conf = vehicles_config(entry).get(vehicle_id, {})
    return str(conf.get(CONF_VEHICLE_NAME) or f"Vehicle {vehicle_id}")


def vehicle_device(
    entry: ConfigEntry, coordinator: AutoLedgerDataUpdateCoordinator, vehicle_id: str
) -> tuple[str, DeviceInfo]:
    """Return the name and the device of a listed vehicle."""
    meta = listed_vehicles(coordinator).get(vehicle_id, {})
    name = vehicle_name(entry, vehicle_id, meta)
    return name, vehicle_device_info(entry, vehicle_id, name, meta.get("make"), meta.get("model"))


class ListedVehicleEntity:
    """Entity of a vehicle that is unavailable once the server stops listing the vehicle.

    A vehicle deleted in AutoLedger, or no longer visible to the token, keeps its device and its
    history in Home Assistant: its entities say they have nothing to show.
    """

    _vehicle_id: str
    coordinator: AutoLedgerDataUpdateCoordinator

    @property
    def available(self) -> bool:
        """Return whether the server still lists the vehicle."""
        return bool(getattr(super(), "available", True)) and (
            self._vehicle_id in listed_vehicles(self.coordinator)
        )


def async_add_vehicle_entities(
    entry: ConfigEntry,
    coordinator: AutoLedgerDataUpdateCoordinator,
    async_add_entities: AddEntitiesCallback,
    build: Callable[[str], Iterable[Any]],
) -> None:
    """Add the entities of every listed vehicle, then those of each vehicle listed later.

    A vehicle linked to Home Assistant that the server does not list (deleted, or hidden from the
    token) keeps its entities, which are then unavailable.
    """
    known: set[str] = set()

    @callback
    def _add_new_vehicles() -> None:
        wanted = [*listed_vehicles(coordinator), *vehicles_config(entry)]
        new = [vehicle_id for vehicle_id in dict.fromkeys(wanted) if vehicle_id not in known]
        if not new:
            return
        known.update(new)
        async_add_entities([entity for vehicle_id in new for entity in build(vehicle_id)])

    _add_new_vehicles()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_vehicles))
