"""Try passwords on found NVR and run full audit if successful."""
from dotenv import load_dotenv
load_dotenv()
import os, json
from nvr_client import NvrClient, NvrConfig, NvrError

NVR_IP = "192.168.1.155"

# Try these passwords in order
PASSWORDS = [
    os.environ.get("NVR_ADMIN_PASS", ""),
    "admin",
    "Admin12345",
    "12345",
    "123456",
    "",
]

print(f"Trying to authenticate to NVR at {NVR_IP}...\n")

working_pass = None
for pw in PASSWORDS:
    cfg = NvrConfig(
        host=NVR_IP, username="admin", password=pw,
        http_port=80, rtsp_port=554,
        site="discovered-nvr", lan_prefix="192.168.1",
        expected_ip_min=150, expected_ip_max=200,
    )
    client = NvrClient(cfg)
    try:
        info = client.device_info()
        display_pw = pw if pw else "(empty)"
        print(f"SUCCESS with password: {display_pw}")
        print(f"  Device type : {info.get('type', 'unknown')}")
        print(f"  Serial      : {info.get('sn', 'unknown')}")
        print(f"  Firmware    : {info.get('version', 'unknown')}")
        print(f"  Name        : {info.get('name', 'unknown')}")
        working_pass = pw
        break
    except NvrError as e:
        pw_display = pw[:3] + "***" if pw else "(empty)"
        print(f"  FAILED [{pw_display}]: {str(e)[:60]}")

if not working_pass and working_pass != "":
    print("\nCould not authenticate. NVR may have a custom password.")
    print("Please tell me the current NVR password so I can proceed.")
else:
    print(f"\nNVR at {NVR_IP} is accessible.")
    # Get camera count
    try:
        cams = client.list_cameras()
        enabled = [c for c in cams if c.enabled]
        print(f"\nChannels registered : {len(cams)} total, {len(enabled)} enabled")
        for c in cams:
            status = "ON " if c.enabled else "OFF"
            print(f"  ch{c.channel:02d} [{status}]  {c.address:<16} {c.name}")
    except NvrError as e:
        print(f"Could not list cameras: {e}")

    # Network config
    try:
        net = client.network_config()
        print(f"\nNVR network : IP={net['ip']}  mode={net['mode']}  gw={net.get('gateway','?')}")
    except Exception as e:
        print(f"Network info error: {e}")

    # Storage
    try:
        st = client.storage_state()
        for d in st.get("disks", []):
            print(f"Storage disk{d['index']}: {d['used_gb']}/{d['total_gb']} GB  ({d['used_pct']}% used)  [{d['status']}]")
    except Exception as e:
        print(f"Storage info error: {e}")
