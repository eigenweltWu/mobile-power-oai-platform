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

from .collectors import ChannelCollector, EventCollector, SnapshotCollector, save_config_provenance
from .config import Settings
from .db import Database
from .oai_client import OaiClient
from .phone_channel import PhoneAgent
from .templates import UL_TRAFFIC_DEFAULT_MBPS

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


def quiesce_phone(serial: str, on: bool) -> bool:
    """Best-effort airplane toggle over USB adb AROUND a forced gNB restart.

    The OAI build asserts and the gNB container dies (exit 134,
    get_searchspace()) whenever a UE performs RRC re-establishment with stale
    modem context — exactly what a phone does when its gNB goes away for a
    restart and comes back before the phone has cycled its radio. Toggling
    airplane mode first clears the modem context so the post-restart attach
    is a FRESH registration (RRC Setup) instead of re-establishment. In real
    over-the-air experiments USB is unplugged, quiesce is skipped and the
    phone's own 60 s no-signal airplane recovery covers the gap.
    """
    try:
        from .phone_channel import AdbTransport
        transport = AdbTransport()
        if serial not in transport.devices():
            return False
        val = "1" if on else "0"
        state = "true" if on else "false"
        transport.shell(serial, f"settings put global airplane_mode_on {val}")
        transport.shell(
            serial, f"am broadcast -a android.intent.action.AIRPLANE_MODE_CHANGED --ez state {state}")
        return True
    except Exception:
        return False


def build_phases(idle_seconds: float = DEFAULT_IDLE_SECONDS,
                 collection_seconds: float = DEFAULT_COLLECTION_SECONDS) -> list[dict]:
    """Compose the idle→loaded→idle plan for the phone."""
    return [
        {"name": "idle",   "durationSeconds": float(idle_seconds)},
        {"name": "loaded", "durationSeconds": float(collection_seconds)},
        {"name": "idle",   "durationSeconds": 0.0},
    ]


