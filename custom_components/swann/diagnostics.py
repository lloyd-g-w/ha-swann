"""Diagnostics for Swann NVR: parsed state plus fresh raw API responses.

Meant for supporting firmware we have never seen: the raw responses show the
real field names. Every raw call is independent, so one failure never breaks
the download, and everything identifying is redacted.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict
import re
from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from . import helpers
from .api import SwannClient, redact_sdp, sdp_backchannel_offered, sdp_media_summary
from .const import DOMAIN
from .coordinator import SwannConfigEntry

# Short: the NVR may hold Event/Check open until something happens.
EVENT_CHECK_TIMEOUT = 8
RAW_TIMEOUT = 15

# Exact keys to redact.
_REDACT_KEYS = frozenset(
    {
        "host",
        "username",
        "user",
        "user_name",
        "password",
        "csrf",
        "csrftoken",
        "x-csrftoken",
        "serial",
        "sn",
        "serial_number",
        "serialnum",
        "device_sn",
        "mac",
        "mac_addr",
        "mac_address",
        "ip",
        "ip_addr",
        "ip_address",
        "ipaddr",
        "ipv4",
        "ipv6",
        "gateway",
        "dns",
        "dns1",
        "dns2",
        "p2p_id",
        "p2p_uid",
        "p2p_qr",
        "qr",
        "qr_code",
        "id",
        "domain",
        "hostname",
        "host_name",
        "server",
        "server_addr",
        "email",
    }
)
# Any key containing one of these is redacted too.
_REDACT_SUBSTRINGS = ("pass", "pwd", "key", "secret", "token", "uid", "email", "serial")

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_MAC_RE = re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b")


def _matching_keys(value: Any, found: set[str]) -> set[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            low = str(key).lower()
            if low in _REDACT_KEYS or any(s in low for s in _REDACT_SUBSTRINGS):
                found.add(key)
            _matching_keys(item, found)
    elif isinstance(value, list):
        for item in value:
            _matching_keys(item, found)
    return found


def _scrub_values(value: Any) -> Any:
    """Mask IP and MAC addresses wherever they appear in strings."""
    if isinstance(value, dict):
        return {k: _scrub_values(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_values(v) for v in value]
    if isinstance(value, str):
        return _MAC_RE.sub(REDACTED, _IPV4_RE.sub(REDACTED, value))
    return value


# Long numeric arrays (motion region grids: 1600 cells per channel) are
# summarised; they made diagnostics files hundreds of KB.
_MAX_NUMBER_LIST = 32


def _shorten(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _shorten(v) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) > _MAX_NUMBER_LIST and all(
            isinstance(v, int | float) and not isinstance(v, bool) for v in value
        ):
            return f"[{len(value)} numbers]"
        return [_shorten(v) for v in value]
    return value


def redact(data: Any) -> Any:
    """Redact identifying keys (exact or substring) and IP/MAC values."""
    keys = _REDACT_KEYS | _matching_keys(data, set())
    return _scrub_values(async_redact_data(_shorten(data), keys))


async def _capture(call: Callable[[], Awaitable[Any]]) -> dict[str, Any]:
    try:
        return {"ok": True, "data": await call()}
    except Exception as err:
        return {"ok": False, "error": f"{type(err).__name__}: {err}"}


async def _raw_responses(client: SwannClient) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    for name, path, body in (
        ("device_info", "Login/DeviceInfo/Get", None),
        ("system_info", "SystemInfo/Base/Get", None),
        ("channel_info", "Login/ChannelInfo/Get", None),
        ("motion_config", "AlarmConfig/Motion/Get", {"page_type": "ChannelConfig"}),
        ("motion_range", "AlarmConfig/Motion/Range", {"page_type": "ChannelConfig"}),
        # Cloud-related pages whose shapes we haven't seen on real firmware.
        ("storage_cloud", "StorageConfig/Cloud/Get", None),
        ("auto_upgrade", "Maintenance/AutoUpgrade/Get", None),
        ("netbase", "NetworkConfig/NetBase/Get", None),
        (
            "intelligent_pd",
            "AlarmConfig/Intelligent/PD/Get",
            {"page_type": "AlarmConfig"},
        ),
    ):
        raw[name] = await _capture(
            lambda p=path, b=body: client.async_raw_request(
                p, b, request_timeout=RAW_TIMEOUT
            )
        )
    # A fresh reader (empty body): doesn't touch the event loop's cursor.
    raw["event_check"] = await _capture(
        lambda: client.async_raw_request(
            "Event/Check", {}, request_timeout=EVENT_CHECK_TIMEOUT
        )
    )
    cloud: dict[str, Any] = {}
    for page, page_type in client.cloud_pages:
        result = await _capture(
            lambda p=page, t=page_type: client.async_raw_request(
                f"{p}/Range",
                {"page_type": t} if t else {},
                request_timeout=RAW_TIMEOUT,
            )
        )
        cloud[page] = "supported" if result["ok"] else result["error"]
    raw["cloud_range"] = cloud
    return raw


RTSP_PROBE_SKIPPED = (
    "skipped: no camera awake (open a live view first, then download "
    "diagnostics within 60 s)"
)
# Channels woken (by a live view) this recently are probed too: the NVR may
# keep reporting them "Sleep" while they stream.
RTSP_PROBE_AFTER_WAKE = 60.0


async def _rtsp_probe_one(client: SwannClient, channel: int) -> dict[str, Any]:
    """DESCRIBE the sub stream with and without the ONVIF backchannel tag."""
    sub = 1
    result: dict[str, Any] = {"subtype": sub}
    sdps: dict[str, str] = {}
    for name, backchannel in (("plain", False), ("backchannel", True)):
        try:
            resp = await client.async_rtsp_describe(
                channel, sub, backchannel=backchannel
            )
        except Exception as err:
            result[name] = {"ok": False, "error": f"{type(err).__name__}: {err}"}
            continue
        sdp = resp.pop("sdp") or ""
        sdps[name] = sdp
        result[name] = {
            "ok": True,
            **resp,
            "media": sdp_media_summary(sdp),
            "sdp": redact_sdp(sdp),
        }
    result["backchannel_offered"] = (
        sdp_backchannel_offered(sdps["plain"], sdps["backchannel"])
        if len(sdps) == 2
        else None
    )
    return result


async def _rtsp_probe(client: SwannClient, coordinator: Any) -> Any:
    """Probe RTSP two-way-talk support, only on cameras already awake.

    Opening a sleeping battery camera's stream wakes it, so diagnostics must
    not touch channels that are asleep (or not recently woken).
    """
    data = coordinator.data
    awake = (
        [
            n
            for n in sorted(data.channels)
            if coordinator.channel_awake(n)
            or coordinator.recently_woken(n, RTSP_PROBE_AFTER_WAKE)
        ]
        if data
        else []
    )
    if not awake:
        return RTSP_PROBE_SKIPPED
    return {str(n): await _rtsp_probe_one(client, n) for n in awake}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SwannConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    integration = await async_get_integration(hass, DOMAIN)
    result: dict[str, Any] = {
        "version": str(integration.version),
        "entry_state": entry.state.value,
        "data": dict(entry.data),
        "options": dict(entry.options),
    }

    loaded = entry.state is ConfigEntryState.LOADED and hasattr(entry, "runtime_data")
    if loaded:
        runtime = entry.runtime_data
        client = runtime.client
        coordinator = runtime.coordinator
        data = coordinator.data
        result["coordinator"] = {
            "last_update_success": coordinator.last_update_success,
            "last_exception": (
                f"{type(coordinator.last_exception).__name__}: "
                f"{coordinator.last_exception}"
                if coordinator.last_exception
                else None
            ),
            "update_interval": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "event_loop_running": coordinator.events_running,
            "last_event_ok": coordinator.last_event_ok,
            "last_event_error": coordinator.last_event_error,
        }
        device = getattr(coordinator, "device", None)
        result["device"] = asdict(device) if device else None
        result["channels"] = (
            [
                {**asdict(ch), "abilities": list(ch.abilities)}
                for ch in data.channels.values()
            ]
            if data
            else None
        )
        result["events"] = (
            {str(n): asdict(ev) for n, ev in data.events.items()} if data else None
        )
        result["cloud_features"] = (
            dict(data.cloud.features) if data and data.cloud else None
        )
        result["cloud_page_support"] = client.cloud_page_support
        result["cloud_link"] = data.cloud_link if data else None
        result["motion_modes"] = (
            {str(n): list(m) for n, m in data.motion_modes.items()} if data else None
        )
        # Walk past a camera, then download: shows real event payloads.
        result["recent_events"] = client.recent_events
        # Open a live view, then download: shows how the wake-up went.
        result["last_wake"] = {
            str(n): record.as_dict()
            for n, record in sorted(coordinator.last_wakes.items())
        }
        result["raw"] = await _raw_responses(client)
        result["rtsp_probe"] = await _rtsp_probe(client, coordinator)
    else:
        # Not loaded (e.g. setup keeps failing): probe with a temporary client.
        client = helpers.create_client(hass, entry.data)
        try:
            result["raw"] = await _raw_responses(client)
        finally:
            await client.async_close()

    return redact(result)
