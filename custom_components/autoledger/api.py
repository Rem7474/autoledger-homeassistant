"""API Client for AutoLedger integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .const import EVENT_TYPE_ODOMETER_UPDATE

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15


class AutoLedgerError(HomeAssistantError):
    """Base exception for AutoLedger errors."""


class AutoLedgerApiError(AutoLedgerError):
    """Exception for AutoLedger API response errors."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        """Initialize API error."""
        super().__init__(message)
        self.status_code = status_code


class AutoLedgerConnectionError(AutoLedgerError):
    """Exception for network connection errors."""


class AutoLedgerAuthError(AutoLedgerError):
    """Exception for authentication errors."""


class AutoLedgerTimeoutError(AutoLedgerError):
    """Exception for network timeout errors."""


class AutoLedgerApiClient:
    """Async API Client for AutoLedger."""

    def __init__(
        self,
        host: str,
        api_key: str,
        session: aiohttp.ClientSession,
        verify_ssl: bool = True,
    ) -> None:
        """Initialize the API client."""
        self._host = host.rstrip("/")
        self._api_key = api_key
        self._session = session
        self._verify_ssl = verify_ssl

    @property
    def host(self) -> str:
        """Return the host."""
        return self._host

    def _get_headers(self) -> dict[str, str]:
        """Return request headers with Bearer token authentication."""
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        endpoint: str,
        **kwargs: Any,
    ) -> Any:
        """Execute an asynchronous HTTP request."""
        url = f"{self._host}{endpoint}"
        headers = self._get_headers()

        if "headers" in kwargs:
            headers.update(kwargs.pop("headers"))

        ssl_ctx = kwargs.pop("ssl", None)
        if ssl_ctx is None and not self._verify_ssl:
            ssl_ctx = False

        timeout = kwargs.pop("timeout", aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT))

        try:
            async with self._session.request(
                method=method,
                url=url,
                headers=headers,
                ssl=ssl_ctx,
                timeout=timeout,
                **kwargs,
            ) as response:
                if response.status in (401, 403):
                    error_text = await response.text()
                    _LOGGER.warning(
                        "Authentication error calling %s: HTTP %s - %s",
                        url,
                        response.status,
                        error_text,
                    )
                    raise AutoLedgerAuthError(
                        f"Authentication failed (HTTP {response.status}): {error_text}"
                    )

                if response.status == 404:
                    raise AutoLedgerApiError(
                        f"Resource not found (HTTP 404): {url}", status_code=404
                    )

                if response.status >= 400:
                    error_text = await response.text()
                    _LOGGER.error(
                        "AutoLedger API returned error HTTP %s for %s: %s",
                        response.status,
                        url,
                        error_text,
                    )
                    raise AutoLedgerApiError(
                        f"API Error (HTTP {response.status}): {error_text}",
                        status_code=response.status,
                    )

                content_type = response.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    return await response.json()
                return await response.text()

        except (
            aiohttp.ClientConnectorError,
            aiohttp.ClientOSError,
            aiohttp.ServerDisconnectedError,
        ) as err:
            _LOGGER.error("Cannot connect to AutoLedger at %s: %s", url, err)
            raise AutoLedgerConnectionError(
                f"Failed to connect to AutoLedger at {url}: {err}"
            ) from err
        except TimeoutError as err:
            _LOGGER.error("Timeout connecting to AutoLedger at %s", url)
            raise AutoLedgerTimeoutError(
                f"Timeout after {DEFAULT_TIMEOUT}s connecting to AutoLedger at {url}"
            ) from err
        except AutoLedgerError:
            raise
        except Exception as err:
            _LOGGER.exception("Unexpected error while communicating with AutoLedger: %s", err)
            raise AutoLedgerApiError(f"Unexpected error: {err}") from err

    async def async_test_connection(self) -> bool:
        """Test API connectivity and credentials."""
        try:
            await self._request("GET", "/api/health")
            return True
        except AutoLedgerApiError as err:
            if err.status_code == 404:
                # If /api/health does not exist, try /api/vehicles
                await self._request("GET", "/api/vehicles")
                return True
            raise

    async def async_get_vehicles(self) -> list[dict[str, Any]]:
        """Fetch list of all vehicles from AutoLedger."""
        res = await self._request("GET", "/api/vehicles")
        if isinstance(res, list):
            return res
        if isinstance(res, dict) and "vehicles" in res and isinstance(res["vehicles"], list):
            return res["vehicles"]
        return []

    async def async_get_vehicle_metrics(self, vehicle_id: str) -> dict[str, Any]:
        """Fetch latest metrics and financial summary for a vehicle."""
        try:
            res = await self._request("GET", f"/api/vehicles/{vehicle_id}/metrics")
            if isinstance(res, dict):
                return res
        except AutoLedgerApiError as err:
            if err.status_code != 404:
                raise

        # Fallback to vehicle endpoint
        res = await self._request("GET", f"/api/vehicles/{vehicle_id}")
        if isinstance(res, dict):
            # Extract nested metrics if present or return vehicle data
            return res.get("metrics", res)
        return {}

    async def async_post_event(self, event_data: dict[str, Any]) -> dict[str, Any]:
        """Post a charging event to AutoLedger with automatic fallback."""
        try:
            res = await self._request(
                "POST",
                "/api/integrations/homeassistant/event",
                json=event_data,
            )
            return res if isinstance(res, dict) else {"status": "success"}
        except AutoLedgerApiError as err:
            vehicle_id = event_data.get("vehicle_id")
            if err.status_code == 404 and vehicle_id is not None:
                # Fallback to POST /api/vehicles/{vehicleId}/charges
                data = event_data.get("data", {})
                charge_payload = {
                    "odometer_km": data.get("odometer_km"),
                    "kwh": data.get("energy_added_kwh"),
                    "soc_start": data.get("soc_start"),
                    "soc_end": data.get("soc_end"),
                    "start_time": data.get("start_time"),
                    "end_time": data.get("end_time"),
                    "location": data.get("location"),
                    "total_cost": data.get("cost"),
                }
                _LOGGER.info(
                    "Endpoint /api/integrations/homeassistant/event not found. "
                    "Falling back to /api/vehicles/%s/charges",
                    vehicle_id,
                )
                res = await self._request(
                    "POST",
                    f"/api/vehicles/{vehicle_id}/charges",
                    json=charge_payload,
                )
                return res if isinstance(res, dict) else {"status": "success"}
            raise

    async def async_submit_charge(
        self,
        vehicle_id: str | None,
        kwh: float,
        cost: float | None = None,
        odometer_km: float | None = None,
        soc_start: int | None = None,
        soc_end: int | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        location: str = "home",
    ) -> dict[str, Any]:
        """Directly submit a charging session."""
        charge_payload = {
            "vehicle_id": vehicle_id,
            "event_type": "charging_session_end",
            "source": "homeassistant_manual",
            "timestamp": end_time or start_time,
            "data": {
                "start_time": start_time,
                "end_time": end_time,
                "energy_added_kwh": kwh,
                "odometer_km": odometer_km,
                "soc_start": soc_start,
                "soc_end": soc_end,
                "location": location,
                "cost": cost,
            },
        }
        return await self.async_post_event(charge_payload)

    async def async_update_odometer(
        self,
        vehicle_id: str,
        odometer_km: float,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        """Send updated vehicle odometer reading to AutoLedger."""
        payload = {
            "vehicle_id": vehicle_id,
            "event_type": EVENT_TYPE_ODOMETER_UPDATE,
            "source": "homeassistant",
            "timestamp": timestamp or dt_util.utcnow().isoformat(),
            "data": {
                "odometer_km": round(odometer_km, 1),
            },
        }
        try:
            res = await self._request(
                "POST",
                "/api/integrations/homeassistant/event",
                json=payload,
            )
            return res if isinstance(res, dict) else {"status": "success"}
        except AutoLedgerApiError as err:
            # Fallback to odometer-checkpoints if /api/integrations/homeassistant/event is 404
            if err.status_code == 404:
                _LOGGER.info(
                    "Endpoint /api/integrations/homeassistant/event returned 404. "
                    "Falling back to /api/vehicles/%s/odometer-checkpoints",
                    vehicle_id,
                )
                checkpoint_payload = {
                    "date": timestamp or dt_util.utcnow().isoformat(),
                    "odometer": round(odometer_km, 1),
                    "notes": "Home Assistant sync",
                }
                res = await self._request(
                    "POST",
                    f"/api/vehicles/{vehicle_id}/odometer-checkpoints",
                    json=checkpoint_payload,
                )
                return res if isinstance(res, dict) else {"status": "success"}
            raise
