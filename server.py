"""
CCTV MCP server — control & self-heal Dahua NVR fleet from Claude.

Fleet-capable: sites.json holds all client NVRs. Switch between sites with
switch_site(), connect remotely via DDNS with mode='remote'. Falls back to
env vars if sites.json is absent (backward-compatible with single-site setup).

ON-ARRIVAL WORKFLOW (run in order at every new store):
  1. discover_nvr          — find the NVR IP on the store's LAN
  2. commission_site        — dry_run=True → review the plan
  3. commission_site        — dry_run=False → apply everything
  4. departure_check_tool   — verify all green before leaving

FLEET MANAGEMENT:
  list_sites / switch_site / add_site

REMOTE ACCESS:
  switch_site(label, mode='remote') → uses DDNS hostname instead of LAN IP

PASSWORDS:
  admin / Admin@135     — on-site, full control (NVR_ADMIN_PASS env)
  josho / Josho@135     — remote viewer via DDNS (NVR_REMOTE_PASS env)

Run:  python server.py   (stdio transport, for .mcp.json)
Env:  NVR_ADMIN_PASS, NVR_REMOTE_PASS  (and optional per-site overrides)
"""
from __future__ import annotations

import json
import os
from datetime import datetime

from mcp.server.fastmcp import FastMCP

from commissioning import assess_site, commission_apply, departure_check
from fleet import FleetManager
from nvr_client import NvrClient, NvrError

mcp = FastMCP("cctv")

_SITES_PATH = os.path.join(os.path.dirname(__file__), "sites.json")
_fleet = FleetManager(_SITES_PATH)

SNAP_DIR = os.path.join(os.path.dirname(__file__), "snapshots")
os.makedirs(SNAP_DIR, exist_ok=True)


def _get_client() -> NvrClient:
    """Return NvrClient for the current active site + mode."""
    return _fleet.build_client()


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, default=str)


def _err(context: str, e: Exception) -> str:
    try:
        site = _fleet.active_site().key
    except Exception:
        site = "unknown"
    return f"ERROR [{site}] {context}: {e}"


# ====================================================================== #
# Fleet management                                                        #
# ====================================================================== #

@mcp.tool()
def list_sites() -> str:
    """List all NVR sites in the fleet: host, DDNS hostname, model, channels, active/mode."""
    return _dump(_fleet.list_sites())


@mcp.tool()
def switch_site(label: str, mode: str = "lan") -> str:
    """
    Switch the active NVR site.
    label: site key from sites.json (e.g. 'my-store').
    mode: 'lan' = on-site LAN IP; 'remote' = DDNS hostname for remote monitoring.
    All subsequent tool calls target the new site until switched again.
    """
    try:
        _fleet.switch_site(label, mode)
        site = _fleet.active_site()
        return _dump({
            "switched_to": label,
            "mode": mode,
            "host": site.ddns_host if mode == "remote" else site.host,
            "label": site.label,
        })
    except (KeyError, ValueError) as e:
        return _err("switch_site", e)


@mcp.tool()
def add_site(
    label: str,
    display_name: str,
    host: str,
    ddns_host: str = "",
    lan_prefix: str = "",
    expected_ip_range: str = "50-64",
    model: str = "Dahua",
    channels: int = 16,
) -> str:
    """
    Register a new store NVR in the fleet.
    label: short key, no spaces (e.g. 'boston-halal-market').
    host: static LAN IP of the NVR (e.g. '192.168.1.108').
    ddns_host: QuickDDNS subdomain for remote access (e.g. 'boston-halal').
    Passwords (Admin@135 / Josho@135) come from env — never stored here.
    """
    try:
        _fleet.add_site(
            key=label,
            label=display_name,
            host=host,
            ddns_host=ddns_host,
            lan_prefix=lan_prefix,
            expected_ip_range=expected_ip_range,
            model=model,
            channels=channels,
        )
        return _dump({
            "added": label,
            "host": host,
            "ddns_host": ddns_host,
            "message": f"Site '{label}' saved. Switch to it with switch_site('{label}')",
        })
    except Exception as e:
        return _err("add_site", e)


# ====================================================================== #
# Discovery                                                               #
# ====================================================================== #

