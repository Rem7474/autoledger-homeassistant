"""Odometer tracking and trip-end stabilization for AutoLedger."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .api import AutoLedgerApiClient
from .const import (
    CONF_ODOMETER_ENTITY,
    DEFAULT_TRIP_END_DEBOUNCE_SECONDS,
)

if TYPE_CHECKING:
    from .coordinator import AutoLedgerDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

SYNC_RETRY_SECONDS = 300


def get_entity_odometer_km(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Extract and convert odometer reading to kilometers."""
    if not entity_id:
        return None
    state_obj = hass.states.get(entity_id)
    if state_obj is None or state_obj.state in ("unknown", "unavailable", None):
        return None
    try:
        val = float(state_obj.state)
        if val < 0:
            return None
        unit = (state_obj.attributes.get("unit_of_measurement") or "km").lower().strip()
        if unit == "mi":
            val = val * 1.609344
        elif unit == "m":
            val = val / 1000.0
        return round(val, 1)
    except (ValueError, TypeError):
        return None


class AutoLedgerOdometerTracker:
    """Tracks vehicle odometer sensors and syncs upon stabilization (trip-end)."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: AutoLedgerApiClient,
        vehicles_config: dict[str, Any],
        coordinator: AutoLedgerDataUpdateCoordinator | None = None,
        debounce_seconds: int = DEFAULT_TRIP_END_DEBOUNCE_SECONDS,
    ) -> None:
        """Initialize the odometer tracker."""
        self.hass = hass
        self.client = client
        self.vehicles_config = vehicles_config or {}
        self.coordinator = coordinator
        self.debounce_seconds = debounce_seconds

        self._last_synced_odometer: dict[str, float] = {}
        self._pending_odometer: dict[str, float] = {}
        self._debounce_timers: dict[str, Callable[[], None]] = {}
        self._retry_timers: dict[str, Callable[[], None]] = {}
        self._unsub_trackers: list[Callable[[], None]] = []
        self._last_sync_time: dict[str, datetime] = {}
        self._listeners: list[Callable[[], None]] = []

    def last_sync_time(self, vehicle_id: str) -> datetime | None:
        """Return when an odometer reading was last pushed successfully for a vehicle."""
        return self._last_sync_time.get(vehicle_id)

    def register_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a callback called after each successful sync; returns an unsubscribe."""
        self._listeners.append(callback)

        def _unsub() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return _unsub

    def set_last_synced_odometer(self, vehicle_id: str, km: float | None) -> None:
        """Record the latest odometer value synced by another component (e.g. charging session)."""
        if km is not None and km > 0:
            current_max = self._last_synced_odometer.get(vehicle_id, 0.0)
            if km > current_max:
                self._last_synced_odometer[vehicle_id] = km

    async def async_setup(self) -> None:
        """Start tracking odometer entity state changes and perform initial sync."""
        # Prime last synced odometer with known backend values from coordinator if available
        if self.coordinator and self.coordinator.data:
            vehicles_dict = self.coordinator.data.get("vehicles", {})
            for vid, vdata in vehicles_dict.items():
                backend_odo = vdata.get("current_odometer")
                if backend_odo and float(backend_odo) > 0:
                    self.set_last_synced_odometer(str(vid), float(backend_odo))

        for vehicle_id, vconf in self.vehicles_config.items():
            if not vehicle_id:
                continue
            odometer_entity = vconf.get(CONF_ODOMETER_ENTITY)
            if not odometer_entity:
                continue

            # 1. Listen for state changes (with trip-end debounce)
            unsub = async_track_state_change_event(
                self.hass,
                [odometer_entity],
                self._async_on_odometer_changed,
            )
            self._unsub_trackers.append(unsub)

            # 2. Initial synchronization on setup/configuration
            current_km = get_entity_odometer_km(self.hass, odometer_entity)
            if current_km is not None and current_km > 0:
                last_synced = self._last_synced_odometer.get(vehicle_id, 0.0)
                if current_km > last_synced:
                    await self.async_sync_vehicle(vehicle_id, current_km, reason="initial_setup")

    async def async_unload(self) -> None:
        """Cancel pending debounce timers and unsubscribe event listeners."""
        for cancel in self._debounce_timers.values():
            cancel()
        self._debounce_timers.clear()
        for cancel in self._retry_timers.values():
            cancel()
        self._retry_timers.clear()

        for unsub in self._unsub_trackers:
            unsub()
        self._unsub_trackers.clear()

    async def _async_on_odometer_changed(self, event: Event[EventStateChangedData]) -> None:
        """Handle state change event for a tracked odometer sensor."""
        entity_id = event.data.get("entity_id")
        if not entity_id:
            return

        # Locate which vehicle this entity belongs to
        target_vid: str | None = None
        for vid, vconf in self.vehicles_config.items():
            if vconf.get(CONF_ODOMETER_ENTITY) == entity_id:
                target_vid = vid
                break

        if not target_vid:
            return

        new_km = get_entity_odometer_km(self.hass, entity_id)
        if new_km is None or new_km <= 0:
            return

        last_synced = self._last_synced_odometer.get(target_vid, 0.0)
        # If the odometer hasn't advanced compared to what AutoLedger knows, do nothing
        if new_km <= last_synced:
            return

        # Store pending stable target
        self._pending_odometer[target_vid] = new_km

        # Vehicle is driving: cancel existing timer and restart debounce
        if target_vid in self._debounce_timers:
            self._debounce_timers[target_vid]()
            del self._debounce_timers[target_vid]

        _LOGGER.debug(
            "Odometer update detected for vehicle %s: %.1f km. Debounce timer started (%ss).",
            target_vid,
            new_km,
            self.debounce_seconds,
        )

        async def _on_debounce_expired(_now: Any, vid: str = target_vid) -> None:
            self._debounce_timers.pop(vid, None)
            await self._async_handle_debounce_expired(vid)

        self._debounce_timers[target_vid] = async_call_later(
            self.hass,
            float(self.debounce_seconds),
            _on_debounce_expired,
        )

    async def _async_handle_debounce_expired(self, vehicle_id: str) -> None:
        """Debounce expired: the vehicle has been parked/stable for debounce_seconds."""
        vconf = self.vehicles_config.get(vehicle_id, {})
        entity_id = vconf.get(CONF_ODOMETER_ENTITY)
        current_km = get_entity_odometer_km(self.hass, entity_id)

        target_km = self._pending_odometer.pop(vehicle_id, None)
        if current_km is not None and current_km > 0:
            target_km = max(target_km or 0.0, current_km)

        if target_km is None or target_km <= 0:
            return

        last_synced = self._last_synced_odometer.get(vehicle_id, 0.0)
        if target_km > last_synced:
            await self.async_sync_vehicle(vehicle_id, target_km, reason="trip_end")
            if self.coordinator:
                await self.coordinator.async_request_refresh()

    async def async_sync_vehicle(
        self,
        vehicle_id: str,
        odometer_km: float | None = None,
        reason: str = "manual",
    ) -> bool:
        """Push an updated odometer reading to AutoLedger."""
        if odometer_km is None:
            vconf = self.vehicles_config.get(vehicle_id, {})
            odometer_km = get_entity_odometer_km(self.hass, vconf.get(CONF_ODOMETER_ENTITY))

        if odometer_km is None or odometer_km <= 0:
            _LOGGER.debug(
                "Cannot sync vehicle %s: invalid odometer value %s", vehicle_id, odometer_km
            )
            return False

        try:
            _LOGGER.info(
                "Syncing odometer for vehicle %s to AutoLedger: %.1f km (reason=%s)",
                vehicle_id,
                odometer_km,
                reason,
            )
            await self.client.async_update_odometer(
                vehicle_id=vehicle_id,
                odometer_km=odometer_km,
            )
            self._last_synced_odometer[vehicle_id] = odometer_km
            self._last_sync_time[vehicle_id] = dt_util.utcnow()
            self._pending_odometer.pop(vehicle_id, None)
            for listener in list(self._listeners):
                listener()
            cancel = self._retry_timers.pop(vehicle_id, None)
            if cancel:
                cancel()
            return True
        except Exception as err:
            _LOGGER.warning(
                "Failed to sync odometer for vehicle %s (%.1f km), retrying in %ss: %s",
                vehicle_id,
                odometer_km,
                SYNC_RETRY_SECONDS,
                err,
            )
            self._schedule_retry(vehicle_id)
            return False

    def _schedule_retry(self, vehicle_id: str) -> None:
        """The sensor may not change again: a failed sync is tried again on its own."""
        if vehicle_id in self._retry_timers:
            return

        async def _on_retry(_now: Any) -> None:
            self._retry_timers.pop(vehicle_id, None)
            await self._async_retry(vehicle_id)

        self._retry_timers[vehicle_id] = async_call_later(
            self.hass, float(SYNC_RETRY_SECONDS), _on_retry
        )

    async def _async_retry(self, vehicle_id: str) -> None:
        vconf = self.vehicles_config.get(vehicle_id, {})
        km = get_entity_odometer_km(self.hass, vconf.get(CONF_ODOMETER_ENTITY))
        if km is not None and km > self._last_synced_odometer.get(vehicle_id, 0.0):
            await self.async_sync_vehicle(vehicle_id, km, reason="retry")
            if self.coordinator:
                await self.coordinator.async_request_refresh()
