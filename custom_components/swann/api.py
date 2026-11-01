"""Client for the Swann (RaySharp firmware) NVR HTTP JSON API.

See research/API.md for the protocol. Summary:

* Every call is ``POST /API/<Path>`` with body ``{"version": "1.0", "data": {...}}``.
* ``/API/Web/Login`` uses HTTP Digest auth; the session token comes back in the
  ``X-csrftoken`` response header and goes into an ``X-csrftoken`` request
  header on every later call (plus the session cookie the NVR sets).
* Live alarm state comes from ``/API/Event/Check``, a cursor-based poll.

Settings are changed read-modify-write: Get the whole object, change only the
wanted keys, Set it back without password fields.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
import hashlib
import logging
import os
import re
import time
from typing import Any, Literal
from urllib.parse import quote

import aiohttp

from .const import RTSP_PORT

_LOGGER = logging.getLogger(__name__)

StreamType = Literal["main", "sub"]

# RaySharp RTSP "subtype": 0 = main stream, 1 = sub stream.
_RTSP_SUBTYPE: dict[str, int] = {"main": 0, "sub": 1}

PATH_LOGIN = "/API/Web/Login"
PATH_LOGOUT = "/API/Web/Logout"
PATH_HEARTBEAT = "/API/Login/Heartbeat"
PATH_DEVICE_INFO = "/API/Login/DeviceInfo/Get"
PATH_SYSTEM_INFO = "/API/SystemInfo/Base/Get"
PATH_CHANNEL_INFO = "/API/Login/ChannelInfo/Get"
PATH_EVENT_CHECK = "/API/Event/Check"
PATH_MOTION_GET = "/API/AlarmConfig/Motion/Get"
PATH_MOTION_SET = "/API/AlarmConfig/Motion/Set"

# Pages holding cloud-related flags, with the page_type their Get/Set need.
# Schedules/Cloud and RemoteReport are deliberately left alone.
_ALARM_PAGES: tuple[str, ...] = (
    "AlarmConfig/Motion",
    "AlarmConfig/PIR",
    "AlarmConfig/IO",
    "AlarmConfig/Intelligent/PID",
    "AlarmConfig/Intelligent/LCD",
    "AlarmConfig/Intelligent/SOD",
    "AlarmConfig/Intelligent/PD",
    "AlarmConfig/Intelligent/FD",
    "AlarmConfig/Intelligent/CC",
    "AlarmConfig/Intelligent/SoundDetection",
    "AlarmConfig/Intelligent/OcclusionDetection",
    "AI/Setup/PVD",
    "AI/Setup/PID",
    "AI/Setup/LCD",
    "AI/Setup/SOD",
    "AI/Setup/FD",
    "AI/Setup/LPD",
)
_CLOUD_PAGES: tuple[tuple[str, str | None], ...] = (
    ("NetworkConfig/P2P", None),
    ("StorageConfig/Cloud", None),
    ("StorageConfig/Server", None),
    ("NetworkConfig/DDNS", None),
    ("Maintenance/AutoUpgrade", None),
    *((page, "AlarmConfig") for page in _ALARM_PAGES),
)
# Per-channel alarm actions that go through Swann's cloud.
_ALARM_CLOUD_KEYS = (
    "push",
    "picture_to_cloud",
    "video_to_cloud",
    "picture_to_dropbox",
    "video_to_dropbox",
)
# Upload backends on StorageConfig/Server that are cloud services (FTP/SFTP
# are the user's own servers and stay untouched).
_SERVER_CLOUD_BACKENDS = (
    "swann_cloud_info",
    "dropbox_info",
    "google_drive_info",
    "aws_info",
)
# Top-level boolean flags per page.
_PAGE_FLAGS: dict[str, tuple[str, ...]] = {
    "NetworkConfig/P2P": ("p2p_switch",),
    "StorageConfig/Cloud": ("cloud_storage",),
    "NetworkConfig/DDNS": ("ddns_enable",),
    "Maintenance/AutoUpgrade": ("auto_upgrade",),
}
# Pages whose Set bodies omit stored passwords; writing them may wipe the
# DDNS/upload-server credentials (unverified), so they are opt-in.
SERVER_PAGES: frozenset[str] = frozenset({"StorageConfig/Server", "NetworkConfig/DDNS"})
# Keys never sent back to the NVR (passwords travel encrypted via TransKey).
_SECRET_KEYS = frozenset(
    {"password", "password_confirm", "base_enc_password", "enc_password"}
)

# Timeouts (seconds). The web UI uses 20 s by default and aborts Event/Check
# after 20 s, which suggests the NVR may hold that request open (long poll).
REQUEST_TIMEOUT = 20
EVENT_CHECK_TIMEOUT = 25
LOGOUT_TIMEOUT = 5

# After a rejected login, don't try again for this long: the NVR locks out
# accounts after repeated failures.
LOGIN_BACKOFF = 300
# After another login took our session, wait before logging in again so the
# two sessions don't keep kicking each other out.
SESSION_TAKEN_HOLDOFF = 120

# Edge-style alarms (no explicit "off" ever seen for that channel/kind) are
# cleared this long after the last "on" report.
EVENT_HOLD_SECONDS = 30

# error_code values meaning the session is gone and a new login may help.
_SESSION_ERRORS = frozenset(
    {
        "session_invalid",
        "no_login",
        "expired",
        "logout",
        "token_invalid",
        "param_changed",
        "network_changed",
    }
)
# error_code values from /API/Web/Login meaning the credentials were refused.
_AUTH_ERRORS = frozenset(
    {
        "verify_failed",
        "login_block",
        "login_failed_or_block",
        "black_ip",
        "no_permission",
        "pwd_error_ntime_forgetpwd",
    }
)
# Event/Check cursor errors: start over with an empty cursor.
_CURSOR_ERRORS = frozenset({"readerID_invalid", "position_invalid"})

_CHANNEL_RE = re.compile(r"(\d+)\s*$")


class SwannError(Exception):
    """Base error for the Swann client."""


class SwannConnectionError(SwannError):
    """The NVR could not be reached or returned an unexpected response."""


class SwannAuthError(SwannError):
    """The NVR rejected the credentials."""


class SwannApiError(SwannConnectionError):
    """The NVR answered but refused the request with an error_code."""

    def __init__(self, path: str, error_code: str | None, status: int) -> None:
        """Store the error code."""
        self.path = path
        self.error_code = error_code
        self.status = status
        super().__init__(f"{path} failed: HTTP {status}, error_code={error_code}")


class SwannSessionTakenError(SwannConnectionError):
    """Another login with the same NVR user took over this session."""


class _SessionExpired(SwannError):
    """Internal: the session token is no longer valid."""


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


# --- helpers -----------------------------------------------------------------


def parse_channel(value: Any, default: int | None = None) -> int | None:
    """Return the 1-based channel number from "CH3", "3" or 3."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and (m := _CHANNEL_RE.search(value)):
        return int(m.group(1))
    return default


