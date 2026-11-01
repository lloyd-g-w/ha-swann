# Swann NVW-MR4K (RaySharp OEM) — local HTTP API reference

Firmware web UI `version_web = "1.2.1.83"` (core.js). Written for a Home Assistant integration
built without live access. **No API request was sent to the device** while writing this; only
static JS chunks were fetched (`login.js`, `main.js`, `live.js`, `remoteSetting.js` from
`https://192.168.86.152/js/<name>.904546b907b1098c8211.js`, gzip-encoded, now in `research/webui-js/`).

## Source and confidence legend

| Tag | Source |
|---|---|
| **[UI]** | Web UI JS of this firmware (`research/webui-js/*.js`, beautified copies made with js-beautify). Ground truth for paths and request shapes. |
| **[HA]** | `research/raysharp-ha` (github.com/11058/OEM-RaySharp-NVR-Ai, HA integration for RaySharp API "V8.2.x"; its comments say tested on V8.2.4.1). Real device responses, but a different (newer, Russian-market) NVR. |
| **[DOC]** | `research/raysharp-docs` (jonathanrobichaud4). **Older `/action/...` API** (2017/2019 docs). Not the `/API/...` API of this firmware. Useful only for RTSP rules and as background. |
| **[GUESS]** | Inference. Must be checked on the device. |

Confidence: **H** = seen in code/strings directly · **M** = consistent across sources but no response sample from *this* device · **L** = guess.

---

## 0. Transport and envelope

* Base: `https://<ip>/API/<Path>` (HTTPS 443, self-signed cert). The web UI uses relative URLs, so HTTP on port 80 works the same way when it is enabled. `NetworkConfig/https` (`net_https`) may enforce a redirect; error code `http_redirect_https` exists. [UI, H]
* **Every call is `POST`**, including "Get"/"Range" operations (axios wrapper `h()` in core.js module 67: `method: "POST"`). [UI, H]
* The UI appends a cache-buster query: `?<yyyy-MM-dd@hh:mm:ss>`, e.g. `/API/Login/Heartbeat?2026-11-01@16:30:00`. Not required (the HA client never sends it). [UI, H] [HA, M]
* Headers: `X-csrftoken: <token>`, `Accept: application/json; charset=utf-8`, `Content-Type: application/json`. [UI, H]
* Session cookie: the UI never reads or sets a cookie itself. The login *response* sets a session cookie and the browser sends it back automatically. The HA client keeps an aiohttp cookie jar and also sends `X-csrftoken`. **Do both.** [UI, H for header; HA, M for cookie] (index.js also accepts a `token` cookie for the VMS embedding case.)
* Request body: `{"version": "1.0", "data": {...}}`. `apiVersion.version` defaults to `"1.0"` in core.js. [UI, H]
* Response, success: HTTP 200, `{"result": "success", "data": {...}}`. [HA, M; UI reads `e.data.*` and `i.data.result`]
* Response, failure: HTTP 400 (sometimes 401/403), `{"result": "failed", "error_code": "<code>", "data": {...}}`. The UI reads `responseJSON.error_code`. Some AI endpoints return HTTP 200 with `data.result == "failed"` and `data.error_code`. [UI, H]
* Standard verbs per resource: `<Path>/Range` (capabilities, enums, min/max), `<Path>/Get`, `<Path>/Set`, `<Path>/Default`; some also have `/Search`, `/Control`, `/Check`. The UI calls `Range` and `Get` together (`initParamPageData`). [UI, H]
* Timeouts used by the UI: 20 s default, 30 s for page `Get`, 120 s for `Search`, 60 s for `ChannelConfig/AutoAddIPC`, `ChannelConfig/ImageControl` and `IPCMaintaint/IPCReboot` Set. [UI, H]

### error_code values (ENU.js `error_code{}`) [UI, H]
`param_error no_permission first_login passwd_weak_login time_abnormal_login passwd_expired_login passwd_expires_state save_failed search_failed session_invalid network_error data_error not_found method_not_allowed http_redirect_https token_invalid verify_failed login_block login_failed_or_block black_ip illegal_operation no_login expired logout login_at_other device_reboot passwd_expired param_changed network_changed frequent_operation have_login`, plus about 60 upgrade/cert/email ones.
Event/Check also uses `readerID_invalid` and `position_invalid` (core.js module 797).

Handling (core.js `handleApiExceptions` / `sysErrorMatLg`) [UI, H]:
* `session_invalid`, `no_login`, `expired`, `logout`, `login_at_other`, `token_invalid` → session is dead → log in again. `login_at_other` means **another login took over the session** (see the session limits below).
* `device_reboot` → NVR is rebooting. The UI retries Event/Check and gives up after 180 s.
* `param_changed`, `network_changed` → config changed elsewhere; log in again or re-read.
* `no_permission` → the user lacks this right.

