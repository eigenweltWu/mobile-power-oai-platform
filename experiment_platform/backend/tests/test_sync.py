"""Tests for NTP-style time sync + drift correction."""
from __future__ import annotations

import numpy as np
import pytest

from experiment_platform.backend.sync import (
    SyncResult, compute_sync, drift_ms_per_s, offset_at_elapsed,
)


def make_exchanges(offset_ms: float, rtt_ms: float, n: int = 15, elapsed_base_ns: int = 1_000_000_000):
    """Generate exchanges whose true offset (PC - phone) equals ``offset_ms``."""
    out = []
    rng = np.random.default_rng(0)
    for i in range(n):
        t1 = 1000.0 * i
        rtt = rtt_ms * (1 + rng.normal(0, 0.1))
        t3 = t1 + rtt
        phone_utc = (t1 + rtt / 2.0) - offset_ms  # PC midpoint - phone == offset_ms
        out.append({
            "t1_ms": t1, "t3_ms": t3,
            "t2": {"utcEpochMs": phone_utc, "elapsedRealtimeNs": elapsed_base_ns + i * 10_000_000},
        })
    return out


def test_offset_computation():
    res = compute_sync(make_exchanges(offset_ms=-5.0, rtt_ms=2.0), "before")
    # offset = PC - phone ≈ -5.0 ms (phone ahead of PC by 5 ms)
    assert abs(res.offset_ms - (-5.0)) < 0.6
    assert res.rtt_min_ms < 3.0
    assert res.uncertainty_ms >= 0
    assert res.n_kept >= 5


def test_offset_interpolation_linear():
    pre = SyncResult(direction="before", offset_ms=-5.0, uncertainty_ms=0.2, rtt_min_ms=1.0,
                     rtt_median_ms=1.2, n_exchanges=15, n_kept=5, elapsed_ns=0, phone_utc_ms=1000.0)
    post = SyncResult(direction="after", offset_ms=-3.0, uncertainty_ms=0.2, rtt_min_ms=1.0,
                      rtt_median_ms=1.2, n_exchanges=15, n_kept=5, elapsed_ns=1_000_000_000_000, phone_utc_ms=1000.0)
    mid = offset_at_elapsed(pre, post, 500_000_000_000)
    assert abs(mid - (-4.0)) < 1e-9
    assert abs(offset_at_elapsed(pre, post, 0) - (-5.0)) < 1e-9
    assert abs(offset_at_elapsed(pre, post, 1_000_000_000_000) - (-3.0)) < 1e-9


def test_offset_single_anchor_constant():
    pre = SyncResult(direction="before", offset_ms=-4.0, uncertainty_ms=0.1, rtt_min_ms=1.0,
                     rtt_median_ms=1.0, n_exchanges=15, n_kept=5, elapsed_ns=0, phone_utc_ms=0.0)
    assert offset_at_elapsed(pre, None, 999_999) == -4.0
    assert offset_at_elapsed(None, None, 0) == 0.0


def test_drift_calculation():
    pre = SyncResult(direction="before", offset_ms=-5.0, uncertainty_ms=0.1, rtt_min_ms=1.0,
                     rtt_median_ms=1.0, n_exchanges=15, n_kept=5, elapsed_ns=0, phone_utc_ms=0.0)
    post = SyncResult(direction="after", offset_ms=-4.0, uncertainty_ms=0.1, rtt_min_ms=1.0,
                      rtt_median_ms=1.0, n_exchanges=15, n_kept=5, elapsed_ns=100_000_000_000, phone_utc_ms=0.0)
    # 1 ms drift over 100 s = 0.01 ms/s
    assert abs(drift_ms_per_s(pre, post) - 0.01) < 1e-9


def test_no_valid_exchanges_raises():
    with pytest.raises(ValueError):
        compute_sync([{"t1_ms": None, "t3_ms": None, "t2": {}}], "before")


# --------------------------------------------------------------------------- #
# Clock status (dashboard "synced" / "not_synced") — task §B2
# --------------------------------------------------------------------------- #
def _acks_to_exchanges(acks):
    """Mirror _compute_clock_status's mapping: ack row -> NTP exchange."""
    out = []
    for a in acks:
        if not (a["phone_recv_ms"] and a["phone_send_ms"]):
            continue
        t2 = (a["phone_recv_ms"] + a["phone_send_ms"]) / 2.0
        out.append({"t1_ms": a["pc_send_ms"], "t3_ms": a["pc_recv_ms"],
                    "t2": {"utcEpochMs": t2}})
    return out


def _make_acks(offset_ms=-5.0, rtt_ms=2.0, n=12):
    acks = []
    for i in range(n):
        t1 = 1000.0 * i
        t3 = t1 + rtt_ms
        t2 = (t1 + rtt_ms / 2.0) - offset_ms  # offset = (t1+rtt/2) - t2
        acks.append({"pc_send_ms": t1, "pc_recv_ms": t3,
                     "phone_recv_ms": t2 - 0.5, "phone_send_ms": t2 + 0.5})
    return acks


def test_compute_sync_from_downlink_acks():
    """Downlink ACK rows map to exchanges whose offset matches the truth."""
    res = compute_sync(_acks_to_exchanges(_make_acks(offset_ms=-5.0)), "before")
    assert abs(res.offset_ms - (-5.0)) < 0.6
    assert res.n_kept >= 1


def test_clock_status_no_acks(tmp_path):
    """An empty DB (no handshake yet) reports not_synced."""
    from experiment_platform.backend.config import Settings
    from experiment_platform.backend.db import Database
    from experiment_platform.backend.api import _compute_clock_status
    s = Settings(data_dir=tmp_path / "data", web_dist_dir=tmp_path / "web",
                 oai_host="127.0.0.1", oai_port=1)
    s.ensure_dirs()
    db = Database(s.db_path)
    clock = _compute_clock_status(db)
    assert clock["state"] == "not_synced"
    assert clock["offset_ms"] is None
    assert clock["delay_ms"] is None
    db.close()


def test_clock_status_with_acks(tmp_path):
    """Recorded downlink ACKs flip the dashboard to synced with a real offset."""
    from experiment_platform.backend.config import Settings
    from experiment_platform.backend.db import Database
    from experiment_platform.backend.api import _compute_clock_status
    s = Settings(data_dir=tmp_path / "data", web_dist_dir=tmp_path / "web",
                 oai_host="127.0.0.1", oai_port=1)
    s.ensure_dirs()
    db = Database(s.db_path)
    for a in _make_acks(offset_ms=-5.0):
        db.execute(
            "INSERT INTO experiment_acks(experiment_id,run_id,seq,direction,"
            "pc_send_ms,phone_recv_ms,phone_send_ms,pc_recv_ms,rtt_ms) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ("EXP", "R1", int(a["pc_send_ms"]), "downlink", a["pc_send_ms"],
             a["phone_recv_ms"], a["phone_send_ms"], a["pc_recv_ms"],
             a["pc_recv_ms"] - a["pc_send_ms"]))
    # a sync_confirm row carries the comm delay (phone_ts - pc_ts)
    db.execute(
        "INSERT INTO experiment_acks(experiment_id,run_id,seq,direction,"
        "pc_send_ms,phone_recv_ms,gnb_data_timestamp_ms) VALUES(?,?,?,?,?,?,?)",
        ("EXP", "R1", -1, "sync_confirm", 5000.0, 5003.0, 4998.0))
    clock = _compute_clock_status(db)
    assert clock["state"] == "synced"
    assert abs(clock["offset_ms"] - (-5.0)) < 1.0
    assert clock["delay_ms"] == 3  # 5003 - 5000
    db.close()
