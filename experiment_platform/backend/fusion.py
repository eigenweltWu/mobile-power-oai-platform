"""Data fusion: clock correction, 1 s merging, energy integration (task §37–40)."""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from .sync import SyncResult, offset_at_elapsed

PHONE_CSV_COLUMNS = [
    "utc_epoch_ms", "elapsed_realtime_ns",
    "experiment_id", "run_id", "condition_id", "session_id", "device_id", "phase",
    "battery_current_now_ua", "battery_current_average_ua", "battery_voltage_mv", "battery_power_w",
    "charge_counter_uah", "soc_percent", "battery_temperature_c", "thermal_status", "thermal_headroom",
    "ss_rsrp_dbm", "ss_rsrq_db", "ss_sinr_db",
    "csi_rsrp_dbm", "csi_rsrq_db", "csi_sinr_db", "csi_cqi",
    "nrarfcn", "pci", "nci", "tac", "network_type",
    "screen_state", "plugged", "charging", "wifi_state", "bluetooth_state", "airplane_mode",
    "workload_type", "workload_target_mbps", "workload_actual_mbps", "app_tx_bytes", "app_rx_bytes",
    "sample_quality_flags",
]


def load_phone_csv(path) -> pd.DataFrame:
    df = pd.read_csv(path)
    for col in PHONE_CSV_COLUMNS:
        if col not in df.columns:
            df[col] = np.nan
    return df


def correct_phone_time(phone: pd.DataFrame, pre: Optional[SyncResult], post: Optional[SyncResult]) -> pd.DataFrame:
    """Add ``t_corrected_epoch_ms`` (phone clock aligned to the PC/reference clock).

    ``t_corrected = t_phone + offset(elapsed)`` where offset interpolates between
    pre/post anchors on the phone's monotonic elapsed clock.
    """
    out = phone.copy()
    if pre is None and post is None:
        out["t_corrected_epoch_ms"] = out["utc_epoch_ms"]
        return out
    if "elapsed_realtime_ns" in out.columns:
        off = out["elapsed_realtime_ns"].map(lambda e: offset_at_elapsed(pre, post, float(e)) if pd.notna(e) else (pre or post).offset_ms)
    else:
        off = (pre or post).offset_ms
    out["t_corrected_epoch_ms"] = out["utc_epoch_ms"] + off
    return out


def compute_oai_pc_offset(snapshots: pd.DataFrame) -> float:
    """Median (OAI clock - PC fetch clock) in ms, from snapshot timestamps."""
    if snapshots.empty or "ts_epoch_ns" not in snapshots or "fetched_utc_ms" not in snapshots:
        return 0.0
    df = snapshots.dropna(subset=["ts_epoch_ns", "fetched_utc_ms"])
    if df.empty:
        return 0.0
    return float(np.median(df["ts_epoch_ns"] / 1e6 - df["fetched_utc_ms"]))


def _trapezoid_energy(phone: pd.DataFrame, phase: str) -> float:
    sub = phone[phone["phase"] == phase].sort_values("elapsed_realtime_ns")
    if len(sub) < 2:
        return 0.0
    t = sub["elapsed_realtime_ns"].to_numpy(dtype=float) / 1e9
    p = sub["battery_power_w"].to_numpy(dtype=float)
    mask = ~np.isnan(p)
    if mask.sum() < 2:
        return 0.0
    return float(np.trapezoid(p[mask], t[mask]))


def energy_by_phase(phone: pd.DataFrame) -> dict[str, float]:
    res = {}
    for ph in ("BASELINE", "ACTIVE", "TAIL"):
        res[f"{ph.lower()}_energy_j"] = _trapezoid_energy(phone, ph)
    res["total_energy_j"] = sum(res.values())
    return res


