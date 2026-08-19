"""OAI-only receive-power calibration diagnostic used by Advanced tools."""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from .oai_client import OaiClient


def _now_ms() -> int:
    return int(time.time() * 1000)


def _fresh_ues(oai: OaiClient) -> list[dict]:
    raw = oai.telemetry_ues_raw()
    if (raw.get("collection") or {}).get("stale"):
        return []
    return [ue for ue in raw.get("ues") or []
            if ue.get("ageSeconds") is not None and float(ue["ageSeconds"]) <= 5.0]


def read_rssp_db(oai: OaiClient) -> Optional[float]:
    """Return the strongest fresh authoritative gNB PUSCH RSSI in dBFS."""
    values = []
    for ue in _fresh_ues(oai):
        uplink = ue.get("uplink") or {}
        value = uplink.get("puschRssi")
        if value is not None and uplink.get("puschRssiUnit") == "dBFS":
            values.append(float(value))
    return max(values) if values else None


def read_ue_rsrp_dbm(oai: OaiClient) -> Optional[float]:
    """Return the strongest fresh UE-reported downlink RSRP in dBm."""
    values = [float(ue["rsrpDbm"]) for ue in _fresh_ues(oai)
              if ue.get("rsrpDbm") is not None]
    return max(values) if values else None


def average_measurement(oai: OaiClient,
                        reader: Callable[[OaiClient], Optional[float]],
                        listening_period_s: float,
                        settle_time_s: float,
                        stop_event: threading.Event) -> Optional[tuple[float, int]]:
    """Wait for signal, then average observations over the settle window."""
    listening_deadline = time.monotonic() + listening_period_s
    first: Optional[float] = None
    while not stop_event.is_set() and time.monotonic() < listening_deadline:
        first = reader(oai)
        if first is not None:
            break
        stop_event.wait(min(1.0, max(0.0, listening_deadline - time.monotonic())))
    if first is None:
        return None

    values = [first]
    settle_deadline = time.monotonic() + settle_time_s
    while not stop_event.is_set() and time.monotonic() < settle_deadline:
        stop_event.wait(min(1.0, max(0.0, settle_deadline - time.monotonic())))
        if stop_event.is_set():
            break
        value = reader(oai)
        if value is not None:
            values.append(value)
    if stop_event.is_set():
        return None
    return sum(values) / len(values), len(values)


def proportional_target_x10(current_x10: int, error_db: float,
                            alpha: float) -> tuple[int, float, float]:
    """Calculate at 0.01 dB, then quantize to the OAI x10 interface."""
    requested_delta_db = round(alpha * error_db, 2)
    delta_x10 = round(requested_delta_db * 10)
    if delta_x10 == 0 and error_db != 0:
        delta_x10 = 1 if error_db > 0 else -1
    updated = max(0, min(400, current_x10 + delta_x10))
    return updated, requested_delta_db, round((updated - current_x10) / 10.0, 2)


