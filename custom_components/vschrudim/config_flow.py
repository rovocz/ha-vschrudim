"""Config flow pro VS Chrudim."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import (
    CONF_SCAN_INTERVAL_HOURS,
    DEFAULT_SCAN_INTERVAL_HOURS,
    DOMAIN,
    MAX_SCAN_INTERVAL_HOURS,
    MIN_SCAN_INTERVAL_HOURS,
)
from .vschrudim_client import VSChrudimAuthError, VSChrudimClient, VSChrudimError

_LOGGER = logging.getLogger(__name__)


def _credentials_schema(
    *, username_default: str | None = None,
) -> vol.Schema:
    """Build the credentials form schema."""
    username = vol.Required(CONF_USERNAME)
    if username_default is not None:
        username = vol.Required(CONF_USERNAME, default=username_default)

    return vol.Schema(
        {
            username: str,
            vol.Required(CONF_PASSWORD): str,
        }
    )


async def _validate(hass: HomeAssistant, data: dict[str, Any]) -> int:
    """Log in and return the number of active consumption places."""
    client = VSChrudimClient(data[CONF_USERNAME], data[CONF_PASSWORD])

    def _do_validate() -> int:
        client.login()
        places = client.list_consumption_places()
        return sum(1 for place in places if place["active"])

    try:
        active_count = await hass.async_add_executor_job(_do_validate)
    except VSChrudimAuthError as err:
        raise InvalidAuth from err
    except VSChrudimError as err:
        raise CannotConnect from err

    if active_count == 0:
        raise NoActivePlaces

    return active_count


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle setup, reauthentication and reconfiguration."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle initial setup."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                active_count = await _validate(self.hass, user_input)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except NoActivePlaces:
                errors["base"] = "no_active_places"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Neočekávaná chyba při ověřování")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(user_input[CONF_USERNAME].lower())
                self._abort_if_unique_id_configured()

                _LOGGER.debug("Nalezeno %s aktivních odběrných míst", active_count)
                return self.async_create_entry(
                    title=user_input[CONF_USERNAME],
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_credentials_schema(),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication after an authentication failure."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user to enter the current credentials again."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            username = entry.data[CONF_USERNAME]
            data = {
                CONF_USERNAME: username,
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            try:
                await _validate(self.hass, data)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except NoActivePlaces:
                errors["base"] = "no_active_places"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Neočekávaná chyba při reautentizaci")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_PASSWORD: data[CONF_PASSWORD]},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Allow the account credentials to be changed."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await _validate(self.hass, user_input)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except NoActivePlaces:
                errors["base"] = "no_active_places"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Neočekávaná chyba při změně účtu")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(user_input[CONF_USERNAME].lower())
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates=user_input,
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_credentials_schema(
                username_default=entry.data[CONF_USERNAME]
            ),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return OptionsFlow()


class OptionsFlow(config_entries.OptionsFlow):
    """Configure the polling interval."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle polling interval options."""
        if user_input is not None:
            self.hass.config_entries.async_update_entry(
                self.config_entry, options=user_input
            )
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_abort(reason="options_updated")

        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL_HOURS, DEFAULT_SCAN_INTERVAL_HOURS
        )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL_HOURS, default=current
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(
                        min=MIN_SCAN_INTERVAL_HOURS,
                        max=MAX_SCAN_INTERVAL_HOURS,
                    ),
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


class CannotConnect(HomeAssistantError):
    """Nepodařilo se spojit se serverem."""


class InvalidAuth(HomeAssistantError):
    """Server odmítl přihlašovací údaje."""


class NoActivePlaces(HomeAssistantError):
    """Účet nemá žádné aktivní odběrné místo."""
