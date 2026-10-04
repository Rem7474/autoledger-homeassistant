"""Trip tracking: start/end position of each drive, sent to AutoLedger when it ends.

The distance is never computed here. AutoLedger derives it from the odometer
difference and records the trip without a distance when there is no odometer.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import AutoLedgerApiClient
from .const import (
    CONF_ODOMETER_ENTITY,
    CONF_TRIP_LOCATION_ENTITY,
    CONF_TRIP_MOVING_ENTITY,
    DEFAULT_TRIP_END_DEBOUNCE_SECONDS,
    TRIP_MIN_ODOMETER_KM,
    TRIP_MIN_SECONDS,
    TRIP_MOVE_THRESHOLD_M,
)
from .odometer_tracker import SYNC_RETRY_SECONDS, get_entity_odometer_km

if TYPE_CHECKING:
    from .coordinator import AutoLedgerDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

_INVALID_STATES = ("unknown", "unavailable", "none", "")
_ON_STATES = ("on", "true", "moving", "driving")
# Used when no moving sensor is configured: how long to wait before a stop is a trip end.
MOVING_SENSOR_END_DEBOUNCE_SECONDS = 60


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters, only used to tell "moved" from "GPS jitter"."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 6371000.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


def read_position(hass: HomeAssistant, entity_id: str | None) -> tuple[float, float] | None:
    """Return (lat, lon) of a device_tracker/person entity, or None when unusable."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or str(state.state).lower() in ("unavailable", "unknown"):
        return None
    attrs = state.attributes or {}
    try:
        lat = float(attrs.get("latitude"))
        lon = float(attrs.get("longitude"))
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return lat, lon


def zone_name_at(hass: HomeAssistant, lat: float, lon: float) -> str | None:
    """Friendly name of the smallest Home Assistant zone containing the point."""
    best: tuple[float, str] | None = None
    for zone in hass.states.async_all("zone"):
        attrs = zone.attributes or {}
        try:
            z_lat = float(attrs["latitude"])
            z_lon = float(attrs["longitude"])
            radius = float(attrs.get("radius", 100))
        except (KeyError, TypeError, ValueError):
            continue
        if haversine_m(lat, lon, z_lat, z_lon) <= radius:
            name = attrs.get("friendly_name") or zone.entity_id
            if name and (best is None or radius < best[0]):
                best = (radius, str(name))
    return best[1] if best else None


def _is_on(state: Any) -> bool | None:
    if state is None:
        return None
    value = str(state.state).lower()
    if value in _INVALID_STATES:
        return None
    return value in _ON_STATES


