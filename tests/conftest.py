"""Fixtures for Swann NVR tests."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import patch

from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.swann.api import (
    Channel,
    ChannelEvents,
    CloudSettings,
    DeviceInfo,
    SwannClient,
)
from custom_components.swann.const import CONF_USE_HTTPS, DOMAIN

SERIAL = "NVW-MR4K-0001"

USER_INPUT = {
    CONF_HOST: "192.0.2.10",
    CONF_PORT: 443,
    CONF_USE_HTTPS: True,
    CONF_VERIFY_SSL: False,
    CONF_USERNAME: "admin",
    CONF_PASSWORD: "p@ss:w/rd",
}

ORIGINAL_CLOUD = CloudSettings(
    features={"p2p": True, "push": True, "cloud_upload": False},
    raw={"opaque": 1},
)


class FakeSwannClient(SwannClient):
    """In-memory stand-in for the NVR."""

    def __init__(self) -> None:
        super().__init__(
            None,  # type: ignore[arg-type]
            USER_INPUT[CONF_HOST],
            USER_INPUT[CONF_PORT],
            USER_INPUT[CONF_USERNAME],
            USER_INPUT[CONF_PASSWORD],
        )
        self.login_error: Exception | None = None
        self.device = DeviceInfo(
            serial=SERIAL,
            model="NVW-MR4K",
            name="Swann NVR",
            firmware_version="1.0.0",
            channel_count=2,
        )
        self.channels = {
            1: Channel(1, "Front Door", online=True, motion_detection_enabled=True),
            2: Channel(2, "Garden", online=True, motion_detection_enabled=False),
        }
        self.events = {
            1: ChannelEvents(motion=True, person=True, vehicle=False),
            2: ChannelEvents(),
        }
        self.cloud = ORIGINAL_CLOUD
        self.cloud_writes: list[CloudSettings] = []
        self.closed = 0

    async def async_close(self) -> None:
        self.closed += 1

    async def async_login(self) -> None:
        if self.login_error:
            raise self.login_error

    async def async_get_device_info(self) -> DeviceInfo:
        return self.device

    async def async_get_channels(self) -> list[Channel]:
        return list(self.channels.values())

    async def async_get_channel_events(self) -> dict[int, ChannelEvents]:
        return dict(self.events)

    async def async_set_motion_detection(self, channel: int, enabled: bool) -> None:
        ch = self.channels[channel]
        self.channels[channel] = Channel(ch.number, ch.name, ch.online, enabled)

    async def async_get_cloud_settings(self) -> CloudSettings:
        return self.cloud

    async def async_set_cloud_settings(self, settings: CloudSettings) -> None:
        self.cloud_writes.append(settings)
        self.cloud = settings


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations in all tests."""
    return


@pytest.fixture(autouse=True)
def mock_ffmpeg_version() -> Generator[None]:
    """Don't spawn ffmpeg when the ffmpeg dependency is set up."""
    with patch(
        "homeassistant.components.ffmpeg.FFmpegManager.async_get_version",
        return_value=None,
    ):
        yield


@pytest.fixture
def fake_client() -> Generator[FakeSwannClient]:
    """Patch client creation to return a fake client."""
    client = FakeSwannClient()
    with patch("custom_components.swann.helpers.create_client", return_value=client):
        yield client


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Return a config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Swann NVR",
        unique_id=SERIAL,
        data=dict(USER_INPUT),
    )


@pytest.fixture(autouse=True)
def no_event_loop(request) -> Generator[None]:
    """Don't run the background Event/Check loop unless a test asks for it."""
    if "event_loop_enabled" in request.keywords:
        yield
        return
    with patch(
        "custom_components.swann.coordinator.SwannCoordinator.async_start_events"
    ):
        yield
