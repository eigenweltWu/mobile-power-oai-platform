"""Tests for OAI client model parsing against real saved schemas + event dedup."""
from __future__ import annotations

import json
from pathlib import Path

from experiment_platform.backend import oai_models as m
from experiment_platform.backend.collectors import EventCollector

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "data" / "oai_schema"


def _load(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8-sig"))


def test_research_ues_parses():
    raw = _load("research_ues.json")
    ues = m.ResearchUes.model_validate(raw)
    assert ues.collection.stale is True
    assert ues.collection.rssiUnit == "dBFS"
    u = ues.ues[0]
    # actual field names (not the spec's "RSRP"/"PCMAX")
    assert u.rsrpDbm == -63.0
    assert u.powerControl.phRawDb == 38.0
    assert u.powerControl.pcmaxDbm == 21.0
    assert u.uplink.puschSnrDb == 32.0
    assert u.uplink.puschRssiUnit == "dBFS"
    assert u.imsi == "466920000000001"


def test_research_events_parses():
    raw = _load("research_events.json")
    ev = m.ResearchEvents.model_validate(raw)
    assert ev.count == len(ev.events) == 10
    first = ev.events[0]
    assert first.tpcPusch == 0
    assert first.rssiUnit == "dBFS"


def test_research_config_null_preserved():
    raw = _load("research_config.json")
    cfg = m.ResearchConfig.model_validate(raw)
    # null config values must stay null (missing != 0)
    assert cfg.ulBlerTargetUpper is None
    assert cfg.minGrantPrb is None
    assert cfg.deltaMcsEnabled is None
    assert cfg.puschTargetSnrX10 == 89
    assert cfg.bandwidthMHz == 100


def test_controls_parses():
    raw = _load("gnb_controls.json")
    c = m.GnbControls.model_validate(raw)
    assert c.puschTarget.mode == "manual"
    assert c.puschTarget.targetSnrDb == 8.9
    assert c.ulScheduler.mode == "auto"


def test_progress_done_semantics():
    p = m.Progress(requestId="x", active=False, action="stop", phase="complete", progress=100, error="")
    assert p.done and not p.failed
    p2 = m.Progress(requestId="x", active=True, action="restart", phase="queued", progress=4, error="")
    assert not p2.done and not p2.failed
    p3 = m.Progress(requestId="x", active=False, phase="complete", progress=100, error="boom")
    assert p3.failed


def test_apply_condition_force_restart_always_restarts(monkeypatch, tmp_path):
    """force_restart=True must issue a REAL gnb restart even when every
    parameter write answers restarted:false (plain persist, no restart hint),
    and must VERIFY it: gNB running with a NEW startedAt.
    Regression for 'template switch only submitted parameters to OAI'."""
    from types import SimpleNamespace

    from experiment_platform.backend.config import Settings
    from experiment_platform.backend.oai_client import OaiClient

    s = Settings(data_dir=tmp_path / "data", web_dist_dir=tmp_path / "web",
                 oai_host="127.0.0.1", oai_port=1)
    client = OaiClient(s)
    calls: list[tuple[str, dict]] = []

    def fake_post(path: str, payload: dict) -> dict:
        calls.append((path, dict(payload)))
        return {"restarted": False}

    monkeypatch.setattr(client, "_post", fake_post)

    # gnb_service posts directly via the httpx client (fire-and-poll)
    def fake_client_post(path: str, json: dict | None = None, **_kw):
        calls.append((path, dict(json or {})))
        if path == "/api/gnb/service":
            started["at"] = "T1"  # the process was replaced
            return SimpleNamespace(status_code=200, text="",
                                   json=lambda: {"ok": True, "requestId": "req-42"})
        raise AssertionError(f"unexpected client.post {path}")

    monkeypatch.setattr(client, "_client",
                        SimpleNamespace(post=fake_client_post))

    def fake_wait(rid, timeout_s=300.0, poll_s=2.0, on_update=None):
        return m.Progress(requestId=rid, active=False, action="restart",
                          phase="complete", progress=100, error="")

    monkeypatch.setattr(client, "wait_for_restart", fake_wait)

    # first status() call = before-evidence (startedAt T0); afterwards the
    # process was replaced (T1) — as a REAL restart would look like.
    started = {"at": "T0"}

    def fake_status():
        gnb = SimpleNamespace(running=True, startedAt=started["at"])
        return SimpleNamespace(gnb=gnb)

    monkeypatch.setattr(client, "status", fake_status)

    cfg = {"bandwidthMHz": 20, "txGainDb": 70}

    # 1) forced: restart issued, awaited, and verified via startedAt change
    res = client.apply_condition(cfg, force_restart=True)
    svc = [c for c in calls if c[0] == "/api/gnb/service"]
    assert len(svc) == 1 and svc[0][1]["action"] == "restart"
    assert svc[0][1]["requestId"].startswith("pc-")
    assert res["restart"]["requestId"] == "req-42"
    assert res["progress"].done and res["progress"].requestId == "req-42"
    assert res["restart_verified"] is True
    assert res["startedAt"]["before"] == "T0"

    # 2) default: silent writes must NOT trigger a restart
    calls.clear()
    res2 = client.apply_condition(cfg)
    assert not any(c[0] == "/api/gnb/service" for c in calls)
    assert "restart" not in res2


def test_apply_condition_force_restart_unchanged_started_at_raises(monkeypatch, tmp_path):
    """If startedAt does NOT change, the gNB never restarted — must raise
    instead of pretending success."""
    from types import SimpleNamespace

    import pytest

    from experiment_platform.backend.config import Settings
    from experiment_platform.backend.oai_client import OaiClient, OaiError

    s = Settings(data_dir=tmp_path / "data", web_dist_dir=tmp_path / "web",
                 oai_host="127.0.0.1", oai_port=1)
    client = OaiClient(s)

    monkeypatch.setattr(client, "_post",
                        lambda path, payload: {"restarted": False})
    monkeypatch.setattr(client, "_client", SimpleNamespace(
        post=lambda path, json=None, **kw: SimpleNamespace(
            status_code=200, text="",
            json=lambda: {"ok": True, "requestId": "req-42"})))
    monkeypatch.setattr(client, "wait_for_restart",
                        lambda rid, timeout_s=300.0, poll_s=2.0, on_update=None:
                        m.Progress(requestId=rid, active=False, action="restart",
                                   phase="complete", progress=100, error=""))
    monkeypatch.setattr(client, "verify_restarted",
                        lambda before, timeout_s=60.0, poll_s=3.0:
                        (False, before, True))  # still same process

    with pytest.raises(OaiError, match="did NOT actually restart"):
        client.apply_condition({"txGainDb": 70}, force_restart=True)


def test_apply_condition_force_restart_failed_progress_raises(monkeypatch, tmp_path):
    """Restart progress failed → must raise instead of continuing to rearm."""
    from types import SimpleNamespace

    import pytest

    from experiment_platform.backend.config import Settings
    from experiment_platform.backend.oai_client import OaiClient, OaiError

    s = Settings(data_dir=tmp_path / "data", web_dist_dir=tmp_path / "web",
                 oai_host="127.0.0.1", oai_port=1)
    client = OaiClient(s)

    monkeypatch.setattr(client, "_post",
                        lambda path, payload: {"restarted": False})
    monkeypatch.setattr(client, "_client", SimpleNamespace(
        post=lambda path, json=None, **kw: SimpleNamespace(
            status_code=200, text="",
            json=lambda: {"ok": True, "requestId": "req-42"})))
    monkeypatch.setattr(client, "wait_for_restart",
                        lambda rid, timeout_s=300.0, poll_s=2.0, on_update=None:
                        m.Progress(requestId=rid, active=False, action="restart",
                                   phase="error", progress=0, error="container exited"))

    with pytest.raises(OaiError, match="restart FAILED"):
        client.apply_condition({"txGainDb": 70}, force_restart=True)


def test_event_dedup_key_stable():
    ev = {"timestampEpochNs": 123, "rnti": "220c", "frame": 477, "slot": 7}
    k1 = EventCollector.dedup_key(ev)
    k2 = EventCollector.dedup_key(dict(ev))
    assert k1 == k2
    ev2 = {**ev, "slot": 8}
    assert EventCollector.dedup_key(ev2) != k1


# ---- /shake + OAI-PC clock base ------------------------------------------- #

def test_shake_returns_payload_on_success(monkeypatch, tmp_path):
    """Happy path: POST /api/shake returns the exchange payload verbatim."""
    from experiment_platform.backend.config import Settings
    from experiment_platform.backend.oai_client import OaiClient

    s = Settings(data_dir=tmp_path / "data", web_dist_dir=tmp_path / "web",
                 oai_host="127.0.0.1", oai_port=1)
    client = OaiClient(s)
    calls: list[tuple[str, dict]] = []

    def fake_post(path: str, payload: dict) -> dict:
        calls.append((path, dict(payload)))
        return {"ok": True, "ue_ip": "10.0.1.77", "rtt_ms": 18.0, "offset_ms": 3.0,
                "exchanges": [{"seq": 1, "monitoring": True}]}

    monkeypatch.setattr(client, "_post", fake_post)
    res = client.shake(n_exchanges=3)
    assert calls == [("/api/shake", {"n_exchanges": 3})]
    assert res["ok"] is True
    assert res["ue_ip"] == "10.0.1.77"
    assert res["exchanges"][0]["monitoring"] is True


def test_shake_error_body_returned_not_raised(monkeypatch, tmp_path):
    """OAI answers 502/503 with a JSON body ({ok:false, ue_ip?, error}) —
    shake() must return that body instead of raising, so callers can treat
    'no UE PDU session' / 'downlink failed' as a normal outcome."""
    import json as _json

    from experiment_platform.backend.config import Settings
    from experiment_platform.backend.oai_client import OaiClient, OaiError

    s = Settings(data_dir=tmp_path / "data", web_dist_dir=tmp_path / "web",
                 oai_host="127.0.0.1", oai_port=1)
    client = OaiClient(s)

    body = _json.dumps({"ok": False, "ue_ip": "10.0.1.48",
                        "error": "downlink failed: timed out"})

    def raise_502(path: str, payload: dict) -> dict:
        raise OaiError("POST", path, 502, body)

    monkeypatch.setattr(client, "_post", raise_502)
    res = client.shake()
    assert res["ok"] is False
    assert res["ue_ip"] == "10.0.1.48"
    assert "downlink failed" in res["error"]


def test_oai_pc_offset_ms_rtt_midpoint(monkeypatch, tmp_path):
    """oai_pc_offset_ms = PC - OAI estimated at the /api/status RTT midpoint.
    Uses the raw status dict so the real-world timestamp string shape
    (ISO-8601 with +00:00) is exercised."""
    from datetime import datetime, timezone

    from experiment_platform.backend.config import Settings
    from experiment_platform.backend.oai_client import OaiClient

    s = Settings(data_dir=tmp_path / "data", web_dist_dir=tmp_path / "web",
                 oai_host="127.0.0.1", oai_port=1)
    client = OaiClient(s)

    # OAI reports a timestamp 250 ms behind the PC clock -> offset ≈ +250:
    # the PC midpoint is ~now, so the offset lands near +250 (tolerance for
    # test scheduling jitter).
    from datetime import timedelta

    def fake_status_raw():
        return {"timestamp": (datetime.now(timezone.utc) - timedelta(milliseconds=250)).isoformat()}

    monkeypatch.setattr(client, "status_raw", fake_status_raw)
    off = client.oai_pc_offset_ms()
    assert 200.0 <= off <= 300.0

    # missing timestamp -> 0.0 fallback, never raises
    monkeypatch.setattr(client, "status_raw", lambda: {})
    assert client.oai_pc_offset_ms() == 0.0


def test_gnb_service_transport_timeout_returns_request_id(monkeypatch, tmp_path):
    """The OAI handler executes service actions SYNCHRONOUSLY (quiesce UE +
    docker stop/start + NG-setup wait — 30 s+), far beyond the 8 s shared
    client timeout. gnb_service must swallow the transport timeout (the OAI
    handler thread keeps running) and return the requestId so
    wait_for_restart() can follow the progress. Regression for
    'start experiment fails with timed out'."""
    from types import SimpleNamespace

    import httpx

    from experiment_platform.backend.config import Settings
    from experiment_platform.backend.oai_client import OaiClient

    s = Settings(data_dir=tmp_path / "data", web_dist_dir=tmp_path / "web",
                 oai_host="127.0.0.1", oai_port=1)
    client = OaiClient(s)

    # slow path: transport times out while OAI is still executing the action
    def slow_post(path, json=None, **kw):
        raise httpx.ReadTimeout("read timed out")

    monkeypatch.setattr(client, "_client", SimpleNamespace(post=slow_post))
    resp = client.gnb_service("restart")
    assert resp["ok"] is True
    assert resp["requestId"].startswith("pc-")

    # fast path: response arrives within the timeout — id echoed/attached back
    def fast_post(path, json=None, **kw):
        return SimpleNamespace(status_code=200, text="",
                               json=lambda: {"ok": True, "message": "基站已重新启动"})

    monkeypatch.setattr(client, "_client", SimpleNamespace(post=fast_post))
    resp2 = client.gnb_service("restart")
    assert resp2["requestId"].startswith("pc-")
    assert resp2["message"] == "基站已重新启动"
