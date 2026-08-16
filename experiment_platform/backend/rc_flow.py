"""Reverberation-chamber (RC) acquisition flow.

Unlike the anechoic chamber (AC — record continuously, clip later), the RC
flow is SAMPLED: after every stirrer step the platform

  1. waits for the stirrer to stand still,
  2. fine-tunes ``puschTargetSnrX10`` (multiple small steps, never a gNB
     restart) so the gNB-received power (CIR peak tap) returns to the
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

from .config import Settings
from .db import Database
from .oai_client import OaiClient
from .stirrer import StirrerAgent, StirrerError


def _now_ms() -> int:
    return int(time.time() * 1000)


def _tap_powers_db(cir: dict) -> list[float]:
    """|h(tau)|^2 per tap in dB from a channel-daemon CIR payload."""
    re = cir.get("cirRe") or []
    im = cir.get("cirIm") or []
    n = min(len(re), len(im))
    return [10.0 * math.log10(max(re[i] * re[i] + im[i] * im[i], 1e-30))
            for i in range(n)]


class RcConfig:
    """Per-campaign knobs (validated defaults — the UI can override each)."""

    DEFAULTS = {
        "step_deg": 5.0,          # stirrer increment per sample
        "n_steps": 12,            # number of samples (stirrer realizations)
        "dwell_s": 20.0,          # loaded window = the actual measurement
        "settle_s": 8.0,          # mechanical settle after the stirrer stops
        "target_rssp_db": -60.0,  # gNB-received peak-tap power to hold
        "rssp_tol_db": 1.5,       # servo deadband
        "pusch_step_x10": 10,     # ±1.0 dB target-SNR per servo iteration
        "max_servo_iters": 6,
        "servo_settle_s": 5.0,    # TPC propagation wait between iterations
        "noise_frames": 20,       # 底噪标定帧数
        "noise_margin_db": 6.0,   # taps below floor+margin are noise
        "stirrer_speed_deg_s": 20.0,
        "simulate_stirrer": False,
    }

    def __init__(self, cfg: Optional[dict] = None):
        c = dict(self.DEFAULTS)
        c.update({k: v for k, v in (cfg or {}).items() if v is not None})
        self.step_deg = float(c["step_deg"])
        self.n_steps = int(c["n_steps"])
        self.dwell_s = float(c["dwell_s"])
        self.settle_s = float(c["settle_s"])
        self.target_rssp_db = float(c["target_rssp_db"])
        self.rssp_tol_db = float(c["rssp_tol_db"])
        self.pusch_step_x10 = int(c["pusch_step_x10"])
        self.max_servo_iters = int(c["max_servo_iters"])
        self.servo_settle_s = float(c["servo_settle_s"])
        self.noise_frames = int(c["noise_frames"])
        self.noise_margin_db = float(c["noise_margin_db"])
        self.stirrer_speed_deg_s = float(c["stirrer_speed_deg_s"])
        self.simulate_stirrer = bool(c["simulate_stirrer"])

    def as_dict(self) -> dict:
        return {
            "step_deg": self.step_deg, "n_steps": self.n_steps,
            "dwell_s": self.dwell_s, "settle_s": self.settle_s,
            "target_rssp_db": self.target_rssp_db, "rssp_tol_db": self.rssp_tol_db,
            "pusch_step_x10": self.pusch_step_x10, "max_servo_iters": self.max_servo_iters,
            "servo_settle_s": self.servo_settle_s, "noise_frames": self.noise_frames,
            "noise_margin_db": self.noise_margin_db,
            "stirrer_speed_deg_s": self.stirrer_speed_deg_s,
            "simulate_stirrer": self.simulate_stirrer,
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
        self.last_rssp_db: Optional[float] = None
        self.noise_floor_db: Optional[float] = None
        self.log: list[dict] = []
        self.error: Optional[str] = None
        self.started_ms = _now_ms()
        self.run_id: Optional[str] = None

    # ---- helpers ------------------------------------------------------------ #
    def _say(self, stage: str, msg: str) -> None:
        self.log.append({"ms": _now_ms(), "stage": stage, "msg": msg})
        self.log = self.log[-200:]

    def _cir(self, timeout_s: float = 10.0) -> Optional[dict]:
        """Fresh CIR from the channel daemon (ok + non-empty graph data)."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and not self._stop.is_set():
            try:
                raw = self.oai.channel_cir()
                if raw.get("ok") and (raw.get("cirRe") or []):
                    return raw
            except Exception:
                pass
            time.sleep(1.0)
        return None

    def _rssp_db(self) -> Optional[float]:
        """Receiver RSSP proxy = strongest CIR tap power (dB)."""
        raw = self._cir(timeout_s=6.0)
        if not raw:
            return None
        taps = _tap_powers_db(raw)
        return max(taps) if taps else None

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
        self.noise_floor_db = floor_db
        out_dir = self.s.raw_dir / "rc" / (self.run_id or self.experiment_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "noise_profile.json"
        path.write_text(json.dumps({
            "captured_utc_ms": _now_ms(), "frames": len(stacks),
            "n_taps": n, "per_tap_median_db": [round(v, 2) for v in per_tap_median],
            "noise_floor_db": round(floor_db, 2),
            "margin_db": self.cfg.noise_margin_db,
        }, ensure_ascii=False), encoding="utf-8")
        self.db.record_file(path)
        self._say("noise", f"noise floor {floor_db:.1f} dB from {len(stacks)} frames")
        return {"noise_floor_db": floor_db, "n_taps": n, "path": str(path)}

    def servo_pusch(self, stirrer: StirrerAgent) -> list[dict]:
        """多次微调 puschTargetSnrX10 until receiver RSSP returns to target.

        Direction: raising the gNB PUSCH target SNR makes TPC command the UE
        UP, raising received power — so error>0 (too weak) → raise target.
        """
        log: list[dict] = []
        # current setting from the OAI controls (authoritative)
        try:
            ctl = self.oai.gnb_controls()
            cur = getattr(ctl, "pusch_target_snr_x10", None)
            if cur is None and isinstance(getattr(ctl, "model_extra", None), dict):
                cur = ctl.model_extra.get("puschTargetSnrX10")  # type: ignore[attr-defined]
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
                self._say("servo", "no CIR available — skipping servo")
                return log
            err = self.cfg.target_rssp_db - rssp
            log.append({"iter": it, "rssp_db": round(rssp, 2),
                        "err_db": round(err, 2), "pusch_x10": self.pusch_x10})
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
                r = self.oai.gnb_pusch_target_snr("manual", new_x10, restart=False)
                self.pusch_x10 = new_x10
                log[-1]["applied_x10"] = new_x10
                log[-1]["resp"] = str(r)[:200]
            except Exception as e:
                log[-1]["apply_error"] = str(e)[:200]
                self._say("servo", f"pusch apply failed: {e}")
                return log
            time.sleep(self.cfg.servo_settle_s)
        self._say("servo", "servo iterations exhausted — continuing with last setting")
        return log

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
        """Grab final CIR, filter against the noise floor, aggregate gNB data."""
        raw = self._cir(timeout_s=8.0)
        taps = _tap_powers_db(raw) if raw else []
        floor = self.noise_floor_db if self.noise_floor_db is not None \
            else float((raw or {}).get("metrics", {}).get("noiseDb", -100.0))
        margin = self.cfg.noise_margin_db
        thresh = floor + margin
        kept = [(i, p) for i, p in enumerate(taps) if p > thresh]
        dt_ns = float((raw or {}).get("dtNs") or 8.138)
        total = sum(10 ** (p / 10.0) for _, p in kept) or 1e-30
        mean_delay = sum(i * dt_ns * 10 ** (p / 10.0) for i, p in kept) / total
        rms_delay = math.sqrt(max(0.0, sum(
            (i * dt_ns - mean_delay) ** 2 * 10 ** (p / 10.0) for i, p in kept) / total))
        m = (raw or {}).get("metrics") or {}
        peak_f = max((p for _, p in kept), default=None)
        # gNB-side aggregates inside the window (collectors already wrote them)
        gnb_summary: dict[str, Any] = {}
        try:
            row = self.db.query_one(
                f"SELECT AVG(ul_goodput_mbps) ul, AVG(dl_goodput_mbps) dl, AVG(pusch_snr_db) snr,"
                f" AVG(ul_mcs) mcs, AVG(ul_bler) bler, COUNT(*) n FROM oai_snapshots"
                f" WHERE run_id=? AND fetched_utc_ms BETWEEN ? AND ?",
                (self.run_id, window["started_utc_ms"], window["ended_utc_ms"]))
            if row:
                gnb_summary = {k: (round(v, 3) if isinstance(v, float) else v)
                               for k, v in row.items() if v is not None}
        except Exception:
            pass
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
            "filtered": {"tap_count": len(kept), "threshold_db": round(thresh, 2),
                         "rms_delay_ns": round(rms_delay, 1),
                         "mean_delay_ns": round(mean_delay, 1),
                         "peak_db": peak_f},
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
               mean_delay_ns, k_factor_db, peak_db, peak_db_filtered,
               started_utc_ms, ended_utc_ms, servo_iters, servo_log,
               gnb_summary, raw_json_path, created_utc)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.experiment_id, self.run_id, index, "RC", angle_deg,
             self.cfg.step_deg, self.pusch_x10, rssp, self.cfg.target_rssp_db,
             (round(self.cfg.target_rssp_db - rssp, 2) if rssp is not None else None),
             self.noise_floor_db, self.cfg.noise_margin_db,
             m.get("tapCount"), len(kept), m.get("rmsDelayNs"), round(rms_delay, 1),
             round(mean_delay, 1), m.get("kFactorDb"), m.get("peakDb"), peak_f,
             window["started_utc_ms"], window["ended_utc_ms"], len(servo_log),
             json.dumps(servo_log, ensure_ascii=False),
             json.dumps(gnb_summary, ensure_ascii=False), str(path),
             datetime.now(timezone.utc).isoformat()))

    # ---- main ---------------------------------------------------------------- #
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

        stirrer = StirrerAgent(self.s, simulate=self.cfg.simulate_stirrer,
                               speed_deg_s=self.cfg.stirrer_speed_deg_s)
        opened = stirrer.open()
        if not opened.get("ok"):
            stirrer.close()
            raise StirrerError(f"stirrer not available: {opened.get('error')} "
                               f"(enable 'simulate' for dry runs)")

        self.state = "noise_calibration"
        noise = self.calibrate_noise(stirrer)

        angle = stirrer.position_deg() or 0.0
        try:
            for i in range(self.cfg.n_steps):
                if self._stop.is_set():
                    break
                self.state = "moving"
                self._say("stirrer", f"sample {i + 1}/{self.cfg.n_steps}: move +{self.cfg.step_deg}°")
                mv = stirrer.move_rel_and_wait(self.cfg.step_deg, timeout_s=180.0)
                if not mv.get("ok"):
                    raise StirrerError(f"stirrer move failed: {mv.get('error')}")
                angle = mv.get("deg", angle)
                self.current_angle_deg = angle
                time.sleep(self.cfg.settle_s)

                self.state = "servo"
                servo_log = self.servo_pusch(stirrer)

                self.state = "recording"
                window = self.trigger_phone_window(plan)
                if window is None:
                    self._say("campaign", f"sample {i + 1} skipped (phone window failed)")
                    continue

                self.state = "finalizing"
                self.finalize_sample(i + 1, angle, window, servo_log, noise)
                self.samples_done = i + 1
                self._say("campaign", f"sample {i + 1} stored @ {angle:.1f}°")
        finally:
            try:
                stirrer.close()
            except Exception:
                pass
        self.state = "stopped" if self._stop.is_set() else "completed"
        self._say("campaign", f"campaign {self.state} — {self.samples_done} samples")

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
            "error": camp.error,
            "config": camp.cfg.as_dict(),
            "log": camp.log[-40:],
        }

    def samples(self, experiment_id: str) -> list[dict]:
        return self.db.query(
            "SELECT * FROM rc_samples WHERE experiment_id=? ORDER BY sample_index",
            (experiment_id,))


_runners: dict[str, RcRunner] = {}


def get_runner(settings: Settings, db: Database, oai: OaiClient) -> RcRunner:
    key = str(settings.db_path)
    if key not in _runners:
        _runners[key] = RcRunner(settings, db, oai)
    return _runners[key]
