"""The electricity price entities and the estimate_charge action."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from test_read_sensors import FULL_SUMMARY, VEHICLES, _entry, _start

from custom_components.autoledger.api import AutoLedgerApiError
from custom_components.autoledger.const import DOMAIN

API = "custom_components.autoledger.api.AutoLedgerApiClient"
SUMMARY = {"vehicles": [{**FULL_SUMMARY["vehicles"][0], "powertrain": "EV"}]}


def _tariff(change_at: str | None = "2026-11-05T22:00:00+01:00") -> dict:
    return {
        "priced": True,
        "unpriced_reason": None,
        "plan": {"id": "p1", "name": "Heures creuses"},
        "now": {"band": "PEAK", "price_per_kwh": {"amount": "0.2516", "currency": "EUR"}},
        "next_change": (
            {
                "at": change_at,
                "band": "OFFPEAK",
                "price_per_kwh": {"amount": "0.1696", "currency": "EUR"},
            }
            if change_at
            else None
        ),
    }


def _enable_all(hass: HomeAssistant, entry) -> None:
    registry = er.async_get(hass)
    for key, object_id in (
        ("tariff_price", "autoledger_electricity_price"),
        ("tariff_band", "autoledger_electricity_band"),
        ("tariff_next_change", "autoledger_electricity_price_next_change"),
        ("tariff_next_price", "autoledger_electricity_next_price"),
    ):
        registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{entry.entry_id}_{key}",
            config_entry=entry,
            suggested_object_id=object_id,
        )


async def _start_with_tariff(hass: HomeAssistant, tariff, *, enable: bool = True):
    entry = _entry(linked=False)
    entry.add_to_hass(hass)
    if enable:
        _enable_all(hass, entry)
    mock = (
        AsyncMock(return_value=tariff)
        if not isinstance(tariff, Exception)
        else AsyncMock(side_effect=tariff)
    )
    patcher = patch(f"{API}.async_get_current_tariff", new=mock)
    patcher.start()
    _, patches = await _start(hass, entry, VEHICLES, SUMMARY)
    return entry, mock, [patcher, *patches]


def _stop(patches) -> None:
    for p in patches:
        p.stop()


async def test_the_price_is_on_and_the_rest_of_the_tariff_is_off_by_default(
    hass: HomeAssistant,
) -> None:
    _, mock, patches = await _start_with_tariff(hass, _tariff(), enable=False)
    try:
        assert hass.states.get("sensor.autoledger_electricity_price").state == "0.2516"
        assert hass.states.get("sensor.autoledger_electricity_band") is None
        assert mock.await_count == 1
    finally:
        _stop(patches)


async def test_the_price_band_and_next_change_are_mapped(hass: HomeAssistant) -> None:
    _, _, patches = await _start_with_tariff(hass, _tariff())
    try:
        price = hass.states.get("sensor.autoledger_electricity_price")
        assert float(price.state) == 0.2516
        assert price.attributes["unit_of_measurement"] == "EUR/kWh"
        assert price.attributes["plan"] == "Heures creuses"
        assert hass.states.get("sensor.autoledger_electricity_band").state == "PEAK"
        change = hass.states.get("sensor.autoledger_electricity_price_next_change")
        assert change.state == "2026-11-05T21:00:00+00:00"
        nxt = hass.states.get("sensor.autoledger_electricity_next_price")
        assert float(nxt.state) == 0.1696
        assert nxt.attributes["band"] == "OFFPEAK"
    finally:
        _stop(patches)


async def test_a_flat_plan_has_no_next_change(hass: HomeAssistant) -> None:
    _, _, patches = await _start_with_tariff(hass, _tariff(change_at=None))
    try:
        assert hass.states.get("sensor.autoledger_electricity_price_next_change").state == "unknown"
        assert hass.states.get("sensor.autoledger_electricity_next_price").state == "unknown"
    finally:
        _stop(patches)


async def test_no_plan_gives_an_unknown_price_and_says_why(hass: HomeAssistant) -> None:
    tariff = {
        "priced": False,
        "unpriced_reason": "no_plan",
        "plan": None,
        "now": None,
        "next_change": None,
    }
    _, _, patches = await _start_with_tariff(hass, tariff)
    try:
        price = hass.states.get("sensor.autoledger_electricity_price")
        assert price.state == "unknown"
        assert price.attributes["unpriced_reason"] == "no_plan"
    finally:
        _stop(patches)


async def test_a_server_without_the_route_leaves_the_price_unknown(hass: HomeAssistant) -> None:
    _, _, patches = await _start_with_tariff(hass, AutoLedgerApiError("not found", status_code=404))
    try:
        assert hass.states.get("sensor.autoledger_electricity_price").state == "unknown"
    finally:
        _stop(patches)


async def test_the_tariff_is_not_read_once_every_entity_is_disabled(hass: HomeAssistant) -> None:
    entry = _entry(linked=False)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for key in ("tariff_price", "tariff_band", "tariff_next_change", "tariff_next_price"):
        registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{entry.entry_id}_{key}",
            config_entry=entry,
            disabled_by=er.RegistryEntryDisabler.USER,
        )
    mock = AsyncMock(return_value=_tariff())
    patcher = patch(f"{API}.async_get_current_tariff", new=mock)
    patcher.start()
    _, patches = await _start(hass, entry, VEHICLES, SUMMARY)
    try:
        assert mock.await_count == 0
    finally:
        _stop([patcher, *patches])


async def test_the_price_is_read_again_when_it_changes(hass: HomeAssistant) -> None:
    soon = (dt_util.utcnow() + timedelta(minutes=5)).isoformat()
    _, mock, patches = await _start_with_tariff(hass, _tariff(change_at=soon))
    try:
        assert mock.await_count == 1
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6))
        await hass.async_block_till_done()
        assert mock.await_count == 2
    finally:
        _stop(patches)


async def test_estimate_charge_returns_the_servers_pricing(hass: HomeAssistant) -> None:
    _, _, patches = await _start_with_tariff(hass, _tariff())
    estimate = {"priced": True, "cost": {"amount": "5.43", "currency": "EUR"}}
    mock = AsyncMock(return_value=estimate)
    patcher = patch(f"{API}.async_estimate_charge", new=mock)
    patcher.start()
    try:
        result = await hass.services.async_call(
            DOMAIN,
            "estimate_charge",
            {"energy_kwh": 32.5, "duration_minutes": 240, "vehicle_id": "v1"},
            blocking=True,
            return_response=True,
        )
        assert result == estimate
        mock.assert_awaited_once_with(32.5, 240, start=None, vehicle_id="v1")
    finally:
        patcher.stop()
        _stop(patches)


async def test_estimate_charge_reports_a_server_refusal(hass: HomeAssistant) -> None:
    _, _, patches = await _start_with_tariff(hass, _tariff())
    patcher = patch(
        f"{API}.async_estimate_charge",
        new=AsyncMock(side_effect=AutoLedgerApiError("bad request", status_code=400)),
    )
    patcher.start()
    try:
        with pytest.raises(HomeAssistantError, match="could not price"):
            await hass.services.async_call(
                DOMAIN,
                "estimate_charge",
                {"energy_kwh": 10, "duration_minutes": 60},
                blocking=True,
                return_response=True,
            )
    finally:
        patcher.stop()
        _stop(patches)
