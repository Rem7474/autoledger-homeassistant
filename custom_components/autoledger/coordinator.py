"""DataUpdateCoordinator for AutoLedger integration."""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AutoLedgerApiClient, AutoLedgerAuthError, AutoLedgerError
from .const import (
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    ISSUE_SERVER_TOO_OLD,
    MIN_SERVER_VERSION,
    READ_TOKEN_SCOPES,
)

_LOGGER = logging.getLogger(__name__)


def _parse_version(version: Any) -> tuple[int, int, int] | None:
    """Return the (major, minor, patch) of a release such as "v1.61.0", None for "dev" or junk."""
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", str(version or ""))
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


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
        self.odometer_tracker: Any = None
        # Whether the token can read the figures (costs, reminders); decided once per entry load
        self._read_access: bool | None = None
        self._read_data: dict[str, Any] = {}

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from AutoLedger API and sync periodic telemetry."""
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
                except AutoLedgerAuthError:
                    raise
                except AutoLedgerError as err:
                    _LOGGER.warning("Failed to update metrics for vehicle %s: %s", vehicle_id, err)
                    metrics_by_id[vehicle_id] = {}

            if self.odometer_tracker:
                for vid, vdata in vehicles_by_id.items():
                    backend_odo = vdata.get("current_odometer")
                    if backend_odo and float(backend_odo) > 0:
                        self.odometer_tracker.set_last_synced_odometer(str(vid), float(backend_odo))

            return {
                "vehicles": vehicles_by_id,
                "metrics": metrics_by_id,
                "read_access": await self._async_fetch_read_data(),
                **self._read_data,
            }

        except AutoLedgerAuthError as err:
            raise ConfigEntryAuthFailed(f"AutoLedger rejected the API token: {err}") from err
        except AutoLedgerError as err:
            raise UpdateFailed(f"AutoLedger API communication failed: {err}") from err
        except Exception as err:
            _LOGGER.exception("Unexpected error fetching AutoLedger data: %s", err)
            raise UpdateFailed(f"Unexpected error: {err}") from err

    async def _async_fetch_read_data(self) -> bool:
        """Refresh the figures of the read API; return whether the token can read them.

        A token that only pushes ("ingest") cannot read them, nor can a server
        older than the read API: the sensors that depend on them are then not
        created. A transient error keeps the previous figures.
        """
        try:
            account = await self.client.async_get_account()
            self._check_server_version(account.get("server_version"))
            if self._read_access is None:
                self._read_access = account.get("scope") in READ_TOKEN_SCOPES
            if not self._read_access:
                return False

            fleet = await self.client.async_get_fleet_reminders()
            reminders = {
                str(item.get("vehicle_id")): item
                for item in fleet.get("vehicles", [])
                if item.get("vehicle_id") is not None
            }

            monthly: dict[str, dict[str, Any]] = {}
            for vehicle_id in self.vehicles_config:
                try:
                    months = (await self.client.async_get_monthly_costs(vehicle_id)).get(
                        "months", []
                    )
                except AutoLedgerAuthError:
                    raise
                except AutoLedgerError as err:
                    _LOGGER.warning("Failed to read the monthly costs of %s: %s", vehicle_id, err)
                    continue
                if months:
                    monthly[vehicle_id] = months[-1]

            summary: dict[str, dict[str, Any]] = {}
            try:
                fleet_summary = await self.client.async_get_summary()
            except AutoLedgerAuthError:
                raise
            except AutoLedgerError as err:
                # A server older than the summary route: the other figures still work.
                _LOGGER.warning("Failed to read the AutoLedger summary: %s", err)
                summary = self._read_data.get("summary", {})
            else:
                summary = {
                    str(item.get("vehicle_id")): item
                    for item in fleet_summary.get("vehicles", [])
                    if item.get("vehicle_id") is not None
                }

            self._read_data = {
                "account": account,
                "reminders": reminders,
                "monthly": monthly,
                "summary": summary,
            }
        except AutoLedgerAuthError:
            raise
        except AutoLedgerError as err:
            _LOGGER.warning("Failed to read the AutoLedger figures: %s", err)
        return bool(self._read_access)

    def _check_server_version(self, version: Any) -> None:
        """Raise a repair issue while the server is older than the routes the integration reads."""
        parsed = _parse_version(version)
        issue_id = f"{ISSUE_SERVER_TOO_OLD}_{self.entry_id}"
        if parsed is None or parsed >= MIN_SERVER_VERSION:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SERVER_TOO_OLD,
            translation_placeholders={
                "version": str(version),
                "minimum": ".".join(str(part) for part in MIN_SERVER_VERSION),
            },
        )
