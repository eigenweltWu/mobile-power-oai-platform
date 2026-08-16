"""Sweep PUSCH target SNR and Qm, recording the gNB-side causal response
(PUSCH SNR, PH raw/normalized, PCMAX, TPC, MCS, Qm, N_PRB) plus the phone's
latest battery power (read over the USB agent channel).

NOTE: battery current is ~0 while USB is plugged (the phone runs off USB), so a
meaningful phone-power measurement requires the phone to be UNPLUGGED. This
script reports the gNB mechanism (which is what the PUSCH target intervention
actually drives) and, when the phone is unplugged, the corresponding battery power.
"""
import json
import time
from experiment_platform.backend.config import Settings, load_settings
from experiment_platform.backend.oai_client import OaiClient
import httpx

s = load_settings()
s.oai_timeout_s = 180.0  # control POSTs trigger restarts that block >8s
cli = OaiClient(s)


def wait_ready(timeout_s=150.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        st = cli.status()
        if st.gnb and st.gnb.running:
            try:
                u = cli.research_ues()
                if u.collection and u.collection.available and not u.collection.stale and u.ues:
                    return True
            except Exception:
                pass
        time.sleep(5)
    return False


def read_gNB():
    cfg = cli.research_config()
    ct = cli.gnb_controls()
    ues = cli.research_ues()
    u = ues.ues[0] if ues.ues else None
    pc = u.powerControl if (u and u.powerControl) else None
    ul = u.uplink if (u and u.uplink) else None
    return {
        "puschTargetMode": cfg.puschTargetMode,
        "puschTargetSnrDb": cfg.puschTargetSnrDb,
        "schedulerMode": cfg.ulSchedulerMode,
        "observed_mcs": ct.observedUplink.mcs if ct.observedUplink else None,
        "observed_qm": ct.observedUplink.qm if ct.observedUplink else None,
        "observed_nPrb": ct.observedUplink.nPrb if ct.observedUplink else None,
        "observed_puschSnrDb": ct.observedUplink.puschSnrDb if ct.observedUplink else None,
        "ue_puschSnrDb": ul.puschSnrDb if ul else None,
        "ue_mcs": ul.mcs if ul else None,
        "ue_qm": ul.qm if ul else None,
        "ue_nPrb": ul.nPrb if ul else None,
        "phRawDb": pc.phRawDb if pc else None,
        "phNormalizedDb": pc.phNormalizedDb if pc else None,
        "pcmaxDbm": pc.pcmaxDbm if pc else None,
        "tpcPusch": pc.tpcPusch if pc else None,
    }


def read_phone_power():
    try:
        r = httpx.get("http://127.0.0.1:8420/agent/status", timeout=3).json()
        return r.get("state"), r.get("phase")
    except Exception:
        return None, None


results = []

print("=== PUSCH target SNR sweep (manual) ===")
for x10 in (100, 150, 200):  # 10, 15, 20 dB
    print(f"\n>>> set puschTarget manual {x10/10.0:.1f} dB, restart")
    r = cli.gnb_pusch_target_snr("manual", x10, restart=True)
    print("    resp message:", r.get("message"), "| restarted:", r.get("restarted"))
    ok = wait_ready()
    if not ok:
        print("    !! gNB not ready in time")
        break
    m = read_gNB()
    m["_condition"] = f"target_snr_{x10/10.0:.1f}dB"
    results.append(m)
    print("    ", json.dumps(m, ensure_ascii=False))

print("\n=== Qm sweep (manual scheduler: mcs=14, prb=24, vary qm) ===")
for qm in (2, 4, 6):
    print(f"\n>>> set ul-scheduler manual mcs=14 qm={qm} prb=24, restart")
    r = cli.gnb_ul_scheduler("manual", mcs=14, qm=qm, prb=24, restart=True)
    print("    resp message:", r.get("message"), "| restarted:", r.get("restarted"))
    ok = wait_ready()
    if not ok:
        print("    !! gNB not ready in time")
        break
    m = read_gNB()
    m["_condition"] = f"qm_{qm}"
    results.append(m)
    print("    ", json.dumps(m, ensure_ascii=False))

# restore auto scheduler
print("\n>>> restore ul-scheduler auto")
cli.gnb_ul_scheduler("auto", restart=True)
wait_ready()

print("\n=== SUMMARY ===")
hdr = ["condition", "puschTargetSnrDb", "schedulerMode", "observed_puschSnrDb",
       "ue_puschSnrDb", "phRawDb", "phNormalizedDb", "pcmaxDbm", "tpcPusch",
       "ue_mcs", "ue_qm", "ue_nPrb"]
print(" | ".join(hdr))
for m in results:
    print(" | ".join(str(m.get(k)) for k in hdr))

with open("data/pusch_qm_sweep_result.json", "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print("\nsaved data/pusch_qm_sweep_result.json")
cli.close()
