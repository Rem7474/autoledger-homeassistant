"""Charge session tracker with debounce and solar pause handling."""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Any

from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .api import AutoLedgerApiClient
from .const import (
    CONF_BATTERY_SOC_ENTITY,
    CONF_CHARGING_LOCATION,
    CONF_CHARGING_STATUS_ENTITY,
    CONF_DEBOUNCE_SECONDS,
    CONF_ENERGY_METER_ENTITY,
    CONF_ENERGY_METER_TYPE,
    CONF_LOCATION_ENTITY,
    CONF_ODOMETER_ENTITY,
    DEFAULT_CHARGING_LOCATION,
    DEFAULT_DEBOUNCE_SECONDS,
    ENERGY_METER_TYPE_SESSION,
    ENERGY_METER_TYPE_TOTAL_INCREASING,
    STATE_CHARGING,
    STATE_COOLING_DOWN,
    STATE_IDLE,
    SYNC_STATUS_ERROR,
    SYNC_STATUS_OK,
    SYNC_STATUS_PENDING,
)

_LOGGER = logging.getLogger(__name__)

CHARGING_POSITIVE_STATES = {
    "on",
    "charging",
    "true",
    "1",
    "active",
}

CHARGING_NEGATIVE_STATES = {
    "off",
    "idle",
    "disconnected",
    "not_charging",
    "false",
    "0",
    "complete",
    "stopped",
}


