from dotenv import load_dotenv
load_dotenv()
from nvr_client import NvrClient

print("Scanning 192.168.1 subnet for NVR devices...")
results = NvrClient.discover_nvr(subnet="192.168.1")
if results:
    for r in results:
        status = "CONFIRMED NVR" if r["confirmed"] else "candidate"
        print(f"  [{status}]  IP={r['ip']}  via={r['method']}  type={r['device_type']}")
else:
    print("  No devices found on 192.168.1.x")
    print("  Trying common Dahua defaults only...")
    results2 = NvrClient.discover_nvr(subnet="")
    for r in results2:
        status = "CONFIRMED NVR" if r["confirmed"] else "candidate"
        print(f"  [{status}]  IP={r['ip']}  via={r['method']}  type={r['device_type']}")