@mcp.tool()
def discover_nvr(subnet: str = "") -> str:
    """
    Scan the current network to find Dahua NVR devices.
    subnet: LAN prefix to sweep, e.g. '192.168.1'. Leave blank for auto-detect
    (UDP broadcast + ARP + common defaults — no full subnet sweep).
    Confirmed devices responded to Dahua CGI — they are real NVRs.
    Run this FIRST when you arrive at a new store to find the NVR IP.
    """
    try:
        results = NvrClient.discover_nvr(subnet)
        return _dump({
            "found": len(results),
            "devices": results,
            "next_step": (
                "Use add_site() to register the NVR, then switch_site() to connect."
                if results else
                "No NVRs found. Try with subnet='192.168.1' (or your store's subnet)."
            ),
        })
    except Exception as e:
        return _err("discover_nvr", e)


# ====================================================================== #
# On-arrival commissioning                                                #
# ====================================================================== #

@mcp.tool()
def commission_site(dry_run: bool = True) -> str:
    """
    ON-ARRIVAL WORKFLOW. Run this first when you connect to a store.

    dry_run=True (default): Audit only — shows what is wrong and what WILL be
    changed, but makes NO changes. Always review this before applying.

    dry_run=False: Executes the full standard config in sequence:
      1. All main streams  → H.265 / 2048 kbps VBR / 15 fps
      2. All sub-streams   → H.265 / 512 kbps VBR / 10 fps  (mobile/remote)
      3. All channels      → continuous 24/7 recording
      4. Audio             → enabled on all channels
      5. Remote user josho → created with Josho@135 (viewer permissions)
      6. QuickDDNS         → configured if ddns_host set in site config
      7. Admin password    → changed to Admin@135 (LAST, then re-auth)
      8. Departure check   → full verification before you leave
    """
    client = _get_client()
    if dry_run:
        try:
            return _dump(assess_site(client))
        except NvrError as e:
            return _err("commission_site (assess)", e)
    else:
        try:
            return _dump(commission_apply(client))
        except NvrError as e:
            return _err("commission_site (apply)", e)


@mcp.tool()
def departure_check_tool() -> str:
    """
    PRE-DEPARTURE CHECKLIST. Run before leaving any site.

    Verifies:
      All cameras ONLINE
      All channels recording (continuous)
      Storage healthy (HDD present, < 80% full)
      Network stable (ping loss < 5%)
      Remote user 'josho' exists
      DDNS (QuickDDNS) configured
      H.265 codec on all main streams
      Sub-streams configured (mobile viewing)
      NVR on static IP (not DHCP)
      Port forward reminder (verify on router)

    Returns READY TO LEAVE or lists blockers that must be fixed first.
    """
    try:
        return _dump(departure_check(_get_client()))
    except NvrError as e:
        return _err("departure_check", e)


@mcp.tool()
def create_remote_user() -> str:
    """
    Create the 'josho' remote monitoring user (viewer-only, Josho@135).
    Safe to run multiple times — skips silently if user already exists.
    This account is used for remote DDNS access from anywhere.
    Requires NVR_REMOTE_PASS env to be set.
    """
    remote_pass = os.environ.get("NVR_REMOTE_PASS", "")
    if not remote_pass:
        return _err("create_remote_user",
                    Exception("NVR_REMOTE_PASS env not set — add it to .env"))
    try:
        return _dump(_get_client().create_remote_user("josho", remote_pass))
    except NvrError as e:
        return _err("create_remote_user", e)


@mcp.tool()
def apply_optimal_encode() -> str:
    """
    Apply standard encode settings to ALL enabled channels:
      Main stream: H.265 / 2048 kbps VBR / 15 fps  (local recording)
      Sub-stream:  H.265 / 512 kbps VBR / 10 fps   (remote/mobile viewing)
    Does NOT change passwords or users. Safe to run any time.
    """
    client = _get_client()
    try:
        cams = [c for c in client.list_cameras() if c.enabled]
        main_results = [client.set_bitrate_and_codec(c.channel, 2048, 15, "H.265")
                        for c in cams]
        sub_results = [client.set_substream_encode(c.channel, 512, 10) for c in cams]
        ok_main = sum(1 for r in main_results if r.get("ok"))
        ok_sub = sum(1 for r in sub_results if r.get("ok"))
        return _dump({
            "channels_configured": len(cams),
            "main_stream": {
                "ok": ok_main,
                "failed": len(cams) - ok_main,
                "setting": "H.265 / 2048 kbps / 15 fps / VBR",
            },
            "sub_stream": {
                "ok": ok_sub,
                "failed": len(cams) - ok_sub,
                "setting": "H.265 / 512 kbps / 10 fps / VBR",
            },
            "main_detail": main_results,
            "sub_detail": sub_results,
        })
    except NvrError as e:
        return _err("apply_optimal_encode", e)


