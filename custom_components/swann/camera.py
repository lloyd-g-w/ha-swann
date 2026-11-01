"""Camera platform for Swann NVR: one RTSP camera per channel."""

from __future__ import annotations

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import async_get_image
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_STREAM_TYPE, DEFAULT_STREAM_TYPE
from .coordinator import SwannConfigEntry, SwannCoordinator
from .entity import SwannChannelEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SwannConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up cameras."""
    coordinator = entry.runtime_data.coordinator
    stream = entry.options.get(CONF_STREAM_TYPE, DEFAULT_STREAM_TYPE)
    async_add_entities(
        SwannCamera(coordinator, ch, stream) for ch in coordinator.data.channels
    )


class SwannCamera(SwannChannelEntity, Camera):
    """RTSP stream of one NVR channel."""

    _attr_name = None
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self, coordinator: SwannCoordinator, channel: int, stream: str
    ) -> None:
        """Initialize the camera."""
        SwannChannelEntity.__init__(self, coordinator, channel, "camera")
        Camera.__init__(self)
        self._stream_type = stream

    async def stream_source(self) -> str | None:
        """Return the RTSP URL."""
        return self.coordinator.client.rtsp_url(self.channel_number, self._stream_type)

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Grab a still frame from the stream via ffmpeg."""
        source = await self.stream_source()
        if source is None:
            return None
        return await async_get_image(self.hass, source, width=width, height=height)
