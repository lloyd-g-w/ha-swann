"""End-to-end tests: the integration with the real client against the mock NVR."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
import copy
import json
from unittest.mock import patch

from aiohttp.test_utils import TestServer
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_HOST,
    CONF_PORT,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.swann import (
    coordinator as coordinator_mod,
    diagnostics,
    helpers,
)
from custom_components.swann.api import SwannClient, SwannConnectionError
from custom_components.swann.const import CONF_USE_HTTPS

from .conftest import USER_INPUT
from .mock_nvr import MockNVR, fixture_data

CLOUD_SWITCH = "switch.nvr_swann_cloud"


@pytest.fixture
async def mock_nvr(socket_enabled) -> AsyncGenerator[MockNVR]:
    """Run the mock NVR and point the integration's client at it."""
    mock = MockNVR(username=USER_INPUT["username"], password=USER_INPUT["password"])
    server = TestServer(mock.app, host="127.0.0.1")
    await server.start_server()
    real_create_client = helpers.create_client

    def _create(hass, data):
        # The real factory (HA's client session), pointed at the mock over HTTP.
        return real_create_client(
            hass,
            {
                **data,
                CONF_HOST: "127.0.0.1",
                CONF_PORT: server.port,
                CONF_USE_HTTPS: False,
            },
        )

    with patch("custom_components.swann.helpers.create_client", side_effect=_create):
        yield mock
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
    hass: HomeAssistant,
    mock_nvr: MockNVR,
    entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("camera.driveway") is not None
    assert hass.states.get("switch.driveway_motion_detection").state == STATE_ON
    assert hass.states.get("switch.garage_motion_detection").state == "unavailable"
    assert hass.states.get(CLOUD_SWITCH).state == STATE_ON

    await hass.config_entries.async_unload(entry.entry_id)
    assert mock_nvr.logouts == 1
    assert "closes the Home Assistant aiohttp session" not in caplog.text


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


async def test_setup_survives_cloud_page_hanging_up(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry
) -> None:
    # Seen on an NVW-MR4K: StorageConfig/Cloud/Range drops the connection.
    mock_nvr.drop["StorageConfig/Cloud/Range"] = -1
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("camera.driveway") is not None
    assert hass.states.get(CLOUD_SWITCH).state == STATE_ON


async def test_setup_survives_every_cloud_page_hanging_up(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry
) -> None:
    for page in ("NetworkConfig/P2P", "StorageConfig/Cloud", "Maintenance/AutoUpgrade"):
        mock_nvr.drop[f"{page}/Range"] = -1
    for path in [p for p, _ in mock_nvr.pages if p.startswith("AlarmConfig/")]:
        mock_nvr.drop[f"{path}/Range"] = -1
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("binary_sensor.driveway_motion") is not None


async def test_setup_survives_cloud_settings_failing(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry
) -> None:
    with patch.object(
        SwannClient,
        "async_get_cloud_settings",
        side_effect=SwannConnectionError("Error calling /API/X/Range: boom"),
    ):
        await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("camera.driveway") is not None
    assert hass.states.get(CLOUD_SWITCH).state == "unavailable"


