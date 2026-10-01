"""Charge session tracker with debounce and solar pause handling for charging stations."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .api import AutoLedgerApiClient
from .const import (
    ASSIGNMENT_MODE_CORRELATION,
    ASSIGNMENT_MODE_FIXED,
    ASSIGNMENT_MODE_INPUT_SELECT,
    ASSIGNMENT_MODE_UNASSIGNED,
    CONF_ASSIGNMENT_MODE,
    CONF_BATTERY_SOC_ENTITY,
    CONF_CHARGER_NAME,
    CONF_CHARGING_LOCATION,
    CONF_CHARGING_STATUS_ENTITY,
    CONF_DEBOUNCE_SECONDS,
    CONF_ENERGY_METER_ENTITY,
    CONF_ENERGY_METER_TYPE,
    CONF_LINKED_VEHICLE_ID,
    CONF_LOCATION_ENTITY,
    CONF_ODOMETER_ENTITY,
    CONF_VEHICLE_NAME,
    CONF_VEHICLE_SELECT_ENTITY,
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
from .odometer_tracker import get_entity_odometer_km

_LOGGER = logging.getLogger(__name__)

CHARGING_POSITIVE_STATES = {
    "on",
    "charging",
    "true",
    "1",
    "active",
}

# States of an entity that is offline or not reporting yet: they tell nothing about charging
UNKNOWN_STATES = {"unavailable", "unknown"}

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


class AutoLedgerChargerTracker:
    """Manages charging station detection, solar anti-bounce debounce, vehicle resolution, and event dispatch."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: AutoLedgerApiClient,
        charger_id: str | None = None,
        config: dict[str, Any] | None = None,
        vehicles_config: dict[str, Any] | None = None,
        coordinator: Any | None = None,
        vehicle_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the charger tracker."""
        self.hass = hass
        self.client = client
        self.charger_id = charger_id or vehicle_id or "charger"
        self.config = config or {}
        self.vehicles_config: dict[str, Any] = vehicles_config or {}
        self.coordinator = coordinator

        # Charger configuration
        self.charger_name: str = config.get(
            CONF_CHARGER_NAME, config.get(CONF_VEHICLE_NAME, charger_id)
        )
        self.charging_status_entity: str | None = config.get(CONF_CHARGING_STATUS_ENTITY)
        self.energy_meter_entity: str | None = config.get(CONF_ENERGY_METER_ENTITY)
        self.energy_meter_type: str = config.get(
            CONF_ENERGY_METER_TYPE, ENERGY_METER_TYPE_TOTAL_INCREASING
        )
        self.location_entity: str | None = config.get(CONF_LOCATION_ENTITY)
        self.default_location: str = config.get(CONF_CHARGING_LOCATION, DEFAULT_CHARGING_LOCATION)
        self.debounce_seconds: int = int(
            config.get(CONF_DEBOUNCE_SECONDS, DEFAULT_DEBOUNCE_SECONDS)
        )

        # Assignment mode determination
        configured_mode = config.get(CONF_ASSIGNMENT_MODE)
        if configured_mode:
            self.assignment_mode = configured_mode
        elif config.get(CONF_LINKED_VEHICLE_ID):
            self.assignment_mode = ASSIGNMENT_MODE_FIXED
        elif config.get(CONF_VEHICLE_SELECT_ENTITY):
            self.assignment_mode = ASSIGNMENT_MODE_INPUT_SELECT
        elif charger_id in self.vehicles_config:
            self.assignment_mode = ASSIGNMENT_MODE_FIXED
        elif config.get(CONF_BATTERY_SOC_ENTITY) or config.get(CONF_ODOMETER_ENTITY):
            self.assignment_mode = ASSIGNMENT_MODE_FIXED
        else:
            self.assignment_mode = ASSIGNMENT_MODE_UNASSIGNED

        self.linked_vehicle_id: str | None = config.get(
            CONF_LINKED_VEHICLE_ID,
            charger_id if self.assignment_mode == ASSIGNMENT_MODE_FIXED else None,
        )
        self.vehicle_select_entity: str | None = config.get(CONF_VEHICLE_SELECT_ENTITY)

        # State tracking
        self.state: str = STATE_IDLE
        self.session_start_time: str | None = None
        self.session_stop_time: str | None = None
        self.energy_start: float | None = None
        self.energy_last_seen: float | None = None
        self.last_energy_kwh: float | None = None

        # Telemetry snapshots per vehicle (recorded at charge start)
        self._vehicles_soc_start: dict[str, int | None] = {}
        self._vehicles_odometer_start: dict[str, float | None] = {}

        # Fallback values for single-vehicle / legacy setups
        self.soc_start: int | None = None
        self.odometer_start: float | None = None

        # Correlation tracking
        self._correlated_vehicle_id: str | None = None

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

    @property
    def vehicle_id(self) -> str | None:
        """Return linked vehicle id or charger id for backward compatibility."""
        return self.linked_vehicle_id or self.charger_id

    @property
    def location(self) -> str:
        """Determine charging location."""
        if self.location_entity:
            state = self.hass.states.get(self.location_entity)
            if state and state.state not in ("unknown", "unavailable", None):
                return str(state.state)
        return self.default_location

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

    def _is_state_charging(self, val_str: str, entity_id: str | None = None) -> bool:
        """Check if an entity state represents an active charging condition."""
        val = str(val_str).lower().strip()
        if val in CHARGING_POSITIVE_STATES:
            return True
        if val in CHARGING_NEGATIVE_STATES:
            return False

        # Support power sensors (e.g. W with > 500W threshold or kW with > 0.5kW threshold)
        try:
            num = float(val)
            if num > 500:
                return True
            if entity_id:
                state_obj = self.hass.states.get(entity_id)
                unit = (
                    (state_obj.attributes.get("unit_of_measurement") or "").lower()
                    if state_obj
                    else ""
                )
                if "kw" in unit and num > 0.5:
                    return True
            return False
        except (ValueError, TypeError):
            return False

    def _is_vehicle_charging(self, vconf: dict[str, Any]) -> bool:
        """Check if a specific vehicle's internal charging status is positive."""
        entity_id = vconf.get(CONF_CHARGING_STATUS_ENTITY)
        if not entity_id:
            return False
        state = self.hass.states.get(entity_id)
        if not state or state.state in ("unknown", "unavailable", None):
            return False
        return self._is_state_charging(state.state, entity_id)

    def _check_correlation(self) -> None:
        """Check configured vehicles to identify if exactly one is charging."""
        if self.assignment_mode != ASSIGNMENT_MODE_CORRELATION:
            return
        active_vehicles = [
            vid for vid, vconf in self.vehicles_config.items() if self._is_vehicle_charging(vconf)
        ]
        if len(active_vehicles) == 1:
            self._correlated_vehicle_id = active_vehicles[0]

    async def async_setup(self) -> None:
        """Start tracking charger state changes."""
        if not self.charging_status_entity:
            _LOGGER.debug("No charging status entity configured for charger %s", self.charger_id)
            return

        unsub = async_track_state_change_event(
            self.hass,
            [self.charging_status_entity],
            self._async_on_charging_status_changed,
        )
        self._unsub_trackers.append(unsub)
        _LOGGER.debug(
            "Tracking charging status on %s for charger %s",
            self.charging_status_entity,
            self.charger_id,
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

    async def _async_on_charging_status_changed(self, event: Event[EventStateChangedData]) -> None:
        """Handle state change of the charging status entity."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")

        if new_state is None:
            return

        new_val = str(new_state.state)
        old_val = str(old_state.state) if old_state else ""

        if new_val == old_val:
            return
        if new_val.lower() in UNKNOWN_STATES:
            # The charger dropped off the network: not a stop, the session goes on when it comes back
            _LOGGER.debug("Charger %s is %s, keeping the session as is", self.charger_id, new_val)
            return

        is_charging = self._is_state_charging(new_val, self.charging_status_entity)

        _LOGGER.debug(
            "Charging status changed for charger %s: %s -> %s (is_charging=%s)",
            self.charger_id,
            old_val,
            new_val,
            is_charging,
        )

        if is_charging:
            await self._async_handle_charge_started()
        else:
            await self._async_handle_charge_paused_or_stopped()

    async def _async_handle_charge_started(self) -> None:
        """Handle transition to active charging."""
        if self.state == STATE_COOLING_DOWN:
            _LOGGER.info(
                "Charging resumed for charger %s during debounce interval. Cancelling timer.",
                self.charger_id,
            )
            if self._debounce_unsub is not None:
                self._debounce_unsub()
                self._debounce_unsub = None
            self.state = STATE_CHARGING
            self._check_correlation()
            self._notify_listeners()
            return

        if self.state == STATE_IDLE:
            self.state = STATE_CHARGING
            self.session_start_time = dt_util.utcnow().isoformat()
            self.energy_start = self._get_entity_numeric_state(self.energy_meter_entity)
            self.energy_last_seen = self.energy_start
            self._correlated_vehicle_id = None

            # Capture start metrics for all configured vehicles
            self._vehicles_soc_start.clear()
            self._vehicles_odometer_start.clear()
            for vid, vconf in self.vehicles_config.items():
                self._vehicles_soc_start[vid] = self._get_entity_int_state(
                    vconf.get(CONF_BATTERY_SOC_ENTITY)
                )
                self._vehicles_odometer_start[vid] = self._get_entity_numeric_state(
                    vconf.get(CONF_ODOMETER_ENTITY)
                )

            # Fallback for standalone/legacy single-vehicle config
            if self.linked_vehicle_id and self.linked_vehicle_id in self.vehicles_config:
                self.soc_start = self._vehicles_soc_start.get(self.linked_vehicle_id)
                self.odometer_start = self._vehicles_odometer_start.get(self.linked_vehicle_id)
            elif self.config.get(CONF_BATTERY_SOC_ENTITY) or self.config.get(CONF_ODOMETER_ENTITY):
                self.soc_start = self._get_entity_int_state(
                    self.config.get(CONF_BATTERY_SOC_ENTITY)
                )
                self.odometer_start = self._get_entity_numeric_state(
                    self.config.get(CONF_ODOMETER_ENTITY)
                )
                target_key = self.linked_vehicle_id or self.charger_id
                self._vehicles_soc_start[target_key] = self.soc_start
                self._vehicles_odometer_start[target_key] = self.odometer_start

            self._check_correlation()

            _LOGGER.info(
                "Started charging session on charger %s (EnergyStart=%s kWh)",
                self.charger_id,
                self.energy_start,
            )
            self._notify_listeners()

    async def _async_handle_charge_paused_or_stopped(self) -> None:
        """Handle transition away from charging (pause or complete)."""
        if self.state != STATE_CHARGING:
            return

        latest_energy = self._get_entity_numeric_state(self.energy_meter_entity)
        if latest_energy is not None:
            self.energy_last_seen = latest_energy

        if self._correlated_vehicle_id is None:
            self._check_correlation()

        # The session ends when charging stopped, not when the anti-bounce delay runs out
        self.session_stop_time = dt_util.utcnow().isoformat()
        self.state = STATE_COOLING_DOWN
        _LOGGER.info(
            "Charging paused on charger %s. Arming anti-bounce timer (%s seconds)",
            self.charger_id,
            self.debounce_seconds,
        )
        self._notify_listeners()

        self._debounce_unsub = async_call_later(
            self.hass,
            float(self.debounce_seconds),
            self._async_handle_debounce_expired,
        )

    async def _async_handle_debounce_expired(self, _now: Any) -> None:
        """Debounce timer expired; finalize and send session to AutoLedger."""
        self._debounce_unsub = None
        _LOGGER.info(
            "Debounce timer expired for charger %s. Finalizing charging session.",
            self.charger_id,
        )
        await self.async_finalize_session()

    async def async_finalize_session(self) -> None:
        """Compute session delta, resolve vehicle, and post event to AutoLedger."""
        end_time = self.session_stop_time or dt_util.utcnow().isoformat()
        start_time = self.session_start_time or end_time

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

        self.last_energy_kwh = round(energy_added_kwh, 3)

        if self.last_energy_kwh <= 0:
            # Plugged in without charging, or no energy meter: nothing to record (AutoLedger refuses 0 kWh)
            _LOGGER.info(
                "Charging session on charger %s added no energy, not sent", self.charger_id
            )
            self.state = STATE_IDLE
            self._reset_session_data()
            self._notify_listeners()
            return

        # Resolve vehicle and telemetry based on assignment_mode
        resolved_vehicle_id: str | None = None
        soc_start: int | None = None
        soc_end: int | None = None
        odometer_end: float | None = None

        if self.assignment_mode == ASSIGNMENT_MODE_FIXED:
            resolved_vehicle_id = self.linked_vehicle_id or self.config.get(CONF_LINKED_VEHICLE_ID)
            vconf = self.vehicles_config.get(resolved_vehicle_id) if resolved_vehicle_id else None
            if not vconf and (
                self.config.get(CONF_BATTERY_SOC_ENTITY) or self.config.get(CONF_ODOMETER_ENTITY)
            ):
                vconf = self.config

            if vconf and resolved_vehicle_id:
                soc_start = self._vehicles_soc_start.get(resolved_vehicle_id, self.soc_start)
                if soc_start is None:
                    soc_start = self._get_entity_int_state(vconf.get(CONF_BATTERY_SOC_ENTITY))
                soc_end = self._get_entity_int_state(vconf.get(CONF_BATTERY_SOC_ENTITY))
                odometer_end = get_entity_odometer_km(self.hass, vconf.get(CONF_ODOMETER_ENTITY))
                if odometer_end is None:
                    odometer_end = self._vehicles_odometer_start.get(
                        resolved_vehicle_id, self.odometer_start
                    )

        elif self.assignment_mode == ASSIGNMENT_MODE_INPUT_SELECT:
            select_entity = self.vehicle_select_entity or self.config.get(
                CONF_VEHICLE_SELECT_ENTITY
            )
            select_val = None
            if select_entity:
                state = self.hass.states.get(select_entity)
                if state and state.state not in ("unknown", "unavailable", None):
                    select_val = str(state.state).strip()

            if select_val:
                matched_id = None
                for vid, vconf in self.vehicles_config.items():
                    vname = str(vconf.get(CONF_VEHICLE_NAME, "")).strip()
                    if select_val.lower() == vid.lower() or (
                        vname and select_val.lower() == vname.lower()
                    ):
                        matched_id = vid
                        break

                if matched_id:
                    resolved_vehicle_id = matched_id
                    vconf = self.vehicles_config[matched_id]
                    soc_start = self._vehicles_soc_start.get(matched_id)
                    if soc_start is None:
                        soc_start = self._get_entity_int_state(vconf.get(CONF_BATTERY_SOC_ENTITY))
                    soc_end = self._get_entity_int_state(vconf.get(CONF_BATTERY_SOC_ENTITY))
                    odometer_end = get_entity_odometer_km(
                        self.hass, vconf.get(CONF_ODOMETER_ENTITY)
                    )
                    if odometer_end is None:
                        odometer_end = self._vehicles_odometer_start.get(matched_id)

        elif self.assignment_mode == ASSIGNMENT_MODE_CORRELATION:
            active_vehicles = [
                vid
                for vid, vconf in self.vehicles_config.items()
                if self._is_vehicle_charging(vconf)
            ]
            target_vid = None
            if len(active_vehicles) == 1:
                target_vid = active_vehicles[0]
            elif self._correlated_vehicle_id is not None:
                target_vid = self._correlated_vehicle_id

            if target_vid and target_vid in self.vehicles_config:
                resolved_vehicle_id = target_vid
                vconf = self.vehicles_config[target_vid]
                soc_start = self._vehicles_soc_start.get(target_vid)
                if soc_start is None:
                    soc_start = self._get_entity_int_state(vconf.get(CONF_BATTERY_SOC_ENTITY))
                soc_end = self._get_entity_int_state(vconf.get(CONF_BATTERY_SOC_ENTITY))
                odometer_end = get_entity_odometer_km(self.hass, vconf.get(CONF_ODOMETER_ENTITY))
                if odometer_end is None:
                    odometer_end = self._vehicles_odometer_start.get(target_vid)

        elif self.assignment_mode == ASSIGNMENT_MODE_UNASSIGNED:
            resolved_vehicle_id = None
            soc_start = None
            soc_end = None
            odometer_end = None

        payload = {
            # Same session, same identifier: AutoLedger recognises a resent session
            "event_id": f"{self.charger_id}:{start_time}",
            "vehicle_id": resolved_vehicle_id,
            "event_type": "charging_session_end",
            "source": "homeassistant",
            "timestamp": end_time,
            "data": {
                "charger_name": self.charger_name,
                "start_time": start_time,
                "end_time": end_time,
                "energy_added_kwh": round(energy_added_kwh, 3),
                "odometer_km": round(odometer_end, 1) if odometer_end is not None else None,
                "soc_start": soc_start,
                "soc_end": soc_end,
                "location": self.location,
                "meter_device_id": self.energy_meter_entity,
                "cost": None,
            },
        }

        _LOGGER.info(
            "Emitting charge session payload for charger %s (vehicle=%s): %s kWh, SoC: %s->%s",
            self.charger_id,
            resolved_vehicle_id,
            payload["data"]["energy_added_kwh"],
            soc_start,
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
                if resolved_vehicle_id and odometer_end is not None:
                    odometer_tracker = getattr(self.coordinator, "odometer_tracker", None)
                    if odometer_tracker:
                        odometer_tracker.set_last_synced_odometer(resolved_vehicle_id, odometer_end)

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
        self.session_stop_time = None
        self.energy_start = None
        self.energy_last_seen = None
        self.soc_start = None
        self.odometer_start = None
        self._vehicles_soc_start.clear()
        self._vehicles_odometer_start.clear()
        self._correlated_vehicle_id = None

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
        vehicle_id: str | None = None,
    ) -> dict[str, Any]:
        """Submit a manual charging session."""
        loc = location or self.location
        target_vid = vehicle_id or self.linked_vehicle_id or self.charger_id
        res = await self.client.async_submit_charge(
            vehicle_id=target_vid,
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


# Backward compatibility alias
AutoLedgerSessionTracker = AutoLedgerChargerTracker