class RsspCalibration(threading.Thread):
    """Calibrate one OAI power observable without an Experiment or stirrer.

    UE RSRP is controlled by gNB TX Gain. gNB PUSCH RSSI can be controlled
    either by RX Gain (receiver calibration) or PUSCH Target SNR (UE TPC).
    Gain changes require a verified gNB restart; Target SNR is hot-applied.
    """

    def __init__(self, oai: OaiClient, cfg: dict):
        super().__init__(daemon=True, name="rssp-calibration")
        self.oai = oai
        self.measurement = str(cfg.get("measurement") or "pusch_rssi")
        default_actuator = "tx_gain" if self.measurement == "ue_rsrp" else "target_snr"
        self.actuator = str(cfg.get("actuator") or default_actuator)
        valid = {"ue_rsrp": {"tx_gain"}, "pusch_rssi": {"rx_gain", "target_snr"}}
        if self.measurement not in valid:
            raise ValueError("measurement must be ue_rsrp or pusch_rssi")
        if self.actuator not in valid[self.measurement]:
            raise ValueError(f"{self.actuator} cannot control {self.measurement}")

        default_target = -80.0 if self.measurement == "ue_rsrp" else -60.0
        self.target_db = float(cfg.get("target_db", cfg.get("target_rssp_db", default_target)))
        self.tolerance_db = float(cfg.get("tolerance_db", cfg.get("rssp_tol_db", 1.5)))
        self.gain_alpha = float(cfg.get("gain_alpha", 0.5))
        self.max_servo_iters = int(cfg.get("max_servo_iters", 6))
        self.listening_period_s = float(cfg.get(
            "listening_period_s", cfg.get("observation_timeout_s", 90.0)))
        self.settle_time_s = float(cfg.get(
            "settle_time_s", cfg.get("servo_settle_s", 5.0)))
        if not -140.0 <= self.target_db <= 0.0:
            raise ValueError("target_db must be between -140 and 0")
        if not 0.1 <= self.tolerance_db <= 20.0:
            raise ValueError("tolerance_db must be between 0.1 and 20 dB")
        if not 0.01 <= self.gain_alpha <= 1.0:
            raise ValueError("gain_alpha must be between 0.01 and 1.0")
        if not 1 <= self.max_servo_iters <= 50:
            raise ValueError("max_servo_iters must be between 1 and 50")
        if not 1.0 <= self.listening_period_s <= 300.0:
            raise ValueError("listening_period_s must be between 1 and 300 seconds")
        if not 0.0 <= self.settle_time_s <= 60.0:
            raise ValueError("settle_time_s must be between 0 and 60 seconds")

        self.unit = "dBm" if self.measurement == "ue_rsrp" else "dBFS"
        self.metric_label = "UE RSRP" if self.measurement == "ue_rsrp" else "PUSCH RSSI"
        self._reader: Callable[[OaiClient], Optional[float]] = (
            read_ue_rsrp_dbm if self.measurement == "ue_rsrp" else read_rssp_db)
        self._stop_event = threading.Event()
        self.state = "preparing"
        self.started_ms = _now_ms()
        self.ended_ms: Optional[int] = None
        self.initial_pusch_mode: Optional[str] = None
        self.initial_pusch_x10: Optional[int] = None
        self.pusch_x10: Optional[int] = None
        self.initial_tx_gain_db: Optional[float] = None
        self.initial_rx_gain_db: Optional[float] = None
        self.tx_gain_db: Optional[float] = None
        self.rx_gain_db: Optional[float] = None
        self.last_value_db: Optional[float] = None
        self.records: list[dict] = []
        self.log: list[dict] = []
        self.error: Optional[str] = None
        self.restore_error: Optional[str] = None
        self._gain_changed = False
        self._pusch_changed = False

    def _say(self, stage: str, message: str) -> None:
        self.log.append({"ms": _now_ms(), "stage": stage, "msg": message})
        self.log = self.log[-100:]

    def stop(self) -> None:
        self.state = "stopping"
        self._stop_event.set()

    def _wait_measurement(self) -> Optional[tuple[float, int]]:
        return average_measurement(
            self.oai, self._reader, self.listening_period_s,
            self.settle_time_s, self._stop_event)

    def _restore(self) -> None:
        if self._gain_changed:
            if self.initial_tx_gain_db is None or self.initial_rx_gain_db is None:
                raise RuntimeError("initial TX/RX Gain is unavailable")
            self.state = "restoring"
            self.oai.apply_condition(
                {"txGainDb": self.initial_tx_gain_db, "rxGainDb": self.initial_rx_gain_db},
                force_restart=True, request_prefix="diagnostic-restore-gain",
            )
            self.tx_gain_db = self.initial_tx_gain_db
            self.rx_gain_db = self.initial_rx_gain_db
            self._say("restore", "Initial TX/RX Gain restored with a verified gNB restart.")
        if self._pusch_changed and self.initial_pusch_mode in {"auto", "manual"}:
            value = self.initial_pusch_x10 if self.initial_pusch_mode == "manual" else None
            result = self.oai.gnb_pusch_target_snr(
                self.initial_pusch_mode, value, restart=False,
                request_id=self.oai.new_request_id("diagnostic", "restore-pusch"),
            )
            changed = bool((result.get("target") or {}).get("effectiveChanged"))
            if changed and not result.get("runtimeApplied"):
                raise RuntimeError("initial PUSCH target was not restored in the running gNB")
            self.pusch_x10 = self.initial_pusch_x10
            self._say("restore", "Initial PUSCH target restored without gNB restart.")

    def _apply(self, error_db: float, iteration: int, point: dict) -> None:
        if self.actuator == "target_snr":
            old_x10 = self.pusch_x10 or 0
            new_x10, requested_delta_db, delta_db = proportional_target_x10(
                old_x10, error_db, self.gain_alpha)
            self._say("calibration", f"{self.metric_label} {point['observed_db']:.1f} {self.unit} "
                      f"(error {error_db:+.1f}) → Target SNR "
                      f"{old_x10 / 10:.1f}→{new_x10 / 10:.1f} dB "
                      f"(α={self.gain_alpha:g}, requested Δ={requested_delta_db:+.2f} dB, "
                      f"OAI Δ={delta_db:+.2f} dB).")
            result = self.oai.gnb_pusch_target_snr(
                "manual", new_x10, restart=False,
                request_id=self.oai.new_request_id("diagnostic", f"power-servo-{iteration}"),
            )
            if not result.get("runtimeApplied"):
                raise RuntimeError("PUSCH target update was not applied to the running gNB")
            self.pusch_x10 = new_x10
            self._pusch_changed = True
            point.update({"applied_actuator": "target_snr", "applied_value": new_x10 / 10.0,
                          "applied_unit": "dB", "applied_x10": new_x10,
                          "gain_alpha": self.gain_alpha,
                          "requested_target_delta_db": requested_delta_db,
                          "target_delta_db": delta_db})
            return

        if self.tx_gain_db is None or self.rx_gain_db is None:
            raise RuntimeError("current TX/RX Gain is unavailable from OAI status")
        current = self.tx_gain_db if self.actuator == "tx_gain" else self.rx_gain_db
        requested_delta_db = self.gain_alpha * error_db
        new_gain = round(max(0.0, min(100.0, current + requested_delta_db)), 2)
        requested = {"txGainDb": self.tx_gain_db, "rxGainDb": self.rx_gain_db}
        requested["txGainDb" if self.actuator == "tx_gain" else "rxGainDb"] = new_gain
        self.state = "restarting"
        self._say("calibration", f"{self.metric_label} {point['observed_db']:.1f} {self.unit} "
                  f"(error {error_db:+.1f}) → {self.actuator.replace('_', ' ').upper()} "
                  f"{current:.1f}→{new_gain:.1f} dB; restarting gNB.")
        self.oai.apply_condition(
            requested, force_restart=True,
            request_prefix=f"diagnostic-{self.actuator}-{iteration}",
        )
        self.tx_gain_db = float(requested["txGainDb"])
        self.rx_gain_db = float(requested["rxGainDb"])
        self._gain_changed = True
        point.update({"applied_actuator": self.actuator, "applied_value": new_gain,
                      "applied_unit": "dB", "gain_alpha": self.gain_alpha,
                      "gain_delta_db": new_gain - current})
        self.state = "calibrating"

    def run(self) -> None:
        terminal_state = "error"
        try:
            self.state = "connecting"
            self.oai.health()
            status = self.oai.status_raw()
            if not (status.get("gnb") or {}).get("running"):
                raise RuntimeError("gNB is not running")
            radio = status.get("radio") or {}
            self.initial_tx_gain_db = (float(radio["txGainDb"])
                                       if radio.get("txGainDb") is not None else None)
            self.initial_rx_gain_db = (float(radio["rxGainDb"])
                                       if radio.get("rxGainDb") is not None else None)
            self.tx_gain_db = self.initial_tx_gain_db
            self.rx_gain_db = self.initial_rx_gain_db

            controls = self.oai.gnb_controls()
            target = getattr(controls, "puschTarget", None)
            mode = getattr(target, "mode", None)
            self.initial_pusch_mode = mode if mode in {"auto", "manual"} else "auto"
            current = getattr(target, "targetSnrX10", None)
            if current is None:
                current = getattr(target, "autoTargetSnrX10", None)
            self.initial_pusch_x10 = int(current) if current is not None else 100
            self.pusch_x10 = self.initial_pusch_x10
            self.state = "calibrating"
            self._say("calibration", f"OAI connected; {self.metric_label} calibration started.")

            for iteration in range(1, self.max_servo_iters + 1):
                if self._stop_event.is_set():
                    terminal_state = "stopped"
                    break
                observation = self._wait_measurement()
                value = observation[0] if observation else None
                self.last_value_db = value
                if value is None:
                    if self._stop_event.is_set():
                        terminal_state = "stopped"
                        break
                    raise RuntimeError(f"No fresh OAI {self.metric_label}; ensure a UE is attached "
                                       "and producing the required radio telemetry.")
                error_db = self.target_db - value
                point = {
                    "ms": _now_ms(), "iter": iteration,
                    "measurement": self.measurement, "metric_label": self.metric_label,
                    "unit": self.unit, "observed_db": round(value, 2),
                    "settle_sample_count": observation[1],
                    "rssp_db": round(value, 2), "target_db": self.target_db,
                    "target_rssp_db": self.target_db, "err_db": round(error_db, 2),
                    "tx_gain_db": self.tx_gain_db, "rx_gain_db": self.rx_gain_db,
                    "pusch_x10": self.pusch_x10,
                    "target_snr_db": (self.pusch_x10 or 0) / 10.0,
                }
                self.records.append(point)
                self.records = self.records[-300:]
                if abs(error_db) <= self.tolerance_db:
                    terminal_state = "converged"
                    self._say("calibration", f"{self.metric_label} {value:.1f} {self.unit} is "
                              f"within ±{self.tolerance_db:.1f} dB of target.")
                    break
                self._apply(error_db, iteration, point)
            else:
                terminal_state = "exhausted"
                self._say("calibration", "Maximum iterations reached before convergence.")
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            terminal_state = "error"
            self._say("error", self.error)
        finally:
            try:
                self._restore()
            except Exception as exc:  # noqa: BLE001
                self.restore_error = str(exc)
                self.error = self.error or self.restore_error
                terminal_state = "error"
                self._say("error", f"Restore failed: {exc}")
            self.state = terminal_state
            self.ended_ms = _now_ms()

    def status(self) -> dict:
        return {
            "running": self.is_alive(), "state": self.state,
            "started_ms": self.started_ms, "ended_ms": self.ended_ms,
            "measurement": self.measurement, "metric_label": self.metric_label,
            "actuator": self.actuator, "unit": self.unit,
            "target_db": self.target_db, "target_rssp_db": self.target_db,
            "tolerance_db": self.tolerance_db, "rssp_tol_db": self.tolerance_db,
            "gain_alpha": self.gain_alpha,
            "max_servo_iters": self.max_servo_iters,
            "listening_period_s": self.listening_period_s,
            "settle_time_s": self.settle_time_s,
            "initial_pusch_mode": self.initial_pusch_mode,
            "initial_pusch_x10": self.initial_pusch_x10,
            "pusch_x10": self.pusch_x10,
            "initial_tx_gain_db": self.initial_tx_gain_db,
            "initial_rx_gain_db": self.initial_rx_gain_db,
            "tx_gain_db": self.tx_gain_db, "rx_gain_db": self.rx_gain_db,
            "last_value_db": self.last_value_db, "last_rssp_db": self.last_value_db,
            "records": list(self.records), "log": list(self.log),
            "error": self.error, "restore_error": self.restore_error,
        }


class RsspCalibrationRunner:
    def __init__(self, oai: OaiClient):
        self.oai = oai
        self.job: Optional[RsspCalibration] = None

    def start(self, cfg: dict) -> dict:
        if self.job and self.job.is_alive():
            raise ValueError("RSSP calibration is already running")
        self.job = RsspCalibration(self.oai, cfg)
        self.job.start()
        return self.status()

    def stop(self) -> dict:
        if not self.job or not self.job.is_alive():
            raise ValueError("no RSSP calibration is running")
        self.job.stop()
        return self.status()

    def status(self) -> dict:
        if not self.job:
            return {"running": False, "state": "idle", "records": [], "log": []}
        return self.job.status()


_runners: dict[int, RsspCalibrationRunner] = {}


def get_runner(oai: OaiClient) -> RsspCalibrationRunner:
    key = id(oai)
    if key not in _runners:
        _runners[key] = RsspCalibrationRunner(oai)
    return _runners[key]
