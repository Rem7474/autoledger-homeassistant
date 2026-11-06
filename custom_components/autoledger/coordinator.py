"""DataUpdateCoordinator for AutoLedger integration."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import AutoLedgerApiClient, AutoLedgerAuthError, AutoLedgerError
from .const import (
    DATA_GROUPS,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    FLEET_KEYS,
    ISSUE_SERVER_TOO_OLD,
    MIN_SERVER_VERSION,
    READ_TOKEN_SCOPES,
    SLOW_GROUPS_INTERVAL,
    TARIFF_KEYS,
)

_LOGGER = logging.getLogger(__name__)


def _parse_version(version: Any) -> tuple[int, int, int] | None:
    """Return the (major, minor, patch) of a release such as "v1.61.0", None for "dev" or junk."""
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", str(version or ""))
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def _amount_number(amount: Any) -> float | None:
    """Return a read API amount ({"amount": "12.34", "currency"}) as a number."""
    try:
        return float(amount["amount"])
    except (KeyError, TypeError, ValueError):
        return None


def _metrics_from_summary(item: dict[str, Any]) -> dict[str, Any]:
    """Shape the last charge and the cost per 100 km of a summary entry like the metrics route."""
    last = item.get("last_charge") or {}
    metrics: dict[str, Any] = {
        "last_charge_cost": _amount_number(last.get("cost")),
        "energy_kwh": last.get("energy_kwh"),
        "duration_minutes": last.get("duration_minutes"),
        "date": last.get("date"),
        "cost_per_100km": _amount_number(item.get("cost_per_100km")),
    }
    if item.get("currency"):
        metrics["currency"] = item["currency"]
    return metrics


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
        self._price_change_cancel: Any = None
        # The opt-in sections read at the last slow cycle: when, for which groups and vehicles,
        # and what they said, so the refreshes in between keep showing it
        self._slow_at: datetime | None = None
        self._slow_groups: set[str] = set()
        self._slow_vehicles: set[str] = set()
        self._slow_sections: dict[str, dict[str, Any]] = {}

    def reset_slow_groups(self) -> None:
        """Read the slow sections at the next refresh, whatever their age."""
        self._slow_at = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from AutoLedger API and sync periodic telemetry."""
        try:
            vehicles_list = await self.client.async_get_vehicles()
            vehicles_by_id: dict[str, dict[str, Any]] = {
                str(v.get("id")): v for v in vehicles_list if "id" in v
            }

            if self.odometer_tracker:
                for vid, vdata in vehicles_by_id.items():
                    backend_odo = vdata.get("current_odometer")
                    if backend_odo and float(backend_odo) > 0:
                        self.odometer_tracker.set_last_synced_odometer(str(vid), float(backend_odo))

            read_access = await self._async_fetch_read_data(list(vehicles_by_id))
            tariff = await self._async_fetch_tariff() if read_access else None
            self._schedule_price_change(tariff)
            return {
                "tariff": tariff,
                "fleet": await self._async_fetch_fleet() if read_access else None,
                "vehicles": vehicles_by_id,
                "metrics": await self._async_vehicle_metrics(vehicles_by_id),
                "read_access": read_access,
                **self._read_data,
            }

        except AutoLedgerAuthError as err:
            raise ConfigEntryAuthFailed(f"AutoLedger rejected the API token: {err}") from err
        except AutoLedgerError as err:
            raise UpdateFailed(f"AutoLedger API communication failed: {err}") from err
        except Exception as err:
            _LOGGER.exception("Unexpected error fetching AutoLedger data: %s", err)
            raise UpdateFailed(f"Unexpected error: {err}") from err

    async def _async_fetch_read_data(self, vehicle_ids: list[str]) -> bool:
        """Refresh the figures of the read API; return whether the token can read them.

        A token that only pushes ("ingest") cannot read them, nor can a server
        older than the read API: the sensors that depend on them are then not
        created. A transient error keeps the previous figures.

        One call to the summary covers every vehicle. A server older than the
        summary fields (the month split) is asked vehicle by vehicle instead.
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

            summary: dict[str, dict[str, Any]] = {}
            wanted = set(self._enabled_data_groups())
            due = self._slow_groups_due(wanted, vehicle_ids)
            try:
                fleet_summary = await self.client.async_get_summary(sorted(wanted) if due else [])
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
                self._merge_slow_sections(summary, wanted, due, vehicle_ids)

            monthly: dict[str, dict[str, Any]] = {}
            for vehicle_id in vehicle_ids:
                item = summary.get(vehicle_id, {})
                if isinstance(item.get("month_costs"), dict):
                    monthly[vehicle_id] = {
                        "month": item.get("month"),
                        "in_progress": True,
                        "costs": item["month_costs"],
                    }
                    continue
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

    async def _async_vehicle_metrics(
        self, vehicles_by_id: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Last charge and cost per 100 km of each vehicle.

        They come with the summary when the server carries them (the ``last_charge`` key
        is there, null for a vehicle that cannot charge); otherwise the metrics route is
        asked for each vehicle that lacks them.
        """
        summary = self._read_data.get("summary", {})
        metrics_by_id: dict[str, dict[str, Any]] = {}
        for vehicle_id in vehicles_by_id:
            item = summary.get(vehicle_id, {})
            if "last_charge" in item:
                metrics_by_id[vehicle_id] = _metrics_from_summary(item)
                continue
            try:
                metrics_by_id[vehicle_id] = await self.client.async_get_vehicle_metrics(vehicle_id)
            except AutoLedgerAuthError:
                raise
            except AutoLedgerError as err:
                _LOGGER.warning("Failed to update metrics for vehicle %s: %s", vehicle_id, err)
                metrics_by_id[vehicle_id] = {}
        return metrics_by_id

    def _enabled_keys(self) -> set[str]:
        """What the enabled entities of the entry are made of: their unique id without the entry.

        A vehicle figure reads ``<vehicle>_<key>``, an account figure ``<key>``.
        """
        registry = er.async_get(self.hass)
        prefix = f"{self.entry_id}_"
        return {
            entity.unique_id.removeprefix(prefix)
            for entity in er.async_entries_for_config_entry(registry, self.entry_id)
            if entity.disabled_by is None and entity.unique_id.startswith(prefix)
        }

    @staticmethod
    def _has_key(enabled: set[str], keys: tuple[str, ...]) -> bool:
        return any(rest == key or rest.endswith(f"_{key}") for rest in enabled for key in keys)

    def _enabled_data_groups(self) -> list[str]:
        """Opt-in sections of the summary that an enabled entity needs.

        A disabled entity costs the server nothing: enabling one reloads the entry, so the
        next refresh asks for its section.
        """
        enabled = self._enabled_keys()
        return [group for group, keys in DATA_GROUPS.items() if self._has_key(enabled, keys)]

    def _slow_groups_due(self, wanted: set[str], vehicle_ids: list[str]) -> bool:
        """Whether the opt-in sections are to be read now rather than taken from the last read."""
        if not wanted:
            return False
        return (
            wanted != self._slow_groups
            or not set(vehicle_ids) <= self._slow_vehicles
            or self._slow_at is None
            or dt_util.utcnow() - self._slow_at >= SLOW_GROUPS_INTERVAL
        )

    def _merge_slow_sections(
        self,
        summary: dict[str, dict[str, Any]],
        wanted: set[str],
        due: bool,
        vehicle_ids: list[str],
    ) -> None:
        """Remember the sections just read, or put the remembered ones back in the summary."""
        if due:
            self._slow_at = dt_util.utcnow()
            self._slow_groups = set(wanted)
            self._slow_vehicles = set(vehicle_ids)
            self._slow_sections = {
                vehicle_id: {group: item[group] for group in wanted if group in item}
                for vehicle_id, item in summary.items()
            }
            return
        for vehicle_id, item in summary.items():
            for group, section in self._slow_sections.get(vehicle_id, {}).items():
                if group in wanted:
                    item[group] = section

    def _tariff_wanted(self) -> bool:
        """Whether an entity of the price is enabled, or none was created yet.

        The price is on by default: on the first refresh of a new entry the registry does not
        hold it yet, and it must not wait for the next poll to show a value.
        """
        registry = er.async_get(self.hass)
        prefix = f"{self.entry_id}_"
        known = {
            entity.unique_id.removeprefix(prefix)
            for entity in er.async_entries_for_config_entry(registry, self.entry_id)
            if entity.unique_id.startswith(prefix)
        }
        return not self._has_key(known, TARIFF_KEYS) or self._has_key(
            self._enabled_keys(), TARIFF_KEYS
        )

    async def _async_fetch_tariff(self) -> dict[str, Any] | None:
        """The price of electricity now, read only while one of its entities is enabled.

        A server older than the route, or a failure, leaves the entities unknown.
        """
        if not self._tariff_wanted():
            return None
        try:
            return await self.client.async_get_current_tariff()
        except AutoLedgerAuthError:
            raise
        except AutoLedgerError as err:
            _LOGGER.warning("Failed to read the current electricity price: %s", err)
            return None

    async def _async_fetch_fleet(self) -> dict[str, Any] | None:
        """The household figures, read only while one of their entities is enabled."""
        if not self._has_key(self._enabled_keys(), FLEET_KEYS):
            return None
        try:
            return await self.client.async_get_fleet()
        except AutoLedgerAuthError:
            raise
        except AutoLedgerError as err:
            _LOGGER.warning("Failed to read the AutoLedger fleet figures: %s", err)
            return None

    def _schedule_price_change(self, tariff: dict[str, Any] | None) -> None:
        """Refresh when the price changes, instead of showing the old one until the next poll."""
        if self._price_change_cancel:
            self._price_change_cancel()
            self._price_change_cancel = None
        change = (tariff or {}).get("next_change")
        when = dt_util.parse_datetime(change["at"]) if isinstance(change, dict) else None
        if when is None or when <= dt_util.utcnow():
            return

        @callback
        def _refresh(_now: datetime) -> None:
            self._price_change_cancel = None
            self.hass.async_create_task(self.async_request_refresh())

        self._price_change_cancel = async_track_point_in_time(self.hass, _refresh, when)

    async def async_shutdown(self) -> None:
        """Stop the refresh planned for the next price change."""
        if self._price_change_cancel:
            self._price_change_cancel()
            self._price_change_cancel = None
        await super().async_shutdown()

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