class AutoLedgerSessionTracker:
    """Manages charge detection, solar anti-bounce debounce, and AutoLedger event dispatch."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: AutoLedgerApiClient,
        vehicle_id: str,
        config: dict[str, Any],
        coordinator: Any | None = None,
    ) -> None:
        """Initialize the session tracker."""
        self.hass = hass
        self.client = client
        self.vehicle_id = vehicle_id
        self.config = config
        self.coordinator = coordinator

        # Entity IDs
        self.charging_status_entity: str | None = config.get(CONF_CHARGING_STATUS_ENTITY)
        self.battery_soc_entity: str | None = config.get(CONF_BATTERY_SOC_ENTITY)
        self.odometer_entity: str | None = config.get(CONF_ODOMETER_ENTITY)
        self.energy_meter_entity: str | None = config.get(CONF_ENERGY_METER_ENTITY)
        self.energy_meter_type: str = config.get(
            CONF_ENERGY_METER_TYPE, ENERGY_METER_TYPE_TOTAL_INCREASING
        )
        self.location_entity: str | None = config.get(CONF_LOCATION_ENTITY)
        self.default_location: str = config.get(
            CONF_CHARGING_LOCATION, DEFAULT_CHARGING_LOCATION
        )
        self.debounce_seconds: int = int(
            config.get(CONF_DEBOUNCE_SECONDS, DEFAULT_DEBOUNCE_SECONDS)
        )

        # State tracking
        self.state: str = STATE_IDLE
        self.session_start_time: str | None = None
        self.soc_start: int | None = None
        self.soc_end: int | None = None
        self.odometer_start: float | None = None
        self.energy_start: float | None = None
        self.energy_last_seen: float | None = None

        # Debounce timer
        self._debounce_unsub: CALLBACK_TYPE | None = None

        # Sync status & metrics
        self.sync_status: str = SYNC_STATUS_OK
        self.last_successful_sync: str | None = None
        self.last_error_message: str | None = None
        self.pending_events_count: int = 0

        # Unsub callbacks
        self._listeners: list[Callable[[], None]] = []
        self._unsub_trackers: list[CALLBACK_TYPE] = []

    def register_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Register a callback for status updates."""
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    def _notify_listeners(self) -> None:
        """Notify all registered listeners."""
        for listener in list(self._listeners):
            try:
                listener()
            except Exception as err:
                _LOGGER.warning("Error notifying tracker listener: %s", err)

    def _get_entity_numeric_state(self, entity_id: str | None) -> float | None:
        """Extract a float value from an entity state."""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", None):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    def _get_entity_int_state(self, entity_id: str | None) -> int | None:
        """Extract an integer value from an entity state."""
        val = self._get_entity_numeric_state(entity_id)
        return int(round(val)) if val is not None else None

    def _get_location(self) -> str:
        """Determine charging location."""
        if self.location_entity:
            state = self.hass.states.get(self.location_entity)
            if state and state.state not in ("unknown", "unavailable", None):
                return str(state.state)
        return self.default_location

    async def async_setup(self) -> None:
        """Start tracking state changes."""
        if not self.charging_status_entity:
            _LOGGER.debug(
                "No charging status entity configured for vehicle %s", self.vehicle_id
            )
            return

        unsub = async_track_state_change_event(
            self.hass,
            [self.charging_status_entity],
            self._async_on_charging_status_changed,
        )
        self._unsub_trackers.append(unsub)
        _LOGGER.debug(
            "Tracking charging status on %s for vehicle %s",
            self.charging_status_entity,
            self.vehicle_id,
        )

    async def async_unload(self) -> None:
        """Unload tracker and cleanup timers."""
        if self._debounce_unsub is not None:
            self._debounce_unsub()
            self._debounce_unsub = None

        for unsub in self._unsub_trackers:
            unsub()
        self._unsub_trackers.clear()
        self._listeners.clear()

    async def _async_on_charging_status_changed(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """Handle state change of the charging status entity."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")

        if new_state is None:
            return

        new_val = str(new_state.state).lower()
        old_val = str(old_state.state).lower() if old_state else ""

        if new_val == old_val:
            return

        is_charging = new_val in CHARGING_POSITIVE_STATES
        is_stopped = new_val in CHARGING_NEGATIVE_STATES

        _LOGGER.debug(
            "Charging status changed for vehicle %s: %s -> %s (is_charging=%s, is_stopped=%s)",
            self.vehicle_id,
            old_val,
            new_val,
            is_charging,
            is_stopped,
        )

        if is_charging:
            await self._async_handle_charge_started()
        elif is_stopped or not is_charging:
            await self._async_handle_charge_paused_or_stopped()

    async def _async_handle_charge_started(self) -> None:
        """Handle transition to active charging."""
        if self.state == STATE_COOLING_DOWN:
            # Solar pause interrupted or charge quickly resumed!
            _LOGGER.info(
                "Charging resumed for vehicle %s during debounce interval. Cancelling timer.",
                self.vehicle_id,
            )
            if self._debounce_unsub is not None:
                self._debounce_unsub()
                self._debounce_unsub = None
            self.state = STATE_CHARGING
            self._notify_listeners()
            return

        if self.state == STATE_IDLE:
            # New charging session starts
            self.state = STATE_CHARGING
            self.session_start_time = dt_util.utcnow().isoformat()
            self.soc_start = self._get_entity_int_state(self.battery_soc_entity)
            self.odometer_start = self._get_entity_numeric_state(self.odometer_entity)
            self.energy_start = self._get_entity_numeric_state(self.energy_meter_entity)
            self.energy_last_seen = self.energy_start

            _LOGGER.info(
                "Started charging session for vehicle %s (SoC=%s%%, Odo=%s km, EnergyStart=%s kWh)",
                self.vehicle_id,
                self.soc_start,
                self.odometer_start,
                self.energy_start,
            )
            self._notify_listeners()

    async def _async_handle_charge_paused_or_stopped(self) -> None:
        """Handle transition away from charging (pause or complete)."""
        if self.state != STATE_CHARGING:
            return

        # Keep last known energy reading
        latest_energy = self._get_entity_numeric_state(self.energy_meter_entity)
        if latest_energy is not None:
            self.energy_last_seen = latest_energy

        self.state = STATE_COOLING_DOWN
        _LOGGER.info(
            "Charging paused for vehicle %s. Arming anti-bounce timer (%s seconds)",
            self.vehicle_id,
            self.debounce_seconds,
        )
        self._notify_listeners()

        # Arm non-blocking timer
        self._debounce_unsub = async_call_later(
            self.hass,
            float(self.debounce_seconds),
            self._async_handle_debounce_expired,
        )

    async def _async_handle_debounce_expired(self, _now: Any) -> None:
        """Debounce timer expired; finalize and send session to AutoLedger."""
        self._debounce_unsub = None
        _LOGGER.info(
            "Debounce timer expired for vehicle %s. Finalizing charging session.",
            self.vehicle_id,
        )
        await self.async_finalize_session()

    async def async_finalize_session(self) -> None:
        """Compute session delta and post event to AutoLedger."""
        end_time = dt_util.utcnow().isoformat()
        start_time = self.session_start_time or end_time

        # Capture final states
        soc_end = self._get_entity_int_state(self.battery_soc_entity)
        odometer_end = self._get_entity_numeric_state(self.odometer_entity)
        if odometer_end is None:
            odometer_end = self.odometer_start

        current_energy = self._get_entity_numeric_state(self.energy_meter_entity)
        if current_energy is None:
            current_energy = self.energy_last_seen

        # Calculate energy added
        energy_added_kwh: float = 0.0
        if self.energy_meter_type == ENERGY_METER_TYPE_TOTAL_INCREASING:
            if current_energy is not None and self.energy_start is not None:
                energy_added_kwh = max(0.0, current_energy - self.energy_start)
            elif current_energy is not None:
                energy_added_kwh = 0.0
        elif self.energy_meter_type == ENERGY_METER_TYPE_SESSION:
            if current_energy is not None:
                energy_added_kwh = max(0.0, current_energy)

        location = self._get_location()

        payload = {
            "vehicle_id": self.vehicle_id,
            "event_type": "charging_session_end",
            "source": "homeassistant",
            "timestamp": end_time,
            "data": {
                "start_time": start_time,
                "end_time": end_time,
                "energy_added_kwh": round(energy_added_kwh, 3),
                "odometer_km": round(odometer_end, 1) if odometer_end is not None else None,
                "soc_start": self.soc_start,
                "soc_end": soc_end,
                "location": location,
                "meter_device_id": self.energy_meter_entity,
                "cost": None,
            },
        }

        _LOGGER.info(
            "Emitting charge session payload for vehicle %s: %s kWh, SoC: %s->%s",
            self.vehicle_id,
            payload["data"]["energy_added_kwh"],
            self.soc_start,
            soc_end,
        )

        self.state = STATE_IDLE
        self.sync_status = SYNC_STATUS_PENDING
        self.pending_events_count += 1
        self._notify_listeners()

        try:
            await self.client.async_post_event(payload)
            self.sync_status = SYNC_STATUS_OK
            self.last_successful_sync = dt_util.utcnow().isoformat()
            self.last_error_message = None
            self.pending_events_count = max(0, self.pending_events_count - 1)
            _LOGGER.info("Successfully sent charge session to AutoLedger")

            if self.coordinator is not None:
                await self.coordinator.async_request_refresh()

        except Exception as err:
            self.sync_status = SYNC_STATUS_ERROR
            self.last_error_message = str(err)
            _LOGGER.error("Failed to post charge event to AutoLedger: %s", err)

        finally:
            self._reset_session_data()
            self._notify_listeners()

    def _reset_session_data(self) -> None:
        """Reset internal session registers."""
        self.session_start_time = None
        self.soc_start = None
        self.soc_end = None
        self.odometer_start = None
        self.energy_start = None
        self.energy_last_seen = None

    async def async_submit_manual_charge(
        self,
        kwh: float,
        cost: float | None = None,
        odometer_km: float | None = None,
        soc_start: int | None = None,
        soc_end: int | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        location: str | None = None,
    ) -> dict[str, Any]:
        """Submit a manual charging session."""
        loc = location or self._get_location()
        res = await self.client.async_submit_charge(
            vehicle_id=self.vehicle_id,
            kwh=kwh,
            cost=cost,
            odometer_km=odometer_km,
            soc_start=soc_start,
            soc_end=soc_end,
            start_time=start_time,
            end_time=end_time,
            location=loc,
        )
        self.last_successful_sync = dt_util.utcnow().isoformat()
        self.sync_status = SYNC_STATUS_OK
        self._notify_listeners()
        if self.coordinator is not None:
            await self.coordinator.async_request_refresh()
        return res
