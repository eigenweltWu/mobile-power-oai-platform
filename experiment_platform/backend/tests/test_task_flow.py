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
    oai.shake.return_value = {"ok": False, "ue_ip": None, "error": "no UE PDU session"}
    oai.oai_pc_offset_ms.return_value = 0.0
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
    flow, db, _oai, _tid = _make_template_flow(tmp_path)
    db.execute("DELETE FROM conditions WHERE condition_id='C1'")
    res = flow.start_experiment("EXP3", "53616213")  # no run/condition pre-created
    assert res["downlink_started"] is True
    # the synthesized condition now exists and the run references it
    cond = db.query_one("SELECT * FROM conditions WHERE condition_id='EXP3_default'")
    assert cond is not None
    run = db.query_one("SELECT * FROM runs WHERE run_id=?", (res["run_id"],))
    assert run["condition_id"] == "EXP3_default"
    flow.stop_experiment("EXP3")
    db.close()


def test_start_then_stop_discards_unconfirmed_run(tmp_path):
    """A Run stopped before sync-confirm never reached the phone and must not
    survive as platform history. Its indexed/raw platform data is discarded."""
    flow, db, _oai, _tid = _make_template_flow(tmp_path)
    s = flow.s
    res = flow.start_experiment("EXP3", "53616213")
    rid = res["run_id"]
    assert db.get_run(rid)["state"] == "PREPARING"
    raw = s.raw_dir / "oai" / "channel" / f"{rid}__1.json"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("{}", encoding="utf-8")
    db.record_file(raw)
    db.execute(
        "INSERT INTO oai_channel(run_id,fetched_utc_ms,raw_json_path) VALUES(?,?,?)",
        (rid, 1, str(raw)))

    result = flow.stop_experiment("EXP3", "53616213")
    assert result["discarded"] is True
    assert result["discard_reason"] == "sync_confirm not completed"
    assert db.get_run(rid) is None
    assert db.query("SELECT * FROM oai_channel WHERE run_id=?", (rid,)) == []
    assert db.query("SELECT * FROM run_transitions WHERE run_id=?", (rid,)) == []
    assert not raw.exists()
    active = db.query(
        "SELECT run_id FROM runs WHERE state IN ('PREPARING','ARMED','RUNNING')")
    assert all(r["run_id"] != rid for r in active)

    # repeated start creates a FRESH run_id (never reuses the stopped one)
    res2 = flow.start_experiment("EXP3", "53616213")
    assert res2["run_id"] != rid
    assert db.get_run(res2["run_id"])["state"] == "PREPARING"
    flow.stop_experiment("EXP3", "53616213")
    db.close()


def test_stop_keeps_run_after_sync_confirm(tmp_path):
    """Once sync-confirm is durable, stopping retains the Run as history."""
    flow, db, _oai, _tid = _make_template_flow(tmp_path)
    result = flow.start_experiment("EXP3", "53616213")
    rid = result["run_id"]
    db.execute(
        "INSERT INTO experiment_acks(experiment_id,run_id,seq,direction,pc_send_ms) "
        "VALUES(?,?,?,?,?)", ("EXP3", rid, -1, "sync_confirm", 1000))

    stopped = flow.stop_experiment("EXP3", "53616213")
    assert stopped["discarded"] is False
    assert db.get_run(rid)["state"] == "STOPPED"
    assert db.query_one(
        "SELECT 1 FROM experiment_acks WHERE run_id=? AND direction='pc_stop'", (rid,))
    db.close()


