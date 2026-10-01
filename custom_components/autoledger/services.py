"""Custom services for the AutoLedger integration."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SERVICE_SYNC = "sync"
SERVICE_SUBMIT_CHARGE = "submit_charge"

SERVICE_SYNC_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
    }
)

SERVICE_SUBMIT_CHARGE_SCHEMA = vol.Schema(
    {
        vol.Required("vehicle_id"): cv.string,
        vol.Required("energy_kwh"): vol.Coerce(float),
        vol.Optional("cost"): vol.Any(vol.Coerce(float), None),
        vol.Optional("odometer_km"): vol.Any(vol.Coerce(float), None),
        vol.Optional("soc_start"): vol.Any(vol.Coerce(int), None),
        vol.Optional("soc_end"): vol.Any(vol.Coerce(int), None),
        vol.Optional("start_time"): vol.Any(cv.string, None),
        vol.Optional("end_time"): vol.Any(cv.string, None),
        vol.Optional("location", default="home"): cv.string,
    }
)


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register custom services for AutoLedger."""

    async def async_handle_sync(call: ServiceCall) -> None:
        """Handle the sync service call."""
        entry_id = call.data.get("entry_id")
        domain_data = hass.data.get(DOMAIN, {})

        refreshed = False
        for e_id, data in domain_data.items():
            if entry_id and e_id != entry_id:
                continue
            coordinator = data.get("coordinator")
            if coordinator:
                _LOGGER.info("Forcing AutoLedger coordinator refresh for entry %s", e_id)
                await coordinator.async_request_refresh()
                refreshed = True

        if not refreshed:
            _LOGGER.warning("No active AutoLedger coordinator found to sync")

    async def async_handle_submit_charge(call: ServiceCall) -> None:
        """Handle the submit_charge service call."""
        vehicle_id = call.data["vehicle_id"]
        kwh = call.data["energy_kwh"]
        cost = call.data.get("cost")
        odometer_km = call.data.get("odometer_km")
        soc_start = call.data.get("soc_start")
        soc_end = call.data.get("soc_end")
        start_time = call.data.get("start_time")
        end_time = call.data.get("end_time")
        location = call.data.get("location", "home")

        domain_data = hass.data.get(DOMAIN, {})
        handled = False

        for _entry_id, data in domain_data.items():
            trackers = data.get("trackers", {})
            if vehicle_id in trackers:
                tracker = trackers[vehicle_id]
                await tracker.async_submit_manual_charge(
                    kwh=kwh,
                    cost=cost,
                    odometer_km=odometer_km,
                    soc_start=soc_start,
                    soc_end=soc_end,
                    start_time=start_time,
                    end_time=end_time,
                    location=location,
                )
                handled = True
                break

            # If vehicle not in trackers, use api_client directly
            client = data.get("client")
            if client:
                await client.async_submit_charge(
                    vehicle_id=vehicle_id,
                    kwh=kwh,
                    cost=cost,
                    odometer_km=odometer_km,
                    soc_start=soc_start,
                    soc_end=soc_end,
                    start_time=start_time,
                    end_time=end_time,
                    location=location,
                )
                coordinator = data.get("coordinator")
                if coordinator:
                    await coordinator.async_request_refresh()
                handled = True
                break

        if not handled:
            _LOGGER.error(
                "Cannot submit charge: Vehicle %s not found in any AutoLedger entry", vehicle_id
            )

    if not hass.services.has_service(DOMAIN, SERVICE_SYNC):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SYNC,
            async_handle_sync,
            schema=SERVICE_SYNC_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SUBMIT_CHARGE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SUBMIT_CHARGE,
            async_handle_submit_charge,
            schema=SERVICE_SUBMIT_CHARGE_SCHEMA,
        )


async def async_unload_services(hass: HomeAssistant) -> None:
    """Unload AutoLedger services if no entries remain."""
    if DOMAIN in hass.data and hass.data[DOMAIN]:
        return

    if hass.services.has_service(DOMAIN, SERVICE_SYNC):
        hass.services.async_remove(DOMAIN, SERVICE_SYNC)

    if hass.services.has_service(DOMAIN, SERVICE_SUBMIT_CHARGE):
        hass.services.async_remove(DOMAIN, SERVICE_SUBMIT_CHARGE)
