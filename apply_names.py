"""One-shot: apply a channel-name map to the NVR and verify each write.

Uses NvrClient (requests + retry/backoff) so it survives the NVR's setConfig
rate-limiting. Re-runnable: only writes channels whose title differs.

Edit NAMES below to match your camera layout, then run:
    python apply_names.py
"""
from __future__ import annotations

import time

from nvr_client import NvrClient, NvrConfig, NvrError

# channel (1-based) -> display name (scene-derived)
NAMES = {
    1: "Front Counter",
    2: "Walk-in Cooler",
    3: "Register POS",
    4: "Pickup Counter",
    5: "Exterior Street",
    6: "Exterior Corner",
    7: "Kitchen Line",
    8: "Prep Dishwash",
    9: "Exterior Side St",
    10: "Make Table",
    11: "Oven Dough",
    12: "Exterior Entrance",
    13: "Dining Area",
    14: "Prep Pickup",
    15: "Back Door Prep",
    16: "Exterior Backyard",
}


def main() -> None:
    client = NvrClient(NvrConfig.from_env())
    current = client.get_channel_names()
    print("current -> target")
    for ch in sorted(NAMES):
        tgt = NAMES[ch]
        cur = current.get(ch, "?")
        if cur.strip() == tgt:
            print(f"ch{ch:<2} already '{tgt}' (skip)")
            continue
        for attempt in range(1, 5):
            try:
                res = client.set_channel_name(ch, tgt)
                if res.get("ok"):
                    print(f"ch{ch:<2} '{cur}' -> '{tgt}'  OK")
                    break
                print(f"ch{ch:<2} attempt {attempt}: {res.get('raw')}")
            except NvrError as e:
                print(f"ch{ch:<2} attempt {attempt} error: {e}")
            time.sleep(3.0 * attempt)
        time.sleep(2.5)  # pace between channels (device rate-limits setConfig)

    print("\n=== FINAL VERIFY ===")
    final = client.get_channel_names()
    ok = 0
    for ch in sorted(NAMES):
        got = final.get(ch, "?").strip()
        mark = "OK" if got == NAMES[ch] else "MISMATCH"
        if got == NAMES[ch]:
            ok += 1
        print(f"ch{ch:<2} = '{got}'  [{mark}]")
    print(f"\n{ok}/{len(NAMES)} channels named correctly.")


if __name__ == "__main__":
    main()