def test_stop_by_run_id_uses_same_discard_rule(tmp_path, monkeypatch):
    """The legacy /runs/{id}/stop path must not bypass the sync-confirm rule."""
    flow, db, _oai, _tid = _make_template_flow(tmp_path)
    db.upsert_run({"run_id": "DRAFT1", "experiment_id": "EXP3", "condition_id": "C1",
                   "state": "DRAFT"})
    agent = _patch_phone(flow, {"status": {}}, monkeypatch)
    agent.stop_task.return_value = {"stopUtcMs": 1234}

    result = flow.stop_experiment("EXP3", requested_run_id="DRAFT1")
    assert result["discarded"] is True
    assert result["phone_stop_ms"] == 1234
    assert db.get_run("DRAFT1") is None
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
    oai.shake.return_value = {"ok": True, "ue_ip": "10.0.1.99", "rtt_ms": 12.0,
                              "offset_ms": 1.5, "exchanges": []}
    oai.oai_pc_offset_ms.return_value = 0.0
    flow = TaskFlow(s, db, oai)
    flow.add_template("EXP3", "T1", {"bandwidthMHz": 20, "txGainDb": 70})
    tid = flow.list_templates("EXP3")[0]["id"]
    flow.set_default_template("EXP3", tid)
    actual = {"bandwidthMHz": 20, "txGainDb": 70}
    oai.research_config_raw.return_value = actual
    oai.status.return_value.gnb.running = True
    oai.research_ues.side_effect = RuntimeError("no ues yet")
    db.execute(
        "INSERT OR REPLACE INTO configuration_apply_state("
        "singleton_id,experiment_id,configuration_id,configuration_version,configuration_name,"
        "requested_config_json,actual_config_json,diff_json,status,verified_utc_ms) "
        "VALUES(1,?,?,?,?,?,?,?,?,?)",
        ("EXP3", tid, 1, "T1", '{"bandwidthMHz":20,"txGainDb":70}',
         '{"bandwidthMHz":20,"txGainDb":70}', '{}', "VERIFIED", 1))
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
    # the switch must force a REAL gNB restart, not just persist parameters
    assert oai.apply_condition.call_args.kwargs.get("force_restart") is True
    agent.rearm.assert_called_once()
    assert res["rearm"]["attempted"] is True
    assert res["rearm"]["ok"] is True
    db.close()


# ---- /shake integration (post-restart time sync + UE IP discovery) -------- #

def test_shake_and_refresh_records_and_refreshes_ip(tmp_path):
    """A successful /shake must (a) overwrite the cached PDU address and
    (b) record the timestamped exchanges as direction='shake' rows in
    experiment_acks + sync_anchors, so the clock status flips to synced
    even before 5G-direct downlink ACKs resume."""
    import json

    from experiment_platform.backend.task_flow import shake_and_refresh

    s = Settings(data_dir=tmp_path / "data", web_dist_dir=tmp_path / "web",
                 oai_host="127.0.0.1", oai_port=1)
    s.ensure_dirs()
    db = Database(s.db_path)
    db.upsert_experiment({"experiment_id": "EXP", "environment": "AC",
                          "operator_name": "", "notes": "", "purpose": "", "flow": "",
                          "initial_oai_config": None,
                          "created_utc": "2026-01-01T00:00:00+00:00",
                          "schema_version": 1})
    db.upsert_condition({"condition_id": "C1", "experiment_id": "EXP", "environment": "AC"})
    db.upsert_run({"run_id": "R1", "experiment_id": "EXP", "condition_id": "C1",
                   "state": "PREPARING"})

    oai = MagicMock()
    # OAI-host-stamped exchange; the phone is monitoring so timestamps exist.
    oai.shake.return_value = {
        "ok": True, "ue_ip": "10.0.1.77", "rtt_ms": 20.0, "offset_ms": 5.0,
        "monitoring": True,
        "exchanges": [{"seq": 1, "pc_send_ms": 1000.0, "pc_recv_ms": 1020.0,
                       "phone_recv_ms": 1005.0, "phone_send_ms": 1007.0,
                       "phone_elapsed_ns": 9_000_000_000, "rtt_ms": 20.0,
                       "monitoring": True}],
    }
    oai.oai_pc_offset_ms.return_value = 0.0  # same clock base for the test

    res = shake_and_refresh(s, oai, db, "EXP", run_id="R1", n_exchanges=1)

    assert res["ok"] is True
    assert res["ue_ip"] == "10.0.1.77"
    assert res["exchanges_recorded"] is True
    # (a) the cached phone_state.json now carries the NEW PDU address
    state = json.loads((s.data_dir / "phone_state.json").read_text(encoding="utf-8"))
    assert state["pdu_ip"] == "10.0.1.77"
    # (b) the shake exchange is recorded on both tables
    acks = db.query("SELECT * FROM experiment_acks WHERE direction='shake'")
    assert len(acks) == 1
    assert acks[0]["pc_send_ms"] == 1000.0
    assert acks[0]["phone_recv_ms"] == 1005.0
    anchors = db.query("SELECT * FROM sync_anchors WHERE run_id='R1' AND direction='before'")
    assert len(anchors) == 1
    assert anchors[0]["rtt_ms"] == 20.0
    db.close()


