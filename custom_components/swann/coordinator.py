"""Data update coordinator for Swann NVR."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    Channel,
    ChannelEvents,
    CloudSettings,
    DeviceInfo,
    SwannAuthError,
    SwannClient,
    SwannConnectionError,
)
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

type SwannConfigEntry = ConfigEntry[SwannRuntimeData]


@dataclass(slots=True)
class SwannData:
    """Polled state of the NVR."""

    channels: dict[int, Channel] = field(default_factory=dict)
    events: dict[int, ChannelEvents] = field(default_factory=dict)
    cloud: CloudSettings | None = None


@dataclass(slots=True)
class SwannRuntimeData:
    """Runtime data stored on the config entry."""

    client: SwannClient
    coordinator: SwannCoordinator
    cloud_store: Store[dict]


class SwannCoordinator(DataUpdateCoordinator[SwannData]):
    """Poll the NVR."""

    config_entry: SwannConfigEntry
    device: DeviceInfo

    def __init__(
        self, hass: HomeAssistant, entry: SwannConfigEntry, client: SwannClient
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.client = client

    async def _async_setup(self) -> None:
        """Log in and fetch static device info (once, before first refresh)."""
        try:
            await self.client.async_login()
            self.device = await self.client.async_get_device_info()
        except SwannAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SwannConnectionError as err:
            raise UpdateFailed(f"Error connecting to NVR: {err}") from err

    async def _async_update_data(self) -> SwannData:
        """Fetch channel, event and cloud state."""
        try:
            channels = await self.client.async_get_channels()
            events = await self.client.async_get_channel_events()
            cloud = await self.client.async_get_cloud_settings()
        except SwannAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SwannConnectionError as err:
            raise UpdateFailed(f"Error communicating with NVR: {err}") from err
        return SwannData(
            channels={ch.number: ch for ch in channels},
            events=events,
            cloud=cloud,
        )
