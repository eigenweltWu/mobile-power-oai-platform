import json
from unittest.mock import MagicMock

import pandas as pd

from experiment_platform.backend.config import Settings
from experiment_platform.backend.db import Database
from experiment_platform.backend.task_flow import TaskFlow


def test_timeline_and_clip_are_scoped_to_selected_run(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", web_dist_dir=tmp_path / "web")
    settings.ensure_dirs()
    db = Database(settings.db_path)
    db.upsert_experiment({
        "experiment_id": "EXP", "environment": "AC", "operator_name": "",
        "notes": "", "purpose": "", "flow": "", "initial_oai_config": None,
        "created_utc": "2026-01-01T00:00:00+00:00", "schema_version": 1,
    })
    db.upsert_condition({"condition_id": "C", "experiment_id": "EXP", "environment": "AC"})
    for run_id, t0 in (("R1", 1_000), ("R2", 100_000)):
        db.upsert_run({"run_id": run_id, "experiment_id": "EXP", "condition_id": "C",
                       "state": "STOPPED", "started_utc_ms": t0})
        db.execute("INSERT INTO sync_anchors(run_id,direction,attempt_index,t1_ms) VALUES(?,?,?,?)",
                   (run_id, "before", 0, t0))
        db.execute("INSERT INTO phone_samples(run_id,utc_epoch_ms,elapsed_realtime_ns) VALUES(?,?,?)",
                   (run_id, t0 + 500, t0 * 1_000_000))

    flow = TaskFlow(settings, db, MagicMock())
    db.execute("INSERT INTO oai_channel(run_id,fetched_utc_ms,tap_count) VALUES(?,?,?)",
               ("R2", 100_700, 99))
    timeline = flow.timeline("EXP", "R2")
    assert [row["run_id"] for row in timeline["samples"]] == ["R2"]
    assert timeline["t0_utc_ms"] == 100_000
    assert timeline["environment"] == "AC"
    assert timeline["channel"] == []
    assert flow.timeline("EXP", "R2", include_channel=True)["channel"][0]["tap_count"] == 99

    result = flow.clip("EXP", "R2", 0, 1_000, "selected")
    clipped = pd.read_csv(result["path"])
    assert set(clipped["run_id"]) == {"R2"}
    db.close()


def test_multi_segment_clip_preserves_order_and_source_time(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", web_dist_dir=tmp_path / "web")
    settings.ensure_dirs()
    db = Database(settings.db_path)
    db.upsert_experiment({"experiment_id": "EXP", "environment": "AC", "operator_name": "",
                          "notes": "", "purpose": "", "flow": "", "initial_oai_config": None,
                          "created_utc": "2026-01-01T00:00:00+00:00", "schema_version": 1})
    db.upsert_condition({"condition_id": "C", "experiment_id": "EXP", "environment": "AC"})
    db.upsert_run({"run_id": "R1", "experiment_id": "EXP", "condition_id": "C",
                   "state": "STOPPED", "started_utc_ms": 1_000})
    db.execute("INSERT INTO sync_anchors(run_id,direction,attempt_index,t1_ms) VALUES(?,?,?,?)",
               ("R1", "before", 0, 1_000))
    for timestamp in (1_100, 1_200, 1_500, 1_600):
        db.execute("INSERT INTO phone_samples(run_id,utc_epoch_ms,elapsed_realtime_ns) VALUES(?,?,?)",
                   ("R1", timestamp, timestamp * 1_000_000))

    flow = TaskFlow(settings, db, MagicMock())
    result = flow.save_clip("EXP", "R1", "two windows", [
        {"source_run_id": "R1", "source_start_relative_ms": 100,
         "source_end_relative_ms": 250, "label": "first"},
        {"source_run_id": "R1", "source_start_relative_ms": 500,
         "source_end_relative_ms": 650, "label": "second"},
    ])

    assert result["segments"] == 2
    assert result["duration_ms"] == 300
    rows = db.query("SELECT * FROM clip_segments WHERE clip_id=? ORDER BY segment_order",
                    (result["clip_id"],))
    assert [row["label"] for row in rows] == ["first", "second"]
    fused = pd.read_csv(result["path"])
    assert fused["segment_order"].tolist() == [1, 1, 2, 2]
    assert fused["source_t_s"].tolist() == [0.1, 0.2, 0.5, 0.6]
    assert fused["clip_t_s"].tolist() == [0.0, 0.1, 0.15, 0.25]
    db.close()


def test_legacy_rc_timeline_derives_window_from_full_cir_before_downsampling(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", web_dist_dir=tmp_path / "web")
    settings.ensure_dirs()
    db = Database(settings.db_path)
    db.upsert_experiment({"experiment_id": "RC", "environment": "RC", "operator_name": "",
                          "notes": "", "purpose": "", "flow": "", "initial_oai_config": None,
                          "created_utc": "2026-01-01T00:00:00+00:00", "schema_version": 1})
    db.upsert_condition({"condition_id": "C", "experiment_id": "RC", "environment": "RC"})
    db.upsert_run({"run_id": "R", "experiment_id": "RC", "condition_id": "C",
                   "state": "STOPPED", "started_utc_ms": 1_000})
    re = [0.0] * 1024
    re[1], re[20], re[-1] = 100.0, 10.0, 50.0
    raw_path = tmp_path / "sample.json"
    raw_path.write_text(json.dumps({"cir": {"ok": True, "cirRe": re,
                                              "cirIm": [0.0] * 1024,
                                              "dtNs": 10}}), encoding="utf-8")
    db.execute("INSERT INTO rc_samples(experiment_id,run_id,sample_index,started_utc_ms,"
               "ended_utc_ms,raw_json_path) VALUES(?,?,?,?,?,?)",
               ("RC", "R", 1, 1_100, 1_200, str(raw_path)))

    sample = TaskFlow(settings, db, MagicMock()).timeline("RC", "R")["rc_samples"][0]

    assert sample["display_delay_window_ns"]["end_ns"] == 250
    assert sample["display_delay_window_ns"]["source"].endswith("peak - 30 dB fallback")
    assert max(point["power_db"] for point in sample["pdp"]) == 40
    db.close()
