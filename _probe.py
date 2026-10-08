"""Probe all ARP-table IPs for Dahua NVR response."""
from dotenv import load_dotenv
load_dotenv()

import urllib.request
import urllib.error
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed

CANDIDATES = [
    "192.168.1.152",
    "192.168.1.155",
    "192.168.1.157",
    "192.168.1.159",
    "192.168.1.166",
    "192.168.1.172",
    "192.168.1.173",
    "192.168.1.174",
    "192.168.1.175",
    "192.168.1.177",
    "192.168.1.178",
    "192.168.1.179",
    "192.168.1.184",
]

PORTS = [80, 8080, 8000, 85, 8888]

def probe_ip(ip):
    results = []
    for port in PORTS:
        # TCP connect first
        try:
            s = socket.create_connection((ip, port), timeout=1.5)
            s.close()
            port_open = True
        except Exception:
            port_open = False

        if not port_open:
            continue

        # Try Dahua CGI (unauthenticated — returns device type or 401)
        for path in [
            "/cgi-bin/magicBox.cgi?action=getDeviceType",
            "/cgi-bin/magicBox.cgi?action=getSoftwareVersion",
        ]:
            url = f"http://{ip}:{port}{path}"
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=2) as r:
                    body = r.read(300).decode(errors="ignore")
                    results.append({"ip": ip, "port": port, "path": path,
                                    "status": r.status, "body": body.strip()[:120]})
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    results.append({"ip": ip, "port": port, "path": path,
                                    "status": 401, "body": "AUTH REQUIRED (Dahua digest auth — device found!)"})
            except Exception:
                pass
    return results

print(f"Probing {len(CANDIDATES)} candidates on ports {PORTS}...\n")
all_results = []
with ThreadPoolExecutor(max_workers=20) as pool:
    futures = {pool.submit(probe_ip, ip): ip for ip in CANDIDATES}
    for fut in as_completed(futures):
        res = fut.result()
        if res:
            all_results.extend(res)

if all_results:
    print("RESPONSIVE DEVICES:")
    seen = set()
    for r in sorted(all_results, key=lambda x: x["ip"]):
        key = (r["ip"], r["port"])
        if key not in seen:
            seen.add(key)
            print(f"\n  {r['ip']}:{r['port']}")
        print(f"    HTTP {r['status']}  {r['path']}")
        print(f"    Body: {r['body']}")
else:
    print("No Dahua devices responded on any tested port.")
    print("\nAll ARP entries with open port 80:")
    for ip in CANDIDATES:
        try:
            s = socket.create_connection((ip, 80), timeout=1.0)
            s.close()
            print(f"  {ip}:80 is OPEN")
        except Exception:
            pass
