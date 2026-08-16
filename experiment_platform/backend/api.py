"""FastAPI application: platform REST API + static frontend serving."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings, load_settings
from .db import Database
from .manager import ExperimentManager
from .oai_client import OaiClient, OaiError
from . import templates

_settings = load_settings()
_settings.ensure_dirs()
_db = Database(_settings.db_path)
_oai = OaiClient(_settings)
_manager = ExperimentManager(_settings, _db, _oai)

app = FastAPI(title="5G Energy Experiment Platform", version=_settings.platform_version)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _compute_clock_status(db: Database, experiment_id: str | None = None) -> dict:
    """Derive the PC↔phone clock offset from recorded downlink ACKs.

    Maps recent downlink ACKs — 5G-direct probes AND /shake exchanges
    (OAI-host relayed, already converted to the PC clock base) — into
    NTP-style exchanges and reuses ``sync.compute_sync`` (which raises
    ValueError when there are no valid samples). Returns a ``not_synced``
    stub when no ACKs exist yet, so the dashboard reflects the real
    handshake state instead of a hardcoded value.

    When ``experiment_id`` is given only that experiment's ACKs count — stale
    ACKs from previous experiments must NOT make a fresh run look synced
    before the phone has even started monitoring."""
    from .sync import compute_sync
    if experiment_id:
        rows = db.query(
            "SELECT pc_send_ms, phone_recv_ms, phone_send_ms, pc_recv_ms "
            "FROM experiment_acks WHERE direction IN ('downlink','shake') AND pc_recv_ms IS NOT NULL "
            "AND experiment_id=? ORDER BY id DESC LIMIT 30", (experiment_id,))
        delay_where = ("SELECT pc_send_ms, phone_recv_ms FROM experiment_acks "
                       "WHERE direction='sync_confirm' AND experiment_id=? "
                       "ORDER BY id DESC LIMIT 1", (experiment_id,))
    else:
        rows = db.query(
            "SELECT pc_send_ms, phone_recv_ms, phone_send_ms, pc_recv_ms "
            "FROM experiment_acks WHERE direction IN ('downlink','shake') AND pc_recv_ms IS NOT NULL "
            "ORDER BY id DESC LIMIT 30")
        delay_where = ("SELECT pc_send_ms, phone_recv_ms FROM experiment_acks "
                       "WHERE direction='sync_confirm' "
                       "ORDER BY id DESC LIMIT 1", ())
    exchanges = []
    for r in rows:
        if not (r["phone_recv_ms"] and r["phone_send_ms"]):
            continue
        t2_utc = (r["phone_recv_ms"] + r["phone_send_ms"]) / 2.0
        exchanges.append({"t1_ms": r["pc_send_ms"], "t3_ms": r["pc_recv_ms"],
                          "t2": {"utcEpochMs": t2_utc}})
    try:
        res = compute_sync(exchanges, "before")
    except ValueError:
        return {"offset_ms": None, "state": "not_synced", "delay_ms": None, "rtt_min_ms": None}
    # communication delay = phone_now - pc_ts, recorded at the last sync-confirm
    delay_row = db.query_one(*delay_where)
    delay_ms = None
    if delay_row and delay_row["phone_recv_ms"] is not None and delay_row["pc_send_ms"] is not None:
        delay_ms = delay_row["phone_recv_ms"] - delay_row["pc_send_ms"]
    return {"offset_ms": res.offset_ms, "state": "synced" if res.n_kept >= 1 else "not_synced",
            "delay_ms": delay_ms, "rtt_min_ms": res.rtt_min_ms}


# --------------------------------------------------------------------------- #
# Platform status / dashboard
# --------------------------------------------------------------------------- #
@app.get("/api/platform/health")
def platform_health():
    oai = {"ok": False, "error": ""}
    try:
        _oai.health()
        oai["ok"] = True
    except Exception as e:  # noqa: BLE001
        oai["error"] = str(e)
    return {"ok": True, "oai": oai}


@app.get("/api/platform/status")
def platform_status():
    oai_ok = True
    status = None
    coll = None
    try:
        status = _oai.status()
    except Exception as e:  # noqa: BLE001
        oai_ok = False
        status = None
    ues = None
    try:
        ues = _oai.research_ues()
        coll = ues.collection
    except Exception:  # noqa: BLE001
        coll = None
    # Rolling throughput sample for the dashboard's 1-minute live chart.
    throughput = None
    if ues and ues.ues:
        try:
            throughput = {
                "epochMs": int((ues.timestampEpochNs or 0) / 1e6) or None,
                "dlMbps": round(sum(u.downlink.goodputMbps or 0.0 for u in ues.ues if u.downlink), 3),
                "ulMbps": round(sum(u.uplink.goodputMbps or 0.0 for u in ues.ues if u.uplink), 3),
                "nUes": len(ues.ues),
            }
        except Exception:  # noqa: BLE001
            throughput = None
    # Active run first (state PREPARING/ARMED/RUNNING, newest rowid); fall back
    # to the most recent run for post-stop display. NOTE: run_id is a string —
    # ordering by it is unreliable, use rowid.
    active = _db.query(
        "SELECT run_id,state,experiment_id,condition_id FROM runs "
        "WHERE state IN ('PREPARING','ARMED','RUNNING') ORDER BY rowid DESC LIMIT 1")
    latest_run = (active or _db.query(
        "SELECT run_id,state,experiment_id,condition_id FROM runs ORDER BY rowid DESC LIMIT 1") or [None])[0]
    if latest_run:
        err_rows = _db.query(
            "SELECT note FROM run_transitions WHERE run_id=? AND to_state='FAILED' ORDER BY utc_ms DESC LIMIT 1",
            (latest_run["run_id"],))
        latest_run["last_error"] = err_rows[0]["note"] if err_rows else None
    files = _db.query("SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes),0) AS bytes FROM files")
    from . import phone_detect as _pd
    phone = _pd.detect_phone(_settings)
    # Clock state is only meaningful for the ACTIVE experiment; stale ACKs
    # from earlier experiments must not make a fresh run look synced.
    clock = (_compute_clock_status(_db, active[0]["experiment_id"]) if active
             else {"offset_ms": None, "state": "not_synced", "delay_ms": None, "rtt_min_ms": None})
    return {
        "phone": {"state": phone["state"], "device": phone.get("pdu_ip"),
                  "usb_attached": phone.get("usb_attached"), "agent_url": phone.get("agent_url"),
                  "serial": phone.get("serial"),
                  "status": phone.get("status")},
        "oai": {
            "healthy": oai_ok,
            "gnb_running": bool(status.gnb and status.gnb.running) if status else None,
            "gnb_status": status.gnb.status if status and status.gnb else None,
            "core_running": status.core.running if status and status.core else None,
            "core_total": status.core.total if status and status.core else None,
            "ue_in_sync": bool(status.ues) if status else None,
            "frequency_mhz": status.radio.frequencyMHz if status and status.radio else None,
            "bandwidth_mhz": status.radio.bandwidthMHz if status and status.radio else None,
            "research_stale": coll.stale if coll else None,
            "throughput": throughput,
        },
        "clock": clock,
        "experiment": {"latest_run": latest_run},
        "storage": {"n_files": files[0]["n"] if files else 0, "bytes": files[0]["bytes"] if files else 0},
    }


# --------------------------------------------------------------------------- #
# OAI read proxy (GET only)
# --------------------------------------------------------------------------- #
@app.get("/api/oai/status")
def oai_status():
    return _oai.status_raw()


@app.get("/api/oai/controls")
def oai_controls():
    return _oai.gnb_controls().model_dump()


@app.get("/api/oai/research/ues")
def oai_research_ues():
    return _oai.research_ues_raw()


@app.get("/api/oai/research/config")
def oai_research_config():
    return _oai.research_config_raw()


@app.get("/api/oai/research/events")
def oai_research_events(limit: int = Query(200, ge=1, le=1000)):
    return _oai.research_events_raw(limit=limit)


@app.get("/api/oai/calibration")
def oai_calibration(device: Optional[str] = None, frequencyMHz: Optional[float] = None):
    return _oai.rf_calibration(device=device, frequencyMHz=frequencyMHz)


@app.get("/api/oai/progress")
def oai_progress(id: Optional[str] = None):
    return _oai.progress(id).model_dump()


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@app.get("/api/settings")
def get_settings():
    return _settings.redacted


@app.put("/api/settings")
def put_settings(payload: dict = Body(...)):
    global _oai, _settings
    # Only OAI connectivity fields are live-editable here; persisted via config.local.json.
    host = payload.get("oai_host")
    port = payload.get("oai_port")
    token = payload.get("oai_control_token")
    if host:
        _settings.oai_host = host
    if port:
        _settings.oai_port = int(port)
    if token is not None:
        _settings.oai_control_token = token
    _oai.close()
    _oai = OaiClient(_settings)
    return _settings.redacted


# --------------------------------------------------------------------------- #
# Experiments / conditions / runs
# --------------------------------------------------------------------------- #
@app.get("/api/experiments")
def list_experiments():
    return _db.query("SELECT * FROM experiments ORDER BY created_utc DESC")


@app.post("/api/experiments")
def create_experiment(payload: dict = Body(...)):
    exp = _manager.create_experiment(
        payload["experiment_id"], payload.get("environment", "AC"),
        payload.get("operator_name", ""), payload.get("notes", ""),
        payload.get("purpose", ""), payload.get("flow", ""),
        json.dumps(payload.get("initial_oai_config"), ensure_ascii=False) if payload.get("initial_oai_config") else None)
    return exp


@app.get("/api/experiments/{experiment_id}/runs")
def list_experiment_runs(experiment_id: str):
    return _db.query("SELECT * FROM runs WHERE experiment_id=? ORDER BY planned_order", (experiment_id,))


@app.get("/api/conditions")
def list_conditions(experiment_id: Optional[str] = None):
    if experiment_id:
        return _db.query("SELECT * FROM conditions WHERE experiment_id=?", (experiment_id,))
    return _db.query("SELECT * FROM conditions")


@app.post("/api/conditions")
def create_condition(payload: dict = Body(...)):
    return _manager.create_condition(payload["condition_id"], payload["experiment_id"],
                                     **{k: v for k, v in payload.items() if k not in ("condition_id", "experiment_id")})


@app.get("/api/templates")
def get_templates():
    return templates.TEMPLATES


@app.get("/api/runs")
def list_runs(experiment_id: Optional[str] = None):
    if experiment_id:
        return _db.query("SELECT * FROM runs WHERE experiment_id=? ORDER BY planned_order, run_id", (experiment_id,))
    return _db.query("SELECT * FROM runs ORDER BY run_id")


@app.post("/api/runs")
def create_run(payload: dict = Body(...)):
    run = _manager.create_run(
        payload["run_id"], payload["experiment_id"], payload["condition_id"],
        payload.get("device_id"), payload.get("session_id"),
        payload.get("planned_order"), payload.get("random_seed"),
        payload.get("start_delay_s", 30.0))
    return run


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    run = _db.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    cond = _db.query_one("SELECT * FROM conditions WHERE condition_id=?", (run["condition_id"],))
    run["condition"] = cond
    run["requested_config"] = json.loads(run["requested_config_json"]) if run.get("requested_config_json") else None
    run["actual_config"] = json.loads(run["actual_config_json"]) if run.get("actual_config_json") else None
    run["quality_flags"] = json.loads(run["quality_flags_json"]) if run.get("quality_flags_json") else []
    err_rows = _db.query(
        "SELECT note FROM run_transitions WHERE run_id=? AND to_state='FAILED' ORDER BY utc_ms DESC LIMIT 1",
        (run_id,))
    run["last_error"] = err_rows[0]["note"] if err_rows else None
    return run


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str):
    for t in ("phone_samples", "oai_snapshots", "oai_events", "sync_anchors", "run_transitions"):
        _db.execute(f"DELETE FROM {t} WHERE run_id=?", (run_id,))
    _db.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
    return {"ok": True}


@app.delete("/api/experiments/{experiment_id}")
def delete_experiment(experiment_id: str):
    flow = _flow()
    loop = flow.downlinks.pop(experiment_id, None)
    if loop:
        loop.stop()
    try:
        return _manager.delete_experiment(experiment_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


# --------------------------------------------------------------------------- #
# Run lifecycle
# --------------------------------------------------------------------------- #
@app.post("/api/runs/{run_id}/prepare")
def prepare_run(run_id: str, payload: dict = Body(...)):
    requested = payload.get("requested_config", payload)
    try:
        return _manager.prepare_run(run_id, requested)
    except OaiError as e:
        raise HTTPException(502, str(e))


@app.post("/api/runs/{run_id}/prepare-config")
def preview_config(payload: dict = Body(...)):
    """Compute requested vs actual WITHOUT applying (dry-run comparison)."""
    requested = payload.get("requested_config", payload)
    actual = _oai.research_config_raw()
    return {"requested": requested, "actual": actual,
            "diff": {k: {"requested": requested.get(k), "actual": actual.get(k)}
                     for k in requested if requested.get(k) is not None and actual.get(k) != requested.get(k)}}


@app.post("/api/runs/{run_id}/arm")
def arm_run(run_id: str, payload: dict = Body(...)):
    """Arm the phone via an already-established adb-forward channel."""
    from .phone_channel import PhoneChannel
    ch = PhoneChannel(payload.get("serial", ""), int(payload.get("pc_port", 8420)))
    run = _db.get_run(run_id)
    plan = _manager.build_plan(run_id, run["experiment_id"], run["condition_id"],
                               _manager._environment(run_id),
                               float(run.get("start_delay_s") or 30.0),
                               payload.get("phases"))
    with ch:
        return _manager.arm_phone(run_id, ch, plan)


@app.post("/api/runs/{run_id}/collect/start")
def collect_start(run_id: str):
    _manager.start_collectors(run_id)
    return {"ok": True}


@app.post("/api/runs/{run_id}/collect/stop")
def collect_stop(run_id: str):
    return _manager.finish_collection(run_id)


@app.post("/api/runs/{run_id}/stop")
def stop_run(run_id: str):
    run = _db.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    experiment_id = run["experiment_id"]
    serial = run.get("device_id") or "53616213"
    stop_ms = int(time.time() * 1000)
    res = {"ok": True, "pc_stop_ms": stop_ms}

    # 1. Stop the downlink loop + record pc_stop ACK
    try:
        _flow().stop_experiment(experiment_id)
        res["downlink_stopped"] = True
    except Exception as e:
        res["downlink_stopped"] = False
        res["downlink_error"] = str(e)

    # 2. Stop OAI collectors
    _manager.stop_collectors(run_id)
    res["collectors_stopped"] = True

    # 3. Tell the phone to stop (record phone stop timestamp)
    try:
        with _flow()._phone(serial) as (agent, _ph):
            pr = agent.stop_task()
            res["phone_stop_ms"] = pr.get("stopUtcMs")
    except Exception as e:
        res["phone_stop_error"] = str(e)

    # 4. Stop the gNB
    try:
        _oai.gnb_service("stop")
        res["gnb_stopped"] = True
    except Exception as e:
        res["gnb_stopped"] = False
        res["gnb_error"] = str(e)

    # 5. Mark the run STOPPED
    _db.transition(run_id, "STOPPED", "stopped by user", utc_ms=stop_ms)
    return res


@app.post("/api/runs/{run_id}/import")
def import_run(run_id: str, payload: dict = Body(...)):
    dest = Path(payload.get("dest_dir") or (_settings.raw_dir / "phone" / run_id))
    return _manager.import_phone_data(run_id, dest)


@app.post("/api/runs/{run_id}/align")
def align_run(run_id: str):
    return _manager.align_and_merge(run_id)


@app.post("/api/runs/{run_id}/quality")
def quality_run(run_id: str):
    return _manager.run_quality(run_id)


@app.get("/api/runs/{run_id}/merged")
def get_merged(run_id: str):
    p = _settings.processed_dir / "merged_1s" / f"{run_id}.csv"
    if not p.exists():
        raise HTTPException(404, "merged data not found; run align first")
    import pandas as pd
    df = pd.read_csv(p)
    records = df.astype(object).where(pd.notna(df), None).to_dict(orient="records")
    return JSONResponse(records)


@app.get("/api/runs/{run_id}/merged.csv")
def get_merged_csv(run_id: str):
    p = _settings.processed_dir / "merged_1s" / f"{run_id}.csv"
    if not p.exists():
        raise HTTPException(404, "merged data not found")
    return FileResponse(p, media_type="text/csv", filename=f"{run_id}_merged_1s.csv")


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
@app.get("/api/experiments/{experiment_id}/export")
def export_experiment_zip(experiment_id: str):
    try:
        p = _manager.export(experiment_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return FileResponse(p, media_type="application/zip", filename=p.name)


# --------------------------------------------------------------------------- #
# Task flow: templates / push / start / stop / collect / timeline / clip
# --------------------------------------------------------------------------- #
import socket as _socket
from . import task_flow as _tf


def _flow():
    return _tf.get_flow(_settings, _db, _oai)


@app.get("/api/experiments/{experiment_id}/templates")
def list_templates(experiment_id: str):
    return _flow().list_templates(experiment_id)


@app.post("/api/experiments/{experiment_id}/templates")
def add_template(experiment_id: str, payload: dict = Body(...)):
    return _flow().add_template(experiment_id, payload["name"], payload.get("config", {}))


@app.delete("/api/experiments/{experiment_id}/templates/{template_id}")
def delete_template(experiment_id: str, template_id: int):
    _flow().delete_template(experiment_id, template_id)
    return {"ok": True}


@app.put("/api/experiments/{experiment_id}")
def update_experiment(experiment_id: str, payload: dict = Body(...)):
    exp = _db.query_one("SELECT * FROM experiments WHERE experiment_id=?", (experiment_id,))
    if not exp:
        raise HTTPException(404, "experiment not found")
    for field in ("purpose", "flow", "initial_oai_config", "notes", "operator_name"):
        if field in payload:
            v = payload[field]
            if field == "initial_oai_config" and isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            _db.execute(f"UPDATE experiments SET {field}=? WHERE experiment_id=?", (v, experiment_id))
    return _db.query_one("SELECT * FROM experiments WHERE experiment_id=?", (experiment_id,))


@app.post("/api/experiments/{experiment_id}/push")
def push_task(experiment_id: str, payload: dict = Body(...)):
    try:
        return _flow().push_task(experiment_id, payload.get("serial", ""), int(payload.get("pc_port", 8420)))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))


@app.get("/api/phone/tasks")
def phone_tasks(serial: str = Query(...), pc_port: int = 8420):
    return _flow().phone_inventory(serial, pc_port)


@app.post("/api/phone/pull")
def phone_pull(payload: dict = Body(...)):
    """Pull ONE run's data from the phone over the USB tunnel (task 4)."""
    hostname = payload.get("hostname") or _socket.gethostname()
    try:
        return _flow().pull_phone_run(
            payload["experimentId"], payload["runId"],
            payload.get("serial", ""), hostname, int(payload.get("pc_port", 8420)))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))