@mcp.tool()
def set_continuous_recording() -> str:
    """Set ALL enabled channels to continuous 24/7 recording mode."""
    client = _get_client()
    try:
        cams = [c for c in client.list_cameras() if c.enabled]
        results = [client.set_continuous_record(c.channel) for c in cams]
        ok = sum(1 for r in results if r.get("ok"))
        return _dump({
            "channels": len(cams),
            "set_ok": ok,
            "failed": len(cams) - ok,
            "detail": results,
        })
    except NvrError as e:
        return _err("set_continuous_recording", e)


@mcp.tool()
def configure_ddns(domain: str) -> str:
    """
    Enable Dahua QuickDDNS on the active NVR.
    domain: subdomain only, e.g. 'my-store-2'
            → full remote URL: http://my-store-2.quickddns.com

    After running this, manually forward on the store router:
      TCP 80   → NVR IP  (web interface + API)
      TCP/UDP 554 → NVR IP  (RTSP live stream)
      TCP 37777 → NVR IP  (Dahua SmartPSS / DMSS desktop)

    Remote login: josho / Josho@135
    """
    try:
        return _dump(_get_client().configure_ddns(domain))
    except NvrError as e:
        return _err("configure_ddns", e)


# ====================================================================== #
# Diagnostics (read-only)                                                 #
# ====================================================================== #

@mcp.tool()
def nvr_info() -> str:
    """Return NVR identity: site label, model, serial, firmware, machine name."""
    try:
        return _dump(_get_client().device_info())
    except NvrError as e:
        return _err("nvr_info", e)


@mcp.tool()
def list_cameras() -> str:
    """List every channel with enable state, IP, MAC, title, model, protocol."""
    try:
        cams = _get_client().list_cameras()
        return _dump([c.__dict__ for c in cams])
    except NvrError as e:
        return _err("list_cameras", e)


@mcp.tool()
def channel_names() -> str:
    """Return the current display name of each channel (1-based)."""
    try:
        return _dump(_get_client().get_channel_names())
    except NvrError as e:
        return _err("channel_names", e)


@mcp.tool()
def camera_status(subtype: int = 0) -> str:
    """
    ONLINE/OFFLINE per channel via a per-channel RTSP probe THROUGH the NVR.
    Reliable remote-safe check — RTSP hits the NVR (:554), not the camera IPs.
    A video track present => ONLINE. Probes in small batches (RTSP session cap).
    """
    try:
        return _dump(_get_client().camera_status(subtype=subtype))
    except NvrError as e:
        return _err("camera_status", e)


@mcp.tool()
def link_quality(count: int = 10) -> str:
    """
    Ping each camera IP for loss%/latency AND flag any camera whose IP falls
    outside the expected block. Off-scheme IP = addressing fault / IP conflict.
    Also surfaces TTL-variation IP-conflict hint.
    """
    try:
        return _dump(_get_client().link_quality(count=count))
    except NvrError as e:
        return _err("link_quality", e)


@mcp.tool()
def audio_audit() -> str:
    """
    Per-channel AudioEnable flags (main/sub) plus live RTSP audio-track check.
    Flags mismatches between config and actual stream.
    """
    try:
        return _dump(_get_client().audio_audit())
    except NvrError as e:
        return _err("audio_audit", e)


@mcp.tool()
def bandwidth_audit() -> str:
    """
    Per-channel main-stream bitrate/fps/resolution + estimated total load.
    NOTE: low encode load does NOT explain offline cameras — use camera_status
    + link_quality for offline/fault diagnosis.
    """
    try:
        return _dump(_get_client().bandwidth_audit())
    except NvrError as e:
        return _err("bandwidth_audit", e)


