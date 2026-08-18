"""FastAPI application: platform REST API + static frontend serving."""
from __future__ import annotations

import json
import shutil
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


def _oai_error_response(exc: OaiError) -> JSONResponse:
    try:
        detail = json.loads(exc.body)
    except Exception:
        detail = {"code": exc.code or "OAI_REQUEST_FAILED", "error": str(exc)}
    status = exc.status if 400 <= exc.status <= 599 else 502
    return JSONResponse(status_code=status, content=detail)


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
    nettest = None
    try:
        ues = _oai.telemetry_ues()
        coll = ues.collection
    except Exception:  # noqa: BLE001
        coll = None
    try:
        nettest = (_oai.nettest_status().get("session") or {})
    except Exception:
        nettest = None
    # Rolling throughput sample for the dashboard's 1-minute live chart.
    throughput = None
    fresh_ues = ([ue for ue in ues.ues
                  if ue.ageSeconds is not None and ue.ageSeconds <= 5.0]
                 if ues and coll and not coll.stale else [])
    if fresh_ues:
        try:
            throughput = {
                "epochMs": int((ues.timestampEpochNs or 0) / 1e6) or None,
                "dlMbps": round(sum(u.downlink.goodputMbps or 0.0 for u in fresh_ues if u.downlink), 3),
                "ulMbps": round(sum(u.uplink.goodputMbps or 0.0 for u in fresh_ues if u.uplink), 3),
                "nUes": len(fresh_ues),
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
            "ue_in_sync": bool(fresh_ues) if ues is not None else None,
            "frequency_mhz": status.radio.frequencyMHz if status and status.radio else None,
            "bandwidth_mhz": status.radio.bandwidthMHz if status and status.radio else None,
            "telemetry_stale": coll.stale if coll else None,
            "research_stale": coll.stale if coll else None,
            "throughput": throughput,
            "nettest": nettest,
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


@app.get("/api/oai/telemetry/ues")
def oai_telemetry_ues():
    return _oai.telemetry_ues_raw()


@app.get("/api/oai/telemetry/config")
def oai_telemetry_config():
    return _oai.telemetry_config_raw()


@app.get("/api/oai/telemetry/events")
def oai_telemetry_events(limit: int = Query(200, ge=1, le=1000)):
    return _oai.telemetry_events_raw(limit=limit)


# Compatibility aliases for existing platform consumers.
app.add_api_route("/api/oai/research/ues", oai_telemetry_ues, methods=["GET"])
app.add_api_route("/api/oai/research/config", oai_telemetry_config, methods=["GET"])
app.add_api_route("/api/oai/research/events", oai_telemetry_events, methods=["GET"])


@app.get("/api/oai/calibration")
def oai_calibration(device: Optional[str] = None, frequencyMHz: Optional[float] = None):
    return _oai.rf_calibration(device=device, frequencyMHz=frequencyMHz)


@app.get("/api/oai/progress")
def oai_progress(id: Optional[str] = None):
    return _oai.progress(id).model_dump()


@app.get("/api/oai/nettest/status")
def oai_nettest_status():
    return _oai.nettest_status()


@app.post("/api/oai/nettest")
def oai_nettest(payload: dict = Body(...)):
    try:
        if payload.get("action") == "stop":
            return _oai.nettest_stop()
        return _oai.nettest_start(
            str(payload.get("direction") or "uplink"),
            str(payload.get("protocol") or "udp"),
            payload.get("rateMbps") or 0.0,
        )
    except OaiError as exc:
        return _oai_error_response(exc)


@app.get("/api/oai/history/configuration")
def oai_configuration_history(limit: int = Query(100, ge=1, le=1000)):
    return _oai.configuration_history(limit)


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
    return _db.query(
        "SELECT e.*, "
        "(SELECT COUNT(*) FROM oai_templates t WHERE t.experiment_id=e.experiment_id AND t.archived_utc IS NULL) AS configuration_count, "
        "(SELECT COUNT(*) FROM runs r WHERE r.experiment_id=e.experiment_id) AS run_count, "
        "(SELECT r.state FROM runs r WHERE r.experiment_id=e.experiment_id ORDER BY COALESCE(r.started_utc_ms,0) DESC,r.rowid DESC LIMIT 1) AS last_run_state, "
        "(SELECT r.quality_status FROM runs r WHERE r.experiment_id=e.experiment_id ORDER BY COALESCE(r.started_utc_ms,0) DESC,r.rowid DESC LIMIT 1) AS last_quality_status, "
        "(SELECT COALESCE(r.ended_utc_ms,r.started_utc_ms) FROM runs r WHERE r.experiment_id=e.experiment_id ORDER BY COALESCE(r.started_utc_ms,0) DESC,r.rowid DESC LIMIT 1) AS last_activity_utc_ms "
        "FROM experiments e ORDER BY e.created_utc DESC")


@app.post("/api/experiments")
def create_experiment(payload: dict = Body(...)):
    experiment_id = str(payload.get("experiment_id") or "").strip()
    if not experiment_id:
        raise HTTPException(422, "experiment_id is required")
    if _db.query_one("SELECT 1 FROM experiments WHERE experiment_id=?", (experiment_id,)):
        raise HTTPException(409, "experiment already exists")
    exp = _manager.create_experiment(
        experiment_id, payload.get("environment", "AC"),
        payload.get("operator_name", ""), payload.get("notes", ""),
        payload.get("purpose", ""), payload.get("flow", ""),
        json.dumps(payload.get("initial_oai_config"), ensure_ascii=False) if payload.get("initial_oai_config") else None)
    try:
        config = dict(payload.get("initial_oai_config") or templates.DEFAULT_OAI_CONFIGURATION)
        if payload.get("environment", "AC") == "RC" and "rcChamber" not in config:
            from .rc_flow import RcConfig
            config["rcChamber"] = RcConfig().as_dict()
            config["rcChamber"].pop("execution_mode", None)
            config["executionMode"] = "REAL_HARDWARE"
        else:
            config.setdefault("executionMode", "REAL_HARDWARE")
        row = _flow().add_template(exp["experiment_id"], "Default", config)
        _flow().set_default_template(exp["experiment_id"], row["id"])
        exp = _db.query_one("SELECT * FROM experiments WHERE experiment_id=?", (exp["experiment_id"],))
        exp["default_configuration"] = row
    except Exception as e:  # experiment exists; surface the partial failure explicitly
        exp["configuration_error"] = str(e)
    return exp


@app.get("/api/experiments/{experiment_id}/runs")
def list_experiment_runs(experiment_id: str):
    return _db.query(
        "SELECT * FROM runs WHERE experiment_id=? "
        "ORDER BY COALESCE(started_utc_ms,planned_start_utc_ms,0) DESC,rowid DESC",
        (experiment_id,))


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
    run["configuration_snapshot"] = (json.loads(run["configuration_snapshot_json"])
                                     if run.get("configuration_snapshot_json") else None)
    run["applied_config_snapshot"] = (json.loads(run["applied_config_snapshot_json"])
                                      if run.get("applied_config_snapshot_json") else None)
    run["alignment"] = json.loads(run["alignment_json"]) if run.get("alignment_json") else None
    run["quality_flags"] = json.loads(run["quality_flags_json"]) if run.get("quality_flags_json") else []
    err_rows = _db.query(
        "SELECT note FROM run_transitions WHERE run_id=? AND to_state='FAILED' ORDER BY utc_ms DESC LIMIT 1",
        (run_id,))
    run["last_error"] = err_rows[0]["note"] if err_rows else None
    run["record_counts"] = {
        "phone": _db.query_one("SELECT COUNT(*) AS n FROM phone_samples WHERE run_id=?", (run_id,))["n"],
        "gnb": _db.query_one("SELECT COUNT(*) AS n FROM oai_snapshots WHERE run_id=?", (run_id,))["n"],
        "cir": _db.query_one("SELECT COUNT(*) AS n FROM oai_channel WHERE run_id=?", (run_id,))["n"],
        "clips": _db.query_one("SELECT COUNT(*) AS n FROM clips WHERE run_id=?", (run_id,))["n"],
    }
    return run


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str, cascade_clips: bool = False):
    clips = _db.query_one("SELECT COUNT(*) n FROM clips WHERE run_id=?", (run_id,))["n"]
    if clips and not cascade_clips:
        raise HTTPException(409, f"This Run has {clips} saved Clip(s); confirm cascade_clips=true")
    for t in ("phone_samples", "oai_snapshots", "oai_events", "oai_channel", "oai_config",
              "sync_anchors", "run_transitions", "clips", "rc_samples"):
        _db.execute(f"DELETE FROM {t} WHERE run_id=?", (run_id,))
    _db.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
    return {"ok": True}


