"""The reminders calendar of each vehicle."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from test_read_sensors import FULL_SUMMARY, VEHICLES, _entry, _start

from custom_components.autoledger.const import DOMAIN

ENTITY = "calendar.family_reminders"


def _enable(hass: HomeAssistant, entry) -> None:
    er.async_get(hass).async_get_or_create(
        "calendar",
        DOMAIN,
        f"{entry.entry_id}_v1_reminders_calendar",
        config_entry=entry,
        suggested_object_id="family_reminders",
    )


async def _events(hass: HomeAssistant, start: datetime, end: datetime):
    calendar = hass.data["calendar"].get_entity(ENTITY)
    return await calendar.async_get_events(hass, start, end)


async def test_the_calendar_is_off_by_default(hass: HomeAssistant) -> None:
    entry = _entry(linked=False)
    _, patches = await _start(hass, entry, VEHICLES, FULL_SUMMARY)
    try:
        registry = er.async_get(hass)
        entity_id = registry.async_get_entity_id(
            "calendar", DOMAIN, f"{entry.entry_id}_v1_reminders_calendar"
        )
        assert registry.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    finally:
        for p in patches:
            p.stop()


async def test_each_dated_reminder_is_an_all_day_event(hass: HomeAssistant) -> None:
    entry = _entry(linked=False)
    entry.add_to_hass(hass)
    _enable(hass, entry)
    _, patches = await _start(hass, entry, VEHICLES, FULL_SUMMARY)
    try:
        zone = dt_util.DEFAULT_TIME_ZONE
        events = await _events(
            hass,
            datetime(2026, 10, 1, tzinfo=zone),
            datetime(2026, 12, 31, tzinfo=zone),
        )
        # the reminder that only follows a distance has no day
        assert [(e.summary, e.start, e.end) for e in events] == [
            ("Technical inspection", date(2026, 10, 20), date(2026, 10, 21)),
            ("Oil change", date(2026, 12, 1), date(2026, 12, 2)),
        ]
        assert events[0].description == "Status: overdue"
        assert events[1].description == "Status: due_soon\nDistance left: 850 km"
    finally:
        for p in patches:
            p.stop()


async def test_a_range_without_a_reminder_is_empty(hass: HomeAssistant) -> None:
    entry = _entry(linked=False)
    entry.add_to_hass(hass)
    _enable(hass, entry)
    _, patches = await _start(hass, entry, VEHICLES, FULL_SUMMARY)
    try:
        zone = dt_util.DEFAULT_TIME_ZONE
        assert (
            await _events(
                hass, datetime(2026, 11, 1, tzinfo=zone), datetime(2026, 11, 30, tzinfo=zone)
            )
            == []
        )
    finally:
        for p in patches:
            p.stop()


async def test_the_next_event_is_the_first_one_not_over(hass: HomeAssistant) -> None:
    entry = _entry(linked=False)
    entry.add_to_hass(hass)
    _enable(hass, entry)
    _, patches = await _start(hass, entry, VEHICLES, FULL_SUMMARY)
    try:
        calendar = hass.data["calendar"].get_entity(ENTITY)
        zone = dt_util.DEFAULT_TIME_ZONE
        for today, expected in (
            (datetime(2026, 10, 20, 9, tzinfo=zone), "Technical inspection"),
            (datetime(2026, 10, 21, 9, tzinfo=zone), "Oil change"),
            (datetime(2026, 12, 1, 9, tzinfo=zone), "Oil change"),
            (datetime(2026, 12, 2, 9, tzinfo=zone), None),
        ):
            with patch("custom_components.autoledger.calendar.dt_util.now", return_value=today):
                event = calendar.event
            assert (event.summary if event else None) == expected, today
    finally:
        for p in patches:
            p.stop()
