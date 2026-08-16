"""Tests for fusion: clock correction, 1 s merge, energy integration."""
from __future__ import annotations

import numpy as np
import pytest

from experiment_platform.backend import fusion
from experiment_platform.backend.sync import SyncResult
from .fixtures import make_oai_events_df, make_oai_snapshots_df, make_phone_samples_csv


def _anchor(offset_ms, elapsed_ns, phone_utc_ms=1_700_000_000_000.0):
    return SyncResult(direction="before", offset_ms=offset_ms, uncertainty_ms=0.1, rtt_min_ms=1.0,
                      rtt_median_ms=1.0, n_exchanges=15, n_kept=5, elapsed_ns=elapsed_ns, phone_utc_ms=phone_utc_ms)


def test_correct_phone_time(tmp_path):
    df = make_phone_samples_csv(tmp_path / "phone_samples.csv")
    pre = _anchor(-5.0, df["elapsed_realtime_ns"].min())
    post = _anchor(-3.0, df["elapsed_realtime_ns"].max())
    out = fusion.correct_phone_time(df, pre, post)
    assert "t_corrected_epoch_ms" in out.columns
    # corrected = utc + offset; at the start offset=-5ms -> corrected == utc - 5
    first = out.iloc[0]
    assert abs(first["t_corrected_epoch_ms"] - (first["utc_epoch_ms"] - 5.0)) < 1.0


def test_energy_by_phase(tmp_path):
    df = make_phone_samples_csv(tmp_path / "phone_samples.csv")
    e = fusion.energy_by_phase(df)
    assert set(e) == {"baseline_energy_j", "active_energy_j", "tail_energy_j", "total_energy_j"}
    # active power > baseline power -> active energy > baseline energy
    assert e["active_energy_j"] > e["baseline_energy_j"] > 0
    assert abs(e["total_energy_j"] - sum(e[k] for k in ("baseline_energy_j", "active_energy_j", "tail_energy_j"))) < 1e-9


def test_merge_1s(tmp_path):
    df = make_phone_samples_csv(tmp_path / "phone_samples.csv")
    pre = _anchor(-5.0, df["elapsed_realtime_ns"].min())
    post = _anchor(-3.0, df["elapsed_realtime_ns"].max())
    phone = fusion.correct_phone_time(df, pre, post)
    snap = make_oai_snapshots_df()
    ev = make_oai_events_df()
    oai_pc_offset = fusion.compute_oai_pc_offset(snap)
    assert abs(oai_pc_offset - 5.0) < 0.1  # snapshots are ~5 ms ahead of PC fetch clock

    merged = fusion.merge_1s(phone, snap, ev, oai_pc_offset, "MOCK_run_0001", "MOCK_AC_TSNR15", "AC")
    assert len(merged) > 0
    for col in ("phone_power_w_mean", "phone_rsrp_dbm_median", "tpc_event_count",
                "tpc_positive_ratio", "gnb_pusch_snr_db", "window_ms"):
        assert col in merged.columns
    # active phase energy should be positive in active windows
    active = merged[merged["phase"] == "ACTIVE"]
    assert active["phone_energy_j"].mean() > 0
    # TPC positive ratio within [0,1]
    assert merged["tpc_positive_ratio"].dropna().between(0, 1).all()


def test_missing_not_zero(tmp_path):
    df = make_phone_samples_csv(tmp_path / "phone_samples.csv")
    # null out a telemetry column and ensure it stays null (not 0) after merge
    df.loc[df.index[:10], "battery_current_now_ua"] = np.nan
    pre = _anchor(0.0, df["elapsed_realtime_ns"].min())
    phone = fusion.correct_phone_time(df, pre, None)
    merged = fusion.merge_1s(phone, make_oai_snapshots_df(0), make_oai_events_df(0), 0.0,
                             "MOCK_run_0001", "MOCK_AC_TSNR15", "AC")
    first_window = merged.sort_values("window_ms").iloc[0]
    # current mean is NaN (missing), not 0
    assert np.isnan(first_window["phone_current_ua_mean"]) or first_window["phone_current_ua_mean"] != 0
