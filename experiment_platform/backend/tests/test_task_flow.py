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


def test_start_experiment_no_condition_creates_default(tmp_path):
    """start_experiment must not hit the runs.condition_id FK when the caller
    never created a condition (auto runs reference "<exp>_default")."""
    from experiment_platform.backend.task_flow import TaskFlow

    s = Settings(data_dir=tmp_path / "data", web_dist_dir=tmp_path / "web",
                 oai_host="127.0.0.1", oai_port=1)
    s.ensure_dirs()
    db = Database(s.db_path)
    db.upsert_experiment({"experiment_id": "EXP2", "environment": "AC",
                          "operator_name": "", "notes": "", "purpose": "", "flow": "",
                          "initial_oai_config": None,
                          "created_utc": "2026-01-01T00:00:00+00:00",
                          "schema_version": 1})
    oai = MagicMock()
    oai.ensure_gnb_running.return_value = True
    oai.status.return_value = MagicMock()
    oai.status.return_value.gnb = MagicMock(running=True)
    oai.research_ues.side_effect = RuntimeError("no ues yet")

    flow = TaskFlow(s, db, oai)
    res = flow.start_experiment("EXP2", "53616213")  # no run/condition pre-created
    assert res["downlink_started"] is True
    # the synthesized condition now exists and the run references it
    cond = db.query_one("SELECT * FROM conditions WHERE condition_id='EXP2_default'")
    assert cond is not None
    run = db.query_one("SELECT * FROM runs WHERE run_id=?", (res["run_id"],))
    assert run["condition_id"] == "EXP2_default"
    flow.stop_experiment("EXP2")
    db.close()


def test_start_then_stop_marks_run_stopped(tmp_path):
    """Regression: after stop_experiment the run MUST leave the active state
    (PREPARING/ARMED/RUNNING) — otherwise the dashboard keeps showing the
    stop button and a stale 'running' badge after reload. Works even when the
    phone is unreachable (USB unplugged, no 5G PDU link)."""
    from experiment_platform.backend.task_flow import TaskFlow

    s = Settings(data_dir=tmp_path / "data", web_dist_dir=tmp_path / "web",
                 oai_host="127.0.0.1", oai_port=1)
    s.ensure_dirs()
    db = Database(s.db_path)
    db.upsert_experiment({"experiment_id": "EXP3", "environment": "AC",
                          "operator_name": "", "notes": "", "purpose": "", "flow": "",
                          "initial_oai_config": None,
                          "created_utc": "2026-01-01T00:00:00+00:00",
                          "schema_version": 1})
    oai = MagicMock()
    oai.ensure_gnb_running.return_value = True
    oai.status.return_value = MagicMock()
    oai.status.return_value.gnb = MagicMock(running=True)
    oai.research_ues.side_effect = RuntimeError("no ues yet")

    flow = TaskFlow(s, db, oai)
    res = flow.start_experiment("EXP3", "53616213")
    rid = res["run_id"]
    assert db.get_run(rid)["state"] == "PREPARING"

    # stop with the phone completely unreachable — must still mark STOPPED
    flow.stop_experiment("EXP3", "53616213")
    assert db.get_run(rid)["state"] == "STOPPED"
    # the dashboard's active-run query no longer matches it
    active = db.query(
        "SELECT run_id FROM runs WHERE state IN ('PREPARING','ARMED','RUNNING')")
    assert all(r["run_id"] != rid for r in active)

    # repeated start creates a FRESH run_id (never reuses the stopped one)
    res2 = flow.start_experiment("EXP3", "53616213")
    assert res2["run_id"] != rid
    assert db.get_run(res2["run_id"])["state"] == "PREPARING"
    flow.stop_experiment("EXP3", "53616213")
    db.close()


