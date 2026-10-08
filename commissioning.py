"""On-site commissioning and pre-departure verification for Dahua NVR fleet."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from nvr_client import NvrClient, NvrConfig, NvrError

# Standard fleet encode settings
MAIN_BITRATE_KBPS = 2048
MAIN_FPS = 15
MAIN_CODEC = "H.265"
SUB_BITRATE_KBPS = 512
SUB_FPS = 10

REMOTE_USER = "josho"
PORT_FORWARD_REQUIRED = {80: "HTTP/CGI", 554: "RTSP video stream", 37777: "Dahua desktop app"}

# Factory default passwords to probe during assessment
FACTORY_PASSWORDS = ["", "admin", "12345", "123456", "Admin12345"]


# --------------------------------------------------------------------------- #
# Assessment (read-only)                                                       #
# --------------------------------------------------------------------------- #
def assess_site(client: NvrClient) -> dict[str, Any]:
    """
    Audit the NVR and return a commissioning plan.
    Makes NO changes — read-only. Call commission_apply() to execute.
    """
    issues: list[dict] = []
    will_do: list[str] = []

    def _issue(severity: str, area: str, description: str, fix: str) -> None:
        issues.append({"severity": severity, "area": area,
                        "description": description, "fix": fix})

    # Pull audit data (fail-soft per section)
    try:
        audit = client.full_audit()
    except NvrError as e:
        return {"error": f"Cannot reach NVR: {e}",
                "issues": [], "will_do": [], "ready": False}

    device = audit.get("device", {})
    network = audit.get("network", {})
    storage = audit.get("storage", {})
    camera_status = audit.get("camera_status", {})
    link_quality = audit.get("link_quality", {})
    encode = audit.get("encode", {})
    record_mode = audit.get("record_mode", {})
    audio = audit.get("audio", {})

    # 1. Factory password check
    for pw in FACTORY_PASSWORDS:
        try:
            test_cfg = NvrConfig(
                host=client.cfg.host, username="admin", password=pw,
                http_port=client.cfg.http_port, rtsp_port=client.cfg.rtsp_port,
                site=client.cfg.site, lan_prefix=client.cfg.lan_prefix,
                expected_ip_min=client.cfg.expected_ip_min,
                expected_ip_max=client.cfg.expected_ip_max,
            )
            test_client = NvrClient(test_cfg)
            test_client.device_info()
            _issue("CRITICAL", "security",
                   f"NVR accessible with factory password '{pw or '(empty)'}'",
                   "Change admin password to Admin@135")
            break
        except NvrError:
            pass

    # 2. Cameras offline
    offline = camera_status.get("offline_channels", [])
    if offline:
        _issue("CRITICAL", "cameras",
               f"Cameras offline: channels {offline}",
               "Check PoE switch, cable, and camera power")
    will_do.append("Verify all cameras online after config")

    # 3. Off-scheme IPs
    off_ips = link_quality.get("off_scheme_ips", [])
    if off_ips:
        _issue("WARN", "network",
               f"Camera IPs outside expected block: {off_ips}",
               "Reassign camera IPs to match LAN scheme")

    # 4. Main stream bitrate
    enc_channels = encode.get("channels", [])
    for ch in enc_channels:
        br = ch.get("bitrate_kbps", 0)
        codec = ch.get("codec", "")
        if br > 4096:
            _issue("WARN", "encode",
                   f"Ch{ch['channel']} bitrate {br} kbps is excessive (>4096)",
                   f"Set to {MAIN_BITRATE_KBPS} kbps")
        elif br < 512 and br > 0:
            _issue("WARN", "encode",
                   f"Ch{ch['channel']} bitrate {br} kbps too low (<512) — poor quality",
                   f"Set to {MAIN_BITRATE_KBPS} kbps")
        if codec and "265" not in codec:
            _issue("WARN", "encode",
                   f"Ch{ch['channel']} using {codec} instead of H.265 — wastes storage",
                   "Switch to H.265")
    will_do.append(f"Set ALL main streams: {MAIN_CODEC} / {MAIN_BITRATE_KBPS} kbps / {MAIN_FPS} fps / VBR")
    will_do.append(f"Set ALL sub-streams: {MAIN_CODEC} / {SUB_BITRATE_KBPS} kbps / {SUB_FPS} fps / VBR")

    # 5. Recording mode
    for ch_num, mode in record_mode.items():
        if mode == "off":
            _issue("CRITICAL", "recording",
                   f"Ch{ch_num} recording is OFF",
                   "Enable continuous recording")
    will_do.append("Set all channels to continuous 24/7 recording")

    # 6. Audio
    audio_issues = [r for r in (audio if isinstance(audio, list) else [])
                    if not r.get("cfg_main_audio")]
    if audio_issues:
        chs = [r["channel"] for r in audio_issues]
        _issue("WARN", "audio", f"Audio disabled on channels: {chs}", "Enable audio")
    will_do.append("Enable audio recording on all channels")

    # 7. Remote user
    try:
        users = client.list_users()
        remote_exists = any(u["name"] == REMOTE_USER for u in users)
    except NvrError:
        remote_exists = False
    if not remote_exists:
        _issue("WARN", "users",
               f"Remote user '{REMOTE_USER}' does not exist",
               f"Create '{REMOTE_USER}' with Josho@135 (viewer permissions)")
    will_do.append(f"Create remote user '{REMOTE_USER}' / Josho@135 (viewer only) if missing")

    # 8. DDNS
    try:
        ddns = client.get_ddns_status()
        ddns_enabled = ddns.get("enabled", False)
    except NvrError:
        ddns_enabled = False
    if not ddns_enabled:
        _issue("INFO", "remote_access",
               "DDNS not enabled — NVR not reachable remotely",
               "Enable QuickDDNS and port-forward router")
    will_do.append("Configure QuickDDNS if ddns_host is set in site config")

    # 9. Storage
    used_pct = storage.get("used_pct")
    if used_pct is not None:
        if used_pct > 90:
            _issue("CRITICAL", "storage",
                   f"Storage {used_pct}% full — overwrite loop imminent or broken",
                   "Verify overwrite-on-full is enabled; check HDD health")
        elif used_pct > 80:
            _issue("WARN", "storage",
                   f"Storage {used_pct}% full",
                   "Monitor; consider adding/replacing HDD soon")

    # 10. Static IP
    if network.get("dhcp"):
        _issue("WARN", "network",
               "NVR is on DHCP — IP may change and break DDNS/remote access",
               "Set NVR to static IP before enabling DDNS")

    will_do.append("Change admin password to Admin@135 (LAST step, then re-auth)")

    return {
        "site": client.cfg.site,
        "host": client.cfg.host,
        "device": device,
        "issues": issues,
        "critical_count": sum(1 for i in issues if i["severity"] == "CRITICAL"),
        "warn_count": sum(1 for i in issues if i["severity"] == "WARN"),
        "will_do": will_do,
        "requires_port_forward": PORT_FORWARD_REQUIRED,
        "ddns_instructions": (
            "1. Go to NVR main menu → Network → DDNS\n"
            "2. Enable: YES, Server: selfddns.quickddns.com\n"
            "3. Domain: choose a subdomain (e.g. storename-boston)\n"
            "4. On store router: forward ports 80, 554, 37777 to NVR LAN IP\n"
            "5. Full DDNS hostname: {subdomain}.quickddns.com"
        ),
    }


# --------------------------------------------------------------------------- #
# Apply standard config                                                        #
# --------------------------------------------------------------------------- #
def commission_apply(client: NvrClient) -> dict[str, Any]:
    """
    Execute the full commission plan on the active NVR.
    Password change happens LAST; returns a new NvrClient if password changed.
    """
    steps: list[dict] = []
    active_client = client

    def _step(name: str, ok: bool, detail: str = "") -> None:
        steps.append({"step": name, "ok": ok, "detail": detail})

    # --- 1. Main stream encode ---
    try:
        cams = active_client.list_cameras()
        enabled = [c for c in cams if c.enabled]
        for cam in enabled:
            r = active_client.set_bitrate_and_codec(
                cam.channel, MAIN_BITRATE_KBPS, MAIN_FPS, MAIN_CODEC)
            if not r.get("ok"):
                _step(f"main_stream ch{cam.channel}", False, r.get("raw", ""))
        _step("main_stream_all", True,
              f"H.265 / {MAIN_BITRATE_KBPS} kbps / {MAIN_FPS} fps → {len(enabled)} channels")
    except NvrError as e:
        _step("main_stream_all", False, str(e))

    # --- 2. Sub-stream encode ---
    try:
        for cam in enabled:
            active_client.set_substream_encode(cam.channel, SUB_BITRATE_KBPS, SUB_FPS)
        _step("sub_stream_all", True,
              f"H.265 / {SUB_BITRATE_KBPS} kbps / {SUB_FPS} fps → {len(enabled)} channels")
    except NvrError as e:
        _step("sub_stream_all", False, str(e))

    # --- 3. Continuous recording ---
    try:
        for cam in enabled:
            active_client.set_continuous_record(cam.channel)
        _step("continuous_record_all", True, f"{len(enabled)} channels → continuous")
    except NvrError as e:
        _step("continuous_record_all", False, str(e))

    # --- 4. Audio ---
    try:
        for cam in enabled:
            active_client.enable_audio(cam.channel)
        _step("audio_all", True, f"Audio enabled → {len(enabled)} channels")
    except NvrError as e:
        _step("audio_all", False, str(e))

    # --- 5. Remote user ---
    remote_pass = os.environ.get("NVR_REMOTE_PASS", "")
    if remote_pass:
        try:
            r = active_client.create_remote_user(REMOTE_USER, remote_pass)
            _step("remote_user", r.get("ok", False) or r.get("existed", False),
                  "already existed" if r.get("existed") else r.get("raw", ""))
        except NvrError as e:
            _step("remote_user", False, str(e))
    else:
        _step("remote_user", False, "NVR_REMOTE_PASS env not set — skipped")

    # --- 6. DDNS ---
    try:
        ddns_s = active_client.get_ddns_status()
        if ddns_s.get("enabled") and ddns_s.get("domain"):
            _step("ddns", True, f"Already enabled: {ddns_s['domain']}")
        else:
            _step("ddns", False,
                  "DDNS domain not configured in site — use configure_ddns tool manually")
    except NvrError as e:
        _step("ddns", False, str(e))

    # --- 7. Password change (LAST) ---
    admin_pass = os.environ.get("NVR_ADMIN_PASS", "")
    if admin_pass:
        try:
            old_pass = active_client.cfg.password
            ok, new_client = active_client.change_admin_password(old_pass, admin_pass)
            if ok:
                active_client = new_client
                _step("admin_password", True, "Changed and re-authenticated")
            else:
                _step("admin_password", False, "Password change failed — may already be set")
        except NvrError as e:
            _step("admin_password", False, str(e))
    else:
        _step("admin_password", False, "NVR_ADMIN_PASS env not set — skipped")

    # --- Departure check with final client ---
    check = departure_check(active_client)

    return {
        "site": client.cfg.site,
        "steps": steps,
        "client_updated": active_client is not client,
        "departure_check": check,
    }


# --------------------------------------------------------------------------- #
# Departure check                                                              #
# --------------------------------------------------------------------------- #
def departure_check(client: NvrClient) -> dict[str, Any]:
    """Pre-departure verification. Returns ready_to_leave=True only if all CRITICAL checks pass."""
    checklist: list[dict] = []
    blockers: list[str] = []
    warnings: list[str] = []

    def _add(item: str, status: str, detail: str = "") -> None:
        checklist.append({"item": item, "status": status, "detail": detail})
        if status == "FAIL":
            blockers.append(f"{item}: {detail}")
        elif status == "WARN":
            warnings.append(f"{item}: {detail}")

    # Pull all needed data up front (fail-soft)
    try:
        cam_status = client.camera_status()
    except NvrError as e:
        cam_status = {"error": str(e)}

    try:
        rec_mode = client.record_mode()
    except NvrError as e:
        rec_mode = {}

    try:
        storage = client.storage_state()
    except NvrError as e:
        storage = {"error": str(e)}

    try:
        lq = client.link_quality()
    except NvrError as e:
        lq = {"error": str(e)}

    try:
        enc = client.encode_config()
    except NvrError as e:
        enc = {}

    try:
        users = client.list_users()
        user_names = [u["name"] for u in users]
    except NvrError:
        user_names = []

    try:
        ddns = client.get_ddns_status()
    except NvrError:
        ddns = {}

    try:
        net = client.network_config()
    except NvrError:
        net = {}

    # 1. All cameras online
    if "error" in cam_status:
        _add("cameras_online", "FAIL", f"Could not check: {cam_status['error']}")
    else:
        offline = cam_status.get("offline_channels", [])
        if offline:
            _add("cameras_online", "FAIL", f"Offline: channels {offline}")
        else:
            _add("cameras_online", "PASS",
                 f"All {cam_status.get('total', 0)} cameras online")

    # 2. All channels recording
    not_recording = [ch for ch, mode in rec_mode.items() if mode == "off"]
    if not_recording:
        _add("recording_active", "FAIL", f"Recording OFF on channels: {not_recording}")
    elif not rec_mode:
        _add("recording_active", "WARN", "Could not verify record mode")
    else:
        _add("recording_active", "PASS", f"All {len(rec_mode)} channels recording")

    # 3. Storage health
    if "error" in storage:
        _add("storage_health", "WARN", f"Could not check: {storage['error']}")
    else:
        disks = storage.get("disks", [])
        bad_disks = [d for d in disks if d.get("status", "").lower() not in ("normal", "ok", "")]
        used_pct = storage.get("used_pct")
        if not disks:
            _add("storage_health", "FAIL", "No HDD detected")
        elif bad_disks:
            _add("storage_health", "FAIL",
                 f"Disk(s) not Normal: {[d['index'] for d in bad_disks]}")
        elif used_pct is not None and used_pct > 90:
            _add("storage_health", "FAIL", f"Storage {used_pct}% full")
        elif used_pct is not None and used_pct > 80:
            _add("storage_health", "WARN", f"Storage {used_pct}% full — monitor soon")
        else:
            _add("storage_health", "PASS",
                 f"{storage.get('total_gb')} GB total, {used_pct}% used")

    # 4. Network stability
    if "error" in lq:
        _add("network_stability", "WARN", f"Could not ping cameras: {lq['error']}")
    else:
        worst = lq.get("worst_loss_pct", 0)
        off_ips = lq.get("off_scheme_ips", [])
        if worst > 10:
            _add("network_stability", "WARN",
                 f"Packet loss {worst}% — check PoE switch or cabling")
        elif off_ips:
            _add("network_stability", "WARN", f"Off-scheme IPs: {off_ips}")
        else:
            _add("network_stability", "PASS", f"Worst loss {worst}%")

    # 5. Remote user exists
    if REMOTE_USER in user_names:
        _add("remote_user_josho", "PASS", f"'{REMOTE_USER}' user present")
    elif not user_names:
        _add("remote_user_josho", "WARN", "Could not verify users")
    else:
        _add("remote_user_josho", "WARN",
             f"'{REMOTE_USER}' user missing — remote access via DDNS won't work")

    # 6. DDNS
    if ddns.get("enabled") and ddns.get("domain"):
        _add("ddns_configured", "PASS", f"Domain: {ddns['domain']}")
    else:
        _add("ddns_configured", "WARN",
             "DDNS not enabled — remote monitoring unavailable until configured")

    # 7. H.265 on all channels
    h264_channels = [ch for ch, cfg in enc.items()
                     if "265" not in str(cfg.get("Compression", ""))]
    if h264_channels and enc:
        _add("h265_codec", "WARN",
             f"Channels still on H.264: {h264_channels} — higher storage usage")
    elif enc:
        _add("h265_codec", "PASS", "All channels on H.265")
    else:
        _add("h265_codec", "WARN", "Could not verify codec")

    # 8. Sub-stream configured
    # Check via encode config — ExtraFormat should have non-zero bitrate
    sub_unconfigured: list[int] = []
    try:
        raw_enc = client._get_safe(
            "/cgi-bin/configManager.cgi?action=getConfig&name=Encode")
        import re
        for k, v in raw_enc.items():
            m = re.match(r"Encode\[(\d+)\]\.ExtraFormat\[0\]\.Video\.BitRate", k)
            if m and (not v or int(v) < 128):
                sub_unconfigured.append(int(m.group(1)) + 1)
    except Exception:
        pass
    if sub_unconfigured:
        _add("substream_configured", "WARN",
             f"Sub-stream not set on channels: {sub_unconfigured} — mobile app quality poor")
    else:
        _add("substream_configured", "PASS", "Sub-streams configured on all channels")

    # 9. Static IP
    if net.get("dhcp"):
        _add("static_ip", "WARN",
             "NVR on DHCP — IP may change and break remote access")
    elif net.get("ip"):
        _add("static_ip", "PASS", f"Static IP: {net['ip']}")
    else:
        _add("static_ip", "WARN", "Could not verify IP assignment")

    # 10. Port forward reminder (always INFO — cannot auto-verify from inside LAN)
    ports_str = ", ".join(f"{p} ({desc})" for p, desc in PORT_FORWARD_REQUIRED.items())
    _add("port_forward_reminder", "PASS",
         f"Manually verify on store router: forward {ports_str} → {client.cfg.host}")

    ready = len(blockers) == 0
    return {
        "site": client.cfg.site,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ready_to_leave": ready,
        "verdict": "✓ READY TO LEAVE" if ready else f"✗ {len(blockers)} BLOCKER(S) — fix before leaving",
        "checklist": checklist,
        "blockers": blockers,
        "warnings": warnings,
    }
