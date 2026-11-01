"""Cloud switch: save settings on off, restore on on, survive a reload."""

from __future__ import annotations

from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.swann.api import CloudSettings

from .conftest import ORIGINAL_CLOUD, FakeSwannClient

CLOUD_SWITCH = "switch.swann_nvr_swann_cloud"


async def _switch(hass: HomeAssistant, service: str) -> None:
    await hass.services.async_call(
        "switch", service, {ATTR_ENTITY_ID: CLOUD_SWITCH}, blocking=True
    )


def _store_key(entry: MockConfigEntry) -> str:
    return f"swann.{entry.entry_id}.cloud"


async def test_cloud_switch_save_and_restore_across_reload(
    hass: HomeAssistant,
    hass_storage: dict,
    fake_client: FakeSwannClient,
    config_entry: MockConfigEntry,
):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(CLOUD_SWITCH).state == STATE_ON

    await _switch(hass, "turn_off")
    assert fake_client.cloud.features == {
        "p2p": False,
        "push": False,
        "cloud_upload": False,
    }
    assert fake_client.cloud.raw == ORIGINAL_CLOUD.raw
    assert hass.states.get(CLOUD_SWITCH).state == STATE_OFF
    assert hass_storage[_store_key(config_entry)]["data"]["saved"] == (
        ORIGINAL_CLOUD.as_dict()
    )

    # Turning off again must not overwrite the snapshot with the all-off state.
    await _switch(hass, "turn_off")
    assert hass_storage[_store_key(config_entry)]["data"]["saved"] == (
        ORIGINAL_CLOUD.as_dict()
    )

    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(CLOUD_SWITCH).state == STATE_OFF

    await _switch(hass, "turn_on")
    assert fake_client.cloud == ORIGINAL_CLOUD
    assert hass.states.get(CLOUD_SWITCH).state == STATE_ON
    assert _store_key(config_entry) not in hass_storage


async def test_cloud_switch_on_without_snapshot(
    hass: HomeAssistant, fake_client: FakeSwannClient, config_entry: MockConfigEntry
):
    fake_client.cloud = CloudSettings(features={"p2p": False})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(CLOUD_SWITCH).state == STATE_OFF

    with pytest.raises(HomeAssistantError):
        await _switch(hass, "turn_on")
    assert fake_client.cloud_writes == []
