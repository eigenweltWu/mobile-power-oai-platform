"""Tests for quality flags."""
from __future__ import annotations

import numpy as np
import pandas as pd

from experiment_platform.backend.quality import compute_quality


def _phone(**kw):
    base = {
        "plugged": 0, "charging": 0, "wifi_state": "off", "screen_state": "off",
        "battery_temperature_c": 31.0, "thermal_status": 0, "nci": "1", "pci": 0,
        "ss_rsrp_dbm": -72.0,
    }
    base.update(kw)
    return pd.DataFrame([base, {**base, "battery_temperature_c": base["battery_temperature_c"] + 0.1}])


def test_pass_when_clean():
    q = compute_quality(phone=_phone(), snapshots=pd.DataFrame([{"collection_stale": 0}]),
                        events=pd.DataFrame([{"tpc_pusch": 0}]), pre=None, post=None,
                        config_before=None, config_after=None, target_rsrp_dbm=-72.0)
    assert q["quality_status"] == "PASS"
    assert q["quality_flags"] == []


def test_phone_data_missing_fails():
    q = compute_quality(phone=None, snapshots=pd.DataFrame([{"collection_stale": 0}]),
                        events=None, pre=None, post=None, config_before=None,
                        config_after=None, target_rsrp_dbm=None)
    assert q["quality_status"] == "FAILED"
    assert "PHONE_DATA_MISSING" in q["quality_flags"]


def test_charging_and_wifi_flags():
    q = compute_quality(phone=_phone(charging=1, wifi_state="on"), snapshots=pd.DataFrame([{"collection_stale": 0}]),
                        events=None, pre=None, post=None, config_before=None,
                        config_after=None, target_rsrp_dbm=-72.0)
    assert "PHONE_CHARGING" in q["quality_flags"]
    assert "WIFI_ON" in q["quality_flags"]
    assert q["quality_status"] == "WARNING"


def test_config_changed_flag():
    before = {"puschTargetSnrX10": 150, "bandwidthMHz": 40}
    after = {"puschTargetSnrX10": 200, "bandwidthMHz": 40}
    q = compute_quality(phone=_phone(), snapshots=pd.DataFrame([{"collection_stale": 0}]),
                        events=None, pre=None, post=None, config_before=before,
                        config_after=after, target_rsrp_dbm=-72.0)
    assert "CONFIG_CHANGED" in q["quality_flags"]


def test_rsrp_out_of_target():
    q = compute_quality(phone=_phone(ss_rsrp_dbm=-90.0), snapshots=pd.DataFrame([{"collection_stale": 0}]),
                        events=None, pre=None, post=None, config_before=None,
                        config_after=None, target_rsrp_dbm=-72.0)
    assert "RSRP_OUT_OF_TARGET" in q["quality_flags"]


def test_cell_changed():
    phone = _phone()
    phone.loc[1, "nci"] = "999"
    q = compute_quality(phone=phone, snapshots=pd.DataFrame([{"collection_stale": 0}]),
                        events=None, pre=None, post=None, config_before=None,
                        config_after=None, target_rsrp_dbm=-72.0)
    assert "CELL_CHANGED" in q["quality_flags"]
