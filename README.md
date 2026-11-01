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
RTSP transport for Home Assistant's stream worker (TCP by default; try UDP if
live view times out), polling interval (seconds), and *Also manage DDNS and cloud upload servers
(experimental)*, off by default (see below).

## Features

One device for the NVR, plus one device per camera channel (linked via the NVR).

- **Camera** per channel, streamed from
  `rtsp://<user>:<pass>@<host>:554/rtsp/streaming?channel=NN&subtype=S`.
- **Binary sensors** per channel: motion, person, vehicle.
- **Switch** per channel: motion detection on/off.
- **Switch** "Swann cloud" on the NVR (see below).

## The "Swann cloud" switch

*On* means the NVR's cloud features are allowed.

**What it changes by default** (only on pages your NVR supports):

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
- **Live view times out:** set *RTSP transport* to UDP in the options.

## Development

```sh
nix develop          # python 3.14 + uv + ruff; creates .venv on first run
pytest
ruff check . && ruff format .
python script/gen_translations.py   # after editing strings.json
```

## License

MIT
