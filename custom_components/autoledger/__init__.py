"""AutoLedger Home Assistant Custom Component."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import AutoLedgerApiClient
from .const import (
    CONF_API_KEY,
    CONF_CHARGERS,
    CONF_HOST,
    CONF_VEHICLES,
    CONF_VERIFY_SSL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import AutoLedgerDataUpdateCoordinator
from .odometer_tracker import AutoLedgerOdometerTracker
from .services import async_setup_services, async_unload_services
from .session_tracker import AutoLedgerChargerTracker
from .trip_tracker import AutoLedgerTripTracker

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the AutoLedger integration component."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up AutoLedger from a config entry."""
    host = entry.data[CONF_HOST]
    api_key = entry.data[CONF_API_KEY]
    verify_ssl = entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)

    session = async_get_clientsession(hass)
    client = AutoLedgerApiClient(
        host=host,
        api_key=api_key,
        session=session,
        verify_ssl=verify_ssl,
    )

    configured_vehicles = entry.options.get(CONF_VEHICLES, {})
    configured_chargers = entry.options.get(CONF_CHARGERS, {})

    coordinator = AutoLedgerDataUpdateCoordinator(
        hass=hass,
        client=client,
        entry_id=entry.entry_id,
        vehicles_config=configured_vehicles,
    )

    # Initial data load
    await coordinator.async_config_entry_first_refresh()

    # Setup odometer tracker for trip-end stabilization and initial sync
    odometer_tracker = AutoLedgerOdometerTracker(
        hass=hass,
        client=client,
        vehicles_config=configured_vehicles,
        coordinator=coordinator,
    )
    coordinator.odometer_tracker = odometer_tracker
    await odometer_tracker.async_setup()

    trip_tracker = AutoLedgerTripTracker(
        hass=hass,
        client=client,
        vehicles_config=configured_vehicles,
        store=Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.trips"),
        coordinator=coordinator,
    )
    await trip_tracker.async_setup()

    # Setup charger trackers for each configured charging station
    trackers: dict[str, AutoLedgerChargerTracker] = {}
    for charger_id, charger_conf in configured_chargers.items():
        tracker = AutoLedgerChargerTracker(
            hass=hass,
            client=client,
            charger_id=charger_id,
            config=charger_conf,
            vehicles_config=configured_vehicles,
            coordinator=coordinator,
            store=Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.charger.{charger_id}"),
        )
        await tracker.async_setup()
        trackers[charger_id] = tracker

    # Backward compatibility fallback for legacy vehicle-attached configs
    if not configured_chargers:
        for vehicle_id, vehicle_conf in configured_vehicles.items():
            if vehicle_conf.get("charging_status_entity"):
                tracker = AutoLedgerChargerTracker(
                    hass=hass,
                    client=client,
                    charger_id=vehicle_id,
                    config=vehicle_conf,
                    vehicles_config=configured_vehicles,
                    coordinator=coordinator,
                    store=Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.charger.{vehicle_id}"),
                )
                await tracker.async_setup()
                trackers[vehicle_id] = tracker

    entry_data = {
        "client": client,
        "coordinator": coordinator,
        "odometer_tracker": odometer_tracker,
        "trip_tracker": trip_tracker,
        "trackers": trackers,
    }

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entry_data
    if hasattr(entry, "runtime_data"):
        entry.runtime_data = entry_data

    # Forward setup to supported platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _remove_stale_entities(hass, entry)

    # Register custom services
    await async_setup_services(hass)

    # Listen for options flow updates to reload the component
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


def _remove_stale_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop registry entries left by a previous configuration (shown as unavailable).

    Once every platform is set up, an entity of this entry still holding only a
    restored state was not created by the current configuration.
    """
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        state = hass.states.get(reg_entry.entity_id)
        if state is not None and state.attributes.get("restored"):
            registry.async_remove(reg_entry.entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, {})
        odometer_tracker = data.get("odometer_tracker")
        if odometer_tracker:
            await odometer_tracker.async_unload()

        trip_tracker = data.get("trip_tracker")
        if trip_tracker:
            await trip_tracker.async_unload()

        trackers = data.get("trackers", {})
        for tracker in trackers.values():
            await tracker.async_unload()

        await async_unload_services(hass)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry after options update."""
    await hass.config_entries.async_reload(entry.entry_id)
