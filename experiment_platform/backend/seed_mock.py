"""Seed a synthetic (CLEARLY MARKED MOCK) experiment and run the full dry run.

Usage:
    python -m experiment_platform.backend.seed_mock

This verifies the entire pipeline (create -> import phone -> insert OAI ->
align/merge -> quality -> export) with synthetic data and no real hardware.
All ids are prefixed MOCK_/TEST_; results must never be reported as real data.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .config import load_settings
from .db import Database
from .manager import ExperimentManager
from .tests.fixtures import (
    MOCK_CONDITION_ID, MOCK_EXPERIMENT_ID, MOCK_RUN_ID,
    make_oai_events_df, make_oai_snapshots_df, make_phone_samples_csv,
)


class _NoopOai:
    def rf_calibration(self, **kw):
        return {"devices": {}}


def _insert(df: pd.DataFrame, db: Database, table: str, extra_cols: dict = None) -> None:
    if extra_cols:
        for k, v in extra_cols.items():
            df[k] = v
    cols = list(df.columns)
    ph = ",".join("?" for _ in cols)
    db.executemany(f"INSERT INTO {table}({','.join(cols)}) VALUES({ph})", df.itertuples(index=False, name=None))


def _seed_sync(db: Database, run_id: str, direction: str, offset_ms: float, n: int = 15) -> None:
    for i in range(n):
        t1 = 1000.0 * i
        rtt = 2.0
        t3 = t1 + rtt
        phone_utc = (t1 + rtt / 2.0) - (-offset_ms)
        db.execute(
            "INSERT INTO sync_anchors(run_id,direction,attempt_index,t1_ms,t2_elapsed_ns,t2_utc_ms,t3_ms,rtt_ms,offset_ms,uncertainty_ms) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (run_id, direction, i, t1, 1_000_000_000 + i * 10_000_000, phone_utc, t3, rtt, offset_ms, 0.2))


def main() -> int:
    s = load_settings()
    s.ensure_dirs()
    db = Database(s.db_path)
    mgr = ExperimentManager(s, db, _NoopOai())

    print(f"Seeding MOCK experiment {MOCK_EXPERIMENT_ID} into {s.data_dir}")
    mgr.create_experiment(MOCK_EXPERIMENT_ID, "AC", operator_name="TEST", notes="MOCK synthetic dry run")
    mgr.create_condition(MOCK_CONDITION_ID, MOCK_EXPERIMENT_ID, environment="AC",
                         target_rsrp_dbm=-72.0, traffic_condition="UL_CBR",
                         pusch_target_snr_x10=150, pusch_target_snr_db=15.0, pusch_target_mode="manual",
                         scheduler_mode="auto", bandwidth_mhz=40, frequency_mhz=3349.92)
    mgr.create_run(MOCK_RUN_ID, MOCK_EXPERIMENT_ID, MOCK_CONDITION_ID, device_id="MOCK_device_1",
                   session_id="MOCK_session_1", start_delay_s=30.0)

    phone_dir = s.raw_dir / "phone" / MOCK_RUN_ID
    phone_dir.mkdir(parents=True, exist_ok=True)
    make_phone_samples_csv(phone_dir / "phone_samples.csv")

    print("Importing phone samples...")
    imported = mgr.import_phone_data(MOCK_RUN_ID, phone_dir)
    print("  imported:", imported)

    print("Inserting synthetic OAI snapshots/events...")
    snap = make_oai_snapshots_df()
    ev = make_oai_events_df()
    ev["dedup_key"] = (ev["ts_epoch_ns"].astype(str) + ":" + ev["rnti"] + ":" +
                       ev["frame"].astype(str) + ":" + ev["slot"].astype(str))
    _insert(snap, db, "oai_snapshots")
    _insert(ev, db, "oai_events")
    _seed_sync(db, MOCK_RUN_ID, "before", -5.0)
    _seed_sync(db, MOCK_RUN_ID, "after", -4.9)

    print("Aligning + merging...")
    aligned = mgr.align_and_merge(MOCK_RUN_ID)
    print("  ", {k: v for k, v in aligned.items() if k != "merged"})

    print("Quality check...")
    q = mgr.run_quality(MOCK_RUN_ID)
    print("  ", q)

    print("Exporting...")
    zip_path = mgr.export(MOCK_EXPERIMENT_ID)
    print("  zip:", zip_path)
    db.close()
    print("SYNTHETIC DRY RUN COMPLETE (MOCK data — not real experiment data)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
