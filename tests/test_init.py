"""Setup / unload tests."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.swann.api import SwannAuthError, SwannConnectionError
from custom_components.swann.const import DOMAIN

from .conftest import SERIAL, FakeSwannClient


async def test_setup_and_unload(
    hass: HomeAssistant, fake_client: FakeSwannClient, config_entry: MockConfigEntry
):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED

    devreg = dr.async_get(hass)
    nvr = devreg.async_get_device_by_identifier((DOMAIN, SERIAL), config_entry.entry_id)
    assert nvr is not None
    assert nvr.model == "NVW-MR4K"
    ch1 = devreg.async_get_device_by_identifier(
        (DOMAIN, f"{SERIAL}_ch01"), config_entry.entry_id
    )
    assert ch1 is not None
    assert ch1.name == "Front Door"
    assert ch1.via_device_id == nvr.id

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED
    assert fake_client.closed == 1


async def test_setup_not_ready(
    hass: HomeAssistant, fake_client: FakeSwannClient, config_entry: MockConfigEntry
):
    fake_client.login_error = SwannConnectionError("down")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert fake_client.closed == 1


async def test_setup_auth_failed(
    hass: HomeAssistant, fake_client: FakeSwannClient, config_entry: MockConfigEntry
):
    fake_client.login_error = SwannAuthError("bad")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)
