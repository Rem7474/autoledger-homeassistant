"""AutoLedger Home Assistant Custom Component."""

from __future__ import annotations

import logging
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import (
    AUTH_METHOD_TOKEN,
    CONF_AUTH_METHOD,
    CONF_CHARGERS,
    CONF_VEHICLES,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import AutoLedgerDataUpdateCoordinator
from .oauth import build_client
from .odometer_tracker import AutoLedgerOdometerTracker
from .services import async_setup_services, async_unload_services
from .session_tracker import AutoLedgerChargerTracker
from .subentries import chargers_config, plan_subentries_from_options, vehicles_config
from .trip_tracker import AutoLedgerTripTracker

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the AutoLedger integration component."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Bring an entry created by an older version up to the current one.

    Version 2 moves vehicles and charging stations from the options to
    subentries and records how the entry authenticates. The entry id, the
    entity unique ids and the names of the stored sessions do not change. The
    version is raised last, so a migration interrupted halfway is replayed and
    skips the subentries it had already created.
    """
    if entry.version > 2:
        # Written by a newer version of the integration: do not guess its layout.
        return False

    if entry.version == 1:
        existing = {(sub.subentry_type, sub.unique_id) for sub in entry.subentries.values()}
        for subentry_type, unique_id, title, data in plan_subentries_from_options(entry.options):
            if (subentry_type, unique_id) in existing:
                continue
            hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data=MappingProxyType(data),
                    subentry_type=subentry_type,
                    title=title,
                    unique_id=unique_id,
                ),
            )
        options = {
            k: v for k, v in entry.options.items() if k not in (CONF_VEHICLES, CONF_CHARGERS)
        }
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_AUTH_METHOD: AUTH_METHOD_TOKEN},
            options=options,
            version=2,
        )
        _LOGGER.info("Migrated AutoLedger entry %s to version 2", entry.entry_id)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up AutoLedger from a config entry."""
    client = build_client(hass, entry)

    configured_vehicles = vehicles_config(entry)
    configured_chargers = chargers_config(entry)

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

    entry_data = {
        "config_signature": _config_signature(entry),
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


def _config_signature(entry: ConfigEntry) -> tuple[Any, ...]:
    """What a reload would change: everything stored on the entry except the OAuth token.

    An OAuth access token lasts one hour and the refreshed one is written to the entry:
    that must not restart the integration every hour.
    """
    data = {k: v for k, v in entry.data.items() if k != "token"}
    subentries = {
        sub_id: (sub.subentry_type, sub.unique_id, sub.title, dict(sub.data))
        for sub_id, sub in entry.subentries.items()
    }
    return (data, dict(entry.options), subentries, entry.title)


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its configuration changed (not when its token was refreshed)."""
    current = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("config_signature")
    if current is not None and current == _config_signature(entry):
        return
    await hass.config_entries.async_reload(entry.entry_id)
