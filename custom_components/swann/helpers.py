"""Helpers shared by setup and the config flow."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import aiohttp
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import SwannClient
from .const import CONF_USE_HTTPS, DEFAULT_USE_HTTPS, DEFAULT_VERIFY_SSL


def create_client(hass: HomeAssistant, data: Mapping[str, Any]) -> SwannClient:
    """Create a client with its own session (the NVR uses session cookies)."""
    verify_ssl = data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
    session = async_create_clientsession(
        hass,
        verify_ssl=verify_ssl,
        cookie_jar=aiohttp.CookieJar(unsafe=True),
    )
    return SwannClient(
        session,
        data[CONF_HOST],
        data[CONF_PORT],
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        use_https=data.get(CONF_USE_HTTPS, DEFAULT_USE_HTTPS),
        verify_ssl=verify_ssl,
    )