def parse_digest_challenge(header: str) -> dict[str, str]:
    """Parse a ``WWW-Authenticate: Digest ...`` header."""
    _, _, params = header.partition(" ")
    return {
        m.group(1).lower(): m.group(2) if m.group(2) is not None else m.group(3)
        for m in re.finditer(r'(\w+)=(?:"([^"]*)"|([^\s,]+))', params)
    }


def digest_hash_name(algorithm: str) -> str:
    """Return the hashlib name for a Digest algorithm token."""
    algo = algorithm.upper().removesuffix("-SESS")
    return {"SHA-256": "sha256", "SHA-512-256": "sha512_256"}.get(algo, "md5")


def digest_authorization(
    username: str,
    password: str,
    method: str,
    uri: str,
    challenge: dict[str, str],
    *,
    nc: int = 1,
    cnonce: str | None = None,
) -> str:
    """Build a Digest ``Authorization`` header (RFC 7616, qop=auth)."""
    algorithm = challenge.get("algorithm", "MD5")
    hash_name = digest_hash_name(algorithm)

    def h(value: str) -> str:
        return hashlib.new(hash_name, value.encode()).hexdigest()

    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    cnonce = cnonce or os.urandom(8).hex()
    ha1 = h(f"{username}:{realm}:{password}")
    if algorithm.upper().endswith("-SESS"):
        ha1 = h(f"{ha1}:{nonce}:{cnonce}")
    ha2 = h(f"{method}:{uri}")
    nc_str = f"{nc:08x}"
    qops = [q.strip() for q in challenge.get("qop", "").split(",") if q.strip()]
    parts = [
        f'username="{username}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
    ]
    if "auth" in qops:
        response = h(f"{ha1}:{nonce}:{nc_str}:{cnonce}:auth:{ha2}")
        parts += [
            f'response="{response}"',
            "qop=auth",
            f"nc={nc_str}",
            f'cnonce="{cnonce}"',
        ]
    else:
        response = h(f"{ha1}:{nonce}:{ha2}")
        parts.append(f'response="{response}"')
    if "opaque" in challenge:
        parts.append(f'opaque="{challenge["opaque"]}"')
    if "algorithm" in challenge:
        parts.append(f"algorithm={algorithm}")
    return "Digest " + ", ".join(parts)


