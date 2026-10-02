"""Button platform for AutoLedger integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ODOMETER_ENTITY, CONF_VEHICLE_NAME, CONF_VEHICLES, DOMAIN
from .odometer_tracker import AutoLedgerOdometerTracker


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one "send odometer now" button per vehicle with an odometer sensor."""
    data = (
        entry.runtime_data
        if hasattr(entry, "runtime_data") and entry.runtime_data
        else hass.data[DOMAIN][entry.entry_id]
    )
    odometer_tracker: AutoLedgerOdometerTracker = data["odometer_tracker"]
    configured_vehicles: dict[str, Any] = entry.options.get(CONF_VEHICLES, {})
    coordinator = data["coordinator"]

    entities: list[ButtonEntity] = []
    for vehicle_id, vehicle_conf in configured_vehicles.items():
        if not vehicle_conf.get(CONF_ODOMETER_ENTITY):
            continue
        v_name = vehicle_conf.get(CONF_VEHICLE_NAME, f"Vehicle {vehicle_id}")
        meta = coordinator.data.get("vehicles", {}).get(vehicle_id, {}) if coordinator.data else {}
        entities.append(
            AutoLedgerSendOdometerButton(
                entry=entry,
                tracker=odometer_tracker,
                vehicle_id=vehicle_id,
                vehicle_name=v_name,
                device_info=DeviceInfo(
                    identifiers={(DOMAIN, f"{entry.entry_id}_{vehicle_id}")},
                    name=v_name,
                    manufacturer=meta.get("make") or "AutoLedger",
                    model=meta.get("model") or "Vehicle",
                ),
            )
        )
    async_add_entities(entities)


class AutoLedgerSendOdometerButton(ButtonEntity):
    """Pushes the current odometer reading to AutoLedger, even when it has not advanced."""

    _attr_icon = "mdi:upload"

    def __init__(
        self,
        entry: ConfigEntry,
        tracker: AutoLedgerOdometerTracker,
        vehicle_id: str,
        vehicle_name: str,
        device_info: DeviceInfo | None = None,
    ) -> None:
        """Initialize the button."""
        self._tracker = tracker
        self._vehicle_id = vehicle_id
        self._attr_name = f"{vehicle_name} Send Odometer"
        self._attr_unique_id = f"{entry.entry_id}_{vehicle_id}_send_odometer"
        self._attr_device_info = device_info

    async def async_press(self) -> None:
        """Send the odometer reading now."""
        await self._tracker.async_sync_vehicle(self._vehicle_id, reason="button")
        coordinator = self._tracker.coordinator
        if coordinator:
            await coordinator.async_request_refresh()
