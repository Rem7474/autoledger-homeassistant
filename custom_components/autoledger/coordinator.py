"""DataUpdateCoordinator for AutoLedger integration."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AutoLedgerApiClient, AutoLedgerError
from .const import DEFAULT_SCAN_INTERVAL_MINUTES, DOMAIN

_LOGGER = logging.getLogger(__name__)


class AutoLedgerDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to manage fetching AutoLedger data at regular intervals."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: AutoLedgerApiClient,
        entry_id: str,
        vehicles_config: dict[str, Any],
        update_interval: timedelta | None = None,
    ) -> None:
        """Initialize the coordinator."""
        if update_interval is None:
            update_interval = timedelta(minutes=DEFAULT_SCAN_INTERVAL_MINUTES)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry_id}",
            update_interval=update_interval,
        )
        self.client = client
        self.entry_id = entry_id
        self.vehicles_config = vehicles_config

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from AutoLedger API."""
        try:
            vehicles_list = await self.client.async_get_vehicles()
            vehicles_by_id: dict[str, dict[str, Any]] = {
                str(v.get("id")): v for v in vehicles_list if "id" in v
            }

            metrics_by_id: dict[str, dict[str, Any]] = {}

            # Determine which vehicles to fetch metrics for (configured vehicles + any available)
            target_ids = set(self.vehicles_config.keys()) | set(vehicles_by_id.keys())

            for vehicle_id in target_ids:
                if not vehicle_id:
                    continue
                try:
                    metrics = await self.client.async_get_vehicle_metrics(vehicle_id)
                    metrics_by_id[vehicle_id] = metrics
                except AutoLedgerError as err:
                    _LOGGER.warning("Failed to update metrics for vehicle %s: %s", vehicle_id, err)
                    metrics_by_id[vehicle_id] = {}

            return {
                "vehicles": vehicles_by_id,
                "metrics": metrics_by_id,
            }

        except AutoLedgerError as err:
            raise UpdateFailed(f"AutoLedger API communication failed: {err}") from err
        except Exception as err:
            _LOGGER.exception("Unexpected error fetching AutoLedger data: %s", err)
            raise UpdateFailed(f"Unexpected error: {err}") from err
