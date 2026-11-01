"""Tests for SwannClient against the mock NVR."""

from __future__ import annotations

from collections.abc import AsyncGenerator
import copy
from typing import Any

import aiohttp
from aiohttp.test_utils import TestServer
import pytest

from custom_components.swann.api import (
    EVENT_HOLD_SECONDS,
    ChannelEvents,
    CloudSettings,
    SwannApiError,
    SwannAuthError,
    SwannClient,
    SwannConnectionError,
    SwannSessionTakenError,
)

from .mock_nvr import MockNVR, fixture_data

ALARM_PAGE = "AlarmConfig/Motion"


class FakeClock:
    """Controllable monotonic clock."""

    def __init__(self) -> None:
        """Start at an arbitrary time."""
        self.now = 1000.0

    def __call__(self) -> float:
        """Return the current time."""
        return self.now


@pytest.fixture
async def nvr(socket_enabled) -> AsyncGenerator[tuple[MockNVR, TestServer]]:
    """Run the mock NVR on localhost."""
    mock = MockNVR()
    server = TestServer(mock.app, host="127.0.0.1")
    await server.start_server()
    yield mock, server
    await server.close()


@pytest.fixture
async def session() -> AsyncGenerator[aiohttp.ClientSession]:
    """HTTP session for the client."""
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
        yield http


def make_client(
    session: aiohttp.ClientSession,
    server: TestServer,
    password: str = "secret",
    clock: FakeClock | None = None,
) -> SwannClient:
    """Create a client pointed at the mock server (plain HTTP)."""
    client = SwannClient(
        session, "127.0.0.1", server.port, "homeassistant", password, use_https=False
    )
    if clock is not None:
        client._clock = clock
    return client


@pytest.fixture
def mock(nvr) -> MockNVR:
    """The mock NVR state."""
    return nvr[0]


@pytest.fixture
def clock() -> FakeClock:
    """A controllable clock."""
    return FakeClock()


@pytest.fixture
def client(nvr, session, clock) -> SwannClient:
    """A client for the mock NVR."""
    return make_client(session, nvr[1], clock=clock)


# --- login / session -----------------------------------------------------


async def test_login_success(client: SwannClient, mock: MockNVR) -> None:
    await client.async_login()
    assert client.logged_in
    assert mock.logins == 1
    await client.async_heartbeat()
    assert mock.requests[-1] == ("Login/Heartbeat", {})


async def test_bad_password(nvr, session, clock) -> None:
    mock, server = nvr
    client = make_client(session, server, password="wrong", clock=clock)
    with pytest.raises(SwannAuthError):
        await client.async_login()
    attempts = len(mock.requests)
    # Lockout protection: no new login attempt during the back-off.
    with pytest.raises(SwannAuthError):
        await client.async_login()
    assert len(mock.requests) == attempts


async def test_login_block_honours_remaining_time(
    client: SwannClient, mock: MockNVR, clock: FakeClock
) -> None:
    mock.login_error = ("login_failed_or_block", 400, {"block_remain_time": 900})
    with pytest.raises(SwannAuthError):
        await client.async_login()
    attempts = len(mock.requests)
    mock.login_error = None
    clock.now += 600  # beyond the default back-off, within the NVR's block time
    with pytest.raises(SwannAuthError):
        await client.async_get_channels()
    assert len(mock.requests) == attempts
    clock.now += 400
    await client.async_login()
    assert client.logged_in


async def test_unreachable(socket_enabled, session) -> None:
    server = TestServer(MockNVR().app, host="127.0.0.1")
    await server.start_server()
    port = server.port
    await server.close()
    client = SwannClient(
        session, "127.0.0.1", port, "homeassistant", "secret", use_https=False
    )
    with pytest.raises(SwannConnectionError):
        await client.async_login()
    with pytest.raises(SwannConnectionError):
        await client.async_get_device_info()


async def test_session_expiry_relogin(client: SwannClient, mock: MockNVR) -> None:
    await client.async_login()
    mock.expire_session()
    info = await client.async_get_device_info()
    assert info.model == "NVW-MR4K"
    assert mock.logins == 2


async def test_login_at_other_does_not_fight(
    client: SwannClient, mock: MockNVR, clock: FakeClock
) -> None:
    await client.async_login()
    mock.kick_session()
    with pytest.raises(SwannSessionTakenError, match="dedicated NVR user"):
        await client.async_heartbeat()
    # No immediate re-login that would kick the other session in turn.
    with pytest.raises(SwannSessionTakenError):
        await client.async_heartbeat()
    assert mock.logins == 1
    clock.now += 300
    await client.async_heartbeat()
    assert mock.logins == 2


async def test_close_logs_out(client: SwannClient, mock: MockNVR) -> None:
    await client.async_login()
    await client.async_close()
    assert mock.logouts == 1
    assert not client.logged_in


# --- device / channels -----------------------------------------------------


