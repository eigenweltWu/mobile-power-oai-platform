import threading
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from experiment_platform.backend.rc_flow import RcCampaign, RcConfig, StirrerError


def _servo_campaign(runtime_applied: bool) -> RcCampaign:
    campaign = RcCampaign.__new__(RcCampaign)
    campaign.oai = Mock()
    campaign.oai.gnb_controls.return_value = SimpleNamespace(
        puschTarget=SimpleNamespace(mode="auto", targetSnrX10=200))
    campaign.oai.gnb_pusch_target_snr.return_value = {
        "runtimeApplied": runtime_applied,
        "target": {"effectiveChanged": True},
    }
    campaign.cfg = RcConfig({
        "target_rssp_db": 20,
        "rssp_tol_db": 1,
        "pusch_step_x10": 10,
        "max_servo_iters": 1,
        "servo_settle_s": 0,
    })
    campaign._stop = threading.Event()
    campaign.pusch_x10 = None
    campaign.last_rssp_db = None
    campaign.log = []
    campaign._rssp_db = lambda: 30.0
    return campaign


def test_servo_reads_nested_target_and_requires_hot_apply():
    campaign = _servo_campaign(runtime_applied=True)

    log = campaign.servo_pusch(Mock())

    campaign.oai.gnb_pusch_target_snr.assert_called_once_with(
        "manual", 190, restart=False)
    assert campaign.pusch_x10 == 190
    assert log[0]["pusch_x10"] == 200


def test_servo_rejects_config_only_update():
    campaign = _servo_campaign(runtime_applied=False)

    with pytest.raises(StirrerError, match="not applied to the running gNB"):
        campaign.servo_pusch(Mock())


def test_campaign_restores_initial_target_without_restart():
    campaign = _servo_campaign(runtime_applied=True)
    campaign.initial_pusch_mode = "auto"
    campaign.initial_pusch_x10 = 200
    campaign.pusch_x10 = 190

    campaign.restore_pusch_target()

    campaign.oai.gnb_pusch_target_snr.assert_called_once_with(
        "auto", None, restart=False)
    assert campaign.pusch_x10 == 200


def test_phone_window_waits_for_idle_before_rearm(monkeypatch):
    campaign = RcCampaign.__new__(RcCampaign)
    campaign.cfg = RcConfig({"settle_s": 0, "dwell_s": 0})
    campaign._stop = threading.Event()
    campaign.log = []
    agent = Mock()
    agent.status.side_effect = [
        {"phase": "LOADED"}, {"phase": "IDLE"},
        {"phase": "LOADED"}, {"phase": "IDLE"},
    ]
    agent.rearm.return_value = {"ok": True}
    campaign._phone = lambda: nullcontext((agent, None))
    monkeypatch.setattr("experiment_platform.backend.rc_flow.time.sleep", lambda _: None)

    result = campaign.trigger_phone_window({"runId": "rc-run"})

    assert result is not None
    assert agent.status.call_count == 4
    agent.rearm.assert_called_once()
