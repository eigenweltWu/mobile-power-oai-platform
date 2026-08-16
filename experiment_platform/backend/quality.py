"""Run quality checks (task §41). Flags only — never delete data."""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from .sync import SyncResult, drift_ms_per_s

# Thresholds (documented; adjustable per deployment).
RTT_POOR_MS = 50.0
UNCERTAINTY_POOR_MS = 20.0
DRIFT_HIGH_MS_PER_S = 0.0005          # 0.5 ms/s == 500 ppm (very loose; flags only gross drift)
DRIFT_HIGH_TOTAL_MS = 100.0
RSRP_TOLERANCE_DB = 3.0


def _flag_set(flags: list[str]) -> list[str]:
    seen: list[str] = []
    for f in flags:
        if f not in seen:
            seen.append(f)
    return seen


def compute_quality(
    *,
    phone: Optional[pd.DataFrame],
    snapshots: Optional[pd.DataFrame],
    events: Optional[pd.DataFrame],
    pre: Optional[SyncResult],
    post: Optional[SyncResult],
    config_before: Optional[dict],
    config_after: Optional[dict],
    target_rsrp_dbm: Optional[float],
    key_config_fields: tuple = ("puschTargetSnrX10", "bandwidthMHz", "frequencyMHz", "txGainDb", "rxGainDb"),
) -> dict:
    flags: list[str] = []

    # Data presence
    if phone is None or len(phone) == 0:
        flags.append("PHONE_DATA_MISSING")
    if snapshots is None or len(snapshots) == 0:
        flags.append("OAI_DATA_MISSING")

    # Clock sync quality
    if pre is not None:
        if pre.rtt_min_ms > RTT_POOR_MS or pre.uncertainty_ms > UNCERTAINTY_POOR_MS:
            flags.append("CLOCK_SYNC_POOR")
    drift = drift_ms_per_s(pre, post)
    total_drift = abs(post.offset_ms - pre.offset_ms) if (pre and post) else 0.0
    if abs(drift) > DRIFT_HIGH_MS_PER_S or total_drift > DRIFT_HIGH_TOTAL_MS:
        flags.append("CLOCK_DRIFT_HIGH")

    # Phone confounders (from phone samples)
    if phone is not None and len(phone):
        if "plugged" in phone.columns and (phone["plugged"].fillna(0) != 0).any():
            flags.append("USB_CONNECTED")
        if "charging" in phone.columns and (phone["charging"].fillna(0) != 0).any():
            flags.append("PHONE_CHARGING")
        if "wifi_state" in phone.columns and phone["wifi_state"].fillna("").str.lower().str.contains("on|enabled|connected").any():
            flags.append("WIFI_ON")
        if "screen_state" in phone.columns and phone["screen_state"].fillna("").str.lower().str.contains("on").any():
            flags.append("SCREEN_ON")
        if "battery_temperature_c" in phone.columns and phone["battery_temperature_c"].notna().any():
            t = phone["battery_temperature_c"].dropna()
            if len(t) > 2 and (t.max() - t.min()) > 5.0:
                flags.append("HIGH_TEMPERATURE_DRIFT")
        if "thermal_status" in phone.columns and phone["thermal_status"].nunique() > 1:
            flags.append("PHONE_THERMAL_CHANGE")

    # Cell change (nci/pci changed mid-run)
    if phone is not None and len(phone) and {"nci", "pci"}.issubset(phone.columns):
        nci = phone["nci"].dropna()
        pci = phone["pci"].dropna()
        if nci.nunique() > 1 or pci.nunique() > 1:
            flags.append("CELL_CHANGED")

    # OAI-side health
    if snapshots is not None and len(snapshots):
        if "collection_stale" in snapshots.columns and (snapshots["collection_stale"] == 1).any():
            flags.append("OAI_STALE")

    # UE out of sync (research snapshot "state")
    # (snapshot normalized table doesn't store state; fall back to events absence heuristics is avoided)

    # Config changed between before/after
    if config_before and config_after:
        changed = False
        for f in key_config_fields:
            if config_before.get(f) != config_after.get(f):
                changed = True
                break
        if changed:
            flags.append("CONFIG_CHANGED")

    # RSRP out of target
    if target_rsrp_dbm is not None and phone is not None and len(phone) and "ss_rsrp_dbm" in phone.columns:
        med = phone["ss_rsrp_dbm"].median()
        if np.isfinite(med) and abs(med - target_rsrp_dbm) > RSRP_TOLERANCE_DB:
            flags.append("RSRP_OUT_OF_TARGET")

    flags = _flag_set(flags)
    if not flags:
        status = "PASS"
    elif any(f in {"PHONE_DATA_MISSING", "OAI_DATA_MISSING"} for f in flags):
        status = "FAILED"
    else:
        status = "WARNING"
    return {"quality_status": status, "quality_flags": flags}