@app.post("/api/experiments/{experiment_id}/templates/{template_id}/apply")
def apply_template(experiment_id: str, template_id: int, payload: dict = Body(default={})):
    """Apply an OAI template (restarts the gNB with the template's config).

    Thin HTTP wrapper around the existing ``task_flow.apply_template`` method —
    no new business logic, just exposes what TaskFlow already does. Only
    allowed while the active run is in an IDLE phase (409 otherwise).
    """
    try:
        return _flow().apply_template(
            experiment_id, template_id,
            serial=payload.get("serial") or "53616213",
            pc_port=int(payload.get("pc_port", 8420)),
        )
    except _tf.TemplateSwitchNotAllowed as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))
    except OaiError as e:
        raise HTTPException(502, f"OAI apply_condition failed: {e}")


@app.post("/api/experiments/{experiment_id}/start")
def start_experiment(experiment_id: str, payload: dict = Body(...)):
    try:
        return _flow().start_experiment(
            experiment_id,
            payload.get("serial", ""),
            int(payload.get("pc_port", 8420)),
            collection_seconds=payload.get("collection_seconds"),
            idle_seconds=payload.get("idle_seconds"),
            template_id=payload.get("template_id"),
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))


@app.post("/api/experiments/{experiment_id}/stop")
def stop_experiment(experiment_id: str, payload: dict = Body(default={})):
    try:
        return _flow().stop_experiment(
            experiment_id,
            payload.get("serial", "53616213"),
            int(payload.get("pc_port", 8420)),
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))


