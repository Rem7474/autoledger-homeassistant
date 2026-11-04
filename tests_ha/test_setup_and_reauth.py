"""Connection, setup and re-authentication driven by the real Home Assistant core."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.autoledger.api import (
    AutoLedgerApiClient,
    AutoLedgerAuthError,
    AutoLedgerScopeError,
)
from custom_components.autoledger.const import (
    CONF_API_KEY,
    CONF_HOST,
    CONF_VERIFY_SSL,
    DOMAIN,
)

HOST = "http://autoledger.local:8080"
VALIDATE = f"{AutoLedgerApiClient.__module__}.AutoLedgerApiClient.async_validate"
VEHICLES = f"{AutoLedgerApiClient.__module__}.AutoLedgerApiClient.async_get_vehicles"
METRICS = f"{AutoLedgerApiClient.__module__}.AutoLedgerApiClient.async_get_vehicle_metrics"


def _entry(**data) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=HOST,
        data={CONF_HOST: HOST, CONF_API_KEY: "old", CONF_VERIFY_SSL: True, **data},
    )


async def test_user_flow_creates_the_entry(hass: HomeAssistant) -> None:
    with patch(VALIDATE, new=AsyncMock(return_value={"scope": "homeassistant"})):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: HOST + "/", CONF_API_KEY: "al_live_x"}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.unique_id == HOST
    assert entry.data[CONF_HOST] == HOST
    assert entry.data[CONF_API_KEY] == "al_live_x"


async def test_user_flow_refuses_a_read_only_token(hass: HomeAssistant) -> None:
    with patch(VALIDATE, new=AsyncMock(side_effect=AutoLedgerScopeError("read"))):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: HOST, CONF_API_KEY: "al_live_x"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_scope"}


async def test_revoked_token_starts_reauthentication_and_recovers(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)

    with patch(VEHICLES, new=AsyncMock(side_effect=AutoLedgerAuthError("revoked"))):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [config_entries.SOURCE_REAUTH]

    with (
        patch(VALIDATE, new=AsyncMock(return_value={"scope": "ingest"})),
        patch(VEHICLES, new=AsyncMock(return_value=[])),
        patch(METRICS, new=AsyncMock(return_value={})),
    ):
        result = await hass.config_entries.flow.async_configure(
            flows[0]["flow_id"], {CONF_API_KEY: "al_live_new"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == "al_live_new"
    assert entry.state is ConfigEntryState.LOADED


async def test_reconfigure_moves_the_unique_id_with_the_host(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)

    with (
        patch(VALIDATE, new=AsyncMock(return_value={})),
        patch(VEHICLES, new=AsyncMock(return_value=[])),
        patch(METRICS, new=AsyncMock(return_value={})),
    ):
        result = await entry.start_reconfigure_flow(hass)
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "https://New.Example/", CONF_API_KEY: "k"}
        )
        await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == "https://New.Example"
    assert entry.unique_id == "https://new.example"
