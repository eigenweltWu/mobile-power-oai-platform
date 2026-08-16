"""Task-flow logic: downlink/uplink ACK handshake, collection matching, clips."""
from __future__ import annotations

import json
import math
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from .collectors import EventCollector, SnapshotCollector, save_config_provenance
from .config import Settings
from .db import Database
from .oai_client import OaiClient
from .phone_channel import PhoneAgent

# Default phase plan handed to the phone at sync-confirm.
# Flow: idle(idle_seconds) → loaded(collection_seconds) → idle(continuous until
# user stops). The first idle window lets the phone stabilise before the
# loaded test; loaded is the high-occupancy measurement window; after loaded
# the phone returns to idle and keeps recording until the user presses stop
# on either side. A phase with durationSeconds == 0 runs forever (the phone
# RunEngine treats 0-duration as "continuous until stop").
DEFAULT_IDLE_SECONDS = 15.0
DEFAULT_COLLECTION_SECONDS = 120.0
DEFAULT_PHASES = [
    {"name": "idle",     "durationSeconds": DEFAULT_IDLE_SECONDS},
    {"name": "loaded",   "durationSeconds": DEFAULT_COLLECTION_SECONDS},
    {"name": "idle",     "durationSeconds": 0.0},
]


class TemplateSwitchNotAllowed(ValueError):
    """Template switch rejected: the active run is not in an IDLE phase."""


def build_phases(idle_seconds: float = DEFAULT_IDLE_SECONDS,
                 collection_seconds: float = DEFAULT_COLLECTION_SECONDS) -> list[dict]:
    """Compose the idle→loaded→idle plan for the phone."""
    return [
        {"name": "idle",   "durationSeconds": float(idle_seconds)},
        {"name": "loaded", "durationSeconds": float(collection_seconds)},
        {"name": "idle",   "durationSeconds": 0.0},
    ]


