"""Experiment manager: orchestrates the run workflow (task §33, Phase D)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import pandas as pd

from . import fusion, quality, state_machine as sm
from .collectors import ChannelCollector, EventCollector, SnapshotCollector, save_config_provenance
from .config import Settings
from .db import Database
from .oai_client import OaiClient
from .sync import SyncResult, compute_sync, drift_ms_per_s, perform_sync
from .export import export_experiment

PLAN_PHASES_DEFAULT = [
    {"name": "baseline", "durationSeconds": 30},
    {"name": "active", "durationSeconds": 120},
    {"name": "tail", "durationSeconds": 60},
]


def _now_ms() -> int:
    return int(time.time() * 1000)


class ExperimentManager:
    def __init__(self, settings: Settings, db: Database, oai: OaiClient):
        self.settings = settings
        self.db = db
        self.oai = oai
        self.collectors: dict[str, list] = {}

    # ------------------------------------------------------------------ CRUD
    def create_experiment(self, experiment_id: str, environment: str, operator_name: str = "",
                          notes: str = "", purpose: str = "", flow: str = "",
                          initial_oai_config: Optional[str] = None) -> dict:
        from datetime import datetime, timezone
        exp = {
            "experiment_id": experiment_id, "environment": environment,
            "operator_name": operator_name, "notes": notes,
            "purpose": purpose, "flow": flow,
            "initial_oai_config": initial_oai_config,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "schema_version": self.settings.schema_version,
        }
        self.db.upsert_experiment(exp)
        return exp

    def create_condition(self, condition_id: str, experiment_id: str, **kw) -> dict:
        cond = {"condition_id": condition_id, "experiment_id": experiment_id}
        chamber = kw.pop("chamber_metadata", None) or kw.pop("chamber_metadata_json", None)
        for k, v in kw.items():
            cond[k] = v
        cond["chamber_metadata_json"] = json.dumps(chamber, ensure_ascii=False) if chamber else None
        self.db.upsert_condition(cond)
        return cond

    def create_run(self, run_id: str, experiment_id: str, condition_id: str,
                   device_id: Optional[str] = None, session_id: Optional[str] = None,
                   planned_order: Optional[int] = None, random_seed: Optional[int] = None,
                   start_delay_s: float = 30.0) -> dict:
        run = {
            "run_id": run_id, "experiment_id": experiment_id, "condition_id": condition_id,
            "device_id": device_id, "session_id": session_id, "state": "DRAFT",
            "planned_order": planned_order, "random_seed": random_seed,
            "start_delay_s": start_delay_s,
        }
        self.db.upsert_run(run)
        self.db.transition(run_id, "DRAFT", "run created")
        return run

    # ------------------------------------------------------------ prepare run
    def prepare_run(self, run_id: str, requested_config: dict) -> dict:
        """Steps 1–5: validate OAI, snapshot pre-state, apply config, wait, verify."""
        run = self.db.get_run(run_id)
        if not run:
            raise ValueError(f"run not found: {run_id}")
        self._transition(run_id, "PREPARING")

        # Step 1: health
        self.oai.health()
        # Step 2: pre-state
        before = save_config_provenance(run_id, "before", self.settings, self.db, self.oai)
        self.db.set_json(run_id, "requested_config_json", requested_config)

        # Step 3: apply config (single-restart optimization)
        self._transition(run_id, "WAITING_GNB")
        results = self.oai.apply_condition(requested_config)
        progress = results.get("progress")

        # Step 3b: ensure the gNB is actually running (shared helper on OaiClient)
        self.oai.ensure_gnb_running()

        # Step 5: verify
        actual = self.oai.research_config_raw()
        self.db.set_json(run_id, "actual_config_json", actual)
        verify = self._verify_ready(actual, results)
        if verify["ok"]:
            self._transition(run_id, "SYNCING_PHONE", "gNB ready")
        else:
            self._transition(run_id, "FAILED", "; ".join(verify["problems"]))
        return {"before": before, "results": results, "actual": actual, "verify": verify,
                "progress": progress.model_dump() if progress else None}

    def _verify_ready(self, actual: dict, results: dict) -> dict:
        problems = []
        status = self.oai.status()
        if not status.gnb or not status.gnb.running:
            problems.append("gNB not running")
        # UE in-sync (research ues may be stale if no traffic; only flag when collection available+stale)
        ues = self.oai.research_ues()
        coll = ues.collection
        if coll and coll.available and coll.stale:
            problems.append("research collection stale")
        if coll and coll.available and not coll.stale and not ues.ues:
            problems.append("no in-sync UE in research snapshot")
        return {"ok": not problems, "problems": problems}

    # ---------------------------------------------------- phone sync + arm
    def sync_phone(self, run_id: str, channel, n_exchanges: int = 15) -> SyncResult:
        self._transition(run_id, "SYNCING_PHONE")
        res = perform_sync(channel, n_exchanges=n_exchanges)
        res.direction = "before"
        self._store_sync(run_id, res)
        return res

    def build_plan(self, run_id: str, experiment_id: str, condition_id: str,
                   environment: str, start_delay_s: float = 30.0, phases: Optional[list] = None) -> dict:
        phases = phases or PLAN_PHASES_DEFAULT
        return {
            "experimentId": experiment_id,
            "runId": run_id,
            "conditionId": condition_id,
            "environment": environment,
            "plannedStartUtc": "",  # phone uses elapsedRealtime countdown, not wall clock
            "startDelaySeconds": start_delay_s,
            "phases": phases,
        }

    def arm_phone(self, run_id: str, channel, plan: dict) -> dict:
        self._transition(run_id, "ARMED")
        r = channel.agent.session(plan)
        r2 = channel.agent.arm(run_id)
        self._transition(run_id, "PHONE_OFFLINE", "USB disconnect expected")
        return {"session": r, "arm": r2}

    # ------------------------------------------------------------- collectors
    def start_collectors(self, run_id: str) -> None:
        self._transition(run_id, "RUNNING")
        snap = SnapshotCollector(run_id, self.settings, self.db, self.oai, interval_s=1.0)
        ev = EventCollector(run_id, self.settings, self.db, self.oai, interval_s=2.0)
        ch = ChannelCollector(run_id, self.settings, self.db, self.oai, interval_s=1.0)
        for c in (snap, ev, ch):
            c.start()
        self.collectors[run_id] = [snap, ev, ch]

    def stop_collectors(self, run_id: str) -> None:
        for c in self.collectors.pop(run_id, []):
            c.stop()
        for c in self.collectors.get(run_id, []):
            c.join(timeout=5)

    # ------------------------------------------------------- finish + import
    def finish_collection(self, run_id: str) -> dict:
        """Run completed: post-state provenance, stop collectors."""
        self.stop_collectors(run_id)
        after = save_config_provenance(run_id, "after", self.settings, self.db, self.oai)
        self._transition(run_id, "WAITING_PHONE_RETURN")
        return after

    def post_sync(self, run_id: str, channel, n_exchanges: int = 15) -> SyncResult:
        res = perform_sync(channel, n_exchanges=n_exchanges)
        res.direction = "after"
        self._store_sync(run_id, res)
        return res

    def import_phone_data(self, run_id: str, dest_dir: Path) -> dict:
        self._transition(run_id, "IMPORTING")
        # Preserve Level 0: copy phone files into raw/phone/<run_id>/ first.
        import shutil
        raw_phone = self.settings.raw_dir / "phone" / run_id
        raw_phone.mkdir(parents=True, exist_ok=True)
        dest = Path(dest_dir)
        for name in ("phone_samples.csv", "phone_events.csv", "phone_session.json", "phone_sync.json"):
            src = dest / name
            if src.exists():
                dst = raw_phone / name
                if src.resolve() != dst.resolve():
                    shutil.copy2(src, dst)
                self.db.record_file(dst)

        samples_csv = raw_phone / "phone_samples.csv"
        events_csv = raw_phone / "phone_events.csv"
        session_json = raw_phone / "phone_session.json"
        sync_json = raw_phone / "phone_sync.json"

        imported = {}
        if samples_csv.exists():
            df = pd.read_csv(samples_csv)
            cols = [c for c in df.columns if c in _PHONE_SAMPLE_DB_COLS]
            rows = df[cols].where(pd.notna(df[cols]), None).itertuples(index=False, name=None)
            self.db.executemany(
                f"INSERT INTO phone_samples({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
                rows,
            )
            imported["samples"] = int(len(df))
        for f in (events_csv, session_json, sync_json):
            if f.exists():
                self.db.record_file(f)
        imported["events"] = events_csv.exists()
        imported["session"] = session_json.exists()
        imported["sync"] = sync_json.exists()
        return imported

    def align_and_merge(self, run_id: str) -> dict:
        self._transition(run_id, "ALIGNING")
        pre = self._load_sync(run_id, "before")
        post = self._load_sync(run_id, "after")
        phone_rows = self.db.query("SELECT * FROM phone_samples WHERE run_id=?", (run_id,))
        phone = pd.DataFrame(phone_rows)
        if phone.empty:
            self._transition(run_id, "FAILED", "no phone data")
            return {"ok": False, "reason": "no phone data"}

        snap_rows = self.db.query("SELECT * FROM oai_snapshots WHERE run_id=?", (run_id,))
        ev_rows = self.db.query("SELECT * FROM oai_events WHERE run_id=?", (run_id,))
        snap = pd.DataFrame(snap_rows)
        ev = pd.DataFrame(ev_rows)

        phone = fusion.correct_phone_time(phone, pre, post)
        oai_pc_offset = fusion.compute_oai_pc_offset(snap)
        merged = fusion.merge_1s(phone, snap, ev, oai_pc_offset, run_id,
                                 self._condition_id(run_id), self._environment(run_id))

        merged_dir = self.settings.processed_dir / "merged_1s"
        merged_dir.mkdir(parents=True, exist_ok=True)
        merged_path = merged_dir / f"{run_id}.csv"
        merged.to_csv(merged_path, index=False)
        self.db.record_file(merged_path)

        # write corrected phone samples back
        self._store_corrected(phone, run_id)
        return {"ok": True, "merged": str(merged_path), "n_windows": int(len(merged)),
                "oai_pc_offset_ms": oai_pc_offset}

    def run_quality(self, run_id: str) -> dict:
        phone = pd.DataFrame(self.db.query("SELECT * FROM phone_samples WHERE run_id=?", (run_id,)))
        snap = pd.DataFrame(self.db.query("SELECT * FROM oai_snapshots WHERE run_id=?", (run_id,)))
        ev = pd.DataFrame(self.db.query("SELECT * FROM oai_events WHERE run_id=?", (run_id,)))
        pre = self._load_sync(run_id, "before")
        post = self._load_sync(run_id, "after")
        before = self._load_config(run_id, "before")
        after = self._load_config(run_id, "after")
        cond = self.db.query_one("SELECT * FROM conditions WHERE condition_id=?",
                                 (self._condition_id(run_id),))
        target_rsrp = (cond or {}).get("target_rsrp_dbm")
        q = quality.compute_quality(
            phone=phone if not phone.empty else None,
            snapshots=snap if not snap.empty else None,
            events=ev if not ev.empty else None,
            pre=pre, post=post,
            config_before=before, config_after=after,
            target_rsrp_dbm=target_rsrp,
        )
        self.db.set_json(run_id, "quality_flags_json", q["quality_flags"])
        self.db.execute("UPDATE runs SET quality_status=? WHERE run_id=?", (q["quality_status"], run_id))
        final = "COMPLETE" if q["quality_status"] == "PASS" else ("WARNING" if q["quality_status"] == "WARNING" else "FAILED")
        self._transition(run_id, final, "quality: " + ",".join(q["quality_flags"]))
        return q

    def export(self, experiment_id: str) -> Path:
        return export_experiment(self.db, self.settings, experiment_id, self.oai)

    def delete_experiment(self, experiment_id: str) -> dict:
        """Remove an experiment and all of its conditions, runs, and derived data."""
        import shutil

        exp = self.db.query_one("SELECT * FROM experiments WHERE experiment_id=?", (experiment_id,))
        if not exp:
            raise ValueError("experiment not found")

        runs = self.db.query("SELECT run_id FROM runs WHERE experiment_id=?", (experiment_id,))
        run_ids = [r["run_id"] for r in runs]
        clips = self.db.query("SELECT output_path FROM clips WHERE experiment_id=?", (experiment_id,))

        # stop any live collectors for this experiment's runs
        for rid in run_ids:
            self.stop_collectors(rid)

        # run-scoped tables (children of runs)
        for rid in run_ids:
            for table in ("sync_anchors", "oai_snapshots", "oai_events", "oai_channel",
                          "oai_config", "run_transitions", "phone_samples", "rc_samples"):
                self.db.execute(f"DELETE FROM {table} WHERE run_id=?", (rid,))

        # experiment-scoped rows
        self.db.execute("DELETE FROM runs WHERE experiment_id=?", (experiment_id,))
        self.db.execute("DELETE FROM conditions WHERE experiment_id=?", (experiment_id,))
        for table in ("oai_templates", "experiment_acks", "collections", "clips"):
            self.db.execute(f"DELETE FROM {table} WHERE experiment_id=?", (experiment_id,))
        self.db.execute("DELETE FROM experiments WHERE experiment_id=?", (experiment_id,))

        # filesystem cleanup (raw + processed artifacts)
        removed = 0

        def _rm(p: Path) -> bool:
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                    return True
                if p.exists():
                    p.unlink()
                    return True
            except Exception:
                pass
            return False

        for rid in run_ids:
            raw_dir = self.settings.raw_dir / "phone" / rid
            if _rm(raw_dir):
                self.db.execute("DELETE FROM files WHERE file_path LIKE ?", (str(raw_dir) + "%",))
                removed += 1
            for f in (self.settings.processed_dir / "merged_1s" / f"{rid}.csv",
                      self.settings.processed_dir / "time_aligned" / f"{rid}.csv"):
                if _rm(f):
                    self.db.execute("DELETE FROM files WHERE file_path=?", (str(f),))
                    removed += 1

        for c in clips:
            p = Path(c["output_path"]) if c.get("output_path") else None
            if p and _rm(p):
                self.db.execute("DELETE FROM files WHERE file_path=?", (str(p),))
                removed += 1

        return {"ok": True, "experiment_id": experiment_id,
                "deleted_runs": len(run_ids), "removed_files": removed}

    def clear_history_files(self) -> dict:
        """Remove orphaned experiment artifacts after the database is empty."""
        import shutil

        if self.db.query_one("SELECT 1 FROM experiments LIMIT 1") or \
                self.db.query_one("SELECT 1 FROM runs LIMIT 1"):
            raise ValueError("delete all experiments before clearing history files")

        data_root = self.settings.data_dir.resolve()
        targets = (
            self.settings.raw_dir,
            self.settings.processed_dir,
            self.settings.data_dir / "staging",
            self.settings.data_dir / "phone_backup",
        )
        removed_files = 0
        for target in targets:
            resolved = target.resolve()
            try:
                resolved.relative_to(data_root)
            except ValueError as exc:
                raise ValueError(f"history path escapes data directory: {resolved}") from exc
            if resolved.exists():
                removed_files += sum(1 for path in resolved.rglob("*") if path.is_file())
                shutil.rmtree(resolved)

        self.db.execute("DELETE FROM files")
        self.settings.ensure_dirs()
        return {"ok": True, "removed_files": removed_files}

    # ------------------------------------------------------------------ utils
    def _transition(self, run_id: str, to: str, note: str = "") -> None:
        cur = self.db.get_run(run_id)
        frm = cur["state"] if cur else "DRAFT"
        if to != frm and not sm.can_transition(frm, to):
            # allow idempotent re-entry and recovery-forced transitions
            if frm not in ("COMPLETE", "WARNING", "FAILED"):
                self.db.transition(run_id, to, note, from_state=frm)
        else:
            self.db.transition(run_id, to, note, from_state=frm)

    def _store_sync(self, run_id: str, res: SyncResult) -> None:
        for i, ex in enumerate(res.exchanges):
            self.db.execute(
                "INSERT INTO sync_anchors(run_id,direction,attempt_index,t1_ms,t2_elapsed_ns,t2_utc_ms,t3_ms,rtt_ms,offset_ms,uncertainty_ms) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (run_id, res.direction, i, ex["t1_ms"], ex.get("elapsed_ns"), ex.get("phone_utc_ms"),
                 ex["t3_ms"], ex["rtt_ms"], ex["offset_ms"], res.uncertainty_ms),
            )

    def _load_sync(self, run_id: str, direction: str) -> Optional[SyncResult]:
        rows = self.db.query("SELECT * FROM sync_anchors WHERE run_id=? AND direction=? ORDER BY attempt_index",
                             (run_id, direction))
        if not rows:
            return None
        exchanges = [{"t1_ms": r["t1_ms"], "t3_ms": r["t3_ms"],
                      "t2": {"utcEpochMs": r["t2_utc_ms"], "elapsedRealtimeNs": r["t2_elapsed_ns"]}}
                     for r in rows]
        try:
            return compute_sync(exchanges, direction)
        except ValueError:
            return None

    def _load_config(self, run_id: str, stage: str) -> Optional[dict]:
        row = self.db.query_one("SELECT * FROM oai_config WHERE run_id=? AND stage=?", (run_id, stage))
        if not row:
            return None
        p = Path(row["config_json_path"])
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _condition_id(self, run_id: str) -> str:
        r = self.db.get_run(run_id)
        return (r or {}).get("condition_id") or ""

    def _environment(self, run_id: str) -> str:
        r = self.db.get_run(run_id)
        e = self.db.query_one("SELECT * FROM experiments WHERE experiment_id=?", ((r or {}).get("experiment_id") or "",))
        return (e or {}).get("environment") or ""

    def _store_corrected(self, phone: pd.DataFrame, run_id: str) -> None:
        if "t_corrected_epoch_ms" not in phone.columns:
            return
        aligned_dir = self.settings.processed_dir / "time_aligned"
        aligned_dir.mkdir(parents=True, exist_ok=True)
        path = aligned_dir / f"{run_id}.csv"
        phone.to_csv(path, index=False)
        self.db.record_file(path)


_PHONE_SAMPLE_DB_COLS = [
    "run_id", "utc_epoch_ms", "elapsed_realtime_ns", "experiment_id", "condition_id",
    "session_id", "device_id", "phase", "battery_current_now_ua", "battery_current_average_ua",
    "battery_voltage_mv", "battery_power_w", "charge_counter_uah", "soc_percent",
    "battery_temperature_c", "thermal_status", "thermal_headroom",
    "ss_rsrp_dbm", "ss_rsrq_db", "ss_sinr_db", "csi_rsrp_dbm", "csi_rsrq_db", "csi_sinr_db", "csi_cqi",
    "nrarfcn", "pci", "nci", "tac", "network_type", "screen_state", "plugged", "charging",
    "wifi_state", "bluetooth_state", "airplane_mode", "workload_type", "workload_target_mbps",
    "workload_actual_mbps", "app_tx_bytes", "app_rx_bytes", "sample_quality_flags",
]
