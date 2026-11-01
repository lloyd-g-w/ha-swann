# Swann NVR for Home Assistant

Custom integration for Swann NVRs running RaySharp firmware (developed against
an NVW-MR4K). It talks to the NVR's local HTTP JSON API and its RTSP server;
no Swann cloud account is involved.

> **Status: untested on real hardware.** The API client was written from the
> NVR's web UI code and tested against a mock NVR only. See
> [First test on your NVR](#first-test-on-your-nvr).

## Installation (HACS)

1. HACS → ⋮ → *Custom repositories* → add `https://github.com/lloyd-g-w/ha-swann`, category
   *Integration*.
2. Install **Swann NVR**, then restart Home Assistant.

Manual: copy `custom_components/swann` into `<config>/custom_components/`.

Requires Home Assistant 2026.10 or newer.

## Setup

*Settings → Devices & services → Add integration → Swann NVR*, then enter:

| Field      | Default | Notes                                                  |
|------------|---------|--------------------------------------------------------|
| Host       |         | IP or hostname of the NVR                              |
| Port       | 443     | Web interface port                                     |
| Use HTTPS  | on      |                                                        |
| Verify SSL | off     | NVRs ship a self-signed certificate                    |
| Username / Password | | An NVR user; admin is needed for the switches     |

The NVR is identified by its serial number, so it can only be added once.

**Options:** camera stream (main = high resolution, sub = low resolution),
*Live view video* and *Use hardware acceleration for transcoding* (see
[Live view and H.265](#live-view-and-h265)), RTSP transport for Home
Assistant's stream worker (TCP by default; try UDP if live view times out),
polling interval (seconds), and *Also manage DDNS and cloud upload servers
(experimental)*, off by default (see below).

## Features

One device for the NVR, plus one device per paired camera (linked via the
NVR). Empty channel slots ("NotPaired") get no device; devices of cameras that
are unpaired later are removed on the next reload.

- **Camera** per channel, streamed from
  `rtsp://<user>:<pass>@<host>:554/rtsp/streaming?channel=NN&subtype=S`.
- **Binary sensors** per channel: motion, person, vehicle; **Awake**
  (diagnostic, see battery cameras below).
- **Alert** event entity per camera (`event.<camera>_alert`), event types
  `motion`, `person`, `vehicle`; see [Alerts and notifications](#alerts-and-notifications).
- **Floodlight** light (on/off, brightness) and **Siren** (on/off, volume =
  the camera's siren level 1–10), only on cameras that have them. They replace
  the read-only floodlight/siren binary sensors of 0.1.4, which are removed.
- **Motion detection**, depending on firmware: a **switch** (on/off) where the
  NVR has a boolean motion switch, or a **"Motion detection mode" select**
  where it has a mode instead (NVW-MR4K: e.g. `PIR_and_Person_or_Vehicle`).
  The select offers the modes the NVR lists; if it lists none, it only shows
  the current mode. There is deliberately no "off" for those cameras.
- **Switch** "Swann cloud" on the NVR (see below), and a diagnostic
  **"Cloud connection"** sensor (the NVR's own cloud link state, e.g.
  `Connected`) to check whether the cloud is really cut off.

The floodlight and siren use the same call as the NVR's web live view
(`PreviewChannel/Floodlight2AudioAlarm/Set`). It is taken from the web UI code
and not yet confirmed on every firmware; if a command fails, please open an
issue with a diagnostics download.

### Alerts and notifications

Each camera has an **Alert** event entity. It fires `motion`, `person` or
`vehicle` when the NVR reports a new detection (at most once per type and
camera every 30 s, so one walk-past is one alert). Each alert is also sent on
the event bus as `swann_alert` with `device_id`, `entity_id`, `channel`,
`name` and `type`, for your own automations.

**Phone notifications with a snapshot** (blueprint):

1. Install the **Home Assistant companion app** on your phone (App Store or
   Google Play), sign in to your Home Assistant, and allow notifications
   (iOS: also allow *Time Sensitive* notifications). The phone then shows up
   as a device of the *Mobile App* integration.
2. Import the blueprint:

   [![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Flloyd-g-w%2Fha-swann%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fswann%2Fnotify_alert.yaml)

   or *Settings → Automations & scenes → Blueprints → Import blueprint* with
   `https://github.com/lloyd-g-w/ha-swann/blob/main/blueprints/automation/swann/notify_alert.yaml`.
3. Create an automation from it: pick the Alert entities, the camera, your
   phone, the alert types (default person, vehicle, doorbell) and a cooldown
   (default 2 minutes). Optionally pick a media player plus a chime sound or a
   TTS engine to announce person/doorbell alerts at home.

The notification shows a snapshot (`/api/camera_proxy/<camera>`; the camera
is awake during an alert, so it is fresh) and opens the camera when tapped.
Doorbell alerts are sent as time-sensitive (iOS) / high priority (Android).

**Doorbell (TODO):** the NVR's web UI has no event for a doorbell press, so
the integration cannot report one yet and the Alert entity has no `doorbell`
type. Unknown event keys are kept in the diagnostics: press the doorbell,
then download diagnostics (*Settings → Devices → Swann NVR → Download
diagnostics*) and open an issue with the `recent_events` part.

Two-way talk is not supported: the NVR only offers it over its proprietary
media protocol (see `research/API.md` §5.5).

### Battery cameras

Battery Wi-Fi cameras (e.g. the NVW-MR4K kit) sleep between events and report
`Sleep`; they count as available, and **Awake** shows whether one is awake
right now.

- **Live view wakes the camera.** When you open a sleeping camera's live view,
  the integration first opens its RTSP stream to wake it and waits until the
  NVR reports it awake (up to 20 s), then starts the player. Expect a few
  seconds' wait before video appears; several viewers share one wake-up.
- **Snapshots never wake a camera.** While it sleeps you see the last frame
  taken while it was awake (or none yet); while awake, a fresh frame at most
  every 10 s.
- Streams are never preloaded. Leave *Preload stream* off in the camera's
  settings: with it on, HA wakes every camera at startup.

### Live view and H.265

Swann cameras stream H.265 video with G.711 audio, which most browsers can't
play over WebRTC, and the codec can't be changed on the camera. So by default
the integration has Home Assistant's built-in go2rtc **transcode live view to
H.264 video and Opus audio**: live view then plays in any browser, with sound.

*Live view video* option:

| Value | Effect |
|---|---|
| Automatic (default) | Transcode H.265 cameras (currently: always, since Swann cameras are H.265) |
| Always transcode to H.264 | Transcode |
| Original stream | Pass the camera's H.265 through; only plays where the browser can decode H.265 (e.g. Safari, the iOS app) |

- **CPU is used only while someone is watching**: one ffmpeg process per
  open camera, stopped when the last viewer closes it.
- **Use the sub stream** (*Camera stream: sub*) with transcoding: 1280×720
  costs a fraction of the CPU of the 1080p main stream and looks fine on a
  dashboard or phone.
- **Hardware acceleration** (*Use hardware acceleration for transcoding*, off
  by default) has ffmpeg use the GPU (VAAPI on Intel/AMD). The container needs
  the GPU passed through, e.g. in the compose file:
  ```yaml
      devices:
        - /dev/dri:/dev/dri
  ```
  If no usable GPU is found, go2rtc's ffmpeg may fail to start; turn it off.
- Transcoding applies to WebRTC (go2rtc) live view only. The fallback HLS
  player and snapshots still use the camera's original RTSP stream.
- If you set a camera **orientation** in its settings, Home Assistant adds its
  own H.264 conversion on top; that still works but is untested.

## The "Swann cloud" switch

*On* means the NVR's cloud features are allowed.

**What it changes by default** (only on pages your NVR supports). On the
NVW-MR4K (firmware V8.2.2) the P2P page does not exist; its web UI keeps the
P2P switch on another settings page, which the integration does not touch yet,
so there the switch mainly turns off push notifications and cloud storage.

- **P2P** (`NetworkConfig/P2P`): the connection the Swann Security app uses.
  **Turning this off disables the Swann Security app away from home**, including
  its remote live view and push notifications. Use Home Assistant's own remote
  access (Nabu Casa, or a VPN) instead.
- **Cloud storage** (`StorageConfig/Cloud`).
- **Per-camera alarm actions**: push notifications and picture/video upload to
  the cloud (and Dropbox) on the motion, PIR, and AI detection pages. Recording
  and motion detection themselves are not touched.
- **Automatic firmware upgrade** (`Maintenance/AutoUpgrade`), if present.

**How it works:**

- **Turning it off** reads the current values, saves them in Home Assistant's
  storage (`.storage/swann.<entry_id>.cloud`), then turns them off. Each page is
  read in full and written back with only those keys changed.
- **Turning it on** writes the saved values back and deletes the snapshot. With
  no snapshot (e.g. cloud was disabled outside Home Assistant), it refuses rather
  than guess; re-enable from the NVR UI or Swann app.
- The snapshot survives restarts and reloads and is deleted when the
  integration entry is removed.

**Experimental: DDNS and cloud upload servers.** With the option *Also manage
DDNS and cloud upload servers* on, the switch also turns off DDNS
(`NetworkConfig/DDNS`) and the Swann cloud/Dropbox/Google Drive/AWS upload
servers (`StorageConfig/Server`; your own FTP/SFTP servers are never touched).
**Risk:** the integration never sends passwords back to the NVR, and it is not
yet known whether the NVR keeps the stored DDNS/upload-server password when a
setting is written without it. It may clear it, and you would have to re-enter
it. With the option off, these pages are not read or written at all. If you
turn the cloud off with the option on and later disable the option, turning the
cloud back on will not restore DDNS or the upload servers (a warning is logged).

**Limits:** this only changes settings on the NVR. It does not firewall the
device, and **it cannot guarantee that the firmware sends nothing else** to
Swann or other servers (NTP, DNS, update checks, or anything the firmware does
regardless of its settings). For a guarantee, block the NVR's internet access
on your router.

## First test on your NVR

1. **Create a dedicated NVR user** for Home Assistant (NVR web UI → user
   management), with admin rights if you want the switches. Don't reuse the
   account you log in with elsewhere: the NVR ends a session when the same user
   logs in somewhere else. Avoid repeated wrong passwords: the NVR locks the
   account for a while after several failed logins.
2. **Install:** copy `custom_components/swann` into `/config/custom_components/`
   (so you have `/config/custom_components/swann/manifest.json`), then restart
   Home Assistant.
3. **Enable debug logging** in `configuration.yaml`, then restart:

   ```yaml
   logger:
     default: warning
     logs:
       custom_components.swann: debug
   ```

4. Add the integration (*Settings → Devices & services → Add integration →
   Swann NVR*), check the cameras and sensors, walk in front of a camera, and
   only then try the Swann cloud switch.
5. **Send back** the lines from *Settings → System → Logs → Show raw logs*
   containing `custom_components.swann` (or `docker logs homeassistant 2>&1 |
   grep swann`), especially:
   - `Logged in to …`, `Session expired …`, any `SwannAuthError` / `login_*`;
   - `… not supported: …` (which settings pages your firmware lacks);
   - `SystemInfo/Base unavailable`, `Motion detection config unavailable`,
     `Event check failed`, `Event cursor reset`;
   - any traceback or warning mentioning `swann`.

   Also say whether the device name, model, serial and channel names look right,
   and how quickly motion/person sensors react. Check the logs for passwords
   before posting them anywhere public.

## Troubleshooting

- **Download diagnostics:** *Settings → Devices & services → Swann NVR → ⋮ →
  Download diagnostics*. The file holds the integration's state plus fresh raw
  responses from the NVR (device info, channels, motion config, one event
  check, which cloud pages exist), with passwords, serials, MAC/IP addresses
  and IDs redacted. It works even if setup keeps failing. Attach it to an
  issue.
- **Person/vehicle detection not working:** walk past a camera, then download
  diagnostics. Its `recent_events` section holds the last 20 raw event entries
  the NVR sent (with debug logging on, each change is also logged as
  `Event/Check CHn: {...}`). Please attach it: these payloads have not been
  verified on real firmware yet.
- **Live view shows nothing (black player):** the camera's H.265 isn't being
  transcoded. Check *Live view video* isn't set to *Original stream*, and that
  the log has no go2rtc/ffmpeg errors for that camera (see
  [Live view and H.265](#live-view-and-h265)). Turn hardware acceleration off
  if it's on.
- **Live view times out:** set *RTSP transport* to UDP in the options.
- **Two-way talk (doorbell microphone):** not supported yet. To check whether
  your NVR could support it, open a camera's live view, wait until it plays,
  and download diagnostics within 30 seconds. The `rtsp_probe` section asks
  the NVR's RTSP server (read-only: OPTIONS and DESCRIBE, no streaming) whether
  it offers an ONVIF audio backchannel; `backchannel_offered: true` means
  talk-back over RTSP looks possible. Only cameras that are already awake are
  probed, so downloading diagnostics never wakes a battery camera.

## Development

```sh
nix develop          # python 3.14 + uv + ruff; creates .venv on first run
pytest
ruff check . && ruff format .
python script/gen_translations.py   # after editing strings.json
```

## License

MIT