@app.post("/api/experiments/{experiment_id}/collect")
def collect_experiment(experiment_id: str, payload: dict = Body(...)):
    hostname = payload.get("hostname") or _socket.gethostname()
    return _flow().collect_from_phone(experiment_id, payload.get("serial", ""), hostname,
                                      int(payload.get("pc_port", 8420)))


@app.get("/api/experiments/{experiment_id}/timeline")
def experiment_timeline(experiment_id: str):
    return _flow().timeline(experiment_id)


@app.post("/api/experiments/{experiment_id}/clip")
def clip_experiment(experiment_id: str, payload: dict = Body(...)):
    """Fused clip on the sync-zeroed axis: start_ms/end_ms are RELATIVE to the
    first pre-run clock sync (t=0); saves a fused CSV copy (phone+gNB+channel)."""
    try:
        return _flow().clip(experiment_id, payload.get("run_id"), float(payload["start_ms"]),
                            float(payload["end_ms"]), payload.get("label", ""))
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/clips/{clip_id}/download")
def download_clip(clip_id: int):
    row = _db.query_one("SELECT * FROM clips WHERE id=?", (clip_id,))
    if not row:
        raise HTTPException(404, "clip not found")
    p = Path(row.get("output_path") or "")
    if not p.exists():
        raise HTTPException(404, "clip file missing on disk")
    return FileResponse(p, media_type="text/csv", filename=p.name)