@app.delete("/api/experiments/{experiment_id}")
def delete_experiment(experiment_id: str):
    flow = _flow()
    loop = flow.downlinks.pop(experiment_id, None)
    if loop:
        loop.stop()
    flow._stop_collectors(experiment_id)
    # Reuse the complete Run cleanup so deleting an Experiment also removes
    # raw gNB/CIR/RC files instead of leaving unindexed history on disk.
    for row in _db.query("SELECT run_id FROM runs WHERE experiment_id=?", (experiment_id,)):
        flow.discard_run(row["run_id"])
    try:
        return _manager.delete_experiment(experiment_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/history")
def clear_history():
    try:
        return _manager.clear_history_files()
    except ValueError as e:
        raise HTTPException(409, str(e))


# --------------------------------------------------------------------------- #
# Run lifecycle
# --------------------------------------------------------------------------- #
@app.post("/api/runs/{run_id}/prepare")
def prepare_run(run_id: str, payload: dict = Body(...)):
    requested = payload.get("requested_config", payload)
    try:
        return _manager.prepare_run(run_id, requested)
    except OaiError as exc:
        return _oai_error_response(exc)


@app.post("/api/runs/{run_id}/prepare-config")
def preview_config(payload: dict = Body(...)):
    """Compute requested vs actual WITHOUT applying (dry-run comparison)."""
    requested = payload.get("requested_config", payload)
    actual = _oai.telemetry_config_raw()
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
    _manager.stop_collectors(run_id)
    return _flow().stop_experiment(
        run["experiment_id"], run.get("device_id") or "53616213",
        requested_run_id=run_id)


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


@app.get("/api/experiments/{experiment_id}/export-preview")
def export_experiment_preview(experiment_id: str):
    experiment = _db.query_one("SELECT * FROM experiments WHERE experiment_id=?", (experiment_id,))
    if not experiment:
        raise HTTPException(404, "Experiment not found")
    runs = _db.query("SELECT * FROM runs WHERE experiment_id=?", (experiment_id,))
    run_ids = [row["run_id"] for row in runs]
    counts = {table: sum(_db.query_one(f"SELECT COUNT(*) n FROM {table} WHERE run_id=?", (run_id,))["n"]
                         for run_id in run_ids)
              for table in ("phone_samples", "oai_snapshots", "oai_channel", "rc_samples", "clips")}
    files = _db.query("SELECT file_path,size_bytes FROM files")
    relevant = [row for row in files if experiment_id in row["file_path"]]
    starts = [row.get("started_utc_ms") for row in runs if row.get("started_utc_ms")]
    ends = [row.get("ended_utc_ms") for row in runs if row.get("ended_utc_ms")]
    return {"experiment_id": experiment_id, "runs": len(runs), "record_counts": counts,
            "time_span": {"start_utc_ms": min(starts) if starts else None,
                          "end_utc_ms": max(ends) if ends else None},
            "files": relevant[:200], "estimated_size_bytes": sum(row.get("size_bytes") or 0 for row in relevant),
            "contents": ["manifest.json", "configurations.csv", "runs.csv", "rc_samples.csv",
                         "clips.csv", "clip_segments.csv", "raw/", "processed/"]}


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
    try:
        return _flow().add_template(experiment_id, payload["name"], payload.get("config", {}))
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.put("/api/experiments/{experiment_id}/templates/{template_id}")
def update_template(experiment_id: str, template_id: int, payload: dict = Body(...)):
    try:
        return _flow().update_template(experiment_id, template_id, payload["name"], payload.get("config", {}))
    except ValueError as e:
        raise HTTPException(404 if "not found" in str(e) else 409, str(e))


@app.post("/api/experiments/{experiment_id}/templates/{template_id}/default")
def set_default_template(experiment_id: str, template_id: int):
    try:
        return _flow().set_default_template(experiment_id, template_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/experiments/{experiment_id}/templates/{template_id}")
def delete_template(experiment_id: str, template_id: int):
    try:
        _flow().delete_template(experiment_id, template_id)
    except ValueError as e:
        raise HTTPException(409, str(e))
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
    except OaiError as exc:
        return _oai_error_response(exc)


def _configuration_readiness(config: dict) -> list[str]:
    missing = [key for key in ("frequencyMHz", "bandwidthMHz", "txGainDb", "rxGainDb",
                                "puschTargetMode", "schedulerMode", "ulTrafficMbps")
               if config.get(key) is None]
    return missing


@app.get("/api/run-control/preflight")
def run_control_preflight(experiment_id: str, configuration_id: int):
    experiment = _db.query_one("SELECT * FROM experiments WHERE experiment_id=?", (experiment_id,))
    row = _db.query_one(
        "SELECT * FROM oai_templates WHERE id=? AND experiment_id=? AND archived_utc IS NULL",
        (configuration_id, experiment_id))
    if not experiment or not row:
        raise HTTPException(404, "Experiment or Configuration not found")
    config = json.loads(row["config_json"])
    platform = platform_status()
    applied = _db.query_one("SELECT * FROM configuration_apply_state WHERE singleton_id=1")
    if applied:
        for key in ("requested_config_json", "actual_config_json", "diff_json"):
            applied[key.removesuffix("_json")] = json.loads(applied[key]) if applied.get(key) else None
    missing = _configuration_readiness(config)
    free_bytes = shutil.disk_usage(_settings.data_dir).free
    checks = [
        {"group": "Experiment", "key": "experiment", "ok": True,
         "label": "Experiment selected", "action": None},
        {"group": "Configuration", "key": "configuration", "ok": not missing,
         "label": "Configuration complete" if not missing else f"Missing: {', '.join(missing)}",
         "action": f"Open Experiment → Configurations"},
        {"group": "gNB", "key": "oai", "ok": bool(platform["oai"]["healthy"]),
         "label": "OAI control endpoint reachable", "action": "Open Advanced → Diagnostics"},
        {"group": "Phone", "key": "phone", "ok": not bool(platform["phone"].get("usb_attached")),
         "label": ("USB removed; Phone/UE sync starts after gNB restart"
                   if not platform["phone"].get("usb_attached") else "Unplug USB before Start"),
         "action": "Unplug USB before Start"},
        {"group": "Storage", "key": "storage", "ok": free_bytes >= 100 * 1024 * 1024,
         "label": "Storage available", "action": "Free at least 100 MB"},
    ]
    mode = _tf.execution_mode(config)
    if experiment.get("environment") == "RC":
        chamber = config.get("rcChamber") or {}
        hardware_ok = mode == "SIMULATION"
        hardware_reason = "Simulation mode confirmed"
        if mode == "REAL_HARDWARE":
            state = _stirrer(False).status()
            hardware_ok = bool(state.get("dll_found") and state.get("exe_ready"))
            hardware_reason = "Stirrer hardware available"
        checks.append({"group": "RC Chamber", "key": "stirrer", "ok": hardware_ok,
                       "label": hardware_reason, "action": "Open Advanced → RC Hardware"})
    issues = [check for check in checks if not check["ok"]]
    return {"ready": not issues, "checks": checks, "issues": issues,
            "experiment": {"experiment_id": experiment_id, "environment": experiment["environment"]},
            "selected": {"id": row["id"], "version": row.get("version") or 1,
                         "name": row["name"], "config": config},
            "applied": applied, "execution_mode": mode}


@app.post("/api/experiments/{experiment_id}/start")
def start_experiment(experiment_id: str, payload: dict = Body(...)):
    try:
        configuration_id = payload.get("template_id")
        if configuration_id is None:
            raise ValueError("Configuration is required")
        preflight = run_control_preflight(experiment_id, int(configuration_id))
        if not preflight["ready"]:
            raise HTTPException(409, {"message": "Run cannot start", "issues": preflight["issues"]})
        result = _flow().start_experiment(
            experiment_id,
            payload.get("serial", ""),
            int(payload.get("pc_port", 8420)),
            collection_seconds=payload.get("collection_seconds"),
            idle_seconds=payload.get("idle_seconds"),
            template_id=payload.get("template_id"),
        )
        if preflight["experiment"]["environment"] == "RC":
            chamber = dict((preflight["selected"]["config"].get("rcChamber") or {}))
            chamber["execution_mode"] = preflight["execution_mode"]
            try:
                _runner().start(_flow(), experiment_id, payload.get("serial", "53616213"),
                                int(payload.get("pc_port", 8420)), chamber)
            except Exception:
                # An RC Run is one atomic execution. If chamber startup fails,
                # tear down the just-created Run instead of leaving a phantom
                # active Run that blocks all subsequent Preflight checks.
                _flow().stop_experiment(experiment_id, payload.get("serial", "53616213"),
                                        int(payload.get("pc_port", 8420)))
                raise
            result["rc_campaign_started"] = True
        return result
    except HTTPException:
        raise
    except OaiError as exc:
        return _oai_error_response(exc)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))


