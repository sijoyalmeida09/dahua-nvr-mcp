"""NVR doctor — one-shot health report for a CCTV site.

Run:  python doctor.py            (uses .env / process env)

Prints the full_audit output as a clean report: device identity, network config,
storage, per-channel record mode + encode, RTSP-based ONLINE/OFFLINE status,
link quality (with off-scheme IP flags), and an audio audit.

Offline detection is RTSP-probe based (through the NVR), NOT ICMP-to-camera, so
it works even when run remotely (camera IPs are often unroutable). Safe /
read-only — makes no config changes.
"""
from __future__ import annotations

from typing import Any

from nvr_client import NvrClient, NvrConfig


def _section(title: str) -> None:
    print(f"\n--- {title} ---")


def _kv(data: dict[str, Any]) -> None:
    if "error" in data:
        print(f"  error: {data['error']}")
        return
    for k, v in data.items():
        print(f"  {k:12}: {v}")


def main() -> None:
    cfg = NvrConfig.from_env()
    client = NvrClient(cfg)

    print("=" * 64)
    print(f"CCTV DOCTOR — site '{cfg.site}' @ {cfg.host}")
    print("=" * 64)

    audit = client.full_audit()

    _section("DEVICE")
    _kv(audit["device"])

    _section("NETWORK")
    _kv(audit["network"])

    _section("STORAGE")
    st = audit["storage"]
    if st.get("error"):
        print(f"  error: {st['error']}")
    else:
        print(f"  total: {st.get('total_gb')} GB   used: {st.get('used_pct')}%")
        for d in st.get("disks", []):
            print(f"  disk{d['index']} [{d['status']}] "
                  f"{d['used_gb']}/{d['total_gb']} GB ({d['used_pct']}%)")

    _section("CAMERA STATUS (RTSP probe through NVR)")
    cs = audit["camera_status"]
    if cs.get("error"):
        print(f"  error: {cs['error']}")
    else:
        rm = audit.get("record_mode", {})
        for c in cs.get("channels", []):
            flag = "" if c["online"] else "  <-- OFFLINE"
            mode = rm.get(str(c["channel"]), rm.get(c["channel"], ""))
            note = f"  ({c['note']})" if c.get("note") and not c["online"] else ""
            print(f"  ch{c['channel']:<2} {c['name']:<18} {c['address']:<14} "
                  f"{'ONLINE ' if c['online'] else 'OFFLINE'} "
                  f"rec={mode}{flag}{note}")
        print(f"  VERDICT: {cs.get('verdict')}")

    _section("LINK QUALITY (ping + addressing)")
    lq = audit["link_quality"]
    if lq.get("error"):
        print(f"  error: {lq['error']}")
    else:
        for c in lq.get("cameras", []):
            off = "  <-- OFF-SCHEME IP" if c["off_scheme_ip"] else ""
            loss = "  <-- LOSS" if c["loss_pct"] else ""
            print(f"  ch{c['channel']:<2} {c['ip']:<14} loss={c['loss_pct']:>3}%  "
                  f"max={c['max_ms']}ms  ttl={c['ttl']}{loss}{off}")
        print(f"  VERDICT: {lq.get('verdict')} "
              f"(worst loss {lq.get('worst_loss_pct')}%)")
        if lq.get("off_scheme_ips"):
            print(f"  OFF-SCHEME IPs: {lq['off_scheme_ips']}")
        if lq.get("ip_conflict_hint"):
            print("  IP-CONFLICT HINT: mixed TTLs across cameras")

    _section("ENCODE / BANDWIDTH")
    bw = audit["encode"]
    if bw.get("error"):
        print(f"  error: {bw['error']}")
    else:
        print(f"  total avg: {bw.get('total_avg_kbps')} kbps   "
              f"est peak: {bw.get('est_peak_kbps')} kbps   "
              f"risk: {bw.get('uplink_risk_100M')}")
        print(f"  NOTE: {bw.get('note')}")

    _section("AUDIO AUDIT")
    au = audit["audio"]
    if isinstance(au, dict) and au.get("error"):
        print(f"  error: {au['error']}")
    else:
        for r in au:
            print(f"  ch{r['channel']:<2} cfg_main={r['cfg_main_audio']!s:<5} "
                  f"stream_audio={r['stream_has_audio']!s:<5} "
                  f"online={r['online']}")


if __name__ == "__main__":
    main()
