"""Camera platform for Swann NVR: one RTSP camera per channel."""

from __future__ import annotations

import time

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import async_get_image
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_RTSP_TRANSPORT,
    CONF_STREAM_TYPE,
    DEFAULT_RTSP_TRANSPORT,
    DEFAULT_STREAM_TYPE,
)
from .coordinator import SwannConfigEntry, SwannCoordinator
from .entity import SwannChannelEntity

PARALLEL_UPDATES = 0

# Snapshots go through ffmpeg on the RTSP stream, which wakes battery cameras
# (and drains them). Reuse the last frame: while a camera sleeps, take a new
# one at most every 30 minutes; while it is awake, at most every 10 seconds.
SNAPSHOT_INTERVAL_AWAKE = 10.0
SNAPSHOT_INTERVAL_ASLEEP = 1800.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SwannConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up cameras."""
    coordinator = entry.runtime_data.coordinator
    stream = entry.options.get(CONF_STREAM_TYPE, DEFAULT_STREAM_TYPE)
    transport = entry.options.get(CONF_RTSP_TRANSPORT, DEFAULT_RTSP_TRANSPORT)
    async_add_entities(
        SwannCamera(coordinator, ch.number, stream, transport)
        for ch in coordinator.paired_channels
    )


class SwannCamera(SwannChannelEntity, Camera):
    """RTSP stream of one NVR channel."""

    _attr_name = None
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator: SwannCoordinator,
        channel: int,
        stream: str,
        transport: str = DEFAULT_RTSP_TRANSPORT,
    ) -> None:
        """Initialize the camera."""
        SwannChannelEntity.__init__(self, coordinator, channel, "camera")
        Camera.__init__(self)
        self._stream_type = stream
        self._snapshot: bytes | None = None
        self._snapshot_time = 0.0
        # Read by HA's stream worker (PyAV/ffmpeg rtsp_transport option).
        self.stream_options[CONF_RTSP_TRANSPORT] = transport

    async def stream_source(self) -> str | None:
        """Return the RTSP URL."""
        return self.coordinator.client.rtsp_url(self.channel_number, self._stream_type)

    @property
    def is_streaming(self) -> bool:
        """Never report streaming: streams are opened on demand only."""
        return False

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a still frame (cached; see SNAPSHOT_INTERVAL_*)."""
        status = (self.connect_status or "").lower()
        interval = (
            SNAPSHOT_INTERVAL_AWAKE if status == "online" else SNAPSHOT_INTERVAL_ASLEEP
        )
        now = time.monotonic()
        if self._snapshot is not None and now - self._snapshot_time < interval:
            return self._snapshot
        source = await self.stream_source()
        if source is None:
            return self._snapshot
        image = await async_get_image(self.hass, source, width=width, height=height)
        if image:
            self._snapshot = image
            self._snapshot_time = now
        return image or self._snapshot
