"""AutoLedger Home Assistant Custom Component."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

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
from .services import async_setup_services, async_unload_services
from .session_tracker import AutoLedgerChargerTracker

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
                )
                await tracker.async_setup()
                trackers[vehicle_id] = tracker

    entry_data = {
        "client": client,
        "coordinator": coordinator,
        "trackers": trackers,
    }

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entry_data
    if hasattr(entry, "runtime_data"):
        entry.runtime_data = entry_data

    # Forward setup to supported platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register custom services
    await async_setup_services(hass)

    # Listen for options flow updates to reload the component
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, {})
        trackers = data.get("trackers", {})
        for tracker in trackers.values():
            await tracker.async_unload()

        await async_unload_services(hass)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry after options update."""
    await hass.config_entries.async_reload(entry.entry_id)