def _error_code(payload: Any) -> str | None:
    """Return the error_code of a failure envelope, else None."""
    if not isinstance(payload, dict):
        return None
    for scope in (payload, payload.get("data")):
        if isinstance(scope, dict) and scope.get("result") == "failed":
            return str(scope.get("error_code") or "unknown")
    if isinstance(payload.get("error_code"), str):
        return payload["error_code"]
    return None


def strip_secrets(value: Any) -> Any:
    """Return a copy without password fields, at any depth."""
    if isinstance(value, dict):
        return {k: strip_secrets(v) for k, v in value.items() if k not in _SECRET_KEYS}
    if isinstance(value, list):
        return [strip_secrets(v) for v in value]
    return value


def _extract_cloud_flags(page: str, data: dict[str, Any]) -> dict[str, bool]:
    """Return ``{"<page>|<location>": bool}`` for every cloud flag on a page."""
    found: dict[str, bool] = {}
    for key in _PAGE_FLAGS.get(page, ()):
        if isinstance(data.get(key), bool):
            found[f"{page}|{key}"] = data[key]
    if page == "StorageConfig/Server":
        for backend in _SERVER_CLOUD_BACKENDS:
            info = data.get(backend)
            if isinstance(info, dict) and isinstance(info.get("enable"), bool):
                found[f"{page}|{backend}.enable"] = info["enable"]
    if page in _ALARM_PAGES:
        for chn, cfg in (data.get("channel_info") or {}).items():
            if not isinstance(cfg, dict):
                continue
            for key in _ALARM_CLOUD_KEYS:
                if isinstance(cfg.get(key), bool):
                    found[f"{page}|{chn}.{key}"] = cfg[key]
    return found


def _apply_cloud_flags(
    page: str, data: dict[str, Any], wanted: dict[str, bool]
) -> bool:
    """Set the wanted flags in a page object in place; return True if changed."""
    changed = False
    for feature, value in wanted.items():
        location = feature.split("|", 1)[1]
        target: Any = data
        *parents, key = location.split(".")
        if page in _ALARM_PAGES and parents:
            target = (data.get("channel_info") or {}).get(parents[0])
        elif parents:
            target = data.get(parents[0])
        if not isinstance(target, dict) or key not in target:
            _LOGGER.debug("%s: %s not present any more, skipped", page, location)
            continue
        if target[key] != value:
            target[key] = value
            changed = True
    return changed


@dataclass(slots=True)
class _Level:
    """State of one alarm kind on one channel."""

    on: bool = False
    last_on: float = 0.0
    explicit_off_seen: bool = False

    def set(self, value: bool, now: float) -> None:
        self.on = value
        if value:
            self.last_on = now
        else:
            self.explicit_off_seen = True

    def value(self, now: float) -> bool:
        if not self.on:
            return False
        if self.explicit_off_seen:
            return True
        # Never seen an explicit "off" here: treat it as an edge and expire it.
        if now - self.last_on >= EVENT_HOLD_SECONDS:
            self.on = False
        return self.on


