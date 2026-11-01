"""Data update coordinator for Swann NVR."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass, field, replace
from datetime import timedelta
import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    STATUS_ONLINE,
    STATUS_SLEEP,
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
# Waking a sleeping battery camera for live view: how long to wait for it to
# report Online, how often to poll ChannelInfo meanwhile, and how long the
# wake stream stays open in total (so the camera is still awake when the
# viewer's own connection arrives).
WAKE_TIMEOUT = 20.0
WAKE_POLL_INTERVAL = 2.0
WAKE_STREAM_HOLD = 30.0
# Alerts (event entities, swann_alert bus event): one per rising edge of a
# kind on a channel, at most once per cooldown (a walk-past toggles
# motion_alarm many times).
ALERT_COOLDOWN = 30.0
ALERT_TYPES = ("motion", "person", "vehicle")
ALERT_BUS_EVENT = "swann_alert"
ALERT_KEY = "alert"


def alert_signal(entry_id: str) -> str:
    """Dispatcher signal carrying ``(channel, alert_type)``."""
    return f"{DOMAIN}_{entry_id}_alert"


type SwannConfigEntry = ConfigEntry[SwannRuntimeData]


@dataclass(slots=True)
class SwannData:
    """Polled state of the NVR."""

    channels: dict[int, Channel] = field(default_factory=dict)
    events: dict[int, ChannelEvents] = field(default_factory=dict)
    cloud: CloudSettings | None = None
    # SystemInfo/Base agent_cloud_link_state, read with the cloud settings.
    cloud_link: str | None = None
    # Allowed AlarmConfig/Motion multiple_switch values per channel (0 = all).
    motion_modes: dict[int, tuple[str, ...]] = field(default_factory=dict)


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
        # Live-view wake-ups: one shared task per channel, and an event set
        # whenever new data shows the channel Online.
        self._wake_tasks: dict[int, asyncio.Task[bool]] = {}
        self._awake_events: dict[int, asyncio.Event] = {}
        # Unique IDs of the entities created in this setup (stale cleanup).
        self.created_unique_ids: set[str] = set()
        # Last alert per (channel, type), on this clock (tests replace it).
        self.alert_clock = time.monotonic
        self._last_alert: dict[tuple[int, str], float] = {}

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
            cloud_link = previous.cloud_link if previous else None
            if previous is not None:
                motion_modes = previous.motion_modes
            elif any(ch.motion_mode is not None for ch in channels):
                motion_modes = await self._async_optional(
                    self.client.async_get_motion_modes(), {}, "Motion modes"
                )
            else:
                motion_modes = {}
            if time.monotonic() >= self._cloud_next:
                # Cloud settings are optional: never fail the refresh.
                cloud = await self._async_optional(
                    self.client.async_get_cloud_settings(), cloud, "Cloud settings"
                )
                cloud_link = await self._async_optional(
                    self.client.async_get_cloud_link_state(),
                    cloud_link,
                    "Cloud link state",
                )
                self._cloud_next = time.monotonic() + CLOUD_REFRESH_INTERVAL
        except SwannAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SwannConnectionError as err:
            raise UpdateFailed(f"Error communicating with NVR: {err}") from err
        return SwannData(
            channels={ch.number: ch for ch in channels},
            events=events,
            cloud=cloud,
            cloud_link=cloud_link,
            motion_modes=motion_modes,
        )

    async def _async_optional[T](self, call: Awaitable[T], default: T, what: str) -> T:
        """Await an optional read; log and return ``default`` on NVR errors."""
        try:
            return await call
        except SwannSessionTakenError:
            raise
        except (SwannApiError, SwannConnectionError) as err:
            _LOGGER.warning("%s unavailable: %s", what, err)
            return default

    @property
    def paired_channels(self) -> list[Channel]:
        """Channels with a camera paired (empty slots get no entities)."""
        return [ch for ch in self.data.channels.values() if ch.paired]

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
        previous = self.data.events
        # Update in place rather than async_set_updated_data, which would keep
        # postponing the interval refresh while events are flowing.
        self.data = replace(self.data, events=events)
        self.async_update_listeners()
        self._async_fire_alerts(previous, events)

    @callback
    def async_refresh_events(self) -> None:
        """Publish the client's current event state (e.g. after a command)."""
        if self.data is None:
            return
        events = self.client.channel_events()
        if events != self.data.events:
            self.data = replace(self.data, events=events)
            self.async_update_listeners()

    # --- alerts --------------------------------------------------------------

    @callback
    def _async_fire_alerts(
        self,
        previous: dict[int, ChannelEvents],
        current: dict[int, ChannelEvents],
    ) -> None:
        """Fire an alert for each new motion/person/vehicle (with cooldown)."""
        now = self.alert_clock()
        paired = {ch.number: ch for ch in self.paired_channels}
        for channel, events in current.items():
            if channel not in paired:
                continue
            before = previous.get(channel, ChannelEvents())
            for kind in ALERT_TYPES:
                if not getattr(events, kind) or getattr(before, kind):
                    continue
                last = self._last_alert.get((channel, kind))
                if last is not None and now - last < ALERT_COOLDOWN:
                    continue
                self._last_alert[(channel, kind)] = now
                self._async_alert(channel, paired[channel].name, kind)

    @callback
    def _async_alert(self, channel: int, name: str | None, kind: str) -> None:
        from .entity import channel_identifier  # noqa: PLC0415 (import cycle)

        serial = self.device.serial
        device = dr.async_get(self.hass).async_get_device_by_identifier(
            (DOMAIN, channel_identifier(serial, channel)), self.config_entry.entry_id
        )
        entity_id = er.async_get(self.hass).async_get_entity_id(
            "event", DOMAIN, f"{serial}_ch{channel:02d}_{ALERT_KEY}"
        )
        _LOGGER.debug("Alert CH%d: %s", channel, kind)
        async_dispatcher_send(
            self.hass, alert_signal(self.config_entry.entry_id), channel, kind
        )
        self.hass.bus.async_fire(
            ALERT_BUS_EVENT,
            {
                "device_id": device.id if device else None,
                "entity_id": entity_id,
                "channel": channel,
                "name": name or f"Channel {channel}",
                "type": kind,
            },
        )

    # --- waking battery cameras ---------------------------------------------

    def channel_status(self, channel: int) -> str | None:
        """Live connect_status (Event/Check), else the polled one."""
        if self.data is None:
            return None
        events = self.data.events.get(channel)
        if events is not None and events.connect_status:
            return events.connect_status
        ch = self.data.channels.get(channel)
        return ch.connect_status if ch is not None else None

    def channel_awake(self, channel: int) -> bool:
        """Return True if the live or the polled status is Online."""
        if self.data is None:
            return False
        events = self.data.events.get(channel)
        ch = self.data.channels.get(channel)
        return any(
            status is not None and status.lower() == STATUS_ONLINE
            for status in (
                events.connect_status if events is not None else None,
                ch.connect_status if ch is not None else None,
            )
        )

    def channel_asleep(self, channel: int) -> bool:
        """Return True if the channel is a battery camera that is asleep."""
        status = self.channel_status(channel)
        return (
            status is not None
            and status.lower() == STATUS_SLEEP
            and not self.channel_awake(channel)
        )

    @callback
    def async_update_listeners(self) -> None:
        """Notify entities, and wake-up waiters of channels now Online."""
        super().async_update_listeners()
        for channel, event in self._awake_events.items():
            if self.channel_awake(channel):
                event.set()

    async def async_wake_channel(self, channel: int) -> bool:
        """Wake a sleeping camera and wait until it reports Online.

        Concurrent callers share one wake-up. Returns False if the camera
        did not report Online within WAKE_TIMEOUT.
        """
        if not self.channel_asleep(channel):
            return True
        task = self._wake_tasks.get(channel)
        if task is None or task.done():
            task = self.config_entry.async_create_background_task(
                self.hass, self._async_wake(channel), f"{DOMAIN} wake {channel}"
            )
            self._wake_tasks[channel] = task
        return await asyncio.shield(task)

    async def _async_wake(self, channel: int) -> bool:
        event = self._awake_events.setdefault(channel, asyncio.Event())
        event.clear()
        _LOGGER.debug("Waking channel %s for live view", channel)
        stream = self.config_entry.async_create_background_task(
            self.hass,
            self._async_hold_wake_stream(channel, WAKE_STREAM_HOLD),
            f"{DOMAIN} wake stream {channel}",
        )
        awake = False
        deadline = time.monotonic() + WAKE_TIMEOUT
        try:
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    async with asyncio.timeout(min(WAKE_POLL_INTERVAL, remaining)):
                        await event.wait()
                except TimeoutError:
                    # The event loop may be slow or backing off: poll too.
                    await self._async_poll_channel_status()
                if self.channel_awake(channel):
                    awake = True
                    break
        finally:
            self._awake_events.pop(channel, None)
            if not awake:
                stream.cancel()
            # Once awake, the stream runs out its hold time on its own (it is
            # an entry background task, so unloading cancels it too).
        if awake:
            _LOGGER.debug("Channel %s is awake", channel)
        else:
            _LOGGER.debug(
                "Channel %s did not report Online within %ss", channel, WAKE_TIMEOUT
            )
        return awake

    async def _async_hold_wake_stream(self, channel: int, hold: float) -> None:
        try:
            await self.client.async_wake(channel, hold)
        except SwannError as err:
            _LOGGER.debug("Wake stream for channel %s failed: %s", channel, err)

    async def _async_poll_channel_status(self) -> None:
        try:
            channels = await self.client.async_get_channels()
        except SwannError as err:
            _LOGGER.debug("Channel poll while waking failed: %s", err)
            return
        if self.data is None:
            return
        self.data = replace(self.data, channels={ch.number: ch for ch in channels})
        self.async_update_listeners()
