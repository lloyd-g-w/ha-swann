"""End-to-end tests: the integration with the real client against the mock NVR."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp.test_utils import TestServer
from homeassistant.components.camera.const import DATA_COMPONENT as CAMERA_DATA_KEY
from homeassistant.components.go2rtc import WebRTCProvider
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
from custom_components.swann.camera import go2rtc_transcode_source
from custom_components.swann.const import (
    CONF_HW_ACCEL,
    CONF_LIVE_VIEW_VIDEO,
    CONF_USE_HTTPS,
    LIVE_VIEW_AUTO,
    LIVE_VIEW_ORIGINAL,
    LIVE_VIEW_TRANSCODE,
)

from .conftest import USER_INPUT
from .mock_nvr import MockNVR, fixture_data

CLOUD_SWITCH = "switch.nvr_swann_cloud"


@pytest.fixture
async def mock_nvr(socket_enabled) -> AsyncGenerator[MockNVR]:
    """Run the mock NVR and point the integration's client at it."""
    mock = MockNVR(username=USER_INPUT["username"], password=USER_INPUT["password"])
    server = TestServer(mock.app, host="127.0.0.1")
    await server.start_server()
    rtsp = await asyncio.start_server(mock.serve_rtsp, "127.0.0.1", 0)
    rtsp_port = rtsp.sockets[0].getsockname()[1]
    real_create_client = helpers.create_client

    def _create(hass, data):
        # The real factory (HA's client session), pointed at the mock over HTTP.
        client = real_create_client(
            hass,
            {
                **data,
                CONF_HOST: "127.0.0.1",
                CONF_PORT: server.port,
                CONF_USE_HTTPS: False,
            },
        )
        client.rtsp_port = rtsp_port
        return client

    with patch("custom_components.swann.helpers.create_client", side_effect=_create):
        yield mock
    rtsp.close()
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


# --- waking battery cameras for live view -------------------------------------


@pytest.fixture
def fast_wake():
    """Short wake timings (real time) for the tests."""
    with (
        patch.object(coordinator_mod, "WAKE_TIMEOUT", 1.5),
        patch.object(coordinator_mod, "WAKE_POLL_INTERVAL", 0.05),
        patch.object(coordinator_mod, "WAKE_STREAM_HOLD", 0.5),
    ):
        yield


def _camera(hass: HomeAssistant, entity_id: str):
    return hass.data[CAMERA_DATA_KEY].get_entity(entity_id)


def _plays(mock_nvr: MockNVR) -> list[str]:
    return [url for method, url in mock_nvr.rtsp_requests if method == "PLAY"]


