"""Tests for the odometer button, last-sync sensor and stale entity cleanup."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntry

import custom_components.autoledger as integration
from custom_components.autoledger.button import AutoLedgerSendOdometerButton, async_setup_entry
from custom_components.autoledger.const import (
    CONF_HOST,
    CONF_ODOMETER_ENTITY,
    CONF_VEHICLE_NAME,
    CONF_VEHICLES,
    DOMAIN,
)
from custom_components.autoledger.odometer_tracker import AutoLedgerOdometerTracker
from custom_components.autoledger.sensor import AutoLedgerOdometerLastSyncSensor
from tests.conftest import MockState as State


def _tracker(mock_hass, client=None, coordinator=None, entity="sensor.odo"):
    return AutoLedgerOdometerTracker(
        hass=mock_hass,
        client=client or MagicMock(async_update_odometer=AsyncMock()),
        vehicles_config={"v1": {CONF_ODOMETER_ENTITY: entity}},
        coordinator=coordinator,
    )


@pytest.mark.asyncio
async def test_last_sync_time_recorded_and_listeners_notified(mock_hass):
    mock_hass.states.get = MagicMock(return_value=State("sensor.odo", "1000"))
    tracker = _tracker(mock_hass)
    seen = []
    unsub = tracker.register_listener(lambda: seen.append(1))

    assert tracker.last_sync_time("v1") is None
    assert await tracker.async_sync_vehicle("v1", 1000.0) is True
    assert tracker.last_sync_time("v1") is not None
    assert seen == [1]

    unsub()
    await tracker.async_sync_vehicle("v1", 1001.0)
    assert seen == [1]


@pytest.mark.asyncio
async def test_failed_sync_keeps_last_sync_time_empty(mock_hass):
    client = MagicMock(async_update_odometer=AsyncMock(side_effect=RuntimeError("down")))
    tracker = _tracker(mock_hass, client=client)
    with patch("custom_components.autoledger.odometer_tracker.async_call_later"):
        assert await tracker.async_sync_vehicle("v1", 1000.0) is False
    assert tracker.last_sync_time("v1") is None


@pytest.mark.asyncio
async def test_button_forces_send_even_when_not_advanced(mock_hass):
    mock_hass.states.get = MagicMock(return_value=State("sensor.odo", "1000"))
    coordinator = MagicMock(async_request_refresh=AsyncMock())
    tracker = _tracker(mock_hass, coordinator=coordinator)
    tracker.set_last_synced_odometer("v1", 1000.0)
    entry = ConfigEntry(entry_id="e1", data={CONF_HOST: "h"}, options={})
    button = AutoLedgerSendOdometerButton(entry, tracker, "v1", "Car")

    await button.async_press()

    tracker.client.async_update_odometer.assert_awaited_once_with(
        vehicle_id="v1", odometer_km=1000.0
    )
    coordinator.async_request_refresh.assert_awaited_once()
    assert button._attr_unique_id == "e1_v1_send_odometer"


@pytest.mark.asyncio
async def test_button_setup_only_for_vehicles_with_odometer(mock_hass):
    entry = ConfigEntry(
        entry_id="e2",
        data={CONF_HOST: "h"},
        options={
            CONF_VEHICLES: {
                "v1": {CONF_VEHICLE_NAME: "A", CONF_ODOMETER_ENTITY: "sensor.odo"},
                "v2": {CONF_VEHICLE_NAME: "B"},
            }
        },
    )
    mock_hass.data[DOMAIN] = {
        "e2": {
            "coordinator": SimpleNamespace(data={"vehicles": {}}),
            "odometer_tracker": _tracker(mock_hass),
            "trackers": {},
        }
    }
    added = []
    await async_setup_entry(mock_hass, entry, added.extend)
    assert [b._attr_unique_id for b in added] == ["e2_v1_send_odometer"]


@pytest.mark.asyncio
async def test_last_sync_sensor_follows_tracker(mock_hass):
    tracker = _tracker(mock_hass)
    entry = ConfigEntry(entry_id="e3", data={CONF_HOST: "h"}, options={})
    sensor = AutoLedgerOdometerLastSyncSensor(entry, tracker, "v1", "Car")
    sensor.async_write_ha_state = MagicMock()

    assert sensor.native_value is None
    await sensor.async_added_to_hass()
    await tracker.async_sync_vehicle("v1", 500.0)
    assert sensor.native_value == tracker.last_sync_time("v1")
    sensor.async_write_ha_state.assert_called_once()
    assert sensor._attr_unique_id == "e3_v1_odometer_last_sync"

    await sensor.async_will_remove_from_hass()
    assert tracker._listeners == []


def test_stale_entities_removed_only_when_restored(mock_hass):
    entry = ConfigEntry(entry_id="e4", data={CONF_HOST: "h"}, options={})
    stale = SimpleNamespace(entity_id="sensor.old")
    live = SimpleNamespace(entity_id="sensor.live")
    mock_hass.states.get = MagicMock(
        side_effect=lambda eid: (
            State(eid, "unavailable", attributes={"restored": True})
            if eid == "sensor.old"
            else State(eid, "unavailable")
        )
    )
    registry = MagicMock()
    with (
        patch.object(integration.er, "async_get", return_value=registry),
        patch.object(integration.er, "async_entries_for_config_entry", return_value=[stale, live]),
    ):
        integration._remove_stale_entities(mock_hass, entry)

    registry.async_remove.assert_called_once_with("sensor.old")
