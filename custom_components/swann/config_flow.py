"""Config flow for Swann NVR."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
import voluptuous as vol

from . import helpers
from .api import DeviceInfo, SwannAuthError, SwannConnectionError
from .const import (
    CONF_MANAGE_SERVERS,
    CONF_RTSP_TRANSPORT,
    CONF_STREAM_TYPE,
    CONF_USE_HTTPS,
    DEFAULT_MANAGE_SERVERS,
    DEFAULT_PORT,
    DEFAULT_RTSP_TRANSPORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STREAM_TYPE,
    DEFAULT_USE_HTTPS,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    RTSP_TRANSPORTS,
    STREAM_TYPES,
)
from .coordinator import SwannConfigEntry

_LOGGER = logging.getLogger(__name__)


def _user_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): (
                vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))
            ),
            vol.Required(
                CONF_USE_HTTPS, default=defaults.get(CONF_USE_HTTPS, DEFAULT_USE_HTTPS)
            ): bool,
            vol.Required(
                CONF_VERIFY_SSL,
                default=defaults.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
            ): bool,
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): str,
            vol.Required(CONF_PASSWORD): str,
        }
    )


class SwannConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Swann NVR."""

    VERSION = 1

    async def _async_validate(
        self, data: Mapping[str, Any]
    ) -> tuple[DeviceInfo | None, dict[str, str]]:
        """Try to log in and read device info."""
        client = helpers.create_client(self.hass, data)
        try:
            await client.async_login()
            info = await client.async_get_device_info()
        except SwannAuthError:
            return None, {"base": "invalid_auth"}
        except SwannConnectionError:
            return None, {"base": "cannot_connect"}
        except Exception:
            _LOGGER.exception("Unexpected error validating Swann NVR")
            return None, {"base": "unknown"}
        finally:
            await client.async_close()
        return info, {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            info, errors = await self._async_validate(user_input)
            if info is not None:
                await self.async_set_unique_id(info.serial)
                self._abort_if_unique_id_configured(
                    updates={
                        CONF_HOST: user_input[CONF_HOST],
                        CONF_PORT: user_input[CONF_PORT],
                    }
                )
                return self.async_create_entry(
                    title=info.name or info.model or user_input[CONF_HOST],
                    data=user_input,
                )
        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for new credentials."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            info, errors = await self._async_validate(data)
            if info is not None:
                await self.async_set_unique_id(info.serial)
                self._abort_if_unique_id_mismatch(reason="wrong_device")
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=entry.data[CONF_USERNAME]): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: SwannConfigEntry) -> SwannOptionsFlow:
        """Return the options flow."""
        return SwannOptionsFlow()


class SwannOptionsFlow(OptionsFlowWithReload):
    """Options: stream, RTSP transport, scan interval, DDNS/server management."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_STREAM_TYPE,
                    default=options.get(CONF_STREAM_TYPE, DEFAULT_STREAM_TYPE),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=STREAM_TYPES,
                        translation_key=CONF_STREAM_TYPE,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(
                    CONF_RTSP_TRANSPORT,
                    default=options.get(CONF_RTSP_TRANSPORT, DEFAULT_RTSP_TRANSPORT),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=RTSP_TRANSPORTS,
                        translation_key=CONF_RTSP_TRANSPORT,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                ),
                vol.Required(
                    CONF_MANAGE_SERVERS,
                    default=options.get(CONF_MANAGE_SERVERS, DEFAULT_MANAGE_SERVERS),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