async def test_device_info(client: SwannClient) -> None:
    info = await client.async_get_device_info()
    assert info.serial == "N8X1R2A3B4C5D6E7"
    assert info.model == "NVW-MR4K"
    assert info.firmware_version == "V8.2.1-20250310"
    assert info.hardware_version == "DM-588"
    assert info.mac == "00:23:63:AA:BB:CC"
    assert info.channel_count == 8


async def test_device_info_without_systeminfo(
    client: SwannClient, mock: MockNVR
) -> None:
    mock.remove_page("SystemInfo/Base")
    mock.page("Login/DeviceInfo")["mac_addr"] = "00:23:63:AA:BB:CC"
    info = await client.async_get_device_info()
    assert info.serial == "00:23:63:AA:BB:CC"
    assert info.model == "NVW-MR4K"


async def test_channels(client: SwannClient) -> None:
    channels = await client.async_get_channels()
    assert [(c.number, c.name, c.online) for c in channels] == [
        (1, "Driveway", True),
        (2, "Front Door", True),
        (3, "Backyard", True),
        (4, "Garage", False),
    ]
    assert [c.motion_detection_enabled for c in channels] == [True, True, True, False]


async def test_channels_without_motion_page(client: SwannClient, mock: MockNVR) -> None:
    mock.remove_page("AlarmConfig/Motion")
    channels = await client.async_get_channels()
    assert len(channels) == 4
    assert not any(c.motion_detection_enabled for c in channels)


# --- events ----------------------------------------------------------------


async def test_events_flow(client: SwannClient, mock: MockNVR, clock: FakeClock):
    mock.events = [
        fixture_data("event_check_initial"),
        fixture_data("event_check_keepalive"),
        fixture_data("event_check_motion"),
        fixture_data("event_check_ai_person"),
        fixture_data("event_check_alarm_end"),
    ]
    events = await client.async_get_channel_events()
    assert events[1] == ChannelEvents()
    assert mock.event_bodies[0] == {}

    events = await client.async_get_channel_events()
    assert not any(e.motion for e in events.values())
    assert mock.event_bodies[1] == {"reader_id": 3, "sequence": 117, "lap_number": 0}

    events = await client.async_get_channel_events()
    assert events[1] == ChannelEvents(motion=True)

    events = await client.async_get_channel_events()
    assert events[1] == ChannelEvents(motion=True, person=True)
    assert events[2] == ChannelEvents(vehicle=True)

    events = await client.async_get_channel_events()
    assert events[1] == ChannelEvents()
    assert events[2] == ChannelEvents()


async def test_events_hold_then_clear(
    client: SwannClient, mock: MockNVR, clock: FakeClock
) -> None:
    """A pulse without an explicit end clears after the hold time."""
    mock.events = [fixture_data("event_check_motion")]
    events = await client.async_get_channel_events()
    assert events[1].motion
    clock.now += EVENT_HOLD_SECONDS + 1
    assert not client.channel_events()[1].motion


async def test_events_cursor_reset(client: SwannClient, mock: MockNVR) -> None:
    mock.events = [
        fixture_data("event_check_initial"),
        ("readerID_invalid", 400),
        fixture_data("event_check_motion"),
    ]
    await client.async_get_channel_events()
    await client.async_get_channel_events()
    events = await client.async_get_channel_events()
    assert mock.event_bodies[2] == {}  # cursor dropped after the error
    assert events[1].motion


async def test_events_cursor_dropped_after_relogin(
    client: SwannClient, mock: MockNVR
) -> None:
    mock.events = [fixture_data("event_check_initial")]
    await client.async_get_channel_events()
    mock.expire_session()
    await client.async_get_channel_events()
    assert mock.logins == 2
    await client.async_get_channel_events()
    assert mock.event_bodies[-1] == {}


# --- motion detection --------------------------------------------------------


async def test_set_motion_detection(client: SwannClient, mock: MockNVR) -> None:
    before = copy.deepcopy(mock.page(ALARM_PAGE, "ChannelConfig"))
    await client.async_set_motion_detection(1, False)
    path, body = mock.sets[-1]
    assert path == "AlarmConfig/Motion/Set"
    expected = fixture_data("alarm_motion_set_request_channelconfig")
    assert body == expected
    # Only CH1.switch changed.
    before["channel_info"]["CH1"]["switch"] = False
    assert mock.page(ALARM_PAGE, "ChannelConfig") == before
    # Action page untouched.
    assert not any(b.get("page_type") == "AlarmConfig" for _, b in mock.sets)


async def test_set_motion_detection_unknown_channel(client: SwannClient) -> None:
    with pytest.raises(SwannApiError):
        await client.async_set_motion_detection(9, True)


# --- cloud settings ------------------------------------------------------------


def _all_off(settings: CloudSettings) -> CloudSettings:
    return CloudSettings(
        features=dict.fromkeys(settings.features, False), raw=settings.raw
    )


