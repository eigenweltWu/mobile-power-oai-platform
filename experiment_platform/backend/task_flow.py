"""Task-flow logic: downlink/uplink ACK handshake, collection matching, clips."""
from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from .config import Settings
from .db import Database
from .oai_client import OaiClient
from .phone_channel import PhoneAgent, PhoneChannel


class DownlinkLoop:
    """Continuously pings the phone (downlink) and records the uplink ACK timestamps."""

    def __init__(self, experiment_id: str, serial: str, pc_port: int, db: Database,
                 interval_s: float = 2.0):
        self.experiment_id = experiment_id
        self.serial = serial
        self.pc_port = pc_port
        self.db = db
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.seq = 0
        self.last_ack: Optional[dict] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"downlink-{self.experiment_id}")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with PhoneChannel(self.serial, self.pc_port) as ch:
                    t_send = time.time() * 1000.0
                    self.seq += 1
                    resp = ch.agent.downlink(self.seq, t_send)
                    t_recv = time.time() * 1000.0
                    ack = {
                        "seq": self.seq,
                        "pc_send_ms": t_send,
                        "pc_recv_ms": t_recv,
                        "phone_recv_ms": resp.get("phoneRecvMs"),
                        "phone_send_ms": resp.get("phoneSendMs"),
                        "rtt_ms": t_recv - t_send,
                    }
                    self.db.execute(
                        "INSERT INTO experiment_acks(experiment_id,run_id,seq,direction,pc_send_ms,phone_recv_ms,phone_send_ms,pc_recv_ms,rtt_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                        (self.experiment_id, None, self.seq, "downlink", t_send,
                         ack["phone_recv_ms"], ack["phone_send_ms"], t_recv, ack["rtt_ms"]))
                    self.last_ack = ack
            except Exception:
                pass
            self._stop.wait(self.interval_s)