@app.post("/api/experiments/{experiment_id}/stop")
def stop_experiment(experiment_id: str, payload: dict = Body(default={})):
    try:
        campaign = _runner().status(experiment_id)
        if campaign.get("running"):
            _runner().stop(experiment_id)
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
def experiment_timeline(experiment_id: str, run_id: Optional[str] = None,
                        include_channel: bool = False):
    return _flow().timeline(experiment_id, run_id, include_channel)


@app.post("/api/experiments/{experiment_id}/clip")
def clip_experiment(experiment_id: str, payload: dict = Body(...)):
    """Fused clip on the sync-zeroed axis: start_ms/end_ms are RELATIVE to the
    first pre-run clock sync (t=0); saves a fused CSV copy (phone+gNB+channel)."""
    try:
        if payload.get("segments") is not None:
            return _flow().save_clip(experiment_id, payload["run_id"], payload.get("name") or
                                     payload.get("label") or "Clip", payload["segments"])
        return _flow().clip(experiment_id, payload.get("run_id"), float(payload["start_ms"]),
                            float(payload["end_ms"]), payload.get("label", ""))
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/clips/{clip_id}")
def get_clip(clip_id: int):
    row = _db.query_one("SELECT * FROM clips WHERE id=?", (clip_id,))
    if not row:
        raise HTTPException(404, "Clip not found")
    row["segments"] = _db.query(
        "SELECT * FROM clip_segments WHERE clip_id=? ORDER BY segment_order", (clip_id,))
    return row


@app.put("/api/clips/{clip_id}")
def update_clip(clip_id: int, payload: dict = Body(...)):
    row = _db.query_one("SELECT * FROM clips WHERE id=?", (clip_id,))
    if not row:
        raise HTTPException(404, "Clip not found")
    try:
        return _flow().save_clip(row["experiment_id"], row["run_id"],
                                 payload.get("name") or payload.get("label") or row["label"],
                                 payload["segments"], clip_id=clip_id)
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.delete("/api/clips/{clip_id}")
def delete_clip(clip_id: int):
    if not _db.query_one("SELECT id FROM clips WHERE id=?", (clip_id,)):
        raise HTTPException(404, "Clip not found")
    _db.execute("DELETE FROM clip_segments WHERE clip_id=?", (clip_id,))
    _db.execute("DELETE FROM clips WHERE id=?", (clip_id,))
    return {"ok": True}


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
def rc_samples(experiment_id: Optional[str] = None, run_id: Optional[str] = None):
    rows = _runner().samples(experiment_id=experiment_id, run_id=run_id)
    return {"ok": True, "experiment_id": experiment_id, "run_id": run_id, "samples": rows}


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
