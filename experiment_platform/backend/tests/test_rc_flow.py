import threading
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from experiment_platform.backend.rc_flow import RcCampaign, RcConfig, StirrerError, analyze_cir


def test_cir_analysis_counts_clusters_not_threshold_bins():
    # Two physical paths span five above-threshold bins. The old implementation
    # reported five "taps"; the traceable contract must report two paths.
    powers_db = [-90, -30, -28, -31, -90, -45, -44, -90]
    amplitudes = [10 ** (power / 20) for power in powers_db]
    result = analyze_cir({"ok": True, "cirRe": amplitudes, "cirIm": [0] * 8,
                          "dtNs": 10, "metrics": {"noiseDb": -90,
                          "rmsDelayNs": 999, "kFactorDb": -1, "peakDb": -28}},
                         noise_floor_db=-80, noise_margin_db=6)

    assert result["processing_status"] == "OK"
    assert result["effective_path_count"] == 2
    assert len(result["effective_paths"]) == 2
    assert result["detection_threshold_db"] == -74
    assert result["rms_delay_ns_raw"] == 999
    assert result["rms_delay_ns_filtered"] != 999


def test_cir_analysis_surfaces_processing_failure():
    result = analyze_cir({"ok": False, "error": "scope timeout"}, -80, 6)
    assert result == {"processing_status": "FAILED", "processing_error": "scope timeout"}


def test_resolved_paths_separate_first_from_strongest_and_keep_phase():
    powers_db = [-100, -50, -100, -100, -30, -100, -100, -100]
    amplitudes = [10 ** (power / 20) for power in powers_db]
    result = analyze_cir(
        {"ok": True, "cirRe": amplitudes, "cirIm": [0.0] * len(amplitudes),
         "dtNs": 10, "metrics": {}}, -90, 6, {"bandwidthMHz": 100})

    first, strongest = result["resolved_paths"]
    assert first["delay_ns"] == 10
    assert first["excess_delay_ns"] == 0
    assert first["is_first_detected"] is True
    assert first["is_strongest"] is False
    assert strongest["delay_ns"] == 40
    assert strongest["relative_power_db"] == 0
    assert result["first_detected_path_id"] != result["strongest_path_id"]
    assert result["nominal_delay_resolution_ns"] == 10


def test_minimum_delay_resolution_merges_close_candidate_peaks():
    powers_db = [-100, -100, -30, -60, -32, -100, -100, -100, -100, -100]
    amplitudes = [10 ** (power / 20) for power in powers_db]
    result = analyze_cir(
        {"ok": True, "cirRe": amplitudes, "cirIm": [0.0] * len(amplitudes),
         "dtNs": 10, "metrics": {}}, -90, 6, {"bandwidthMHz": 20})

    assert result["raw_delay_bin_count"] == 10
    assert result["candidate_peak_count"] == 2
    assert result["effective_peak_count"] == 2
    assert result["resolved_path_count"] == 1
    assert result["minimum_resolvable_separation_ns"] == 50


def test_configured_delay_window_is_recorded_not_hard_coded():
    powers_db = [-100, -30, -100, -100, -100, -100, -100, -100]
    amplitudes = [10 ** (power / 20) for power in powers_db]
    result = analyze_cir(
        {"ok": True, "cirRe": amplitudes, "cirIm": [0.0] * len(amplitudes),
         "dtNs": 10, "metrics": {}}, -90, 6,
        {"bandwidthMHz": 100, "rcChamber": {
            "delay_window_start_ns": 5, "delay_window_end_ns": 45}})

    assert result["analysis_delay_window_ns"] == {
        "start_ns": 5.0, "end_ns": 45.0,
        "source": "CONFIGURED_CHAMBER_AND_SYSTEM_WINDOW"}


def _servo_campaign(runtime_applied: bool) -> RcCampaign:
    campaign = RcCampaign.__new__(RcCampaign)
    campaign.oai = Mock()
    campaign.oai.gnb_controls.return_value = SimpleNamespace(
        puschTarget=SimpleNamespace(mode="auto", targetSnrX10=200))
    campaign.oai.gnb_pusch_target_snr.return_value = {
        "runtimeApplied": runtime_applied,
        "target": {"effectiveChanged": True},
    }
    campaign.oai.new_request_id.side_effect = (
        lambda prefix, action: f"{prefix or 'pc'}-{action}-id")
    campaign.cfg = RcConfig({
        "target_rssp_db": 20,
        "rssp_tol_db": 1,
        "gain_alpha": 0.1,
        "max_servo_iters": 1,
        "listening_period_s": 1,
        "settle_time_s": 0,
    })
    campaign._stop = threading.Event()
    campaign.run_id = "R1"
    campaign.current_sample_index = 1
    campaign.pusch_x10 = None
    campaign.last_rssp_db = None
    campaign.log = []
    campaign._rssp_db = lambda: 30.0
    return campaign


