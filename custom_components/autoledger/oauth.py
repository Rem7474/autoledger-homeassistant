"""OAuth connection to an AutoLedger server.

AutoLedger speaks OAuth 2.1 with dynamic client registration (RFC 7591), PKCE S256
and a refresh token that works once: presenting it a second time revokes the grant.
The helpers here discover the server, register Home Assistant as a public client,
and keep the access token fresh. Home Assistant's own ``OAuth2Session`` serializes
the refreshes and writes the new token to the config entry before any further call.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AutoLedgerApiClient, AutoLedgerAuthError, AutoLedgerConnectionError
from .const import (
    AUTH_METHOD_OAUTH,
    CONF_API_KEY,
    CONF_AUTH_METHOD,
    CONF_AUTHORIZE_URL,
    CONF_CLIENT_ID,
    CONF_HOST,
    CONF_TOKEN_URL,
    CONF_VERIFY_SSL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    OAUTH_SCOPE,
)

_LOGGER = logging.getLogger(__name__)

METADATA_PATH = "/.well-known/oauth-authorization-server"
TIMEOUT = aiohttp.ClientTimeout(total=15)
CLIENT_NAME = "Home Assistant"


class OAuthUnavailableError(Exception):
    """The server does not offer what Home Assistant needs to connect with OAuth."""


async def _json(
    hass: HomeAssistant, method: str, url: str, verify_ssl: bool, **kwargs: Any
) -> tuple[int, Any]:
    """Send a request and return the status with the decoded JSON body (None if not JSON)."""
    session = async_get_clientsession(hass)
    try:
        async with session.request(
            method, url, ssl=None if verify_ssl else False, timeout=TIMEOUT, **kwargs
        ) as response:
            try:
                body = await response.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                body = None
            return response.status, body
    except (TimeoutError, aiohttp.ClientError) as err:
        raise AutoLedgerConnectionError(str(err)) from err


async def async_discover(hass: HomeAssistant, host: str, verify_ssl: bool) -> dict[str, str]:
    """Read the OAuth endpoints of the server.

    Raises ``OAuthUnavailableError`` when the instance does not serve OAuth (it is off
    by default on the server) or does not allow the ``homeassistant`` scope, and
    ``AutoLedgerConnectionError`` when it cannot be reached.
    """
    status, body = await _json(hass, "GET", f"{host.rstrip('/')}{METADATA_PATH}", verify_ssl)
    if status != 200 or not isinstance(body, dict):
        raise OAuthUnavailableError(f"metadata answered HTTP {status}")

    endpoints = {
        key: body.get(key)
        for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint")
    }
    if not all(isinstance(value, str) and value for value in endpoints.values()):
        raise OAuthUnavailableError("incomplete server metadata")
    scopes = body.get("scopes_supported")
    if isinstance(scopes, list) and OAUTH_SCOPE not in scopes:
        raise OAuthUnavailableError(f"scope {OAUTH_SCOPE} is not offered")
    return {
        CONF_AUTHORIZE_URL: endpoints["authorization_endpoint"],
        CONF_TOKEN_URL: endpoints["token_endpoint"],
        "registration_endpoint": endpoints["registration_endpoint"],
    }


async def async_register_client(
    hass: HomeAssistant, registration_endpoint: str, redirect_uri: str, verify_ssl: bool
) -> str:
    """Register Home Assistant as a public client and return its client id."""
    status, body = await _json(
        hass,
        "POST",
        registration_endpoint,
        verify_ssl,
        json={
            "client_name": CLIENT_NAME,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": OAUTH_SCOPE,
        },
    )
    client_id = body.get("client_id") if isinstance(body, dict) else None
    if status not in (200, 201) or not isinstance(client_id, str) or not client_id:
        raise OAuthUnavailableError(f"client registration answered HTTP {status}")
    return client_id


class AutoLedgerOAuth2Implementation(config_entry_oauth2_flow.LocalOAuth2ImplementationWithPkce):
    """Authorization code flow with PKCE for a public client registered on the fly."""

    def __init__(
        self,
        hass: HomeAssistant,
        client_id: str,
        authorize_url: str,
        token_url: str,
        verify_ssl: bool = DEFAULT_VERIFY_SSL,
    ) -> None:
        """Initialize the implementation."""
        super().__init__(hass, DOMAIN, client_id, authorize_url, token_url)
        self._verify_ssl = verify_ssl

    @property
    def name(self) -> str:
        """Name of the implementation."""
        return "AutoLedger"

    @property
    def extra_authorize_data(self) -> dict:
        """Ask for the scope that lets Home Assistant record and read."""
        return {"scope": OAUTH_SCOPE, **super().extra_authorize_data}

    async def _token_request(self, data: dict) -> dict:
        """Call the token endpoint; a public client sends no secret."""
        status, body = await _json(
            self.hass,
            "POST",
            self.token_url,
            self._verify_ssl,
            data={**data, "client_id": self.client_id},
        )
        if status >= 500 or (body is None and status != 200):
            raise AutoLedgerConnectionError(f"token endpoint answered HTTP {status}")
        if status >= 400:
            # invalid_grant: the refresh token was used, expired or the grant revoked.
            error = body.get("error", "unknown") if isinstance(body, dict) else "unknown"
            raise AutoLedgerAuthError(f"token endpoint refused the request: {error}")
        if not isinstance(body, dict) or not body.get("access_token"):
            raise AutoLedgerAuthError("token endpoint returned no access token")
        return body


def implementation_from_entry_data(
    hass: HomeAssistant, data: dict[str, Any]
) -> AutoLedgerOAuth2Implementation:
    """Rebuild the implementation of a config entry from what it stored."""
    return AutoLedgerOAuth2Implementation(
        hass,
        data[CONF_CLIENT_ID],
        data[CONF_AUTHORIZE_URL],
        data[CONF_TOKEN_URL],
        data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
    )


def oauth_entry_data(
    host: str,
    verify_ssl: bool,
    endpoints: dict[str, str],
    client_id: str,
    token: dict[str, Any],
) -> dict[str, Any]:
    """The data of a config entry connected with OAuth."""
    return {
        CONF_HOST: host,
        CONF_VERIFY_SSL: verify_ssl,
        CONF_CLIENT_ID: client_id,
        CONF_AUTHORIZE_URL: endpoints[CONF_AUTHORIZE_URL],
        CONF_TOKEN_URL: endpoints[CONF_TOKEN_URL],
        "token": token,
    }


def _oauth_session(
    hass: HomeAssistant, entry: ConfigEntry
) -> config_entry_oauth2_flow.OAuth2Session:
    """The one OAuth session of an entry.

    The refresh token works once, so two refreshes racing (the coordinator and a
    configuration flow, each with its own client) would revoke the grant. They share
    the session, and so its lock.
    """
    sessions: dict[str, config_entry_oauth2_flow.OAuth2Session] = hass.data.setdefault(
        f"{DOMAIN}_oauth_sessions", {}
    )
    session = sessions.get(entry.entry_id)
    if session is None or session.implementation.client_id != entry.data.get(CONF_CLIENT_ID):
        session = config_entry_oauth2_flow.OAuth2Session(
            hass, entry, implementation_from_entry_data(hass, dict(entry.data))
        )
        sessions[entry.entry_id] = session
    return session


def build_client(hass: HomeAssistant, entry: ConfigEntry) -> AutoLedgerApiClient:
    """Build the API client of an entry, with a static token or an OAuth session."""
    session = async_get_clientsession(hass)
    verify_ssl = entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
    if entry.data.get(CONF_AUTH_METHOD) != AUTH_METHOD_OAUTH:
        return AutoLedgerApiClient(
            host=entry.data[CONF_HOST],
            api_key=entry.data[CONF_API_KEY],
            session=session,
            verify_ssl=verify_ssl,
        )

    oauth_session = _oauth_session(hass, entry)

    async def access_token() -> str:
        # The session holds a lock around the refresh and stores the new token in the
        # entry before returning: a single-use refresh token is never sent twice.
        await oauth_session.async_ensure_token_valid()
        return str(oauth_session.token["access_token"])

    return AutoLedgerApiClient(
        host=entry.data[CONF_HOST],
        api_key="",
        session=session,
        verify_ssl=verify_ssl,
        token_provider=access_token,
    )
