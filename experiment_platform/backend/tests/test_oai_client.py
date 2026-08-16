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


def test_event_dedup_key_stable():
    ev = {"timestampEpochNs": 123, "rnti": "220c", "frame": 477, "slot": 7}
    k1 = EventCollector.dedup_key(ev)
    k2 = EventCollector.dedup_key(dict(ev))
    assert k1 == k2
    ev2 = {**ev, "slot": 8}
    assert EventCollector.dedup_key(ev2) != k1
