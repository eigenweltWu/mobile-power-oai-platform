from types import SimpleNamespace
from unittest.mock import Mock

from experiment_platform.backend.rssp_calibration import (
    RsspCalibration, average_measurement, read_rssp_db, read_ue_rsrp_dbm,
    proportional_target_x10,
)


def _oai(rssp_values):
    oai = Mock()
    oai.health.return_value = {"ok": True}
    oai.status_raw.return_value = {
        "gnb": {"running": True}, "radio": {"txGainDb": 60, "rxGainDb": 40},
    }
    oai.gnb_controls.return_value = SimpleNamespace(
        puschTarget=SimpleNamespace(
            mode="auto", targetSnrX10=200, autoTargetSnrX10=200,
        )
    )
    values = iter(rssp_values)
    oai.telemetry_ues_raw.side_effect = lambda: {
        "collection": {"stale": False},
        "ues": [{
            "ageSeconds": 0.2,
            "uplink": {"puschRssi": next(values), "puschRssiUnit": "dBFS"},
        }],
    }
    oai.gnb_pusch_target_snr.return_value = {
        "runtimeApplied": True, "target": {"effectiveChanged": True},
    }
    oai.new_request_id.side_effect = lambda prefix, action: f"{prefix}-{action}"
    return oai


def test_oai_only_calibration_converges_and_restores_initial_policy():
    oai = _oai([-70.0, -60.4])
    job = RsspCalibration(oai, {
        "target_rssp_db": -60.0, "rssp_tol_db": 1.0,
        "gain_alpha": 0.1, "max_servo_iters": 4,
        "servo_settle_s": 0,
    })

    job.run()

    assert job.state == "converged"
    assert [point["rssp_db"] for point in job.records] == [-70.0, -60.4]
    assert job.records[0]["applied_x10"] == 210
    assert job.records[0]["gain_alpha"] == 0.1
    assert job.records[0]["requested_target_delta_db"] == 1.0
    assert job.records[0]["target_delta_db"] == 1.0
    assert oai.gnb_pusch_target_snr.call_args_list[0].args == ("manual", 210)
    assert oai.gnb_pusch_target_snr.call_args_list[0].kwargs["restart"] is False
    assert oai.gnb_pusch_target_snr.call_args_list[-1].args == ("auto", None)
    assert oai.gnb_pusch_target_snr.call_args_list[-1].kwargs["restart"] is False
    assert job.pusch_x10 == 200


def test_calibration_rejects_missing_fresh_pusch_rssi():
    oai = _oai([])
    oai.telemetry_ues_raw.side_effect = lambda: {
        "collection": {"stale": True}, "ues": [],
    }
    job = RsspCalibration(oai, {"servo_settle_s": 0, "observation_timeout_s": 1})

    job.run()

    assert job.state == "error"
    assert "No fresh OAI PUSCH RSSI" in (job.error or "")
    oai.gnb_pusch_target_snr.assert_not_called()


def test_ue_rsrp_calibration_changes_only_tx_gain_and_restores_gains():
    oai = _oai([])
    values = iter([-90.0, -80.4])
    oai.telemetry_ues_raw.side_effect = lambda: {
        "collection": {"stale": False},
        "ues": [{"ageSeconds": 0.2, "rsrpDbm": next(values)}],
    }
    oai.apply_condition.return_value = {"restart_verified": True}
    job = RsspCalibration(oai, {
        "measurement": "ue_rsrp", "actuator": "tx_gain", "target_db": -80,
        "tolerance_db": 1, "gain_alpha": 0.1, "max_servo_iters": 3,
        "servo_settle_s": 0,
    })

    job.run()

    assert job.state == "converged"
    first, restore = oai.apply_condition.call_args_list
    assert first.args[0] == {"txGainDb": 61.0, "rxGainDb": 40.0}
    assert first.kwargs["force_restart"] is True
    assert restore.args[0] == {"txGainDb": 60.0, "rxGainDb": 40.0}
    assert job.records[0]["applied_actuator"] == "tx_gain"
    assert job.records[0]["gain_alpha"] == 0.1
    assert job.records[0]["gain_delta_db"] == 1.0


def test_read_rssp_uses_strongest_fresh_dbfs_observation_only():
    oai = Mock()
    oai.telemetry_ues_raw.return_value = {
        "collection": {"stale": False},
        "ues": [
            {"ageSeconds": 0.5, "uplink": {"puschRssi": -72, "puschRssiUnit": "dBFS"}},
            {"ageSeconds": 1.0, "uplink": {"puschRssi": -65, "puschRssiUnit": "dBFS"}},
            {"ageSeconds": 9.0, "uplink": {"puschRssi": -20, "puschRssiUnit": "dBFS"}},
            {"ageSeconds": 0.2, "uplink": {"puschRssi": -10, "puschRssiUnit": "raw"}},
        ],
    }

    assert read_rssp_db(oai) == -65.0


def test_read_ue_rsrp_uses_fresh_ue_report_only():
    oai = Mock()
    oai.telemetry_ues_raw.return_value = {
        "collection": {"stale": False},
        "ues": [{"ageSeconds": 0.2, "rsrpDbm": -81},
                {"ageSeconds": 7.0, "rsrpDbm": -20}],
    }
    assert read_ue_rsrp_dbm(oai) == -81.0


def test_measurement_waits_for_signal_then_averages_settle_window(monkeypatch):
    clock = [0.0]
    values = iter([None, -60.0, -58.0, -56.0])

    class Event:
        def is_set(self):
            return False

        def wait(self, seconds):
            clock[0] += seconds
            return False

    monkeypatch.setattr("experiment_platform.backend.rssp_calibration.time.monotonic",
                        lambda: clock[0])

    assert average_measurement(Mock(), lambda _oai: next(values), 3, 2, Event()) == (-58.0, 3)


def test_target_control_calculates_hundredths_before_oai_x10_quantization():
    assert proportional_target_x10(200, 1.23, 0.25) == (203, 0.31, 0.3)