async def test_channel_devices_linked_without_via_device(
    hass: HomeAssistant,
    mock_nvr: MockNVR,
    entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Newer HA rejects via_device on cameras (entities failed to add).
    await _setup(hass, entry)
    assert "via_device" not in caplog.text
    registry = dr.async_get(hass)
    nvr = registry.async_get_device_by_identifier(
        ("swann", "N8X1R2A3B4C5D6E7"), entry.entry_id
    )
    assert nvr is not None
    channels = [
        d
        for d in dr.async_entries_for_config_entry(registry, entry.entry_id)
        if d.id != nvr.id
    ]
    assert len(channels) == 4
    assert all(d.via_device_id == nvr.id for d in channels)
    assert {d.name for d in channels} >= {"Driveway", "Garage"}
    ent = er.async_get(hass).async_get("camera.driveway")
    assert registry.async_get(ent.device_id).via_device_id == nvr.id


@pytest.mark.event_loop_enabled
async def test_heartbeat_loop_keeps_session(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry
) -> None:
    with (
        patch.object(coordinator_mod, "HEARTBEAT_INTERVAL", 0.01),
        patch.object(coordinator_mod, "EVENT_MIN_INTERVAL", 0.05),
    ):
        await _setup(hass, entry)
        for _ in range(200):
            if mock_nvr.heartbeats >= 3:
                break
            await asyncio.sleep(0.01)
        assert mock_nvr.heartbeats >= 3
        assert mock_nvr.logins == 1
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    count = mock_nvr.heartbeats
    await asyncio.sleep(0.05)
    assert mock_nvr.heartbeats == count  # stopped on unload


async def test_heartbeat_recovers_after_no_heartbeat(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry
) -> None:
    now = [1000.0]
    mock_nvr.clock = lambda: now[0]
    mock_nvr.heartbeat_timeout = 15
    await _setup(hass, entry)
    coordinator = entry.runtime_data.coordinator
    now[0] += 30  # session dropped: no heartbeat in time
    await coordinator.async_send_heartbeat()
    assert mock_nvr.logins == 2
    assert mock_nvr.heartbeats == 1
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert hass.states.get("camera.driveway").state != "unavailable"


async def test_diagnostics(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry
) -> None:
    await _setup(hass, entry)
    mock_nvr.drop["SystemInfo/Base/Get"] = -1  # one call failing
    diag = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(diag)
    for secret in (
        "N8X1R2A3B4C5D6E7",
        "00:23:63:AA:BB:CC",
        "192.168.86.152",
        "SWANN0123456789",
        USER_INPUT["password"],
        "127.0.0.1",
        USER_INPUT["host"],
    ):
        assert secret not in text, secret
    assert diag["version"]
    assert diag["coordinator"]["last_update_success"] is True
    assert diag["coordinator"]["event_loop_running"] is False
    raw = diag["raw"]
    assert raw["system_info"]["ok"] is False
    assert "SystemInfo/Base/Get" in raw["system_info"]["error"]
    assert raw["device_info"]["data"]["model"] == "NVW-MR4K"
    assert raw["channel_info"]["data"]["channel_param"]["items"][0]["channel_name"]
    assert raw["motion_config"]["ok"] is True
    assert raw["event_check"]["ok"] is True
    assert raw["cloud_range"]["NetworkConfig/P2P"] == "supported"
    assert "NetworkConfig/DDNS" not in raw["cloud_range"]  # option off
    assert diag["channels"][0]["abilities"] == [
        "md",
        "pd",
        "vd",
        "Floodlight",
        "AudioAlarm",
        "Talk",
    ]
    assert diag["data"]["password"] == "**REDACTED**"


async def test_diagnostics_when_not_loaded(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry
) -> None:
    diag = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    assert diag["raw"]["device_info"]["ok"] is True
    assert "coordinator" not in diag
    assert mock_nvr.logouts == 1  # temporary client closed


# --- the user's real NVW-MR4K (battery Wi-Fi cameras) ------------------------


def _real_event(**changes: dict) -> dict:
    """The real idle Event/Check, with per-channel changes (CHn -> fields)."""
    data = copy.deepcopy(fixture_data("real_mr4k_event_check_initial"))
    data["sequence"] += 1
    for alarm in data["alarm_list"][0]["channel_alarm"]:
        alarm.update(changes.get(alarm["channel"], {}))
    return data


async def _setup_real(hass: HomeAssistant, mock_nvr: MockNVR, entry) -> None:
    mock_nvr.use_real_mr4k()
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED


async def test_real_mr4k_entities(
    hass: HomeAssistant,
    mock_nvr: MockNVR,
    entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _setup_real(hass, mock_nvr, entry)
    states = {s.entity_id: s.state for s in hass.states.async_all()}
    # Sleeping battery cameras are available; empty slots 5-8 get nothing.
    assert states["camera.syd_driveway"] != "unavailable"
    assert states["binary_sensor.sydney_door_motion"] == STATE_OFF
    assert states["binary_sensor.syd_driveway_awake"] == STATE_OFF
    assert not [e for e in states if "channel_5" in e or "channel_8" in e]
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert sorted(d.name for d in devices) == [
        "Syd Driveway",
        "Syd Garden",
        "Syd Side Path",
        "Sydney Door",
        "Wetherill St",
    ]
    # Floodlight only where the camera has one; siren where it has AudioAlarm.
    assert states["binary_sensor.syd_driveway_floodlight"] == STATE_OFF
    assert "binary_sensor.sydney_door_floodlight" not in states
    assert states["binary_sensor.sydney_door_siren"] == STATE_OFF
    # No boolean motion switch on this firmware: a mode select instead.
    assert "switch.syd_driveway_motion_detection" not in states
    select = hass.states.get("select.syd_driveway_motion_detection_mode")
    assert select.state == "PIR_and_Person_or_Vehicle"
    assert select.attributes["options"] == [
        "PIR_and_Motion",
        "PIR_and_Person",
        "PIR_and_Person_or_Vehicle",
    ]
    assert states["sensor.wetherill_st_cloud_connection"] == "Connected"
    assert "via_device" not in caplog.text


async def test_real_mr4k_motion_mode_select(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry
) -> None:
    await _setup_real(hass, mock_nvr, entry)
    before = copy.deepcopy(mock_nvr.page("AlarmConfig/Motion", "ChannelConfig"))
    await hass.services.async_call(
        "select",
        "select_option",
        {
            ATTR_ENTITY_ID: "select.syd_garden_motion_detection_mode",
            "option": "PIR_and_Motion",
        },
        blocking=True,
    )
    path, body = mock_nvr.sets[-1]
    assert path == "AlarmConfig/Motion/Set"
    assert body["channel_info"]["CH3"]["multiple_switch"] == "PIR_and_Motion"
    # Only that key changed (region grid and other channels untouched).
    body["channel_info"]["CH3"]["multiple_switch"] = "PIR_and_Person_or_Vehicle"
    assert body["channel_info"] == before["channel_info"]
    state = hass.states.get("select.syd_garden_motion_detection_mode")
    assert state.state == "PIR_and_Motion"


async def test_real_mr4k_motion_mode_read_only_without_range(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry
) -> None:
    mock_nvr.use_real_mr4k()
    mock_nvr.ranges.clear()  # Range lists no modes
    await _setup(hass, entry)
    state = hass.states.get("select.syd_garden_motion_detection_mode")
    assert state.attributes["options"] == ["PIR_and_Person_or_Vehicle"]
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "select",
            "select_option",
            {
                ATTR_ENTITY_ID: "select.syd_garden_motion_detection_mode",
                "option": "Off",
            },
            blocking=True,
        )
    assert not mock_nvr.sets


async def test_real_mr4k_live_events(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry
) -> None:
    await _setup_real(hass, mock_nvr, entry)
    coordinator = entry.runtime_data.coordinator
    online = {"camera_connect_status": {"connect_status": "Online"}}
    mock_nvr.events = [
        _real_event(
            CH1={"motion_alarm": True, **online},
            CH2={
                "Floodlight_AudioAlarm": {
                    "floodlight_switch": True,
                    "audioAlarm_switch": True,
                }
            },
            CH4={"camera_connect_status": {"connect_status": "Offline"}},
        )
    ]
    await coordinator.async_poll_events()
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.syd_driveway_motion").state == STATE_ON
    assert hass.states.get("binary_sensor.syd_driveway_awake").state == STATE_ON
    assert hass.states.get("binary_sensor.syd_side_path_floodlight").state == STATE_ON
    assert hass.states.get("binary_sensor.syd_side_path_siren").state == STATE_ON
    assert hass.states.get("camera.sydney_door").state == "unavailable"

    mock_nvr.events = [_real_event()]
    await coordinator.async_poll_events()
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.syd_driveway_motion").state == STATE_OFF
    assert hass.states.get("binary_sensor.syd_driveway_awake").state == STATE_OFF
    assert hass.states.get("camera.sydney_door").state != "unavailable"


async def test_real_mr4k_removes_unpaired_devices(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry
) -> None:
    registry = dr.async_get(hass)
    stale = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("swann", "stale-channel-5")},
        name="Channel 5",
    )
    await _setup_real(hass, mock_nvr, entry)
    assert registry.async_get(stale.id) is None


async def test_real_mr4k_diagnostics_recent_events(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry
) -> None:
    await _setup_real(hass, mock_nvr, entry)
    coordinator = entry.runtime_data.coordinator
    mock_nvr.events = [
        _real_event(CH1={"motion_alarm": True}),
        _real_event(),
    ]
    await coordinator.async_poll_events()
    await coordinator.async_poll_events()
    diag = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    recent = diag["recent_events"]
    assert [e["entry"]["motion_alarm"] for e in recent] == [True, False]
    assert recent[0]["entry"]["channel"] == "CH1"
    assert "Floodlight_AudioAlarm" in recent[0]["entry"]
    assert "floodlight_value_range" not in json.dumps(recent)
    assert diag["raw"]["motion_range"]["ok"]
    assert diag["motion_modes"]["1"][0] == "PIR_and_Motion"
    assert diag["cloud_link"] == "Connected"
    for name in ("storage_cloud", "auto_upgrade", "netbase", "intelligent_pd"):
        assert name in diag["raw"]
