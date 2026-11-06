"""The opt-in sections are read about once an hour, the other figures at every refresh."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from test_data_groups import ENTITIES, SUMMARY
from test_read_sensors import FULL_SUMMARY, VEHICLES, _entry, _start, _state

from custom_components.autoledger.const import DOMAIN, SLOW_GROUPS_INTERVAL

SECTION_KEYS = ("tires", "battery", "ownership", "sources")


def _server_answer(include):
    """What the server sends: only the sections that are asked for."""
    return {
        "vehicles": [
            {k: v for k, v in item.items() if k not in SECTION_KEYS or k in include}
            for item in SUMMARY["vehicles"]
        ]
    }


async def _setup(hass: HomeAssistant, keys=("tires_wear", "vehicle_value")):
    entry = _entry(linked=False)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for key in keys:
        registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{entry.entry_id}_v1_{key}",
            config_entry=entry,
            suggested_object_id=ENTITIES[key],
        )
    mocks, patches = await _start(hass, entry, VEHICLES, SUMMARY)
    mocks["summary"].side_effect = _server_answer
    return entry, mocks, patches, hass.data[DOMAIN][entry.entry_id]["coordinator"]


async def test_the_sections_are_read_once_then_kept_between_slow_cycles(
    hass: HomeAssistant,
) -> None:
    entry, mocks, patches, coordinator = await _setup(hass)
    try:
        assert sorted(mocks["summary"].await_args_list[0].args[0]) == ["ownership", "tires"]
        mocks["summary"].reset_mock()

        await coordinator.async_refresh()
        await hass.async_block_till_done()

        # the refresh still asks for the figures, but not for the slow sections
        assert [list(call.args[0]) for call in mocks["summary"].await_args_list] == [[]]
        # and the last values stay on the entities
        assert float(_state(hass, "tire_wear").state) == 62.5
        assert _state(hass, "estimated_value").state == "21500.00"
    finally:
        for p in patches:
            p.stop()


async def test_the_sections_are_read_again_after_an_hour(hass: HomeAssistant) -> None:
    _, mocks, patches, coordinator = await _setup(hass)
    try:
        mocks["summary"].reset_mock()
        coordinator._slow_at = dt_util.utcnow() - SLOW_GROUPS_INTERVAL - timedelta(minutes=1)

        await coordinator.async_refresh()

        assert sorted(mocks["summary"].await_args_list[0].args[0]) == ["ownership", "tires"]
    finally:
        for p in patches:
            p.stop()


async def test_the_sync_action_reads_the_sections_at_once(hass: HomeAssistant) -> None:
    _, mocks, patches, coordinator = await _setup(hass)
    try:
        mocks["summary"].reset_mock()
        coordinator.reset_slow_groups()

        await coordinator.async_refresh()

        assert sorted(mocks["summary"].await_args_list[0].args[0]) == ["ownership", "tires"]
    finally:
        for p in patches:
            p.stop()


async def test_a_vehicle_that_appears_is_not_left_waiting_for_the_hour(
    hass: HomeAssistant,
) -> None:
    _, mocks, patches, coordinator = await _setup(hass)
    try:
        mocks["summary"].reset_mock()
        coordinator._slow_vehicles = set()

        await coordinator.async_refresh()

        assert sorted(mocks["summary"].await_args_list[0].args[0]) == ["ownership", "tires"]
    finally:
        for p in patches:
            p.stop()


async def test_nothing_is_remembered_for_a_group_that_is_no_longer_enabled(
    hass: HomeAssistant,
) -> None:
    _, mocks, patches, coordinator = await _setup(hass)
    try:
        mocks["summary"].reset_mock()

        # the tires entity is gone from the enabled ones (the entry would reload in real life)
        with patch.object(coordinator, "_enabled_data_groups", return_value=["ownership"]):
            data = await coordinator._async_update_data()

        # the set of groups changed: it is read again at once, and no stale tires remain
        assert [list(call.args[0]) for call in mocks["summary"].await_args_list] == [["ownership"]]
        assert "tires" not in data["summary"]["v1"]
        assert "ownership" in data["summary"]["v1"]
    finally:
        for p in patches:
            p.stop()


async def test_without_sections_enabled_nothing_is_remembered(hass: HomeAssistant) -> None:
    entry = _entry(linked=False)
    mocks, patches = await _start(hass, entry, VEHICLES, FULL_SUMMARY)
    try:
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        await coordinator.async_refresh()
        assert [list(call.args[0]) for call in mocks["summary"].await_args_list] == [[], []]
        assert coordinator._slow_sections == {}
    finally:
        for p in patches:
            p.stop()
