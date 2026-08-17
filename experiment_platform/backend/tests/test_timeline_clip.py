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
    timeline = flow.timeline("EXP", "R2")
    assert [row["run_id"] for row in timeline["samples"]] == ["R2"]
    assert timeline["t0_utc_ms"] == 100_000

    result = flow.clip("EXP", "R2", 0, 1_000, "selected")
    clipped = pd.read_csv(result["path"])
    assert clipped["run_id"].tolist() == ["R2"]
    db.close()