class DownlinkLoop:
    """Continuously pings the phone (downlink) and records the uplink ACK timestamps.

    On the first successful ACK it triggers the sync-confirm handshake: the PC
    captures the gNB data timestamp and POSTs it (plus the run plan) to the phone,
    which computes the communication delay and auto-arms the run. The phone stays
    the source of truth for arming — the PC never arms directly.
    """

    def __init__(self, experiment_id: str, serial: str, pc_port: int, db: Database,
                 settings: Settings, oai: OaiClient, plan: dict,
                 run_id: Optional[str] = None, interval_s: float = 2.0):
        self.experiment_id = experiment_id
        self.serial = serial
        self.pc_port = pc_port
        self.db = db
        self.settings = settings
        self.oai = oai
        self.plan = plan
        self.run_id = run_id
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.seq = 0
        self.sync_confirmed = False
        self.last_ack: Optional[dict] = None
        self.last_sync_confirm: Optional[dict] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"downlink-{self.experiment_id}")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _resolve_agent(self):
        """Return (PhoneAgent, None) when a 5G PDU IP is known, else (None, None).

        Live communication ONLY goes over the 5G air interface — during
        experiments the USB cable is unplugged by design. There is no USB
        fallback: USB is used exclusively for post-experiment data
        collection. When the PDU IP is unknown the loop keeps waiting."""
        from .phone_detect import AGENT_PORT, detect_phone
        ph = detect_phone(self.settings, self.serial)
        pdu = ph.get("pdu_ip")
        if pdu:
            return PhoneAgent(base_url=f"http://{pdu}:{AGENT_PORT}"), None
        return None, None

    def _gnb_timestamp_ms(self) -> int:
        """gNB-side data timestamp (ms) captured at sync-confirm for alignment."""
        ues = self.oai.research_ues()
        ns = ues.timestampEpochNs
        return int(ns // 1_000_000) if ns else int(time.time() * 1000)

    def _run(self) -> None:
        while not self._stop.is_set():
            agent = None
            cleanup = None
            try:
                agent, cleanup = self._resolve_agent()
                if agent is None:
                    # phone offline — wait and retry (it may come online shortly)
                    self._stop.wait(self.interval_s)
                    continue

                t_send = time.time() * 1000.0
                self.seq += 1
                resp = agent.downlink(self.seq, t_send)
                t_recv = time.time() * 1000.0

                # The phone only returns a valid ACK when it is in monitoring mode
                # (user clicked "开始任务"). If monitoring=false the phone is alive
                # but not ready — skip recording and keep probing. This ensures the
                # handshake cannot complete until BOTH sides have started.
                if not resp.get("monitoring", True):
                    self._stop.wait(self.interval_s)
                    continue

                phone_recv = resp.get("phoneRecvMs")
                phone_send = resp.get("phoneSendMs")
                phone_elapsed = resp.get("phoneElapsedNs")
                rtt = t_recv - t_send
                t2_utc = ((phone_recv + phone_send) / 2.0) if (phone_recv and phone_send) else None
                offset = ((t_send + rtt / 2.0) - t2_utc) if t2_utc is not None else None

                # 1. experiment_acks row (the uplink ACK record)
                self.db.execute(
                    "INSERT INTO experiment_acks(experiment_id,run_id,seq,direction,pc_send_ms,phone_recv_ms,phone_send_ms,pc_recv_ms,rtt_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                    (self.experiment_id, self.run_id, self.seq, "downlink", t_send,
                     phone_recv, phone_send, t_recv, rtt))
                # 2. PC sync_anchors row (every downlink, reused by fusion/offset)
                if self.run_id:
                    self.db.execute(
                        "INSERT INTO sync_anchors(run_id,direction,attempt_index,t1_ms,t2_elapsed_ns,t2_utc_ms,t3_ms,rtt_ms,offset_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                        (self.run_id, "before", self.seq, t_send, phone_elapsed,
                         t2_utc, t_recv, rtt, offset))
                self.last_ack = {"seq": self.seq, "pc_send_ms": t_send, "pc_recv_ms": t_recv,
                                 "phone_recv_ms": phone_recv, "phone_send_ms": phone_send,
                                 "phone_elapsed_ns": phone_elapsed, "rtt_ms": rtt}

                # 3. first successful ACK → trigger sync-confirm (phone auto-arms)
                if not self.sync_confirmed:
                    self._trigger_sync_confirm(agent)
            except Exception:
                # transient (phone toggling, gNB restarting) — retry next tick
                pass
            finally:
                if cleanup is not None:
                    try:
                        cleanup()
                    except Exception:
                        pass
            self._stop.wait(self.interval_s)

    def _trigger_sync_confirm(self, agent: PhoneAgent) -> None:
        """Hand the phone the PC + gNB timestamps so it can arm. Idempotent: a
        failure leaves sync_confirmed=False so the next ACK retries (the phone
        side is also idempotent)."""
        if self.sync_confirmed:
            return
        try:
            gnb_ts = self._gnb_timestamp_ms()
        except Exception:
            gnb_ts = int(time.time() * 1000)
        try:
            pc_ts = time.time() * 1000.0
            r = agent.sync_confirm(pc_ts, gnb_ts, self.plan)
            if not r.get("ok"):
                return
            self.sync_confirmed = True
            self.last_sync_confirm = r
            # record the sync_confirm ack row (carries the gNB data timestamp)
            self.db.execute(
                "INSERT INTO experiment_acks(experiment_id,run_id,seq,direction,pc_send_ms,phone_recv_ms,phone_send_ms,pc_recv_ms,rtt_ms,gnb_data_timestamp_ms) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (self.experiment_id, self.run_id, -1, "sync_confirm", pc_ts,
                 r.get("phone_timestamp_ms"), None, None, None, gnb_ts))
            if self.run_id:
                self.db.transition(self.run_id, "ARMED", "phone sync-confirm received")
                self.db.transition(self.run_id, "RUNNING", "phone armed after handshake")
        except Exception:
            # phone not ready / transient — retry on the next ACK
            pass


