"""Sensors fed by the AutoLedger read API (maintenance reminders, costs, pending charges)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AutoLedgerDataUpdateCoordinator

PARALLEL_UPDATES = 0


def _distance_km(distance: dict[str, Any] | None) -> float | None:
    """Return a read API distance ({"value", "unit"}, always kilometres) as a number."""
    if not isinstance(distance, dict):
        return None
    try:
        return float(distance["value"])
    except (KeyError, TypeError, ValueError):
        return None


def _amount(amount: dict[str, Any] | None) -> Decimal | None:
    """Return a read API amount ({"amount": "12.34", "currency"}) as a Decimal."""
    if not isinstance(amount, dict):
        return None
    try:
        return Decimal(str(amount["amount"]))
    except (KeyError, InvalidOperation):
        return None


def _day(value: str | None) -> date | None:
    """Parse a YYYY-MM-DD day."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


class _VehicleReadSensor(CoordinatorEntity[AutoLedgerDataUpdateCoordinator], SensorEntity):
    """Base of the per-vehicle sensors fed by the read API."""

    _key: str
    _title: str

    def __init__(
        self,
        coordinator: AutoLedgerDataUpdateCoordinator,
        entry: ConfigEntry,
        vehicle_id: str,
        vehicle_name: str,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._vehicle_id = vehicle_id
        self._attr_name = f"{vehicle_name} {self._title}"
        self._attr_unique_id = f"{entry.entry_id}_{vehicle_id}_{self._key}"
        self._attr_device_info = device_info

    @property
    def _reminders(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return data.get("reminders", {}).get(self._vehicle_id, {})

    @property
    def _month(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return data.get("monthly", {}).get(self._vehicle_id, {})


class AutoLedgerRemindersOverdueSensor(_VehicleReadSensor):
    """Number of maintenance reminders whose limit is passed."""

    _key = "reminders_overdue"
    _title = "Overdue Reminders"
    _attr_icon = "mdi:wrench-clock"
    _status = "overdue"

    @property
    def native_value(self) -> int | None:
        """Return the number of reminders in this status."""
        value = self._reminders.get("overdue")
        return value if isinstance(value, int) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """List the reminders in this status."""
        return {
            "reminders": [
                {
                    "title": item.get("title"),
                    "category": item.get("category"),
                    "due_date": item.get("due_date"),
                    "remaining_days": item.get("remaining_days"),
                    "remaining_km": _distance_km(item.get("remaining_km")),
                }
                for item in self._reminders.get("reminders", [])
                if item.get("status") == self._status
            ]
        }


class AutoLedgerRemindersDueSoonSensor(AutoLedgerRemindersOverdueSensor):
    """Number of maintenance reminders inside their warning window."""

    _key = "reminders_due_soon"
    _title = "Reminders Due Soon"
    _attr_icon = "mdi:wrench-outline"
    _status = "due_soon"

    @property
    def native_value(self) -> int | None:
        """Return the number of reminders due soon."""
        value = self._reminders.get("due_soon")
        return value if isinstance(value, int) else None


class AutoLedgerNextServiceDateSensor(_VehicleReadSensor):
    """Date of the closest maintenance reminder (past when it is overdue)."""

    _key = "next_service_date"
    _title = "Next Service Date"
    _attr_device_class = SensorDeviceClass.DATE

    def _closest(self) -> tuple[date, dict[str, Any]] | None:
        dated = [
            (day, item)
            for item in self._reminders.get("reminders", [])
            if (day := _day(item.get("due_date"))) is not None
        ]
        return min(dated, key=lambda pair: pair[0]) if dated else None

    @property
    def native_value(self) -> date | None:
        """Return the closest due date."""
        closest = self._closest()
        return closest[0] if closest else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Name the reminder the date comes from."""
        closest = self._closest()
        return {"reminder": closest[1].get("title")} if closest else {}


class AutoLedgerNextServiceDistanceSensor(_VehicleReadSensor):
    """Kilometres left before the closest distance-based reminder (negative once passed)."""

    _key = "next_service_distance"
    _title = "Next Service Distance"
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_native_unit_of_measurement = "km"
    _attr_suggested_display_precision = 0

    def _closest(self) -> tuple[float, dict[str, Any]] | None:
        measured = [
            (km, item)
            for item in self._reminders.get("reminders", [])
            if (km := _distance_km(item.get("remaining_km"))) is not None
        ]
        return min(measured, key=lambda pair: pair[0]) if measured else None

    @property
    def native_value(self) -> float | None:
        """Return the smallest remaining distance."""
        closest = self._closest()
        return closest[0] if closest else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Name the reminder the distance comes from."""
        closest = self._closest()
        return {"reminder": closest[1].get("title")} if closest else {}


class AutoLedgerMonthCostSensor(_VehicleReadSensor):
    """What the vehicle costs in the month in progress, all categories."""

    _key = "month_cost"
    _title = "Cost This Month"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_suggested_display_precision = 2

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the currency of the vehicle."""
        total = self._month.get("costs", {}).get("total")
        return total.get("currency") if isinstance(total, dict) else None

    @property
    def native_value(self) -> Decimal | None:
        """Return the total of the month in progress."""
        return _amount(self._month.get("costs", {}).get("total"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Break the month down by category."""
        month = self._month
        if not month:
            return {}
        costs = month.get("costs", {})
        attrs: dict[str, Any] = {
            "month": month.get("month"),
            "in_progress": month.get("in_progress"),
        }
        for category in (
            "energy",
            "tolls",
            "maintenance",
            "insurance",
            "financing",
            "tires",
            "other",
        ):
            value = _amount(costs.get(category))
            attrs[category] = str(value) if value is not None else None
        return attrs


class AutoLedgerPendingChargesSensor(
    CoordinatorEntity[AutoLedgerDataUpdateCoordinator], SensorEntity
):
    """Charging sessions pushed to the account that no vehicle claimed yet."""

    _attr_icon = "mdi:ev-plug-type2"
    _attr_name = "AutoLedger Charges To Qualify"

    def __init__(self, coordinator: AutoLedgerDataUpdateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_pending_charges"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="AutoLedger",
            manufacturer="AutoLedger",
            model="Account",
        )

    @property
    def native_value(self) -> int | None:
        """Return the number of charging sessions waiting for a vehicle."""
        value = (self.coordinator.data or {}).get("account", {}).get("pending_charges")
        return value if isinstance(value, int) else None
