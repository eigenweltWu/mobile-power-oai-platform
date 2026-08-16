"""Full-flow test: start gNB -> 5G time sync -> arm phone -> mid-run PUSCH
target change -> stop. Phone is reached over 5G (no USB)."""
import json
import time
import httpx

from experiment_platform.backend.config import load_settings
from experiment_platform.backend.oai_client import OaiClient

PHONE = "http://10.0.1.24:8420"  # 5G PDU IP
EXP = "FLOW_TEST_1"
RUN = "FLOW_run_0001"
COND = "FLOW_AC_C1"

s = load_settings()
s.oai_timeout_s = 180.0
cli = OaiClient(s)


def phone_post(path, payload=None):
    r = httpx.post(PHONE + path, json=payload or {}, timeout=10)
    r.raise_for_status()
    return r.json()


def phone_get(path):
    r = httpx.get(PHONE + path, timeout=10)
    r.raise_for_status()
    return r.json()


def wait_ue(timeout_s=180.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        st = cli.status()
        if st.gnb and st.gnb.running:
            try:
                u = cli.research_ues()
                if u.collection and u.collection.available and not u.collection.stale and u.ues:
                    return u.ues[0]
            except Exception:
                pass
        time.sleep(5)
    return None


print("=== 1. 启动 gNB ===")
try:
    cli.gnb_service("start")
    print("   start 请求已发出")
except Exception as e:
    print("   start 超时(可能已在启动):", type(e).__name__)

ue = wait_ue()
if not ue:
    print("!! gNB/UE 未就绪")
    raise SystemExit(1)
print(f"   UE in-sync: rnti={ue.rnti} rsrp={ue.rsrpDbm}")

print("\n=== 2. 5G 下行/上行对时 (10次) ===")
offsets = []
for i in range(10):
    t1 = time.time() * 1000.0
    resp = phone_post("/agent/downlink", {"seq": i, "pcSendMs": t1})
    t3 = time.time() * 1000.0
    rtt = t3 - t1
    # offset = PC - phone  (t2 = phoneRecvMs)
    off = (t1 + rtt / 2.0) - resp["phoneRecvMs"]
    offsets.append(off)
    if i % 3 == 0:
        print(f"   [{i}] rtt={rtt:.0f}ms offset={off:.1f}ms")
import statistics
med_off = statistics.median(offsets)
print(f"   对时结果: offset={med_off:.1f}ms (PC-phone), n={len(offsets)}")

print("\n=== 3. 下发任务 + ARM (baseline15s/active30s/tail10s) ===")
plan = {
    "experimentId": EXP, "runId": RUN, "conditionId": COND, "environment": "AC",
    "startDelaySeconds": 3,
    "phases": [
        {"name": "baseline", "durationSeconds": 15},
        {"name": "active", "durationSeconds": 30},
        {"name": "tail", "durationSeconds": 10},
    ],
}
print("   session:", phone_post("/agent/session", plan))
print("   arm:", phone_post("/agent/arm", {"runId": RUN}))

print("\n=== 4. 等待进入 ACTIVE 阶段 (约 20s) ===")
for i in range(20):
    st = phone_get("/agent/status")
    if st.get("phase") == "ACTIVE":
        print(f"   [{i}s] phase=ACTIVE")
        break
    time.sleep(1)

print("\n=== 5. 中途修改 PUSCH Target (manual 8.9dB -> 20dB, 重启) ===")
r = cli.gnb_pusch_target_snr("manual", 200, restart=True)
print("   修改响应 message:", r.get("message"))
print("   等待 UE 重新注册...")
ue2 = wait_ue(timeout_s=120)
if ue2:
    cfg = cli.research_config()
    print(f"   UE 重新注册: rnti={ue2.rnti}, 实际 puschTarget={cfg.puschTargetSnrDb}dB")
else:
    print("   !! UE 未重新注册")

print("\n=== 6. 等待 run 完成 ===")
for i in range(120):
    st = phone_get("/agent/status")
    if st.get("state") == "COMPLETE":
        print(f"   [{i}s] state=COMPLETE")
        break
    if i % 10 == 0:
        print(f"   [{i}s] state={st.get('state')} phase={st.get('phase')}")
    time.sleep(1)

print("\n=== 7. 停止实验 (双方记录时间戳) ===")
pc_stop_ms = int(time.time() * 1000)
phone_stop = phone_post("/agent/task/stop", {})
print(f"   PC stop_ms={pc_stop_ms}  手机响应={phone_stop}")

print("\n=== 8. 导出手机数据 ===")
export = phone_get("/agent/export")
files = export.get("files", {})
print("   文件:", list(files.keys()))
csv = files.get("phone_samples.csv", "")
rows = len(csv.splitlines()) - 1 if csv else 0
print(f"   phone_samples.csv 行数: {rows}")

cli.close()
print("\nFLOW_TEST_DONE")
