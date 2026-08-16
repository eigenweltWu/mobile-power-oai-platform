"""Real end-to-end dry run: time sync -> arm -> offline run -> OAI collectors
-> export -> import -> align -> quality -> export ZIP. Uses the platform's own
components (PhoneChannel / sync / manager / collectors)."""
import time
import json
from pathlib import Path

from experiment_platform.backend.config import load_settings
from experiment_platform.backend.db import Database
from experiment_platform.backend.oai_client import OaiClient
from experiment_platform.backend.phone_channel import PhoneChannel
from experiment_platform.backend.sync import perform_sync
from experiment_platform.backend.manager import ExperimentManager

EXP = "LIVE_DRYRUN_2"
RUN = "LIVE_run_0002"
COND = "LIVE_AC_C2"
SERIAL = "53616213"

s = load_settings()
s.ensure_dirs()
db = Database(s.db_path)
oai = OaiClient(s)
mgr = ExperimentManager(s, db, oai)

print("=== create experiment/condition/run ===")
mgr.create_experiment(EXP, "AC", operator_name="LIVE", notes="real end-to-end dry run")
mgr.create_condition(COND, EXP, environment="AC", target_rsrp_dbm=-66.0, traffic_condition="UL_CBR",
                     pusch_target_mode="manual", pusch_target_snr_x10=89, pusch_target_snr_db=8.9,
                     scheduler_mode="auto", bandwidth_mhz=100, frequency_mhz=3349.92)
mgr.create_run(RUN, EXP, COND, device_id="53616213", session_id="LIVE_session_1", start_delay_s=5.0)
print("  ok")

with PhoneChannel(SERIAL, 8420) as ch:
    print("=== pre-sync (15 exchanges) ===")
    pre = perform_sync(ch, n_exchanges=15)
    print(f"  offset_ms={pre.offset_ms:.3f}  rtt_min_ms={pre.rtt_min_ms:.2f}  uncertainty_ms={pre.uncertainty_ms:.3f}  n_kept={pre.n_kept}")
    db.execute("DELETE FROM sync_anchors WHERE run_id=? AND direction='before'", (RUN,))
    for i, ex in enumerate(pre.exchanges):
        db.execute("INSERT INTO sync_anchors(run_id,direction,attempt_index,t1_ms,t2_elapsed_ns,t2_utc_ms,t3_ms,rtt_ms,offset_ms,uncertainty_ms) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (RUN, "before", i, ex["t1_ms"], ex.get("elapsed_ns"), ex.get("phone_utc_ms"), ex["t3_ms"], ex["rtt_ms"], ex["offset_ms"], pre.uncertainty_ms))

    print("=== arm phone (plan: delay 5s, baseline 10s, active 15s, tail 10s) ===")
    plan = {
        "experimentId": EXP, "runId": RUN, "conditionId": COND, "environment": "AC",
        "startDelaySeconds": 5,
        "phases": [
            {"name": "baseline", "durationSeconds": 10},
            {"name": "active", "durationSeconds": 15},
            {"name": "tail", "durationSeconds": 10},
        ],
    }
    ch.agent.session(plan)
    r = ch.agent.arm(RUN)
    print("  arm:", r)

print("=== start OAI collectors ===")
mgr.start_collectors(RUN)

print("=== wait for run completion ===")
with PhoneChannel(SERIAL, 8420) as ch:
    for i in range(120):
        st = ch.agent.status()
        if i % 5 == 0:
            print(f"  [{i*2}s] state={st.get('state')} phase={st.get('phase')}")
        if st.get("state") in ("COMPLETE", "ABORTED"):
            print("  final state:", st.get("state"))
            break
        time.sleep(2)

print("=== stop collectors + post-sync ===")
mgr.stop_collectors(RUN)
with PhoneChannel(SERIAL, 8420) as ch:
    post = perform_sync(ch, n_exchanges=15)
    print(f"  post offset_ms={post.offset_ms:.3f} rtt_min_ms={post.rtt_min_ms:.2f}")
    for i, ex in enumerate(post.exchanges):
        db.execute("INSERT INTO sync_anchors(run_id,direction,attempt_index,t1_ms,t2_elapsed_ns,t2_utc_ms,t3_ms,rtt_ms,offset_ms,uncertainty_ms) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (RUN, "after", i, ex["t1_ms"], ex.get("elapsed_ns"), ex.get("phone_utc_ms"), ex["t3_ms"], ex["rtt_ms"], ex["offset_ms"], post.uncertainty_ms))

print("=== export phone data + import ===")
staging = s.data_dir / "staging" / RUN
staging.mkdir(parents=True, exist_ok=True)
with PhoneChannel(SERIAL, 8420) as ch:
    files = ch.agent.export(staging)
    print("  exported files:", [f.name for f in files])
imported = mgr.import_phone_data(RUN, staging)
print("  imported:", imported)

print("=== align + merge ===")
aligned = mgr.align_and_merge(RUN)
print("  ", {k: v for k, v in aligned.items() if k != "merged"})

print("=== quality ===")
q = mgr.run_quality(RUN)
print("  ", q)

print("=== export experiment ZIP ===")
zip_path = mgr.export(EXP)
print("  zip:", zip_path)

# summary
snap = db.query("SELECT COUNT(*) n FROM oai_snapshots WHERE run_id=?", (RUN,))
ev = db.query("SELECT COUNT(*) n FROM oai_events WHERE run_id=?", (RUN,))
samples = db.query("SELECT COUNT(*) n FROM phone_samples WHERE run_id=?", (RUN,))
print(f"\n=== RESULT: phone_samples={samples[0]['n']} oai_snapshots={snap[0]['n']} oai_events={ev[0]['n']} ===")
# markers live in phone_events.csv (phone-side), read from the raw file
ev_csv = s.raw_dir / "phone" / RUN / "phone_events.csv"
if ev_csv.exists():
    import csv
    with open(ev_csv, newline="", encoding="utf-8") as f:
        markers = [row["marker_type"] for row in csv.DictReader(f)]
    print("markers:", markers)
db.close()
oai.close()
print("DRY_RUN_COMPLETE")
