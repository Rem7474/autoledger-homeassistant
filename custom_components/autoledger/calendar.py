"""Calendar of the maintenance reminders of each vehicle."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import AutoLedgerDataUpdateCoordinator
from .vehicles import ListedVehicleEntity, async_add_vehicle_entities, vehicle_device

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one calendar per vehicle the token can read."""
    data = (
        entry.runtime_data
        if hasattr(entry, "runtime_data") and entry.runtime_data
        else hass.data[DOMAIN][entry.entry_id]
    )
    coordinator: AutoLedgerDataUpdateCoordinator = data["coordinator"]
    if not (coordinator.data and coordinator.data.get("read_access")):
        return

    def build(vehicle_id: str) -> list[CalendarEntity]:
        name, device_info = vehicle_device(entry, coordinator, vehicle_id)
        return [AutoLedgerRemindersCalendar(coordinator, entry, vehicle_id, name, device_info)]

    async_add_vehicle_entities(entry, coordinator, async_add_entities, build)


class AutoLedgerRemindersCalendar(
    ListedVehicleEntity, CoordinatorEntity[AutoLedgerDataUpdateCoordinator], CalendarEntity
):
    """The dated maintenance reminders of a vehicle, one all-day event each.

    A reminder that only follows a distance has no day to show; it stays in the reminder
    sensors. A reminder already late keeps its past day, so it appears in the past.
    """

    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:calendar-wrench"

    def __init__(
        self,
        coordinator: AutoLedgerDataUpdateCoordinator,
        entry: ConfigEntry,
        vehicle_id: str,
        vehicle_name: str,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the calendar."""
        super().__init__(coordinator)
        self._vehicle_id = vehicle_id
        self._attr_name = f"{vehicle_name} Reminders"
        self._attr_unique_id = f"{entry.entry_id}_{vehicle_id}_reminders_calendar"
        self._attr_device_info = device_info

    def _events(self) -> list[CalendarEvent]:
        reminders = (
            (self.coordinator.data or {}).get("reminders", {}).get(self._vehicle_id, {})
        ).get("reminders", [])
        events: list[CalendarEvent] = []
        for reminder in reminders:
            due = _day(reminder.get("due_date"))
            if due is None:
                continue
            events.append(
                CalendarEvent(
                    start=due,
                    end=due + timedelta(days=1),
                    summary=str(reminder.get("title") or "Reminder"),
                    description=_description(reminder),
                    uid=str(reminder["id"]) if reminder.get("id") else None,
                )
            )
        return sorted(events, key=lambda event: event.start)

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next event, the one in progress today included."""
        today = dt_util.now().date()
        return next((event for event in self._events() if event.end > today), None)

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Return the events whose day falls in the range."""
        first, last = start_date.date(), end_date.date()
        return [event for event in self._events() if event.start < last and event.end > first]


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _description(reminder: dict[str, Any]) -> str:
    """Status, distance left and last service of a reminder, one per line."""
    lines = [f"Status: {reminder.get('status')}"] if reminder.get("status") else []
    remaining = reminder.get("remaining_km")
    if isinstance(remaining, dict) and remaining.get("value") is not None:
        lines.append(f"Distance left: {remaining['value']} {remaining.get('unit', 'km')}")
    if reminder.get("last_service_date"):
        lines.append(f"Last service: {reminder['last_service_date']}")
    return "\n".join(lines)
