# Mock-NVR fixtures (Swann NVW-MR4K, web UI 1.2.1.83)

Conventions:
* Files named `*_request.json` are the request bodies a client should send. All others are response bodies.
* `_status` (HTTP status, default 200) and `_headers` (response headers) are meta keys for the mock server. Strip them before sending the body.
* All requests are `POST /API/<Path>` with `{"version":"1.0","data":{...}}`. Every response is wrapped as `{"result":"success"|"failed", "data":{...}, "error_code"?}`.

**None of these were captured from the device.** Key names come from the web UI JS (see API.md, tags [UI]). Values and nesting not seen in the JS are modelled.

| File | Endpoint | Key names | Shape/values |
|---|---|---|---|
| login_range.json | Login/Range | H (core.js) | values L |
| web_login_401_challenge.json, web_login.json | Web/Login (digest) | H (token in `X-csrftoken` header; data keys from login.js) | values L |
| web_login_error_*.json | Web/Login | H (error codes, `block_remain_time`) | `remain_login_count` L |
| web_logout.json, heartbeat.json, set_success.json | Web/Logout, Login/Heartbeat, any Set | M | data `{}` L |
| error_*.json | any | H (codes from ENU.js) | status codes M (HA client says 400) |
| systeminfo_base.json | SystemInfo/Base/Get | M (HA client) | L |
| login_deviceinfo.json | Login/DeviceInfo/Get | partly H (`videoloss_status`,`nocamera_display`,`hdmi2_enable`) | rest L |
| login_channelinfo.json | Login/ChannelInfo/Get | H (`channel_param.items[]`, `channel_name`, `channel_alias`), M (`connect_status`,`ability`) | `model` L |
| preview_streamurl.json | Preview/StreamUrl/Get | L | URLs match the confirmed RTSP format |
| event_check_*.json | Event/Check | H (`reader_id,sequence,lap_number,heat_alarm,alarm_list[].channel_alarm[]`, `int_alarm.alarm_val/int_subtype`, `record_flag`, `camera_connect_status`, `videoloss`, error codes) | `motion_alarm` M (HA client), values L |
| networkconfig_p2p_*.json | NetworkConfig/P2P | H (`p2p_switch,p2p_id,app_url_*`; Set sends only `p2p_switch`) | range format L |
| networkconfig_netbase_get.json | NetworkConfig/NetBase | H (`p2p_switch`,`APR_switch`) | `port{}` L |
| storageconfig_cloud_get.json | StorageConfig/Cloud | H (`cloud_storage,cloud_type,cloud_over_write,cloud_status`, per-chn `folder_name`) | nesting L |
| storageconfig_server_get.json | StorageConfig/Server | H (`enc_mode`, `*_info`, `.enable`, `password_empty`) | L |
| networkconfig_ddns_get.json | NetworkConfig/DDNS | H | values L |
| networkconfig_tuya_get.json | NetworkConfig/Tuya | H (`enable`) | |
| networkconfig_email_get.json | NetworkConfig/Email | L | L |
| maintenance_autoupgrade_get.json | Maintenance/AutoUpgrade | `auto_upgrade` L, others H | L |
| schedules_cloud_get.json | Schedules/Cloud | L | L |
| alarm_motion_get_alarmconfig.json | AlarmConfig/Motion (`page_type:AlarmConfig`) | H (action keys from alarm_motion.vue) | value types L |
| alarm_motion_get_channelconfig.json / _set_request | AlarmConfig/Motion (`page_type:ChannelConfig`) | H (`switch,sensitivity,smart_moiton_detection,target_type,intervals,copy_ch`) | values L |
| ai_setup_pvd_get_channelconfig.json | AI/Setup/PVD | `switch,rule_info,type_vehicle` H, `type_human` L | L |
| alarm_combination_get.json | AlarmConfig/Combination | H (`group[].PolicyConfigs[0].UploadToCloud` bitmask bytes) | L |
| preview_floodlight2audioalarm_*.json | PreviewChannel/Floodlight2AudioAlarm | H (live.js) | values L |

## Real device: NVW-MR4K, firmware V8.2.2-20250718 (tests/fixtures/real_mr4k_*)

Captured (redacted) from the user's diagnostics download, numeric region
arrays shortened to 4 entries: `login_deviceinfo`, `systeminfo_base`,
`login_channelinfo`, `alarm_motion_get_channelconfig`, `event_check_initial`
(idle). **Modelled, a guess:** `alarm_motion_range_channelconfig`: structure
from the web UI code (`channel_info.items.CHn.items.multiple_switch`), mode
values from its English strings (PIR_and_Motion, PIR_and_Person,
PIR_and_Person_or_Vehicle). All event payloads other than idle are modelled.