class AutoLedgerTripTracker:
    """Detects trips of vehicles that have a position entity and reports them."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: AutoLedgerApiClient,
        vehicles_config: dict[str, Any],
        store: Store | None = None,
        coordinator: AutoLedgerDataUpdateCoordinator | None = None,
        end_debounce_seconds: int = DEFAULT_TRIP_END_DEBOUNCE_SECONDS,
    ) -> None:
        self.hass = hass
        self.client = client
        self.coordinator = coordinator
        self.store = store
        self.end_debounce_seconds = end_debounce_seconds
        self.vehicles_config = {
            vid: conf
            for vid, conf in (vehicles_config or {}).items()
            if vid and conf.get(CONF_TRIP_LOCATION_ENTITY)
        }

        self._open: dict[str, dict[str, Any]] = {}
        self._parked: dict[str, dict[str, Any]] = {}
        self._pending: list[dict[str, Any]] = []
        self._debounce: dict[str, Callable[[], None]] = {}
        self._retry: Callable[[], None] | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._last_trip: dict[str, dict[str, Any]] = {}
        self._listeners: list[Callable[[], None]] = []

    # -- public API ---------------------------------------------------------

    def last_trip(self, vehicle_id: str) -> dict[str, Any] | None:
        return self._last_trip.get(vehicle_id)

    def register_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(callback)

        def _unsub() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return _unsub

    async def async_setup(self) -> None:
        await self._async_load()

        for vid, conf in self.vehicles_config.items():
            entities = [
                e
                for e in (
                    conf.get(CONF_TRIP_LOCATION_ENTITY),
                    conf.get(CONF_TRIP_MOVING_ENTITY),
                    conf.get(CONF_ODOMETER_ENTITY),
                )
                if e
            ]
            self._unsubs.append(
                async_track_state_change_event(self.hass, entities, self._async_on_change)
            )
            self._capture_parked(vid)
            if vid in self._open:
                # Home Assistant restarted mid-trip: let the debounce decide whether it ended.
                self._restart_debounce(vid)

        if self._pending:
            await self._async_flush_pending()

    async def async_unload(self) -> None:
        for cancel in self._debounce.values():
            cancel()
        self._debounce.clear()
        if self._retry:
            self._retry()
            self._retry = None
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    async def async_log_trip(
        self,
        vehicle_id: str,
        start_time: datetime,
        end_time: datetime,
        start: tuple[float, float] | None = None,
        end: tuple[float, float] | None = None,
        start_address: str | None = None,
        end_address: str | None = None,
        start_odometer_km: float | None = None,
        end_odometer_km: float | None = None,
    ) -> bool:
        """Send a trip given explicitly (service call). Returns True when accepted."""
        trip = self._build_trip(
            vehicle_id,
            start_time,
            end_time,
            start,
            end,
            start_address,
            end_address,
            start_odometer_km,
            end_odometer_km,
        )
        return await self._async_send(trip, queue_on_failure=False)

    # -- detection ----------------------------------------------------------

    def _vehicle_for(self, entity_id: str) -> str | None:
        for vid, conf in self.vehicles_config.items():
            if entity_id in (
                conf.get(CONF_TRIP_LOCATION_ENTITY),
                conf.get(CONF_TRIP_MOVING_ENTITY),
                conf.get(CONF_ODOMETER_ENTITY),
            ):
                return vid
        return None

    def _capture_parked(self, vid: str) -> None:
        """Remember where the vehicle is parked, to measure the next departure against."""
        conf = self.vehicles_config[vid]
        pos = read_position(self.hass, conf.get(CONF_TRIP_LOCATION_ENTITY))
        odo = get_entity_odometer_km(self.hass, conf.get(CONF_ODOMETER_ENTITY))
        parked = self._parked.setdefault(vid, {})
        if pos:
            parked["pos"] = pos
        if odo is not None:
            parked["odo"] = odo

    async def _async_on_change(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data.get("entity_id")
        vid = self._vehicle_for(entity_id) if entity_id else None
        if not vid:
            return
        conf = self.vehicles_config[vid]
        now = dt_util.utcnow()
        moving_entity = conf.get(CONF_TRIP_MOVING_ENTITY)

        if moving_entity:
            if entity_id == moving_entity:
                moving = _is_on(event.data.get("new_state"))
                if moving is True:
                    self._open_trip(vid, now)
                    self._cancel_debounce(vid)
                elif moving is False and vid in self._open:
                    self._open[vid]["last_motion"] = now.isoformat()
                    self._restart_debounce(vid, MOVING_SENSOR_END_DEBOUNCE_SECONDS)
                return
            if vid in self._open and entity_id == conf.get(CONF_TRIP_LOCATION_ENTITY):
                return
            if vid not in self._open:
                self._capture_parked(vid)
            return

        # Fallback mode: movement itself is the signal.
        if vid in self._open:
            if self._has_moved(vid):
                self._open[vid]["last_motion"] = now.isoformat()
            self._restart_debounce(vid)
            self._persist_soon()
            return

        if self._has_moved(vid):
            self._open_trip(vid, now)
            self._restart_debounce(vid)
        else:
            self._capture_parked(vid)

    def _has_moved(self, vid: str) -> bool:
        """Position away from the parked anchor, or odometer above the parked reading."""
        conf = self.vehicles_config[vid]
        parked = self._parked.get(vid, {})
        pos = read_position(self.hass, conf.get(CONF_TRIP_LOCATION_ENTITY))
        if pos and parked.get("pos"):
            if haversine_m(*parked["pos"], *pos) > TRIP_MOVE_THRESHOLD_M:
                return True
        odo = get_entity_odometer_km(self.hass, conf.get(CONF_ODOMETER_ENTITY))
        return (
            odo is not None
            and parked.get("odo") is not None
            and odo - parked["odo"] >= TRIP_MIN_ODOMETER_KM
        )

    def _open_trip(self, vid: str, now: datetime) -> None:
        if vid in self._open:
            return
        conf = self.vehicles_config[vid]
        parked = self._parked.get(vid, {})
        pos = parked.get("pos") or read_position(self.hass, conf.get(CONF_TRIP_LOCATION_ENTITY))
        odo = parked.get("odo")
        if odo is None:
            odo = get_entity_odometer_km(self.hass, conf.get(CONF_ODOMETER_ENTITY))
        self._open[vid] = {
            "start_time": now.isoformat(),
            "last_motion": now.isoformat(),
            "start_pos": list(pos) if pos else None,
            "start_odometer": odo,
        }
        _LOGGER.debug("Trip started for vehicle %s", vid)
        self._persist_soon()

    def _cancel_debounce(self, vid: str) -> None:
        cancel = self._debounce.pop(vid, None)
        if cancel:
            cancel()

    def _restart_debounce(self, vid: str, seconds: int | None = None) -> None:
        self._cancel_debounce(vid)

        async def _expired(_now: Any, vehicle_id: str = vid) -> None:
            self._debounce.pop(vehicle_id, None)
            await self._async_finish(vehicle_id)

        self._debounce[vid] = async_call_later(
            self.hass, float(seconds or self.end_debounce_seconds), _expired
        )

    # -- finishing ----------------------------------------------------------

    def _build_trip(
        self,
        vid: str,
        start_time: datetime,
        end_time: datetime,
        start: tuple[float, float] | None,
        end: tuple[float, float] | None,
        start_address: str | None,
        end_address: str | None,
        start_odo: float | None,
        end_odo: float | None,
    ) -> dict[str, Any]:
        if start and not start_address:
            start_address = zone_name_at(self.hass, *start)
        if end and not end_address:
            end_address = zone_name_at(self.hass, *end)
        return {
            "vehicle_id": vid,
            "event_id": f"{vid}-{int(start_time.timestamp())}",
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "start_lat": start[0] if start else None,
            "start_lon": start[1] if start else None,
            "end_lat": end[0] if end else None,
            "end_lon": end[1] if end else None,
            "start_address": start_address,
            "end_address": end_address,
            "start_odometer_km": start_odo,
            "end_odometer_km": end_odo,
        }

    async def _async_finish(self, vid: str) -> None:
        trip_state = self._open.get(vid)
        if not trip_state:
            return
        conf = self.vehicles_config[vid]
        moving_entity = conf.get(CONF_TRIP_MOVING_ENTITY)
        if moving_entity and _is_on(self.hass.states.get(moving_entity)):
            return

        start_time = datetime.fromisoformat(trip_state["start_time"])
        end_time = datetime.fromisoformat(trip_state["last_motion"])
        if end_time < start_time:
            end_time = start_time

        start = tuple(trip_state["start_pos"]) if trip_state.get("start_pos") else None
        end = read_position(self.hass, conf.get(CONF_TRIP_LOCATION_ENTITY))
        start_odo = trip_state.get("start_odometer")
        end_odo = get_entity_odometer_km(self.hass, conf.get(CONF_ODOMETER_ENTITY))

        self._open.pop(vid, None)
        # The vehicle is now parked where the trip ended.
        parked = self._parked.setdefault(vid, {})
        if end:
            parked["pos"] = end
        if end_odo is not None:
            parked["odo"] = end_odo

        if self._is_noise(start_time, end_time, start, end, start_odo, end_odo):
            _LOGGER.debug("Trip of vehicle %s ignored (too short / no movement)", vid)
            await self._async_save()
            return

        trip = self._build_trip(
            vid, start_time, end_time, start, end, None, None, start_odo, end_odo
        )
        await self._async_send(trip, queue_on_failure=True)
        await self._async_save()

    @staticmethod
    def _is_noise(
        start_time: datetime,
        end_time: datetime,
        start: tuple[float, float] | None,
        end: tuple[float, float] | None,
        start_odo: float | None,
        end_odo: float | None,
    ) -> bool:
        if start_odo is not None and end_odo is not None and end_odo < start_odo:
            # Odometer went backwards: the reading is unreliable, don't send it as a distance
            return False
        short = (end_time - start_time).total_seconds() < TRIP_MIN_SECONDS
        still = (
            start is not None
            and end is not None
            and haversine_m(*start, *end) < TRIP_MOVE_THRESHOLD_M
        )
        no_odo_move = (
            start_odo is None or end_odo is None or end_odo - start_odo < TRIP_MIN_ODOMETER_KM
        )
        return short and (still or start is None or end is None) and no_odo_move

    # -- sending / persistence ----------------------------------------------

    async def _async_send(self, trip: dict[str, Any], queue_on_failure: bool) -> bool:
        start_odo = trip.get("start_odometer_km")
        end_odo = trip.get("end_odometer_km")
        if start_odo is not None and end_odo is not None and end_odo < start_odo:
            # Never ship a negative distance: keep the trip, drop the unusable odometer pair.
            trip = {**trip, "start_odometer_km": None, "end_odometer_km": None}
        try:
            await self.client.async_submit_drive(
                vehicle_id=trip["vehicle_id"],
                event_id=trip["event_id"],
                start_time=trip["start_time"],
                end_time=trip["end_time"],
                start_lat=trip["start_lat"],
                start_lon=trip["start_lon"],
                end_lat=trip["end_lat"],
                end_lon=trip["end_lon"],
                start_address=trip["start_address"],
                end_address=trip["end_address"],
                start_odometer_km=trip["start_odometer_km"],
                end_odometer_km=trip["end_odometer_km"],
            )
        except Exception as err:
            _LOGGER.warning("Failed to send trip %s: %s", trip["event_id"], err)
            if queue_on_failure and not any(
                p["event_id"] == trip["event_id"] for p in self._pending
            ):
                self._pending.append(trip)
                self._schedule_retry()
            return False

        self._last_trip[trip["vehicle_id"]] = trip
        for listener in list(self._listeners):
            listener()
        if self.coordinator:
            await self.coordinator.async_request_refresh()
        return True

    def _schedule_retry(self) -> None:
        if self._retry:
            return

        async def _on_retry(_now: Any) -> None:
            self._retry = None
            await self._async_flush_pending()

        self._retry = async_call_later(self.hass, float(SYNC_RETRY_SECONDS), _on_retry)

    async def _async_flush_pending(self) -> None:
        remaining: list[dict[str, Any]] = []
        for trip in self._pending:
            if not await self._async_send(trip, queue_on_failure=False):
                remaining.append(trip)
        self._pending = remaining
        if remaining:
            self._schedule_retry()
        await self._async_save()

    def _persist_soon(self) -> None:
        if self.store is not None:
            self.store.async_delay_save(self._snapshot, 5)

    def _snapshot(self) -> dict[str, Any]:
        return {"open": self._open, "pending": self._pending}

    async def _async_save(self) -> None:
        if self.store is not None:
            await self.store.async_save(self._snapshot())

    async def _async_load(self) -> None:
        if self.store is None:
            return
        data = await self.store.async_load()
        if not isinstance(data, dict):
            return
        for vid, trip in (data.get("open") or {}).items():
            if vid in self.vehicles_config and isinstance(trip, dict):
                self._open[vid] = trip
        self._pending = [
            t for t in (data.get("pending") or []) if isinstance(t, dict) and "event_id" in t
        ]
