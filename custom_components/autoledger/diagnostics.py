"""Diagnostics support for AutoLedger."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_API_KEY, DOMAIN
from .coordinator import AutoLedgerDataUpdateCoordinator
from .session_tracker import AutoLedgerSessionTracker

TO_REDACT = {
    CONF_API_KEY,
    "api_key",
    "token",
    "authorization",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = (
        entry.runtime_data
        if hasattr(entry, "runtime_data") and entry.runtime_data
        else hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    )

    coordinator: AutoLedgerDataUpdateCoordinator | None = data.get("coordinator")
    trackers: dict[str, AutoLedgerSessionTracker] = data.get("trackers", {})

    trackers_diagnostics: dict[str, Any] = {}
    for vid, tracker in trackers.items():
        trackers_diagnostics[vid] = {
            "state": tracker.state,
            "session_start_time": tracker.session_start_time,
            "soc_start": tracker.soc_start,
            "odometer_start": tracker.odometer_start,
            "energy_start": tracker.energy_start,
            "debounce_seconds": tracker.debounce_seconds,
            "debounce_timer_active": tracker._debounce_unsub is not None,
            "sync_status": tracker.sync_status,
            "last_successful_sync": tracker.last_successful_sync,
            "last_error_message": tracker.last_error_message,
            "pending_events_count": tracker.pending_events_count,
            "config": tracker.config,
        }

    return {
        "entry": {
            "entry_id": entry.entry_id,
            "version": entry.version,
            "domain": entry.domain,
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "coordinator_data": coordinator.data if coordinator else None,
        "trackers": trackers_diagnostics,
    }