# --------------------------------------------------------------------------- #
# Reverberation chamber (RC): stirrer control + sampled acquisition campaigns
# --------------------------------------------------------------------------- #
import threading as _threading

from . import rc_flow as _rc
from .stirrer import StirrerAgent, StirrerError

_stirrer_lock = _threading.Lock()
_stirrer_agents: dict[str, StirrerAgent] = {}


def _stirrer(simulate: bool = False) -> StirrerAgent:
    key = "sim" if simulate else "usb"
    with _stirrer_lock:
        if key not in _stirrer_agents:
            _stirrer_agents[key] = StirrerAgent(_settings, simulate=simulate)
        return _stirrer_agents[key]


def _runner() -> _rc.RcRunner:
    return _rc.get_runner(_settings, _db, _oai)


@app.get("/api/stirrer/status")
def stirrer_status(simulate: bool = False):
    return _stirrer(simulate).status()


@app.post("/api/stirrer/connect")
def stirrer_connect(payload: dict = Body(default={})):
    """Open the USB link to the stirrer controller (or start the virtual motor)."""
    agent = _stirrer(bool(payload.get("simulate")))
    try:
        agent.ensure_helper()
    except StirrerError as e:
        raise HTTPException(502, str(e))
    r = agent.open()
    if not r.get("ok"):
        raise HTTPException(502, f"stirrer open failed: {r.get('error')}")
    return agent.status()