---

## 1. Authentication and session

### 1.1 Pre-login (no auth needed)
`POST /API/Login/Range` body `{"version":"1.0","data":{}}` → login page capabilities. [UI, H fields from core.js `init` case "/API/Login/Range"]
Fields read: `first_login_flag, default_username, support_first_login_language, site_version, username{min_len,max_len}, password{min_len,max_len}, lang_strs, pwd_tip, Savepassword, support_recover_password, use_recover_password, device_type, default_lang, custom_name, custom_logo, password_tips, login_exclusivity`.
Fixture: `fixtures/login_range.json` (values are [GUESS]).
* `first_login_flag: true` → the device has no admin password yet; the UI then calls `FirstLogin/Password`. The integration should refuse to set up and ask the user to finish setup on the NVR.
* `login_exclusivity` / `custom_name`: when exclusivity is supported (`supportLoginExclusivity(custom_name)`), the UI sends `oem_type: <custom_name>` in the login body. Swann's `custom_name` value is unknown → [GUESS] send it only if `login_exclusivity` is present.

`POST /API/Login/DeviceInfo/Get` and `/API/Login/ChannelInfo/Get` also exist (`login_deviceInfo`, `login_channelInfo`). They are used after login; whether they need auth is unknown [GUESS: they do].

### 1.2 Login — `POST /API/Web/Login` [UI, H]
HTTP **Digest** auth (RFC 2617/7616, own implementation in core.js module 446, `digestAuthRequest` v0.8.0):
1. First POST with no `Authorization` (XHR opened with user/pass, so the browser may send Basic) → `401` + `WWW-Authenticate: Digest realm=..., nonce=..., qop="auth", opaque=..., [algorithm=SHA-256|MD5]`.
2. Second POST with `Authorization: Digest username="..", realm="..", nonce="..", uri="/API/Web/Login", response="..", opaque="..", qop=auth, nc=00000001, cnonce=".."` (the qop value is sent unquoted).
   * HA1 = H(user:realm:pass), HA2 = H("POST":uri), resp = H(HA1:nonce:nc:cnonce:qop:HA2). H is SHA-256 if `algorithm=SHA-256`, otherwise MD5.
   * **The uri in the digest must be exactly the request path** (the UI's login call has no cache-buster).
   * Headers: `Content-type: application/json`, `X-Requested-With: XMLHttpRequest`.
3. Body (JSON): `{"data": {"remote_terminal_info": "WEB,chrome"}}` (+ `"oem_type": <custom_name>` when exclusivity is supported). **Note: no `version` key** in the login body. The HA client sends `{"version":"1.0","data":{}}`, which also works on V8.2.x [HA, M]. aiohttp/httpx `DigestAuth` work; the HA client uses `aiohttp.DigestAuthMiddleware` (aiohttp ≥ 3.12) [HA, H].
4. Success: HTTP 200; **token is in the response header `X-csrftoken`**. If it is comma-separated, take the first element with spaces removed. Body `data` fields: `last_login_time, last_login_ip, pwd_remain_time, fail_login_count, user, token, strength_need_check`. [UI, H] Fixture: `fixtures/web_login.json`.
5. Failure: HTTP 401/400 with `error_code`: `verify_failed` (bad credentials), `login_block` (`data.login_remain_time`), `login_failed_or_block` (`data.block_remain_time`), `black_ip`, `no_permission`, `have_login`, `pwd_error_ntime_forgetpwd`. [UI login.js, H]
   * **Brute-force lockout exists** → never retry login in a tight loop. Back off for minutes after `verify_failed`, stop on `login_block`/`login_failed_or_block` and raise reauth.
6. After login, digest is **not** used again: all other calls carry `X-csrftoken` + cookie. [UI, H] (The HA client keeps the digest middleware on the session and works on V8.2; harmless.)

### 1.3 Heartbeat — `POST /API/Login/Heartbeat` [UI, H]
Body `{"version":"1.0","data":{}}`. The UI runs it in a Web Worker every **10 s** (`timeout_time = 1e4`). Fixture: `fixtures/heartbeat.json` (data is `{}` [GUESS]).
Session idle timeout is unknown [TODO]. The HA client heartbeats once per coordinator poll (30 s default) and that is enough on V8.2 [HA, M]. Recommendation: heartbeat every 10–20 s. Event/Check long-poll traffic probably also keeps the session alive [GUESS].
A dead session gives HTTP 400/401 with `session_invalid`/`no_login`/`expired`; the HA client notes "HTTP 400, not 401" [HA, M].

### 1.4 Logout — `POST /API/Web/Logout` body `{"version":"1.0","data":{}}` [UI, H]
Call on unload. Sessions are limited (below), so leaked sessions can lock HA out.

### 1.5 Session limits [GUESS/M]
* `login_at_other` + `login_exclusivity` mean the firmware can **kick an older session of the same user** when the same user logs in again (exclusive login). A dedicated NVR user for HA is strongly recommended, so the user's browser/app sessions don't fight HA.
* Max concurrent web sessions: unknown [TODO] (RaySharp typically allows about 10 network users).
* `Login/TransKey`, `Web/Get_Private_Key`, `Maintenance/TransKey`: RSA/AES key exchange the UI uses to encrypt **passwords inside Set bodies** (user management, email/FTP/DDNS passwords). Not needed for read-only or boolean toggles. **Avoid round-tripping password fields** in Set bodies [GUESS].

---

## 2. Device info, channels, streams

### 2.1 Device info
* `POST /API/SystemInfo/Base/Get` — model, firmware, serial, MAC, channel count. [UI path, H; fields HA, M] Fixture `fixtures/systeminfo_base.json`.
* `POST /API/Login/DeviceInfo/Get` — the UI calls this right after login (`initDeviceData`), giving `devInfo`: capability flags (`videoloss_status`, `nocamera_display`, …). The HA client also uses it as `API_DEVICE_INFO` [HA, H]. Fixture `fixtures/login_deviceinfo.json`.
* `POST /API/SystemInfo/Channel/Get`, `/API/SystemInfo/Record/Get`, `/API/SystemInfo/Network/Get` — status pages. [UI path H, shape M]
* Disk: `POST /API/StorageConfig/Disk/Get` [UI H] (HA uses this for disk sensors).

### 2.2 Channel list — `POST /API/Login/ChannelInfo/Get` [UI H]
The UI keeps it as `chnInfo.channel_param.items[]` indexed by channel number - 1. Per item: `channel` ("CH1"), `channel_name`, `channel_alias`, `connect_status` ("Online"/"Offline" [M]), `ability[]` (capability strings, e.g. "md", "pd", "vd", "Floodlight", "AudioAlarm" [M]). Event/Check pushes `camera_connect_status` deltas into the same objects (live.js `vt()`). [UI H]
Fixture: `fixtures/login_channelinfo.json`.
IP channel details (IP, model of the camera, protocol): `POST /API/ChannelConfig/IPChannel/Get` [UI path H, fields L].

### 2.3 RTSP
* Confirmed on device (by user): `rtsp://<user>:<pass>@<ip>:554/rtsp/streaming?channel=01&subtype=0` (main), `subtype=1` (sub). Digest or basic auth.
* `POST /API/Preview/StreamUrl/Get` → per-channel URLs as the device reports them (the UI uses this for its RTSP/WebSocket player; HA uses it as `API_STREAM_URL`) [UI H path; shape HA M]. Fixture `fixtures/preview_streamurl.json`.
* RTSP port/enable: `POST /API/NetworkConfig/Rtsp/Get|Set` [UI H].
* Older-firmware form `rtsp://ip:554/chA/B` [DOC, L for this device].

### 2.4 Snapshot
* `POST /API/Snapshot` (UI key `Snapshot`) with `data: {"channel": "CH1"}` [UI path H; body GUESS], returns JPEG (responseType blob). The HA client uses `/API/Snapshot/Get` [HA]. See §7 [TODO verify].
* Fallback: HA `camera` from the RTSP sub-stream via ffmpeg (always works).

---


## 3. Live events / alarm state

### 3.1 Mechanism: `POST /API/Event/Check` (cursor-based polling, near real time) [UI H]
This is how the UI gets **all** live state (core.js module 797, `alarmReport + "/Check"`). No websocket, no SSE.
* First request: `{"version":"1.0","data":{}}`.
* Next requests: `{"version":"1.0","data":{"reader_id":<int>,"sequence":<int>,"lap_number":<int>}}`, using the values from the previous response.
* The UI re-requests **1 ms** after each response, and aborts/retries if a request takes > **20 s**. If the response contains `heat_alarm` (a keep-alive: "nothing new"), it skips dispatching and loops again.
* Errors: `readerID_invalid`/`position_invalid` → start over with an empty body. `device_reboot` → retry every 20 s, treat as expired after 180 s. Other errors → retry immediately (1 ms) when there is a JSON body, after 20 s when there is none (network error).
* Whether the device *holds* the request (long poll) or answers immediately is **firmware-dependent**: the HA client says "NVR responds immediately" on V8.2.4.1, so it sleeps 2 s between calls [HA, M]. The UI's 1 ms loop plus 20 s abort suggests the server holds the request for up to about 10–20 s when there is nothing to report [GUESS]. **Implement both**: loop with a 25 s client timeout; if responses come back in < 200 ms, throttle to about 1 s.
* **Latency for HA binary sensors: about 0.1–2 s** (one round trip). This is the event source to use.
* One reader per session. Re-login (new token) invalidates `reader_id` → reset the cursor.

Response fields read by the UI (live.js `vt()`, core.js 797). Fixtures `fixtures/event_check_initial.json`, `event_check_motion.json`, `event_check_ai_person.json`, `event_check_keepalive.json`:
```json
{"result":"success","data":{
  "reader_id": 3, "sequence": 118, "lap_number": 0,
  "videoloss_status": "...", "hdmi2_enable": false,
  "alarm_list": [ {
     "time": "2026-11-01 16:30:05",
     "channel_alarm": [ {
        "channel": "CH1",
        "channel_name": "Driveway",            // name change
        "channel_alias": "...",
        "camera_connect_status": {"connect_status":"Online","ability":[...]},  // merged into chnInfo item
        "record_flag": {"h":"","i":"","m":"R","s":"","p":"","r":"R","t":""},  // OSD status letters
        "videoloss": false,
        "int_alarm": {"alarm_val": true, "int_subtype": "pd"},   // AI/intelligent alarm
        "motion_alarm": true, "io_alarm": false, "pir_alarm": false,
        "Floodlight_AudioAlarm": {...},        // deterrence state
        "ptz_alarm": ..., "talkback_alarm": [...], "doorbell_call": true
     } ],
     "talkback_alarm": [...],
     "system_alarm": [ {"hdd": {"hdd_alarm_type": "Ok"}} ],
     "alarm_out_state": [...]
  } ],
  "ai_snap_picture": {"SnapedObjInfo":[...], "FaceInfo":[...], "PlateInfo":[...]},  // [HA] some firmwares only
  "ftp_check_update": {...}, "http_check_update": {...}   // cloud/FTP firmware-update notices (UI pops a dialog)
}}
```
* Keys seen in the UI: `alarm_list[].channel_alarm[].{channel, channel_name, channel_alias, camera_connect_status, record_flag, videoloss, int_alarm, Floodlight_AudioAlarm, ptz_alarm}`, `alarm_list[].{talkback_alarm, system_alarm[].hdd.hdd_alarm_type, alarm_out_state}`, top-level `videoloss_status, hdmi2_enable, heat_alarm, reader_id, sequence, lap_number, ftp_check_update, http_check_update`. [UI H]
* `motion_alarm`, `pir_alarm`, `io_alarm` booleans and the `int_subtype` values come from the HA client's reading of "API spec Get&Push Table 5/10" [HA, M]: `int_subtype` ∈ `pd, vd, pd_vd, fd, lcd, pid, sod, lpd, rsd, intrusion, region_entrance, region_exiting, sound, ...`.
* `record_flag` letters drive the live-view OSD icons (live.js `ft()`); value `"R"` = red/active, other non-empty value = green/enabled. Letter meanings [GUESS]: `m` motion, `i` I/O alarm, `s` smart/AI, `p` PIR, `r` recording, `h` ?, `t` ?. **`record_flag.m == "R"` is a state-style motion indicator** (level, not edge); see §3.3.
* Channel strings: "CH1".."CHn" (1-based); AI database APIs use a 0-based `Chn` int [HA, H].

### 3.2 Other event paths
* **EventPush (HTTP push from the NVR to a server)**: `AlarmConfig/EventPush/Get|Set` exists in this UI (`alarm_event_push`) [UI H]. The HA client configures it to POST JSON to an HA webhook with body `{"params":{"name":"HA","table":{"addr":..,"port":8123,"url":"/api/webhook/<id>","enable":true,"method":"POST","auth_enable":false,"keep_alive_interval":"0","push_way":"HTTP"}}}` [HA, M — this shape is suspicious: the UI-style body would be `{"version":"1.0","data":{...}}`; check the Get shape first]. Payload = same `alarm_list`/`ai_snap_picture` structure. Optional; Event/Check is simpler and needs no inbound connectivity.
* **AI object database polling**: `POST /API/AI/SnapedObjects/Search` then `/API/AI/SnapedObjects/GetByIndex` (UI builds them in core.js; `Engine:1` in UI, `Engine:0` in HA). Used by HA for person/vehicle snapshots when Event/Check lacks `ai_snap_picture`. Latency = poll interval (HA: 10 s). [UI H paths; HA M]
* `AI/processAlarm/Get` (`aiAlarmReport`) — listed but no caller in the downloaded chunks [UI H path, purpose L].
* Older-API `/action/eventManage` subscribe [DOC] — not this firmware.

### 3.3 Recommended HA binary sensors
| Sensor | Source | Off logic |
|---|---|---|
| Motion per channel | `channel_alarm[].motion_alarm` (edge) or `record_flag.m=="R"` (level) | level if `record_flag` is present; else auto-off 10–30 s after the last true |
| Person/Vehicle/LCD/PID/SOD | `int_alarm{alarm_val,int_subtype}` | `alarm_val:false` if sent; else timeout |
| Camera online | `camera_connect_status.connect_status` + ChannelInfo poll | state |
| Video loss | `videoloss` | state |
| HDD fault | `system_alarm[].hdd.hdd_alarm_type != "Ok"` | state |

## 4. Cloud-related settings ("Swann cloud" master switch)

General pattern (remoteSetting.js) [UI H]: the page calls `<Path>/Range` + `<Path>/Get` (`initParamPageData`). Save = `<Path>/Set` with the **whole `data` object from Get**, edited, wrapped as `{"version":"1.0","data":{...}}`. So the safe write is **read-modify-write**: Get → change one key → Set the full object. Exceptions are noted below.
**Password fields**: pages that hold credentials (DDNS, Email, FTP, Server/Cloud) send passwords encrypted (`base_enc_password{seq,peer_key,cipher}` via TransKey). Get returns `password_empty` and no password. When you write these back, **delete any `password` key** and leave out `base_enc_password` so the stored secret is not touched [UI H for mechanism; GUESS that omitting keeps it].

| # | Feature | Path | Key(s) to turn off | Confidence |
|---|---|---|---|---|
| 1 | P2P (Swann Security app, remote access through RaySharp/Swann P2P servers) | `NetworkConfig/P2P` | Set body is **only** `{"p2p_switch": false}` (the UI sends nothing else) | UI H |
| 1b | Same switch on the port page | `NetworkConfig/NetBase` | `p2p_switch` (also `APR_switch` = UPnP/auto port forward) inside NetBase Set | UI H key; read-modify-write |
| 2 | Cloud recording (Dropbox / Google Drive) | `StorageConfig/Cloud` | `cloud_storage: false` (other fields: `cloud_type`, `cloud_over_write`, `video_type`, `cloud_status`, `channel`/per-channel `folder_name`, `stream_type`) | UI H keys |
| 2b | Upload server (FTP/SFTP/AWS S3/Dropbox/Google Drive/**Swann Cloud**) | `StorageConfig/Server` (`net_server`) | `enc_mode` selects the active backend; each `<x>_info.enable` (`ftp_info, sftp_info, aws_info, dropbox_info, google_drive_info, swann_cloud_info`) → false | UI H keys |
| 3 | Cloud upload schedule | `Schedules/Cloud` | schedule grid (shape TODO; same as other Schedules/*: per channel, 7×48 or 7×24 grid [GUESS]). Turning off #2/#4 is enough; leave this alone | UI path H, shape L |
| 4 | Per-alarm cloud actions | `AlarmConfig/Motion`, `AlarmConfig/PIR`, `AlarmConfig/IO`, `AlarmConfig/Intelligent/*`, `AI/Setup/*` with `page_type:"AlarmConfig"` | `channel_info.CHn.picture_to_cloud`, `video_to_cloud` (+ `picture_to_dropbox`, `video_to_dropbox` on Motion). App push = `push` | UI H keys |
| 4b | Combination/AI policy pages | `AlarmConfig/Combination`, some AI pages | `group[i].PolicyConfigs[0].UploadToCloud` = **byte array bitmask**, bit `(ch-1)%8` of byte `floor((ch-1)/8)` | UI H |
| 5 | **App push notifications** (Swann Security push via P2P cloud) | same alarm pages as #4 | `channel_info.CHn.push: false` | UI H key; that it goes through the cloud = M |
| 6 | Email alerts | `NetworkConfig/Email`; per-alarm `send_email` | `enable` in Email [L]; per-alarm `send_email` | UI H path, keys partly L |
| 7 | DDNS (Swann DDNS) | `NetworkConfig/DDNS` | `ddns_enable: false` (others: `server, domain, username, use_login, iptype, url, email, service_id`) | UI H |
| 8 | Firmware cloud upgrade / auto-upgrade | `Maintenance/CloudUpgrade` (check/start), `Maintenance/AutoUpgrade` | AutoUpgrade enable key TODO (page shows `device_type`, `firm_version`); Event/Check carries `ftp_check_update`/`http_check_update` when the device found an update | UI H paths, keys L |
| 9 | Smart-home cloud | `NetworkConfig/Tuya` (`enable`), `NetworkConfig/SMARTHOME` (Alexa/Google: `UserName`, `ScreenStream`) | `enable: false` | UI H |
| 10 | RemoteReport | `RemoteReport` | No caller in the downloaded chunks; probably a log/diagnostic upload to RaySharp. Not worth touching | UI path H, meaning L |
| 11 | RSP2P log upload | `RSP2PLog`, `RSP2PLog/Token` (`rsp2p_log_enable`) | diagnostics; leave alone | UI H path |
| 12 | MQTT / GB28181 / SNMP | `NetworkConfig/MqttServerInfo`, `T28181`, `Snmp` | not Swann cloud | UI H path |

Not present in this UI: `NetworkConfig/CloudService` (the HA client polls it; likely 404 here), `AlarmConfig/Disarming` (the HA client polls it; not in this firmware's URI table). [UI H by absence]

**Master switch algorithm** (recommended):
1. For each feature: `Range` (does it exist? if not, skip; the menu `Login/DevicePage` also tells which pages exist [UI H, shape L]), then `Get`, and store a snapshot of only the keys you change (`{"NetworkConfig/P2P": {"p2p_switch": true}, "AlarmConfig/Motion": {"CH1": {"push":true,...}}}`) in HA storage.
2. Set off: P2P (#1), cloud storage (#2/#2b), per-channel `push`/`picture_to_cloud`/`video_to_cloud` on every alarm page (#4/#4b/#5), DDNS (#7), Tuya/SmartHome (#9), auto-upgrade (#8).
3. Restore = Get the current values and write back the stored keys.
4. State of the switch = OR over the stored keys (off only if all are off).
Effect: the Swann Security app can't connect (P2P off) and gets no push; nothing is uploaded; local RTSP/HTTP/ONVIF still work. **Warning**: P2P off removes remote access unless the user has a VPN.

## 5. Per-channel detection settings (toggle targets)

### 5.1 Motion detection enable — `AlarmConfig/Motion` [UI H]
Same resource, two views selected by `page_type`:
* `{"page_type":"ChannelConfig"}` → detection settings (chn_motion page): `channel_info.CHn.{switch, sensitivity, smart_moiton_detection (sic), target_type, AiSensitivity, multiple_switch, distance, intervals, light_linkage, ...}`. **`switch` = motion detection on/off.**
* `{"page_type":"AlarmConfig"}` → actions (alarm_motion page): `channel_info.CHn.{record_enable, post_recording, buzzer, latch_time, show_message, full_screen, send_email, push, picture_to_cloud, video_to_cloud, ftp_picture_upload, ftp_video_upload, light_linkage, siren_linkage, http_listening, alarm_out ...}`.
* Get: `POST /API/AlarmConfig/Motion/Get` `{"version":"1.0","data":{"page_type":"ChannelConfig"}}`. Without page_type → `param_error` on V8.2 [HA H].
* Set: the UI sends the **whole object** it got back (`channel_info` for all channels) plus `page_type`. Range gives the per-field enum/limits and `support_copy`; `copy_ch` groups channels. Fixtures `alarm_motion_get_channelconfig.json`, `alarm_motion_get_alarmconfig.json`.
* Sending only one channel inside `channel_info` probably works (the UI's `supportDataFilter` sometimes drops fields) [GUESS]. Do the full read-modify-write.

### 5.2 AI detection (person/vehicle, line crossing, intrusion, …) [UI H paths]
* NVR-side AI: `AI/Setup/PVD` (person/vehicle), `AI/Setup/PID` (intrusion), `AI/Setup/LCD` (line crossing), `AI/Setup/SOD`, `AI/Setup/FD`, `AI/Setup/CrossCount`, `AI/Setup/LPD`, `AI/Setup/RSD`; schedule `AI/Setup/AISchedule`, `Schedules/AI`.
* Camera-side "Intelligent": `AlarmConfig/Intelligent/{PID,LCD,SOD,PD,FD,CC,SoundDetection,OcclusionDetection}`.
* Same pattern: `page_type:"ChannelConfig"` → `channel_info.CHn.switch` (+ `rule_info`, `schedule`, `type_vehicle`, `category`...); `page_type:"AlarmConfig"` → actions (same keys as §5.1, rendered by alarm_intelligent.vue). [UI H keys `switch`, `rule_info`, `type_vehicle`]
* Which ones exist on the NVW-MR4K depends on the cameras. Probe with `Range`; a 404/`not_found` means unsupported.

### 5.3 Deterrence (siren / white light) [UI H paths]
* Config: `AlarmConfig/Deterrence` (`chn_floodlight`), schedule `Schedules/Deterrence`, siren `AlarmConfig/VoiceAlarm` (`chn_siren`), voice prompts `AlarmConfig/VoicePrompts`.
* Live manual trigger (live.js `Ae`/`Te`/`Me`, URI `preview_Floodlight2AudioAlarm`) [UI H]:
  * Get: `POST /API/PreviewChannel/Floodlight2AudioAlarm/Get` `{"version":"1.0","data":{"channel":"CH1"}}` → `{floodlight_switch, audioAlarm_switch, intelligent_switch, redBlueLight_switch, floodlight_value, floodlight_value_range, floodlight_value_adjustable, audioAlarm_value, audioAlarm_value_range, audioAlarm_value_adjustable, ...}`.
  * Set: `POST /API/PreviewChannel/Floodlight2AudioAlarm/Set` with the **whole Get object sent back** (ranges included), plus `channel` ("CH1") and `operation_type` ∈ `"Floodlight"|"AudioAlarm"|"RedBlueLight"|"IntelligentSwitch"` (the control changed; note `IntelligentSwitch`, not `Intelligent`). `Te()` only overwrites keys already present (`null != H.x`): the four `*_switch` booleans and `floodlight_value` / `audioAlarm_value`. Debounced 100 ms in the UI. Example: `{"channel":"CH1","operation_type":"Floodlight","floodlight_switch":true,"floodlight_value":80,"audioAlarm_switch":false,"audioAlarm_value":5,...}`.
  * Meaning: `floodlight_value` = brightness, real range 1–100 (value 50) [UI H, device H for range]. `audioAlarm_value` = siren level, real range 1–10 (value 5); ENU labels it "Siren level", so treated as volume [M]. No duration in the live trigger (duration lives in `AlarmConfig/VoiceAlarm`) [UI H].
  * Only offered when the channel `ability` has `Floodlight` / `AudioAlarm`. `Me()` also turns the siren off while `isDualtalkWorking`.
  * Live state: Event/Check `channel_alarm[].Floodlight_AudioAlarm.{floodlight_switch,floodlight_value,floodlight_value_range,audioAlarm_switch,audioAlarm_value,audioAlarm_value_range}` [device H, seen on the NVW-MR4K idle snapshot].
  * Integration (light + siren entities): Get, change switch/value, add `channel` + `operation_type`, Set. **The Set has not been tried on real hardware** [UI H for the body, device unverified].
* Manual alarm out: `PreviewChannel/ManualAlarm/Get|Set`.

### 5.4 Doorbell [UI H for absence]
* The Swann UI (webui-js) has **no Event/Check key for a doorbell press**. ENU.js only has labels: `doorbell:"Doorbell"`, motion mode `Pir_Vehicle_Person_Doorbell`, log types `DoorbellStart`/`DoorbellEnd`. live.js `bt()` reads `talkback_alarm` only to close a talk session (`talkback_close`, `error_code`).
* raysharp-ha (third party, unverified) parses `channel_alarm[].doorbell_call` (true = ring start, false = end) "for RaySharp video-doorbell firmware" and treats `talkback_alarm[]` as an intercom call [HA, L for Swann].
* Integration: nothing doorbell-specific. Any unmodelled non-idle key in a `channel_alarm` entry (e.g. `doorbell_call: true`) is kept in diagnostics `recent_events`, so one press plus a diagnostics download will show the real key.

### 5.5 Two-way talk [UI H]
* The UI talks through the **player plugin only**: `F.Talkback(z.SubMsgStartTalkToDevice, {talk_mode: ...})`, main message `MainMsgDualTalk` (101), and only when `f.browser.msie` (the ActiveX/NPAPI plugin). Audio goes over the proprietary media protocol on `media_port` 9000 (same channel as playback).
* No HTTP talk API in the URI table, no WebSocket anywhere, no RTSP backchannel (no `ONVIF backchannel`/`sendonly` in the JS; untested on the RTSP server [GUESS]).
* Abilities: `TalkHalf`, `TalkFull`, `DualTalk` (the MR4K cameras list `TalkHalf`, `TalkFull`).
* raysharp-ha's `/API/AccessSystem/Intercom/CallSetup/Set` is SIP intercom setup on access-control devices, not camera talk.
* So HA two-way talk would need the port-9000 media protocol reverse-engineered (or an RTSP backchannel test with go2rtc). Not implemented.
* 0.1.8: diagnostics `rtsp_probe` sends DESCRIBE to awake channels' sub stream with and without `Require: www.onvif.org/ver20/backchannel` and reports whether a sendonly audio track appears (`backchannel_offered`). Result on the NVW-MR4K: pending a diagnostics file taken while a camera is awake.

## 6. Version differences and detection
* Request `version` is always `"1.0"` in this UI (`apiVersion.version`); it is not a firmware version. [UI H]
* Firmware/UI version: `SystemInfo/Base/Get` → `firmware_version`, `web_version` etc. [M]; the UI hardcodes `version_web 1.2.1.83`, `version_ocx 1.0.0.73`; `Login/Range.site_version`.
* Feature detection = **`/Range` responses and `Login/DevicePage` (menu tree)**; RaySharp firmwares differ per OEM (`custom_name`), not by an API version number. Channel `ability[]` lists per-camera features.
* Differences seen vs the HA client's V8.2.x: V8.2 has `AlarmConfig/Disarming`, `NetworkConfig/CloudService`, Event/Check that answers immediately, `ai_snap_picture` missing from events. This UI has `StorageConfig/Server` with `swann_cloud_info`, `Schedules/Cloud`. Paths like `/API/System/General/Get`, `/API/Stream/Rtsp Url/Get` in the HA client are leftovers that do not match this UI (here `SystemConfig/General`).
* Older RaySharp firmware (2017–2019 docs) used `/action/...` with Basic auth: not applicable. [DOC]

## 8. Waking sleeping battery cameras

Seen on the user's NVW-MR4K (V8.2.2): battery Wi-Fi cameras report `connect_status: "Sleep"` in `Login/ChannelInfo` and in Event/Check `camera_connect_status`, and the channel `ability` list includes `"NotAutoReconnect"` [device, H].

* **There is no HTTP wake call in this UI.** No path in the URI table (`core.js`) mentions wake, sleep, battery or keep-alive; `PreviewChannel/*` covers PTZ, manual alarm, Floodlight2AudioAlarm, zero channel and ControlBtnOper only [UI, H].
* **The UI wakes a camera by playing it.** `core.js` enum `ChnConnctStatusEnum` has `SLEEP:"Sleep"` and `isSleep(ch)`. Live view treats Sleep like Online: the play button is enabled when `!isOnline && !isSleep` is false (`live.js`), and "open all" calls `play(ch)` for every channel that `isOnline(ch) || isSleep(ch)` (`core.js`) [UI, H]. Playback goes over the proprietary media channel (`media_port` 9000, id/key from login) [UI, H], so opening any stream is what wakes the camera [GUESS/M].
* **`getAutoConn()`** builds a per-channel 0/1 list (0 for `NotAutoReconnect`) passed to the player plugin: the plugin does not reconnect to such channels by itself, i.e. it does not keep battery cameras awake [UI H for the code, meaning M].
* **RTSP wakes it too** [device, H]: `ffprobe -rtsp_transport tcp` on `rtsp://NVR:554/rtsp/streaming?channel=01&subtype=0` returned stream info for a sleeping camera, after a few seconds. go2rtc and HA's stream worker time out (~5 s) first.
* **Integration (0.1.5):** to wake, open RTSP on the sub stream (OPTIONS, DESCRIBE with Digest, SETUP interleaved TCP, PLAY), discard data, wait until Event/Check (or a ChannelInfo poll every 2 s) reports `Online` (timeout 20 s), keep the wake stream open 10 s more, then TEARDOWN. Only done when the frontend opens live view (WebRTC offer or HLS stream), never for snapshots. Whether DESCRIBE alone would wake the camera, and how long it stays awake after the stream closes (Motion config `SleepDuration: 5`, unit unknown), are unverified [GUESS].

## 7. Open items (need one live check, read-only)
1. Real shapes of `SystemInfo/Base/Get`, `Login/ChannelInfo/Get`, `Preview/StreamUrl/Get` (fixtures are modelled).
2. Event/Check: does it hold the request? Which keys appear for motion vs `int_alarm` on this NVR?
3. Session idle timeout and max sessions; whether a second login by the same user kicks the first (`login_at_other`).
4. `Snapshot` endpoint: path is in the URI table (`/API/Snapshot`), but there is no caller or body in the downloaded chunks (HA client uses `/API/Snapshot/Get`). Use RTSP frames as the fallback.
5. `Schedules/Cloud`, `Maintenance/AutoUpgrade` keys.

## Appendix A. Existing integration `11058/OEM-RaySharp-NVR-Ai` (cloned to research/raysharp-ha, HEAD 07cbe54, 2026-09-14, 26 commits, one author)
* **License: the README says MIT and links to `LICENSE`, but the repo has no LICENSE file.** Legally unclear: do not copy code. Use it as a reference only, or ask the author to add the file.
* Client: aiohttp + `DigestAuthMiddleware`, `X-csrftoken` + cookie jar, re-login on 400/401 session errors. Event/Check is polled from the coordinator with `reader_id` cursor, 2 s sleep, 15 s timeout. It also has an HTTP "push/webhook" receiver (alarm server → HA) and parses many guessed alarm-type aliases (`NVR_ALARM_TYPE_MAP`).
* Entities: camera (RTSP + snapshot), binary_sensor per channel × type, switches, buttons, event, image (AI snapshots), disk sensors, talkback (websocket + bundled JS).
* Quality: works for its author's V8.2.4.1 device. But: Russian-only docs/comments, large monolithic files (`__init__.py` parsing, coordinator about 830 lines), several endpoints that do not exist in this firmware (`AlarmConfig/Disarming`, `NetworkConfig/CloudService`, `System/General`, `Stream/Rtsp Url`), alias maps built by guessing, no tests, no cloud/privacy features.
* The HA community thread "Swann DVR CCTV systems and HA" was not needed. The UI JS answered the protocol questions.