# int_subtype values (research/API.md 3.1) mapped to the sensors they drive.
_PERSON_SUBTYPES = frozenset({"pd", "pd_vd"})
_VEHICLE_SUBTYPES = frozenset({"vd", "pd_vd"})


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
        manage_servers: bool = False,
    ) -> None:
        """Initialize the client.

        ``manage_servers`` lets the cloud settings include DDNS and the cloud
        upload servers (experimental, see SERVER_PAGES).
        """
        self._session = session
        self.host = host
        self.port = port
        self.username = username
        self._password = password
        self.use_https = use_https
        self.verify_ssl = verify_ssl
        self.manage_servers = manage_servers
        self._token: str | None = None
        self._login_lock = asyncio.Lock()
        self._login_generation = 0
        self._login_blocked_until = 0.0
        self._nc = 0
        # Event/Check cursor and per-channel alarm state.
        self._cursor: dict[str, int] | None = None
        self._cursor_generation = -1
        self._levels: dict[tuple[int, str], _Level] = {}
        self._clock = time.monotonic
        self._page_support: dict[str, bool] = {}
        self._taken_until = 0.0

    @property
    def base_url(self) -> str:
        """Return the base URL of the web API."""
        scheme = "https" if self.use_https else "http"
        return f"{scheme}://{self.host}:{self.port}"

    @property
    def logged_in(self) -> bool:
        """Return True if the client holds a session token."""
        return self._token is not None

    def rtsp_url(self, channel: int, stream: StreamType = "main") -> str:
        """Return the RTSP URL for a channel (format confirmed on the device)."""
        user = quote(self.username, safe="")
        password = quote(self._password, safe="")
        return (
            f"rtsp://{user}:{password}@{self.host}:{RTSP_PORT}"
            f"/rtsp/streaming?channel={channel:02d}&subtype={_RTSP_SUBTYPE[stream]}"
        )

    async def async_close(self) -> None:
        """Log out (sessions are limited on the NVR) and detach the HTTP session.

        Detach rather than close: the session comes from Home Assistant's
        async_create_clientsession, which shares HA's connector and must not be
        closed.
        """
        if self._session is None or self._session.closed:
            return
        if self._token is not None:
            try:
                await self._post(PATH_LOGOUT, {}, request_timeout=LOGOUT_TIMEOUT)
            except SwannError as err:
                _LOGGER.debug("Logout failed: %s", err)
            self._token = None
        self._session.detach()

    # --- transport -------------------------------------------------------

    def _ssl(self) -> bool | None:
        return None if self.verify_ssl else False

    def _store_token(self, resp: aiohttp.ClientResponse) -> None:
        token = resp.headers.get("X-csrftoken")
        if token:
            self._token = token.split(",")[0].strip()

    async def _post(
        self,
        path: str,
        data: dict[str, Any] | None = None,
        *,
        request_timeout: float = REQUEST_TIMEOUT,
    ) -> dict[str, Any]:
        """POST to the API once with the current session; return ``data``."""
        headers = {"Accept": "application/json"}
        if self._token:
            headers["X-csrftoken"] = self._token
        try:
            async with self._session.post(
                self.base_url + path,
                json={"version": "1.0", "data": data or {}},
                headers=headers,
                ssl=self._ssl(),
                timeout=aiohttp.ClientTimeout(total=request_timeout),
            ) as resp:
                try:
                    payload = await resp.json(content_type=None)
                except ValueError:
                    payload = None
                status = resp.status
                if status == 200:
                    self._store_token(resp)
        except TimeoutError as err:
            raise SwannConnectionError(f"Timeout calling {path}") from err
        except aiohttp.ClientError as err:
            raise SwannConnectionError(f"Error calling {path}: {err}") from err

        code = _error_code(payload)
        if status == 200 and code is None:
            if not isinstance(payload, dict):
                raise SwannConnectionError(f"Unexpected response from {path}")
            result = payload.get("data")
            return result if isinstance(result, dict) else {}
        if code == "login_at_other":
            # Logging straight back in would kick the other session (often the
            # user's browser or app), which would then kick us: don't fight.
            self._token = None
            self._taken_until = self._clock() + SESSION_TAKEN_HOLDOFF
            raise SwannSessionTakenError(
                "Another login with the same NVR user took over the session. "
                "Use a dedicated NVR user for Home Assistant."
            )
        if code in _SESSION_ERRORS or (status == 401 and code is None):
            raise _SessionExpired(code or "http_401")
        raise SwannApiError(path, code, status)

    async def _request(
        self,
        path: str,
        data: dict[str, Any] | None = None,
        *,
        request_timeout: float = REQUEST_TIMEOUT,
    ) -> dict[str, Any]:
        """POST with login on demand and one re-login if the session expired."""
        if self._token is None:
            if self._clock() < self._taken_until:
                raise SwannSessionTakenError(
                    "NVR session was taken over by another login; waiting "
                    "before logging in again"
                )
            await self.async_login()
        generation = self._login_generation
        try:
            return await self._post(path, data, request_timeout=request_timeout)
        except _SessionExpired as err:
            _LOGGER.debug("Session expired (%s) on %s, logging in again", err, path)
        await self._relogin(generation)
        try:
            return await self._post(path, data, request_timeout=request_timeout)
        except _SessionExpired as err:
            raise SwannConnectionError(
                f"{path}: session rejected right after login ({err})"
            ) from err

    async def _relogin(self, generation: int) -> None:
        """Log in again unless another task already did since ``generation``."""
        async with self._login_lock:
            if generation == self._login_generation:
                self._token = None
                await self._login_locked()

    # --- session -----------------------------------------------------------

    async def async_login(self) -> None:
        """Authenticate with the NVR.

        Raises SwannAuthError / SwannConnectionError.
        """
        async with self._login_lock:
            await self._login_locked()

    async def _login_locked(self) -> None:
        if (wait := self._login_blocked_until - self._clock()) > 0:
            raise SwannAuthError(
                f"Login was refused recently; not retrying for {int(wait)} s"
            )
        try:
            await self._login_exchange()
        except TimeoutError as err:
            raise SwannConnectionError(f"Timeout connecting to {self.host}") from err
        except aiohttp.ClientError as err:
            raise SwannConnectionError(f"Cannot connect to {self.host}: {err}") from err

    async def _login_exchange(self) -> None:
        """Digest challenge/response against /API/Web/Login."""
        url = self.base_url + PATH_LOGIN
        # Same body as the web UI's login call (it has no "version" key).
        body = {"data": {"remote_terminal_info": "WEB,chrome"}}
        headers: dict[str, str] = {"X-Requested-With": "XMLHttpRequest"}
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        for attempt in range(2):
            async with self._session.post(
                url, json=body, headers=headers, ssl=self._ssl(), timeout=timeout
            ) as resp:
                try:
                    payload = await resp.json(content_type=None)
                except ValueError:
                    payload = None
                code = _error_code(payload)
                if resp.status == 200 and code is None:
                    self._store_token(resp)
                    if self._token is None and isinstance(payload, dict):
                        token = (payload.get("data") or {}).get("token")
                        self._token = str(token) if token else None
                    if self._token is None:
                        raise SwannConnectionError("Login returned no token")
                    self._login_generation += 1
                    _LOGGER.debug("Logged in to %s", self.host)
                    return
                challenge = resp.headers.get("WWW-Authenticate", "")
                if (
                    attempt == 0
                    and resp.status == 401
                    and code is None
                    and challenge.lower().startswith("digest")
                ):
                    self._nc += 1
                    headers["Authorization"] = digest_authorization(
                        self.username,
                        self._password,
                        "POST",
                        PATH_LOGIN,
                        parse_digest_challenge(challenge),
                        nc=self._nc,
                    )
                    continue
                if resp.status == 401 or code in _AUTH_ERRORS:
                    backoff = LOGIN_BACKOFF
                    extra = (payload or {}).get("data") if payload else None
                    if isinstance(extra, dict):
                        for key in ("block_remain_time", "login_remain_time"):
                            if isinstance(extra.get(key), int) and extra[key] > 0:
                                backoff = max(backoff, extra[key])
                    self._login_blocked_until = self._clock() + backoff
                    raise SwannAuthError(f"Login rejected ({code or 'HTTP 401'})")
                raise SwannApiError(PATH_LOGIN, code, resp.status)

    async def async_heartbeat(self) -> None:
        """Keep the session alive (the web UI does this every 10 s)."""
        await self._request(PATH_HEARTBEAT)

    # --- device ------------------------------------------------------------

    async def async_get_device_info(self) -> DeviceInfo:
        """Return static device information (serial, model, firmware...)."""
        dev = await self._request(PATH_DEVICE_INFO)
        try:
            sysinfo = await self._request(PATH_SYSTEM_INFO)
        except SwannApiError as err:
            _LOGGER.debug("SystemInfo/Base unavailable: %s", err)
            sysinfo = {}
        info = {**dev, **sysinfo}
        mac = info.get("mac_addr") or info.get("mac_address")
        serial = (
            info.get("serialNum")
            or info.get("serial_number")
            or info.get("serial_id")
            or mac
        )
        if not serial:
            raise SwannConnectionError("NVR reported neither serial nor MAC address")
        model = str(
            info.get("device_model")
            or info.get("model")
            or info.get("device_type")
            or "NVR"
        )
        try:
            channel_count = int(info.get("channel_num") or 0)
        except TypeError, ValueError:
            channel_count = 0
        return DeviceInfo(
            serial=str(serial),
            model=model,
            name=str(info.get("device_name") or f"Swann {model}"),
            firmware_version=info.get("software_version")
            or info.get("firmware_version")
            or info.get("http_api_version"),
            hardware_version=info.get("hardware_version"),
            mac=str(mac) if mac else None,
            channel_count=channel_count,
        )

    async def async_get_channels(self) -> list[Channel]:
        """Return all channels with name, online and motion-detection state."""
        data = await self._request(PATH_CHANNEL_INFO)
        try:
            motion = await self.async_get_motion_detection()
        except SwannApiError as err:
            _LOGGER.debug("Motion detection config unavailable: %s", err)
            motion = {}
        items = (data.get("channel_param") or {}).get("items") or []
        channels: list[Channel] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            number = parse_channel(item.get("channel"), index + 1) or index + 1
            name = item.get("channel_name") or item.get("channel_alias")
            status = str(item.get("connect_status", "online")).lower()
            channels.append(
                Channel(
                    number=number,
                    name=str(name or f"CH{number}"),
                    online=status == "online",
                    motion_detection_enabled=motion.get(number, False),
                )
            )
        return channels

    # --- events --------------------------------------------------------------

    async def async_get_channel_events(self) -> dict[int, ChannelEvents]:
        """Run one Event/Check round and return the event state per channel.

        Keeps a cursor across calls, so call it from one loop only. The NVR may
        hold the request open until something happens.
        """
        if self._cursor_generation != self._login_generation:
            self._cursor = None  # a new login invalidates the reader
        body = dict(self._cursor) if self._cursor else {}
        try:
            data = await self._request(
                PATH_EVENT_CHECK, body, request_timeout=EVENT_CHECK_TIMEOUT
            )
        except SwannApiError as err:
            if err.error_code in _CURSOR_ERRORS:
                _LOGGER.debug("Event cursor reset (%s)", err.error_code)
                self._cursor = None
                return self.channel_events()
            raise
        if body and self._login_generation != self._cursor_generation:
            # Re-logged in during the call: the old reader is stale, start over.
            self._cursor = None
            self._apply_events(data)
            return self.channel_events()
        self._cursor_generation = self._login_generation
        cursor = {
            key: data[key]
            for key in ("reader_id", "sequence", "lap_number")
            if isinstance(data.get(key), int) and not isinstance(data.get(key), bool)
        }
        self._cursor = cursor or None
        self._apply_events(data)
        return self.channel_events()

    def channel_events(self) -> dict[int, ChannelEvents]:
        """Return the current event state per channel (no request)."""
        now = self._clock()
        values: dict[int, dict[str, bool]] = {}
        for (channel, kind), level in self._levels.items():
            values.setdefault(channel, {})[kind] = level.value(now)
        return {
            channel: ChannelEvents(
                motion=kinds.get("motion", False),
                person=kinds.get("person", False),
                vehicle=kinds.get("vehicle", False),
            )
            for channel, kinds in sorted(values.items())
        }

    def _level(self, channel: int, kind: str) -> _Level:
        return self._levels.setdefault((channel, kind), _Level())

    def _apply_events(self, data: dict[str, Any]) -> None:
        now = self._clock()
        for entry in data.get("alarm_list") or []:
            if not isinstance(entry, dict):
                continue
            for alarm in entry.get("channel_alarm") or []:
                if isinstance(alarm, dict):
                    self._apply_channel_alarm(alarm, now)

    def _apply_channel_alarm(self, alarm: dict[str, Any], now: float) -> None:
        channel = parse_channel(alarm.get("channel"))
        if channel is None:
            return
        # Motion: record_flag.m is a level ("R" = active) and wins over the
        # motion_alarm boolean when present.
        flags = alarm.get("record_flag")
        if isinstance(flags, dict) and "m" in flags:
            self._level(channel, "motion").set(flags.get("m") == "R", now)
        elif isinstance(alarm.get("motion_alarm"), bool):
            self._level(channel, "motion").set(alarm["motion_alarm"], now)

        int_alarms = alarm.get("int_alarm")
        if isinstance(int_alarms, dict):
            int_alarms = [int_alarms]
        if not isinstance(int_alarms, list):
            return
        for item in int_alarms:
            if not isinstance(item, dict):
                continue
            subtype = str(item.get("int_subtype", "")).lower()
            value = bool(item.get("alarm_val"))
            if subtype in _PERSON_SUBTYPES:
                self._level(channel, "person").set(value, now)
            if subtype in _VEHICLE_SUBTYPES:
                self._level(channel, "vehicle").set(value, now)

    # --- motion detection (research/API.md 5.1) ------------------------------

    async def _async_get_motion_config(self) -> dict[str, Any]:
        return await self._request(PATH_MOTION_GET, {"page_type": "ChannelConfig"})

    async def async_get_motion_detection(self) -> dict[int, bool]:
        """Return motion detection enable per channel."""
        data = await self._async_get_motion_config()
        result: dict[int, bool] = {}
        for key, cfg in (data.get("channel_info") or {}).items():
            channel = parse_channel(key)
            if channel is not None and isinstance(cfg, dict) and "switch" in cfg:
                result[channel] = bool(cfg["switch"])
        return result

    async def async_set_motion_detection(self, channel: int, enabled: bool) -> None:
        """Enable or disable motion detection on a channel (read-modify-write)."""
        data = await self._async_get_motion_config()
        chn = (data.get("channel_info") or {}).get(f"CH{channel}")
        if not isinstance(chn, dict) or "switch" not in chn:
            raise SwannApiError(PATH_MOTION_GET, "channel_not_supported", 200)
        chn["switch"] = enabled
        data["page_type"] = "ChannelConfig"
        await self._request(PATH_MOTION_SET, strip_secrets(data))

    # --- cloud settings (research/API.md 4) -----------------------------------

    async def _async_page_supported(self, page: str, page_type: str | None) -> bool:
        """Probe ``<page>/Range`` once; unsupported pages are skipped."""
        if page not in self._page_support:
            body = {"page_type": page_type} if page_type else {}
            try:
                await self._request(f"/API/{page}/Range", body)
            except SwannApiError as err:
                _LOGGER.debug("%s not supported: %s", page, err)
                self._page_support[page] = False
            else:
                self._page_support[page] = True
        return self._page_support[page]

    async def _async_get_page(self, page: str, page_type: str | None) -> dict[str, Any]:
        body = {"page_type": page_type} if page_type else {}
        return await self._request(f"/API/{page}/Get", body)

    async def async_get_cloud_settings(self) -> CloudSettings:
        """Return the NVR's cloud-related settings (only on supported pages).

        Feature keys encode where each flag lives, so restoring a saved
        CloudSettings writes back exactly the values it was read with.
        """
        features: dict[str, bool] = {}
        pages: dict[str, Any] = {}
        for page, page_type in _CLOUD_PAGES:
            if page in SERVER_PAGES and not self.manage_servers:
                continue
            if not await self._async_page_supported(page, page_type):
                continue
            try:
                data = await self._async_get_page(page, page_type)
            except SwannApiError as err:
                _LOGGER.debug("Reading %s failed: %s", page, err)
                continue
            found = _extract_cloud_flags(page, data)
            if found:
                features.update(found)
                pages[page] = sorted(found)
        return CloudSettings(
            features=features, raw={"format": 1, "pages": pages, "values": features}
        )

    async def async_set_cloud_settings(self, settings: CloudSettings) -> None:
        """Write cloud flags: per page, read the whole object, change, write back."""
        by_page: dict[str, dict[str, bool]] = {}
        for feature, value in settings.features.items():
            page = feature.split("|", 1)[0]
            by_page.setdefault(page, {})[feature] = value
        page_types = dict(_CLOUD_PAGES)
        for page, wanted in by_page.items():
            if page not in page_types:
                _LOGGER.warning("Ignoring unknown cloud setting page %s", page)
                continue
            if page in SERVER_PAGES and not self.manage_servers:
                _LOGGER.warning(
                    "Not writing %s: managing DDNS/cloud upload servers is "
                    "disabled in the integration options",
                    page,
                )
                continue
            page_type = page_types[page]
            if not await self._async_page_supported(page, page_type):
                _LOGGER.warning("%s no longer supported; not restoring it", page)
                continue
            data = await self._async_get_page(page, page_type)
            if page == "NetworkConfig/P2P":
                value = wanted.get("NetworkConfig/P2P|p2p_switch")
                if value is not None and data.get("p2p_switch") != value:
                    # The web UI sends only this key for P2P.
                    await self._request(
                        "/API/NetworkConfig/P2P/Set", {"p2p_switch": value}
                    )
                continue
            if not _apply_cloud_flags(page, data, wanted):
                continue  # already as wanted: don't write
            if page_type:
                data["page_type"] = page_type
            await self._request(f"/API/{page}/Set", strip_secrets(data))