@app.post("/api/stirrer/disconnect")
def stirrer_disconnect(payload: dict = Body(default={})):
    agent = _stirrer(bool(payload.get("simulate")))
    agent.close()
    return agent.status()


@app.post("/api/stirrer/move")
def stirrer_move(payload: dict = Body(...)):
    """Manual jog: {"deg": 5} relative move (blocks until standstill)."""
    agent = _stirrer(bool(payload.get("simulate")))
    if not agent._opened:
        raise HTTPException(409, "stirrer not connected")
    try:
        return agent.move_rel_and_wait(float(payload.get("deg", 0.0)))
    except StirrerError as e:
        raise HTTPException(502, str(e))


@app.post("/api/stirrer/stop")
def stirrer_stop(payload: dict = Body(default={})):
    agent = _stirrer(bool(payload.get("simulate")))
    if not agent._opened:
        raise HTTPException(409, "stirrer not connected")
    return agent.stop()


@app.post("/api/rc/campaign/start")
def rc_campaign_start(payload: dict = Body(...)):
    """Launch a sampled RC acquisition for a RUNNING experiment.

    Requires the experiment to have been started first (gNB up, phone armed);
    the campaign then walks the stirrer, servos puschTargetSnrX10 to hold the
    receiver RSSP, and triggers one timed phone record window per step.
    """
    try:
        return _runner().start(
            _flow(), payload["experimentId"],
            payload.get("serial", "53616213"), int(payload.get("pc_port", 8420)),
            {k: v for k, v in payload.items()
             if k not in ("experimentId", "serial", "pc_port")})
    except ValueError as e:
        raise HTTPException(404, str(e))
    except StirrerError as e:
        raise HTTPException(502, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))


