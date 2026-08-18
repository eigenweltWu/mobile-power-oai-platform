"""Experiment/run export (task §48–54): ZIP with manifest, raw, processed,
and feature tables (features_m1/m2/m3). CSV for humans, Parquet for analysis.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from .config import Settings
from .db import Database

CONTEXT_COLS = ["device_id", "day", "session_id", "run_id", "condition_id",
                "orientation_deg", "environment", "stirrer_state", "phase"]
TARGET_COLS = ["phone_energy_j", "phone_power_w_mean"]

FEATURES = {
    "m1": ["phone_rsrp_dbm_median"],
    "m2": ["phone_rsrp_dbm_median", "phone_rsrq_db_median", "phone_sinr_db_median",
           "phone_soc_percent_mean", "phone_temperature_c_mean",
           "phone_voltage_mv_mean", "phone_current_ua_mean"],
    "m3": ["phone_rsrp_dbm_median", "phone_rsrq_db_median", "phone_sinr_db_median",
           "phone_soc_percent_mean", "phone_temperature_c_mean",
           "tpc_positive_ratio", "tpc_event_count", "ph_normalized_mean",
           "gnb_ph_raw_db", "gnb_pcmax_dbm", "pusch_snr_mean", "gnb_pusch_snr_db",
           "pusch_rssi_mean", "ul_mcs_mode", "gnb_ul_mcs", "gnb_dl_mcs",
           "gnb_qm", "gnb_n_prb", "harq_retransmission_ratio",
           "gnb_ul_bler", "gnb_dl_bler", "gnb_dtx", "gnb_cqi", "gnb_ri", "gnb_pmi",
           "gnb_ul_goodput_mbps", "gnb_dl_goodput_mbps"],
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_features(merged: pd.DataFrame, run_meta: dict) -> dict[str, pd.DataFrame]:
    """Return {'m1': df, 'm2': df, 'm3': df} at 1 s window granularity."""
    if merged.empty:
        return {"m1": pd.DataFrame(), "m2": pd.DataFrame(), "m3": pd.DataFrame()}

    base = pd.DataFrame(index=merged.index)
    for c in CONTEXT_COLS:
        if c in merged.columns:
            base[c] = merged[c]
        else:
            base[c] = run_meta.get(c)
    # day derived from corrected window time
    if "window_ms" in merged.columns:
        base["day"] = pd.to_datetime(merged["window_ms"], unit="ms", utc=True).dt.strftime("%Y-%m-%d")
    for c in TARGET_COLS:
        base[c] = merged[c] if c in merged.columns else None
    # sample_id MUST be a stable per-window key (teacher/student pairing); assign last
    base["sample_id"] = (merged["run_id"].astype(str) + ":" +
                         merged["window_ms"].astype("int64").astype(str))

    out = {}
    for level, feats in FEATURES.items():
        present = [f for f in feats if f in merged.columns]
        df = pd.concat([base, merged[present]], axis=1)
        out[level] = df
    return out


def _write_csv_parquet(zf: zipfile.ZipFile, name: str, df: pd.DataFrame) -> None:
    csv_buf = df.to_csv(index=False)
    zf.writestr(name, csv_buf)
    try:
        pq = io.BytesIO()
        df.to_parquet(pq, index=False, engine="pyarrow")
        zf.writestr(name.replace(".csv", ".parquet"), pq.getvalue())
    except Exception:
        # Parquet is best-effort; CSV is the fallback of record.
        pass


def build_manifest(settings: Settings, db: Database, experiment_id: str,
                   runs_meta: list[dict], devices: list[dict], sync_rows: list[dict],
                   quality_flags: list[str]) -> dict:
    processing = db.query(
        "SELECT DISTINCT processing_algorithm,processing_version,noise_method,noise_margin_db "
        "FROM rc_samples WHERE experiment_id=?", (experiment_id,))
    return {
        "experiment_id": experiment_id,
        "created_utc": _utcnow(),
        "platform_version": settings.platform_version,
        "schema_version": settings.schema_version,
        "oai": settings.redacted,
        "devices": devices,
        "runs": runs_meta,
        "execution_modes": {r["run_id"]: r.get("execution_mode") or "UNKNOWN" for r in runs_meta},
        "simulated_runs": [r["run_id"] for r in runs_meta if r.get("simulation")],
        "metric_contracts": {
            "bler": "fraction from OAI snapshot estimator; UI renders percent",
            "harq_retransmission_rate": "retransmission deltas / (initial transmission deltas + retransmission deltas)",
            "rc_resolved_paths": ("local PDP peaks that pass noise threshold, prominence and "
                                  "minimum delay-separation / resolution constraints"),
            "raw_vs_derived": {
                "measured": ["raw OAI complex channel payload", "PUSCH RSSI", "HARQ counters/deltas", "goodput snapshots"],
                "derived": ["complex frequency response when reconstructed from CIR", "PDP", "noise floor", "candidate peaks", "resolved effective multipath components", "RMS delay", "K-factor", "window summaries"],
            },
        },
        "channel_processing": {
            "source_of_truth": "immutable per-Sample raw OAI channel JSON",
            "standard_pipeline": ["complex channel source", "complex CIR", "PDP",
                                  "noise threshold", "local peak detection", "prominence filter",
                                  "minimum separation", "resolved effective components"],
            "processing_variants": processing,
            "raw_data_retained": True,
            "derived_data_overwrites_raw": False,
            "phase_rule": "absolute phase is not physically interpreted without recorded calibration",
            "spatial_rule": "AoA/AoD unavailable without a spatial measurement dimension",
        },
        "clock_sync": {
            "n_runs": len(sync_rows),
            "anchors": sync_rows[:200],
        },
        "quality_flags": quality_flags,
    }


def _table_to_df(db: Database, table: str, where: str = "", params: tuple = ()) -> pd.DataFrame:
    sql = f"SELECT * FROM {table}"
    if where:
        sql += " WHERE " + where
    return pd.DataFrame(db.query(sql, params))


def export_experiment(db: Database, settings: Settings, experiment_id: str,
                      oai_client=None) -> Path:
    settings.ensure_dirs()
    exp = db.query_one("SELECT * FROM experiments WHERE experiment_id=?", (experiment_id,))
    if not exp:
        raise ValueError(f"experiment not found: {experiment_id}")

    runs = db.query("SELECT * FROM runs WHERE experiment_id=?", (experiment_id,))
    run_ids = [r["run_id"] for r in runs]
    conditions = db.query("SELECT * FROM conditions WHERE experiment_id=?", (experiment_id,))
    configurations = db.query("SELECT * FROM oai_templates WHERE experiment_id=?", (experiment_id,))
    clips = db.query("SELECT * FROM clips WHERE experiment_id=?", (experiment_id,))
    clip_ids = [row["id"] for row in clips]
    clip_segments = [segment for clip_id in clip_ids for segment in
                     db.query("SELECT * FROM clip_segments WHERE clip_id=? ORDER BY segment_order", (clip_id,))]
    rc_samples = [sample for run_id in run_ids for sample in
                  db.query("SELECT * FROM rc_samples WHERE run_id=? ORDER BY sample_index", (run_id,))]
    devices = db.query("SELECT * FROM devices")
    sync_rows = []
    for rid in run_ids:
        sync_rows += db.query("SELECT * FROM sync_anchors WHERE run_id=?", (rid,))

    out_dir = settings.processed_dir / "export"
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"experiment_{experiment_id}.zip"

    # merged 1s + features across runs
    merged_parts, summary_parts = [], []
    features_by_level = {"m1": [], "m2": [], "m3": []}
    for rid in run_ids:
        run = db.query_one("SELECT * FROM runs WHERE run_id=?", (rid,))
        cond = db.query_one("SELECT * FROM conditions WHERE condition_id=?", (run["condition_id"],)) if run else None
        m = db.query("SELECT * FROM phone_samples WHERE run_id=?", (rid,))
        mdf = pd.DataFrame(m)
        if mdf.empty:
            continue
        # attach corrected time if present, else raw utc
        if "t_corrected_epoch_ms" not in mdf.columns or mdf["t_corrected_epoch_ms"].isna().all():
            mdf["t_corrected_epoch_ms"] = mdf["utc_epoch_ms"]
        mdf["window_ms"] = (mdf["t_corrected_epoch_ms"] // 1000 * 1000).astype("int64")
        # simple 1 s re-aggregation for export (or reuse stored merged file)
        merged_file = settings.processed_dir / "merged_1s" / f"{rid}.csv"
        if merged_file.exists():
            m1s = pd.read_csv(merged_file)
        else:
            m1s = _reaggregate(mdf, rid, run, cond)
        merged_parts.append(m1s)

        meta = {
            "device_id": (run or {}).get("device_id"),
            "environment": (cond or {}).get("environment"),
            "orientation_deg": (cond or {}).get("orientation_deg"),
            "stirrer_state": (cond or {}).get("stirrer_state"),
            "session_id": (run or {}).get("session_id"),
            "condition_id": (run or {}).get("condition_id"),
        }
        for level, fdf in build_features(m1s, meta).items():
            if not fdf.empty:
                features_by_level[level].append(fdf)

    merged_all = pd.concat(merged_parts, ignore_index=True) if merged_parts else pd.DataFrame()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # top-level tables
        _write_csv_parquet(zf, "devices.csv", pd.DataFrame(devices))
        _write_csv_parquet(zf, "conditions.csv", pd.DataFrame(conditions))
        _write_csv_parquet(zf, "configurations.csv", pd.DataFrame(configurations))
        _write_csv_parquet(zf, "runs.csv", pd.DataFrame(runs))
        _write_csv_parquet(zf, "rc_samples.csv", pd.DataFrame(rc_samples))
        _write_csv_parquet(zf, "clips.csv", pd.DataFrame(clips))
        _write_csv_parquet(zf, "clip_segments.csv", pd.DataFrame(clip_segments))
        _write_csv_parquet(zf, "sync.csv", pd.DataFrame(sync_rows))
        if not merged_all.empty:
            _write_csv_parquet(zf, "processed/merged_1s.csv", merged_all)
            # per-run summary (energy integrals) — derived, raw always preserved
            summary_rows = []
            for rid, g in merged_all.groupby("run_id"):
                summary_rows.append({
                    "run_id": rid,
                    "condition_id": g["condition_id"].iloc[0] if "condition_id" in g else "",
                    "environment": g["environment"].iloc[0] if "environment" in g else "",
                    "total_energy_j": float(g["phone_energy_j"].sum()) if "phone_energy_j" in g else None,
                    "active_energy_j": float(g[g["phase"] == "ACTIVE"]["phone_energy_j"].sum()) if "phone_energy_j" in g else None,
                    "mean_power_w": float(g["phone_power_w_mean"].mean()) if "phone_power_w_mean" in g else None,
                })
            _write_csv_parquet(zf, "processed/run_summary.csv", pd.DataFrame(summary_rows))
        for level in ("m1", "m2", "m3"):
            fdf = pd.concat(features_by_level[level], ignore_index=True) if features_by_level[level] else pd.DataFrame()
            if not fdf.empty:
                _write_csv_parquet(zf, f"features_{level}.csv", fdf)

        # manifest
        manifest = build_manifest(settings, db, experiment_id, runs, devices, sync_rows,
                                  [f for r in runs for f in (json.loads(r.get("quality_flags_json") or "[]"))])
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

        # Level 0/1 files already on disk — include by reference with provenance.
        # Only raw/ and processed/time_aligned/ are copied; the export zip itself
        # is excluded. Arcnames preserve the data-dir-relative path.
        data_root = settings.data_dir.resolve()
        for f in db.query("SELECT * FROM files"):
            p = Path(f["file_path"]).resolve()
            if not p.exists() or p == zip_path.resolve():
                continue
            try:
                rel = p.relative_to(data_root)
            except ValueError:
                continue
            if rel.parts and rel.parts[0] in ("raw",) or (rel.parts and rel.parts[0] == "processed" and rel.parts[1] == "time_aligned"):
                zf.write(p, arcname=str(rel))

        # metadata (chamber + calibration)
        chamber_rows = []
        for c in conditions:
            cm = json.loads(c.get("chamber_metadata_json") or "{}")
            chamber_rows.append({"condition_id": c["condition_id"], **cm})
        _write_csv_parquet(zf, "metadata/chamber.csv", pd.DataFrame(chamber_rows))
        if oai_client is not None:
            try:
                cal = oai_client.rf_calibration()
                rows = []
                for dev, dd in (cal.get("devices") or {}).items():
                    for pt in (dd.get("allPoints") or []):
                        rows.append({"device": dev, **pt})
                _write_csv_parquet(zf, "metadata/calibration.csv", pd.DataFrame(rows))
            except Exception:
                pass

    db.record_file(zip_path)
    return zip_path


def _reaggregate(mdf: pd.DataFrame, rid: str, run: Optional[dict], cond: Optional[dict]) -> pd.DataFrame:
    g = mdf.groupby("window_ms").agg(
        phone_power_w_mean=("battery_power_w", "mean"),
        phone_rsrp_dbm_median=("ss_rsrp_dbm", "median"),
        phone_rsrq_db_median=("ss_rsrq_db", "median"),
        phone_sinr_db_median=("ss_sinr_db", "median"),
        phone_soc_percent_mean=("soc_percent", "mean"),
        phone_temperature_c_mean=("battery_temperature_c", "mean"),
        phone_voltage_mv_mean=("battery_voltage_mv", "mean"),
        phone_current_ua_mean=("battery_current_now_ua", "mean"),
    ).reset_index()
    g["phone_energy_j"] = g["phone_power_w_mean"]
    g["run_id"] = rid
    g["condition_id"] = (cond or {}).get("condition_id")
    g["environment"] = (cond or {}).get("environment")
    g["sample_id"] = rid + ":" + g["window_ms"].astype(str)
    return g
