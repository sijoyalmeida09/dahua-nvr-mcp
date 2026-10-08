"""Fleet registry — load sites.json, switch active NVR site at runtime."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from nvr_client import NvrClient, NvrConfig


@dataclass(frozen=True)
class SiteEntry:
    key: str
    label: str
    host: str
    ddns_host: str
    ddns_port: int
    http_port: int
    rtsp_port: int
    lan_prefix: str
    expected_ip_min: int
    expected_ip_max: int
    model: str
    channels: int
    admin_pass_env: str
    remote_pass_env: str


class FleetManager:
    def __init__(self, sites_path: str):
        self._path = sites_path
        self._data: dict[str, Any] = {}
        self._active_key: str = ""
        self._active_mode: str = "lan"
        self._load()

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #
    def list_sites(self) -> list[dict]:
        sites = self._data.get("sites", {})
        return [
            {
                "key": k,
                "label": v.get("label", k),
                "host": v.get("host", ""),
                "ddns_host": v.get("ddns_host", ""),
                "model": v.get("model", ""),
                "channels": v.get("channels", 0),
                "active": k == self._active_key,
                "mode": self._active_mode if k == self._active_key else "—",
            }
            for k, v in sites.items()
        ]

    def active_site(self) -> SiteEntry:
        return self._entry(self._active_key)

    def switch_site(self, key: str, mode: str = "lan") -> None:
        sites = self._data.get("sites", {})
        if key not in sites:
            raise KeyError(f"Site '{key}' not found. Available: {list(sites)}")
        if mode not in ("lan", "remote"):
            raise ValueError("mode must be 'lan' or 'remote'")
        self._active_key = key
        self._active_mode = mode
        self._data["active"] = key

    def add_site(
        self,
        key: str,
        label: str,
        host: str,
        ddns_host: str = "",
        ddns_port: int = 80,
        http_port: int = 80,
        rtsp_port: int = 554,
        lan_prefix: str = "",
        expected_ip_range: str = "50-64",
        model: str = "Dahua",
        channels: int = 16,
    ) -> None:
        if not lan_prefix:
            parts = host.rsplit(".", 1)
            lan_prefix = parts[0] if len(parts) == 2 else host
        self._data.setdefault("sites", {})[key] = {
            "label": label,
            "host": host,
            "ddns_host": ddns_host,
            "ddns_port": ddns_port,
            "http_port": http_port,
            "rtsp_port": rtsp_port,
            "lan_prefix": lan_prefix,
            "expected_ip_range": expected_ip_range,
            "model": model,
            "channels": channels,
            "admin_pass_env": "NVR_ADMIN_PASS",
            "remote_pass_env": "NVR_REMOTE_PASS",
        }
        self._save()

    def build_config(self, mode: str | None = None) -> NvrConfig:
        mode = mode or self._active_mode
        entry = self.active_site()
        admin_pass = os.environ.get(entry.admin_pass_env, os.environ.get("NVR_PASS", ""))
        host = entry.ddns_host if (mode == "remote" and entry.ddns_host) else entry.host
        port = entry.ddns_port if (mode == "remote" and entry.ddns_host) else entry.http_port
        return NvrConfig(
            host=host,
            username="admin",
            password=admin_pass,
            http_port=port,
            rtsp_port=entry.rtsp_port,
            site=entry.key,
            lan_prefix=entry.lan_prefix,
            expected_ip_min=entry.expected_ip_min,
            expected_ip_max=entry.expected_ip_max,
        )

    def build_client(self, mode: str | None = None) -> NvrClient:
        return NvrClient(self.build_config(mode))

    # ------------------------------------------------------------------ #
    # Internal                                                            #
    # ------------------------------------------------------------------ #
    def _entry(self, key: str) -> SiteEntry:
        sites = self._data.get("sites", {})
        if key not in sites:
            raise KeyError(f"Active site '{key}' missing from sites.json")
        v = sites[key]
        lo, hi = _parse_range(v.get("expected_ip_range", "50-64"))
        return SiteEntry(
            key=key,
            label=v.get("label", key),
            host=v.get("host", ""),
            ddns_host=v.get("ddns_host", ""),
            ddns_port=int(v.get("ddns_port", 80)),
            http_port=int(v.get("http_port", 80)),
            rtsp_port=int(v.get("rtsp_port", 554)),
            lan_prefix=v.get("lan_prefix", ""),
            expected_ip_min=lo,
            expected_ip_max=hi,
            model=v.get("model", "Dahua"),
            channels=int(v.get("channels", 16)),
            admin_pass_env=v.get("admin_pass_env", "NVR_ADMIN_PASS"),
            remote_pass_env=v.get("remote_pass_env", "NVR_REMOTE_PASS"),
        )

    def _load(self) -> None:
        if os.path.exists(self._path):
            with open(self._path, encoding="utf-8") as f:
                self._data = json.load(f)
        else:
            # Fall back to env-based single site (backward compatible)
            self._data = _env_fallback_data()
        self._active_key = self._data.get("active", next(iter(self._data.get("sites", {})), ""))
        self._active_mode = "lan"

    def _save(self) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp, self._path)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _parse_range(raw: str) -> tuple[int, int]:
    import re
    m = re.match(r"\s*(\d+)\s*-\s*(\d+)\s*$", raw or "")
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if 0 <= lo <= hi <= 255:
            return lo, hi
    return 50, 64


def _env_fallback_data() -> dict:
    """Build a minimal sites.json structure from environment variables."""
    host = os.environ.get("NVR_HOST", "192.168.1.100")
    site = os.environ.get("NVR_SITE", "default")
    return {
        "active": site,
        "sites": {
            site: {
                "label": site,
                "host": host,
                "ddns_host": "",
                "ddns_port": 80,
                "http_port": int(os.environ.get("NVR_HTTP_PORT", 80)),
                "rtsp_port": int(os.environ.get("NVR_RTSP_PORT", 554)),
                "lan_prefix": os.environ.get("NVR_LAN_PREFIX", host.rsplit(".", 1)[0]),
                "expected_ip_range": os.environ.get("NVR_EXPECTED_IP_RANGE", "50-64"),
                "model": "Dahua",
                "channels": 16,
                "admin_pass_env": "NVR_PASS",
                "remote_pass_env": "NVR_REMOTE_PASS",
            }
        },
    }
