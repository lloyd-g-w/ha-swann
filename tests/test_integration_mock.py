"""End-to-end tests: the integration with the real client against the mock NVR."""

from __future__ import annotations

from collections.abc import AsyncGenerator
import copy
from unittest.mock import patch

import aiohttp
from aiohttp.test_utils import TestServer
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.swann import coordinator as coordinator_mod
from custom_components.swann.api import SwannClient

from .conftest import USER_INPUT
from .mock_nvr import MockNVR, fixture_data

CLOUD_SWITCH = "switch.nvr_swann_cloud"


@pytest.fixture
async def mock_nvr(socket_enabled) -> AsyncGenerator[MockNVR]:
    """Run the mock NVR and point the integration's client at it."""
    mock = MockNVR(username=USER_INPUT["username"], password=USER_INPUT["password"])
    server = TestServer(mock.app, host="127.0.0.1")
    await server.start_server()
    sessions: list[aiohttp.ClientSession] = []

    def _create(hass, data):
        # Like helpers.create_client: one session per client (closed on unload).
        sessions.append(
            aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        )
        return SwannClient(
            sessions[-1],
            "127.0.0.1",
            server.port,
            data["username"],
            data["password"],
            use_https=False,
        )

    with patch("custom_components.swann.helpers.create_client", side_effect=_create):
        yield mock
    for http in sessions:
        await http.close()
    await server.close()


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """Config entry matching the mock NVR's serial."""
    entry = MockConfigEntry(
        domain="swann", unique_id="N8X1R2A3B4C5D6E7", data=dict(USER_INPUT)
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_entities(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry
) -> None:
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("camera.driveway") is not None
    assert hass.states.get("switch.driveway_motion_detection").state == STATE_ON
    assert hass.states.get("switch.garage_motion_detection").state == "unavailable"
    assert hass.states.get(CLOUD_SWITCH).state == STATE_ON

    await hass.config_entries.async_unload(entry.entry_id)
    assert mock_nvr.logouts == 1


async def test_events_reach_binary_sensors(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry
) -> None:
    mock_nvr.events = [fixture_data("event_check_initial")]
    await _setup(hass, entry)
    coordinator = entry.runtime_data.coordinator
    assert hass.states.get("binary_sensor.driveway_motion").state == STATE_OFF

    mock_nvr.events = [
        fixture_data("event_check_ai_person"),
        fixture_data("event_check_alarm_end"),
    ]
    await coordinator.async_poll_events()
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.driveway_motion").state == STATE_ON
    assert hass.states.get("binary_sensor.driveway_person").state == STATE_ON
    assert hass.states.get("binary_sensor.front_door_vehicle").state == STATE_ON

    await coordinator.async_poll_events()
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.driveway_person").state == STATE_OFF
    assert hass.states.get("binary_sensor.front_door_vehicle").state == STATE_OFF


@pytest.mark.event_loop_enabled
async def test_background_event_loop(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry
) -> None:
    with patch.object(coordinator_mod, "EVENT_MIN_INTERVAL", 0.01):
        await _setup(hass, entry)
        mock_nvr.events = [fixture_data("event_check_motion")]
        for _ in range(200):
            if hass.states.get("binary_sensor.driveway_motion").state == STATE_ON:
                break
            await hass.async_block_till_done()
            await __import__("asyncio").sleep(0.01)
        assert hass.states.get("binary_sensor.driveway_motion").state == STATE_ON
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_motion_switch(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry
) -> None:
    await _setup(hass, entry)
    await hass.services.async_call(
        "switch",
        "turn_off",
        {ATTR_ENTITY_ID: "switch.driveway_motion_detection"},
        blocking=True,
    )
    cfg = mock_nvr.page("AlarmConfig/Motion", "ChannelConfig")["channel_info"]
    assert cfg["CH1"]["switch"] is False
    assert hass.states.get("switch.driveway_motion_detection").state == STATE_OFF


async def test_cloud_switch_round_trip(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry
) -> None:
    original = copy.deepcopy(mock_nvr.pages)
    await _setup(hass, entry)

    await hass.services.async_call(
        "switch", "turn_off", {ATTR_ENTITY_ID: CLOUD_SWITCH}, blocking=True
    )
    assert hass.states.get(CLOUD_SWITCH).state == STATE_OFF
    assert mock_nvr.page("NetworkConfig/P2P")["p2p_switch"] is False
    # DDNS and upload servers are opt-in: untouched by default.
    assert (
        mock_nvr.page("StorageConfig/Server")
        == original[("StorageConfig/Server", None)]
    )
    assert mock_nvr.page("NetworkConfig/DDNS") == original[("NetworkConfig/DDNS", None)]
    assert not any(
        p.startswith(("StorageConfig/Server", "NetworkConfig/DDNS"))
        for p, _ in mock_nvr.requests
    )

    # Survives a reload (snapshot is in HA storage).
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(CLOUD_SWITCH).state == STATE_OFF

    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: CLOUD_SWITCH}, blocking=True
    )
    assert hass.states.get(CLOUD_SWITCH).state == STATE_ON
    assert mock_nvr.pages == original


async def test_cloud_switch_manages_servers_when_enabled(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry
) -> None:
    original = copy.deepcopy(mock_nvr.pages)
    hass.config_entries.async_update_entry(entry, options={"manage_servers": True})
    await _setup(hass, entry)

    await hass.services.async_call(
        "switch", "turn_off", {ATTR_ENTITY_ID: CLOUD_SWITCH}, blocking=True
    )
    assert mock_nvr.page("StorageConfig/Server")["swann_cloud_info"]["enable"] is False
    assert mock_nvr.page("NetworkConfig/DDNS")["ddns_enable"] is False

    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: CLOUD_SWITCH}, blocking=True
    )
    assert hass.states.get(CLOUD_SWITCH).state == STATE_ON
    assert mock_nvr.pages == original


async def test_login_at_other_makes_entities_unavailable(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry
) -> None:
    await _setup(hass, entry)
    mock_nvr.kick_session()
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    assert not coordinator.last_update_success
    assert "dedicated NVR user" in str(coordinator.last_exception)
    assert hass.states.get("camera.driveway").state == "unavailable"
