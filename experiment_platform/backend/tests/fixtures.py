"""Synthetic fixtures for automated tests (CLEARLY MARKED MOCK/TEST).

These must never be presented as real experiment data. Every generated file and
experiment id is prefixed ``MOCK_`` / ``TEST_``.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

MOCK_EXPERIMENT_ID = "MOCK_AC_DRYRUN"
MOCK_RUN_ID = "MOCK_run_0001"
MOCK_CONDITION_ID = "MOCK_AC_TSNR15"

PHASE_DURATIONS = {"BASELINE": 30, "ACTIVE": 120, "TAIL": 60}
BATTERY_HZ = 5


def make_phone_samples_csv(path: Path, run_id: str = MOCK_RUN_ID,
                          experiment_id: str = MOCK_EXPERIMENT_ID,
                          condition_id: str = MOCK_CONDITION_ID) -> pd.DataFrame:
    """Generate a phone_samples.csv over baseline+active+tail at 5 Hz."""
    total_s = sum(PHASE_DURATIONS.values())
    n = total_s * BATTERY_HZ
    rng = np.random.default_rng(42)
    t_elapsed = np.arange(n) / BATTERY_HZ * 1e9  # elapsed_realtime_ns
    base_utc = 1_700_000_000_000  # some epoch ms
    utc = base_utc + np.arange(n) / BATTERY_HZ * 1000.0

    phase = np.concatenate([
        np.full(PHASE_DURATIONS["BASELINE"] * BATTERY_HZ, "BASELINE"),
        np.full(PHASE_DURATIONS["ACTIVE"] * BATTERY_HZ, "ACTIVE"),
        np.full(PHASE_DURATIONS["TAIL"] * BATTERY_HZ, "TAIL"),
    ])

    # baseline lower current; active higher (UL load); tail decays back
    base_current = 250_000.0
    active_current = 420_000.0
    current = np.where(phase == "ACTIVE", active_current, base_current)
    current += rng.normal(0, 8000, n)
    voltage = 3900.0 + rng.normal(0, 10, n)
    power_w = np.abs(current * voltage) / 1e9  # uA*mV -> W (1e-6 * 1e-3)

    rsrp = np.where(phase == "ACTIVE", -72.0, -70.0) + rng.normal(0, 0.6, n)
    sinr = np.where(phase == "ACTIVE", 22.0, 24.0) + rng.normal(0, 0.4, n)
    temp = 31.0 + np.minimum(np.arange(n) / n * 2.0, 2.0) + rng.normal(0, 0.05, n)

    df = pd.DataFrame({
        "utc_epoch_ms": utc,
        "elapsed_realtime_ns": t_elapsed.astype("int64"),
        "experiment_id": experiment_id,
        "run_id": run_id,
        "condition_id": condition_id,
        "session_id": "MOCK_session_1",
        "device_id": "MOCK_device_1",
        "phase": phase,
        "battery_current_now_ua": current,
        "battery_current_average_ua": current * 0.99,
        "battery_voltage_mv": voltage,
        "battery_power_w": power_w,
        "charge_counter_uah": np.nan,
        "soc_percent": 80.0 - np.arange(n) / n * 0.5,
        "battery_temperature_c": temp,
        "thermal_status": 0,
        "thermal_headroom": np.nan,
        "ss_rsrp_dbm": rsrp,
        "ss_rsrq_db": -11.0,
        "ss_sinr_db": sinr,
        "csi_rsrp_dbm": np.nan, "csi_rsrq_db": np.nan, "csi_sinr_db": np.nan, "csi_cqi": np.nan,
        "nrarfcn": 630624, "pci": 0, "nci": "12345678", "tac": 1, "network_type": "NR",
        "screen_state": "off", "plugged": 0, "charging": 0,
        "wifi_state": "off", "bluetooth_state": "off", "airplane_mode": 0,
        "workload_type": "UL_CBR", "workload_target_mbps": 5.0, "workload_actual_mbps": 4.8,
        "app_tx_bytes": (np.arange(n) / BATTERY_HZ * 600_000).astype("int64"),
        "app_rx_bytes": 0,
        "sample_quality_flags": "",
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return df


def make_oai_snapshots_df(run_id: str = MOCK_RUN_ID, n: int = 210) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    fetched = np.arange(n) * 1000 + 1_700_000_001_000
    ts_epoch_ns = fetched * 1_000_000 + 5_000_000  # oai clock ~ +5ms
    return pd.DataFrame({
        "run_id": run_id,
        "fetched_utc_ms": fetched,
        "ts_epoch_ns": ts_epoch_ns,
        "ts_utc": pd.to_datetime(ts_epoch_ns, unit="ns", utc=True).astype(str),
        "rnti": "220c", "imsi": "466920000000001",
        "rsrp_dbm": -72.0 + rng.normal(0, 0.5, n),
        "ssb_sinr_db": 22.0, "ph_raw_db": 38.0, "ph_normalized_db": 48.0, "pcmax_dbm": 21.0,
        "pusch_snr_db": 18.0 + rng.normal(0, 0.5, n),
        "pusch_rssi": -72.0, "pusch_rssi_unit": "dBFS",
        "ul_mcs": 14, "dl_mcs": 3, "qm": 6, "n_prb": 5,
        "cqi": 12, "ri": 1, "pmi": 0, "ul_ri": 1, "tpmi": 0,
        "ul_bler": 0.03, "dl_bler": 0.12,
        "harq_initial_tx_delta": 14.0, "harq_retransmission_delta": 18.0,
        "harq_retransmission_ratio": 0.56, "dtx": 2,
        "ul_goodput_mbps": 4.8, "dl_goodput_mbps": 0.01,
        "collection_stale": 0, "raw_json_path": "MOCK",
    })


def make_oai_events_df(run_id: str = MOCK_RUN_ID, n: int = 2000) -> pd.DataFrame:
    rng = np.random.default_rng(11)
    ts_epoch_ns = np.sort(1_700_000_001_000_000_000 + rng.integers(0, 210_000_000_000, n))
    tpc = rng.choice([-1, 0, 0, 1, 1, 1], n)  # bias toward positive
    return pd.DataFrame({
        "run_id": run_id,
        "ts_epoch_ns": ts_epoch_ns,
        "ts_utc": pd.to_datetime(ts_epoch_ns, unit="ns", utc=True).astype(str),
        "rnti": "220c", "frame": rng.integers(0, 1024, n), "slot": rng.integers(0, 20, n),
        "pusch_snr_db": 18.0 + rng.normal(0, 0.5, n),
        "ph_normalized_db": 48.0, "tpc_pusch": tpc, "tb_size_bytes": rng.integers(63, 309, n),
        "tpc_in_flight_db": 0.0, "delta_mcs_db": 0.0, "n_prb": 5, "mcs": 14,
        "rssi": -39.0, "rssi_unit": "dBFS",
    })


def write_mock_oai_json(snap: pd.DataFrame, ev: pd.DataFrame, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "snapshots.csv").to_csv(dest / "snapshots.csv", index=False)
    ev.to_csv(dest / "events.csv", index=False)
    (dest / "config_before.json").write_text(json.dumps({
        "puschTargetSnrX10": 150, "bandwidthMHz": 40, "frequencyMHz": 3349.92,
        "txGainDb": 60, "rxGainDb": 40}), encoding="utf-8")
    (dest / "config_after.json").write_text(json.dumps({
        "puschTargetSnrX10": 150, "bandwidthMHz": 40, "frequencyMHz": 3349.92,
        "txGainDb": 60, "rxGainDb": 40}), encoding="utf-8")
