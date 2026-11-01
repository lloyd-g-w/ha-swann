"""A small in-process mock of the Swann/RaySharp NVR HTTP API.

Responses are built from the JSON fixtures in tests/fixtures (copied from
research/fixtures). Only the behaviour the client relies on is modelled:
Digest login with an X-csrftoken session, Get/Set/Range pages, Event/Check
with a queue of responses, and error injection.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import copy
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any

from aiohttp import web

FIXTURES = Path(__file__).parent / "fixtures"
REALM = "NVR"
NONCE = "6c1f3a9b2e8d4f70"


def load_fixture(name: str) -> dict[str, Any]:
    """Load a fixture JSON file."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


def fixture_data(name: str) -> dict[str, Any]:
    """Return the ``data`` object of a success fixture."""
    return copy.deepcopy(load_fixture(name)["data"])


def _md5(value: str) -> str:
    return hashlib.md5(value.encode()).hexdigest()


def _error(code: str, status: int = 400, data: dict | None = None) -> web.Response:
    return web.json_response(
        {"result": "failed", "error_code": code, "data": data or {}}, status=status
    )


def _ok(data: dict[str, Any] | None = None, **headers: str) -> web.Response:
    return web.json_response({"result": "success", "data": data or {}}, headers=headers)


class MockNVR:
    """State and request handlers of the mock NVR."""

    def __init__(self, username: str = "homeassistant", password: str = "secret"):
        """Initialize with the fixture defaults."""
        self.username = username
        self.password = password
        self.token: str | None = None
        self.token_counter = 0
        self.logins = 0
        self.rtsp_requests: list[tuple[str, str]] = []
        self.rtsp_connections = 0
        # ONVIF backchannel (two-way talk) offered when the client asks for it.
        self.rtsp_backchannel = False
        self.rtsp_describes: list[str | None] = []
        self.rtsp_sdp_host = "192.168.86.152"
        self.wake_delay = 0.0
        self.wake_ok = True
        self.logouts = 0
        self.login_error: tuple[str, int, dict] | None = None
        self.next_error: dict[str, tuple[str, int]] = {}
        # path -> how many more requests to it get their connection dropped
        # (-1: always), like firmware that hangs up instead of answering.
        self.drop: dict[str, int] = {}
        # Like the real NVR: end the session when no heartbeat (or login) came
        # within this many seconds of ``clock``. None disables it.
        self.heartbeat_timeout: float | None = None
        self.clock: Callable[[], float] = time.monotonic
        self.last_heartbeat = 0.0
        self.heartbeats = 0
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.sets: list[tuple[str, dict[str, Any]]] = []
        self.events: list[dict[str, Any] | tuple[str, int]] = []
        self.event_bodies: list[dict[str, Any]] = []
        # PreviewChannel/Floodlight2AudioAlarm state per "CHn" (use_real_mr4k).
        self.deterrence: dict[str, dict[str, Any]] = {}
        # (page, page_type) -> data. page_type None = page without page_type.
        self.pages: dict[tuple[str, str | None], dict[str, Any]] = {
            ("Login/DeviceInfo", None): fixture_data("login_deviceinfo"),
            ("SystemInfo/Base", None): fixture_data("systeminfo_base"),
            ("Login/ChannelInfo", None): fixture_data("login_channelinfo"),
            ("NetworkConfig/P2P", None): fixture_data("networkconfig_p2p_get"),
            ("StorageConfig/Cloud", None): fixture_data("storageconfig_cloud_get"),
            ("StorageConfig/Server", None): fixture_data("storageconfig_server_get"),
            ("NetworkConfig/DDNS", None): fixture_data("networkconfig_ddns_get"),
            ("Maintenance/AutoUpgrade", None): fixture_data(
                "maintenance_autoupgrade_get"
            ),
            ("AlarmConfig/Motion", "ChannelConfig"): fixture_data(
                "alarm_motion_get_channelconfig"
            ),
            ("AlarmConfig/Motion", "AlarmConfig"): fixture_data(
                "alarm_motion_get_alarmconfig"
            ),
        }
        # (page, page_type) -> Range data (default: a generic {"ranged": True}).
        self.ranges: dict[tuple[str, str | None], dict[str, Any]] = {}
        self.app = web.Application()
        self.app.router.add_post("/API/Web/Login", self._login)
        self.app.router.add_post("/API/{path:.+}", self._api)

    # --- helpers -----------------------------------------------------------

    def page(self, page: str, page_type: str | None = None) -> dict[str, Any]:
        """Return the stored data of a page (for assertions)."""
        return self.pages[(page, page_type)]

    def use_real_mr4k(self) -> None:
        """Answer like the user's NVW-MR4K (firmware V8.2.2-20250718).

        Shapes from a real diagnostics download (tests/fixtures/real_mr4k_*);
        only the Motion Range is modelled (see fixtures/README.md).
        """
        for key, name in (
            (("Login/DeviceInfo", None), "login_deviceinfo"),
            (("SystemInfo/Base", None), "systeminfo_base"),
            (("Login/ChannelInfo", None), "login_channelinfo"),
            (("AlarmConfig/Motion", "ChannelConfig"), "alarm_motion_get_channelconfig"),
        ):
            self.pages[key] = fixture_data(f"real_mr4k_{name}")
        self.ranges[("AlarmConfig/Motion", "ChannelConfig")] = fixture_data(
            "real_mr4k_alarm_motion_range_channelconfig"
        )
        self.remove_page("NetworkConfig/P2P")  # 404 on this firmware
        self.events = [fixture_data("real_mr4k_event_check_initial")]
        # The Get answer is not captured; the UI reads the same keys as the
        # Event/Check Floodlight_AudioAlarm object, so model it from that.
        initial = fixture_data("real_mr4k_event_check_initial")
        for alarm in initial["alarm_list"][0]["channel_alarm"]:
            self.deterrence[alarm["channel"]] = copy.deepcopy(
                alarm["Floodlight_AudioAlarm"]
            )

    # --- RTSP stub: opening a stream wakes a sleeping channel ----------------

    def set_channel_status(self, channel: int, status: str) -> None:
        """Set a channel's connect_status in ChannelInfo."""
        info = self.pages[("Login/ChannelInfo", None)]
        items = info["channel_param"]["items"]
        items[channel - 1]["connect_status"] = status

    def channel_status(self, channel: int) -> str:
        info = self.pages[("Login/ChannelInfo", None)]
        return info["channel_param"]["items"][channel - 1]["connect_status"]

    def _wake(self, channel: int) -> None:
        """The camera came up: ChannelInfo and Event/Check report Online."""
        self.set_channel_status(channel, "Online")
        event = copy.deepcopy(fixture_data("real_mr4k_event_check_initial"))
        event["sequence"] = event.get("sequence", 0) + 1
        for alarm in event["alarm_list"][0]["channel_alarm"]:
            if alarm["channel"] == f"CH{channel}":
                alarm["camera_connect_status"] = {"connect_status": "Online"}
        self.events.append(event)

    async def serve_rtsp(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Minimal RTSP server with Digest auth (realm 'device', like the NVR).

        PLAY on a sleeping channel wakes it after ``wake_delay`` seconds
        (unless ``wake_ok`` is False), then a dummy interleaved frame is sent
        now and then until the client goes away.
        """
        self.rtsp_connections += 1
        realm, nonce = "device", "8aa253c3674b"
        wake_task: asyncio.Task | None = None
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                lines = head.decode().split("\r\n")
                method, url, _ = lines[0].split(" ", 2)
                headers = {}
                for line in lines[1:]:
                    if ":" in line:
                        key, _, value = line.partition(":")
                        headers[key.strip().lower()] = value.strip()
                cseq = headers.get("cseq", "0")
                self.rtsp_requests.append((method, url))

                def reply(
                    status: str, extra: str = "", body: str = "", cseq: str = cseq
                ) -> None:
                    text = f"RTSP/1.0 {status}\r\nCSeq: {cseq}\r\n{extra}"
                    if body:
                        text += f"Content-Length: {len(body)}\r\n"
                    writer.write((text + "\r\n" + body).encode())

                if method == "OPTIONS":
                    reply(
                        "200 OK", "Public: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN\r\n"
                    )
                elif method == "TEARDOWN":
                    reply("200 OK")
                    await writer.drain()
                    return
                elif not self._rtsp_auth_ok(headers, method, url, realm, nonce):
                    reply(
                        "401 Unauthorized",
                        f'WWW-Authenticate: Digest realm="{realm}",nonce="{nonce}"\r\n',
                    )
                elif method == "DESCRIBE":
                    host = self.rtsp_sdp_host
                    sdp = (
                        f"v=0\r\no=- 1001 1 IN IP4 {host}\r\ns=RTSP Session\r\n"
                        f"c=IN IP4 {host}\r\n"
                        "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H265/90000\r\n"
                        "a=control:trackID=0\r\na=recvonly\r\n"
                        "m=audio 0 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n"
                        "a=control:trackID=1\r\na=recvonly\r\n"
                    )
                    asked = "www.onvif.org/ver20/backchannel" in headers.get(
                        "require", ""
                    )
                    extra = ""
                    if asked and self.rtsp_backchannel:
                        sdp += (
                            "m=audio 0 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n"
                            "a=control:trackID=2\r\na=sendonly\r\n"
                        )
                    elif asked:
                        extra = "Unsupported: www.onvif.org/ver20/backchannel\r\n"
                    self.rtsp_describes.append(headers.get("require"))
                    reply(
                        "200 OK",
                        f"Content-Base: {url}/\r\nContent-Type: application/sdp\r\n"
                        + extra,
                        sdp,
                    )
                elif method == "SETUP":
                    reply("200 OK", "Session: 12345678;timeout=60\r\n")
                elif method == "PLAY":
                    reply("200 OK", "Session: 12345678\r\n")
                    channel = int(re.search(r"channel=(\d+)", url).group(1))
                    if self.wake_ok and self.channel_status(channel) == "Sleep":
                        wake_task = asyncio.get_running_loop().create_task(
                            self._wake_later(channel)
                        )
                    writer.write(b"$\x00\x00\x04abcd")
                else:
                    reply("405 Method Not Allowed")
                await writer.drain()
        except asyncio.IncompleteReadError, ConnectionError:
            pass
        finally:
            if wake_task is not None and not wake_task.done():
                wake_task.cancel()
            writer.close()

    async def _wake_later(self, channel: int) -> None:
        await asyncio.sleep(self.wake_delay)
        self._wake(channel)

    def _rtsp_auth_ok(
        self, headers: dict[str, str], method: str, url: str, realm: str, nonce: str
    ) -> bool:
        auth = headers.get("authorization", "")
        if not auth.startswith("Digest "):
            return False
        params = dict(re.findall(r'(\w+)="?([^",]*)"?', auth))
        ha1 = _md5(f"{self.username}:{realm}:{self.password}")
        ha2 = _md5(f"{method}:{params.get('uri', '')}")
        if params.get("qop") == "auth":
            expected = _md5(
                f"{ha1}:{nonce}:{params['nc']}:{params['cnonce']}:auth:{ha2}"
            )
        else:
            expected = _md5(f"{ha1}:{nonce}:{ha2}")
        return params.get("response") == expected and params.get("uri") == url

    def remove_page(self, page: str) -> None:
        """Make a page unsupported (Range/Get/Set answer 404)."""
        for key in [k for k in self.pages if k[0] == page]:
            del self.pages[key]

    def expire_session(self) -> None:
        """Forget the current token, as on an idle timeout or reboot."""
        self.token = None

    def kick_session(self) -> None:
        """Simulate another login with the same user taking the session."""
        self.token = "taken-by-other"

    def _digest_ok(self, header: str, method: str) -> bool:
        fields = dict(re.findall(r'(\w+)="?([^",]+)"?', header))
        if fields.get("username") != self.username or fields.get("nonce") != NONCE:
            return False
        ha1 = _md5(f"{self.username}:{REALM}:{self.password}")
        ha2 = _md5(f"{method}:{fields.get('uri')}")
        expected = _md5(
            f"{ha1}:{NONCE}:{fields.get('nc')}:{fields.get('cnonce')}:auth:{ha2}"
        )
        return fields.get("response") == expected

    # --- handlers ----------------------------------------------------------

    async def _login(self, request: web.Request) -> web.Response:
        self.requests.append(("Web/Login", {}))
        auth = request.headers.get("Authorization")
        if auth is None:
            return web.Response(
                status=401,
                headers={
                    "WWW-Authenticate": (
                        f'Digest realm="{REALM}", qop="auth", nonce="{NONCE}", '
                        'opaque="a1b2c3d4e5f6", algorithm="MD5"'
                    )
                },
            )
        if self.login_error:
            code, status, data = self.login_error
            return _error(code, status, data)
        if not self._digest_ok(auth, request.method):
            return _error("verify_failed", 401, {"fail_login_count": 1})
        self.logins += 1
        self.token_counter += 1
        self.token = f"token{self.token_counter:028d}"
        self.last_heartbeat = self.clock()
        data = fixture_data("web_login")
        return _ok(data, **{"X-csrftoken": self.token})

    async def _api(self, request: web.Request) -> web.Response:
        path = request.match_info["path"]
        try:
            body = await request.json()
        except ValueError:
            body = {}
        data = body.get("data") or {}
        self.requests.append((path, data))

        if self.drop.get(path):
            if self.drop[path] > 0:
                self.drop[path] -= 1
            request.transport.close()
            return web.Response()

        if path in self.next_error:
            code, status = self.next_error.pop(path)
            return _error(code, status)
        if self.token is None or request.headers.get("X-csrftoken") != self.token:
            if self.token == "taken-by-other":
                return _error("login_at_other")
            return _error("session_invalid")

        if (
            self.heartbeat_timeout is not None
            and self.clock() - self.last_heartbeat > self.heartbeat_timeout
        ):
            self.token = None
            return _error("no_heartbeat")

        if path == "Web/Logout":
            self.logouts += 1
            self.token = None
            return _ok()
        if path == "Login/Heartbeat":
            self.heartbeats += 1
            self.last_heartbeat = self.clock()
            return _ok()
        if path == "Event/Check":
            return self._event_check(data)
        if path.startswith("PreviewChannel/Floodlight2AudioAlarm/"):
            return self._deterrence(path, data)

        page, _, action = path.rpartition("/")
        page_type = data.get("page_type")
        key = (page, page_type)
        if key not in self.pages and (page, None) in self.pages and page_type:
            key = (page, None)
        if key not in self.pages:
            if any(k[0] == page for k in self.pages):
                return _error("param_error")
            return _error("not_found", 404)
        if action == "Get":
            return _ok(copy.deepcopy(self.pages[key]))
        if action == "Range":
            return _ok(copy.deepcopy(self.ranges.get(key, {"ranged": True})))
        if action == "Set":
            self.sets.append((path, copy.deepcopy(data)))
            stored = self.pages[key]
            new = {k: v for k, v in data.items() if k != "page_type"}
            _deep_update(stored, new)
            return _ok()
        return _error("not_found", 404)

    def _deterrence(self, path: str, data: dict[str, Any]) -> web.Response:
        state = self.deterrence.get(str(data.get("channel")))
        if state is None:
            return _error("param_error")
        if path.endswith("/Get"):
            return _ok(copy.deepcopy(state))
        if path.endswith("/Set"):
            self.sets.append((path, copy.deepcopy(data)))
            for key, value in data.items():
                if key not in ("channel", "operation_type"):
                    state[key] = value
            return _ok()
        return _error("not_found", 404)

    def _event_check(self, data: dict[str, Any]) -> web.Response:
        self.event_bodies.append(copy.deepcopy(data))
        if not self.events:
            return _ok(fixture_data("event_check_keepalive"))
        item = self.events.pop(0)
        if isinstance(item, tuple):
            return _error(*item)
        return _ok(item)


def _deep_update(target: dict[str, Any], new: dict[str, Any]) -> None:
    for key, value in new.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value
