"""Reverberation-chamber (RC) acquisition flow.

Unlike the anechoic chamber (AC — record continuously, clip later), the RC
flow is SAMPLED: after every stirrer step the platform

  1. waits for the stirrer to stand still,
  2. fine-tunes ``puschTargetSnrX10`` (multiple small steps, never a gNB
     restart) so the authoritative OAI PUSCH RSSI (dBFS) returns to the
     configured target — the stirrer must not change the receiver's RSSP,
  3. triggers ONE timed phone recording window (idle → loaded(dwell) via
     /agent/session + /agent/rearm — same run id, so all samples land in a
     single run), while the platform collectors keep recording gNB
     snapshots / CIR at 1 Hz,
  4. after the loaded window ends IMMEDIATELY grabs a final CIR, filters
     its taps against the calibrated noise floor and stores one
     ``rc_samples`` row (stirrer angle, pusch setting, RSSP, filtered
     multipath metrics, window timestamps).

The noise floor is calibrated once per campaign from a stack of CIR frames
(no workload required — the scope CIR is UL-reference driven), giving a
per-tap median power profile; taps within ``noise_margin_db`` of it are
discarded when counting multipath components.
"""
from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
from scipy.signal import find_peaks

from .config import Settings
from .db import Database
from .oai_client import OaiClient
from .stirrer import StirrerAgent, StirrerError


def _now_ms() -> int:
    return int(time.time() * 1000)


def _tap_powers_db(cir: dict) -> list[float]:
    """PDP power per delay sample from the cached 8787 PHY snapshot."""
    cached = cir.get("cir") or []
    if cached:
        return [float(point["powerDb"]) for point in cached
                if isinstance(point, dict) and point.get("powerDb") is not None]
    re = cir.get("cirRe") or []
    im = cir.get("cirIm") or []
    n = min(len(re), len(im))
    return [10.0 * math.log10(max(re[i] * re[i] + im[i] * im[i], 1e-30))
            for i in range(n)]


PROCESSING_ALGORITHM = "standard-ifft-local-peak"
PROCESSING_VERSION = "3.0"
NOISE_METHOD = "median of per-tap calibration medians, strongest 10% excluded"
PEAK_PROMINENCE_DB = 3.0


def _clusters_above(values_db: list[float], threshold_db: float) -> list[list[int]]:
    """Return contiguous threshold regions on the circular PDP.

    A single physical impulse occupies several adjacent FFT bins.  Counting
    bins (the old implementation) inflated one path into tens or hundreds of
    "taps".  One contiguous circular region is therefore one candidate path.
    """
    groups: list[list[int]] = []
    for i, value in enumerate(values_db):
        if value < threshold_db:
            continue
        if groups and groups[-1][-1] == i - 1:
            groups[-1].append(i)
        else:
            groups.append([i])
    if len(groups) > 1 and groups[0][0] == 0 and groups[-1][-1] == len(values_db) - 1:
        groups[0] = groups.pop() + groups[0]
    return groups