def test_shake_and_refresh_failure_never_raises(tmp_path):
    """OAI unreachable / no PDU session: /shake returns ok:false — must come
    back as a result dict, never raise into the caller (start/apply paths)."""
    from experiment_platform.backend.task_flow import shake_and_refresh

    s = Settings(data_dir=tmp_path / "data", web_dist_dir=tmp_path / "web",
                 oai_host="127.0.0.1", oai_port=1)
    s.ensure_dirs()
    db = Database(s.db_path)
    oai = MagicMock()
    oai.shake.return_value = {"ok": False, "ue_ip": None, "error": "no UE PDU session"}

    res = shake_and_refresh(s, oai, db, "EXP", run_id=None, n_exchanges=1)
    assert res["ok"] is False
    assert res["ue_ip"] is None
    assert "no UE PDU session" in res["error"]
    # nothing recorded
    assert db.query("SELECT * FROM experiment_acks WHERE direction='shake'") == []
    db.close()


def test_downlink_loop_shakes_when_phone_offline(tmp_path):
    """PDU unreachable (fresh gNB restart, new UE address): the loop must ask
    /shake to re-resolve the address instead of silently retrying the stale
    one. Throttled to one /shake per 10 s of offline probing."""
    loop, db, oai = _make_loop(tmp_path)
    oai.shake.return_value = {"ok": False, "ue_ip": None, "error": "downlink failed"}

    loop._resolve_agent = lambda: (None, None)
    loop.start()
    import time as _t
    _t.sleep(0.15)  # interval 0.01 s -> many probes, but throttled to 1 shake
    loop.stop()

    assert oai.shake.call_count >= 1
    assert loop.last_shake is not None
    assert loop.last_shake["ok"] is False
    # no ACK rows were written while offline
    assert db.query("SELECT * FROM experiment_acks WHERE direction='downlink'") == []
    db.close()


def test_apply_template_shakes_after_gnb_restart(tmp_path, monkeypatch):
    """Template switch = forced REAL gNB restart -> the UE usually re-lands
    on a NEW PDU address. The switch must run /shake (sync + IP refresh)
    between the restart and the rearm, and surface it in the response."""
    flow, db, oai, tid = _make_template_flow(tmp_path)
    db.upsert_run({"run_id": "R9", "experiment_id": "EXP3", "condition_id": "C1",
                   "state": "RUNNING"})
    agent = _patch_phone(flow, {"status": {"state": "RUNNING", "phase": "IDLE"}},
                         monkeypatch)
    agent.rearm.return_value = {"ok": True, "state": "RUNNING", "phase": "IDLE"}

    res = flow.apply_template("EXP3", tid)

    oai.shake.assert_called()
    assert res["shake"]["attempted"] is True
    assert res["shake"]["ok"] is True
    assert res["shake"]["ue_ip"] == "10.0.1.99"
    # rearm still happened after the shake
    agent.rearm.assert_called_once()
    db.close()


def test_apply_template_without_active_run_skips_rearm(tmp_path, monkeypatch):
    """With no run in flight the switch just applies the config — no rearm."""
    flow, db, oai, tid = _make_template_flow(tmp_path)
    res = flow.apply_template("EXP3", tid)
    oai.apply_condition.assert_called_once()
    assert res["rearm"]["attempted"] is False
    db.close()


