"""Constants for the Swann NVR integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "swann"

CONF_USE_HTTPS: Final = "use_https"
CONF_STREAM_TYPE: Final = "stream_type"
CONF_MANAGE_SERVERS: Final = "manage_servers"
# Same key as homeassistant.components.stream.CONF_RTSP_TRANSPORT.
CONF_RTSP_TRANSPORT: Final = "rtsp_transport"

STREAM_MAIN: Final = "main"
STREAM_SUB: Final = "sub"
STREAM_TYPES: Final = [STREAM_MAIN, STREAM_SUB]
RTSP_TRANSPORTS: Final = ["tcp", "udp"]

DEFAULT_PORT: Final = 443
DEFAULT_USE_HTTPS: Final = True
DEFAULT_VERIFY_SSL: Final = False
DEFAULT_STREAM_TYPE: Final = STREAM_MAIN
DEFAULT_MANAGE_SERVERS: Final = False
DEFAULT_RTSP_TRANSPORT: Final = "tcp"
DEFAULT_SCAN_INTERVAL: Final = 10  # seconds
MIN_SCAN_INTERVAL: Final = 2
MAX_SCAN_INTERVAL: Final = 3600

RTSP_PORT: Final = 554

CLOUD_STORE_VERSION: Final = 1