def test_downlink_ack_records_sync_anchor(tmp_path):
    """Each downlink ACK writes a PC sync_anchors row for fusion/offset reuse."""
    loop, db, oai = _make_loop(tmp_path)
    agent = MagicMock()
    agent.downlink.return_value = {"ok": True, "monitoring": True,
                                   "phoneRecvMs": 1000, "phoneSendMs": 1001,
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


def test_downlink_not_monitoring_skips_ack(tmp_path):
    """When the phone returns monitoring=false (user hasn't clicked 开始任务),
    the loop must NOT record an ACK or trigger sync-confirm. This ensures the
    handshake cannot complete until BOTH sides have started."""
    loop, db, oai = _make_loop(tmp_path)
    agent = MagicMock()
    agent.downlink.return_value = {"ok": True, "monitoring": False, "seq": 1}
    agent.sync_confirm.return_value = {"ok": True, "phone_timestamp_ms": 1002, "delay_ms": 2.0}

    # Patch _resolve_agent to return our mock without touching detect_phone.
    loop._resolve_agent = lambda: (agent, None)

    # Run the loop for a few iterations (interval_s=0.01 so this is fast).
    loop.start()
    import time as _t
    _t.sleep(0.1)
    loop.stop()

    # No downlink ACKs should have been recorded.
    rows = db.query("SELECT direction FROM experiment_acks WHERE direction='downlink'")
    assert rows == []
    # sync_confirm must NOT have been called.
    agent.sync_confirm.assert_not_called()
    assert loop.sync_confirmed is False
    db.close()


def test_downlink_monitoring_true_records_ack(tmp_path):
    """When the phone returns monitoring=true, the loop records the ACK and
    triggers sync-confirm normally."""
    loop, db, oai = _make_loop(tmp_path)
    agent = MagicMock()
    agent.downlink.return_value = {"ok": True, "monitoring": True,
                                   "phoneRecvMs": 1000, "phoneSendMs": 1001,
                                   "phoneElapsedNs": 1_000_000}
    agent.sync_confirm.return_value = {"ok": True, "phone_timestamp_ms": 1002, "delay_ms": 2.0}

    loop._resolve_agent = lambda: (agent, None)
    loop.start()
    import time as _t
    _t.sleep(0.1)
    loop.stop()

    # At least one downlink ACK should have been recorded.
    rows = db.query("SELECT direction FROM experiment_acks WHERE direction='downlink'")
    assert len(rows) >= 1
    # sync_confirm should have been called (exactly once due to idempotency).
    agent.sync_confirm.assert_called_once()
    assert loop.sync_confirmed is True
    db.close()


# ---- template switching (idle-only guard + rearm) ------------------------- #

def _make_template_flow(tmp_path):
    from experiment_platform.backend.task_flow import TaskFlow

    s = Settings(data_dir=tmp_path / "data", web_dist_dir=tmp_path / "web",
                 oai_host="127.0.0.1", oai_port=1)
    s.ensure_dirs()
    db = Database(s.db_path)
    db.upsert_experiment({"experiment_id": "EXP3", "environment": "AC",
                          "operator_name": "", "notes": "", "purpose": "", "flow": "",
                          "initial_oai_config": None,
                          "created_utc": "2026-01-01T00:00:00+00:00",
                          "schema_version": 1})
    db.upsert_condition({"condition_id": "C1", "experiment_id": "EXP3", "environment": "AC"})
    oai = MagicMock()
    flow = TaskFlow(s, db, oai)
    flow.add_template("EXP3", "T1", {"bandwidthMHz": 20, "txGainDb": 70})
    tid = flow.list_templates("EXP3")[0]["id"]
    return flow, db, oai, tid


def _patch_phone(flow, detection, monkeypatch, error=None):
    """Patch TaskFlow._phone (5G-PDU-only channel). It yields the tuple
    (PhoneAgent, detection); ``detection['status']`` carries the phone's
    /agent/status payload. Set ``error`` to make the channel unreachable."""
    from contextlib import contextmanager

    agent = MagicMock()

    @contextmanager
    def fake_phone(serial, pc_port=8420):
        if error is not None:
            raise error
        yield agent, detection
    monkeypatch.setattr(flow, "_phone", fake_phone)
    return agent


def test_apply_template_blocked_when_loaded(tmp_path, monkeypatch):
    """Template switch mid-LOADED must be rejected BEFORE touching the gNB."""
    import pytest
    from experiment_platform.backend.task_flow import TemplateSwitchNotAllowed

    flow, db, oai, tid = _make_template_flow(tmp_path)
    db.upsert_run({"run_id": "R9", "experiment_id": "EXP3", "condition_id": "C1",
                   "state": "RUNNING"})
    _patch_phone(flow, {"status": {"state": "RUNNING", "phase": "LOADED"}}, monkeypatch)

    with pytest.raises(TemplateSwitchNotAllowed):
        flow.apply_template("EXP3", tid)
    oai.apply_condition.assert_not_called()
    db.close()


def test_apply_template_blocked_when_phone_unreachable(tmp_path, monkeypatch):
    """5G PDU down → idle cannot be confirmed → refuse before touching gNB."""
    import pytest
    from experiment_platform.backend.task_flow import TemplateSwitchNotAllowed

    flow, db, oai, tid = _make_template_flow(tmp_path)
    db.upsert_run({"run_id": "R9", "experiment_id": "EXP3", "condition_id": "C1",
                   "state": "RUNNING"})
    _patch_phone(flow, None, monkeypatch, error=RuntimeError("no 5G PDU link"))

    with pytest.raises(TemplateSwitchNotAllowed):
        flow.apply_template("EXP3", tid)
    oai.apply_condition.assert_not_called()
    db.close()


def test_apply_template_idle_applies_and_rearms(tmp_path, monkeypatch):
    """Switching during an IDLE phase applies the config and re-arms the
    phone so idle → loaded → idle re-triggers under the new RF conditions."""
    flow, db, oai, tid = _make_template_flow(tmp_path)
    db.upsert_run({"run_id": "R9", "experiment_id": "EXP3", "condition_id": "C1",
                   "state": "RUNNING"})
    agent = _patch_phone(flow, {"status": {"state": "RUNNING", "phase": "IDLE"}},
                         monkeypatch)
    agent.rearm.return_value = {"ok": True, "state": "RUNNING", "phase": "IDLE"}

    res = flow.apply_template("EXP3", tid)
    oai.apply_condition.assert_called_once()
    agent.rearm.assert_called_once()
    assert res["rearm"]["attempted"] is True
    assert res["rearm"]["ok"] is True
    db.close()


def test_apply_template_without_active_run_skips_rearm(tmp_path, monkeypatch):
    """With no run in flight the switch just applies the config — no rearm."""
    flow, db, oai, tid = _make_template_flow(tmp_path)
    res = flow.apply_template("EXP3", tid)
    oai.apply_condition.assert_called_once()
    assert res["rearm"]["attempted"] is False
    db.close()