def analyze_cir(cir: Optional[dict], noise_floor_db: Optional[float],
                noise_margin_db: float, measurement: Optional[dict] = None) -> dict:
    """Create the traceable channel-analysis contract consumed by Result.

    Resolved paths are local peaks that pass noise, prominence and minimum
    delay-separation constraints. Backend-supplied metrics remain available
    separately and are never relabelled as resolved results.
    """
    if not cir or not cir.get("ok"):
        return {"processing_status": "FAILED", "processing_error":
                (cir or {}).get("error") or "CIR unavailable"}
    powers_db = _tap_powers_db(cir)
    if not powers_db:
        return {"processing_status": "FAILED", "processing_error": "empty CIR"}
    if noise_floor_db is None or not math.isfinite(float(noise_floor_db)):
        return {"processing_status": "FAILED", "processing_error":
                "noise estimation failed"}

    peak_db = max(powers_db)
    metrics = cir.get("metrics") or {}
    raw_noise = metrics.get("noiseDb")
    raw_threshold = max(float(raw_noise) + 6.0 if raw_noise is not None else -math.inf,
                        peak_db - 25.0)
    threshold_db = float(noise_floor_db) + float(noise_margin_db)
    dt_ns = float(cir.get("dtNs") or 8.138)
    n = len(powers_db)
    cfg = measurement or {}
    chamber_cfg = cfg.get("rcChamber") or {}
    prominence_db = float(chamber_cfg.get("peak_prominence_db", PEAK_PROMINENCE_DB))
    configured_bw = cfg.get("bandwidthMHz") or cir.get("bandwidthMHz")
    sampled_bw_mhz = 1000.0 / dt_ns
    effective_bw_mhz = float(configured_bw) if configured_bw else sampled_bw_mhz
    nominal_resolution_ns = 1000.0 / effective_bw_mhz
    min_separation_ns = max(dt_ns, nominal_resolution_ns)
    min_separation_bins = max(1, math.ceil(min_separation_ns / dt_ns))
    values = np.asarray(powers_db, dtype=float)
    tiled = np.tile(values, 3)

    def circular_peaks(*, prominence: Optional[float] = None,
                       distance: Optional[int] = None) -> tuple[list[int], dict[int, float]]:
        kwargs: dict[str, Any] = {"height": threshold_db}
        if prominence is not None:
            kwargs["prominence"] = prominence
        if distance is not None:
            kwargs["distance"] = distance
        indexes, props = find_peaks(tiled, **kwargs)
        selected = [(position, int(index - n)) for position, index in enumerate(indexes)
                    if n <= index < 2 * n]
        prominence_values = props.get("prominences", [])
        return ([index for _, index in selected],
                {index: float(prominence_values[position]) for position, index in selected}
                if len(prominence_values) else {})

    candidate_indexes, _ = circular_peaks()
    effective_indexes, _ = circular_peaks(prominence=prominence_db)
    resolved_indexes, prominences = circular_peaks(
        prominence=prominence_db, distance=min_separation_bins)
    resolved_indexes.sort()

    re = cir.get("cirRe") or []
    im = cir.get("cirIm") or []
    strongest_index = max(resolved_indexes, key=lambda index: powers_db[index]) if resolved_indexes else None
    first_index = resolved_indexes[0] if resolved_indexes else None
    strongest_power = powers_db[strongest_index] if strongest_index is not None else None
    paths = []
    for path_number, index in enumerate(resolved_indexes, 1):
        margin = powers_db[index] - threshold_db
        prominence = prominences.get(index, 0.0)
        confidence = "HIGH" if margin >= 12 and prominence >= 6 else (
            "MEDIUM" if margin >= 6 else "LOW")
        paths.append({
            "path_id": f"P{path_number}", "path": path_number,
            "peak_index": index, "delay_ns": round(index * dt_ns, 3),
            "excess_delay_ns": round((index - int(first_index)) * dt_ns, 3),
            "complex_gain_real": float(re[index]), "complex_gain_imag": float(im[index]),
            "magnitude_db": round(powers_db[index], 3), "power_db": round(powers_db[index], 3),
            "relative_power_db": round(powers_db[index] - float(strongest_power), 3),
            "phase_deg": round(math.degrees(math.atan2(float(im[index]), float(re[index]))), 3),
            "phase_calibration": ((cir.get("calibration") or {}).get("phase") or "UNCALIBRATED"),
            "noise_floor_db": round(float(noise_floor_db), 3),
            "threshold_db": round(threshold_db, 3),
            "snr_above_noise_db": round(powers_db[index] - float(noise_floor_db), 3),
            "margin_above_threshold_db": round(margin, 3),
            "peak_prominence_db": round(prominence, 3),
            "near_threshold": margin < 3.0, "resolved": True, "confidence": confidence,
            "is_first_detected": index == first_index,
            "is_strongest": index == strongest_index,
            "detection_algorithm": PROCESSING_ALGORITHM,
            "processing_version": PROCESSING_VERSION,
        })

    if paths:
        energies = [10 ** (path["power_db"] / 10.0) for path in paths]
        delays = [path["excess_delay_ns"] for path in paths]
        total = sum(energies)
        mean_delay = sum(delay * energy for delay, energy in zip(delays, energies)) / total
        rms_delay = math.sqrt(sum((delay - mean_delay) ** 2 * energy
                                  for delay, energy in zip(delays, energies)) / total)
        strongest_energy = max(energies)
        remainder = total - strongest_energy
        k_factor = 10.0 * math.log10(strongest_energy / remainder) if remainder > 0 else None
        filtered_peak = max(path["power_db"] for path in paths)
    else:
        mean_delay = rms_delay = k_factor = filtered_peak = None

    configured_window_start = float(chamber_cfg.get("delay_window_start_ns") or 0.0)
    configured_window_end = float(chamber_cfg.get("delay_window_end_ns") or 0.0)
    capture_end_ns = (n - 1) * dt_ns
    if configured_window_end > configured_window_start:
        analysis_window = {
            "start_ns": round(max(0.0, configured_window_start), 3),
            "end_ns": round(min(capture_end_ns, configured_window_end), 3),
            "source": "CONFIGURED_CHAMBER_AND_SYSTEM_WINDOW",
        }
    elif paths:
        margin_ns = max(4.0 * nominal_resolution_ns, 2.0 * float(rms_delay or 0.0))
        auto_end = max(path["delay_ns"] for path in paths) + margin_ns
        auto_end = math.ceil(auto_end / nominal_resolution_ns) * nominal_resolution_ns
        analysis_window = {"start_ns": 0.0,
                           "end_ns": round(min(capture_end_ns, auto_end), 3),
                           "source": "AUTO_RESOLVED_PATH_ENVELOPE"}
    else:
        analysis_window = None

    center_mhz = cfg.get("frequencyMHz") or cir.get("frequencyMHz")
    frequency_spacing_mhz = sampled_bw_mhz / n
    complex_values = np.asarray(re, dtype=float) + 1j * np.asarray(im, dtype=float)
    raw_h_re = cir.get("frequencyResponseRe") or cir.get("hRe") or []
    raw_h_im = cir.get("frequencyResponseIm") or cir.get("hIm") or []
    raw_frequencies = cir.get("frequencyHz") or cir.get("frequenciesHz") or []
    has_raw_h = len(raw_h_re) == len(raw_h_im) == n
    has_raw_grid = len(raw_frequencies) == n
    response = (np.asarray(raw_h_re, dtype=float) + 1j * np.asarray(raw_h_im, dtype=float)
                if has_raw_h else np.fft.fft(complex_values))
    display_step = max(1, n // 512)
    start_mhz = float(center_mhz) - sampled_bw_mhz / 2 if center_mhz else 0.0
    frequency_response = [{
        "frequency_mhz": round(float(raw_frequencies[index]) / 1e6 if has_raw_grid
                               else start_mhz + index * frequency_spacing_mhz, 6),
        "real": round(float(response[index].real), 9),
        "imag": round(float(response[index].imag), 9),
        "magnitude_db": round(20.0 * math.log10(max(abs(response[index]), 1e-15)), 3),
        "phase_deg": round(math.degrees(math.atan2(response[index].imag, response[index].real)), 3),
    } for index in range(0, n, display_step)]
    complex_cir = [{
        "delay_ns": round(index * dt_ns, 3), "real": round(float(re[index]), 9),
        "imag": round(float(im[index]), 9),
        "magnitude": round(abs(complex_values[index]), 9),
        "power_db": round(powers_db[index], 3),
        "phase_deg": round(math.degrees(math.atan2(float(im[index]), float(re[index]))), 3),
    } for index in range(0, n, display_step)]

    return {
        "processing_status": "OK",
        "processing_error": None,
        "processing_algorithm": PROCESSING_ALGORITHM,
        "processing_version": PROCESSING_VERSION,
        "noise_method": NOISE_METHOD,
        "noise_floor_db": round(float(noise_floor_db), 3),
        "noise_margin_db": float(noise_margin_db),
        "detection_threshold_db": round(threshold_db, 3),
        "raw_detection_threshold_db": round(raw_threshold, 3),
        "raw_delay_bin_count": n,
        "candidate_peak_count": len(candidate_indexes),
        "effective_peak_count": len(effective_indexes),
        "resolved_path_count": len(paths),
        # Compatibility aliases. These are not raw tap/path semantics.
        "raw_path_count": len(candidate_indexes),
        "effective_path_count": len(paths),
        "removed_path_count": max(0, len(candidate_indexes) - len(paths)),
        "rms_delay_ns_raw": metrics.get("rmsDelayNs"),
        "rms_delay_ns_filtered": round(rms_delay, 3) if rms_delay is not None else None,
        "mean_delay_ns_filtered": round(mean_delay, 3) if mean_delay is not None else None,
        "k_factor_db_raw": metrics.get("kFactorDb"),
        "k_factor_db_filtered": round(k_factor, 3) if k_factor is not None else None,
        "peak_db_raw": metrics.get("peakDb"),
        "peak_db_filtered": round(filtered_peak, 3) if filtered_peak is not None else None,
        "delay_reference": "FIRST_RESOLVED_COMPONENT",
        "first_detected_path_id": paths[0]["path_id"] if paths else None,
        "strongest_path_id": next((path["path_id"] for path in paths if path["is_strongest"]), None),
        "effective_bandwidth_mhz": round(effective_bw_mhz, 6),
        "sampled_fft_bandwidth_mhz": round(sampled_bw_mhz, 6),
        "nominal_delay_resolution_ns": round(nominal_resolution_ns, 6),
        "minimum_resolvable_separation_ns": round(min_separation_ns, 6),
        "capture_delay_window_ns": {"start_ns": 0.0, "end_ns": round(capture_end_ns, 3)},
        "analysis_delay_window_ns": analysis_window,
        "peak_prominence_threshold_db": prominence_db,
        "peak_detection_method": "scipy.signal.find_peaks on circular PDP",
        "window_function": cir.get("windowFunction") or "UPSTREAM_UNAVAILABLE",
        "frequency_spacing_mhz": round(frequency_spacing_mhz, 9),
        "frequency_grid_consistency": ("CONSISTENT" if has_raw_grid and n > 1 and np.allclose(
            np.diff(np.asarray(raw_frequencies, dtype=float)),
            np.diff(np.asarray(raw_frequencies, dtype=float))[0]) else
            ("SYNTHESIZED_GRID" if has_raw_h else "RECONSTRUCTED_FROM_COMPLEX_CIR")),
        "frequency_response_source": "RAW_COMPLEX_H_F" if has_raw_h else "FFT_FROM_COMPLEX_CIR",
        "measurement_metadata": {
            "start_frequency_mhz": round(float(raw_frequencies[0]) / 1e6, 6)
            if has_raw_grid else round(start_mhz, 6),
            "end_frequency_mhz": round(float(raw_frequencies[-1]) / 1e6, 6)
            if has_raw_grid else round(start_mhz + (n - 1) * frequency_spacing_mhz, 6),
            "effective_bandwidth_mhz": round(effective_bw_mhz, 6),
            "n_frequency_points": n, "frequency_spacing_mhz": round(
                (float(raw_frequencies[1]) - float(raw_frequencies[0])) / 1e6
                if has_raw_grid and n > 1 else frequency_spacing_mhz, 9),
            "missing_frequency_samples": 0 if has_raw_grid else None,
            "window_function": cir.get("windowFunction") or "UPSTREAM_UNAVAILABLE",
            "fft_ifft_length": n,
        },
        "calibration": cir.get("calibration") or {
            "amplitude": "UNAVAILABLE", "phase": "UNCALIBRATED",
            "cable_delay": "UNAVAILABLE", "system_response": "UNAVAILABLE"},
        "complex_cir": complex_cir,
        "frequency_response": frequency_response,
        "resolved_paths": paths,
        "effective_paths": paths,
    }


class RcConfig:
    """Per-campaign knobs (validated defaults — the UI can override each)."""

    DEFAULTS = {
        "sync_exchanges": 3,
        "sync_max_rtt_ms": 1000.0,
        "step_deg": 5.0,          # stirrer increment per sample
        "n_steps": 12,            # number of samples (stirrer realizations)
        "dwell_s": 20.0,          # loaded window = the actual measurement
        "settle_s": 8.0,          # mechanical settle after the stirrer stops
        "target_rssp_db": -60.0,  # gNB PUSCH RSSI target (dBFS)
        "rssp_tol_db": 1.5,       # servo deadband
        "pusch_step_x10": 10,     # ±1.0 dB target-SNR per servo iteration
        "adjust_target_snr": 1,
        "adjust_tx_gain": 0,
        "tx_gain_step_db": 1.0,
        "max_servo_iters": 6,
        "servo_settle_s": 5.0,    # TPC propagation wait between iterations
        "noise_frames": 20,       # 底噪标定帧数
        "noise_margin_db": 6.0,   # taps below floor+margin are noise
        "peak_prominence_db": 3.0,
        "delay_window_start_ns": 0.0,
        "delay_window_end_ns": 0.0,
        "stirrer_speed_deg_s": 20.0,
        "execution_mode": "REAL_HARDWARE",
    }

    def __init__(self, cfg: Optional[dict] = None):
        c = dict(self.DEFAULTS)
        c.update({k: v for k, v in (cfg or {}).items() if v is not None})
        self.step_deg = float(c["step_deg"])
        self.sync_exchanges = max(1, int(c["sync_exchanges"]))
        self.sync_max_rtt_ms = float(c["sync_max_rtt_ms"])
        self.n_steps = int(c["n_steps"])
        self.dwell_s = float(c["dwell_s"])
        self.settle_s = float(c["settle_s"])
        self.target_rssp_db = float(c["target_rssp_db"])
        self.rssp_tol_db = float(c["rssp_tol_db"])
        self.pusch_step_x10 = int(c["pusch_step_x10"])
        self.adjust_target_snr = bool(int(c["adjust_target_snr"]))
        self.adjust_tx_gain = bool(int(c["adjust_tx_gain"]))
        self.tx_gain_step_db = float(c["tx_gain_step_db"])
        self.max_servo_iters = int(c["max_servo_iters"])
        self.servo_settle_s = float(c["servo_settle_s"])
        self.noise_frames = int(c["noise_frames"])
        self.noise_margin_db = float(c["noise_margin_db"])
        self.peak_prominence_db = float(c["peak_prominence_db"])
        self.delay_window_start_ns = float(c["delay_window_start_ns"])
        self.delay_window_end_ns = float(c["delay_window_end_ns"])
        self.stirrer_speed_deg_s = float(c["stirrer_speed_deg_s"])
        legacy_simulation = bool(c.get("simulate_stirrer", False))
        self.execution_mode = str(c.get("execution_mode") or
                                  ("SIMULATION" if legacy_simulation else "REAL_HARDWARE")).upper()
        if self.execution_mode not in {"REAL_HARDWARE", "SIMULATION"}:
            raise ValueError("execution_mode must be REAL_HARDWARE or SIMULATION")
        self.simulate_stirrer = self.execution_mode == "SIMULATION"

    def as_dict(self) -> dict:
        return {
            "sync_exchanges": self.sync_exchanges,
            "sync_max_rtt_ms": self.sync_max_rtt_ms,
            "step_deg": self.step_deg, "n_steps": self.n_steps,
            "dwell_s": self.dwell_s, "settle_s": self.settle_s,
            "target_rssp_db": self.target_rssp_db, "rssp_tol_db": self.rssp_tol_db,
            "pusch_step_x10": self.pusch_step_x10, "max_servo_iters": self.max_servo_iters,
            "adjust_target_snr": int(self.adjust_target_snr),
            "adjust_tx_gain": int(self.adjust_tx_gain),
            "tx_gain_step_db": self.tx_gain_step_db,
            "servo_settle_s": self.servo_settle_s, "noise_frames": self.noise_frames,
            "noise_margin_db": self.noise_margin_db,
            "peak_prominence_db": self.peak_prominence_db,
            "delay_window_start_ns": self.delay_window_start_ns,
            "delay_window_end_ns": self.delay_window_end_ns,
            "stirrer_speed_deg_s": self.stirrer_speed_deg_s,
            "execution_mode": self.execution_mode,
        }


class RcCampaign(threading.Thread):
    """One RC acquisition campaign for one experiment (background thread)."""

    def __init__(self, flow, settings: Settings, db: Database, oai: OaiClient,
                 experiment_id: str, serial: str, pc_port: int, cfg: RcConfig):
        super().__init__(daemon=True, name=f"rc-{experiment_id}")
        self.flow = flow
        self.s = settings
        self.db = db
        self.oai = oai
        self.experiment_id = experiment_id
        self.serial = serial
        self.pc_port = pc_port
        self.cfg = cfg
        self._stop = threading.Event()
        # live state surfaced to the UI
        self.state = "preparing"
        self.samples_done = 0
        self.current_angle_deg: Optional[float] = None
        self.pusch_x10: Optional[int] = None
        self.initial_pusch_mode: Optional[str] = None
        self.initial_pusch_x10: Optional[int] = None
        self.last_rssp_db: Optional[float] = None
        self.noise_floor_db: Optional[float] = None
        self.noise_std_db: Optional[float] = None
        self.noise_frame_count = 0
        self.log: list[dict] = []
        self.error: Optional[str] = None
        self.started_ms = _now_ms()
        self.run_id: Optional[str] = None
        self.current_sample_index = 0
        self.current_tx_gain_db: Optional[float] = None
        self.current_rx_gain_db: Optional[float] = None
        self.calibration_records: list[dict] = []

    # ---- helpers ------------------------------------------------------------ #
    def _say(self, stage: str, msg: str) -> None:
        self.log.append({"ms": _now_ms(), "stage": stage, "msg": msg})
        self.log = self.log[-200:]

    def _cir(self, timeout_s: float = 10.0) -> Optional[dict]:
        """Fresh CIR from the 8787 cached PHY snapshot."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and not self._stop.is_set():
            try:
                raw = self.oai.channel_cir()
                if raw.get("ok") and (raw.get("cir") or raw.get("cirRe") or []):
                    return raw
            except Exception:
                pass
            time.sleep(1.0)
        return None

    def _rssp_db(self) -> Optional[float]:
        """gNB receive-power proxy from OAI PUSCH RSSI (explicitly dBFS).

        CIR amplitudes are uncalibrated scope units and cannot be compared to
        a target such as -60 dBFS.  The previous proxy made the live servo see
        values around +57 dB against a -60 target.  OAI already exposes the
        measured uplink ``puschRssi`` and its unit, so use that authoritative
        receive-power observation instead.
        """
        raw = self.oai.telemetry_ues_raw()
        if (raw.get("collection") or {}).get("stale"):
            return None
        values = []
        for ue in raw.get("ues") or []:
            age = ue.get("ageSeconds")
            if age is None or float(age) > 5.0:
                continue
            uplink = ue.get("uplink") or {}
            value = uplink.get("puschRssi")
            if value is not None and uplink.get("puschRssiUnit") == "dBFS":
                values.append(float(value))
        return max(values) if values else None

    def _phone(self):
        return self.flow._phone(self.serial, self.pc_port)

    # ---- stages -------------------------------------------------------------- #
    def calibrate_noise(self, stirrer: StirrerAgent) -> Optional[dict]:
        """底噪采集: stack CIR frames, per-tap median → noise profile file."""
        self._say("noise", f"capturing {self.cfg.noise_frames} CIR frames for noise floor")
        stacks: list[list[float]] = []
        for _ in range(self.cfg.noise_frames):
            if self._stop.is_set():
                return None
            raw = self._cir(timeout_s=8.0)
            if raw:
                stacks.append(_tap_powers_db(raw))
            time.sleep(0.5)
        if len(stacks) < max(3, self.cfg.noise_frames // 3):
            self._say("noise", "not enough CIR frames — noise calibration skipped")
            return None
        n = min(len(t) for t in stacks)
        per_tap_median = []
        for i in range(n):
            col = sorted(t[i] for t in stacks)
            per_tap_median.append(col[len(col) // 2])
        # the floor = median across taps EXCLUDING the strongest 10% (the
        # signal taps) so a live UL signal does not lift the estimate
        ranked = sorted(per_tap_median)
        cut = max(1, int(len(ranked) * 0.9))
        floor_db = ranked[:cut][len(ranked[:cut]) // 2]
        frame_floors = []
        for taps in stacks:
            ordered = sorted(taps)
            frame_cut = max(1, int(len(ordered) * 0.9))
            frame_floors.append(ordered[:frame_cut][len(ordered[:frame_cut]) // 2])
        mean_floor = sum(frame_floors) / len(frame_floors)
        std_floor = math.sqrt(sum((value - mean_floor) ** 2 for value in frame_floors) /
                              len(frame_floors))
        self.noise_floor_db = floor_db
        self.noise_std_db = std_floor
        self.noise_frame_count = len(stacks)
        out_dir = self.s.raw_dir / "rc" / (self.run_id or self.experiment_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "noise_profile.json"
        path.write_text(json.dumps({
            "captured_utc_ms": _now_ms(), "frames": len(stacks),
            "n_taps": n, "per_tap_median_db": [round(v, 2) for v in per_tap_median],
            "noise_floor_db": round(floor_db, 2),
            "noise_std_db": round(std_floor, 3),
            "noise_method": NOISE_METHOD,
            "margin_db": self.cfg.noise_margin_db,
        }, ensure_ascii=False), encoding="utf-8")
        self.db.record_file(path)
        self._say("noise", f"noise floor {floor_db:.1f} dB from {len(stacks)} frames")
        return {"noise_floor_db": floor_db, "noise_std_db": std_floor,
                "frames": len(stacks), "n_taps": n, "path": str(path)}

    def servo_pusch(self, stirrer: StirrerAgent) -> list[dict]:
        """多次微调 puschTargetSnrX10 until receiver RSSP returns to target.

        Direction: raising the gNB PUSCH target SNR makes TPC command the UE
        UP, raising received power — so error>0 (too weak) → raise target.
        """
        log: list[dict] = []
        # current setting from the OAI controls (authoritative)
        try:
            ctl = self.oai.gnb_controls()
            target = getattr(ctl, "puschTarget", None)
            cur = getattr(target, "targetSnrX10", None)
            if cur is not None:
                self.pusch_x10 = int(cur)
        except Exception:
            pass
        if self.pusch_x10 is None:
            self.pusch_x10 = 100  # 10.0 dB fallback start point
        for it in range(self.cfg.max_servo_iters):
            if self._stop.is_set():
                break
            rssp = self._rssp_db()
            self.last_rssp_db = rssp
            if rssp is None:
                self._say("servo", "authoritative OAI PUSCH RSSI unavailable — skipping servo")
                return log
            err = self.cfg.target_rssp_db - rssp
            point = {"ms": _now_ms(), "sample_index": getattr(self, "current_sample_index", 0),
                     "iter": it, "rssp_db": round(rssp, 2),
                     "target_rssp_db": self.cfg.target_rssp_db,
                     "err_db": round(err, 2), "pusch_x10": self.pusch_x10,
                     "target_snr_db": self.pusch_x10 / 10.0,
                     "tx_gain_db": getattr(self, "current_tx_gain_db", None)}
            log.append(point)
            if not hasattr(self, "calibration_records"):
                self.calibration_records = []
            self.calibration_records.append(dict(point))
            self.calibration_records = self.calibration_records[-300:]
            if abs(err) <= self.cfg.rssp_tol_db:
                self._say("servo", f"RSSP {rssp:.1f} dB within ±{self.cfg.rssp_tol_db} dB of target")
                return log
            # scale the step: full step for large errors, half near the band
            step = self.cfg.pusch_step_x10 if abs(err) > 2 * self.cfg.rssp_tol_db \
                else max(1, self.cfg.pusch_step_x10 // 2)
            new_x10 = self.pusch_x10 + (step if err > 0 else -step)
            new_x10 = max(0, min(400, new_x10))  # 0..40 dB sanity clamp
            self._say("servo", f"RSSP {rssp:.1f} dB (err {err:+.1f}) → pusch {self.pusch_x10 / 10:.1f}→{new_x10 / 10:.1f} dB")
            try:
                # restart=false ALWAYS: a per-sample gNB restart (~40 s, UE
                # re-attach) would destroy the sampled measurement cadence.
                if not self.cfg.adjust_target_snr and not self.cfg.adjust_tx_gain:
                    raise StirrerError("power calibration has no enabled actuator")
                if self.cfg.adjust_target_snr:
                    r = self.oai.gnb_pusch_target_snr(
                        "manual", new_x10, restart=False,
                        request_id=self.oai.new_request_id(
                            getattr(self, "run_id", None),
                            f"rc-{getattr(self, 'current_sample_index', 0)}-servo-{it + 1}"))
                    if not r.get("runtimeApplied"):
                        raise StirrerError(
                            "PUSCH target update was not applied to the running gNB; "
                            "refusing to record an invalid servo sample")
                    self.pusch_x10 = new_x10
                    log[-1]["applied_x10"] = new_x10
                    log[-1]["resp"] = str(r)[:200]
                if self.cfg.adjust_tx_gain:
                    if self.current_tx_gain_db is None or self.current_rx_gain_db is None:
                        raise StirrerError("TX/RX Gain unavailable for power calibration")
                    gain = max(0.0, min(100.0, self.current_tx_gain_db +
                                       (self.cfg.tx_gain_step_db if err > 0 else -self.cfg.tx_gain_step_db)))
                    gain_result = self.flow.apply_calibration_gain(
                        self.experiment_id, self.run_id or self.experiment_id,
                        self.serial, gain, self.current_rx_gain_db)
                    self.current_tx_gain_db = gain
                    log[-1]["applied_tx_gain_db"] = gain
                    log[-1]["gain_resp"] = str(gain_result)[:200]
            except Exception as e:
                log[-1]["apply_error"] = str(e)[:200]
                self._say("servo", f"pusch apply failed: {e}")
                raise
            time.sleep(self.cfg.servo_settle_s)
        self._say("servo", "servo iterations exhausted — continuing with last setting")
        return log

    def restore_pusch_target(self) -> None:
        """Restore the campaign's initial power-control policy without restart."""
        if self.initial_pusch_mode not in {"auto", "manual"}:
            return
        value = self.initial_pusch_x10 if self.initial_pusch_mode == "manual" else None
        result = self.oai.gnb_pusch_target_snr(
            self.initial_pusch_mode, value, restart=False,
            request_id=self.oai.new_request_id(
                getattr(self, "run_id", None), "rc-restore-pusch"))
        changed = bool((result.get("target") or {}).get("effectiveChanged"))
        if changed and not result.get("runtimeApplied"):
            raise StirrerError(
                "failed to restore the initial PUSCH target in the running gNB")
        self.pusch_x10 = self.initial_pusch_x10
        self._say(
            "servo",
            f"restored PUSCH target {self.initial_pusch_mode} / "
            f"{(self.initial_pusch_x10 or 0) / 10:.1f} dB",
        )

    def trigger_phone_window(self, plan: dict) -> Optional[dict]:
        """One timed record window: per-sample plan + rearm, then watch phases.

        The phone (already ARMED via the normal start flow) re-runs
        idle → loaded(dwell) → idle with the SAME run id. We poll /agent/status
        to stamp the loaded window boundaries — immune to the phone's local
        idle-override stretching the head phase.
        """
        sample_plan = dict(plan)
        sample_plan["phases"] = [
            {"name": "idle", "durationSeconds": self.cfg.settle_s},
            {"name": "loaded", "durationSeconds": self.cfg.dwell_s},
            {"name": "idle", "durationSeconds": 0.0},
        ]
        sample_plan["idleSeconds"] = self.cfg.settle_s
        sample_plan["collectionSeconds"] = self.cfg.dwell_s
        try:
            with self._phone() as (agent, _ph):
                # The normal experiment plan may still be in its initial
                # LOADED phase while noise calibration and the first stirrer
                # move finish. Rearm is valid only from IDLE, so wait instead
                # of silently dropping the first RC sample.
                idle_deadline = time.monotonic() + 150.0
                while time.monotonic() < idle_deadline and not self._stop.is_set():
                    if agent.status().get("phase") == "IDLE":
                        break
                    time.sleep(0.4)
                else:
                    self._say("phone", "phone never returned to IDLE — sample window lost")
                    return None
                agent.session(sample_plan)
                r = agent.rearm()
                if not r.get("ok"):
                    self._say("phone", f"rearm rejected: {r}")
                    return None
                self._say("phone", f"rearmed — waiting for LOADED ({self.cfg.dwell_s:.0f}s)")
                # wait for LOADED (bounded by phone idle override + settle + margin)
                t_wait = self.cfg.settle_s + 120.0
                t0 = time.monotonic()
                started_ms = None
                while time.monotonic() - t0 < t_wait and not self._stop.is_set():
                    st = agent.status()
                    if st.get("phase") == "LOADED":
                        started_ms = _now_ms()
                        break
                    time.sleep(0.4)
                if started_ms is None:
                    self._say("phone", "phone never reached LOADED — sample window lost")
                    return None
                # wait for LOADED to end → the workload stops exactly at dwell_s
                deadline = started_ms + int((self.cfg.dwell_s + 30.0) * 1000)
                while _now_ms() < deadline and not self._stop.is_set():
                    st = agent.status()
                    if st.get("phase") != "LOADED":
                        return {"started_utc_ms": started_ms, "ended_utc_ms": _now_ms()}
                    time.sleep(0.4)
                return {"started_utc_ms": started_ms, "ended_utc_ms": _now_ms(),
                        "note": "loaded window did not close in time (force-ended)"}
        except Exception as e:
            self._say("phone", f"phone window failed: {e}")
            return None

    def finalize_sample(self, index: int, angle_deg: float, window: dict,
                        servo_log: list[dict], noise: Optional[dict]) -> None:
        """Grab final CIR and persist one window-consistent analytics object."""
        raw = self._cir(timeout_s=8.0)
        run = self.db.query_one(
            "SELECT configuration_snapshot_json FROM runs WHERE run_id=?", (self.run_id,)) or {}
        try:
            measurement = json.loads(run.get("configuration_snapshot_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            measurement = {}
        channel = analyze_cir(raw, self.noise_floor_db, self.cfg.noise_margin_db,
                              measurement)
        m = (raw or {}).get("metrics") or {}
        # OAI exposes interval deltas. Summing deltas inside the exact loaded
        # window gives per-Sample HARQ counts; cumulative harqRounds are never
        # mistaken for a Sample value.
        gnb_summary: dict[str, Any] = {}
        try:
            row = self.db.query_one(
                "SELECT AVG(ul_goodput_mbps) ul_goodput_mbps,"
                " AVG(dl_goodput_mbps) dl_goodput_mbps, AVG(pusch_snr_db) pusch_snr_db,"
                " AVG(ul_mcs) ul_mcs, AVG(ul_bler) ul_bler, AVG(dl_bler) dl_bler,"
                " SUM(ul_harq_initial_tx_delta) ul_harq_tx,"
                " SUM(ul_harq_retransmission_delta) ul_harq_retx,"
                " SUM(dl_harq_initial_tx_delta) dl_harq_tx,"
                " SUM(dl_harq_retransmission_delta) dl_harq_retx,"
                " SUM(ul_harq_errors) ul_harq_errors, SUM(dl_harq_errors) dl_harq_errors,"
                " COUNT(*) snapshot_count"
                " FROM oai_snapshots"
                f" WHERE run_id=? AND fetched_utc_ms BETWEEN ? AND ?",
                (self.run_id, window["started_utc_ms"], window["ended_utc_ms"]))
            if row:
                gnb_summary = {k: (round(v, 3) if isinstance(v, float) else v)
                               for k, v in row.items() if v is not None}
                for prefix in ("ul", "dl"):
                    tx = gnb_summary.get(f"{prefix}_harq_tx")
                    retx = gnb_summary.get(f"{prefix}_harq_retx")
                    gnb_summary[f"{prefix}_harq_retx_rate"] = (
                        round(float(retx) / (float(tx) + float(retx)), 6)
                        if tx is not None and retx is not None and float(tx) + float(retx) > 0
                        else None)
        except Exception:
            pass
        data_complete = bool(channel.get("processing_status") == "OK" and
                             gnb_summary.get("snapshot_count", 0) > 0)
        analytics = {
            "identity": {"experiment_id": self.experiment_id, "run_id": self.run_id,
                         "sample_id": index, "angle_deg": angle_deg},
            "window": {"start_utc_ms": window["started_utc_ms"],
                       "end_utc_ms": window["ended_utc_ms"],
                       "aggregation": "measurement window only"},
            "channel": channel,
            "radio": {"rssp_dbfs": self.last_rssp_db,
                      "target_rssp_dbfs": self.cfg.target_rssp_db,
                      "pusch_target_snr_x10": self.pusch_x10},
            "link": {**gnb_summary,
                     "bler_contract": "OAI snapshot estimator; arithmetic mean over measurement window",
                     "harq_contract": "sum of OAI per-snapshot deltas over measurement window"},
            "quality": {"data_complete": data_complete,
                        "alignment_status": "MASTER_UTC_WINDOW",
                        "processing_status": channel.get("processing_status"),
                        "processing_error": channel.get("processing_error")},
        }
        # persist raw per-sample payload (CIR + servo log + metadata)
        out_dir = self.s.raw_dir / "rc" / (self.run_id or self.experiment_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"sample_{index:03d}.json"
        payload = {
            "sample_index": index, "stirrer_angle_deg": angle_deg,
            "window": window, "pusch_target_snr_x10": self.pusch_x10,
            "target_rssp_db": self.cfg.target_rssp_db,
            "noise_floor_db": self.noise_floor_db,
            "servo_log": servo_log, "gnb_summary": gnb_summary,
            "analytics": analytics,
            "cir": raw,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.db.record_file(path)
        rssp = self.last_rssp_db
        self.db.execute(
            """INSERT INTO rc_samples(
               experiment_id, run_id, sample_index, environment, stirrer_angle_deg,
               stirrer_step_deg, pusch_target_snr_x10, rssp_db, target_rssp_db,
               rssp_error_db, noise_floor_db, noise_margin_db,
               tap_count, tap_count_filtered, rms_delay_ns, rms_delay_ns_filtered,
               raw_delay_bin_count, candidate_peak_count, effective_peak_count, resolved_path_count,
               mean_delay_ns, k_factor_db, peak_db, peak_db_filtered,
               started_utc_ms, ended_utc_ms, servo_iters, servo_log,
               gnb_summary, raw_json_path, analytics_json,
               processing_status, processing_error, detection_threshold_db,
               noise_std_db, noise_frame_count, processing_algorithm, processing_version,
               noise_method, data_complete, alignment_status, created_utc)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.experiment_id, self.run_id, index, "RC", angle_deg,
             self.cfg.step_deg, self.pusch_x10, rssp, self.cfg.target_rssp_db,
             (round(self.cfg.target_rssp_db - rssp, 2) if rssp is not None else None),
             self.noise_floor_db, self.cfg.noise_margin_db,
             channel.get("raw_path_count"), channel.get("effective_path_count"),
             m.get("rmsDelayNs"), channel.get("rms_delay_ns_filtered"),
             channel.get("raw_delay_bin_count"), channel.get("candidate_peak_count"),
             channel.get("effective_peak_count"), channel.get("resolved_path_count"),
             channel.get("mean_delay_ns_filtered"), channel.get("k_factor_db_filtered"),
             m.get("peakDb"), channel.get("peak_db_filtered"),
             window["started_utc_ms"], window["ended_utc_ms"], len(servo_log),
             json.dumps(servo_log, ensure_ascii=False),
             json.dumps(gnb_summary, ensure_ascii=False), str(path),
             json.dumps(analytics, ensure_ascii=False),
             channel.get("processing_status"), channel.get("processing_error"),
             channel.get("detection_threshold_db"), self.noise_std_db, self.noise_frame_count,
             PROCESSING_ALGORITHM, PROCESSING_VERSION, NOISE_METHOD,
             1 if data_complete else 0, "MASTER_UTC_WINDOW",
             datetime.now(timezone.utc).isoformat()))

    # ---- main ---------------------------------------------------------------- #
    def _wait_for_sync(self, loop) -> bool:
        self.state = "waiting_sync"
        self._say("sync", "waiting for post-restart UE synchronization")
        while not loop.sync_confirmed and not self._stop.wait(0.5):
            pass
        if self._stop.is_set():
            self.state = "stopped"
            return False
        if not hasattr(loop, "seq") or not hasattr(self, "cfg"):
            self._say("sync", "UE synchronized; Run time anchor recorded")
            return True
        first_seq = loop.seq
        deadline = time.monotonic() + max(30.0, self.cfg.sync_exchanges * loop.interval_s * 3)
        while not self._stop.is_set() and time.monotonic() < deadline:
            ack = loop.last_ack or {}
            if (loop.seq - first_seq + 1 >= self.cfg.sync_exchanges and
                    float(ack.get("rtt_ms") or float("inf")) <= self.cfg.sync_max_rtt_ms):
                self._say("sync", f"UE synchronized; {self.cfg.sync_exchanges} exchanges confirmed")
                return True
            self._stop.wait(0.25)
        if self._stop.is_set():
            self.state = "stopped"
            return False
        raise StirrerError(
            f"clock synchronization did not meet RTT ≤ {self.cfg.sync_max_rtt_ms:.0f} ms")

    def run(self) -> None:
        try:
            self._run_inner()
        except Exception as e:  # noqa: BLE001 — surface any failure to the UI
            self.error = str(e)
            self.state = "error"
            self._say("campaign", f"FAILED: {e}")

    def _run_inner(self) -> None:
        exp = self.db.query_one("SELECT * FROM experiments WHERE experiment_id=?",
                                (self.experiment_id,))
        if not exp:
            raise ValueError("experiment not found")
        run = self.db.query_one(
            "SELECT run_id FROM runs WHERE experiment_id=? "
            "AND state IN ('PREPARING','ARMED','RUNNING') ORDER BY rowid DESC LIMIT 1",
            (self.experiment_id,))
        if not run:
            raise ValueError("no active run — start the experiment first, then launch the RC campaign")
        self.run_id = run["run_id"]
        loop = self.flow.downlinks.get(self.experiment_id)
        plan = loop.plan if loop else None
        if not plan:
            raise ValueError("downlink loop plan not found — start the experiment first")

        # RC motion and measurement must never race the post-restart UE
        # synchronization. DownlinkLoop records the successful sync-confirm as
        # the Run's time anchor; only then may the chamber campaign proceed.
        if not self._wait_for_sync(loop):
            return

        try:
            controls = self.oai.gnb_controls()
            target = getattr(controls, "puschTarget", None)
            self.initial_pusch_mode = getattr(target, "mode", None)
            initial_x10 = getattr(target, "targetSnrX10", None)
            self.initial_pusch_x10 = int(initial_x10) if initial_x10 is not None else None
            self.pusch_x10 = self.initial_pusch_x10
            actual = self.oai.telemetry_config_raw()
            self.current_tx_gain_db = float(actual.get("txGainDb")) if actual.get("txGainDb") is not None else None
            self.current_rx_gain_db = float(actual.get("rxGainDb")) if actual.get("rxGainDb") is not None else None
        except Exception as e:
            raise StirrerError(f"cannot read initial PUSCH target: {e}") from e

        stirrer = StirrerAgent(self.s, simulate=self.cfg.simulate_stirrer,
                               speed_deg_s=self.cfg.stirrer_speed_deg_s)
        opened = stirrer.open()
        if not opened.get("ok"):
            stirrer.close()
            raise StirrerError(f"stirrer not available: {opened.get('error')} "
                               f"(enable 'simulate' for dry runs)")

        try:
            angle = stirrer.position_deg() or 0.0
            noise = None
            for i in range(self.cfg.n_steps):
                if self._stop.is_set():
                    break
                self.current_sample_index = i + 1
                self.current_angle_deg = angle
                self.state = "power_calibration"
                servo_log = self.servo_pusch(stirrer)

                if noise is None:
                    self.state = "noise_capture"
                    noise = self.calibrate_noise(stirrer)

                self.state = "loaded_capture"
                window = self.trigger_phone_window(plan)
                if window is None:
                    self._say("campaign", f"sample {i + 1} skipped (phone window failed)")
                else:
                    self.state = "finalizing"
                    self.finalize_sample(i + 1, angle, window, servo_log, noise)
                    self.samples_done += 1
                    self._say("campaign", f"sample {i + 1} stored @ {angle:.1f}°")

                if i + 1 < self.cfg.n_steps and not self._stop.is_set():
                    self.state = "stirrer_rotation"
                    self._say("stirrer", f"rotate +{self.cfg.step_deg}° for sample {i + 2}")
                    mv = stirrer.move_rel_and_wait(self.cfg.step_deg, timeout_s=180.0)
                    if not mv.get("ok"):
                        raise StirrerError(f"stirrer move failed: {mv.get('error')}")
                    angle = mv.get("deg", angle)
                    time.sleep(self.cfg.settle_s)
        finally:
            try:
                stirrer.close()
            except Exception:
                pass
            self.restore_pusch_target()
        self.state = ("stopped" if self._stop.is_set() else
                      ("completed" if self.samples_done == self.cfg.n_steps else "incomplete"))
        self._say("campaign", f"campaign {self.state} — {self.samples_done} samples")
        if self.state == "completed" and self.run_id:
            stopped = self.flow.stop_experiment(
                self.experiment_id, self.serial, self.pc_port, requested_run_id=self.run_id)
            if not stopped.get("discarded") and self.db.get_run(self.run_id):
                self.db.transition(self.run_id, "COMPLETE", "RC campaign completed automatically")

    def stop(self) -> None:
        self._stop.set()


class RcRunner:
    """Singleton registry of active RC campaigns (one per experiment)."""

    def __init__(self, settings: Settings, db: Database, oai: OaiClient):
        self.s = settings
        self.db = db
        self.oai = oai
        self.campaigns: dict[str, RcCampaign] = {}

    def start(self, flow, experiment_id: str, serial: str, pc_port: int,
              cfg: dict) -> dict:
        old = self.campaigns.get(experiment_id)
        if old and old.is_alive():
            raise ValueError("an RC campaign is already running for this experiment")
        camp = RcCampaign(flow, self.s, self.db, self.oai, experiment_id,
                          serial, pc_port, RcConfig(cfg))
        camp.start()
        self.campaigns[experiment_id] = camp
        return self.status(experiment_id)

    def stop(self, experiment_id: str) -> dict:
        camp = self.campaigns.get(experiment_id)
        if not camp:
            raise ValueError("no RC campaign for this experiment")
        camp.stop()
        return {"ok": True, "state": "stopping"}

    def status(self, experiment_id: str) -> dict:
        camp = self.campaigns.get(experiment_id)
        if not camp:
            return {"running": False, "state": "idle"}
        return {
            "running": camp.is_alive(),
            "state": camp.state,
            "experiment_id": camp.experiment_id,
            "run_id": camp.run_id,
            "samples_done": camp.samples_done,
            "n_steps": camp.cfg.n_steps,
            "current_angle_deg": camp.current_angle_deg,
            "pusch_x10": camp.pusch_x10,
            "last_rssp_db": camp.last_rssp_db,
            "target_rssp_db": camp.cfg.target_rssp_db,
            "noise_floor_db": camp.noise_floor_db,
            "current_sample_index": camp.current_sample_index,
            "current_tx_gain_db": camp.current_tx_gain_db,
            "calibration_records": camp.calibration_records,
            "error": camp.error,
            "config": camp.cfg.as_dict(),
            "log": camp.log[-40:],
        }

    def samples(self, experiment_id: Optional[str] = None,
                run_id: Optional[str] = None) -> list[dict]:
        if run_id:
            rows = self.db.query(
                "SELECT * FROM rc_samples WHERE run_id=? ORDER BY sample_index", (run_id,))
        elif experiment_id:
            rows = self.db.query(
                "SELECT * FROM rc_samples WHERE experiment_id=? ORDER BY run_id,sample_index",
                (experiment_id,))
        else:
            return []
        for row in rows:
            for key in ("servo_log", "gnb_summary", "analytics_json"):
                if row.get(key):
                    try:
                        row[key.removesuffix("_json")] = json.loads(row[key])
                    except (TypeError, json.JSONDecodeError):
                        pass
        return rows


_runners: dict[str, RcRunner] = {}


def get_runner(settings: Settings, db: Database, oai: OaiClient) -> RcRunner:
    key = str(settings.db_path)
    if key not in _runners:
        _runners[key] = RcRunner(settings, db, oai)
    return _runners[key]
