"""Sensor platform for AutoLedger integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
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
    CONF_CHARGER_NAME,
    CONF_CHARGERS,
    CONF_VEHICLE_NAME,
    CONF_VEHICLES,
    DOMAIN,
    STATE_COOLING_DOWN,
)
from .coordinator import AutoLedgerDataUpdateCoordinator
from .session_tracker import AutoLedgerChargerTracker

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up AutoLedger sensors based on a config entry."""
    data = (
        entry.runtime_data
        if hasattr(entry, "runtime_data") and entry.runtime_data
        else hass.data[DOMAIN][entry.entry_id]
    )
    coordinator: AutoLedgerDataUpdateCoordinator = data["coordinator"]
    trackers: dict[str, AutoLedgerChargerTracker] = data["trackers"]

    configured_vehicles: dict[str, Any] = entry.options.get(CONF_VEHICLES, {})
    configured_chargers: dict[str, Any] = entry.options.get(CONF_CHARGERS, {})
    entities: list[SensorEntity] = []

    # 1. Sensors per vehicle
    for vehicle_id, vehicle_conf in configured_vehicles.items():
        v_name = vehicle_conf.get(CONF_VEHICLE_NAME, f"Vehicle {vehicle_id}")

        # Retrieve vehicle metadata from coordinator
        vehicle_meta = (
            coordinator.data.get("vehicles", {}).get(vehicle_id, {}) if coordinator.data else {}
        )
        make = vehicle_meta.get("make")
        model = vehicle_meta.get("model")

        # Vehicle last charge cost
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

        # Vehicle cost per 100km
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

        # Backward compatibility for legacy tracker directly attached to vehicle
        if vehicle_id in trackers and vehicle_id not in configured_chargers:
            v_tracker = trackers[vehicle_id]
            entities.append(
                AutoLedgerChargerChargingStateSensor(
                    entry=entry,
                    tracker=v_tracker,
                    charger_id=vehicle_id,
                    charger_name=v_name,
                    device_info=_get_vehicle_device_info(entry, vehicle_id, v_name, make, model),
                )
            )
            entities.append(
                AutoLedgerChargerSyncStatusSensor(
                    entry=entry,
                    tracker=v_tracker,
                    charger_id=vehicle_id,
                    charger_name=v_name,
                    device_info=_get_vehicle_device_info(entry, vehicle_id, v_name, make, model),
                )
            )

    # 2. Sensors per charging station
    for charger_id, charger_conf in configured_chargers.items():
        c_name = charger_conf.get(CONF_CHARGER_NAME, f"Charger {charger_id}")
        tracker = trackers.get(charger_id)
        if not tracker:
            continue

        c_device_info = _get_charger_device_info(entry, charger_id, c_name)

        # Charger charging state sensor
        entities.append(
            AutoLedgerChargerChargingStateSensor(
                entry=entry,
                tracker=tracker,
                charger_id=charger_id,
                charger_name=c_name,
                device_info=c_device_info,
            )
        )

        # Charger last energy kWh sensor
        entities.append(
            AutoLedgerChargerLastEnergySensor(
                entry=entry,
                tracker=tracker,
                charger_id=charger_id,
                charger_name=c_name,
                device_info=c_device_info,
            )
        )

        # Charger sync status sensor
        entities.append(
            AutoLedgerChargerSyncStatusSensor(
                entry=entry,
                tracker=tracker,
                charger_id=charger_id,
                charger_name=c_name,
                device_info=c_device_info,
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
    )


def _get_charger_device_info(
    entry: ConfigEntry,
    charger_id: str,
    charger_name: str,
) -> DeviceInfo:
    """Return device info for a charging station."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{charger_id}")},
        name=charger_name,
        manufacturer="AutoLedger",
        model="Charging Station",
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


class AutoLedgerChargerChargingStateSensor(SensorEntity):
    """Sensor displaying real-time charging and solar debounce state for a charger."""

    _attr_icon = "mdi:ev-station"

    def __init__(
        self,
        entry: ConfigEntry,
        tracker: AutoLedgerChargerTracker,
        charger_id: str | None = None,
        charger_name: str | None = None,
        device_info: DeviceInfo | None = None,
        vehicle_id: str | None = None,
        vehicle_name: str | None = None,
        make: str | None = None,
        model: str | None = None,
    ) -> None:
        """Initialize the charging state sensor."""
        self._entry = entry
        self._tracker = tracker
        self._charger_id = charger_id or vehicle_id or "charger"
        self._charger_name = charger_name or vehicle_name or self._charger_id
        self._attr_name = f"{self._charger_name} Charging State"
        self._attr_unique_id = f"{entry.entry_id}_{self._charger_id}_charging_state"
        self._attr_device_info = device_info or _get_charger_device_info(
            entry, self._charger_id, self._charger_name
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


class AutoLedgerChargerLastEnergySensor(SensorEntity):
    """Sensor displaying energy delivered during the last charging session for a charger."""

    _attr_icon = "mdi:lightning-bolt"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = "kWh"

    def __init__(
        self,
        entry: ConfigEntry,
        tracker: AutoLedgerChargerTracker,
        charger_id: str,
        charger_name: str,
        device_info: DeviceInfo | None = None,
    ) -> None:
        """Initialize the last energy sensor."""
        self._entry = entry
        self._tracker = tracker
        self._charger_id = charger_id
        self._charger_name = charger_name
        self._attr_name = f"{charger_name} Last Energy"
        self._attr_unique_id = f"{entry.entry_id}_{charger_id}_last_energy_kwh"
        self._attr_device_info = device_info or _get_charger_device_info(
            entry, charger_id, charger_name
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
    def native_value(self) -> float | None:
        """Return energy added in last session in kWh."""
        return self._tracker.last_energy_kwh


class AutoLedgerChargerSyncStatusSensor(SensorEntity):
    """Sensor displaying AutoLedger synchronization state for a charger."""

    _attr_icon = "mdi:sync"

    def __init__(
        self,
        entry: ConfigEntry,
        tracker: AutoLedgerChargerTracker,
        charger_id: str | None = None,
        charger_name: str | None = None,
        device_info: DeviceInfo | None = None,
        vehicle_id: str | None = None,
        vehicle_name: str | None = None,
        make: str | None = None,
        model: str | None = None,
    ) -> None:
        """Initialize the sync status sensor."""
        self._entry = entry
        self._tracker = tracker
        self._charger_id = charger_id or vehicle_id or "charger"
        self._charger_name = charger_name or vehicle_name or self._charger_id
        self._attr_name = f"{self._charger_name} Sync Status"
        self._attr_unique_id = f"{entry.entry_id}_{self._charger_id}_sync_status"
        self._attr_device_info = device_info or _get_charger_device_info(
            entry, self._charger_id, self._charger_name
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


# Aliases for backwards compatibility
AutoLedgerChargingStateSensor = AutoLedgerChargerChargingStateSensor
AutoLedgerSyncStatusSensor = AutoLedgerChargerSyncStatusSensor