# ---- every start owns a fresh gNB restart --------------------------------- #

def test_start_experiment_force_restarts_gnb(tmp_path):
    """A stopped gNB and no prior Apply state must not block Start."""
    flow, db, oai, tid = _make_template_flow(tmp_path)
    db.execute("DELETE FROM configuration_apply_state")
    oai.status.return_value.gnb.running = False
    flow.start_experiment("EXP3", "53616213")
    oai.apply_condition.assert_called_once_with(
        {"bandwidthMHz": 20, "txGainDb": 70}, force_restart=True)
    oai.ensure_gnb_running.assert_not_called()
    applied = db.query_one("SELECT * FROM configuration_apply_state")
    assert applied["configuration_id"] == tid
    assert applied["status"] == "VERIFIED"
    db.close()


def test_configuration_update_versions_and_keeps_run_snapshot(tmp_path):
    """Editing a used Configuration creates v2 while v1 remains frozen."""
    import json

    flow, db, oai, tid = _make_template_flow(tmp_path)
    original = {"bandwidthMHz": 20, "txGainDb": 70}
    applied = {"bandwidthMHz": 20, "txGainDb": 70}
    flow.set_default_template("EXP3", tid)
    oai.research_config_raw.return_value = applied

    result = flow.start_experiment("EXP3", "53616213")
    run = db.get_run(result["run_id"])
    assert run["configuration_id"] == tid
    assert run["configuration_name"] == "T1"
    assert json.loads(run["requested_config_json"]) == original
    assert json.loads(run["actual_config_json"]) == applied

    changed = flow.update_template("EXP3", tid, "T1 edited", {"bandwidthMHz": 40, "txGainDb": 65})
    assert changed["id"] != tid
    assert changed["version"] == 2
    frozen = db.get_run(result["run_id"])
    assert frozen["configuration_name"] == "T1"
    assert json.loads(frozen["requested_config_json"]) == original
    assert json.loads(frozen["actual_config_json"]) == applied

    flow.stop_experiment("EXP3")
    db.close()


def test_default_configuration_cannot_be_archived(tmp_path):
    """The explicit next-Run default must be changed before it can be archived."""
    import pytest

    flow, db, _oai, tid = _make_template_flow(tmp_path)
    flow.set_default_template("EXP3", tid)
    with pytest.raises(ValueError, match="default configuration"):
        flow.delete_template("EXP3", tid)
    assert flow.list_templates("EXP3")[0]["id"] == tid
    db.close()


def test_start_experiment_restart_failure_marks_run_error(tmp_path):
    """A failed Start-owned restart marks the fresh Run as ERROR."""
    import pytest

    flow, db, oai, _tid = _make_template_flow(tmp_path)
    db.execute("DELETE FROM configuration_apply_state")
    oai.apply_condition.side_effect = RuntimeError("gNB restart FAILED: crashed")
    with pytest.raises(RuntimeError, match="gNB restart FAILED"):
        flow.start_experiment("EXP3", "53616213")
    assert db.query_one("SELECT * FROM configuration_apply_state") is None
    assert db.query_one("SELECT state FROM runs ORDER BY rowid DESC LIMIT 1")["state"] == "ERROR"
    db.close()


def test_start_experiment_mounts_channel_collector(tmp_path):
    """Every run must mount the ChannelCollector — without it the complex-CIR
    multipath metrics never reach oai_channel and the Timeline CIR charts
    stay empty even though the frontend implements them."""
    from experiment_platform.backend.collectors import ChannelCollector

    flow, db, oai, _tid = _make_template_flow(tmp_path)
    flow.start_experiment("EXP3", "53616213")
    mounted = flow.collectors.get("EXP3", [])
    assert any(isinstance(c, ChannelCollector) for c in mounted), \
        "ChannelCollector not mounted: CIR charts on Timeline will never fill"
    # stop the threads the start spun up
    flow._stop_collectors("EXP3")
    db.close()