class TaskFlow:
    def __init__(self, settings: Settings, db: Database, oai: OaiClient):
        self.s = settings
        self.db = db
        self.oai = oai
        self.downlinks: dict[str, DownlinkLoop] = {}
        # OAI research collectors per experiment (SnapshotCollector writes
        # ul/dl_goodput_mbps + PUSCH stats into oai_snapshots every second).
        self.collectors: dict[str, list] = {}

    # ---- phone connection (detected before every phone operation) ---------- #
    @contextmanager
    def _phone(self, serial: str, pc_port: int = 8420):
        """Yield (PhoneAgent, detection). Raise if the phone is unreachable.

        5G air interface ONLY — during experiments USB is unplugged by design;
        USB is used exclusively for post-experiment data collection."""
        from .phone_detect import AGENT_PORT, detect_phone
        ph = detect_phone(self.s, serial)
        pdu = ph.get("pdu_ip")
        if pdu:
            yield PhoneAgent(base_url=f"http://{pdu}:{AGENT_PORT}"), ph
        else:
            raise RuntimeError("phone unreachable: no 5G PDU link (USB is not used during experiments)")

    # ---- OAI research collectors (goodput / PUSCH recording) ---------------- #
    def _start_collectors(self, run_id: Optional[str], experiment_id: str) -> None:
        """Start the per-run research collectors (replacing any leftover ones).

        SnapshotCollector persists ul/dl_goodput_mbps into oai_snapshots every
        second — without it the platform never records the gNB-side goodput."""
        self._stop_collectors(experiment_id)
        if not run_id:
            return
        try:
            save_config_provenance(run_id, "before", self.s, self.db, self.oai)
        except Exception:
            pass  # provenance is best-effort; the run itself may proceed
        snap = SnapshotCollector(run_id, self.s, self.db, self.oai, interval_s=1.0)
        ev = EventCollector(run_id, self.s, self.db, self.oai, interval_s=2.0)
        snap.start()
        ev.start()
        self.collectors[experiment_id] = [snap, ev]

    def _stop_collectors(self, experiment_id: str) -> None:
        for c in self.collectors.pop(experiment_id, []):
            c.stop()
            c.join(timeout=5)

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

    def apply_template(self, experiment_id: str, template_id: int,
                       serial: str = "53616213", pc_port: int = 8420) -> dict:
        row = self.db.query_one("SELECT * FROM oai_templates WHERE id=? AND experiment_id=?", (template_id, experiment_id))
        if not row:
            raise ValueError("template not found")
        cfg = json.loads(row["config_json"])

        # Template switching is only allowed while the experiment is IDLE:
        # either no run in flight, or the active run currently sits in an
        # IDLE phase (head or tail). Switching mid-LOADED would corrupt the
        # measurement window, and switching during PREPARING would fight the
        # start flow's own gNB restart. The phase is read over the 5G PDU
        # link only — USB is never used while an experiment is running.
        run = self.db.query_one(
            "SELECT run_id, state FROM runs WHERE experiment_id=? "
            "AND state IN ('PREPARING','ARMED','RUNNING') ORDER BY rowid DESC LIMIT 1",
            (experiment_id,))
        if run:
            try:
                with self._phone(serial, pc_port) as (agent, ph):
                    # NOTE: _phone() yields a (PhoneAgent, detection) tuple —
                    # the detection dict already carries the phone's
                    # /agent/status payload harvested during PDU probing.
                    phase = ((ph.get("status") or {}).get("phase"))
            except Exception as e:
                raise TemplateSwitchNotAllowed(
                    f"cannot confirm idle phase: phone unreachable over 5G PDU ({e})")
            if phase != "IDLE":
                raise TemplateSwitchNotAllowed(
                    f"template switch allowed only in idle phase (current: {phase or 'unknown'})")

        # force_restart: the template's RF conditions only take effect on a
        # REAL gNB restart — the phone then re-runs idle → loaded → idle.
        result = self.oai.apply_condition(cfg, force_restart=True)

        # The gNB restarted with new RF conditions — re-trigger the phone's
        # phase machine (idle → loaded → idle) over the 5G PDU link.
        rearm: dict = {"attempted": False}
        if run:
            rearm["attempted"] = True
            try:
                with self._phone(serial, pc_port) as (agent, ph):
                    rearm.update(agent.rearm())
            except Exception as e:
                rearm["error"] = str(e)
        return {"config": cfg, "result": result, "rearm": rearm}

    # ---- start / stop ------------------------------------------------------ #
    def start_experiment(self, experiment_id: str, serial: str, pc_port: int = 8420,
                         collection_seconds: Optional[float] = None,
                         idle_seconds: Optional[float] = None,
                         template_id: Optional[int] = None) -> dict:
        exp = self.db.query_one("SELECT * FROM experiments WHERE experiment_id=?", (experiment_id,))
        if not exp:
            raise ValueError("experiment not found")

        # Initial OAI config resolution: the selected Template (there is no
        # separate "initial config" concept — the startup config IS one of the
        # experiment's templates) > the experiment's stored initial_oai_config.
        initial = None
        if template_id is not None:
            row = self.db.query_one(
                "SELECT config_json FROM oai_templates WHERE id=? AND experiment_id=?",
                (template_id, experiment_id))
            if row and row["config_json"]:
                initial = json.loads(row["config_json"])
        if initial is None:
            initial = json.loads(exp["initial_oai_config"]) if exp.get("initial_oai_config") else {}

        # Resolve idle/collection timings (explicit param > global default).
        idle_s = float(idle_seconds) if idle_seconds is not None else DEFAULT_IDLE_SECONDS
        collection_s = (float(collection_seconds)
                        if collection_seconds is not None else DEFAULT_COLLECTION_SECONDS)

        # A FRESH run_id for every start — never reuse a previous run. The id
        # travels to the phone inside the sync-confirm plan; the phone persists
        # it (RunEntity) so later data exchange can match on it.
        run_id = f"{experiment_id}_r{int(time.time() * 1000)}"
        condition_id = f"{experiment_id}_default"
        plan = {
            "experimentId": experiment_id,
            "runId": run_id,
            "conditionId": condition_id,
            "environment": exp["environment"],
            "plannedStartUtc": "",  # phone uses elapsedRealtime countdown, not wall clock
            "startDelaySeconds": 0.0,
            "idleSeconds": idle_s,
            "collectionSeconds": collection_s,
            "phases": build_phases(idle_s, collection_s),
        }

        # Stop any leftover downlink loop for this experiment before starting
        # a new one (repeated starts must not leak probes fighting each other).
        old_loop = self.downlinks.pop(experiment_id, None)
        if old_loop:
            old_loop.stop()

        # Create/activate the run so the dashboard's latest_run reflects the
        # current experiment (not a stale STOPPED row from a previous run).
        # runs.condition_id has an FK to conditions — make sure the row exists
        # (auto experiments may reference a synthesized "<exp>_default" id).
        if not self.db.query_one("SELECT 1 FROM conditions WHERE condition_id=?", (condition_id,)):
            self.db.upsert_condition({
                "condition_id": condition_id,
                "experiment_id": experiment_id,
                "environment": exp["environment"],
            })
        self.db.upsert_run({
            "run_id": run_id, "experiment_id": experiment_id,
            "condition_id": condition_id,
            "device_id": serial, "session_id": None, "state": "PREPARING",
            "planned_order": None, "random_seed": None,
            "start_delay_s": 0.0,
        })
        self.db.transition(run_id, "PREPARING", "start_experiment: gNB starting + downlink loop")

        # 1. Ensure the gNB process is running (do NOT block waiting for the
        #    UE here — after the restart below the UE re-selects the cell and
        #    completes the air-interface handshake on its own).
        gnb_ready = self.oai.ensure_gnb_running(wait_ue=False)

        # 2. Apply the selected template's config unconditionally — the
        #    experiment must run under the chosen RF conditions, so a running
        #    gNB is restarted with the template values.
        result = self.oai.apply_condition(initial) if initial else {"no_config": True}

        # 3. Verify gNB + UE in-sync
        st = self.oai.status()
        ready = bool(st.gnb and st.gnb.running)
        try:
            ues = self.oai.research_ues()
            in_sync = bool(ues.ues) and bool(ues.collection and not ues.collection.stale)
        except Exception:
            in_sync = False

        # 4. Start the downlink loop (records ACKs, triggers sync-confirm on the
        #    first ACK — the phone arms itself, the PC never arms directly)
        loop = DownlinkLoop(experiment_id, serial, pc_port, self.db, self.s, self.oai, plan, run_id)
        loop.start()
        self.downlinks[experiment_id] = loop

        # 5. OAI research collectors — record per-UE ul/dl_goodput_mbps, PUSCH
        #    SNR/MCS/PRB into oai_snapshots (1 s) and scheduler events into
        #    oai_events (2 s) for THIS run, so the platform stores the gNB-side
        #    goodput alongside the phone telemetry.
        self._start_collectors(run_id, experiment_id)
        return {"ok": ready and in_sync, "gnb_started": gnb_ready, "gnb_running": ready,
                "ue_in_sync": in_sync, "config_applied": initial, "downlink_started": True,
                "sync_pending": True, "run_id": run_id}

    def stop_experiment(self, experiment_id: str, serial: str = "53616213",
                        pc_port: int = 8420) -> dict:
        """Complete stop: downlink loop + phone stop_task + gNB stop + run transition.

        Mirrors the /api/runs/{run_id}/stop flow so both stop entry points
        (by experiment_id and by run_id) do the full teardown — the phone
        receives the stop command (records its stop timestamp) and the gNB
        is shut down, not just the downlink loop.
        """
        loop = self.downlinks.pop(experiment_id, None)
        run_id = loop.run_id if loop else None
        if loop:
            loop.stop()
        # Stop the OAI research collectors FIRST (before the gNB goes down) so
        # the last goodput samples are flushed with a reachable API.
        self._stop_collectors(experiment_id)
        if run_id is None:
            # Loop lost (e.g. backend restarted mid-run): fall back to the
            # experiment's active run row so the run still gets marked STOPPED
            # and the dashboard switches back to the start button.
            row = self.db.query_one(
                "SELECT run_id FROM runs WHERE experiment_id=? "
                "AND state IN ('PREPARING','ARMED','RUNNING') ORDER BY rowid DESC LIMIT 1",
                (experiment_id,))
            run_id = row["run_id"] if row else None
        stop_ms = int(time.time() * 1000)
        # Post-state provenance (config snapshot after the run).
        if run_id:
            try:
                save_config_provenance(run_id, "after", self.s, self.db, self.oai)
            except Exception:
                pass

        # 1. PC stop ACK row
        self.db.execute(
            "INSERT INTO experiment_acks(experiment_id,run_id,seq,direction,pc_send_ms,phone_recv_ms,phone_send_ms,pc_recv_ms,rtt_ms) VALUES(?,?,?,?,?,?,?,?,?)",
            (experiment_id, run_id, -1, "pc_stop", stop_ms, None, None, None, None))

        # 2. Tell the phone to stop (records the phone-side stop timestamp)
        phone_stop_ms = None
        try:
            with self._phone(serial, pc_port) as (agent, _ph):
                pr = agent.stop_task()
                phone_stop_ms = pr.get("stopUtcMs")
        except Exception:
            pass  # phone already offline / unplugged — PC stop still recorded

        # 3. Stop the gNB
        try:
            self.oai.gnb_service("stop")
        except Exception:
            pass

        # 4. Mark the run STOPPED
        if run_id:
            self.db.transition(run_id, "STOPPED", "stopped by user", utc_ms=stop_ms)
        return {"ok": True, "pc_stop_ms": stop_ms, "phone_stop_ms": phone_stop_ms, "run_id": run_id}

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
        # gNB-side research snapshots (ul/dl_goodput_mbps recorded by the
        # SnapshotCollector during the run), UTC-stamped for alignment with
        # the phone samples (phone_samples.utc_epoch_ms).
        gnb = []
        for rid in run_ids:
            gnb += self.db.query(
                "SELECT run_id, fetched_utc_ms, ts_utc, rnti, ul_goodput_mbps, dl_goodput_mbps,"
                " pusch_snr_db, ul_mcs, n_prb, collection_stale"
                " FROM oai_snapshots WHERE run_id=? ORDER BY fetched_utc_ms", (rid,))
        # CIR multipath metrics time-series (oai_channel table).
        channel = []
        for rid in run_ids:
            channel += self.db.query(
                "SELECT run_id, fetched_utc_ms, ts_utc, rms_delay_ns, k_factor_db,"
                " tap_count, peak_db, noise_db, mean_delay_ns"
                " FROM oai_channel WHERE run_id=? ORDER BY fetched_utc_ms", (rid,))
        # Latest CIR power-delay profile (|h(tau)|^2 in dB) for the PDP chart.
        cir = None
        if run_ids:
            latest = self.db.query_one(
                "SELECT raw_json_path, dt_ns FROM oai_channel WHERE run_id=?"
                " ORDER BY fetched_utc_ms DESC LIMIT 1", (run_ids[0],))
            if latest and latest.get("raw_json_path"):
                p = Path(latest["raw_json_path"])
                if p.exists():
                    try:
                        raw = json.loads(p.read_text(encoding="utf-8"))
                        re = raw.get("cirRe") or []
                        im = raw.get("cirIm") or []
                        dt_ns = latest.get("dt_ns") or raw.get("dtNs") or 8.138
                        n = min(len(re), len(im))
                        step = max(1, n // 512)
                        pdp = []
                        for i in range(0, n, step):
                            v = re[i] * re[i] + im[i] * im[i]
                            pdp.append({"tau_ns": round(i * dt_ns, 1),
                                        "power_db": round(10.0 * math.log10(max(v, 1e-30)), 2)})
                        cir = {"dt_ns": dt_ns, "n_samples": n, "pdp": pdp}
                    except Exception:
                        cir = None
        return {"samples": rows, "acks": acks, "clips": clips, "runs": runs,
                "gnb": gnb, "channel": channel, "cir": cir}

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