def test_servo_reads_nested_target_and_requires_hot_apply():
    campaign = _servo_campaign(runtime_applied=True)

    log = campaign.servo_pusch(Mock())

    campaign.oai.gnb_pusch_target_snr.assert_called_once_with(
        "manual", 190, restart=False, request_id="R1-rc-1-servo-1-id")
    assert campaign.pusch_x10 == 190
    assert log[0]["pusch_x10"] == 200
    assert log[0]["gain_alpha"] == 0.1
    assert log[0]["requested_target_delta_db"] == -1.0
    assert log[0]["target_delta_db"] == -1.0
    assert campaign.servo_failure_reason == "MAX_ITERATIONS_WITHOUT_CONVERGENCE"


def test_servo_can_adjust_rx_gain_instead_of_target_snr():
    campaign = _servo_campaign(runtime_applied=True)
    campaign.cfg = RcConfig({
        "calibration_actuator": "rx_gain", "target_rssp_db": 20,
        "rssp_tol_db": 1, "gain_alpha": 0.33, "max_servo_iters": 1,
        "listening_period_s": 1, "settle_time_s": 0,
    })
    campaign.flow = Mock()
    campaign.flow.apply_calibration_gain.return_value = {"ok": True}
    campaign.experiment_id = "RC1"
    campaign.serial = "UE1"
    campaign.current_tx_gain_db = 60
    campaign.current_rx_gain_db = 40
    campaign._rssp_db = lambda: 30.03

    log = campaign.servo_pusch(Mock())

    campaign.flow.apply_calibration_gain.assert_called_once_with(
        "RC1", "R1", "UE1", 60, 36.69)
    campaign.oai.gnb_pusch_target_snr.assert_not_called()
    assert campaign.current_rx_gain_db == 36.69
    assert log[0]["applied_rx_gain_db"] == 36.69


def test_servo_requests_intervention_when_listening_period_expires():
    campaign = _servo_campaign(runtime_applied=True)
    campaign._average_rssp = lambda: None

    assert campaign.servo_pusch(Mock()) == []
    assert campaign.servo_failure_reason == "LISTENING_PERIOD_EXPIRED_WITHOUT_PUSCH_RSSI"


def test_user_configuration_resumes_paused_campaign():
    campaign = RcCampaign.__new__(RcCampaign)
    campaign._stop = threading.Event()
    campaign._resume_after_configuration = threading.Event()
    campaign.intervention_required = False
    campaign.intervention_reason = None
    campaign.intervention_started_ms = None
    campaign.log = []
    campaign.oai = Mock()
    campaign.oai.gnb_controls.return_value = SimpleNamespace(
        puschTarget=SimpleNamespace(targetSnrX10=270))
    campaign.oai.telemetry_config_raw.return_value = {"txGainDb": 63, "rxGainDb": 42}
    outcome = []
    worker = threading.Thread(target=lambda: outcome.append(
        campaign._pause_for_configuration("MAX_ITERATIONS_WITHOUT_CONVERGENCE")))

    worker.start()
    assert campaign._resume_after_configuration.wait(0.01) is False
    assert campaign.intervention_required is True
    campaign.resume_after_configuration()
    worker.join(1)

    assert outcome == [True]
    assert campaign.intervention_required is False
    assert campaign.pusch_x10 == 270
    assert campaign.current_tx_gain_db == 63


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
        "auto", None, restart=False, request_id="R1-rc-restore-pusch-id")
    assert campaign.pusch_x10 == 200


def test_rc_campaign_waits_for_post_start_sync_anchor():
    campaign = RcCampaign.__new__(RcCampaign)
    campaign.log = []
    loop = SimpleNamespace(sync_confirmed=False)

    class StopAfterSync:
        def wait(self, _seconds):
            loop.sync_confirmed = True
            return False

        def is_set(self):
            return False

    campaign._stop = StopAfterSync()

    assert campaign._wait_for_sync(loop) is True
    assert campaign.state == "waiting_sync"
    assert campaign.log[-1]["msg"] == "UE synchronized; Run time anchor recorded"


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
