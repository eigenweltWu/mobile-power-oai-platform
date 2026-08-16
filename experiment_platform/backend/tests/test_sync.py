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