def merge_1s(phone_corrected: pd.DataFrame, snapshots: pd.DataFrame, events: pd.DataFrame,
             oai_pc_offset_ms: float, run_id: str, condition_id: str, environment: str) -> pd.DataFrame:
    """Build the unified 1 s analysis table (Level 2)."""
    ph = phone_corrected.copy()
    ph["window_ms"] = (np.floor(ph["t_corrected_epoch_ms"] / 1000.0) * 1000.0).astype("int64")

    # --- phone aggregates ------------------------------------------------- #
    def _p10(s):
        return s.quantile(0.10)
    def _p90(s):
        return s.quantile(0.90)
    def _mode(s):
        v = s.mode()
        return v.iloc[0] if len(v) else np.nan

    phone_agg = ph.groupby("window_ms").agg(
        phone_current_ua_mean=("battery_current_now_ua", "mean"),
        phone_current_ua_median=("battery_current_now_ua", "median"),
        phone_current_ua_std=("battery_current_now_ua", "std"),
        phone_voltage_mv_mean=("battery_voltage_mv", "mean"),
        phone_power_w_mean=("battery_power_w", "mean"),
        phone_rsrp_dbm_median=("ss_rsrp_dbm", "median"),
        phone_rsrp_dbm_p10=("ss_rsrp_dbm", _p10),
        phone_rsrp_dbm_p90=("ss_rsrp_dbm", _p90),
        phone_rsrq_db_median=("ss_rsrq_db", "median"),
        phone_sinr_db_median=("ss_sinr_db", "median"),
        phone_soc_percent_mean=("soc_percent", "mean"),
        phone_temperature_c_mean=("battery_temperature_c", "mean"),
        phone_sample_count=("t_corrected_epoch_ms", "count"),
        phase=("phase", _mode),
    )
    # per-window energy ~ mean power x window duration (1 s)
    phone_agg["phone_energy_j"] = phone_agg["phone_power_w_mean"] * 1.0

    # --- gNB snapshot (nearest/fresh) ------------------------------------ #
    snap = snapshots.copy()
    if not snap.empty:
        snap["snap_ts_ms"] = snap["ts_epoch_ns"] / 1e6 - oai_pc_offset_ms
        snap = snap.dropna(subset=["snap_ts_ms"]).sort_values("snap_ts_ms")
        snap["window_ms"] = (np.floor(snap["snap_ts_ms"] / 1000.0) * 1000.0).astype("int64")
        # prefer fresh snapshots
        snap = snap.sort_values(["window_ms", "collection_stale", "snap_ts_ms"])
        snap_agg = snap.drop_duplicates("window_ms", keep="first").set_index("window_ms")
        snap_cols = [
            "rsrp_dbm", "ssb_sinr_db", "pusch_snr_db", "ph_raw_db", "ph_normalized_db", "pcmax_dbm",
            "pusch_rssi", "pusch_rssi_unit", "ul_mcs", "dl_mcs", "qm", "n_prb",
            "cqi", "ri", "pmi", "ul_ri", "tpmi", "ul_bler", "dl_bler",
            "harq_initial_tx_delta", "harq_retransmission_delta", "harq_retransmission_ratio",
            "dtx", "ul_goodput_mbps", "dl_goodput_mbps",
        ]
        snap_agg = snap_agg[[c for c in snap_cols if c in snap_agg.columns]]
        snap_agg = snap_agg.add_prefix("gnb_")
        snap_agg = snap_agg.rename(columns={"gnb_pusch_rssi_unit": "pusch_rssi_unit"})
    else:
        snap_agg = pd.DataFrame()

    # --- gNB events aggregation ------------------------------------------ #
    ev = events.copy()
    if not ev.empty:
        ev["ev_ts_ms"] = ev["ts_epoch_ns"] / 1e6 - oai_pc_offset_ms
        ev = ev.dropna(subset=["ev_ts_ms"])
        ev["window_ms"] = (np.floor(ev["ev_ts_ms"] / 1000.0) * 1000.0).astype("int64")
        ev["tpc_pos"] = (ev["tpc_pusch"] > 0).astype(int)
        ev["tpc_zero"] = (ev["tpc_pusch"] == 0).astype(int)
        ev["tpc_neg"] = (ev["tpc_pusch"] < 0).astype(int)
        ev_agg = ev.groupby("window_ms").agg(
            tpc_event_count=("tpc_pusch", "count"),
            tpc_positive_count=("tpc_pos", "sum"),
            tpc_zero_count=("tpc_zero", "sum"),
            tpc_negative_count=("tpc_neg", "sum"),
            pusch_snr_mean=("pusch_snr_db", "mean"),
            ph_normalized_mean=("ph_normalized_db", "mean"),
            pusch_rssi_mean=("rssi", "mean"),
            ul_mcs_mode=("mcs", _mode),
            n_prb_mean=("n_prb", "mean"),
        )
        ev_agg["tpc_positive_ratio"] = ev_agg["tpc_positive_count"] / ev_agg["tpc_event_count"].replace(0, np.nan)
    else:
        ev_agg = pd.DataFrame()

    merged = phone_agg
    if not snap_agg.empty:
        merged = merged.join(snap_agg, how="left")
    if not ev_agg.empty:
        merged = merged.join(ev_agg, how="left")

    merged = merged.reset_index()
    merged.insert(0, "run_id", run_id)
    merged.insert(1, "condition_id", condition_id)
    merged.insert(2, "experiment_id", phone_corrected["experiment_id"].iloc[0] if len(phone_corrected) else "")
    merged.insert(3, "environment", environment)

    # carried gNB event metrics that are also in snapshots
    if "gnb_harq_retransmission_ratio" in merged.columns:
        merged["harq_retransmission_ratio"] = merged["gnb_harq_retransmission_ratio"]
    return merged


def run_summary(phone_corrected: pd.DataFrame, merged: pd.DataFrame, run_id: str,
                condition_id: str, environment: str) -> dict:
    energy = energy_by_phase(phone_corrected)
    # total delivered bits (successful UL/DL goodput not directly available per bit,
    # so use phone app bytes delta as the UE-observable delivered volume).
    if "app_tx_bytes" in phone_corrected.columns:
        tx = phone_corrected["app_tx_bytes"]
        delivered_bits = float((tx.max() - tx.min())) * 8 if tx.notna().any() and len(tx) > 1 else 0.0
    else:
        delivered_bits = 0.0
    active_e = energy.get("active_energy_j", 0.0)
    baseline_e = energy.get("baseline_energy_j", 0.0)
    e_bit = (active_e - baseline_e) / delivered_bits if delivered_bits > 0 else None
    return {
        "run_id": run_id,
        "condition_id": condition_id,
        "environment": environment,
        **energy,
        "energy_per_bit_j_per_bit": e_bit,
        "j_per_mb": (e_bit * 8e6) if e_bit is not None else None,
        "active_seconds": _trapezoid_seconds(phone_corrected, "ACTIVE"),
        "phone_rsrp_median_dbm": float(phone_corrected["ss_rsrp_dbm"].median()) if "ss_rsrp_dbm" in phone_corrected and phone_corrected["ss_rsrp_dbm"].notna().any() else None,
        "quality_status": "",
        "quality_flags": "",
    }


def _trapezoid_seconds(phone: pd.DataFrame, phase: str) -> float:
    sub = phone[phone["phase"] == phase]
    if "elapsed_realtime_ns" not in sub.columns or len(sub) < 2:
        return 0.0
    t = sub["elapsed_realtime_ns"].to_numpy(dtype=float)
    return float((t.max() - t.min()) / 1e9)