class TaskFlow:
    def __init__(self, settings: Settings, db: Database, oai: OaiClient):
        self.s = settings
        self.db = db
        self.oai = oai
        self.downlinks: dict[str, DownlinkLoop] = {}

    # ---- phone connection (detected before every phone operation) ---------- #
    @contextmanager
    def _phone(self, serial: str, pc_port: int = 8420):
        """Yield (PhoneAgent, detection). Raise if the phone is unreachable."""
        from .phone_detect import detect_phone
        ph = detect_phone(self.s, serial)
        if ph["state"] == "CONNECTED" and ph.get("agent_url"):
            yield PhoneAgent(base_url=ph["agent_url"]), ph
        elif ph["state"] == "ATTACHED":
            ch = PhoneChannel(serial, pc_port)
            ch.connect()
            try:
                yield ch.agent, ph
            finally:
                ch.disconnect()
        else:
            raise RuntimeError("phone OFFLINE: neither USB-attached nor 5G-reachable")

    # ---- OAI templates ----------------------------------------------------- #
    def list_templates(self, experiment_id: str) -> list[dict]:
        return self.db.query("SELECT * FROM oai_templates WHERE experiment_id=? ORDER BY id", (experiment_id,))

    def add_template(self, experiment_id: str, name: str, config: dict) -> dict:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute("INSERT INTO oai_templates(experiment_id,name,config_json,created_utc) VALUES(?,?,?,?)",
                        (experiment_id, name, json.dumps(config, ensure_ascii=False), now))
        return {"name": name, "config": config}

    def delete_template(self, experiment_id: str, template_id: int) -> None:
        self.db.execute("DELETE FROM oai_templates WHERE id=? AND experiment_id=?", (template_id, experiment_id))

    def apply_template(self, experiment_id: str, template_id: int) -> dict:
        row = self.db.query_one("SELECT * FROM oai_templates WHERE id=? AND experiment_id=?", (template_id, experiment_id))
        if not row:
            raise ValueError("template not found")
        cfg = json.loads(row["config_json"])
        result = self.oai.apply_condition(cfg)
        return {"config": cfg, "result": result}

    # ---- start / stop ------------------------------------------------------ #
    def start_experiment(self, experiment_id: str, serial: str, pc_port: int = 8420) -> dict:
        exp = self.db.query_one("SELECT * FROM experiments WHERE experiment_id=?", (experiment_id,))
        if not exp:
            raise ValueError("experiment not found")
        initial = json.loads(exp["initial_oai_config"]) if exp.get("initial_oai_config") else {}

        # 1. Ensure the gNB is started (agreed flow: Start Experiment auto-starts gNB)
        gnb_ready = self.oai.ensure_gnb_running()

        # 2. Apply the initial OAI config (may restart again if params differ)
        result = self.oai.apply_condition(initial) if initial else {"no_config": True}

        # 3. Verify gNB + UE in-sync
        st = self.oai.status()
        ready = bool(st.gnb and st.gnb.running)
        try:
            ues = self.oai.research_ues()
            in_sync = bool(ues.ues) and bool(ues.collection and not ues.collection.stale)
        except Exception:
            in_sync = False

        # 4. Start the downlink loop (records uplink ACK timestamps)
        loop = DownlinkLoop(experiment_id, serial, pc_port, self.db)
        loop.start()
        self.downlinks[experiment_id] = loop
        return {"ok": ready and in_sync, "gnb_started": gnb_ready, "gnb_running": ready,
                "ue_in_sync": in_sync, "config_applied": initial, "downlink_started": True}

    def stop_experiment(self, experiment_id: str) -> dict:
        loop = self.downlinks.pop(experiment_id, None)
        if loop:
            loop.stop()
        stop_ms = int(time.time() * 1000)
        self.db.execute(
            "INSERT INTO experiment_acks(experiment_id,run_id,seq,direction,pc_send_ms,phone_recv_ms,phone_send_ms,pc_recv_ms,rtt_ms) VALUES(?,?,?,?,?,?,?,?,?)",
            (experiment_id, None, -1, "pc_stop", stop_ms, None, None, None, None))
        return {"ok": True, "pc_stop_ms": stop_ms}

    # ---- push / phone inventory ------------------------------------------- #
    def push_task(self, experiment_id: str, serial: str, pc_port: int = 8420) -> dict:
        exp = self.db.query_one("SELECT * FROM experiments WHERE experiment_id=?", (experiment_id,))
        if not exp:
            raise ValueError("experiment not found")
        templates = self.list_templates(experiment_id)
        task = {
            "experimentId": experiment_id,
            "environment": exp["environment"],
            "purpose": exp.get("purpose") or "",
            "flow": exp.get("flow") or "",
            "templates": [{"name": t["name"], "config": json.loads(t["config_json"])} for t in templates],
        }
        with self._phone(serial, pc_port) as (agent, _ph):
            return agent.push_task(task)

    def list_phone_tasks(self, serial: str, pc_port: int = 8420) -> dict:
        with self._phone(serial, pc_port) as (agent, _ph):
            return agent.list_tasks()

    # ---- collection -------------------------------------------------------- #
    def collect_from_phone(self, experiment_id: str, serial: str, hostname: str, pc_port: int = 8420) -> dict:
        dest = self.s.raw_dir / "phone" / experiment_id
        dest.mkdir(parents=True, exist_ok=True)
        with self._phone(serial, pc_port) as (agent, _ph):
            files = agent.export(dest)
            # mark collected on the phone (records time + hostname + count)
            agent.mark_collected(experiment_id, hostname)
        for f in files:
            self.db.record_file(f)
        count = self.db.query_one("SELECT COUNT(*) n FROM collections WHERE experiment_id=?", (experiment_id,))
        now = int(time.time() * 1000)
        self.db.execute(
            "INSERT INTO collections(experiment_id,device_id,hostname,collected_utc_ms,files_json,count) VALUES(?,?,?,?,?,?)",
            (experiment_id, serial, hostname, now,
             json.dumps([str(f) for f in files]), (count["n"] if count else 0) + 1))
        return {"ok": True, "files": [f.name for f in files], "hostname": hostname,
                "collected_utc_ms": now, "count": (count["n"] if count else 0) + 1}

    def phone_inventory(self, serial: str, pc_port: int = 8420) -> dict:
        """List phone-side experiments and whether this PC has collected each."""
        try:
            phone_tasks = self.list_phone_tasks(serial, pc_port)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        items = phone_tasks.get("experiments", []) if isinstance(phone_tasks, dict) else []
        out = []
        for it in items:
            eid = it.get("experimentId") if isinstance(it, dict) else it
            collected = self.db.query(
                "SELECT COUNT(*) n, MAX(collected_utc_ms) last FROM collections WHERE experiment_id=?", (eid,))
            out.append({"experiment_id": eid, "collected_count": collected[0]["n"] if collected else 0,
                        "last_collected_ms": collected[0]["last"] if collected else None, "raw": it})
        return {"ok": True, "phone_experiments": out}

    # ---- timeline / clip --------------------------------------------------- #
    def timeline(self, experiment_id: str) -> dict:
        runs = self.db.query("SELECT run_id FROM runs WHERE experiment_id=?", (experiment_id,))
        run_ids = [r["run_id"] for r in runs]
        rows = []
        for rid in run_ids:
            rows += self.db.query("SELECT * FROM phone_samples WHERE run_id=? ORDER BY elapsed_realtime_ns", (rid,))
        acks = self.db.query("SELECT * FROM experiment_acks WHERE experiment_id=? ORDER BY id", (experiment_id,))
        markers = []
        for a in acks:
            markers.append({"kind": "ack", "ms": a["pc_send_ms"], "rtt_ms": a["rtt_ms"]})
        clips = self.db.query("SELECT * FROM clips WHERE experiment_id=?", (experiment_id,))
        return {"samples": rows, "acks": acks, "clips": clips, "runs": runs}

    def clip(self, experiment_id: str, run_id: Optional[str], start_ms: float, end_ms: float,
             label: str) -> dict:
        rows = self.db.query(
            "SELECT * FROM phone_samples WHERE run_id=? AND elapsed_realtime_ns>=? AND elapsed_realtime_ns<=? ORDER BY elapsed_realtime_ns",
            (run_id, int(start_ms * 1e6), int(end_ms * 1e6)))
        out_dir = self.s.processed_dir / "clips"
        out_dir.mkdir(parents=True, exist_ok=True)
        import pandas as pd
        df = pd.DataFrame(rows)
        fname = f"{experiment_id}_{label or 'clip'}_{int(start_ms)}_{int(end_ms)}.csv"
        path = out_dir / fname
        df.to_csv(path, index=False)
        self.db.record_file(path)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "INSERT INTO clips(experiment_id,run_id,start_ms,end_ms,label,created_utc,output_path) VALUES(?,?,?,?,?,?,?)",
            (experiment_id, run_id, start_ms, end_ms, label, now, str(path)))
        return {"ok": True, "path": str(path), "n_rows": int(len(df))}


_flows: dict[str, TaskFlow] = {}


def get_flow(settings: Settings, db: Database, oai: OaiClient) -> TaskFlow:
    key = str(settings.db_path)
    if key not in _flows:
        _flows[key] = TaskFlow(settings, db, oai)
    return _flows[key]