@app.post("/api/rc/campaign/stop")
def rc_campaign_stop(payload: dict = Body(...)):
    try:
        return _runner().stop(payload["experimentId"])
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/rc/campaign/status")
def rc_campaign_status(experiment_id: str):
    return _runner().status(experiment_id)


@app.get("/api/rc/samples")
def rc_samples(experiment_id: str):
    rows = _runner().samples(experiment_id)
    for r in rows:
        for col in ("servo_log", "gnb_summary"):
            if r.get(col):
                try:
                    r[col] = json.loads(r[col])
                except Exception:
                    pass
    return {"ok": True, "experiment_id": experiment_id, "samples": rows}


# --------------------------------------------------------------------------- #
# Frontend static (built React, if present)
# --------------------------------------------------------------------------- #
# Windows mimetypes maps .js -> text/plain, which breaks ES-module scripts
# (browser refuses to execute non-JS MIME for <script type=module> -> blank page).
import mimetypes as _mimetypes
_mimetypes.add_type("text/javascript", ".js")
_mimetypes.add_type("text/javascript", ".mjs")
_mimetypes.add_type("text/css", ".css")
_mimetypes.add_type("application/json", ".json")
_mimetypes.add_type("image/svg+xml", ".svg")
_mimetypes.add_type("image/png", ".png")
_mimetypes.add_type("image/x-icon", ".ico")

_dist = _settings.web_dist_dir
if _dist.exists():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="web")
