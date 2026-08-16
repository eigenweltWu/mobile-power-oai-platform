"""Phone↔PC time synchronisation (task §16, Phase B).

NTP-style exchange: PC records t1 (send) and t3 (receive); the phone returns its
clock (utc_ms + elapsed_realtime_ns) as t2. Offset is estimated from the lowest-RTT
samples. ``offset`` convention follows the spec: ``t_corrected = t_phone + offset``,
i.e. ``offset = PC_clock - phone_clock``.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass
class SyncResult:
    direction: str                      # "before" | "after"
    offset_ms: float                    # PC - phone (median of min-RTT samples)
    uncertainty_ms: float               # MAD of min-RTT sample offsets
    rtt_min_ms: float
    rtt_median_ms: float
    n_exchanges: int
    n_kept: int
    elapsed_ns: Optional[int] = None    # phone elapsed at anchor (min-RTT sample)
    phone_utc_ms: Optional[float] = None
    exchanges: list[dict] = field(default_factory=list)


def _offset_for(ex: dict) -> Optional[float]:
    t1 = ex.get("t1_ms")
    t3 = ex.get("t3_ms")
    t2 = ex.get("t2") or {}
    utc = t2.get("utcEpochMs") if isinstance(t2, dict) else None
    if None in (t1, t3, utc):
        return None
    rtt = t3 - t1
    return (t1 + rtt / 2.0) - utc  # PC midpoint - phone  ==  PC - phone approx


def compute_sync(exchanges: list[dict], direction: str, min_kept: int = 5) -> SyncResult:
    """Compute offset from the lowest-RTT samples."""
    valid = []
    for ex in exchanges:
        t1 = ex.get("t1_ms")
        t3 = ex.get("t3_ms")
        if None in (t1, t3):
            continue
        off = _offset_for(ex)
        if off is None:
            continue
        t2 = ex.get("t2") or {}
        valid.append({
            "t1_ms": t1, "t3_ms": t3, "rtt_ms": t3 - t1, "offset_ms": off,
            "elapsed_ns": t2.get("elapsedRealtimeNs"), "phone_utc_ms": t2.get("utcEpochMs"),
        })

    if not valid:
        raise ValueError("no valid time exchanges")

    valid.sort(key=lambda x: x["rtt_ms"])
    k = max(min_kept, min(len(valid), max(1, int(np.ceil(len(valid) * 0.3)))))
    kept = valid[:k]
    offsets = [x["offset_ms"] for x in kept]
    rtts = [x["rtt_ms"] for x in valid]
    offset = float(np.median(offsets))
    mad = float(np.median([abs(o - offset) for o in offsets])) if offsets else 0.0
    # uncertainty: MAD plus a floor from the fastest RTT (half min RTT as worst one-way error)
    uncertainty = max(mad, float(min(rtts)) / 2.0) if rtts else mad

    anchor = kept[0] if kept else valid[0]
    return SyncResult(
        direction=direction,
        offset_ms=offset,
        uncertainty_ms=uncertainty,
        rtt_min_ms=float(min(rtts)) if rtts else 0.0,
        rtt_median_ms=float(np.median(rtts)) if rtts else 0.0,
        n_exchanges=len(exchanges),
        n_kept=len(kept),
        elapsed_ns=anchor.get("elapsed_ns"),
        phone_utc_ms=anchor.get("phone_utc_ms"),
        exchanges=valid,
    )


def perform_sync(channel, n_exchanges: int = 15) -> SyncResult:
    exchanges = [channel.time_exchange() for _ in range(n_exchanges)]
    return compute_sync(exchanges, direction="before")


def offset_at_elapsed(pre: Optional[SyncResult], post: Optional[SyncResult], elapsed_ns: float) -> float:
    """Linearly interpolate PC-phone offset between pre/post anchors.

    Uses the phone's monotonic ``elapsed_realtime_ns`` as the interpolation
    variable so the correction is immune to wall-clock jumps (task §14).
    """
    if pre is None and post is None:
        return 0.0
    if post is None or pre is None or pre.elapsed_ns is None or post.elapsed_ns is None:
        a = pre if pre is not None else post
        return a.offset_ms
    if pre.elapsed_ns == post.elapsed_ns:
        return pre.offset_ms
    frac = (elapsed_ns - pre.elapsed_ns) / (post.elapsed_ns - pre.elapsed_ns)
    return pre.offset_ms + (post.offset_ms - pre.offset_ms) * frac


def drift_ms_per_s(pre: Optional[SyncResult], post: Optional[SyncResult]) -> float:
    """Return offset drift in ms per second (for CLOCK_DRIFT_HIGH flag)."""
    if not pre or not post or pre.elapsed_ns is None or post.elapsed_ns is None:
        return 0.0
    dt_s = (post.elapsed_ns - pre.elapsed_ns) / 1e9
    if dt_s == 0:
        return 0.0
    return (post.offset_ms - pre.offset_ms) / dt_s
