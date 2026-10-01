"""Tests for the AutoLedgerApiClient."""

from __future__ import annotations

from unittest.mock import MagicMock

import aiohttp
import pytest

from custom_components.autoledger.api import (
    AutoLedgerApiClient,
    AutoLedgerApiError,
    AutoLedgerAuthError,
    AutoLedgerConnectionError,
    AutoLedgerTimeoutError,
)


class MockClientResponse:
    """Mock aiohttp ClientResponse."""

    def __init__(
        self,
        status: int = 200,
        json_data: dict | list | None = None,
        text_data: str = "",
        headers: dict | None = None,
    ) -> None:
        self.status = status
        self._json_data = json_data
        self._text_data = text_data
        self.headers = headers or {"Content-Type": "application/json"}

    async def json(self):
        return self._json_data

    async def text(self):
        return self._text_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


@pytest.fixture
def mock_session():
    """Mock aiohttp session."""
    session = MagicMock()
    return session


@pytest.mark.asyncio
async def test_connection_success_health(mock_session):
    """Test successful connection via /api/health."""
    mock_session.request.return_value = MockClientResponse(status=200, json_data={"status": "ok"})
    client = AutoLedgerApiClient("http://autoledger.local:8080", "test_key", mock_session)

    result = await client.async_test_connection()
    assert result is True
    assert client.host == "http://autoledger.local:8080"


@pytest.mark.asyncio
async def test_connection_fallback_to_vehicles(mock_session):
    """Test connection falling back to /api/vehicles if /api/health is 404."""
    resp_404 = MockClientResponse(status=404, text_data="Not Found")
    resp_200 = MockClientResponse(status=200, json_data=[])
    mock_session.request.side_effect = [resp_404, resp_200]

    client = AutoLedgerApiClient("http://autoledger.local:8080/", "test_key", mock_session)
    result = await client.async_test_connection()
    assert result is True


@pytest.mark.asyncio
async def test_connection_auth_error(mock_session):
    """Test authentication error handling."""
    mock_session.request.return_value = MockClientResponse(status=401, text_data="Unauthorized")
    client = AutoLedgerApiClient("http://autoledger.local:8080", "invalid_key", mock_session)

    with pytest.raises(AutoLedgerAuthError):
        await client.async_test_connection()


@pytest.mark.asyncio
async def test_connection_timeout(mock_session):
    """Test connection timeout handling."""
    mock_session.request.side_effect = TimeoutError()
    client = AutoLedgerApiClient("http://autoledger.local:8080", "test_key", mock_session)

    with pytest.raises(AutoLedgerTimeoutError):
        await client.async_test_connection()


@pytest.mark.asyncio
async def test_connection_network_error(mock_session):
    """Test connection network/connector error."""
    mock_session.request.side_effect = aiohttp.ClientConnectorError(
        connection_key=MagicMock(), os_error=OSError("Unreachable")
    )
    client = AutoLedgerApiClient("http://autoledger.local:8080", "test_key", mock_session)

    with pytest.raises(AutoLedgerConnectionError):
        await client.async_test_connection()


@pytest.mark.asyncio
async def test_get_vehicles(mock_session):
    """Test fetching vehicles list."""
    vehicles_data = [
        {"id": "v-123", "make": "Tesla", "model": "Model Y", "name": "Red Car"},
        {"id": "v-456", "make": "MG", "model": "MG4", "name": "White Car"},
    ]
    mock_session.request.return_value = MockClientResponse(status=200, json_data=vehicles_data)
    client = AutoLedgerApiClient("http://autoledger.local:8080", "test_key", mock_session)

    vehicles = await client.async_get_vehicles()
    assert len(vehicles) == 2
    assert vehicles[0]["id"] == "v-123"
    assert vehicles[1]["model"] == "MG4"


@pytest.mark.asyncio
async def test_get_vehicle_metrics(mock_session):
    """Test fetching vehicle metrics."""
    metrics_data = {
        "last_charge_cost": 8.75,
        "cost_per_100km": 3.42,
        "currency": "EUR",
        "energy_kwh": 42.5,
        "duration_minutes": 180,
    }
    mock_session.request.return_value = MockClientResponse(status=200, json_data=metrics_data)
    client = AutoLedgerApiClient("http://autoledger.local:8080", "test_key", mock_session)

    metrics = await client.async_get_vehicle_metrics("v-123")
    assert metrics["last_charge_cost"] == 8.75
    assert metrics["cost_per_100km"] == 3.42
    assert metrics["currency"] == "EUR"


@pytest.mark.asyncio
async def test_post_event_success(mock_session):
    """Test posting charging event successfully to /api/integrations/homeassistant/event."""
    mock_session.request.return_value = MockClientResponse(status=200, json_data={"status": "ok"})
    client = AutoLedgerApiClient("http://autoledger.local:8080", "test_key", mock_session)

    payload = {
        "vehicle_id": "v-123",
        "event_type": "charging_session_end",
        "source": "homeassistant",
        "data": {
            "energy_added_kwh": 25.0,
            "odometer_km": 15000.0,
        },
    }

    res = await client.async_post_event(payload)
    assert res == {"status": "ok"}
    assert (
        mock_session.request.call_args[1]["url"]
        == "http://autoledger.local:8080/api/integrations/homeassistant/event"
    )