async def test_get_cloud_settings(client: SwannClient) -> None:
    settings = await client.async_get_cloud_settings()
    f = settings.features
    assert f["NetworkConfig/P2P|p2p_switch"] is True
    assert f["StorageConfig/Cloud|cloud_storage"] is True
    assert f["StorageConfig/Server|swann_cloud_info.enable"] is True
    assert f["StorageConfig/Server|dropbox_info.enable"] is False
    assert f["NetworkConfig/DDNS|ddns_enable"] is True
    assert f["Maintenance/AutoUpgrade|auto_upgrade"] is True
    assert f[f"{ALARM_PAGE}|CH1.push"] is True
    assert f[f"{ALARM_PAGE}|CH3.picture_to_cloud"] is False
    # FTP/SFTP are the user's own servers, never touched.
    assert not any("ftp" in k for k in f)
    assert settings.raw["values"] == f


async def test_cloud_off_and_restore_round_trip(
    client: SwannClient, mock: MockNVR
) -> None:
    original_pages = copy.deepcopy(mock.pages)
    saved = await client.async_get_cloud_settings()

    await client.async_set_cloud_settings(_all_off(saved))
    now = await client.async_get_cloud_settings()
    assert not any(now.features.values())
    assert mock.page("NetworkConfig/P2P")["p2p_switch"] is False
    alarm = mock.page(ALARM_PAGE, "AlarmConfig")["channel_info"]["CH1"]
    assert (alarm["push"], alarm["picture_to_cloud"], alarm["record_enable"]) == (
        False,
        False,
        True,
    )
    # Motion detection itself (ChannelConfig view) untouched.
    assert (
        mock.page(ALARM_PAGE, "ChannelConfig")
        == original_pages[(ALARM_PAGE, "ChannelConfig")]
    )
    # P2P is written with only its switch, like the web UI.
    assert ("NetworkConfig/P2P/Set", {"p2p_switch": False}) in mock.sets
    # No password fields are ever sent.
    for _, body in mock.sets:
        assert "password" not in str(body).replace("password_empty", "")

    # Restore from the saved snapshot (as stored by the switch).
    restored = CloudSettings(features=dict(saved.features), raw=saved.raw)
    await client.async_set_cloud_settings(restored)
    assert mock.pages == original_pages


async def test_cloud_set_skips_unchanged_pages(
    client: SwannClient, mock: MockNVR
) -> None:
    saved = await client.async_get_cloud_settings()
    await client.async_set_cloud_settings(saved)
    assert mock.sets == []


async def test_cloud_partial_support(client: SwannClient, mock: MockNVR) -> None:
    for page in (
        "StorageConfig/Cloud",
        "NetworkConfig/DDNS",
        "Maintenance/AutoUpgrade",
    ):
        mock.remove_page(page)
    settings = await client.async_get_cloud_settings()
    assert not any(k.startswith("StorageConfig/Cloud") for k in settings.features)
    assert "NetworkConfig/P2P|p2p_switch" in settings.features
    probes = [p for p, _ in mock.requests if p.endswith("/Range")]
    # Unsupported pages are probed once and then remembered.
    await client.async_get_cloud_settings()
    assert [p for p, _ in mock.requests if p.endswith("/Range")] == probes

    await client.async_set_cloud_settings(_all_off(settings))
    assert not any((await client.async_get_cloud_settings()).features.values())
    assert not any(
        p.startswith(("StorageConfig/Cloud", "NetworkConfig/DDNS"))
        for p, _ in mock.sets
    )


async def test_cloud_restore_skips_vanished_page(
    client: SwannClient, mock: MockNVR
) -> None:
    saved = await client.async_get_cloud_settings()
    await client.async_set_cloud_settings(_all_off(saved))
    other = make_client(client._session, _server_of(client))
    mock.remove_page("NetworkConfig/DDNS")
    # A fresh client (e.g. after an HA restart) restores what still exists.
    await other.async_set_cloud_settings(saved)
    assert mock.page("NetworkConfig/P2P")["p2p_switch"] is True


def _server_of(client: SwannClient) -> Any:
    class _S:
        port = client.port

    return _S()


async def test_cloud_extra_alarm_page(client: SwannClient, mock: MockNVR) -> None:
    pvd = fixture_data("alarm_motion_get_alarmconfig")
    mock.pages[("AI/Setup/PVD", "AlarmConfig")] = pvd
    settings = await client.async_get_cloud_settings()
    assert settings.features["AI/Setup/PVD|CH2.video_to_cloud"] is True
    await client.async_set_cloud_settings(_all_off(settings))
    body = next(b for p, b in mock.sets if p == "AI/Setup/PVD/Set")
    assert body["page_type"] == "AlarmConfig"
    assert body["channel_info"]["CH2"]["video_to_cloud"] is False
