"""Client for the Swann (RaySharp firmware) NVR HTTP JSON API.

The HTTP endpoints are still being mapped; every method that talks to the
device raises NotImplementedError until that mapping is done. The method
signatures and dataclasses below are the contract the rest of the integration
relies on.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal
from urllib.parse import quote

import aiohttp

from .const import RTSP_PORT

StreamType = Literal["main", "sub"]

# RaySharp RTSP "subtype": 0 = main stream, 1 = sub stream.
_RTSP_SUBTYPE: dict[str, int] = {"main": 0, "sub": 1}


class SwannError(Exception):
    """Base error for the Swann client."""


class SwannConnectionError(SwannError):
    """The NVR could not be reached or returned an unexpected response."""


class SwannAuthError(SwannError):
    """The NVR rejected the credentials."""


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """Static information about the NVR."""

    serial: str
    model: str
    name: str
    firmware_version: str | None = None
    hardware_version: str | None = None
    mac: str | None = None
    channel_count: int = 0


@dataclass(frozen=True, slots=True)
class Channel:
    """A camera channel on the NVR (1-based)."""

    number: int
    name: str
    online: bool = True
    motion_detection_enabled: bool = False


@dataclass(frozen=True, slots=True)
class ChannelEvents:
    """Current alarm/event state of one channel."""

    motion: bool = False
    person: bool = False
    vehicle: bool = False


@dataclass(frozen=True, slots=True)
class CloudSettings:
    """Cloud-related settings of the NVR.

    ``features`` maps a feature name (e.g. P2P, push, cloud upload; exact set
    defined by the API mapping) to whether it is enabled. ``raw`` holds the
    payload as read from the device so the client can write it back faithfully.
    """

    features: dict[str, bool]
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def any_enabled(self) -> bool:
        """Return True if any cloud feature is enabled."""
        return any(self.features.values())

    def all_off(self) -> CloudSettings:
        """Return a copy with every cloud feature disabled."""
        return replace(self, features=dict.fromkeys(self.features, False))

    def as_dict(self) -> dict[str, Any]:
        """Serialize for storage."""
        return {"features": dict(self.features), "raw": self.raw}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CloudSettings:
        """Deserialize from storage."""
        return cls(
            features={str(k): bool(v) for k, v in data["features"].items()},
            raw=dict(data.get("raw", {})),
        )


class SwannClient:
    """Async client for one NVR."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        use_https: bool = True,
        verify_ssl: bool = False,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self.host = host
        self.port = port
        self.username = username
        self._password = password
        self.use_https = use_https
        self.verify_ssl = verify_ssl

    @property
    def base_url(self) -> str:
        """Return the base URL of the web API."""
        scheme = "https" if self.use_https else "http"
        return f"{scheme}://{self.host}:{self.port}"

    def rtsp_url(self, channel: int, stream: StreamType = "main") -> str:
        """Return the RTSP URL for a channel (format confirmed on the device)."""
        user = quote(self.username, safe="")
        password = quote(self._password, safe="")
        return (
            f"rtsp://{user}:{password}@{self.host}:{RTSP_PORT}"
            f"/rtsp/streaming?channel={channel:02d}&subtype={_RTSP_SUBTYPE[stream]}"
        )

    async def async_close(self) -> None:
        """Log out (when implemented) and close the HTTP session."""
        if self._session is not None and not self._session.closed:
            await self._session.close()

    # --- Device API: to be filled in from the API mapping --------------------

    async def async_login(self) -> None:
        """Authenticate with the NVR.

        Raises SwannAuthError / SwannConnectionError.
        """
        raise NotImplementedError

    async def async_get_device_info(self) -> DeviceInfo:
        """Return static device information (serial, model, firmware...)."""
        raise NotImplementedError

    async def async_get_channels(self) -> list[Channel]:
        """Return all channels with name, online and motion-detection state."""
        raise NotImplementedError

    async def async_get_channel_events(self) -> dict[int, ChannelEvents]:
        """Return the current event state keyed by channel number."""
        raise NotImplementedError

    async def async_set_motion_detection(self, channel: int, enabled: bool) -> None:
        """Enable or disable motion detection on a channel."""
        raise NotImplementedError

    async def async_get_cloud_settings(self) -> CloudSettings:
        """Return the NVR's current cloud-related settings."""
        raise NotImplementedError

    async def async_set_cloud_settings(self, settings: CloudSettings) -> None:
        """Apply cloud-related settings to the NVR."""
        raise NotImplementedError
