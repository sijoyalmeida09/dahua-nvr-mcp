"""
Dahua/Amcrest NVR client — digest-auth CGI + RTSP helpers.

Targets the My Store NVR (Dahua DH-NVR4216-16P-HDS3) but works
against any Dahua-protocol recorder. All network I/O is defensive: short
timeouts, bounded retries with backoff (the device rate-limits rapid digest
re-auth), and structured error returns instead of raised exceptions where a
tool caller needs a value.

Offline detection (IMPORTANT): the live-state CGI `/cgi-bin/api_camera_state.cgi`
returns "Not Implemented" on this firmware, and pinging a camera's LAN IP is
unreliable from a remote host (camera IPs aren't always routable / ICMP is
blocked). The RELIABLE offline check is a per-channel RTSP probe THROUGH the NVR
(rtsp://.../cam/realmonitor?channel=N&subtype=0) with ffprobe — RTSP goes to the
NVR on :554, so it works remotely. See `probe_channel` / `camera_status`.

No global mutable state — every call builds its own request; the shared
requests.Session only pools TCP connections.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests
from requests.auth import HTTPDigestAuth

DEFAULT_TIMEOUT = 15
MAX_RETRIES = 4
BACKOFF_BASE = 1.2  # seconds; multiplied by attempt number

# RTSP probe tuning. The device caps concurrent RTSP sessions (~4), so probe
# in small batches. rw_timeout is the per-read ceiling; overall bounds the
# subprocess so a wedged stream can't hang a tool call.
RTSP_BATCH = 3
RTSP_RW_TIMEOUT_US = 9_000_000        # 9 s, passed to ffprobe -rw_timeout
RTSP_PROC_TIMEOUT = 14                # s, subprocess overall ceiling

# Default expected camera-IP window on the flat LAN (external PoE switch).
# A camera whose last octet falls outside this range is an addressing red flag.
DEFAULT_IP_RANGE = (50, 64)


@dataclass(frozen=True)
class NvrConfig:
    """Immutable connection + site settings. Build once, pass around."""
    host: str
    username: str
    password: str
    http_port: int = 80
    rtsp_port: int = 554
    site: str = "my-store"
    lan_prefix: str = "10.1.10"
    expected_ip_min: int = DEFAULT_IP_RANGE[0]
    expected_ip_max: int = DEFAULT_IP_RANGE[1]

    @staticmethod
    def from_env() -> "NvrConfig":
        lo, hi = _parse_ip_range(os.environ.get("NVR_EXPECTED_IP_RANGE", ""))
        return NvrConfig(
            host=os.environ.get("NVR_HOST", "192.168.1.100"),
            username=os.environ.get("NVR_USER", "admin"),
            password=os.environ.get("NVR_PASS", ""),
            http_port=int(os.environ.get("NVR_HTTP_PORT", "80")),
            rtsp_port=int(os.environ.get("NVR_RTSP_PORT", "554")),
            site=os.environ.get("NVR_SITE", "my-store"),
            lan_prefix=os.environ.get("NVR_LAN_PREFIX", "10.1.10"),
            expected_ip_min=lo,
            expected_ip_max=hi,
        )

    def rtsp_url(self, channel: int, subtype: int = 1) -> str:
        """RTSP URL for a channel THROUGH the NVR. Password is URL-encoded."""
        pw = quote(self.password, safe="")
        user = quote(self.username, safe="")
        return (
            f"rtsp://{user}:{pw}@{self.host}:{self.rtsp_port}"
            f"/cam/realmonitor?channel={channel}&subtype={subtype}"
        )

    def rtsp_url_redacted(self, channel: int, subtype: int = 1) -> str:
        """Same URL with the password masked — safe to log / return to callers."""
        return (
            f"rtsp://{self.username}:***@{self.host}:{self.rtsp_port}"
            f"/cam/realmonitor?channel={channel}&subtype={subtype}"
        )


def _parse_ip_range(raw: str) -> tuple[int, int]:
    """Parse 'lo-hi' (last-octet range) from env; fall back to the default."""
    m = re.match(r"\s*(\d{1,3})\s*-\s*(\d{1,3})\s*$", raw or "")
    if not m:
        return DEFAULT_IP_RANGE
    lo, hi = int(m.group(1)), int(m.group(2))
    if 0 <= lo <= hi <= 255:
        return lo, hi
    return DEFAULT_IP_RANGE


@dataclass(frozen=True)
class Camera:
    """One registered channel's state (immutable snapshot of NVR config)."""
    channel: int            # 1-based channel number
    slot: int               # 0-based RemoteDevice index
    enabled: bool
    address: str
    mac: str
    name: str               # channel title
    device_name: str        # camera model/name reported by device
    protocol: str
    version: str
    online: bool | None = None   # filled by RTSP probe


@dataclass(frozen=True)
class ChannelStatus:
    """Result of an RTSP offline probe for one channel."""
    channel: int
    name: str
    address: str
    online: bool
    has_video: bool
    has_audio: bool
    codecs: str
    note: str = ""


class NvrError(Exception):
    pass


