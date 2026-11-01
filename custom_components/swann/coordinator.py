"""Data update coordinator for Swann NVR."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import timedelta
import logging
import time

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
    SwannApiError,
    SwannAuthError,
    SwannClient,
    SwannConnectionError,
    SwannError,
    SwannSessionTakenError,
)
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Cloud settings span many pages; read them rarely unless a switch changed them.
CLOUD_REFRESH_INTERVAL = 300
# Event/Check pacing: if the NVR answers immediately instead of holding the
# request, wait this long between rounds. Back off on errors.
EVENT_MIN_INTERVAL = 1.0
EVENT_ERROR_DELAY = 10.0
# The NVR ends sessions without a heartbeat (error no_heartbeat); the web UI
# sends one every 10 s.
HEARTBEAT_INTERVAL = 10.0

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
    """Poll channels/settings on an interval; events come from a separate loop."""

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
        self._cloud_next = 0.0
        self._events_started = False
        # Event loop status (for diagnostics).
        self.last_event_ok: float | None = None
        self.last_event_error: str | None = None

    async def _async_setup(self) -> None:
        """Log in and fetch static device info (once, before first refresh)."""
        try:
            await self.client.async_login()
            self.device = await self.client.async_get_device_info()
        except SwannAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SwannConnectionError as err:
            raise UpdateFailed(f"Error connecting to NVR: {err}") from err

    async def async_refresh_cloud(self) -> None:
        """Refresh now, including the cloud settings."""
        self._cloud_next = 0.0
        await self.async_refresh()

    async def _async_update_data(self) -> SwannData:
        """Fetch channel and (occasionally) cloud state."""
        previous = self.data
        try:
            channels = await self.client.async_get_channels()
            if previous is None:
                # Initial event state; afterwards the event loop owns Event/Check.
                events = await self.client.async_get_channel_events()
            else:
                events = previous.events
            cloud = previous.cloud if previous else None
            if time.monotonic() >= self._cloud_next:
                try:
                    cloud = await self.client.async_get_cloud_settings()
                except SwannSessionTakenError:
                    raise
                except (SwannApiError, SwannConnectionError) as err:
                    # Cloud settings are optional: never fail the refresh.
                    _LOGGER.warning("Cloud settings unavailable: %s", err)
                self._cloud_next = time.monotonic() + CLOUD_REFRESH_INTERVAL
        except SwannAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SwannConnectionError as err:
            raise UpdateFailed(f"Error communicating with NVR: {err}") from err
        return SwannData(
            channels={ch.number: ch for ch in channels},
            events=events,
            cloud=cloud,
        )

    @property
    def events_running(self) -> bool:
        """Return True once the Event/Check loop has been started."""
        return self._events_started

    def async_start_events(self) -> None:
        """Start the Event/Check and heartbeat loops (stopped on unload)."""
        if self._events_started:
            return
        self._events_started = True
        self.config_entry.async_create_background_task(
            self.hass, self._async_event_loop(), f"{DOMAIN} events"
        )
        self.config_entry.async_create_background_task(
            self.hass, self._async_heartbeat_loop(), f"{DOMAIN} heartbeat"
        )

    async def _async_heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            await self.async_send_heartbeat()

    async def async_send_heartbeat(self) -> None:
        """Send one heartbeat; the client logs in again if the session is gone."""
        try:
            await self.client.async_heartbeat()
        except SwannError as err:
            # The interval refresh reports real failures (reauth, unavailable).
            _LOGGER.debug("Heartbeat failed: %s", err)

    async def _async_event_loop(self) -> None:
        while True:
            started = time.monotonic()
            delay = EVENT_MIN_INTERVAL
            try:
                await self.async_poll_events()
            except SwannAuthError as err:
                # The interval refresh turns this into a reauth prompt.
                _LOGGER.debug("Event check: authentication failed: %s", err)
                self.last_event_error = f"{type(err).__name__}: {err}"
                delay = EVENT_ERROR_DELAY
            except SwannConnectionError as err:
                _LOGGER.debug("Event check failed: %s", err)
                self.last_event_error = f"{type(err).__name__}: {err}"
                delay = EVENT_ERROR_DELAY
            else:
                self.last_event_ok = time.time()
                self.last_event_error = None
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.0, delay - elapsed))

    async def async_poll_events(self) -> None:
        """Run one Event/Check round and notify entities if anything changed."""
        events = await self.client.async_get_channel_events()
        if self.data is None or events == self.data.events:
            return
        # Update in place rather than async_set_updated_data, which would keep
        # postponing the interval refresh while events are flowing.
        self.data = replace(self.data, events=events)
        self.async_update_listeners()
