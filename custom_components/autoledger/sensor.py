"""Sensor platform for AutoLedger integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_CURRENCY,
    ATTR_DATE,
    ATTR_DEBOUNCE_ACTIVE,
    ATTR_DURATION_MINUTES,
    ATTR_ENERGY_KWH,
    ATTR_ENERGY_START_KWH,
    ATTR_LAST_ERROR_MESSAGE,
    ATTR_LAST_SUCCESSFUL_SYNC,
    ATTR_PENDING_EVENTS_COUNT,
    ATTR_SESSION_START_TIME,
    CONF_HOST,
    CONF_VEHICLE_ID,
    CONF_VEHICLE_NAME,
    CONF_VEHICLES,
    DOMAIN,
    STATE_COOLING_DOWN,
)
from .coordinator import AutoLedgerDataUpdateCoordinator
from .session_tracker import AutoLedgerSessionTracker

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up AutoLedger sensors based on a config entry."""
    data = entry.runtime_data if hasattr(entry, "runtime_data") and entry.runtime_data else hass.data[DOMAIN][entry.entry_id]
    coordinator: AutoLedgerDataUpdateCoordinator = data["coordinator"]
    trackers: dict[str, AutoLedgerSessionTracker] = data["trackers"]

    configured_vehicles: dict[str, Any] = entry.options.get(CONF_VEHICLES, {})
    entities: list[SensorEntity] = []

    for vehicle_id, vehicle_conf in configured_vehicles.items():
        v_name = vehicle_conf.get(CONF_VEHICLE_NAME, f"Vehicle {vehicle_id}")
        tracker = trackers.get(vehicle_id)

        # Retrieve vehicle info from coordinator if available
        vehicle_meta = coordinator.data.get("vehicles", {}).get(vehicle_id, {})
        make = vehicle_meta.get("make")
        model = vehicle_meta.get("model")

        # 1. Last charge cost sensor
        entities.append(
            AutoLedgerLastChargeCostSensor(
                coordinator=coordinator,
                entry=entry,
                vehicle_id=vehicle_id,
                vehicle_name=v_name,
                make=make,
                model=model,
            )
        )

        # 2. Cost per 100km sensor
        entities.append(
            AutoLedgerCostPer100KmSensor(
                coordinator=coordinator,
                entry=entry,
                vehicle_id=vehicle_id,
                vehicle_name=v_name,
                make=make,
                model=model,
            )
        )

        # 3. Sync status sensor
        if tracker:
            entities.append(
                AutoLedgerSyncStatusSensor(
                    entry=entry,
                    tracker=tracker,
                    vehicle_id=vehicle_id,
                    vehicle_name=v_name,
                    make=make,
                    model=model,
                )
            )

            # 4. Charging state sensor
            entities.append(
                AutoLedgerChargingStateSensor(
                    entry=entry,
                    tracker=tracker,
                    vehicle_id=vehicle_id,
                    vehicle_name=v_name,
                    make=make,
                    model=model,
                )
            )

    async_add_entities(entities)


def _get_vehicle_device_info(
    entry: ConfigEntry,
    vehicle_id: str,
    vehicle_name: str,
    make: str | None = None,
    model: str | None = None,
) -> DeviceInfo:
    """Return device info for a vehicle."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{vehicle_id}")},
        name=vehicle_name,
        manufacturer=make or "AutoLedger",
        model=model or "Vehicle",
        via_device=(DOMAIN, entry.entry_id),
    )


class AutoLedgerLastChargeCostSensor(
    CoordinatorEntity[AutoLedgerDataUpdateCoordinator], SensorEntity
):
    """Sensor for the financial cost of the last charging session."""

    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(
        self,
        coordinator: AutoLedgerDataUpdateCoordinator,
        entry: ConfigEntry,
        vehicle_id: str,
        vehicle_name: str,
        make: str | None = None,
        model: str | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._vehicle_id = vehicle_id
        self._attr_name = f"{vehicle_name} Last Charge Cost"
        self._attr_unique_id = f"{entry.entry_id}_{vehicle_id}_last_charge_cost"
        self._attr_device_info = _get_vehicle_device_info(
            entry, vehicle_id, vehicle_name, make, model
        )

    @property
    def _metrics(self) -> dict[str, Any]:
        """Return metrics dict for this vehicle."""
        if not self.coordinator.data:
            return {}
        return self.coordinator.data.get("metrics", {}).get(self._vehicle_id, {})

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return currency unit."""
        return self._metrics.get("currency", "€")

    @property
    def native_value(self) -> float | None:
        """Return cost of last charge."""
        cost = self._metrics.get("last_charge_cost")
        if cost is not None:
            try:
                return round(float(cost), 2)
            except (ValueError, TypeError):
                pass
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return session details attributes."""
        metrics = self._metrics
        return {
            ATTR_ENERGY_KWH: metrics.get("energy_kwh"),
            ATTR_DURATION_MINUTES: metrics.get("duration_minutes"),
            ATTR_DATE: metrics.get("date"),
            ATTR_CURRENCY: metrics.get("currency", "€"),
        }


class AutoLedgerCostPer100KmSensor(
    CoordinatorEntity[AutoLedgerDataUpdateCoordinator], SensorEntity
):
    """Sensor for the average cost per 100km."""

    _attr_icon = "mdi:cash-multiple"

    def __init__(
        self,
        coordinator: AutoLedgerDataUpdateCoordinator,
        entry: ConfigEntry,
        vehicle_id: str,
        vehicle_name: str,
        make: str | None = None,
        model: str | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._vehicle_id = vehicle_id
        self._attr_name = f"{vehicle_name} Cost Per 100km"
        self._attr_unique_id = f"{entry.entry_id}_{vehicle_id}_cost_per_100km"
        self._attr_device_info = _get_vehicle_device_info(
            entry, vehicle_id, vehicle_name, make, model
        )

    @property
    def _metrics(self) -> dict[str, Any]:
        """Return metrics dict for this vehicle."""
        if not self.coordinator.data:
            return {}
        return self.coordinator.data.get("metrics", {}).get(self._vehicle_id, {})

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return unit of measurement."""
        currency = self._metrics.get("currency", "€")
        return f"{currency}/100km"

    @property
    def native_value(self) -> float | None:
        """Return smoothed cost per 100km."""
        val = self._metrics.get("cost_per_100km")
        if val is not None:
            try:
                return round(float(val), 2)
            except (ValueError, TypeError):
                pass
        return None


