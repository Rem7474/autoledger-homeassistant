"""Tests for AutoLedgerTripTracker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.autoledger.const import (
    CONF_ODOMETER_ENTITY,
    CONF_TRIP_LOCATION_ENTITY,
    CONF_TRIP_MOVING_ENTITY,
)
from custom_components.autoledger.trip_tracker import (
    AutoLedgerTripTracker,
    haversine_m,
    read_position,
    zone_name_at,
)
from tests.conftest import MockHass

HOME = (45.9, 6.12)
FAR = (45.95, 6.2)
LOC = "device_tracker.car"
MOVING = "binary_sensor.car_moving"
ODO = "sensor.car_odo"


def set_pos(hass, pos, state="not_home"):
    hass.states.set(LOC, state, {"latitude": pos[0], "longitude": pos[1]})


def event(entity_id, new_state=None):
    return MagicMock(data={"entity_id": entity_id, "new_state": new_state})


def make(hass, with_moving=False, with_odo=True, store=None):
    conf = {CONF_TRIP_LOCATION_ENTITY: LOC}
    if with_moving:
        conf[CONF_TRIP_MOVING_ENTITY] = MOVING
    if with_odo:
        conf[CONF_ODOMETER_ENTITY] = ODO
    client = MagicMock()
    client.async_submit_drive = AsyncMock(return_value={"status": "ok"})
    tracker = AutoLedgerTripTracker(hass, client, {"v1": conf, "v2": {}}, store=store)
    return tracker, client


def test_haversine_and_read_position():
    assert haversine_m(*HOME, *HOME) == 0
    assert 5000 < haversine_m(*HOME, *FAR) < 9000
    hass = MockHass()
    assert read_position(hass, None) is None
    assert read_position(hass, LOC) is None
    hass.states.set(LOC, "unavailable", {"latitude": 1, "longitude": 1})
    assert read_position(hass, LOC) is None
    hass.states.set(LOC, "home", {"latitude": 0, "longitude": 0})
    assert read_position(hass, LOC) is None
    hass.states.set(LOC, "home", {"latitude": "x", "longitude": 1})
    assert read_position(hass, LOC) is None
    hass.states.set(LOC, "home", {"latitude": 95, "longitude": 1})
    assert read_position(hass, LOC) is None
    set_pos(hass, HOME)
    assert read_position(hass, LOC) == HOME


def test_zone_name_picks_smallest_containing_zone():
    hass = MockHass()
    hass.states.set(
        "zone.big",
        "0",
        {"latitude": HOME[0], "longitude": HOME[1], "radius": 5000, "friendly_name": "City"},
    )
    hass.states.set(
        "zone.home",
        "0",
        {"latitude": HOME[0], "longitude": HOME[1], "radius": 100, "friendly_name": "Home"},
    )
    hass.states.set("zone.bad", "0", {"radius": 100})
    assert zone_name_at(hass, *HOME) == "Home"
    assert zone_name_at(hass, *FAR) is None


def test_only_vehicles_with_a_position_entity_are_tracked():
    tracker, _ = make(MockHass())
    assert list(tracker.vehicles_config) == ["v1"]


@pytest.mark.asyncio
async def test_fallback_mode_trip_from_position_and_odometer():
    hass = MockHass()
    set_pos(hass, HOME, "home")
    hass.states.set(ODO, "1000.0", {"unit_of_measurement": "km"})
    hass.states.set(
        "zone.home",
        "0",
        {"latitude": HOME[0], "longitude": HOME[1], "radius": 100, "friendly_name": "Maison"},
    )
    tracker, client = make(hass)
    await tracker.async_setup()

    set_pos(hass, FAR)
    hass.states.set(ODO, "1012.4", {"unit_of_measurement": "km"})
    with patch("custom_components.autoledger.trip_tracker.async_call_later") as later:
        await tracker._async_on_change(event(LOC))
        assert later.call_args.args[1] == 300.0
    assert "v1" in tracker._open

    await tracker._async_finish("v1")
    kwargs = client.async_submit_drive.call_args.kwargs
    assert kwargs["start_lat"] == HOME[0]
    assert kwargs["end_lat"] == FAR[0]
    assert kwargs["start_odometer_km"] == 1000.0
    assert kwargs["end_odometer_km"] == 1012.4
    assert kwargs["start_address"] == "Maison"
    assert kwargs["end_address"] is None
    assert kwargs["event_id"].startswith("v1-")
    assert "distance" not in kwargs
    assert tracker.last_trip("v1")["event_id"] == kwargs["event_id"]
    assert "v1" not in tracker._open


@pytest.mark.asyncio
async def test_gps_jitter_does_not_open_a_trip():
    hass = MockHass()
    set_pos(hass, HOME)
    tracker, client = make(hass, with_odo=False)
    await tracker.async_setup()
    set_pos(hass, (HOME[0] + 0.0002, HOME[1]))
    await tracker._async_on_change(event(LOC))
    assert tracker._open == {}
    client.async_submit_drive.assert_not_called()


@pytest.mark.asyncio
async def test_moving_sensor_mode_without_odometer_sends_no_distance_data():
    hass = MockHass()
    set_pos(hass, HOME)
    hass.states.set(MOVING, "off")
    tracker, client = make(hass, with_moving=True, with_odo=False)
    await tracker.async_setup()

    hass.states.set(MOVING, "on")
    with patch("custom_components.autoledger.trip_tracker.async_call_later"):
        await tracker._async_on_change(event(MOVING, hass.states.get(MOVING)))
        assert "v1" in tracker._open
        # position changes while moving never close the trip
        set_pos(hass, FAR)
        await tracker._async_on_change(event(LOC))
        assert "v1" in tracker._open

        hass.states.set(MOVING, "off")
        await tracker._async_on_change(event(MOVING, hass.states.get(MOVING)))
    tracker._open["v1"]["start_time"] = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
    await tracker._async_finish("v1")
    kwargs = client.async_submit_drive.call_args.kwargs
    assert kwargs["start_odometer_km"] is None
    assert kwargs["end_odometer_km"] is None
    assert kwargs["end_lat"] == FAR[0]


@pytest.mark.asyncio
async def test_moving_sensor_back_on_cancels_end():
    hass = MockHass()
    set_pos(hass, HOME)
    tracker, client = make(hass, with_moving=True)
    await tracker.async_setup()
    hass.states.set(MOVING, "on")
    await tracker._async_on_change(event(MOVING, hass.states.get(MOVING)))
    await tracker._async_finish("v1")
    client.async_submit_drive.assert_not_called()
    assert "v1" in tracker._open


@pytest.mark.asyncio
async def test_unknown_moving_state_is_ignored():
    hass = MockHass()
    set_pos(hass, HOME)
    hass.states.set(MOVING, "unavailable")
    tracker, _ = make(hass, with_moving=True)
    await tracker.async_setup()
    await tracker._async_on_change(event(MOVING, hass.states.get(MOVING)))
    assert tracker._open == {}


@pytest.mark.asyncio
async def test_short_trip_without_movement_is_noise():
    hass = MockHass()
    set_pos(hass, HOME)
    hass.states.set(ODO, "1000.0")
    tracker, client = make(hass, with_moving=True)
    await tracker.async_setup()
    hass.states.set(MOVING, "on")
    await tracker._async_on_change(event(MOVING, hass.states.get(MOVING)))
    hass.states.set(MOVING, "off")
    await tracker._async_on_change(event(MOVING, hass.states.get(MOVING)))
    await tracker._async_finish("v1")
    client.async_submit_drive.assert_not_called()


@pytest.mark.asyncio
async def test_backwards_odometer_is_not_sent():
    hass = MockHass()
    set_pos(hass, HOME)
    hass.states.set(ODO, "1000.0")
    tracker, client = make(hass, with_moving=True)
    await tracker.async_setup()
    hass.states.set(MOVING, "on")
    await tracker._async_on_change(event(MOVING, hass.states.get(MOVING)))
    set_pos(hass, FAR)
    hass.states.set(ODO, "900.0")
    hass.states.set(MOVING, "off")
    await tracker._async_on_change(event(MOVING, hass.states.get(MOVING)))
    await tracker._async_finish("v1")
    kwargs = client.async_submit_drive.call_args.kwargs
    assert kwargs["start_odometer_km"] is None
    assert kwargs["end_odometer_km"] is None


@pytest.mark.asyncio
async def test_failed_send_is_queued_persisted_and_retried():
    hass = MockHass()
    set_pos(hass, HOME)
    hass.states.set(ODO, "1000.0")
    from homeassistant.helpers.storage import Store

    store = Store(hass, 1, "k")
    tracker, client = make(hass, store=store)
    await tracker.async_setup()
    set_pos(hass, FAR)
    hass.states.set(ODO, "1020.0")
    with patch("custom_components.autoledger.trip_tracker.async_call_later"):
        await tracker._async_on_change(event(LOC))
    client.async_submit_drive.side_effect = RuntimeError("down")
    with patch("custom_components.autoledger.trip_tracker.async_call_later") as later:
        await tracker._async_finish("v1")
        assert later.call_args.args[1] == 300.0
    assert len(tracker._pending) == 1
    assert len(store.data["pending"]) == 1

    # A fresh tracker (restart) retries the stored trip with the same event_id
    event_id = store.data["pending"][0]["event_id"]
    tracker2, client2 = make(hass, store=store)
    await tracker2.async_setup()
    assert client2.async_submit_drive.call_args.kwargs["event_id"] == event_id
    assert tracker2._pending == []


@pytest.mark.asyncio
async def test_open_trip_survives_restart_and_restarts_debounce():
    hass = MockHass()
    set_pos(hass, HOME)
    from homeassistant.helpers.storage import Store

    store = Store(hass, 1, "k")
    tracker, _ = make(hass, with_moving=True, store=store)
    await tracker.async_setup()
    hass.states.set(MOVING, "on")
    await tracker._async_on_change(event(MOVING, hass.states.get(MOVING)))
    assert "v1" in store.data["open"]

    tracker2, _ = make(hass, with_moving=True, store=store)
    with patch("custom_components.autoledger.trip_tracker.async_call_later") as later:
        await tracker2.async_setup()
        later.assert_called_once()
    assert "v1" in tracker2._open
    await tracker2.async_unload()


@pytest.mark.asyncio
async def test_manual_log_trip():
    hass = MockHass()
    tracker, client = make(hass)
    ok = await tracker.async_log_trip(
        "v1",
        datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 8, 30, tzinfo=UTC),
        start=HOME,
        end_address="Bureau",
        start_odometer_km=10.0,
        end_odometer_km=25.0,
    )
    assert ok is True
    kwargs = client.async_submit_drive.call_args.kwargs
    assert kwargs["end_address"] == "Bureau"
    assert kwargs["end_lat"] is None

    client.async_submit_drive.side_effect = RuntimeError("down")
    assert (
        await tracker.async_log_trip(
            "v1", datetime(2026, 1, 2, tzinfo=UTC), datetime(2026, 1, 2, 1, tzinfo=UTC)
        )
        is False
    )
    assert tracker._pending == []
