"""AutoLedger Home Assistant Custom Component."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AutoLedgerApiClient
from .const import (
    CONF_API_KEY,
    CONF_HOST,
    CONF_VEHICLES,
    CONF_VERIFY_SSL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import AutoLedgerDataUpdateCoordinator
from .services import async_setup_services, async_unload_services
from .session_tracker import AutoLedgerSessionTracker

_LOGGER = logging.getLogger(__name__)


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

    coordinator = AutoLedgerDataUpdateCoordinator(
        hass=hass,
        client=client,
        entry_id=entry.entry_id,
        vehicles_config=configured_vehicles,
    )

    # Initial data load
    await coordinator.async_config_entry_first_refresh()

    # Setup session trackers for each configured vehicle
    trackers: dict[str, AutoLedgerSessionTracker] = {}
    for vehicle_id, vehicle_conf in configured_vehicles.items():
        tracker = AutoLedgerSessionTracker(
            hass=hass,
            client=client,
            vehicle_id=vehicle_id,
            config=vehicle_conf,
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
