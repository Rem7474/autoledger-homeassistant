"""Connecting with OAuth, driven by the real Home Assistant core.

The AutoLedger server is simulated at the HTTP level (discovery, dynamic client
registration, token endpoint); the authorization redirect goes through the real
callback view of Home Assistant.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.autoledger.api import AutoLedgerApiClient, AutoLedgerScopeError
from custom_components.autoledger.const import (
    AUTH_METHOD_OAUTH,
    AUTH_METHOD_TOKEN,
    CONF_API_KEY,
    CONF_AUTH_METHOD,
    CONF_HOST,
    CONF_VERIFY_SSL,
    DOMAIN,
)

HOST = "http://autoledger.local:8080"
METADATA = f"{HOST}/.well-known/oauth-authorization-server"
REGISTER = f"{HOST}/oauth/register"
TOKEN = f"{HOST}/oauth/token"
AUTHORIZE = f"{HOST}/oauth/authorize"
VALIDATE = f"{AutoLedgerApiClient.__module__}.AutoLedgerApiClient.async_validate"
MY = "https://my.home-assistant.io/redirect/oauth"


@pytest.fixture(autouse=True)
async def _http(hass: HomeAssistant) -> None:
    """The callback view of the authorization redirect, and the My Home Assistant link."""
    hass.config.components.add("my")
    assert await async_setup_component(hass, "auth", {})


def _server(
    aioclient_mock,
    *,
    metadata: dict | None = None,
    token: dict | None = None,
    client_id: str = "client-1",
) -> None:
    aioclient_mock.get(
        METADATA,
        json=metadata
        or {
            "issuer": HOST,
            "authorization_endpoint": AUTHORIZE,
            "token_endpoint": TOKEN,
            "registration_endpoint": REGISTER,
            "scopes_supported": ["read", "write", "homeassistant"],
        },
    )
    aioclient_mock.post(REGISTER, json={"client_id": client_id}, status=201)
    aioclient_mock.post(
        TOKEN,
        json=token
        or {
            "access_token": "al_live_first",
            "refresh_token": "al_refresh_first",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "homeassistant",
        },
    )


def _calls(aioclient_mock, method: str, url: str) -> list:
    return [c for c in aioclient_mock.mock_calls if c[0] == method and str(c[1]) == url]


async def _authorize(hass, hass_client_no_auth, result, **callback) -> dict:
    """Follow the external step: open the redirect the way the browser would."""
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    state = parse_qs(urlparse(result["url"]).query)["state"][0]
    client = await hass_client_no_auth()
    query = "&".join(f"{k}={v}" for k, v in (callback or {"code": "the-code"}).items())
    resp = await client.get(f"/auth/external/callback?{query}&state={state}")
    assert resp.status == 200
    return await hass.config_entries.flow.async_configure(result["flow_id"])


async def _start_oauth(hass, hass_client_no_auth, host: str = HOST, **callback):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["oauth", "token"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "oauth"}
    )
    assert result["step_id"] == "oauth"
    return await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: host})


async def test_connect_with_oauth(hass, hass_client_no_auth, aioclient_mock) -> None:
    _server(aioclient_mock)

    result = await _start_oauth(hass, hass_client_no_auth)

    # the browser is sent to AutoLedger with PKCE and the scope Home Assistant needs
    query = parse_qs(urlparse(result["url"]).query)
    assert result["url"].startswith(AUTHORIZE)
    assert query["client_id"] == ["client-1"]
    assert query["scope"] == ["homeassistant"]
    assert query["redirect_uri"] == [MY]
    assert query["code_challenge_method"] == ["S256"]
    registration = _calls(aioclient_mock, "POST", REGISTER)[0][2]
    assert registration["redirect_uris"] == [MY]
    assert registration["token_endpoint_auth_method"] == "none"
    assert registration["scope"] == "homeassistant"

    with patch(VALIDATE, new=AsyncMock(return_value={"scope": "homeassistant"})):
        result = await _authorize(hass, hass_client_no_auth, result)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.unique_id == HOST
    assert entry.version == 2
    assert entry.data[CONF_AUTH_METHOD] == AUTH_METHOD_OAUTH
    assert entry.data["client_id"] == "client-1"
    assert entry.data["token"]["access_token"] == "al_live_first"
    assert entry.data["token"]["refresh_token"] == "al_refresh_first"
    assert entry.data["token"]["expires_at"] > time.time()
    assert CONF_API_KEY not in entry.data
    # the code is exchanged with the PKCE verifier and without any client secret
    exchange = _calls(aioclient_mock, "POST", TOKEN)[0][2]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["code"] == "the-code"
    assert exchange["client_id"] == "client-1"
    assert len(exchange["code_verifier"]) >= 43
    assert "client_secret" not in exchange


async def test_a_server_without_oauth_is_reported(
    hass, hass_client_no_auth, aioclient_mock
) -> None:
    aioclient_mock.get(METADATA, status=404)

    result = await _start_oauth(hass, hass_client_no_auth)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "oauth_unavailable"}


async def test_a_server_that_does_not_offer_the_scope_is_reported(
    hass, hass_client_no_auth, aioclient_mock
) -> None:
    _server(
        aioclient_mock,
        metadata={
            "authorization_endpoint": AUTHORIZE,
            "token_endpoint": TOKEN,
            "registration_endpoint": REGISTER,
            "scopes_supported": ["read"],
        },
    )

    result = await _start_oauth(hass, hass_client_no_auth)

    assert result["errors"] == {"base": "oauth_unavailable"}
    assert not _calls(aioclient_mock, "POST", REGISTER)


async def test_the_access_must_include_the_homeassistant_scope(
    hass, hass_client_no_auth, aioclient_mock
) -> None:
    """The consent page gives `read` only unless the user ticks the recording box."""
    _server(aioclient_mock)
    result = await _start_oauth(hass, hass_client_no_auth)

    with patch(VALIDATE, new=AsyncMock(side_effect=AutoLedgerScopeError("read"))):
        result = await _authorize(hass, hass_client_no_auth, result)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "oauth_scope_not_granted"


async def test_refusing_the_authorization_aborts(hass, hass_client_no_auth, aioclient_mock) -> None:
    _server(aioclient_mock)
    result = await _start_oauth(hass, hass_client_no_auth)

    result = await _authorize(hass, hass_client_no_auth, result, error="access_denied")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "user_rejected_authorize"
    assert not _calls(aioclient_mock, "POST", TOKEN)


async def test_a_server_already_configured_is_not_registered_again(
    hass, hass_client_no_auth, aioclient_mock
) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=HOST, version=2, data={CONF_HOST: HOST}).add_to_hass(
        hass
    )
    _server(aioclient_mock)

    result = await _start_oauth(hass, hass_client_no_auth)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert not _calls(aioclient_mock, "POST", REGISTER)


# ---------------------------------------------------------------- tokens over time


def _oauth_entry(*, expires_in: float) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=HOST,
        version=2,
        data={
            CONF_HOST: HOST,
            CONF_VERIFY_SSL: True,
            CONF_AUTH_METHOD: AUTH_METHOD_OAUTH,
            "client_id": "client-1",
            "authorize_url": AUTHORIZE,
            "token_url": TOKEN,
            "token": {
                "access_token": "al_live_old",
                "refresh_token": "al_refresh_old",
                "token_type": "Bearer",
                "expires_in": 3600,
                "expires_at": time.time() + expires_in,
            },
        },
    )


def _api(aioclient_mock) -> None:
    aioclient_mock.get(f"{HOST}/api/integrations/homeassistant/vehicles", json=[])
    aioclient_mock.get(f"{HOST}/api/integrations/v1/whoami", json={"scope": "homeassistant"})
    aioclient_mock.get(f"{HOST}/api/integrations/v1/reminders", json={"vehicles": []})
    aioclient_mock.get(f"{HOST}/api/integrations/v1/costs", json={})


async def test_an_expired_access_token_is_refreshed_and_stored(hass, aioclient_mock) -> None:
    entry = _oauth_entry(expires_in=-10)
    entry.add_to_hass(hass)
    _server(
        aioclient_mock,
        token={
            "access_token": "al_live_new",
            "refresh_token": "al_refresh_new",
            "token_type": "Bearer",
            "expires_in": 3600,
        },
    )
    _api(aioclient_mock)

    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    refresh = _calls(aioclient_mock, "POST", TOKEN)
    assert len(refresh) == 1
    assert refresh[0][2]["grant_type"] == "refresh_token"
    assert refresh[0][2]["refresh_token"] == "al_refresh_old"
    # the rotated refresh token is stored before anything else uses the grant
    assert entry.data["token"]["refresh_token"] == "al_refresh_new"
    # the API is called with the new access token, never the old one
    auth = {
        c[3]["Authorization"]
        for c in aioclient_mock.mock_calls
        if str(c[1]).startswith(f"{HOST}/api/")
    }
    assert auth == {"Bearer al_live_new"}
    # storing a refreshed token is not a configuration change: no reload
    reload.assert_not_called()


async def test_a_valid_access_token_is_not_refreshed(hass, aioclient_mock) -> None:
    entry = _oauth_entry(expires_in=3000)
    entry.add_to_hass(hass)
    _server(aioclient_mock)
    _api(aioclient_mock)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert not _calls(aioclient_mock, "POST", TOKEN)
    assert entry.data["token"]["access_token"] == "al_live_old"


async def test_a_refused_refresh_token_starts_reauthorization(
    hass, hass_client_no_auth, aioclient_mock
) -> None:
    entry = _oauth_entry(expires_in=-10)
    entry.add_to_hass(hass)
    _server(aioclient_mock)
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        METADATA,
        json={
            "authorization_endpoint": AUTHORIZE,
            "token_endpoint": TOKEN,
            "registration_endpoint": REGISTER,
        },
    )
    aioclient_mock.post(REGISTER, json={"client_id": "client-2"}, status=201)
    aioclient_mock.post(TOKEN, json={"error": "invalid_grant"}, status=400)
    _api(aioclient_mock)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [config_entries.SOURCE_REAUTH]
    assert flows[0]["step_id"] == "reauth_oauth"

    # the user approves again: the token endpoint now accepts the new code
    aioclient_mock.clear_requests()
    _server(
        aioclient_mock,
        token={
            "access_token": "al_live_again",
            "refresh_token": "al_refresh_again",
            "expires_in": 3600,
        },
        client_id="client-2",
    )
    _api(aioclient_mock)
    result = await hass.config_entries.flow.async_configure(flows[0]["flow_id"], {})
    with patch(VALIDATE, new=AsyncMock(return_value={"scope": "homeassistant"})):
        result = await _authorize(hass, hass_client_no_auth, result)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["token"]["access_token"] == "al_live_again"
    assert entry.data["client_id"] == "client-2"


async def test_a_token_entry_switches_to_oauth_in_place(
    hass, hass_client_no_auth, aioclient_mock
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=HOST,
        version=2,
        data={
            CONF_HOST: HOST,
            CONF_API_KEY: "al_live_static",
            CONF_VERIFY_SSL: True,
            CONF_AUTH_METHOD: AUTH_METHOD_TOKEN,
        },
    )
    entry.add_to_hass(hass)
    _server(aioclient_mock)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "oauth"}
    )
    # the form is prefilled with the server of the entry
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: HOST})
    with (
        patch(VALIDATE, new=AsyncMock(return_value={"scope": "homeassistant"})),
        patch("custom_components.autoledger.async_setup_entry", new=AsyncMock(return_value=True)),
    ):
        result = await _authorize(hass, hass_client_no_auth, result)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_AUTH_METHOD] == AUTH_METHOD_OAUTH
    assert CONF_API_KEY not in entry.data
    assert entry.data["token"]["access_token"] == "al_live_first"
    assert entry.unique_id == HOST


async def test_concurrent_requests_refresh_the_token_once(hass, aioclient_mock) -> None:
    """A refresh token works once: two calls racing must not both spend it."""
    import asyncio

    from custom_components.autoledger.oauth import build_client

    entry = _oauth_entry(expires_in=-10)
    entry.add_to_hass(hass)
    _server(
        aioclient_mock,
        token={
            "access_token": "al_live_new",
            "refresh_token": "al_refresh_new",
            "expires_in": 3600,
        },
    )
    _api(aioclient_mock)

    # the coordinator's client and the client of a configuration flow
    first, second = build_client(hass, entry), build_client(hass, entry)
    await asyncio.gather(first.async_get_vehicles(), second.async_get_vehicles())

    assert len(_calls(aioclient_mock, "POST", TOKEN)) == 1
    assert entry.data["token"]["refresh_token"] == "al_refresh_new"
