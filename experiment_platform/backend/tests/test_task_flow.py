"""Tests for the downlink loop + sync-confirm handshake (task §B1).

Mocks the phone agent and OAI client; verifies the first ACK triggers exactly
one sync-confirm (with the gNB timestamp) and that the phone-side idempotency
holds (a repeat confirm is a no-op at the loop level).
"""
from __future__ import annotations

from unittest.mock import MagicMock

from experiment_platform.backend.config import Settings
from experiment_platform.backend.db import Database
from experiment_platform.backend.task_flow import DEFAULT_PHASES, DownlinkLoop


class FakeUes:
    # 1.7e15 ns -> 1.7e9 ms when integer-divided by 1e6
    timestampEpochNs = 1_700_000_000_000_000


def _make_loop(tmp_path) -> tuple[DownlinkLoop, Database, MagicMock]:
    from datetime import datetime, timezone
    s = Settings(data_dir=tmp_path / "data", web_dist_dir=tmp_path / "web",
                 oai_host="127.0.0.1", oai_port=1)
    s.ensure_dirs()
    db = Database(s.db_path)
    # experiment + condition must exist before the run (FK constraints are on)
    db.upsert_experiment({"experiment_id": "EXP", "environment": "AC",
                          "operator_name": "", "notes": "", "purpose": "", "flow": "",
                          "initial_oai_config": None,
                          "created_utc": datetime.now(timezone.utc).isoformat(),
                          "schema_version": 1})
    db.upsert_condition({"condition_id": "C1", "experiment_id": "EXP", "environment": "AC"})
    db.upsert_run({"run_id": "R1", "experiment_id": "EXP", "condition_id": "C1",
                   "state": "PREPARING"})
    oai = MagicMock()
    oai.research_ues.return_value = FakeUes()
    plan = {"experimentId": "EXP", "runId": "R1", "conditionId": "C1",
            "environment": "AC", "startDelaySeconds": 1.0, "phases": DEFAULT_PHASES}
    loop = DownlinkLoop("EXP", "53616213", 8420, db, s, oai, plan,
                        run_id="R1", interval_s=0.01)
    return loop, db, oai


def test_first_ack_triggers_sync_confirm_once(tmp_path):
    loop, db, oai = _make_loop(tmp_path)
    agent = MagicMock()
    agent.sync_confirm.return_value = {"ok": True, "phone_timestamp_ms": 1002, "delay_ms": 2.0}

    loop._trigger_sync_confirm(agent)
    assert loop.sync_confirmed is True
    agent.sync_confirm.assert_called_once()
    # the plan was handed to the phone (3rd positional arg)
    assert agent.sync_confirm.call_args.args[2]["runId"] == "R1"

    # second call must not re-trigger (loop-level idempotency)
    loop._trigger_sync_confirm(agent)
    assert agent.sync_confirm.call_count == 1

    # the sync_confirm ack row carries the gNB data timestamp
    rows = db.query("SELECT direction, gnb_data_timestamp_ms FROM experiment_acks "
                    "WHERE direction='sync_confirm'")
    assert len(rows) == 1
    assert rows[0]["gnb_data_timestamp_ms"] == 1_700_000_000  # ns // 1e6
    db.close()


def test_sync_confirm_failure_leaves_unconfirmed(tmp_path):
    """A transient failure must not set sync_confirmed — the next ACK retries."""
    loop, db, oai = _make_loop(tmp_path)
    agent = MagicMock()
    agent.sync_confirm.side_effect = RuntimeError("phone busy")

    loop._trigger_sync_confirm(agent)
    assert loop.sync_confirmed is False
    # no sync_confirm ack row recorded
    rows = db.query("SELECT direction FROM experiment_acks WHERE direction='sync_confirm'")
    assert rows == []
    db.close()


def test_downlink_ack_records_sync_anchor(tmp_path):
    """Each downlink ACK writes a PC sync_anchors row for fusion/offset reuse."""
    loop, db, oai = _make_loop(tmp_path)
    agent = MagicMock()
    agent.downlink.return_value = {"phoneRecvMs": 1000, "phoneSendMs": 1001,
                                   "phoneElapsedNs": 1_000_000}
    agent.sync_confirm.return_value = {"ok": True, "phone_timestamp_ms": 1002, "delay_ms": 2.0}

    # simulate one iteration's body (without the sleep/loop)
    t_send = 500.0
    resp = agent.downlink(1, t_send)
    t_recv = 502.0
    phone_recv = resp["phoneRecvMs"]
    phone_send = resp["phoneSendMs"]
    t2_utc = (phone_recv + phone_send) / 2.0
    loop.db.execute(
        "INSERT INTO sync_anchors(run_id,direction,attempt_index,t1_ms,t2_elapsed_ns,t2_utc_ms,t3_ms,rtt_ms,offset_ms) VALUES(?,?,?,?,?,?,?,?,?)",
        (loop.run_id, "before", 1, t_send, resp["phoneElapsedNs"], t2_utc, t_recv, t_recv - t_send, (t_send + (t_recv - t_send) / 2.0) - t2_utc))
    rows = db.query("SELECT * FROM sync_anchors WHERE run_id='R1' AND direction='before'")
    assert len(rows) == 1
    assert rows[0]["t2_utc_ms"] == 1000.5
    db.close()