@pytest.mark.asyncio
async def test_post_event_fallback_to_charges(mock_session):
    """Test fallback to /api/vehicles/{id}/charges when event endpoint returns 404."""
    resp_404 = MockClientResponse(status=404, text_data="Not Found")
    resp_200 = MockClientResponse(status=200, json_data={"charge_id": "ch-789"})
    mock_session.request.side_effect = [resp_404, resp_200]

    client = AutoLedgerApiClient("http://autoledger.local:8080", "test_key", mock_session)

    payload = {
        "vehicle_id": "v-123",
        "event_type": "charging_session_end",
        "source": "homeassistant",
        "data": {
            "energy_added_kwh": 30.5,
            "odometer_km": 18500.0,
            "soc_start": 20,
            "soc_end": 80,
            "location": "home",
        },
    }

    res = await client.async_post_event(payload)
    assert res == {"charge_id": "ch-789"}
    # Second call should have targeted /api/vehicles/v-123/charges
    second_call_url = mock_session.request.call_args_list[1][1]["url"]
    assert second_call_url == "http://autoledger.local:8080/api/vehicles/v-123/charges"


@pytest.mark.asyncio
async def test_submit_charge(mock_session):
    """Test async_submit_charge helper."""
    mock_session.request.return_value = MockClientResponse(
        status=200, json_data={"status": "success"}
    )
    client = AutoLedgerApiClient("http://autoledger.local:8080", "test_key", mock_session)

    res = await client.async_submit_charge(
        vehicle_id="v-123",
        kwh=22.4,
        cost=5.60,
        odometer_km=25400.0,
        soc_start=30,
        soc_end=75,
    )
    assert res == {"status": "success"}


@pytest.mark.asyncio
async def test_post_event_none_vehicle_id_success(mock_session):
    """Test posting charging event with vehicle_id=None (unassigned session)."""
    mock_session.request.return_value = MockClientResponse(status=200, json_data={"status": "ok"})
    client = AutoLedgerApiClient("http://autoledger.local:8080", "test_key", mock_session)

    payload = {
        "vehicle_id": None,
        "event_type": "charging_session_end",
        "source": "homeassistant",
        "data": {
            "charger_name": "Wallbox Garage",
            "energy_added_kwh": 18.2,
        },
    }

    res = await client.async_post_event(payload)
    assert res == {"status": "ok"}
    assert (
        mock_session.request.call_args[1]["url"]
        == "http://autoledger.local:8080/api/integrations/homeassistant/event"
    )


@pytest.mark.asyncio
async def test_post_event_none_vehicle_id_no_fallback_on_404(mock_session):
    """Test that when vehicle_id is None and event endpoint is 404, no fallback is attempted."""
    mock_session.request.return_value = MockClientResponse(status=404, text_data="Not Found")
    client = AutoLedgerApiClient("http://autoledger.local:8080", "test_key", mock_session)

    payload = {
        "vehicle_id": None,
        "event_type": "charging_session_end",
        "source": "homeassistant",
        "data": {
            "charger_name": "Wallbox Garage",
            "energy_added_kwh": 18.2,
        },
    }

    with pytest.raises(AutoLedgerApiError) as exc_info:
        await client.async_post_event(payload)

    assert exc_info.value.status_code == 404
    # Ensure only 1 call was made (no fallback call to /api/vehicles/{id}/charges)
    assert mock_session.request.call_count == 1


@pytest.mark.asyncio
async def test_update_odometer_success(mock_session):
    """Test successful odometer update via /api/integrations/homeassistant/event."""
    mock_session.request.return_value = MockClientResponse(
        status=200, json_data={"status": "recorded"}
    )
    client = AutoLedgerApiClient("http://autoledger.local:8080", "test_key", mock_session)

    res = await client.async_update_odometer("veh-123", 52300.4)
    assert res == {"status": "recorded"}
    assert (
        mock_session.request.call_args[1]["url"]
        == "http://autoledger.local:8080/api/integrations/homeassistant/event"
    )
    assert mock_session.request.call_args[1]["json"]["data"]["odometer_km"] == 52300.4
    assert mock_session.request.call_args[1]["json"]["event_type"] == "odometer_update"


@pytest.mark.asyncio
async def test_update_odometer_fallback_on_404(mock_session):
    """Test odometer update falls back to /api/vehicles/{id}/odometer-checkpoints when event endpoint is 404."""
    resp_404 = MockClientResponse(status=404, text_data="Not Found")
    resp_fallback = MockClientResponse(status=201, json_data={"id": "cp-1", "odometer": 52300.4})
    mock_session.request.side_effect = [resp_404, resp_fallback]

    client = AutoLedgerApiClient("http://autoledger.local:8080", "test_key", mock_session)
    res = await client.async_update_odometer("veh-123", 52300.4)

    assert res == {"id": "cp-1", "odometer": 52300.4}
    assert mock_session.request.call_count == 2
    assert (
        mock_session.request.call_args_list[0][1]["url"]
        == "http://autoledger.local:8080/api/integrations/homeassistant/event"
    )
    assert (
        mock_session.request.call_args_list[1][1]["url"]
        == "http://autoledger.local:8080/api/vehicles/veh-123/odometer-checkpoints"
    )
    assert mock_session.request.call_args_list[1][1]["json"]["odometer"] == 52300.4
