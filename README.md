# CCTV MCP — Dahua NVR control & self-heal

Control, diagnose, and self-heal a Dahua NVR from Claude (or any MCP client).
Tested against the **Dahua DH-NVR4216-16P-HDS3** 16-channel PoE recorder but
works against any Dahua-protocol NVR. The server is **fleet-capable**: add
multiple sites to `sites.json` and switch between them via `NVR_SITE`.

> See `sites.json.example` for the full fleet config format.

## What it does

| Area | Tools |
|------|-------|
| Identify | `nvr_info`, `list_cameras`, `channel_names` |
| **Offline detection** | **`camera_status`** (RTSP probe through the NVR) |
| Link / addressing | `link_quality` (loss + off-scheme camera-IP flag) |
| Bandwidth | `bandwidth_audit`, `set_bitrate`, `optimize_bandwidth` |
| Audio | `audio_audit`, `enable_audio`, `enable_all_audio` |
| Network | `scan_network` (LAN ping sweep) |
| Visuals | `snapshot` (grab a live frame for vision-based naming) |
| One-shot | **`full_audit`** (everything in a single call) |
| Fixes | `set_channel_name`, `enable_audio`, `set_bitrate`, `reboot_nvr` |

## Offline detection — RTSP probe THROUGH the NVR (important)

The obvious approaches **do not work** on this hardware / from a remote host:

- The live-state CGI `/cgi-bin/api_camera_state.cgi` returns **"Not Implemented"**
  on this firmware.
- Pinging each camera's LAN IP is unreliable remotely — camera IPs aren't always
  routable and ICMP to them is often dropped.

The **reliable** offline check is a **per-channel RTSP probe through the NVR**:

```
rtsp://user:pass@HOST:554/cam/realmonitor?channel=N&subtype=0
```

`camera_status()` runs `ffprobe` against that URL for each channel — **a video
track present ⇒ ONLINE, none ⇒ OFFLINE**. Because RTSP goes to the NVR on `:554`
(not to the camera), this works even when the MCP runs off-site. Details:

- Probes in **batches of 3** to respect the NVR's **~4 concurrent RTSP session**
  cap.
- Per-read `rw_timeout` ≈ 9 s and an overall subprocess ceiling so a wedged
  stream can't hang the tool.
- The password is **URL-encoded** in the RTSP URL (`@` → `%40`, etc.).
- Output joins channel number + title + camera IP + online state.

`doctor.py` uses this same RTSP-probe path (not ICMP-to-camera), so it is
correct when run remotely.

## Bandwidth is NOT the offline cause here

The audit found encode is already lean: **~2048 kbps VBR H.265, 15 fps, 5MP**.
Total load is well under the uplink budget. `bandwidth_audit` therefore returns a
`note` making explicit that **low total load does not explain a channel going
offline** — for offline/fault diagnosis use `camera_status` (RTSP) and
`link_quality` instead.

## Addressing faults

`link_quality` pings each camera IP for loss%/latency and **flags any camera
whose IP is outside the expected block** (`NVR_LAN_PREFIX` + expected range,
default `.50–.64`). An off-scheme IP (e.g. one camera on `192.168.1.23` while the
rest are `.50–.64`) is an addressing-fault / IP-conflict red flag. It also
surfaces an optional IP-conflict hint from TTL variation across cameras.

## Typical deployment facts

- **NVR:** Dahua DH-NVR4216-16P-HDS3, 16-channel PoE.
- **16 channels**, cameras on a flat LAN (`192.168.1.50–.64`) via an external PoE switch. (The NVR's built-in PoE ports use a separate `192.168.0.x` subnet and are not needed when cameras are on a shared switch.)
- Cameras: 5MP H.265, ~2 Mbps VBR each — total load well under a gigabit uplink.
- Audio: G.711u, enabled per-channel.

## Example channel map

Edit `apply_names.py` to define your own layout. Example for a 16-camera retail store:

| Ch | Name | Ch | Name |
|----|------|----|------|
| 1 | Front Entrance | 9 | Exterior Side |
| 2 | Storage Room | 10 | Aisle A |
| 3 | Register POS | 11 | Aisle B |
| 4 | Checkout | 12 | Exterior Rear |
| 5 | Exterior Front | 13 | Dining/Lobby |
| 6 | Exterior Corner | 14 | Back Office |
| 7 | Production Area | 15 | Loading Dock |
| 8 | Break Room | 16 | Parking Lot |

## Setup

```bash
cd nvr-mcp
pip install -r requirements.txt
cp .env.example .env      # fill in real creds (password from your env file)
# ffmpeg + ffprobe must be on PATH (used for snapshots + RTSP probe)
python server.py          # stdio MCP server
```

### Env

| Var | Purpose | Default |
|-----|---------|---------|
| `NVR_HOST` | NVR IP | `192.168.1.100` |
| `NVR_USER` / `NVR_PASS` | credentials (no default password) | — |
| `NVR_HTTP_PORT` / `NVR_RTSP_PORT` | ports | `80` / `554` |
| `NVR_SITE` | site label shown in output | `my-store` |
| `NVR_LAN_PREFIX` | camera subnet prefix | `192.168.1` |
| `NVR_EXPECTED_IP_RANGE` | expected last-octet window `lo-hi` | `50-64` |

## Registration

Register in `.mcp.json` (repo root) under the new name **`cctv`** — replace any
old `cctv` block:

```json
"cctv": {
  "command": "python",
  "args": ["server.py"],
  "env": {
    "NVR_HOST": "192.168.1.100",
    "NVR_USER": "admin",
    "NVR_PASS": "...",
    "NVR_SITE": "my-store",
    "NVR_LAN_PREFIX": "192.168.1",
    "NVR_EXPECTED_IP_RANGE": "50-64"
  }
}
```

> Put the real password in your .env file — never commit it.
> This README does **not** edit `.mcp.json` for you — update it manually.

## Multi-site (future)

`sites.json.example` documents the shape a site registry would take (host, site
label, `lan_prefix`, `expected_ip_range`, credential env var per site). Today the
server runs a **single active site via env**; the JSON is a forward-looking
template for when a registry loader is added.

## Known device quirks (baked into the client)

- **Digest auth rate-limits rapid requests** → client retries with backoff.
- **RTSP session cap (~4 concurrent)** → probes/snapshots run in small batches.
- **Only the main stream (subtype=0)** is reliably relayed for these OEM
  cameras; probes and snapshots use main.
- **curl needs `-g`** for `ChannelTitle[N]` URLs (bracket globbing); the Python
  client uses `requests`, so it's unaffected.

## Standalone scripts

- `apply_names.py` — write the channel-name map above and verify each.
- `doctor.py` — one-shot health report (prints the `full_audit` output using
  RTSP-based offline detection).
