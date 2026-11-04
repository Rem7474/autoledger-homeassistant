"""Tests for the connection steps of the AutoLedger config flow.

Vehicles and charging stations are subentries: their flows run against the real core in
tests_ha/test_subentry_flows.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntry

from custom_components.autoledger.api import (
    AutoLedgerAuthError,
    AutoLedgerConnectionError,
    AutoLedgerScopeError,
)
from custom_components.autoledger.config_flow import AutoLedgerConfigFlow
from custom_components.autoledger.const import (
    CONF_API_KEY,
    CONF_HOST,
    CONF_VERIFY_SSL,
)


@pytest.mark.asyncio
async def test_flow_user_form_initial(mock_hass):
    """Test initial rendering of user config flow step."""
    flow = AutoLedgerConfigFlow()
    flow.hass = mock_hass

    result = await flow.async_step_user(user_input=None)
    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["errors"] == {}


@pytest.mark.asyncio
async def test_flow_user_success(mock_hass):
    """Test successful config flow submission."""
    flow = AutoLedgerConfigFlow()
    flow.hass = mock_hass

    user_input = {
        CONF_HOST: "http://192.168.1.100:8080",
        CONF_API_KEY: "secret_token_123",
        CONF_VERIFY_SSL: True,
    }

    with patch(
        "custom_components.autoledger.config_flow.AutoLedgerApiClient.async_validate",
        new_callable=AsyncMock,
        return_value=True,
    ):
        result = await flow.async_step_user(user_input=user_input)

    assert result["type"] == "create_entry"
    assert "192.168.1.100:8080" in result["title"]
    assert result["data"][CONF_HOST] == "http://192.168.1.100:8080"
    assert result["data"][CONF_API_KEY] == "secret_token_123"


@pytest.mark.asyncio
async def test_flow_user_invalid_auth(mock_hass):
    """Test config flow with authentication failure."""
    flow = AutoLedgerConfigFlow()
    flow.hass = mock_hass

    user_input = {
        CONF_HOST: "https://autoledger.domain.com",
        CONF_API_KEY: "wrong_token",
        CONF_VERIFY_SSL: True,
    }

    with patch(
        "custom_components.autoledger.config_flow.AutoLedgerApiClient.async_validate",
        side_effect=AutoLedgerAuthError("Invalid credentials"),
    ):
        result = await flow.async_step_user(user_input=user_input)

    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_auth"


@pytest.mark.asyncio
async def test_flow_user_cannot_connect(mock_hass):
    """Test config flow with network connection failure."""
    flow = AutoLedgerConfigFlow()
    flow.hass = mock_hass

    user_input = {
        CONF_HOST: "https://unreachable.host",
        CONF_API_KEY: "any_token",
        CONF_VERIFY_SSL: False,
    }

    with patch(
        "custom_components.autoledger.config_flow.AutoLedgerApiClient.async_validate",
        side_effect=AutoLedgerConnectionError("Unreachable"),
    ):
        result = await flow.async_step_user(user_input=user_input)

    assert result["type"] == "form"
    assert result["errors"]["base"] == "cannot_connect"


@pytest.mark.asyncio
async def test_flow_reconfigure(mock_hass):
    """Test reconfigure flow successfully updating credentials."""
    entry = ConfigEntry(
        entry_id="test_reconfig",
        data={CONF_HOST: "http://old.local", CONF_API_KEY: "old_key"},
    )
    flow = AutoLedgerConfigFlow()
    flow.hass = mock_hass
    flow._reconfigure_entry = entry

    # Initial form
    form = await flow.async_step_reconfigure(user_input=None)
    assert form["type"] == "form"
    assert form["step_id"] == "reconfigure"

    # Submit valid update
    with patch(
        "custom_components.autoledger.config_flow.AutoLedgerApiClient.async_validate",
        new_callable=AsyncMock,
        return_value=True,
    ):
        res = await flow.async_step_reconfigure(
            user_input={
                CONF_HOST: "http://new.local",
                CONF_API_KEY: "new_key",
                CONF_VERIFY_SSL: True,
            }
        )

    assert res["type"] == "abort"
    assert res["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == "http://new.local"
    assert entry.data[CONF_API_KEY] == "new_key"


def _reauth_flow(mock_hass, entry):
    flow = AutoLedgerConfigFlow()
    flow.hass = mock_hass
    flow.source = "reauth"
    flow._reauth_entry = entry
    return flow


def _token_entry(**data):
    return ConfigEntry(
        entry_id="entry_reauth",
        data={CONF_HOST: "http://autoledger.local", CONF_API_KEY: "old", **data},
    )


@pytest.mark.asyncio
async def test_reauth_shows_the_token_form_for_the_entry_host(mock_hass):
    flow = _reauth_flow(mock_hass, _token_entry())

    start = await flow.async_step_reauth({CONF_HOST: "http://autoledger.local"})

    assert start["type"] == "form"
    assert start["step_id"] == "reauth_confirm"
    assert start["description_placeholders"] == {"host": "http://autoledger.local"}


@pytest.mark.asyncio
async def test_reauth_stores_the_new_token(mock_hass):
    entry = _token_entry()
    flow = _reauth_flow(mock_hass, entry)

    with patch(
        "custom_components.autoledger.config_flow.AutoLedgerApiClient.async_validate",
        new_callable=AsyncMock,
        return_value={"scope": "homeassistant"},
    ):
        result = await flow.async_step_reauth_confirm({CONF_API_KEY: "new"})

    assert result == {"type": "abort", "reason": "reauth_successful"}
    assert entry.data[CONF_API_KEY] == "new"
    assert entry.data[CONF_HOST] == "http://autoledger.local"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "error"),
    [
        (AutoLedgerAuthError("no"), "invalid_auth"),
        (AutoLedgerScopeError("read"), "invalid_scope"),
        (AutoLedgerConnectionError("down"), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_reauth_reports_why_the_token_is_not_usable(mock_hass, raised, error):
    entry = _token_entry()
    flow = _reauth_flow(mock_hass, entry)

    with patch(
        "custom_components.autoledger.config_flow.AutoLedgerApiClient.async_validate",
        side_effect=raised,
    ):
        result = await flow.async_step_reauth_confirm({CONF_API_KEY: "new"})

    assert result["type"] == "form"
    assert result["errors"]["base"] == error
    assert entry.data[CONF_API_KEY] == "old"


@pytest.mark.asyncio
async def test_user_step_rejects_a_read_only_token(mock_hass):
    flow = AutoLedgerConfigFlow()
    flow.hass = mock_hass

    with patch(
        "custom_components.autoledger.config_flow.AutoLedgerApiClient.async_validate",
        side_effect=AutoLedgerScopeError("read"),
    ):
        result = await flow.async_step_user(
            {CONF_HOST: "http://autoledger.local", CONF_API_KEY: "al_live_x"}
        )

    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_scope"


@pytest.mark.asyncio
async def test_reconfigure_moves_the_unique_id_with_the_host(mock_hass):
    entry = _token_entry()
    entry.unique_id = "http://autoledger.local"
    flow = AutoLedgerConfigFlow()
    flow.hass = mock_hass
    flow._reconfigure_entry = entry

    with patch(
        "custom_components.autoledger.config_flow.AutoLedgerApiClient.async_validate",
        new_callable=AsyncMock,
        return_value={},
    ):
        result = await flow.async_step_reconfigure(
            {CONF_HOST: "https://New.Example/", CONF_API_KEY: "k"}
        )

    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == "https://New.Example"
    assert entry.unique_id == "https://new.example"


@pytest.mark.asyncio
async def test_reconfigure_refuses_a_host_another_entry_already_uses(mock_hass):
    entry = _token_entry()
    other = ConfigEntry(entry_id="other", data={CONF_HOST: "http://b.local", CONF_API_KEY: "k"})
    other.unique_id = "http://b.local"
    flow = AutoLedgerConfigFlow()
    flow.hass = mock_hass
    flow._reconfigure_entry = entry
    flow._current_entries = [entry, other]

    result = await flow.async_step_reconfigure({CONF_HOST: "http://B.local", CONF_API_KEY: "k"})

    assert result == {"type": "abort", "reason": "already_configured"}
    assert entry.data[CONF_HOST] == "http://autoledger.local"