@mcp.tool()
def scan_network(start: int = 1, end: int = 254) -> str:
    """Ping-sweep the camera LAN subnet to find all live hosts (rogue/unregistered)."""
    try:
        return _dump(_get_client().scan_network(start=start, end=end))
    except NvrError as e:
        return _err("scan_network", e)


@mcp.tool()
def snapshot(channel: int) -> str:
    """
    Grab a live JPEG frame from a channel and save it under snapshots/.
    Returns the file path so the agent can view it (e.g. to auto-name the channel).
    """
    try:
        ts = datetime.now().strftime("%H%M%S")
        out = os.path.join(SNAP_DIR, f"ch{channel:02d}_{ts}.jpg")
        return _dump(_get_client().snapshot(channel, out, subtype=0))
    except NvrError as e:
        return _err("snapshot", e)


@mcp.tool()
def full_audit() -> str:
    """
    One-shot health report — everything in a single call:
    device identity, network config, HDD/storage, record mode, encode,
    camera_status (RTSP online/offline), link_quality, audio audit.
    Each section fails soft so a partial outage still returns the rest.
    """
    try:
        return _dump(_get_client().full_audit())
    except NvrError as e:
        return _err("full_audit", e)


@mcp.tool()
def list_users() -> str:
    """List all NVR user accounts with name, group, and authority list."""
    try:
        return _dump(_get_client().list_users())
    except NvrError as e:
        return _err("list_users", e)


@mcp.tool()
def ddns_status() -> str:
    """Check current QuickDDNS config: enabled, domain, provider, update period."""
    try:
        return _dump(_get_client().get_ddns_status())
    except NvrError as e:
        return _err("ddns_status", e)


# ====================================================================== #
# Fixes (write)                                                           #
# ====================================================================== #

@mcp.tool()
def set_channel_name(channel: int, name: str) -> str:
    """Rename a channel (e.g. 'Counter', 'Kitchen', 'Walk-in Freezer'). 1-based."""
    try:
        return _dump(_get_client().set_channel_name(channel, name))
    except NvrError as e:
        return _err(f"set_channel_name(ch{channel})", e)


@mcp.tool()
def enable_audio(channel: int) -> str:
    """Force audio recording+live ON for one channel (main+sub stream)."""
    try:
        return _dump(_get_client().enable_audio(channel))
    except NvrError as e:
        return _err(f"enable_audio(ch{channel})", e)


@mcp.tool()
def enable_all_audio() -> str:
    """Force audio ON for every registered enabled channel at once."""
    try:
        client = _get_client()
        results = [client.enable_audio(c.channel)
                   for c in client.list_cameras() if c.enabled]
        return _dump(results)
    except NvrError as e:
        return _err("enable_all_audio", e)


@mcp.tool()
def set_bitrate(channel: int, bitrate_kbps: int, fps: int = 0) -> str:
    """Cap one channel's main-stream bitrate (kbps) and optionally fps (0=leave)."""
    try:
        return _dump(_get_client().set_bitrate(channel, bitrate_kbps,
                                               fps=fps if fps > 0 else None))
    except NvrError as e:
        return _err(f"set_bitrate(ch{channel})", e)


@mcp.tool()
def optimize_bandwidth(bitrate_kbps: int = 2048, fps: int = 15) -> str:
    """
    Apply a uniform main-stream bitrate + fps to every enabled channel.
    Default 2 Mbps / 15 fps — standard fleet setting.
    For H.265 + sub-stream + codec, use apply_optimal_encode instead.
    """
    try:
        client = _get_client()
        results = [client.set_bitrate(c.channel, bitrate_kbps, fps=fps)
                   for c in client.list_cameras() if c.enabled]
        return _dump(results)
    except NvrError as e:
        return _err("optimize_bandwidth", e)


@mcp.tool()
def reboot_nvr(confirm: bool = False) -> str:
    """Reboot the recorder. Requires confirm=true. Briefly stops all recording."""
    if not confirm:
        return "Refused: pass confirm=true to reboot the NVR."
    try:
        return _dump(_get_client().reboot())
    except NvrError as e:
        return _err("reboot_nvr", e)


if __name__ == "__main__":
    mcp.run()