class AutoLedgerSyncStatusSensor(SensorEntity):
    """Sensor displaying AutoLedger synchronization state."""

    _attr_icon = "mdi:sync"

    def __init__(
        self,
        entry: ConfigEntry,
        tracker: AutoLedgerSessionTracker,
        vehicle_id: str,
        vehicle_name: str,
        make: str | None = None,
        model: str | None = None,
    ) -> None:
        """Initialize the sync status sensor."""
        self._entry = entry
        self._tracker = tracker
        self._vehicle_id = vehicle_id
        self._attr_name = f"{vehicle_name} Sync Status"
        self._attr_unique_id = f"{entry.entry_id}_{vehicle_id}_sync_status"
        self._attr_device_info = _get_vehicle_device_info(
            entry, vehicle_id, vehicle_name, make, model
        )
        self._unsub_listener = None

    async def async_added_to_hass(self) -> None:
        """Register tracker callback when added to hass."""
        self._unsub_listener = self._tracker.register_listener(self._handle_tracker_update)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister tracker callback."""
        if self._unsub_listener:
            self._unsub_listener()
            self._unsub_listener = None

    @callback
    def _handle_tracker_update(self) -> None:
        """Handle status update from tracker."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        """Return sync state: ok, pending, error."""
        return self._tracker.sync_status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return sync attributes."""
        return {
            ATTR_LAST_SUCCESSFUL_SYNC: self._tracker.last_successful_sync,
            ATTR_LAST_ERROR_MESSAGE: self._tracker.last_error_message,
            ATTR_PENDING_EVENTS_COUNT: self._tracker.pending_events_count,
        }


class AutoLedgerChargingStateSensor(SensorEntity):
    """Sensor displaying real-time charging and solar debounce state."""

    _attr_icon = "mdi:ev-station"

    def __init__(
        self,
        entry: ConfigEntry,
        tracker: AutoLedgerSessionTracker,
        vehicle_id: str,
        vehicle_name: str,
        make: str | None = None,
        model: str | None = None,
    ) -> None:
        """Initialize the charging state sensor."""
        self._entry = entry
        self._tracker = tracker
        self._vehicle_id = vehicle_id
        self._attr_name = f"{vehicle_name} Charging State"
        self._attr_unique_id = f"{entry.entry_id}_{vehicle_id}_charging_state"
        self._attr_device_info = _get_vehicle_device_info(
            entry, vehicle_id, vehicle_name, make, model
        )
        self._unsub_listener = None

    async def async_added_to_hass(self) -> None:
        """Register tracker callback when added to hass."""
        self._unsub_listener = self._tracker.register_listener(self._handle_tracker_update)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister tracker callback."""
        if self._unsub_listener:
            self._unsub_listener()
            self._unsub_listener = None

    @callback
    def _handle_tracker_update(self) -> None:
        """Handle status update from tracker."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        """Return state: idle, charging, cooling_down."""
        return self._tracker.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return charging state attributes."""
        return {
            ATTR_SESSION_START_TIME: self._tracker.session_start_time,
            ATTR_DEBOUNCE_ACTIVE: self._tracker.state == STATE_COOLING_DOWN,
            ATTR_ENERGY_START_KWH: self._tracker.energy_start,
        }
