"""Binary sensors fed by the AutoLedger read API."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AutoLedgerDataUpdateCoordinator
from .vehicles import ListedVehicleEntity, async_add_vehicle_entities, vehicle_device

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the binary sensors of the vehicles the token can read."""
    data = (
        entry.runtime_data
        if hasattr(entry, "runtime_data") and entry.runtime_data
        else hass.data[DOMAIN][entry.entry_id]
    )
    coordinator: AutoLedgerDataUpdateCoordinator = data["coordinator"]
    if not (coordinator.data and coordinator.data.get("read_access")):
        return

    def build(vehicle_id: str) -> list[BinarySensorEntity]:
        name, device_info = vehicle_device(entry, coordinator, vehicle_id)
        return [AutoLedgerDataGapsSensor(coordinator, entry, vehicle_id, name, device_info)]

    async_add_vehicle_entities(entry, coordinator, async_add_entities, build)


class AutoLedgerDataGapsSensor(
    ListedVehicleEntity, CoordinatorEntity[AutoLedgerDataUpdateCoordinator], BinarySensorEntity
):
    """On when the records of the vehicle lack something that weakens its figures."""

    _attr_icon = "mdi:database-alert-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: AutoLedgerDataUpdateCoordinator,
        entry: ConfigEntry,
        vehicle_id: str,
        vehicle_name: str,
        device_info: Any,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._vehicle_id = vehicle_id
        self._attr_name = f"{vehicle_name} Data Gaps"
        self._attr_unique_id = f"{entry.entry_id}_{vehicle_id}_data_gaps"
        self._attr_device_info = device_info

    @property
    def _summary(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get("summary", {}).get(self._vehicle_id, {})

    @property
    def is_on(self) -> bool | None:
        """Return whether something is missing from the records."""
        gaps = self._summary.get("data_gaps")
        return gaps > 0 if isinstance(gaps, int) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Give the number of gaps and how complete the figures are."""
        summary = self._summary
        if not summary:
            return {}
        return {
            "data_gaps": summary.get("data_gaps"),
            "completeness_pct": summary.get("completeness_pct"),
        }
