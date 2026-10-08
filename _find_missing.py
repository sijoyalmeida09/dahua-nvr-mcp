"""Find the missing camera — probe .10, .11, .19 with all known protocols."""
import requests, socket, re
from requests.auth import HTTPDigestAuth

CANDIDATES = {"192.168.1.10": "40-62-31 (Dahua OUI)",
              "192.168.1.11": "5c-fc-e1 (HTTP 401 earlier)",
              "192.168.1.19": "ba-24-fe (NEW - not seen before)"}

PASSWORDS = ["Admin@135", "admin", "12345", "123456", ""]
USERS     = ["admin", "Admin"]

def xml_val(text, tag):
    m = re.search(rf"<(?:[^:>]+:)?{tag}>([^<]*)<", text)
    return m.group(1).strip() if m else "?"

for ip, note in CANDIDATES.items():
    print(f"\n{'='*55}")
    print(f"{ip}  [{note}]")

    # Port scan first
    open_ports = []
    for port in [80, 443, 554, 8080, 37777, 8000]:
        s = socket.socket(); s.settimeout(1.2)
        if s.connect_ex((ip, port)) == 0:
            open_ports.append(port)
        s.close()
    print(f"  Open ports: {open_ports}")

    found = False
    for user in USERS:
        for pw in PASSWORDS:
            auth = HTTPDigestAuth(user, pw)
            # Try ISAPI
            try:
                r = requests.get(f"http://{ip}/ISAPI/System/deviceInfo",
                                 auth=auth, timeout=4)
                if r.status_code == 200:
                    model   = xml_val(r.text, "model")
                    dtype   = xml_val(r.text, "deviceType")
                    fw      = xml_val(r.text, "firmwareVersion")
                    sn      = xml_val(r.text, "serialNumber")
                    print(f"  ISAPI auth={user}/{pw}: model={model} type={dtype} fw={fw} sn={sn[:16]}..")
                    found = True
                    break
            except Exception:
                pass
            # Try Dahua CGI
            try:
                r = requests.get(f"http://{ip}/cgi-bin/magicBox.cgi?action=getSystemInfo",
                                 auth=auth, timeout=4)
                if r.status_code == 200 and "serialNumber" in r.text:
                    print(f"  Dahua CGI auth={user}/{pw}: {r.text[:150]}")
                    found = True
                    break
            except Exception:
                pass
        if found:
            break

    if not found:
        # Try root page
        for port in open_ports:
            try:
                r = requests.get(f"http://{ip}:{port}/",
                                 auth=HTTPDigestAuth("admin", "Admin@135"),
                                 timeout=4)
                print(f"  HTTP:{port} -> {r.status_code} | {r.text[:150]}")
            except Exception as e:
                print(f"  HTTP:{port} -> {e}")

print("\nDone.")
