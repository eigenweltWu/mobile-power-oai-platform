"""End-to-end synthetic dry run (CLEARLY MARKED MOCK).

Exercises: create experiment/condition/run -> import phone CSV -> insert OAI
snapshots/events -> align/merge -> quality -> export. Uses no real hardware and
no real OAI control (a stub OAI client is injected)."""
from __future__ import annotations

import json
import time

import pandas as pd

from experiment_platform.backend.config import Settings
from experiment_platform.backend.db import Database
from experiment_platform.backend.manager import ExperimentManager
from .fixtures import (
    MOCK_CONDITION_ID, MOCK_EXPERIMENT_ID, MOCK_RUN_ID,
    make_oai_events_df, make_oai_snapshots_df, make_phone_samples_csv,
)


class StubOai:
    def rf_calibration(self, **kw):
        return {"devices": {}}


def _make_manager(tmp_path) -> tuple[ExperimentManager, Database, Settings]:
    s = Settings(data_dir=tmp_path / "data", web_dist_dir=tmp_path / "web", oai_host="127.0.0.1", oai_port=1)
    s.ensure_dirs()
    db = Database(s.db_path)
    mgr = ExperimentManager(s, db, StubOai())
    return mgr, db, s


def _insert_snapshots(db: Database, df: pd.DataFrame) -> None:
    cols = list(df.columns)
    ph = ",".join("?" for _ in cols)
    db.executemany(f"INSERT INTO oai_snapshots({','.join(cols)}) VALUES({ph})", df.itertuples(index=False, name=None))


def _insert_events(db: Database, df: pd.DataFrame) -> None:
    df = df.copy()
    df["dedup_key"] = df["ts_epoch_ns"].astype(str) + ":" + df["rnti"] + ":" + df["frame"].astype(str) + ":" + df["slot"].astype(str)
    cols = list(df.columns)
    ph = ",".join("?" for _ in cols)
    db.executemany(f"INSERT INTO oai_events({','.join(cols)}) VALUES({ph})", df.itertuples(index=False, name=None))


def _insert_sync(db: Database, run_id: str, direction: str, offset_ms: float, n: int = 15):
    for i in range(n):
        t1 = 1000.0 * i
        rtt = 2.0
        t3 = t1 + rtt
        phone_utc = (t1 + rtt / 2.0) - (-offset_ms)
        db.execute(
            "INSERT INTO sync_anchors(run_id,direction,attempt_index,t1_ms,t2_elapsed_ns,t2_utc_ms,t3_ms,rtt_ms,offset_ms,uncertainty_ms) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (run_id, direction, i, t1, 1_000_000_000 + i * 10_000_000, phone_utc, t3, rtt, offset_ms, 0.2),
        )


def _insert_config(db: Database, run_id: str):
    db.execute("INSERT INTO oai_config(run_id,stage,config_json_path,sha256) VALUES(?,?,?,?)",
               (run_id, "before", "MOCK/before.json", "abc"))
    db.execute("INSERT INTO oai_config(run_id,stage,config_json_path,sha256) VALUES(?,?,?,?)",
               (run_id, "after", "MOCK/after.json", "abc"))


def test_clear_history_files_is_scoped_and_requires_empty_database(tmp_path):
    mgr, db, s = _make_manager(tmp_path)
    for directory in (s.raw_dir, s.processed_dir, s.data_dir / "staging", s.data_dir / "phone_backup"):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "old.txt").write_text("old", encoding="utf-8")
    tools_file = s.data_dir / "tools" / "keep.txt"
    tools_file.parent.mkdir()
    tools_file.write_text("keep", encoding="utf-8")

    mgr.create_experiment("still-present", "AC")
    try:
        mgr.clear_history_files()
        assert False, "history cleanup must reject a non-empty database"
    except ValueError:
        pass
    mgr.delete_experiment("still-present")

    result = mgr.clear_history_files()
    assert result == {"ok": True, "removed_files": 4}
    assert tools_file.read_text(encoding="utf-8") == "keep"
    assert s.raw_dir.is_dir() and s.processed_dir.is_dir()
    assert not (s.data_dir / "staging").exists()
    assert not (s.data_dir / "phone_backup").exists()


def test_full_synthetic_dry_run(tmp_path):
    mgr, db, s = _make_manager(tmp_path)

    mgr.create_experiment(MOCK_EXPERIMENT_ID, "AC", operator_name="TEST", notes="MOCK dry run")
    mgr.create_condition(MOCK_CONDITION_ID, MOCK_EXPERIMENT_ID, environment="AC",
                         target_rsrp_dbm=-72.0, traffic_condition="UL_CBR",
                         pusch_target_snr_x10=150, pusch_target_snr_db=15.0, pusch_target_mode="manual",
                         scheduler_mode="auto", bandwidth_mhz=40, frequency_mhz=3349.92)
    mgr.create_run(MOCK_RUN_ID, MOCK_EXPERIMENT_ID, MOCK_CONDITION_ID, device_id="MOCK_device_1",
                   session_id="MOCK_session_1", start_delay_s=30.0)

    # phone data
    phone_dir = tmp_path / "phone"
    phone_csv = phone_dir / "phone_samples.csv"
    make_phone_samples_csv(phone_csv)
    imported = mgr.import_phone_data(MOCK_RUN_ID, phone_dir)
    assert imported["samples"] > 1000

    # OAI data
    _insert_snapshots(db, make_oai_snapshots_df())
    _insert_events(db, make_oai_events_df())
    _insert_sync(db, MOCK_RUN_ID, "before", -5.0)
    _insert_sync(db, MOCK_RUN_ID, "after", -4.9)
    _insert_config(db, MOCK_RUN_ID)

    # align + merge
    aligned = mgr.align_and_merge(MOCK_RUN_ID)
    assert aligned["ok"]
    assert aligned["n_windows"] > 100

    # quality
    q = mgr.run_quality(MOCK_RUN_ID)
    assert q["quality_status"] in {"PASS", "WARNING", "FAILED"}
    run = db.get_run(MOCK_RUN_ID)
    assert run["state"] in {"COMPLETE", "WARNING", "FAILED"}

    # export
    zip_path = mgr.export(MOCK_EXPERIMENT_ID)
    assert zip_path.exists()
    assert zip_path.suffix == ".zip"

    # manifest sanity
    import zipfile
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert "manifest.json" in names
        assert "processed/merged_1s.csv" in names
        assert "features_m1.csv" in names and "features_m2.csv" in names and "features_m3.csv" in names
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["experiment_id"] == MOCK_EXPERIMENT_ID
        # token must never leak: only a boolean "configured" flag is allowed
        assert manifest["oai"]["oai_control_token_configured"] is False
        assert "X-OAI-Control-Token" not in json.dumps(manifest)
        # teacher/student pairing invariant: sample_id must be non-empty and unique
        import csv
        rows = list(csv.DictReader(zf.read("features_m1.csv").decode("utf-8-sig").splitlines()))
        assert rows and all(r["sample_id"] for r in rows)
        assert len({r["sample_id"] for r in rows}) == len(rows)
    db.close()
