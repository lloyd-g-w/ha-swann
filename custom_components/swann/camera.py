"""Camera platform for Swann NVR: one RTSP camera per channel."""

from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    WebRTCSendMessage,
)
from homeassistant.components.ffmpeg import async_get_image
from homeassistant.components.stream import Stream
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_HW_ACCEL,
    CONF_LIVE_VIEW_VIDEO,
    CONF_RTSP_TRANSPORT,
    CONF_STREAM_TYPE,
    DEFAULT_HW_ACCEL,
    DEFAULT_LIVE_VIEW_VIDEO,
    DEFAULT_RTSP_TRANSPORT,
    DEFAULT_STREAM_TYPE,
    LIVE_VIEW_ORIGINAL,
)
from .coordinator import SwannConfigEntry, SwannCoordinator
from .entity import SwannChannelEntity

PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)

# Snapshots go through ffmpeg on the RTSP stream, which would wake a sleeping
# battery camera (and drain it). So a fresh frame is only taken while the
# camera is awake, at most every 10 s; while it sleeps the last one is reused.
SNAPSHOT_INTERVAL_AWAKE = 10.0

# Live view of battery cameras (HA 2026.10): HA calls stream_source() in many
# places that are not someone watching: provider detection when the entity is
# added (camera.async_refresh_providers -> webrtc.async_get_supported_provider),
# go2rtc snapshots (go2rtc async_get_image -> _update_stream_source) and media
# source browsing. So stream_source() never wakes the camera. The two paths
# that mean "the frontend opened the stream" are wrapped instead:
#   * WebRTC: Camera.async_handle_async_webrtc_offer -> go2rtc provider, which
#     calls stream_source() and then connects to RTSP with a ~5 s read timeout;
#   * HLS: Camera.async_create_stream, which calls stream_source() inside a
#     10 s timeout (CAMERA_STREAM_SOURCE_TIMEOUT), so the wake must come first.
# Both wake the camera and wait for it to report Online before going on.
# async_create_stream is also used by the camera.record / play_stream services
# and, only if the user enables "Preload stream", once at HA start: all are
# explicit requests for video, so waking there is intended.

# Transcoding for live view (HA 2026.10 go2rtc integration). The cameras send
# H.265 video and G.711 audio, which browsers can't play over WebRTC. go2rtc
# takes its source straight from Camera.stream_source(): in
# WebRTCProvider._update_stream_source it registers that string as the stream's
# first producer (re-adding it whenever it differs from what go2rtc has). There
# is no hook for an integration to pass its own go2rtc source, and the only
# special case is a hard-coded "ffmpeg:" prefix for the generic integration.
# But go2rtc's schemes list includes "ffmpeg" (that's what makes the generic
# workaround work), so stream_source() may return
#     ffmpeg:<rtsp url>#video=h264#audio=opus[#hardware]
# and go2rtc runs ffmpeg to transcode while someone watches.
# stream_source() has other callers that need the plain RTSP URL: HA's stream
# worker (HLS, record), snapshots, provider detection and media browsing. So
# the ffmpeg source is returned only to the task running our
# async_handle_async_webrtc_offer: go2rtc's offer handler awaits stream_source()
# in that same task. Keyed by task rather than a ContextVar, which would leak
# into any task created during the offer.


def go2rtc_transcode_source(rtsp_url: str, hardware: bool = False) -> str:
    """Return a go2rtc source that transcodes to H.264 video and Opus audio."""
    source = f"ffmpeg:{rtsp_url}#video=h264#audio=opus"
    if hardware:
        source += "#hardware"
    return source


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SwannConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up cameras."""
    coordinator = entry.runtime_data.coordinator
    stream = entry.options.get(CONF_STREAM_TYPE, DEFAULT_STREAM_TYPE)
    transport = entry.options.get(CONF_RTSP_TRANSPORT, DEFAULT_RTSP_TRANSPORT)
    live_view = entry.options.get(CONF_LIVE_VIEW_VIDEO, DEFAULT_LIVE_VIEW_VIDEO)
    hardware = entry.options.get(CONF_HW_ACCEL, DEFAULT_HW_ACCEL)
    async_add_entities(
        SwannCamera(
            coordinator,
            ch.number,
            stream,
            transport,
            transcode=live_view != LIVE_VIEW_ORIGINAL,
            hardware=hardware,
        )
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
        *,
        transcode: bool = True,
        hardware: bool = False,
    ) -> None:
        """Initialize the camera."""
        SwannChannelEntity.__init__(self, coordinator, channel, "camera")
        Camera.__init__(self)
        self._stream_type = stream
        self._transcode = transcode
        self._hardware = hardware
        self._offer_tasks: set[asyncio.Task] = set()
        self._snapshot: bytes | None = None
        self._snapshot_time = 0.0
        # Read by HA's stream worker (PyAV/ffmpeg rtsp_transport option).
        self.stream_options[CONF_RTSP_TRANSPORT] = transport
        # Overriding async_handle_async_webrtc_offer makes Camera.__init__ treat
        # us as a native WebRTC camera; we only wrap the provider (go2rtc).
        self._supports_native_async_webrtc = False

    async def stream_source(self) -> str | None:
        """Return the stream source; never wakes the camera (see module comment).

        The plain RTSP URL, except for go2rtc during a WebRTC offer with
        transcoding on, which gets an ffmpeg source converting to H.264/Opus.
        """
        url = self.coordinator.client.rtsp_url(self.channel_number, self._stream_type)
        if not (self._transcode and asyncio.current_task() in self._offer_tasks):
            return url
        source = go2rtc_transcode_source(url, self._hardware)
        provider = self.webrtc_provider
        if provider is not None and not provider.async_is_supported(source):
            # go2rtc without ffmpeg: fall back to the original stream.
            _LOGGER.debug("%s: go2rtc can't transcode, using RTSP", self.entity_id)
            return url
        return source

    async def async_wake_for_viewing(self) -> None:
        """Wake a sleeping battery camera and wait until it reports Online."""
        if not self.coordinator.channel_asleep(self.channel_number):
            return
        if not await self.coordinator.async_wake_channel(self.channel_number):
            _LOGGER.debug(
                "%s did not wake in time; opening the stream anyway", self.entity_id
            )

    async def async_create_stream(self) -> Stream | None:
        """Wake the camera before HA's stream worker (HLS) opens it."""
        if self.stream is None:
            await self.async_wake_for_viewing()
        return await super().async_create_stream()

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        """Wake the camera, then let go2rtc connect (transcoding if enabled)."""
        await self.async_wake_for_viewing()
        task = asyncio.current_task()
        assert task is not None
        self._offer_tasks.add(task)
        try:
            await super().async_handle_async_webrtc_offer(
                offer_sdp, session_id, send_message
            )
        finally:
            self._offer_tasks.discard(task)

    @property
    def is_streaming(self) -> bool:
        """Never report streaming: streams are opened on demand only."""
        return False

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a still frame; never wakes a sleeping camera."""
        if not self.coordinator.channel_awake(self.channel_number):
            return self._snapshot
        now = time.monotonic()
        if (
            self._snapshot is not None
            and now - self._snapshot_time < SNAPSHOT_INTERVAL_AWAKE
        ):
            return self._snapshot
        source = await self.stream_source()
        if source is None:
            return self._snapshot
        image = await async_get_image(self.hass, source, width=width, height=height)
        if image:
            self._snapshot = image
            self._snapshot_time = now
        return image or self._snapshot
