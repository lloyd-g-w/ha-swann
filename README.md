# Swann NVR for Home Assistant

Custom integration for Swann NVRs running RaySharp firmware (developed against
an NVW-MR4K). It talks to the NVR's local HTTP JSON API and its RTSP server;
no Swann cloud account is involved.

> **Status: early scaffold.** The HTTP API client (`custom_components/swann/api.py`)
> is not implemented yet, so the integration cannot connect to a real NVR.

## Installation (HACS)

1. HACS → ⋮ → *Custom repositories* → add this repository's URL, category
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

**Options:** camera stream (main = high resolution, sub = low resolution) and
polling interval (seconds).

## Features

One device for the NVR, plus one device per camera channel (linked via the NVR).

- **Camera** per channel, streamed from
  `rtsp://<user>:<pass>@<host>:554/rtsp/streaming?channel=NN&subtype=S`.
- **Binary sensors** per channel: motion, person, vehicle.
- **Switch** per channel: motion detection on/off.
- **Switch** "Swann cloud" on the NVR (see below).

## The "Swann cloud" switch

*On* means the NVR's cloud features are allowed.

- **Turning it off** reads the NVR's current cloud-related settings, saves them
  in Home Assistant's storage (`.storage/swann.<entry_id>.cloud`), then sets all
  of them to off on the NVR.
- **Turning it on** writes the saved settings back and deletes the snapshot.
  If there is no snapshot (e.g. cloud was disabled outside Home Assistant), it
  refuses rather than guess; re-enable from the NVR UI or Swann app.
- The snapshot survives restarts and reloads; it is deleted when the
  integration entry is removed.

**Limits:** this only changes settings on the NVR. It does not firewall the
device, and it cannot guarantee that the firmware stops all outbound traffic
(e.g. NTP, DNS, update checks or anything the firmware does regardless of its
settings). If you need a guarantee, block the NVR's internet access on your
router/firewall.

## Development

```sh
nix develop          # python 3.14 + uv + ruff; creates .venv on first run
pytest
ruff check . && ruff format .
python script/gen_translations.py   # after editing strings.json
```

## License

MIT