async def test_wake_before_live_view(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry, fast_wake
) -> None:
    mock_nvr.wake_delay = 0.2
    await _setup_real(hass, mock_nvr, entry)
    coordinator = entry.runtime_data.coordinator
    camera = _camera(hass, "camera.syd_driveway")
    # Setup (provider detection calls stream_source) wakes nothing.
    assert mock_nvr.rtsp_connections == 0
    assert coordinator.channel_asleep(1)

    assert await coordinator.async_wake_channel(1) is True
    assert coordinator.channel_awake(1)
    # Woken over RTSP on the sub stream, with Digest auth accepted.
    assert len(_plays(mock_nvr)) == 1
    assert "channel=01&subtype=1" in _plays(mock_nvr)[0]
    # Awake now: stream_source is immediate and nothing else is sent.
    url = await camera.stream_source()
    assert "channel=01&subtype=0" in url
    assert await coordinator.async_wake_channel(1) is True
    assert len(_plays(mock_nvr)) == 1
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_concurrent_viewers_share_one_wake(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry, fast_wake
) -> None:
    mock_nvr.wake_delay = 0.2
    await _setup_real(hass, mock_nvr, entry)
    camera = _camera(hass, "camera.syd_garden")
    results = await asyncio.gather(
        *(camera.async_wake_for_viewing() for _ in range(3)),
        entry.runtime_data.coordinator.async_wake_channel(3),
    )
    assert results[-1] is True
    assert mock_nvr.rtsp_connections == 1
    assert len(_plays(mock_nvr)) == 1
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_wake_timeout_still_opens_stream(
    hass: HomeAssistant,
    mock_nvr: MockNVR,
    entry: MockConfigEntry,
    fast_wake,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_nvr.wake_ok = False  # the camera never comes up
    await _setup_real(hass, mock_nvr, entry)
    coordinator = entry.runtime_data.coordinator
    camera = _camera(hass, "camera.sydney_door")
    caplog.set_level("DEBUG", logger="custom_components.swann")
    with patch(
        "homeassistant.components.camera.Camera.async_create_stream",
        return_value=None,
    ) as create:
        await camera.async_create_stream()
    create.assert_awaited_once()
    assert "did not wake in time" in caplog.text
    assert coordinator.channel_asleep(4)
    # The wake stream was abandoned (no lingering RTSP session).
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_webrtc_offer_wakes_first(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry, fast_wake
) -> None:
    mock_nvr.wake_delay = 0.1
    await _setup_real(hass, mock_nvr, entry)
    coordinator = entry.runtime_data.coordinator
    camera = _camera(hass, "camera.syd_side_path")
    seen = []

    async def provider_offer(self, offer, session_id, send_message):
        seen.append(coordinator.channel_awake(2))

    with patch(
        "homeassistant.components.camera.Camera.async_handle_async_webrtc_offer",
        provider_offer,
    ):
        await camera.async_handle_async_webrtc_offer("sdp", "s1", lambda m: None)
    assert seen == [True]
    # Still treated as a provider-backed (not native) WebRTC camera.
    assert camera._supports_native_async_webrtc is False
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.event_loop_enabled
async def test_wake_via_event_check(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry, fast_wake
) -> None:
    """The Online status arrives through the Event/Check loop."""
    mock_nvr.wake_delay = 0.1
    with (
        patch.object(coordinator_mod, "EVENT_MIN_INTERVAL", 0.01),
        patch.object(coordinator_mod, "WAKE_POLL_INTERVAL", 10.0),
    ):
        await _setup_real(hass, mock_nvr, entry)
        coordinator = entry.runtime_data.coordinator
        # The ChannelInfo poll never runs (10 s); only Event/Check says Online.
        mock_nvr.set_channel_status(1, "Sleep")
        assert await coordinator.async_wake_channel(1) is True
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_snapshot_never_wakes(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry
) -> None:
    await _setup_real(hass, mock_nvr, entry)
    camera = _camera(hass, "camera.syd_driveway")
    with patch(
        "custom_components.swann.camera.async_get_image", return_value=b"jpeg"
    ) as grab:
        assert await camera.async_camera_image() is None
    grab.assert_not_called()
    assert mock_nvr.rtsp_connections == 0


# --- stale entity cleanup -------------------------------------------------------


async def _serial_after_setup(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Set up once to learn the device serial, then unload."""
    await _setup(hass, entry)
    serial = entry.runtime_data.coordinator.device.serial
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    return serial


async def test_stale_entities_removed(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry
) -> None:
    mock_nvr.use_real_mr4k()
    serial = await _serial_after_setup(hass, entry)
    registry = er.async_get(hass)
    # 0.1.3's motion switch on a sleeping camera, and an empty slot's sensor.
    old_switch = registry.async_get_or_create(
        "switch",
        "swann",
        f"{serial}_ch01_motion_detection",
        config_entry=entry,
        suggested_object_id="syd_driveway_motion_detection_old",
    )
    unpaired = registry.async_get_or_create(
        "binary_sensor",
        "swann",
        f"{serial}_ch05_motion",
        config_entry=entry,
        suggested_object_id="channel_5_motion",
    )
    mock_nvr.events = [fixture_data("real_mr4k_event_check_initial")]
    await _setup(hass, entry)
    assert registry.async_get(old_switch.entity_id) is None
    assert registry.async_get(unpaired.entity_id) is None
    assert hass.states.get("select.syd_driveway_motion_detection_mode") is not None
    assert hass.states.get("camera.syd_driveway") is not None


async def test_offline_channel_entities_kept(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry
) -> None:
    mock_nvr.use_real_mr4k()
    serial = await _serial_after_setup(hass, entry)
    mock_nvr.set_channel_status(2, "Offline")
    registry = er.async_get(hass)
    # An offline camera may report no abilities: none of its entities go.
    kept = registry.async_get_or_create(
        "binary_sensor",
        "swann",
        f"{serial}_ch02_retired_kind",
        config_entry=entry,
    )
    mock_nvr.events = [fixture_data("real_mr4k_event_check_initial")]
    await _setup(hass, entry)
    assert registry.async_get(kept.entity_id) is not None


async def test_stale_cleanup_skipped_when_nvr_unreachable(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry
) -> None:
    registry = er.async_get(hass)
    kept = registry.async_get_or_create(
        "switch",
        "swann",
        f"{entry.unique_id}_ch01_motion_detection",
        config_entry=entry,
    )
    mock_nvr.next_error["Login/DeviceInfo/Get"] = ("not_found", 404)
    with patch.object(
        SwannClient, "async_login", side_effect=SwannConnectionError("down")
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert registry.async_get(kept.entity_id) is not None


# --- live view transcoding via HA's go2rtc provider (0.1.6) ---


class FakeStreams:
    """go2rtc REST streams API: records what HA registers."""

    def __init__(self) -> None:
        self.added: list[tuple[str, list[str]]] = []

    async def list(self) -> dict:
        return {
            name: SimpleNamespace(producers=[SimpleNamespace(url=u) for u in urls])
            for name, urls in self.added
        }

    async def add(self, name: str, sources: list[str]) -> None:
        self.added.append((name, sources))


class FakeRestClient:
    def __init__(self, schemes: set[str]) -> None:
        self.streams = FakeStreams()
        self._schemes = schemes
        self.schemes = SimpleNamespace(list=self._list_schemes)
        self.preload = SimpleNamespace(list=self._list_preload)

    async def _list_schemes(self) -> set[str]:
        return self._schemes

    async def _list_preload(self) -> dict:
        return {}


async def _attach_go2rtc(
    hass: HomeAssistant, camera, schemes: set[str] | None = None
) -> FakeRestClient:
    """Give the camera HA's real go2rtc provider, backed by a fake REST client."""
    rest = FakeRestClient(schemes if schemes is not None else {"rtsp", "ffmpeg"})
    provider = WebRTCProvider(hass, "http://go2rtc", None, rest)
    await provider.initialize()
    camera._webrtc_provider = provider
    return rest


async def _offer(camera, rest: FakeRestClient, events: list[str]) -> str:
    """Run a WebRTC offer up to go2rtc registering the stream; return its source."""
    provider = camera.webrtc_provider

    async def offer(self, cam, sdp, session_id, send_message):
        events.append("offer")
        await self._update_stream_source(cam)  # what go2rtc does first

    with patch.object(WebRTCProvider, "async_handle_async_webrtc_offer", offer):
        await camera.async_handle_async_webrtc_offer("sdp", "s1", lambda m: None)
    assert provider is camera.webrtc_provider
    return rest.streams.added[-1][1][0]


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, "transcode"),
        ({CONF_LIVE_VIEW_VIDEO: LIVE_VIEW_AUTO}, "transcode"),
        ({CONF_LIVE_VIEW_VIDEO: LIVE_VIEW_TRANSCODE}, "transcode"),
        (
            {CONF_LIVE_VIEW_VIDEO: LIVE_VIEW_TRANSCODE, CONF_HW_ACCEL: True},
            "hardware",
        ),
        ({CONF_LIVE_VIEW_VIDEO: LIVE_VIEW_ORIGINAL}, "original"),
        ({CONF_LIVE_VIEW_VIDEO: LIVE_VIEW_ORIGINAL, CONF_HW_ACCEL: True}, "original"),
    ],
)
async def test_go2rtc_source_per_option(
    hass: HomeAssistant,
    mock_nvr: MockNVR,
    entry: MockConfigEntry,
    fast_wake,
    options: dict,
    expected: str,
) -> None:
    hass.config_entries.async_update_entry(entry, options=options)
    mock_nvr.wake_delay = 0.1
    await _setup_real(hass, mock_nvr, entry)
    coordinator = entry.runtime_data.coordinator
    camera = _camera(hass, "camera.syd_driveway")
    rest = await _attach_go2rtc(hass, camera)
    url = coordinator.client.rtsp_url(1, "main")

    events: list[str] = []
    orig_wake = camera.async_wake_for_viewing

    async def wake() -> None:
        await orig_wake()
        events.append(f"awake={coordinator.channel_awake(1)}")

    with patch.object(camera, "async_wake_for_viewing", wake):
        source = await _offer(camera, rest, events)

    assert events == ["awake=True", "offer"]  # woken before go2rtc connects
    assert len(_plays(mock_nvr)) == 1
    assert (
        source
        == {
            "transcode": f"ffmpeg:{url}#video=h264#audio=opus",
            "hardware": f"ffmpeg:{url}#video=h264#audio=opus#hardware",
            "original": url,
        }[expected]
    )
    # Outside the offer, stream_source() is the plain RTSP URL again
    # (HA stream worker / HLS, snapshots, provider detection).
    assert await camera.stream_source() == url
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_stream_source_plain_during_concurrent_offer(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry, fast_wake
) -> None:
    """Other callers of stream_source() never see the ffmpeg source."""
    await _setup_real(hass, mock_nvr, entry)
    camera = _camera(hass, "camera.syd_driveway")
    rest = await _attach_go2rtc(hass, camera)
    url = entry.runtime_data.coordinator.client.rtsp_url(1, "main")
    seen_elsewhere: list[str | None] = []

    async def offer(self, cam, sdp, session_id, send_message):
        # A concurrent task (e.g. HLS) asking for the source mid-offer.
        task = hass.async_create_task(cam.stream_source())
        seen_elsewhere.append(await task)
        await self._update_stream_source(cam)

    with patch.object(WebRTCProvider, "async_handle_async_webrtc_offer", offer):
        await camera.async_handle_async_webrtc_offer("sdp", "s1", lambda m: None)
    assert seen_elsewhere == [url]
    assert rest.streams.added[-1][1][0].startswith("ffmpeg:")
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_go2rtc_without_ffmpeg_falls_back(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry, fast_wake
) -> None:
    await _setup_real(hass, mock_nvr, entry)
    camera = _camera(hass, "camera.syd_driveway")
    rest = await _attach_go2rtc(hass, camera, schemes={"rtsp"})
    url = entry.runtime_data.coordinator.client.rtsp_url(1, "main")
    assert await _offer(camera, rest, []) == url
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_hls_stream_uses_plain_rtsp(
    hass: HomeAssistant, mock_nvr: MockNVR, entry: MockConfigEntry, fast_wake
) -> None:
    """HA's stream worker (HLS fallback) is handed the RTSP URL, not ffmpeg:."""
    hass.config_entries.async_update_entry(
        entry, options={CONF_LIVE_VIEW_VIDEO: LIVE_VIEW_TRANSCODE}
    )
    await _setup_real(hass, mock_nvr, entry)
    camera = _camera(hass, "camera.syd_driveway")
    await _attach_go2rtc(hass, camera)
    url = entry.runtime_data.coordinator.client.rtsp_url(1, "main")
    sources: list[str | None] = []

    async def create_stream(self):
        sources.append(await self.stream_source())

    with patch(
        "homeassistant.components.camera.Camera.async_create_stream", create_stream
    ):
        await camera.async_create_stream()
    assert sources == [url]
    assert len(_plays(mock_nvr)) == 1  # still woken first
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def test_transcode_source_format() -> None:
    url = "rtsp://u:p@nvr:554/rtsp/streaming?channel=01&subtype=1"
    assert go2rtc_transcode_source(url) == f"ffmpeg:{url}#video=h264#audio=opus"
    assert go2rtc_transcode_source(url, hardware=True).endswith("#hardware")