class NvrClient:
    def __init__(self, cfg: NvrConfig):
        self.cfg = cfg
        self._session = requests.Session()
        self._session.auth = HTTPDigestAuth(cfg.username, cfg.password)

    # ------------------------------------------------------------------ #
    # Low-level CGI                                                       #
    # ------------------------------------------------------------------ #
    def _base(self) -> str:
        return f"http://{self.cfg.host}:{self.cfg.http_port}"

    def _get(self, path: str, timeout: int = DEFAULT_TIMEOUT) -> str:
        """GET a CGI path with retry/backoff. Returns text; raises NvrError."""
        url = f"{self._base()}{path}"
        last = ""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = self._session.get(url, timeout=timeout)
                if r.status_code == 200:
                    return r.text
                last = f"HTTP {r.status_code}: {r.text[:120]}"
                # 401/500 on rapid re-auth -> back off and retry
                if r.status_code in (401, 500, 400):
                    time.sleep(BACKOFF_BASE * attempt)
                    continue
                raise NvrError(last)
            except requests.RequestException as e:
                last = f"request error: {e}"
                time.sleep(BACKOFF_BASE * attempt)
        raise NvrError(f"{path} failed after {MAX_RETRIES} tries: {last}")

    def _get_safe(self, path: str) -> dict[str, str]:
        """_get + parse into a table dict; return {} on error (never raises)."""
        try:
            return self._parse_table(self._get(path))
        except NvrError:
            return {}

    @staticmethod
    def _parse_table(text: str) -> dict[str, str]:
        """Parse Dahua 'table.a.b=c' lines into a flat dict {a.b: c}."""
        out: dict[str, str] = {}
        for line in text.splitlines():
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if key.startswith("table."):
                key = key[len("table."):]
            out[key] = val.strip()
        return out

    # ------------------------------------------------------------------ #
    # Device identity                                                    #
    # ------------------------------------------------------------------ #
    def device_info(self) -> dict[str, str]:
        info: dict[str, str] = {"site": self.cfg.site, "host": self.cfg.host}
        for action, label in [
            ("getDeviceType", "type"),
            ("getSerialNo", "sn"),
            ("getSoftwareVersion", "version"),
            ("getMachineName", "name"),
        ]:
            try:
                txt = self._get(f"/cgi-bin/magicBox.cgi?action={action}")
                for line in txt.splitlines():
                    if "=" in line:
                        _, _, v = line.partition("=")
                        info[label] = v.strip()
            except NvrError as e:
                info[label] = f"(error: {e})"
        return info

    # ------------------------------------------------------------------ #
    # Network config / storage / record mode                            #
    # ------------------------------------------------------------------ #
    def network_config(self) -> dict[str, Any]:
        """NVR's own LAN settings: IP, mask, gateway, DHCP vs static, MAC."""
        t = self._get_safe("/cgi-bin/configManager.cgi?action=getConfig&name=Network")
        # Dahua exposes eth0 under Network.eth0.* ; DHCP toggle under Network.eth0.DhcpEnable
        eth = {k[len("Network.eth0."):]: v for k, v in t.items()
               if k.startswith("Network.eth0.")}
        dhcp = eth.get("DhcpEnable", "").lower() == "true"
        return {
            "ip": eth.get("IPAddress", ""),
            "netmask": eth.get("SubnetMask", ""),
            "gateway": t.get("Network.DefaultGateway", eth.get("DefaultGateway", "")),
            "mac": eth.get("PhysicalAddress", ""),
            "dhcp": dhcp,
            "mode": "dhcp" if dhcp else "static",
        }

    def storage_state(self) -> dict[str, Any]:
        """HDD presence + capacity + used%, parsed from storageDevice CGI."""
        try:
            txt = self._get("/cgi-bin/storageDevice.cgi?action=getDeviceAllInfo")
        except NvrError as e:
            return {"ok": False, "error": str(e), "disks": []}
        t = self._parse_table(txt)
        # keys look like: list[0].Detail[0].TotalBytes / .UsedBytes / .Status
        disks: dict[int, dict[str, Any]] = {}
        rex = re.compile(r"list\[(\d+)\]\.Detail\[0\]\.(\w+)")
        for k, v in t.items():
            m = rex.match(k)
            if m:
                disks.setdefault(int(m.group(1)), {})[m.group(2)] = v
        rows = []
        total_all = used_all = 0.0
        for i in sorted(disks):
            d = disks[i]
            total = _to_float(d.get("TotalBytes"))
            used = _to_float(d.get("UsedBytes"))
            total_all += total
            used_all += used
            rows.append({
                "index": i,
                "status": d.get("Status", ""),
                "total_gb": round(total / 1e9, 1),
                "used_gb": round(used / 1e9, 1),
                "used_pct": round(100 * used / total, 1) if total else None,
            })
        return {
            "ok": bool(rows),
            "disks": rows,
            "total_gb": round(total_all / 1e9, 1),
            "used_pct": round(100 * used_all / total_all, 1) if total_all else None,
        }

    def record_mode(self) -> dict[int, str]:
        """Per-channel record mode: 0=auto/schedule, 1=manual, 2=off (1-based)."""
        t = self._get_safe("/cgi-bin/configManager.cgi?action=getConfig&name=RecordMode")
        out: dict[int, str] = {}
        labels = {"0": "auto/schedule", "1": "manual", "2": "off"}
        for k, v in t.items():
            m = re.match(r"RecordMode\[(\d+)\]\.Mode", k)
            if m:
                out[int(m.group(1)) + 1] = labels.get(v.strip(), v.strip())
        return out

    # ------------------------------------------------------------------ #
    # Cameras / channels                                                 #
    # ------------------------------------------------------------------ #
    def list_cameras(self) -> list[Camera]:
        remote = self._parse_table(
            self._get("/cgi-bin/configManager.cgi?action=getConfig&name=RemoteDevice")
        )
        titles = self._parse_table(
            self._get("/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle")
        )
        # Group RemoteDevice by slot index. Keys look like:
        #   RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_{N}.Address=...
        # slot N maps to channel N+1.
        slots: dict[int, dict[str, str]] = {}
        rex = re.compile(r"RemoteDevice\.uuid:System_CONFIG_NETCAMERA_INFO_(\d+)\.(.+)")
        for k, v in remote.items():
            m = rex.match(k)
            if not m:
                continue
            idx = int(m.group(1))
            slots.setdefault(idx, {})[m.group(2)] = v

        cameras: list[Camera] = []
        for idx in sorted(slots):
            d = slots[idx]
            channel = idx + 1
            name = (
                titles.get(f"ChannelTitle[{idx}].Name")
                or titles.get(f"ChannelTitle[{idx}][0].Name")
                or f"Channel{channel}"
            )
            cameras.append(
                Camera(
                    channel=channel,
                    slot=idx,
                    enabled=d.get("Enable", "false").lower() == "true",
                    address=d.get("Address", ""),
                    mac=d.get("Mac", ""),
                    name=name.strip(),
                    device_name=d.get("Name", "").strip(),
                    protocol=d.get("ProtocolType", ""),
                    version=d.get("Version", "").strip(),
                )
            )
        return cameras

    def get_channel_names(self) -> dict[int, str]:
        titles = self._parse_table(
            self._get("/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle")
        )
        out: dict[int, str] = {}
        rex = re.compile(r"ChannelTitle\[(\d+)\](?:\[0\])?\.Name")
        for k, v in titles.items():
            m = rex.match(k)
            if m:
                out[int(m.group(1)) + 1] = v.strip()
        return out

    def set_channel_name(self, channel: int, name: str) -> dict[str, Any]:
        """
        Set a channel's display title (1-based). Dahua stores some channels as
        single-index (ChannelTitle[N]) and others as double-index
        (ChannelTitle[N][0]); a write only sticks in the matching form. Try
        single-index first, then double-index.
        """
        _require_channel(channel)
        if not name.strip():
            raise NvrError("channel name must not be empty")
        idx = channel - 1
        enc = quote(name, safe="")
        forms = [f"ChannelTitle[{idx}].Name", f"ChannelTitle[{idx}][0].Name"]
        last = ""
        for key in forms:
            txt = self._get(
                f"/cgi-bin/configManager.cgi?action=setConfig&{key}={enc}"
            )
            last = txt.strip()
            if "ok" in txt.lower():
                return {"channel": channel, "name": name, "ok": True,
                        "form": key, "raw": last[:80]}
        return {"channel": channel, "name": name, "ok": False, "raw": last[:80]}

    # ------------------------------------------------------------------ #
    # RTSP offline detection (the reliable, remote-safe path)            #
    # ------------------------------------------------------------------ #
    def probe_channel(self, channel: int, subtype: int = 0) -> ChannelStatus:
        """
        Probe ONE channel's RTSP stream THROUGH the NVR with ffprobe. Presence
        of a video track => channel ONLINE. Works remotely (RTSP hits the NVR,
        not the camera). Never raises — returns a status with a note on error.
        """
        _require_channel(channel)
        name, address = self._channel_meta(channel)
        try:
            fp = self._tool("ffprobe")
        except NvrError as e:
            return ChannelStatus(channel, name, address, False, False, False,
                                 "", note=str(e))
        url = self.cfg.rtsp_url(channel, subtype)
        cmd = [
            fp, "-v", "error",
            "-rw_timeout", str(RTSP_RW_TIMEOUT_US),
            "-rtsp_transport", "tcp",
            "-show_entries", "stream=codec_type,codec_name",
            "-of", "csv=p=0", url,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=RTSP_PROC_TIMEOUT)
        except subprocess.TimeoutExpired:
            return ChannelStatus(channel, name, address, False, False, False,
                                 "", note="probe timeout (no stream)")
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        has_v = any("video" in ln for ln in lines)
        has_a = any("audio" in ln for ln in lines)
        note = ""
        if not has_v and proc.stderr.strip():
            note = proc.stderr.strip().splitlines()[-1][:120]
        return ChannelStatus(
            channel=channel, name=name, address=address,
            online=has_v, has_video=has_v, has_audio=has_a,
            codecs=",".join(lines), note=note,
        )

    def camera_status(self, subtype: int = 0,
                      channels: list[int] | None = None) -> dict[str, Any]:
        """
        RTSP-probe every (or the given) channel in small batches to respect the
        ~4 concurrent RTSP session cap. Returns online/offline joined with
        channel number, title, and camera IP — the single view a human needs to
        see what is actually down.
        """
        cams = [c for c in self.list_cameras() if c.enabled]
        if channels is not None:
            wanted = set(channels)
            cams = [c for c in cams if c.channel in wanted]
        chans = [c.channel for c in cams]

        results: dict[int, ChannelStatus] = {}
        for batch in _chunk(chans, RTSP_BATCH):
            with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                for st in pool.map(lambda ch: self.probe_channel(ch, subtype), batch):
                    results[st.channel] = st

        rows = [results[ch].__dict__ for ch in sorted(results)]
        online = sum(1 for r in rows if r["online"])
        offline = [r["channel"] for r in rows if not r["online"]]
        return {
            "site": self.cfg.site,
            "total": len(rows),
            "online": online,
            "offline_count": len(offline),
            "offline_channels": offline,
            "verdict": "ALL ONLINE" if not offline else f"OFFLINE: {offline}",
            "channels": rows,
        }

    def _channel_meta(self, channel: int) -> tuple[str, str]:
        """Best-effort (name, address) for a channel; ('','') if unavailable."""
        try:
            for c in self.list_cameras():
                if c.channel == channel:
                    return c.name, c.address
        except NvrError:
            pass
        return f"Channel{channel}", ""

    # ------------------------------------------------------------------ #
    # Link quality / addressing faults                                  #
    # ------------------------------------------------------------------ #
    def link_quality(self, count: int = 10) -> dict[str, Any]:
        """
        Ping each camera IP for loss%/latency AND flag any camera whose IP is
        outside the expected block (lan_prefix + expected_ip_min..max). An
        off-scheme IP is an addressing-fault / IP-conflict red flag. TTL spread
        across cameras is surfaced as an optional IP-conflict hint.
        """
        cams = [c for c in self.list_cameras() if c.enabled and c.address]
        rows = []
        ttls: list[int] = []
        for c in cams:
            p = self.ping(c.address, count=count)
            off = self._is_off_scheme(c.address)
            if p.get("ttl") is not None:
                ttls.append(int(p["ttl"]))
            rows.append({
                "channel": c.channel,
                "name": c.name,
                "ip": c.address,
                "loss_pct": p["loss_pct"],
                "max_ms": p["max_ms"],
                "ttl": p.get("ttl"),
                "off_scheme_ip": off,
                "expected_block": f"{self.cfg.lan_prefix}."
                                  f"{self.cfg.expected_ip_min}-"
                                  f"{self.cfg.expected_ip_max}",
            })
        worst = max((r["loss_pct"] for r in rows), default=0)
        off_scheme = [r["ip"] for r in rows if r["off_scheme_ip"]]
        # Optional IP-conflict hint: mixed TTLs from same-subnet hosts can mean
        # replies are coming from different devices sharing an IP.
        ttl_conflict = len({t for t in ttls}) > 1 if ttls else False
        verdict = (
            "STABLE" if worst == 0 and not off_scheme else
            "ADDRESSING FAULT — off-scheme camera IP" if off_scheme else
            "MINOR LOSS (watch switch/uplink)" if worst <= 5 else
            "UNSTABLE (switch/PoE/cabling)"
        )
        return {
            "site": self.cfg.site,
            "verdict": verdict,
            "worst_loss_pct": worst,
            "off_scheme_ips": off_scheme,
            "ip_conflict_hint": ttl_conflict,
            "cameras": rows,
        }

    def _is_off_scheme(self, address: str) -> bool:
        """True if IP is not in lan_prefix.expected_min..expected_max."""
        if not address.startswith(self.cfg.lan_prefix + "."):
            return True
        try:
            last = int(address.rsplit(".", 1)[1])
        except (ValueError, IndexError):
            return True
        return not (self.cfg.expected_ip_min <= last <= self.cfg.expected_ip_max)

    # ------------------------------------------------------------------ #
    # Audio                                                              #
    # ------------------------------------------------------------------ #
    def audio_config(self) -> dict[int, dict[str, bool]]:
        """Per-channel AudioEnable for main & sub stream (1-based channel)."""
        enc = self._parse_table(
            self._get("/cgi-bin/configManager.cgi?action=getConfig&name=Encode")
        )
        out: dict[int, dict[str, bool]] = {}
        for k, v in enc.items():
            m = re.match(r"Encode\[(\d+)\]\.(Main|Extra)Format\[0\]\.AudioEnable", k)
            if m:
                ch = int(m.group(1)) + 1
                stream = "main" if m.group(2) == "Main" else "sub"
                out.setdefault(ch, {})[stream] = v.lower() == "true"
        return out

    def audio_audit(self) -> list[dict[str, Any]]:
        """Config audio flags joined with a live RTSP audio-track check."""
        cfg = self.audio_config()
        rows = []
        for ch in sorted(cfg):
            st = self.probe_channel(ch, subtype=0)
            rows.append({
                "channel": ch,
                "cfg_main_audio": cfg[ch].get("main"),
                "cfg_sub_audio": cfg[ch].get("sub"),
                "stream_has_audio": st.has_audio,
                "online": st.online,
            })
        return rows

    def enable_audio(self, channel: int) -> dict[str, Any]:
        """Force audio ON for main + sub stream of a channel (1-based)."""
        _require_channel(channel)
        idx = channel - 1
        path = (
            f"/cgi-bin/configManager.cgi?action=setConfig"
            f"&Encode[{idx}].MainFormat[0].AudioEnable=true"
            f"&Encode[{idx}].ExtraFormat[0].AudioEnable=true"
        )
        txt = self._get(path)
        ok = "ok" in txt.lower()
        return {"channel": channel, "ok": ok, "raw": txt.strip()[:80]}

    # ------------------------------------------------------------------ #
    # Encode / bandwidth                                                 #
    # ------------------------------------------------------------------ #
    def encode_config(self) -> dict[int, dict[str, str]]:
        """Per-channel main-stream bitrate/fps/resolution (1-based channel)."""
        enc = self._parse_table(
            self._get("/cgi-bin/configManager.cgi?action=getConfig&name=Encode")
        )
        out: dict[int, dict[str, str]] = {}
        rex = re.compile(r"Encode\[(\d+)\]\.MainFormat\[0\]\.Video\.(\w+)")
        for k, v in enc.items():
            m = rex.match(k)
            if m and m.group(2) in ("BitRate", "FPS", "Width", "Height",
                                    "Compression", "BitRateControl"):
                out.setdefault(int(m.group(1)) + 1, {})[m.group(2)] = v
        return out

    def bandwidth_audit(self) -> dict[str, Any]:
        """Per-channel encode + total load. NOTE: bandwidth is NOT the offline
        cause at this site — see the `note` field."""
        enc = self.encode_config()
        total = 0
        rows = []
        for ch in sorted(enc):
            br = int(enc[ch].get("BitRate", 0) or 0)
            total += br
            rows.append({
                "channel": ch,
                "bitrate_kbps": br,
                "control": enc[ch].get("BitRateControl"),
                "codec": enc[ch].get("Compression"),
                "fps": enc[ch].get("FPS"),
                "res": f"{enc[ch].get('Width')}x{enc[ch].get('Height')}",
            })
        peak = int(total * 2.2)  # H.265 I-frame burst factor
        risk = ("OK for 100M uplink" if peak < 70000 else
                "TIGHT on 100M — reduce bitrate/split uplink" if peak < 100000 else
                "OVER 100M — WILL DROP (daisy-chained = worse)")
        return {
            "total_avg_kbps": total,
            "est_peak_kbps": peak,
            "uplink_risk_100M": risk,
            "note": ("Encode is lean here (~2 Mbps VBR H.265). Low total load "
                     "does NOT explain a channel going offline — use "
                     "camera_status (RTSP) and link_quality for offline/faults."),
            "channels": rows,
        }

    def set_bitrate(self, channel: int, bitrate_kbps: int,
                    fps: int | None = None) -> dict[str, Any]:
        """Cap a channel's MAIN-stream bitrate (kbps) and optionally fps
        (1-based channel)."""
        _require_channel(channel)
        if bitrate_kbps <= 0:
            raise NvrError("bitrate_kbps must be > 0")
        idx = channel - 1
        parts = [f"Encode[{idx}].MainFormat[0].Video.BitRate={int(bitrate_kbps)}"]
        if fps is not None:
            if fps <= 0:
                raise NvrError("fps must be > 0 when provided")
            parts.append(f"Encode[{idx}].MainFormat[0].Video.FPS={int(fps)}")
        path = "/cgi-bin/configManager.cgi?action=setConfig&" + "&".join(parts)
        txt = self._get(path)
        ok = "ok" in txt.lower()
        return {"channel": channel, "bitrate_kbps": bitrate_kbps, "fps": fps,
                "ok": ok, "raw": txt.strip()[:80]}

    # ------------------------------------------------------------------ #
    # Snapshot (needs ffmpeg on PATH)                                    #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _tool(name: str) -> str:
        p = shutil.which(name)
        if not p:
            raise NvrError(f"{name} not found on PATH (install ffmpeg)")
        return p

    def snapshot(self, channel: int, out_path: str, subtype: int = 0) -> dict[str, Any]:
        """Grab one JPEG frame from a channel via RTSP (reliable path)."""
        _require_channel(channel)
        ff = self._tool("ffmpeg")
        url = self.cfg.rtsp_url(channel, subtype)
        cmd = [
            ff, "-nostdin", "-loglevel", "error",
            "-rtsp_transport", "tcp", "-timeout", str(RTSP_RW_TIMEOUT_US),
            "-i", url, "-frames:v", "1", "-update", "1",
            "-q:v", "3", "-y", out_path,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        except subprocess.TimeoutExpired:
            return {"channel": channel, "path": out_path, "bytes": 0,
                    "ok": False, "err": "snapshot timeout"}
        size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
        return {
            "channel": channel,
            "path": out_path,
            "bytes": size,
            "ok": size > 2000,
            "err": proc.stderr.strip()[:160],
        }

    # ------------------------------------------------------------------ #
    # Network diagnostics                                                #
    # ------------------------------------------------------------------ #
    def ping(self, ip: str, count: int = 4) -> dict[str, Any]:
        """Windows ping wrapper -> loss% + max latency + TTL. Never raises."""
        cmd = ["ping", "-n", str(count), "-w", "1000", ip]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=count * 2 + 10)
            out = proc.stdout
        except subprocess.TimeoutExpired:
            return {"ip": ip, "loss_pct": 100, "max_ms": None,
                    "ttl": None, "alive": False}
        loss_m = re.search(r"\((\d+)%\s*loss\)", out, re.I)
        max_m = re.search(r"Maximum\s*=\s*(\d+)ms", out, re.I)
        ttl_m = re.search(r"TTL=(\d+)", out, re.I)
        loss = int(loss_m.group(1)) if loss_m else 100
        return {
            "ip": ip,
            "loss_pct": loss,
            "max_ms": int(max_m.group(1)) if max_m else None,
            "ttl": int(ttl_m.group(1)) if ttl_m else None,
            "alive": loss < 100,
        }

    def scan_network(self, start: int = 1, end: int = 254) -> dict[str, Any]:
        """Ping-sweep the camera LAN subnet to find all live hosts."""
        alive = []
        for i in range(start, end + 1):
            ip = f"{self.cfg.lan_prefix}.{i}"
            if self.ping(ip, count=1)["alive"]:
                alive.append(ip)
        return {"subnet": f"{self.cfg.lan_prefix}.0/24",
                "alive": alive, "count": len(alive)}

    def reboot(self) -> dict[str, Any]:
        """Reboot the whole NVR (drops all recording briefly)."""
        txt = self._get("/cgi-bin/magicBox.cgi?action=reboot")
        return {"ok": "ok" in txt.lower(), "raw": txt.strip()[:80]}

    # ------------------------------------------------------------------ #
    # User management                                                    #
    # ------------------------------------------------------------------ #
    def list_users(self) -> list[dict[str, Any]]:
        """List all NVR users with name, group, and authority list."""
        txt = self._get("/cgi-bin/userManager.cgi?action=getUserInfoAll")
        users: dict[int, dict[str, str]] = {}
        for line in txt.splitlines():
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            # Keys: users[N].Name / users[N].Group / users[N].AuthorityList
            m = re.match(r"users\[(\d+)\]\.(\w+)", key)
            if m:
                idx = int(m.group(1))
                users.setdefault(idx, {})[m.group(2)] = val.strip()
        return [
            {"index": i, "name": d.get("Name", ""), "group": d.get("Group", ""),
             "authorities": d.get("AuthorityList", "")}
            for i, d in sorted(users.items())
        ]

    def create_remote_user(self, username: str, password: str) -> dict[str, Any]:
        """Create a viewer-level remote user. Skips if the user already exists."""
        try:
            existing = [u["name"] for u in self.list_users()]
            if username in existing:
                return {"created": False, "existed": True, "username": username, "ok": True}
        except NvrError:
            pass
        pw = quote(password, safe="")
        path = (
            f"/cgi-bin/userManager.cgi?action=addUser"
            f"&user.Name={username}&user.Password={pw}"
            f"&user.Group=user&user.AuthorityList=Monitor,Playback"
        )
        txt = self._get(path)
        ok = "ok" in txt.lower()
        return {"created": ok, "existed": False, "username": username,
                "ok": ok, "raw": txt.strip()[:120]}

    def change_admin_password(
        self, old_password: str, new_password: str
    ) -> "tuple[bool, NvrClient]":
        """
        Change the admin password. Returns (success, new_client).
        If successful, the returned NvrClient is re-authenticated with the new password.
        The original client becomes invalid — use the returned one for all subsequent calls.
        """
        old_enc = quote(old_password, safe="")
        new_enc = quote(new_password, safe="")

        # Try primary path
        ok = False
        for path in [
            f"/cgi-bin/userManager.cgi?action=modifyPassword&name=admin&pwd={new_enc}&oldPwd={old_enc}",
            f"/cgi-bin/userManager.cgi?action=modifyUser&user.Name=admin&user.Password={new_enc}&user.OldPassword={old_enc}",
        ]:
            try:
                txt = self._get(path)
                if "ok" in txt.lower():
                    ok = True
                    break
            except NvrError:
                continue

        if not ok:
            return False, self

        # Build new client with updated password and verify it works
        new_cfg = NvrConfig(
            host=self.cfg.host,
            username=self.cfg.username,
            password=new_password,
            http_port=self.cfg.http_port,
            rtsp_port=self.cfg.rtsp_port,
            site=self.cfg.site,
            lan_prefix=self.cfg.lan_prefix,
            expected_ip_min=self.cfg.expected_ip_min,
            expected_ip_max=self.cfg.expected_ip_max,
        )
        new_client = NvrClient(new_cfg)
        try:
            new_client.device_info()
            return True, new_client
        except NvrError:
            return False, self

    # ------------------------------------------------------------------ #
    # Encode: sub-stream + codec setter                                  #
    # ------------------------------------------------------------------ #
    def set_substream_encode(
        self, channel: int, bitrate_kbps: int = 512, fps: int = 10
    ) -> dict[str, Any]:
        """Set sub-stream (ExtraFormat[0]) to H.265 VBR for remote/mobile viewing."""
        _require_channel(channel)
        idx = channel - 1
        path = (
            f"/cgi-bin/configManager.cgi?action=setConfig"
            f"&Encode[{idx}].ExtraFormat[0].Video.Compression=H.265"
            f"&Encode[{idx}].ExtraFormat[0].Video.BitRate={int(bitrate_kbps)}"
            f"&Encode[{idx}].ExtraFormat[0].Video.FPS={int(fps)}"
            f"&Encode[{idx}].ExtraFormat[0].Video.BitRateControl=VBR"
        )
        txt = self._get(path)
        ok = "ok" in txt.lower()
        return {"channel": channel, "bitrate_kbps": bitrate_kbps, "fps": fps,
                "codec": "H.265", "ok": ok, "raw": txt.strip()[:80]}

    def set_bitrate_and_codec(
        self, channel: int, bitrate_kbps: int, fps: int, codec: str = "H.265"
    ) -> dict[str, Any]:
        """Set main stream codec + bitrate + fps + VBR in one CGI call."""
        _require_channel(channel)
        idx = channel - 1
        path = (
            f"/cgi-bin/configManager.cgi?action=setConfig"
            f"&Encode[{idx}].MainFormat[0].Video.Compression={codec}"
            f"&Encode[{idx}].MainFormat[0].Video.BitRate={int(bitrate_kbps)}"
            f"&Encode[{idx}].MainFormat[0].Video.FPS={int(fps)}"
            f"&Encode[{idx}].MainFormat[0].Video.BitRateControl=VBR"
        )
        txt = self._get(path)
        ok = "ok" in txt.lower()
        return {"channel": channel, "bitrate_kbps": bitrate_kbps, "fps": fps,
                "codec": codec, "ok": ok, "raw": txt.strip()[:80]}

    def set_continuous_record(self, channel: int) -> dict[str, Any]:
        """Set one channel to continuous (manual) 24/7 recording. Mode=1 on Dahua."""
        _require_channel(channel)
        idx = channel - 1
        path = f"/cgi-bin/configManager.cgi?action=setConfig&RecordMode[{idx}].Mode=1"
        txt = self._get(path)
        ok = "ok" in txt.lower()
        return {"channel": channel, "mode": "continuous", "ok": ok, "raw": txt.strip()[:80]}

    # ------------------------------------------------------------------ #
    # DDNS                                                               #
    # ------------------------------------------------------------------ #
    def configure_ddns(
        self, domain: str, provider: str = "selfddns.quickddns.com"
    ) -> dict[str, Any]:
        """Enable Dahua QuickDDNS with a chosen subdomain."""
        path = (
            f"/cgi-bin/configManager.cgi?action=setConfig"
            f"&DDNS[0].Enable=true"
            f"&DDNS[0].HostName={provider}"
            f"&DDNS[0].Domain={quote(domain, safe='')}"
            f"&DDNS[0].UpdatePeriod=5"
        )
        txt = self._get(path)
        ok = "ok" in txt.lower()
        # Read back to confirm
        readback = self.get_ddns_status()
        return {
            "ok": ok,
            "domain": domain,
            "provider": provider,
            "enabled": readback.get("enabled", False),
            "readback_domain": readback.get("domain", ""),
        }

    def get_ddns_status(self) -> dict[str, Any]:
        """Read current DDNS configuration from the NVR."""
        t = self._get_safe("/cgi-bin/configManager.cgi?action=getConfig&name=DDNS")
        return {
            "enabled": t.get("DDNS[0].Enable", "false").lower() == "true",
            "domain": t.get("DDNS[0].Domain", ""),
            "provider": t.get("DDNS[0].HostName", ""),
            "update_period": t.get("DDNS[0].UpdatePeriod", ""),
        }

    # ------------------------------------------------------------------ #
    # Network discovery (static method — no NVR connection needed)       #
    # ------------------------------------------------------------------ #
    @staticmethod
    def discover_nvr(subnet: str = "") -> list[dict[str, Any]]:
        """
        Find Dahua NVR devices on the local network.
        Strategy: UDP broadcast (37810) → ARP table → common defaults → TCP sweep.
        """
        import socket
        import struct
        from concurrent.futures import ThreadPoolExecutor, as_completed

        found: dict[str, dict] = {}  # ip -> result dict

        def _confirm(ip: str, method: str) -> dict[str, Any]:
            """Try magicBox.cgi to confirm a Dahua device and get its type."""
            try:
                import urllib.request
                url = f"http://{ip}/cgi-bin/magicBox.cgi?action=getDeviceType"
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=2) as r:
                    body = r.read(200).decode(errors="ignore")
                    dev_type = ""
                    for line in body.splitlines():
                        if "=" in line:
                            dev_type = line.partition("=")[2].strip()
                    return {"ip": ip, "method": method, "device_type": dev_type,
                            "confirmed": bool(dev_type)}
            except Exception:
                return {"ip": ip, "method": method, "device_type": "", "confirmed": False}

        # --- Step 1: UDP broadcast on port 37810 ---
        try:
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            udp.settimeout(2.0)
            payload = b'{"method":"GET","params":{"DahuaNetIPSearch":{}},"id":1}'
            udp.sendto(payload, ("255.255.255.255", 37810))
            # Also try older binary discovery header
            binary_probe = b"\x20\x00\x00\x00" + b"\x00" * 16
            udp.sendto(binary_probe, ("255.255.255.255", 37810))
            import time
            deadline = time.time() + 2.0
            while time.time() < deadline:
                try:
                    data, addr = udp.recvfrom(4096)
                    ip = addr[0]
                    if ip not in found:
                        found[ip] = {"ip": ip, "method": "udp_37810",
                                     "device_type": "", "confirmed": False}
                except socket.timeout:
                    break
                except Exception:
                    break
            udp.close()
        except Exception:
            pass

        # --- Step 2: ARP table scan ---
        DAHUA_OUIS = {"bc:ad:28", "3c:ef:8c", "e8:9f:ec", "4c:11:bf",
                      "a4:14:37", "90:02:a9", "d4:a6:51", "bc:32:b2"}
        try:
            import subprocess as sp
            result = sp.run(["arp", "-a"], capture_output=True, text=True, timeout=5)
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) < 2:
                    continue
                ip_part = parts[0].strip("()")
                mac_part = parts[1].lower() if len(parts) > 1 else ""
                prefix = ":".join(mac_part.replace("-", ":").split(":")[:3])
                if prefix in DAHUA_OUIS and ip_part not in found:
                    found[ip_part] = {"ip": ip_part, "method": "arp_dahua_mac",
                                      "device_type": "", "confirmed": False}
        except Exception:
            pass

        # --- Step 3: Common Dahua default IPs ---
        defaults = ["192.168.1.108", "192.168.0.108", "10.0.0.108", "172.16.0.108"]
        if subnet:
            defaults.append(f"{subnet}.108")
        for ip in defaults:
            if ip not in found:
                found[ip] = {"ip": ip, "method": "common_default",
                             "device_type": "", "confirmed": False}

        # --- Step 4: TCP port 80 sweep of provided subnet ---
        if subnet:
            candidates: list[str] = []
            for i in range(1, 255):
                ip = f"{subnet}.{i}"
                if ip not in found:
                    candidates.append(ip)

            def _tcp_probe(ip: str) -> str | None:
                try:
                    s = socket.create_connection((ip, 80), timeout=0.3)
                    s.close()
                    return ip
                except Exception:
                    return None

            with ThreadPoolExecutor(max_workers=50) as pool:
                for ip in pool.map(_tcp_probe, candidates):
                    if ip and ip not in found:
                        found[ip] = {"ip": ip, "method": "tcp_sweep",
                                     "device_type": "", "confirmed": False}

        # --- Confirm all candidates via magicBox.cgi ---
        results: list[dict] = []
        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = {pool.submit(_confirm, entry["ip"], entry["method"]): entry["ip"]
                       for entry in found.values()}
            for fut in as_completed(futures):
                r = fut.result()
                if r["confirmed"] or found[futures[fut]]["method"] in (
                        "udp_37810", "arp_dahua_mac"):
                    results.append(r)

        results.sort(key=lambda x: (not x["confirmed"], x["ip"]))
        return results

    # ------------------------------------------------------------------ #
    # One-shot full audit                                               #
    # ------------------------------------------------------------------ #
    def full_audit(self) -> dict[str, Any]:
        """Everything a human needs in one call: identity, network, storage,
        record mode, per-channel encode, RTSP camera_status, link_quality,
        audio audit. Each section fails soft with an error string."""
        def _soft(fn):
            try:
                return fn()
            except NvrError as e:
                return {"error": str(e)}

        return {
            "site": self.cfg.site,
            "device": _soft(self.device_info),
            "network": _soft(self.network_config),
            "storage": _soft(self.storage_state),
            "record_mode": _soft(self.record_mode),
            "encode": _soft(self.bandwidth_audit),
            "camera_status": _soft(self.camera_status),
            "link_quality": _soft(self.link_quality),
            "audio": _soft(self.audio_audit),
        }


# ---------------------------------------------------------------------- #
# Module helpers                                                          #
# ---------------------------------------------------------------------- #
def _require_channel(channel: int) -> None:
    if not isinstance(channel, int) or channel < 1:
        raise NvrError(f"channel must be a positive integer, got {channel!r}")


def _to_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _chunk(seq: list[int], size: int) -> list[list[int]]:
    return [seq[i:i + size] for i in range(0, len(seq), max(1, size))]