def shake_and_refresh(settings, oai, db, experiment_id, run_id=None,
                      n_exchanges: int = 3) -> dict:
    """gNB 重启后经 OAI /api/shake 完成对时并刷新 UE PDU IP.

    Every gNB restart (experiment start OR template switch) kicks the UE
    offline; it re-registers and usually lands on a NEW PDU address that the
    PC cannot discover by itself (no USB during experiments, and the PC is
    not in the PDU subnet). /shake solves both at once:

    1. ``ue_ip`` — resolved by the OAI host from the oai-upf session table
       (USB-free). Refreshed into phone_state.json so detect_phone and the
       DownlinkLoop immediately use the new address.
    2. ``exchanges`` — NTP-style timestamp exchanges the OAI host performs
       against the phone agent (requires the phone to be in monitoring
       mode). Stamped with the OAI host clock, converted to the PC clock
       base via oai_pc_offset_ms and recorded into experiment_acks
       (direction='shake') + sync_anchors so the clock status reflects the
       shake-based sync even before 5G-direct downlink ACKs resume.

    Never raises — returns {ok, stage, ...} describing the outcome.
    """
    from .phone_detect import refresh_pdu_ip
    try:
        resp = oai.shake(n_exchanges)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "stage": "shake", "error": str(e)}

    ue_ip = resp.get("ue_ip")
    out = {"ok": bool(resp.get("ok")), "ue_ip": ue_ip,
           "error": resp.get("error"), "monitoring": resp.get("monitoring")}
    if not ue_ip:
        out.setdefault("error", out.get("error") or "no UE PDU session")
        return out

    # 1. refresh the cached PDU address — the DownlinkLoop resumes probing
    #    the CURRENT UE address over 5G right after this.
    try:
        refresh_pdu_ip(settings, ue_ip)
    except Exception as e:  # noqa: BLE001
        out["refresh_error"] = str(e)

    # 2. record the timestamped exchanges (phone in monitoring mode) on the
    #    PC clock base so the clock status can flip to synced from the shake.
    try:
        off = oai.oai_pc_offset_ms()
    except Exception:  # noqa: BLE001
        off = 0.0
    ex_ok = False
    for ex in (resp.get("exchanges") or []):
        if not ex.get("monitoring", False):
            continue
        pr, ps = ex.get("phone_recv_ms"), ex.get("phone_send_ms")
        if not (pr and ps and ex.get("pc_send_ms") and ex.get("pc_recv_ms")):
            continue
        t1 = ex["pc_send_ms"] + off
        t3 = ex["pc_recv_ms"] + off
        t2_utc = (pr + ps) / 2.0
        rtt = ex["rtt_ms"]
        offset = (t1 + rtt / 2.0) - t2_utc
        db.execute(
            "INSERT INTO experiment_acks(experiment_id,run_id,seq,direction,pc_send_ms,phone_recv_ms,phone_send_ms,pc_recv_ms,rtt_ms) VALUES(?,?,?,?,?,?,?,?,?)",
            (experiment_id, run_id, ex.get("seq"), "shake", t1, pr, ps, t3, rtt))
        if run_id:
            db.execute(
                "INSERT INTO sync_anchors(run_id,direction,attempt_index,t1_ms,t2_elapsed_ns,t2_utc_ms,t3_ms,rtt_ms,offset_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, "before", ex.get("seq") or 0, t1,
                 ex.get("phone_elapsed_ns"), t2_utc, t3, rtt, offset))
        ex_ok = True
    out["exchanges_recorded"] = ex_ok
    if ex_ok:
        out["rtt_ms"] = resp.get("rtt_ms")
        out["offset_ms"] = resp.get("offset_ms")
    return out


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
        self._last_shake_ms = 0.0
        self.last_shake: Optional[dict] = None

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
                    # phone offline — after a gNB restart the UE usually
                    # re-registers with a NEW PDU address the PC cannot see.
                    # Ask the OAI host (inside the PDU subnet) via /shake to
                    # re-resolve the address (and sync clocks when the phone
                    # is already monitoring). Throttled: /shake scans UPF
                    # docker logs, too expensive to run every 2 s probe.
                    now_ms = time.time() * 1000.0
                    if now_ms - self._last_shake_ms >= 10_000:
                        self._last_shake_ms = now_ms
                        self.last_shake = shake_and_refresh(
                            self.settings, self.oai, self.db,
                            self.experiment_id, self.run_id, n_exchanges=1)
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
        # ChannelCollector persists complex-CIR multipath metrics + the raw
        # power-delay profile into oai_channel (feeds the Timeline CIR charts).
        # The OAI producer now snapshots on demand, so continuous polling is
        # safe for both AC and RC while RcCampaign still stores settled samples.
        ch = ChannelCollector(run_id, self.s, self.db, self.oai, interval_s=1.0)
        snap.start()
        ev.start()
        ch.start()
        self.collectors[experiment_id] = [snap, ev, ch]

    def _stop_collectors(self, experiment_id: str) -> None:
        for c in self.collectors.pop(experiment_id, []):
            c.stop()
            c.join(timeout=5)

    # ---- OAI templates ----------------------------------------------------- #
    def list_templates(self, experiment_id: str) -> list[dict]:
        return self.db.query(
            "SELECT t.*, CASE WHEN e.default_template_id=t.id THEN 1 ELSE 0 END AS is_default, "
            "(SELECT COUNT(*) FROM runs r WHERE r.configuration_id=t.id) AS used_by_runs "
            "FROM oai_templates t JOIN experiments e ON e.experiment_id=t.experiment_id "
            "WHERE t.experiment_id=? AND t.archived_utc IS NULL ORDER BY t.id",
            (experiment_id,))

    def add_template(self, experiment_id: str, name: str, config: dict) -> dict:
        from datetime import datetime, timezone
        name = str(name or "").strip()
        if not name:
            raise ValueError("configuration name is required")
        if self.db.query_one(
                "SELECT 1 FROM oai_templates WHERE experiment_id=? AND name=? AND archived_utc IS NULL",
                (experiment_id, name)):
            raise ValueError("configuration name already exists")
        now = datetime.now(timezone.utc).isoformat()
        template_id = self.db.execute(
            "INSERT INTO oai_templates(experiment_id,name,config_json,created_utc,updated_utc) VALUES(?,?,?,?,?)",
            (experiment_id, name, json.dumps(config, ensure_ascii=False), now, now))
        return self.db.query_one("SELECT * FROM oai_templates WHERE id=?", (template_id,))

    def update_template(self, experiment_id: str, template_id: int, name: str, config: dict) -> dict:
        from datetime import datetime, timezone
        name = str(name or "").strip()
        if not name:
            raise ValueError("configuration name is required")
        row = self.db.query_one(
            "SELECT * FROM oai_templates WHERE id=? AND experiment_id=? AND archived_utc IS NULL",
            (template_id, experiment_id))
        if not row:
            raise ValueError("configuration not found")
        if self.db.query_one(
                "SELECT 1 FROM oai_templates WHERE experiment_id=? AND name=? "
                "AND id<>? AND archived_utc IS NULL",
                (experiment_id, name, template_id)):
            raise ValueError("configuration name already exists")
        now = datetime.now(timezone.utc).isoformat()
        config_json = json.dumps(config, ensure_ascii=False)
        self.db.execute(
            "UPDATE oai_templates SET name=?,config_json=?,updated_utc=? WHERE id=?",
            (name, config_json, now, template_id))
        exp = self.db.query_one("SELECT default_template_id FROM experiments WHERE experiment_id=?", (experiment_id,))
        if exp and exp.get("default_template_id") == template_id:
            self.db.execute("UPDATE experiments SET initial_oai_config=? WHERE experiment_id=?",
                            (config_json, experiment_id))
        return self.db.query_one("SELECT * FROM oai_templates WHERE id=?", (template_id,))

    def set_default_template(self, experiment_id: str, template_id: int) -> dict:
        row = self.db.query_one(
            "SELECT * FROM oai_templates WHERE id=? AND experiment_id=? AND archived_utc IS NULL",
            (template_id, experiment_id))
        if not row:
            raise ValueError("configuration not found")
        self.db.execute(
            "UPDATE experiments SET default_template_id=?,initial_oai_config=? WHERE experiment_id=?",
            (template_id, row["config_json"], experiment_id))
        return row

    def delete_template(self, experiment_id: str, template_id: int) -> None:
        from datetime import datetime, timezone
        row = self.db.query_one(
            "SELECT id FROM oai_templates WHERE id=? AND experiment_id=? AND archived_utc IS NULL",
            (template_id, experiment_id))
        if not row:
            raise ValueError("configuration not found")
        exp = self.db.query_one("SELECT default_template_id FROM experiments WHERE experiment_id=?", (experiment_id,))
        if exp and exp.get("default_template_id") == template_id:
            raise ValueError("default configuration cannot be archived")
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "UPDATE oai_templates SET archived_utc=?,updated_utc=? "
            "WHERE id=? AND experiment_id=? AND archived_utc IS NULL",
            (now, now, template_id, experiment_id))

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
        # Quiesce the phone first: a stale-context re-establishment against
        # the returning gNB crashes it (see quiesce_phone).
        quiesced = quiesce_phone(serial, True)
        if quiesced:
            time.sleep(2.0)
        try:
            result = self.oai.apply_condition(cfg, force_restart=True)
        finally:
            if quiesced:
                quiesce_phone(serial, False)
                time.sleep(8.0)

        # The gNB just restarted — the UE re-registers (usually with a NEW
        # PDU address). Re-resolve it via /shake and sync clocks before the
        # rearm below, otherwise rearm targets the stale address and fails.
        # Retried briefly: right after a restart the UE may take a few
        # seconds to complete its PDU session; each attempt is cheap.
        shake: dict = {"ok": False, "attempted": False}
        run_row = self.db.query_one(
            "SELECT run_id FROM runs WHERE experiment_id=? "
            "AND state IN ('ARMED','RUNNING') ORDER BY rowid DESC LIMIT 1",
            (experiment_id,))
        for _ in range(4):
            shake["attempted"] = True
            shake = {**shake, **shake_and_refresh(
                self.s, self.oai, self.db, experiment_id,
                run_row["run_id"] if run_row else None, n_exchanges=1)}
            if shake.get("ok"):
                break
            time.sleep(5.0)

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
        return {"config": cfg, "result": result, "shake": shake, "rearm": rearm}

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
        configuration_id = None
        configuration_name = None
        if template_id is not None:
            row = self.db.query_one(
                "SELECT id,name,config_json FROM oai_templates WHERE id=? AND experiment_id=? AND archived_utc IS NULL",
                (template_id, experiment_id))
            if not row:
                raise ValueError("configuration not found")
            configuration_id, configuration_name = row["id"], row["name"]
            initial = json.loads(row["config_json"])
        if initial is None:
            if exp.get("default_template_id"):
                row = self.db.query_one(
                    "SELECT id,name,config_json FROM oai_templates WHERE id=? AND archived_utc IS NULL",
                    (exp["default_template_id"],))
                if row:
                    configuration_id, configuration_name = row["id"], row["name"]
                    initial = json.loads(row["config_json"])
            if initial is None:
                initial = json.loads(exp["initial_oai_config"]) if exp.get("initial_oai_config") else {}
                configuration_name = "Legacy default" if initial else None

        # Resolve idle/collection timings (explicit param > global default).
        idle_s = float(idle_seconds) if idle_seconds is not None else DEFAULT_IDLE_SECONDS
        collection_s = (float(collection_seconds)
                        if collection_seconds is not None else DEFAULT_COLLECTION_SECONDS)

        # A FRESH run_id for every start — never reuse a previous run. The id
        # travels to the phone inside the sync-confirm plan; the phone persists
        # it (RunEntity) so later data exchange can match on it.
        run_id = f"{experiment_id}_r{int(time.time() * 1000)}"
        condition_id = f"{experiment_id}_default"
        # UL traffic setting from the selected template. The default is paced
        # CBR; saturation remains available only through an explicit >=100
        # Mbps value so an ordinary experiment cannot destabilize the cell.
        ul_mbps = initial.get("ulTrafficMbps") if initial else None
        try:
            ul_mbps = float(ul_mbps) if ul_mbps is not None else UL_TRAFFIC_DEFAULT_MBPS
        except (TypeError, ValueError):
            ul_mbps = UL_TRAFFIC_DEFAULT_MBPS
        plan = {
            "experimentId": experiment_id,
            "runId": run_id,
            "conditionId": condition_id,
            "environment": exp["environment"],
            "plannedStartUtc": "",  # phone uses elapsedRealtime countdown, not wall clock
            "startDelaySeconds": 0.0,
            "idleSeconds": idle_s,
            "collectionSeconds": collection_s,
            "ulTrafficMbps": ul_mbps,
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
            "configuration_id": configuration_id,
            "configuration_name": configuration_name,
            "requested_config_json": json.dumps(initial or {}, ensure_ascii=False),
            "started_utc_ms": int(time.time() * 1000),
        })
        self.db.transition(run_id, "PREPARING", "start_experiment: gNB starting + downlink loop")

        # 1. Apply the selected template's config and ALWAYS force a REAL
        #    gNB restart on experiment start — every run must begin from a
        #    known RF state (a previous experiment or template switch may
        #    have left effective parameters half-applied, and per-parameter
        #    restart:false writes give no reliable restart signal).
        #    apply_condition(force_restart=True) submits the parameters,
        #    issues ONE restart, awaits it and VERIFIES the process was
        #    replaced. The UE re-registers on its own afterwards — we never
        #    block waiting for it here.
        gnb_ready = False
        # Clear the phone modem's context BEFORE the restart (see
        # quiesce_phone) — otherwise its RRC re-establishment against the
        # returning gNB trips the get_searchspace() assert and kills the gNB.
        quiesced = quiesce_phone(serial, True)
        if quiesced:
            time.sleep(2.0)  # let the modem release the PDU session
        try:
            result = self.oai.apply_condition(initial or {}, force_restart=True)
            actual = self.oai.research_config_raw()
            if isinstance(actual, dict):
                self.db.set_json(run_id, "actual_config_json", actual)
            gnb_ready = True
        except Exception as e:
            self.db.transition(run_id, "ERROR", f"start_experiment: gNB restart failed: {e}")
            raise
        finally:
            if quiesced:
                quiesce_phone(serial, False)
                time.sleep(8.0)  # fresh registration + PDU session before shake

        # 3. Verify gNB + UE in-sync
        st = self.oai.status()
        ready = bool(st.gnb and st.gnb.running)
        try:
            ues = self.oai.research_ues()
            in_sync = bool(ues.ues) and bool(ues.collection and not ues.collection.stale)
        except Exception:
            in_sync = False

        # 4. The gNB (re)start above kicks the UE offline — re-resolve its
        #    PDU address via /shake and sync clocks when the phone is already
        #    monitoring. Best-effort here (single attempt, never raises);
        #    the DownlinkLoop keeps re-shaking every 10 s while the phone is
        #    unreachable, so the start call never blocks on the UE.
        shake = shake_and_refresh(self.s, self.oai, self.db, experiment_id, run_id, n_exchanges=1)

        # 5. Start the downlink loop (records ACKs, triggers sync-confirm on the
        #    first ACK — the phone arms itself, the PC never arms directly)
        loop = DownlinkLoop(experiment_id, serial, pc_port, self.db, self.s, self.oai, plan, run_id)
        loop.start()
        self.downlinks[experiment_id] = loop

        # 6. OAI research collectors — record per-UE ul/dl_goodput_mbps, PUSCH
        #    SNR/MCS/PRB into oai_snapshots (1 s) and scheduler events into
        #    oai_events (2 s) for THIS run, so the platform stores the gNB-side
        #    goodput alongside the phone telemetry.
        self._start_collectors(run_id, experiment_id)
        return {"ok": ready and in_sync, "gnb_started": gnb_ready, "gnb_running": ready,
                "ue_in_sync": in_sync, "config_applied": initial,
                "configuration_id": configuration_id, "configuration_name": configuration_name,
                "downlink_started": True,
                "sync_pending": True, "run_id": run_id, "shake": shake}

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
            self.db.execute("UPDATE runs SET ended_utc_ms=? WHERE run_id=?", (stop_ms, run_id))
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

    @contextmanager
    def _phone_usb(self, serial: str, pc_port: int = 8420):
        """USB-only tunnel for post-experiment data sync (inventory / per-run pull).

        Opens an ``adb forward`` on a DEDICATED port (pc_port + 2000) so it never
        fights with the detection probe (pc_port + 1000) or the live downlink
        loop (pc_port) — sharing ports made adb tear each other's tunnels down.
        """
        from .phone_channel import AdbTransport
        from .phone_detect import AGENT_PORT
        data_port = pc_port + 2000
        transport = AdbTransport()
        if serial not in transport.devices():
            raise RuntimeError("phone not attached via USB (plug in and enable debugging)")
        transport.forward(serial, data_port, AGENT_PORT)
        try:
            yield PhoneAgent(base_port=data_port, timeout=60.0)
        finally:
            try:
                transport.remove_forward(serial, data_port)
            except Exception:
                pass

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
        """Phone-side experiment/run inventory merged with this platform's state.

        USB-only (task 4): lists every experiment the phone knows (TaskStore)
        together with per-run sample summaries (Room), then flags which runs and
        experiments also exist HERE so the UI can highlight the intersection.
        """
        try:
            with self._phone_usb(serial, pc_port) as agent:
                inv = agent.data_inventory()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        platform_exps = {r["experiment_id"] for r in
                         self.db.query("SELECT experiment_id FROM experiments")}
        platform_runs = {r["run_id"]: r for r in self.db.query(
            "SELECT run_id, experiment_id, state, started_utc_ms, ended_utc_ms FROM runs")}
        platform_samples = {r["run_id"]: r["n"] for r in self.db.query(
            "SELECT run_id, COUNT(*) n FROM phone_samples GROUP BY run_id")}
        out = []
        for it in (inv.get("experiments") or []):
            eid = it.get("experimentId") or ""
            runs = []
            for r in (it.get("runs") or []):
                rid = r.get("runId") or ""
                pr = platform_runs.get(rid)
                runs.append({
                    "run_id": rid,
                    "phone_sample_count": r.get("sampleCount"),
                    "first_utc_ms": r.get("firstUtcMs"),
                    "last_utc_ms": r.get("lastUtcMs"),
                    "in_platform": pr is not None,
                    "platform_state": pr["state"] if pr else None,
                    "platform_started_ms": pr["started_utc_ms"] if pr else None,
                    "platform_sample_count": platform_samples.get(rid, 0),
                })
            collected = self.db.query_one(
                "SELECT COUNT(*) n, MAX(collected_utc_ms) last FROM collections WHERE experiment_id=?", (eid,))
            task = it.get("task") if isinstance(it.get("task"), dict) else {}
            out.append({
                "experiment_id": eid,
                "environment": task.get("environment"),
                "phone_collection_count": it.get("collectionCount"),
                "in_platform": eid in platform_exps,
                "collected_count": collected["n"] if collected else 0,
                "last_collected_ms": collected["last"] if collected else None,
                "runs": runs,
            })
        out.sort(key=lambda e: (not e["in_platform"], e["experiment_id"]))
        return {"ok": True, "serial": serial, "phone_experiments": out}

    def pull_phone_run(self, experiment_id: str, run_id: str, serial: str, hostname: str,
                       pc_port: int = 8420) -> dict:
        """Pull ONE run's data from the phone over USB and import it (task 4).

        The phone exports that run's raw CSV/JSON via ``/agent/export?runId=``;
        imported samples REPLACE any previous rows for the same run, so pulling
        twice is idempotent.
        """
        import pandas as pd
        from .manager import _PHONE_SAMPLE_DB_COLS
        dest = self.s.raw_dir / "phone" / run_id
        dest.mkdir(parents=True, exist_ok=True)
        with self._phone_usb(serial, pc_port) as agent:
            files = agent.export(dest, run_id=run_id)
            agent.mark_collected(experiment_id, hostname)
        for f in files:
            self.db.record_file(f)
        imported = {"samples": 0, "events": False, "session": False, "sync": False}
        samples_csv = dest / "phone_samples.csv"
        if samples_csv.exists():
            df = pd.read_csv(samples_csv)
            cols = [c for c in df.columns if c in _PHONE_SAMPLE_DB_COLS]
            self.db.execute("DELETE FROM phone_samples WHERE run_id=?", (run_id,))
            rows = df[cols].where(pd.notna(df[cols]), None).itertuples(index=False, name=None)
            self.db.executemany(
                f"INSERT INTO phone_samples({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
                rows)
            imported["samples"] = int(len(df))
        imported["events"] = (dest / "phone_events.csv").exists()
        imported["session"] = (dest / "phone_session.json").exists()
        imported["sync"] = (dest / "phone_sync.json").exists()
        now = int(time.time() * 1000)
        count = self.db.query_one(
            "SELECT COUNT(*) n FROM collections WHERE experiment_id=?", (experiment_id,))
        self.db.execute(
            "INSERT INTO collections(experiment_id,device_id,hostname,collected_utc_ms,files_json,count)"
            " VALUES(?,?,?,?,?,?)",
            (experiment_id, serial, hostname, now,
             json.dumps([str(f) for f in files]), (count["n"] if count else 0) + 1))
        return {"ok": True, "run_id": run_id, "files": [f.name for f in files],
                "imported": imported}

    # ---- timeline / clip --------------------------------------------------- #
    def clip_t0(self, experiment_id: str, run_id: Optional[str] = None) -> dict:
        """Time origin for the fused timeline: the FIRST pre-run clock sync
        (task 5: “把首次开始对时的时间设为 0”). Falls back to the earliest
        phone sample, then the earliest run start, all on the PC/UTC axis."""
        sql = "SELECT run_id, started_utc_ms FROM runs WHERE experiment_id=?"
        args: tuple = (experiment_id,)
        if run_id:
            sql += " AND run_id=?"
            args += (run_id,)
        runs = self.db.query(sql + " ORDER BY run_id", args)
        run_ids = [r["run_id"] for r in runs]
        ph = ",".join("?" for _ in run_ids)
        cands: list[tuple[int, str]] = []
        if run_ids:
            row = self.db.query_one(
                f"SELECT MIN(t1_ms) AS t FROM sync_anchors WHERE direction='before'"
                f" AND t1_ms IS NOT NULL AND run_id IN ({ph})", tuple(run_ids))
            if row and row["t"]:
                cands.append((int(row["t"]), "sync_before"))
            row = self.db.query_one(
                f"SELECT MIN(utc_epoch_ms) AS t FROM phone_samples WHERE utc_epoch_ms IS NOT NULL"
                f" AND run_id IN ({ph})", tuple(run_ids))
            if row and row["t"]:
                cands.append((int(row["t"]), "phone_first_sample"))
        for r in runs:
            if r.get("started_utc_ms"):
                cands.append((int(r["started_utc_ms"]), "run_started"))
        if not cands:
            raise ValueError("no timestamps to anchor the fused timeline")
        t0, src = min(cands, key=lambda c: c[0])
        return {"t0_utc_ms": t0, "t0_source": src}

    def timeline(self, experiment_id: str, run_id: Optional[str] = None) -> dict:
        sql = "SELECT run_id, state, started_utc_ms, ended_utc_ms FROM runs WHERE experiment_id=?"
        args: tuple = (experiment_id,)
        if run_id:
            sql += " AND run_id=?"
            args += (run_id,)
        runs = self.db.query(sql + " ORDER BY run_id", args)
        run_ids = [r["run_id"] for r in runs]
        rows = []
        for rid in run_ids:
            rows += self.db.query("SELECT * FROM phone_samples WHERE run_id=? ORDER BY elapsed_realtime_ns", (rid,))
        ack_sql = "SELECT * FROM experiment_acks WHERE experiment_id=?"
        ack_args: tuple = (experiment_id,)
        if run_id:
            ack_sql += " AND run_id=?"
            ack_args += (run_id,)
        acks = self.db.query(ack_sql + " ORDER BY id", ack_args)
        markers = []
        for a in acks:
            markers.append({"kind": "ack", "ms": a["pc_send_ms"], "rtt_ms": a["rtt_ms"]})
        clip_sql = "SELECT * FROM clips WHERE experiment_id=?"
        clip_args: tuple = (experiment_id,)
        if run_id:
            clip_sql += " AND run_id=?"
            clip_args += (run_id,)
        clips = self.db.query(clip_sql + " ORDER BY id DESC", clip_args)
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
                " ORDER BY fetched_utc_ms DESC LIMIT 1", (run_ids[-1],))
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
        try:
            origin = self.clip_t0(experiment_id, run_id)
        except ValueError:
            origin = {"t0_utc_ms": None, "t0_source": None}
        return {"samples": rows, "acks": acks, "clips": clips, "runs": runs,
                "gnb": gnb, "channel": channel, "cir": cir, **origin}

    def clip(self, experiment_id: str, run_id: Optional[str], start_ms: float, end_ms: float,
             label: str) -> dict:
        """Fused clip on the sync-zeroed time axis (task 5).

        ``start_ms``/``end_ms`` are milliseconds RELATIVE to the experiment's
        time origin (first pre-run clock sync — see clip_t0). The clip fuses
        phone samples + gNB snapshots + channel metrics inside the window into
        one CSV stamped ``t_s`` (seconds since the origin) and records it as a
        new copy in the clips table (re-saving never overwrites).
        """
        import pandas as pd
        from datetime import datetime, timezone
        t0 = self.clip_t0(experiment_id, run_id)["t0_utc_ms"]
        lo, hi = t0 + float(start_ms), t0 + float(end_ms)
        run_sql = " AND run_id=?" if run_id else ""
        window_args: tuple = (lo, hi, run_id) if run_id else (lo, hi)
        phone = self.db.query(
            "SELECT utc_epoch_ms AS ts, run_id, phase, battery_power_w, battery_current_now_ua,"
            " battery_voltage_mv, soc_percent, ss_rsrp_dbm, ss_sinr_db, workload_actual_mbps"
            " FROM phone_samples WHERE utc_epoch_ms IS NOT NULL AND utc_epoch_ms BETWEEN ? AND ?"
            + run_sql + " ORDER BY utc_epoch_ms", window_args)
        gnb = self.db.query(
            "SELECT fetched_utc_ms AS ts, run_id, ul_goodput_mbps, dl_goodput_mbps, pusch_snr_db,"
            " ul_mcs, n_prb FROM oai_snapshots WHERE fetched_utc_ms IS NOT NULL"
            " AND fetched_utc_ms BETWEEN ? AND ?" + run_sql + " ORDER BY fetched_utc_ms",
            window_args)
        channel = self.db.query(
            "SELECT fetched_utc_ms AS ts, run_id, rms_delay_ns, k_factor_db, tap_count, peak_db,"
            " noise_db FROM oai_channel WHERE fetched_utc_ms IS NOT NULL"
            " AND fetched_utc_ms BETWEEN ? AND ?" + run_sql + " ORDER BY fetched_utc_ms",
            window_args)

        def _frame(rows: list[dict], source: str):
            if not rows:
                return None
            df = pd.DataFrame(rows)
            df.insert(0, "t_s", ((df["ts"] - t0) / 1000.0).round(3))
            df.insert(1, "source", source)
            return df.drop(columns=["ts"])

        frames = [f for f in (_frame(phone, "phone"), _frame(gnb, "gnb"),
                              _frame(channel, "channel")) if f is not None]
        fused = pd.concat(frames, ignore_index=True, sort=False) if frames else \
            pd.DataFrame(columns=["t_s", "source"])
        out_dir = self.s.processed_dir / "clips"
        out_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{experiment_id}_{label or 'clip'}_s{int(start_ms)}_e{int(end_ms)}_{int(time.time() * 1000)}.csv"
        path = out_dir / fname
        fused.to_csv(path, index=False)
        self.db.record_file(path)
        now = datetime.now(timezone.utc).isoformat()
        clip_id = self.db.execute(
            "INSERT INTO clips(experiment_id,run_id,start_ms,end_ms,label,created_utc,output_path)"
            " VALUES(?,?,?,?,?,?,?)",
            (experiment_id, run_id, start_ms, end_ms, label, now, str(path)))
        return {"ok": True, "clip_id": clip_id, "path": str(path), "n_rows": int(len(fused)),
                "t0_utc_ms": t0}


_flows: dict[str, TaskFlow] = {}


def get_flow(settings: Settings, db: Database, oai: OaiClient) -> TaskFlow:
    key = str(settings.db_path)
    if key not in _flows:
        _flows[key] = TaskFlow(settings, db, oai)
    return _flows[key]
