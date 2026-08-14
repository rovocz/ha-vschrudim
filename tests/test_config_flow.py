"""Tests for the VS Chrudim config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vschrudim.config_flow import (
    CannotConnect,
    InvalidAuth,
    NoActivePlaces,
)
from custom_components.vschrudim.const import CONF_SCAN_INTERVAL_HOURS, DOMAIN

USERNAME = "test@example.com"
PASSWORD = "secret"


async def test_user_flow_success(hass):
    """A valid account creates one config entry."""
    with patch(
        "custom_components.vschrudim.config_flow._validate",
        new=AsyncMock(return_value=1),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert result["type"] is FlowResultType.FORM

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == USERNAME
    assert result["data"] == {
        CONF_USERNAME: USERNAME,
        CONF_PASSWORD: PASSWORD,
    }


async def test_user_flow_errors(hass):
    """Expected validation errors are translated to form errors."""
    for exception, error in (
        (InvalidAuth, "invalid_auth"),
        (CannotConnect, "cannot_connect"),
        (NoActivePlaces, "no_active_places"),
    ):
        with patch(
            "custom_components.vschrudim.config_flow._validate",
            new=AsyncMock(side_effect=exception),
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "user"}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
            )

        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": error}


async def test_user_flow_unknown_error(hass):
    """Unexpected validation errors are handled safely."""
    with patch(
        "custom_components.vschrudim.config_flow._validate",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


async def test_duplicate_account(hass):
    """The same account cannot be configured twice."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id=USERNAME,
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    ).add_to_hass(hass)

    with patch(
        "custom_components.vschrudim.config_flow._validate",
        new=AsyncMock(return_value=1),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow(hass):
    """The polling interval can be changed."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USERNAME,
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
        options={CONF_SCAN_INTERVAL_HOURS: 1},
    )
    entry.add_to_hass(hass)

    with patch.object(hass.config_entries, "async_reload", new=AsyncMock()):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_SCAN_INTERVAL_HOURS: 6},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "options_updated"
    assert entry.options[CONF_SCAN_INTERVAL_HOURS] == 6


async def test_reauth_success(hass):
    """Reauthentication updates the stored password."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USERNAME,
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: "old"},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vschrudim.config_flow._validate",
        new=AsyncMock(return_value=1),
    ), patch.object(
        hass.config_entries, "async_reload", new=AsyncMock()
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new"


async def test_reconfigure_success(hass):
    """Reconfiguration updates the existing config entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USERNAME,
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: "old"},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vschrudim.config_flow._validate",
        new=AsyncMock(return_value=1),
    ), patch.object(
        hass.config_entries, "async_reload", new=AsyncMock()
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: USERNAME, CONF_PASSWORD: "new"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PASSWORD] == "new"

async def test_reauth_errors(hass):
    """Reauthentication shows validation errors and remains on the form."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USERNAME,
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: "old"},
    )
    entry.add_to_hass(hass)

    for exception, error in (
        (InvalidAuth, "invalid_auth"),
        (CannotConnect, "cannot_connect"),
        (NoActivePlaces, "no_active_places"),
        (RuntimeError, "unknown"),
    ):
        with patch(
            "custom_components.vschrudim.config_flow._validate",
            new=AsyncMock(side_effect=exception),
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
                data=entry.data,
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_PASSWORD: "new"}
            )

        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": error}


async def test_reconfigure_errors(hass):
    """Reconfiguration shows validation errors and remains on the form."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USERNAME,
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: "old"},
    )
    entry.add_to_hass(hass)

    for exception, error in (
        (InvalidAuth, "invalid_auth"),
        (CannotConnect, "cannot_connect"),
        (NoActivePlaces, "no_active_places"),
        (RuntimeError, "unknown"),
    ):
        with patch(
            "custom_components.vschrudim.config_flow._validate",
            new=AsyncMock(side_effect=exception),
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={
                    "source": SOURCE_RECONFIGURE,
                    "entry_id": entry.entry_id,
                },
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_USERNAME: USERNAME, CONF_PASSWORD: "new"},
            )

        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": error}
