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

from .collectors import (ChannelCollector, ConfigurationCollector, EventCollector,
                         SnapshotCollector, save_config_provenance)
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

RADIO_CONFIG_KEYS = (
    "frequencyMHz", "bandwidthMHz", "txGainDb", "rxGainDb",
    "puschTargetMode", "puschTargetSnrX10", "schedulerMode", "mcs", "qm", "nPrb",
)


def execution_mode(config: dict) -> str:
    mode = str(config.get("executionMode") or
               (config.get("rcChamber") or {}).get("execution_mode") or
               "REAL_HARDWARE").upper()
    return mode if mode in {"REAL_HARDWARE", "SIMULATION"} else "REAL_HARDWARE"


def radio_config(config: dict) -> dict:
    """Only hardware-applicable keys; RC/traffic metadata stay in Snapshot."""
    return {key: config[key] for key in RADIO_CONFIG_KEYS if config.get(key) is not None}


def config_diff(requested: dict, actual: dict) -> dict:
    """Compare only authoritative fields returned by the OAI config API."""
    aliases = {"schedulerMode": "ulSchedulerMode"}
    scheduler = ((actual.get("controls") or {}).get("ulScheduler") or {})
    scheduler_values = {
        "mcs": actual.get("ulManualMcs", scheduler.get("mcs")),
        "qm": scheduler.get("qm"),
        "nPrb": actual.get("ulManualPrb", scheduler.get("nPrb")),
    }
    diff = {}
    for key, wanted in radio_config(requested).items():
        got = scheduler_values.get(key, actual.get(key, actual.get(aliases.get(key, ""))))
        if isinstance(wanted, (int, float)) and isinstance(got, (int, float)):
            same = abs(float(wanted) - float(got)) < 1e-6
        else:
            same = wanted == got
        if not same:
            diff[key] = {"requested": wanted, "actual": got}
    return diff


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
        self._nettest_session_id = ""
        self._nettest_active = False
        self._nettest_attempted_phase = False
        self._nettest_thread: Optional[threading.Thread] = None
        self.last_nettest: Optional[dict] = None
        self._phase_anchor_monotonic: Optional[float] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"downlink-{self.experiment_id}")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=12)
        if self._nettest_thread and self._nettest_thread is not threading.current_thread():
            self._nettest_thread.join(timeout=12)
        self._stop_nettest()

    def _sync_nettest(self, phase: Optional[str]) -> None:
        target_mbps = float(self.plan.get("ulTrafficMbps") or 0.0)
        should_run = (phase or "").upper() == "LOADED" and target_mbps > 0.0
        if should_run and not self._nettest_attempted_phase:
            self._nettest_attempted_phase = True
            try:
                response = self.oai.nettest_start("uplink", "udp", target_mbps)
                session = response.get("session") or response.get("state") or {}
                self._nettest_session_id = str(session.get("sessionId") or "")
                self._nettest_active = bool(
                    session.get("running") or session.get("state") in {"STARTING", "RUNNING"})
                if not self._nettest_active:
                    raise RuntimeError("OAI NetworkTest did not enter STARTING or RUNNING")
                self.last_nettest = session
            except Exception as exc:
                self.last_nettest = {
                    "state": "FAILED", "errorCode": getattr(exc, "code", ""),
                    "error": str(exc),
                }
                raise
        elif should_run and self._nettest_active:
            self.last_nettest = self.oai.nettest_status().get("session") or {}
            self._nettest_active = bool(
                self.last_nettest.get("running") or
                self.last_nettest.get("state") in {"STARTING", "RUNNING"})
        elif not should_run:
            if self._nettest_active:
                self._stop_nettest()
            self._nettest_attempted_phase = False

    def _planned_phase(self) -> Optional[str]:
        """Return the phone phase from the synchronized local plan clock.

        NetworkTest control must not depend on /agent/status while the uplink
        itself is congested. Sync-confirm arms the phone and establishes this
        anchor at the same instant, so the PC can start/stop traffic from the
        immutable phase plan even when phone HTTP responses are delayed.
        """
        if self._phase_anchor_monotonic is None:
            return None
        elapsed = time.monotonic() - self._phase_anchor_monotonic
        elapsed -= float(self.plan.get("startDelaySeconds") or 0.0)
        if elapsed < 0:
            return None
        for phase in self.plan.get("phases") or []:
            name = str(phase.get("name") or "")
            duration = float(phase.get("durationSeconds") or 0.0)
            if duration <= 0 or elapsed < duration:
                return name
            elapsed -= duration
        return None

    def _nettest_window(self) -> Optional[tuple[float, float]]:
        """Delay from sync-confirm and duration of the first loaded phase."""
        delay = float(self.plan.get("startDelaySeconds") or 0.0)
        for phase in self.plan.get("phases") or []:
            duration = float(phase.get("durationSeconds") or 0.0)
            if str(phase.get("name") or "").upper() == "LOADED":
                return (delay, duration) if duration > 0 else None
            if duration <= 0:
                return None
            delay += duration
        return None

    def _start_nettest_schedule(self) -> None:
        window = self._nettest_window()
        if window is None or float(self.plan.get("ulTrafficMbps") or 0.0) <= 0.0:
            return

        def run() -> None:
            delay, duration = window
            if self._stop.wait(delay):
                return
            try:
                self._sync_nettest("LOADED")
            except Exception:
                return
            self._stop.wait(duration)
            self._sync_nettest("IDLE")

        self._nettest_thread = threading.Thread(
            target=run, daemon=True, name=f"nettest-{self.experiment_id}")
        self._nettest_thread.start()

    def _stop_nettest(self) -> None:
        if not self._nettest_active:
            return
        try:
            current = (self.oai.nettest_status().get("session") or {})
            if not self._nettest_session_id or current.get("sessionId") == self._nettest_session_id:
                self.last_nettest = self.oai.nettest_stop()
        except Exception:
            pass
        finally:
            self._nettest_active = False
            self._nettest_session_id = ""

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
        ues = self.oai.telemetry_ues()
        if not ues.collection or ues.collection.stale or not any(
                ue.ageSeconds is not None and ue.ageSeconds <= 5.0 for ue in ues.ues):
            raise RuntimeError("OAI telemetry is stale")
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
            self._phase_anchor_monotonic = time.monotonic()
            # record the sync_confirm ack row (carries the gNB data timestamp)
            self.db.execute(
                "INSERT INTO experiment_acks(experiment_id,run_id,seq,direction,pc_send_ms,phone_recv_ms,phone_send_ms,pc_recv_ms,rtt_ms,gnb_data_timestamp_ms) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (self.experiment_id, self.run_id, -1, "sync_confirm", pc_ts,
                 r.get("phone_timestamp_ms"), None, None, None, gnb_ts))
            self.sync_confirmed = True
            self.last_sync_confirm = r
            self._start_nettest_schedule()
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
        # OAI telemetry collectors per experiment (SnapshotCollector writes
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
        cfg = ConfigurationCollector(run_id, self.s, self.db, self.oai, interval_s=5.0)
        environment_row = self.db.query_one(
            "SELECT environment FROM experiments WHERE experiment_id=?",
            (experiment_id,))
        is_rc = str((environment_row or {}).get("environment") or "").upper() == "RC"
        snap.start()
        ev.start()
        cfg.start()
        collectors = [snap, ev, cfg]
        # CIR is an RC-only observable. Polling the scope during an AC power
        # run needlessly re-opens its single WebSocket producer and can crash
        # the gNB webscope process under sustained reconnect churn.
        if is_rc:
            ch = ChannelCollector(run_id, self.s, self.db, self.oai, interval_s=1.0)
            ch.start()
            collectors.append(ch)
        self.collectors[experiment_id] = collectors

    def _stop_collectors(self, experiment_id: str) -> None:
        for c in self.collectors.pop(experiment_id, []):
            c.stop()
            c.join(timeout=5)

    def discard_run(self, run_id: str) -> dict:
        """Delete a Run that never reached the phone, including its indexed data."""
        path_columns = (
            ("oai_snapshots", "raw_json_path"), ("oai_events", "raw_json_path"),
            ("oai_configuration_events", "raw_json_path"),
            ("oai_channel", "raw_json_path"), ("oai_config", "config_json_path"),
            ("clips", "output_path"), ("rc_samples", "raw_json_path"),
        )
        paths: set[str] = set()
        for table, column in path_columns:
            paths.update(r[column] for r in self.db.query(
                f"SELECT {column} FROM {table} WHERE run_id=? AND {column} IS NOT NULL", (run_id,)))
        # Imported/processed phone files are indexed separately and include
        # the Run ID in their path even though the files table has no run_id.
        paths.update(r["file_path"] for r in self.db.query("SELECT file_path FROM files")
                     if run_id in Path(r["file_path"]).parts or run_id in Path(r["file_path"]).name)

        for table in ("phone_samples", "oai_snapshots", "oai_events", "oai_channel", "oai_config", "oai_configuration_events",
                      "sync_anchors", "run_transitions", "clips", "rc_samples"):
            self.db.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
        self.db.execute("DELETE FROM experiment_acks WHERE run_id=?", (run_id,))
        self.db.execute("DELETE FROM runs WHERE run_id=?", (run_id,))

        root = self.s.data_dir.resolve()
        removed_files = 0
        for value in paths:
            still_referenced = any(self.db.query_one(
                f"SELECT 1 FROM {table} WHERE {column}=? LIMIT 1", (value,))
                for table, column in path_columns)
            if still_referenced:
                continue
            self.db.execute("DELETE FROM files WHERE file_path=?", (value,))
            try:
                path = Path(value).resolve()
                path.relative_to(root)
            except (OSError, ValueError):
                continue
            try:
                if path.is_file():
                    path.unlink()
                    removed_files += 1
            except OSError:
                pass
        return {"discarded": True, "discard_reason": "sync_confirm not completed",
                "removed_files": removed_files}

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
        used = self.db.query_one("SELECT COUNT(*) n FROM runs WHERE configuration_id=?", (template_id,))
        if used and used["n"]:
            # A used Configuration is immutable. Editing creates the next
            # version and archives only the editable catalog entry; historical
            # Runs retain both the old id/version and their frozen Snapshot.
            new_id = self.db.execute(
                "INSERT INTO oai_templates(experiment_id,name,config_json,created_utc,updated_utc,version,supersedes_id) "
                "VALUES(?,?,?,?,?,?,?)",
                (experiment_id, name, config_json, now, now, int(row.get("version") or 1) + 1, template_id))
            self.db.execute("UPDATE oai_templates SET archived_utc=?,updated_utc=? WHERE id=?",
                            (now, now, template_id))
            exp = self.db.query_one("SELECT default_template_id FROM experiments WHERE experiment_id=?", (experiment_id,))
            if exp and exp.get("default_template_id") == template_id:
                self.db.execute(
                    "UPDATE experiments SET default_template_id=?,initial_oai_config=? WHERE experiment_id=?",
                    (new_id, config_json, experiment_id))
            return self.db.query_one("SELECT * FROM oai_templates WHERE id=?", (new_id,))
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
            request_prefix = (run["run_id"] if run else
                              f"{experiment_id}-config-{template_id}-{int(time.time() * 1000)}")
            result = self.oai.apply_condition(
                radio_config(cfg), force_restart=True, request_prefix=request_prefix)
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
        actual_raw = self.oai.telemetry_config_raw()
        actual = actual_raw if isinstance(actual_raw, dict) else {}
        differences = config_diff(cfg, actual)
        verified_ms = int(time.time() * 1000)
        status = "VERIFIED" if actual and not differences else ("DIFFERENT" if actual else "UNAVAILABLE")
        self.db.execute(
            "INSERT OR REPLACE INTO configuration_apply_state("
            "singleton_id,experiment_id,configuration_id,configuration_version,configuration_name,"
            "requested_config_json,actual_config_json,diff_json,status,verified_utc_ms)"
            " VALUES(1,?,?,?,?,?,?,?,?,?)",
            (experiment_id, template_id, int(row.get("version") or 1), row["name"],
             json.dumps(cfg, ensure_ascii=False), json.dumps(actual, ensure_ascii=False),
             json.dumps(differences, ensure_ascii=False), status, verified_ms))
        return {"config": cfg, "actual": actual, "diff": differences,
                "verification_status": status, "verified_utc_ms": verified_ms,
                "result": result, "shake": shake, "rearm": rearm}

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
        configuration_version = None
        configuration_name = None
        if template_id is not None:
            row = self.db.query_one(
                "SELECT id,name,config_json,version FROM oai_templates WHERE id=? AND experiment_id=? AND archived_utc IS NULL",
                (template_id, experiment_id))
            if not row:
                raise ValueError("configuration not found")
            configuration_id, configuration_version, configuration_name = row["id"], row.get("version") or 1, row["name"]
            initial = json.loads(row["config_json"])
        if initial is None:
            if exp.get("default_template_id"):
                row = self.db.query_one(
                    "SELECT id,name,config_json,version FROM oai_templates WHERE id=? AND archived_utc IS NULL",
                    (exp["default_template_id"],))
                if row:
                    configuration_id, configuration_version, configuration_name = row["id"], row.get("version") or 1, row["name"]
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
        if exp["environment"] == "RC":
            # RC measurement windows are owned exclusively by RcCampaign.
            # Arm into IDLE; do not run an unrelated AC-style loaded window
            # before Sample 1.
            plan["idleSeconds"] = 0.0
            plan["collectionSeconds"] = 0.0
            plan["phases"] = [{"name": "idle", "durationSeconds": 0.0}]
        mode = execution_mode(initial or {})
        snapshot = json.loads(json.dumps(initial or {}))
        snapshot.setdefault("executionMode", mode)
        snapshot["_provenance"] = {
            "configuration_id": configuration_id,
            "configuration_version": configuration_version,
            "configuration_name": configuration_name,
            "snapshot_scope": "execution-time requested conditions",
        }
        if exp["environment"] == "RC":
            chamber = snapshot.setdefault("rcChamber", {})
            chamber.setdefault("processing_algorithm", "standard-ifft-local-peak")
            chamber.setdefault("processing_version", "3.0")
            chamber.setdefault("noise_method", "median of per-tap calibration medians, strongest 10% excluded")

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
            "configuration_version": configuration_version,
            "configuration_name": configuration_name,
            "requested_config_json": json.dumps(initial or {}, ensure_ascii=False),
            "configuration_snapshot_json": json.dumps(snapshot, ensure_ascii=False),
            "execution_mode": mode, "simulation": 1 if mode == "SIMULATION" else 0,
            "started_utc_ms": int(time.time() * 1000),
        })
        self.db.transition(run_id, "PREPARING", "start_experiment: gNB starting + downlink loop")

        # Starting a Run owns gNB initialization. The selected Configuration is
        # submitted and a real restart is forced even when the gNB is currently
        # stopped or already happens to expose the same values. UE attach and
        # clock synchronization intentionally happen after this restart.
        quiesced = quiesce_phone(serial, True)
        if quiesced:
            time.sleep(2.0)
        try:
            self.oai.apply_condition(
                radio_config(initial or {}), force_restart=True, request_prefix=run_id)
            actual_raw = self.oai.telemetry_config_raw()
            actual = actual_raw if isinstance(actual_raw, dict) else {}
        except Exception as e:
            self.db.transition(run_id, "ERROR", f"start_experiment: gNB restart failed: {e}")
            raise
        finally:
            if quiesced:
                quiesce_phone(serial, False)
                time.sleep(8.0)
        differences = config_diff(initial or {}, actual)
        verified_ms = int(time.time() * 1000)
        apply_status = "VERIFIED" if actual and not differences else ("DIFFERENT" if actual else "UNAVAILABLE")
        self.db.execute(
            "INSERT OR REPLACE INTO configuration_apply_state("
            "singleton_id,experiment_id,configuration_id,configuration_version,configuration_name,"
            "requested_config_json,actual_config_json,diff_json,status,verified_utc_ms)"
            " VALUES(1,?,?,?,?,?,?,?,?,?)",
            (experiment_id, configuration_id, int(configuration_version or 1), configuration_name,
             json.dumps(initial or {}, ensure_ascii=False), json.dumps(actual, ensure_ascii=False),
             json.dumps(differences, ensure_ascii=False), apply_status, verified_ms))
        if differences or not actual:
            self.db.transition(run_id, "ERROR", f"Configuration verification {apply_status}: {differences}")
            raise ValueError(f"Configuration verification {apply_status}: {differences}")
        self.db.set_json(run_id, "actual_config_json", actual)
        self.db.execute(
            "UPDATE runs SET applied_configuration_id=?,applied_config_snapshot_json=? WHERE run_id=?",
            (configuration_id, json.dumps(actual, ensure_ascii=False), run_id))
        gnb_ready = True

        # 3. Verify gNB + UE in-sync
        st = self.oai.status()
        ready = bool(st.gnb and st.gnb.running)
        try:
            in_sync = bool(self.oai.fresh_ues())
        except Exception:
            in_sync = False

        # 4. The gNB (re)start above kicks the UE offline — re-resolve its
        #    PDU address via /shake and sync clocks when the phone is already
        #    monitoring. Best-effort here (single attempt, never raises);
        #    the DownlinkLoop keeps re-shaking every 10 s while the phone is
        #    unreachable, so the start call never blocks on the UE.
        shake = shake_and_refresh(self.s, self.oai, self.db, experiment_id, run_id, n_exchanges=1)
        ue_ip = shake.get("ue_ip") if isinstance(shake, dict) else None
        if ue_ip:
            try:
                agent = PhoneAgent(base_url=f"http://{ue_ip}:8420")
                phone_status = agent.status()
                stale_run = phone_status.get("runId") not in {None, "", run_id}
                if stale_run:
                    agent.stop_task()
                    time.sleep(1.0)
                    phone_status = agent.status()
                if not phone_status.get("monitoring"):
                    agent.start_task(experiment_id)
                    shake = shake_and_refresh(
                        self.s, self.oai, self.db, experiment_id, run_id,
                        n_exchanges=1)
            except Exception:
                # DownlinkLoop will keep retrying/re-shaking if the UE briefly
                # disappears while its stale task is being reset.
                pass

        # 5. Start the downlink loop (records ACKs, triggers sync-confirm on the
        #    first ACK — the phone arms itself, the PC never arms directly)
        loop = DownlinkLoop(experiment_id, serial, pc_port, self.db, self.s, self.oai, plan, run_id)
        loop.start()
        self.downlinks[experiment_id] = loop

        # 6. OAI telemetry collectors — record per-UE ul/dl_goodput_mbps, PUSCH
        #    SNR/MCS/PRB into oai_snapshots (1 s) and scheduler events into
        #    oai_events (2 s) for THIS run, so the platform stores the gNB-side
        #    goodput alongside the phone telemetry.
        self._start_collectors(run_id, experiment_id)
        return {"ok": True, "gnb_started": gnb_ready, "gnb_running": ready,
                "ue_in_sync": in_sync, "config_applied": initial,
                "configuration_id": configuration_id, "configuration_name": configuration_name,
                "downlink_started": True,
                "sync_pending": True, "run_id": run_id, "phase_plan": plan,
                "shake": shake}

    def stop_experiment(self, experiment_id: str, serial: str = "53616213",
                        pc_port: int = 8420, requested_run_id: Optional[str] = None) -> dict:
        """Complete stop: downlink loop + phone stop_task + gNB stop + run transition.

        Both stop entry points (by experiment_id and by run_id) route here
        for the full teardown — the phone
        receives the stop command (records its stop timestamp) and the gNB
        is shut down, not just the downlink loop.
        """
        loop = self.downlinks.pop(experiment_id, None)
        run_id = loop.run_id if loop else requested_run_id
        if loop:
            loop.stop()
        # Stop the OAI telemetry collectors FIRST (before the gNB goes down) so
        # the last goodput samples are flushed with a reachable API.
        self._stop_collectors(experiment_id)
        if run_id is None:
            # Loop lost (e.g. backend restarted mid-run): fall back to the
            # experiment's active run row so the run still gets marked STOPPED
            # and the dashboard switches back to the start button.
            row = self.db.query_one(
                "SELECT run_id FROM runs WHERE experiment_id=? "
                "AND state IN ('PREPARING','WAITING_GNB','SYNCING_PHONE','ARMED','PHONE_OFFLINE',"
                "'RUNNING','WAITING_PHONE_RETURN','IMPORTING','ALIGNING') "
                "ORDER BY rowid DESC LIMIT 1",
                (experiment_id,))
            run_id = row["run_id"] if row else None
        synchronized = bool(run_id and self.db.query_one(
            "SELECT 1 FROM experiment_acks WHERE run_id=? AND direction='sync_confirm' "
            "UNION SELECT 1 FROM run_transitions WHERE run_id=? AND to_state='ARMED' LIMIT 1",
            (run_id, run_id)))
        stop_ms = int(time.time() * 1000)
        # Post-state provenance (config snapshot after the run).
        if run_id and synchronized:
            try:
                save_config_provenance(run_id, "after", self.s, self.db, self.oai)
            except Exception:
                pass

        # 1. PC stop ACK row
        if synchronized:
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

        # 3. Stop the gNB. Local teardown still completes if infrastructure
        # stopping fails, but the result and Run transition retain that fact.
        gnb_stop = {"ok": False, "error": "gNB stop was not attempted"}
        try:
            stop = self.oai.gnb_service(
                "stop", request_id=self.oai.new_request_id(run_id or experiment_id, "stop"))
            rid = self.oai.extract_request_id(stop)
            if rid:
                progress = self.oai.wait_for_restart(rid, timeout_s=120.0)
                if progress.failed:
                    raise RuntimeError(progress.error or progress.message or "gNB stop failed")
                if not progress.done:
                    raise RuntimeError(
                        f"gNB stop did not complete: phase={progress.phase or 'unknown'}")
                gnb_stop = {"ok": True, "response": stop, "progress": progress.model_dump()}
            elif stop.get("ok") is True:
                gnb_stop = {"ok": True, "response": stop}
            else:
                raise RuntimeError("gNB stop did not return ok=true")
        except Exception as exc:
            gnb_stop = {"ok": False, "error": str(exc)}
        if run_id:
            try:
                ConfigurationCollector(run_id, self.s, self.db, self.oai).collect_once()
            except Exception:
                pass

        # 4. Mark the run STOPPED
        discarded = False
        discard_reason = None
        removed_files = 0
        if run_id and synchronized:
            self.db.execute("UPDATE runs SET ended_utc_ms=? WHERE run_id=?", (stop_ms, run_id))
            note = ("stopped by user" if gnb_stop["ok"] else
                    f"stopped locally; gNB stop failed: {gnb_stop['error']}")
            self.db.transition(run_id, "STOPPED", note, utc_ms=stop_ms)
        elif run_id:
            result = self.discard_run(run_id)
            discarded = True
            discard_reason = result["discard_reason"]
            removed_files = result["removed_files"]
        return {"ok": True, "pc_stop_ms": stop_ms, "phone_stop_ms": phone_stop_ms,
                "gnb_stop": gnb_stop,
                "run_id": run_id, "discarded": discarded, "discard_reason": discard_reason,
                "removed_files": removed_files}

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
        with self._phone_usb(serial, pc_port) as agent:
            return agent.push_task(task)

    def list_phone_tasks(self, serial: str, pc_port: int = 8420) -> dict:
        with self._phone_usb(serial, pc_port) as agent:
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
        with self._phone_usb(serial, pc_port) as agent:
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
        seen_experiments: set[str] = set()
        seen_runs: set[str] = set()
        for it in (inv.get("experiments") or []):
            eid = it.get("experimentId") or ""
            seen_experiments.add(eid)
            runs = []
            for r in (it.get("runs") or []):
                rid = r.get("runId") or ""
                seen_runs.add(rid)
                pr = platform_runs.get(rid)
                conflict = bool(pr and pr.get("experiment_id") != eid)
                phone_count = r.get("sampleCount")
                platform_count = platform_samples.get(rid, 0)
                runs.append({
                    "run_id": rid,
                    "phone_sample_count": phone_count,
                    "first_utc_ms": r.get("firstUtcMs"),
                    "last_utc_ms": r.get("lastUtcMs"),
                    "in_platform": pr is not None,
                    "platform_state": pr["state"] if pr else None,
                    "platform_started_ms": pr["started_utc_ms"] if pr else None,
                    "platform_sample_count": platform_count,
                    "reconciliation": ("IDENTITY_CONFLICT" if conflict else
                                       "BOTH_MATCH" if pr and phone_count == platform_count else
                                       "BOTH_DATA_DIFFERS" if pr else "PHONE_ONLY"),
                })
            collected = self.db.query_one(
                "SELECT COUNT(*) n, MAX(collected_utc_ms) last FROM collections WHERE experiment_id=?", (eid,))
            task = it.get("task") if isinstance(it.get("task"), dict) else {}
            out.append({
                "experiment_id": eid,
                "environment": task.get("environment"),
                "phone_collection_count": it.get("collectionCount"),
                "in_platform": eid in platform_exps,
                "reconciliation": "BOTH" if eid in platform_exps else "PHONE_ONLY",
                "collected_count": collected["n"] if collected else 0,
                "last_collected_ms": collected["last"] if collected else None,
                "runs": runs,
            })
        # Reconciliation is a union, not a phone-filtered view. Runs that exist
        # only in platform history must stay visible so an operator can tell
        # "not on phone" from "not loaded yet" without guessing.
        for experiment in self.db.query("SELECT experiment_id,environment FROM experiments ORDER BY experiment_id"):
            eid = experiment["experiment_id"]
            if eid in seen_experiments:
                target = next(item for item in out if item["experiment_id"] == eid)
            else:
                target = {"experiment_id": eid, "environment": experiment.get("environment"),
                          "phone_collection_count": None, "in_platform": True,
                          "reconciliation": "PLATFORM_ONLY", "collected_count": 0,
                          "last_collected_ms": None, "runs": []}
                out.append(target)
            for rid, pr in platform_runs.items():
                if pr.get("experiment_id") != eid or rid in seen_runs:
                    continue
                target["runs"].append({"run_id": rid, "phone_sample_count": None,
                    "first_utc_ms": None, "last_utc_ms": None, "in_platform": True,
                    "platform_state": pr.get("state"),
                    "platform_started_ms": pr.get("started_utc_ms"),
                    "platform_sample_count": platform_samples.get(rid, 0),
                    "reconciliation": "PLATFORM_ONLY"})
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
        sql = "SELECT run_id, started_utc_ms, time_origin_type, time_origin_utc_ms FROM runs WHERE experiment_id=?"
        args: tuple = (experiment_id,)
        if run_id:
            sql += " AND run_id=?"
            args += (run_id,)
        runs = self.db.query(sql + " ORDER BY run_id", args)
        if run_id and runs and runs[0].get("time_origin_utc_ms"):
            return {"t0_utc_ms": int(runs[0]["time_origin_utc_ms"]),
                    "t0_source": runs[0].get("time_origin_type") or "persisted"}
        run_ids = [r["run_id"] for r in runs]
        ph = ",".join("?" for _ in run_ids)
        if run_ids:
            row = self.db.query_one(
                f"SELECT MIN(t1_ms) AS t FROM sync_anchors WHERE direction='before'"
                f" AND t1_ms IS NOT NULL AND run_id IN ({ph})", tuple(run_ids))
            if row and row["t"]:
                result = {"t0_utc_ms": int(row["t"]), "t0_source": "sync_before"}
                if run_id:
                    self.db.execute("UPDATE runs SET time_origin_type=?,time_origin_utc_ms=? WHERE run_id=?",
                                    (result["t0_source"], result["t0_utc_ms"], run_id))
                return result
            row = self.db.query_one(
                f"SELECT MIN(utc_epoch_ms) AS t FROM phone_samples WHERE utc_epoch_ms IS NOT NULL"
                f" AND run_id IN ({ph})", tuple(run_ids))
            if row and row["t"]:
                result = {"t0_utc_ms": int(row["t"]), "t0_source": "phone_first_sample"}
                if run_id:
                    self.db.execute("UPDATE runs SET time_origin_type=?,time_origin_utc_ms=? WHERE run_id=?",
                                    (result["t0_source"], result["t0_utc_ms"], run_id))
                return result
        for r in runs:
            if r.get("started_utc_ms"):
                result = {"t0_utc_ms": int(r["started_utc_ms"]), "t0_source": "run_started"}
                if run_id:
                    self.db.execute("UPDATE runs SET time_origin_type=?,time_origin_utc_ms=? WHERE run_id=?",
                                    (result["t0_source"], result["t0_utc_ms"], run_id))
                return result
        raise ValueError("no timestamps to anchor the fused timeline")

    def timeline(self, experiment_id: str, run_id: Optional[str] = None,
                 include_channel: bool = False) -> dict:
        experiment = self.db.query_one(
            "SELECT environment FROM experiments WHERE experiment_id=?", (experiment_id,))
        if not experiment:
            raise ValueError("experiment not found")
        environment = experiment["environment"]
        load_channel = environment == "RC" or include_channel
        sql = ("SELECT run_id,experiment_id,state,started_utc_ms,ended_utc_ms,quality_status,"
               "configuration_id,configuration_version,configuration_name,configuration_snapshot_json,"
               "execution_mode,simulation,time_origin_type,time_origin_utc_ms,alignment_json "
               "FROM runs WHERE experiment_id=?")
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
        for clip_row in clips:
            clip_row["segments"] = self.db.query(
                "SELECT * FROM clip_segments WHERE clip_id=? ORDER BY segment_order", (clip_row["id"],))
        # gNB-side research snapshots (ul/dl_goodput_mbps recorded by the
        # SnapshotCollector during the run), UTC-stamped for alignment with
        # the phone samples (phone_samples.utc_epoch_ms).
        gnb = []
        for rid in run_ids:
            gnb += self.db.query(
                "SELECT run_id, fetched_utc_ms, ts_utc, rnti, ul_goodput_mbps, dl_goodput_mbps,"
                " pusch_snr_db, ul_mcs, n_prb, ul_bler, dl_bler,"
                " ul_harq_initial_tx_delta,ul_harq_retransmission_delta,ul_harq_retransmission_ratio,"
                " dl_harq_initial_tx_delta,dl_harq_retransmission_delta,dl_harq_retransmission_ratio,"
                " collection_stale"
                " FROM oai_snapshots WHERE run_id=? ORDER BY fetched_utc_ms", (rid,))
        configuration_events = []
        for rid in run_ids:
            for event in self.db.query(
                    "SELECT run_id,request_id,ts_utc,event_json FROM oai_configuration_events "
                    "WHERE run_id=? ORDER BY id", (rid,)):
                try:
                    event["event"] = json.loads(event.pop("event_json"))
                except (TypeError, json.JSONDecodeError):
                    event["event"] = None
                configuration_events.append(event)
        # CIR multipath metrics time-series (oai_channel table).
        channel = []
        if load_channel:
            for rid in run_ids:
                channel += self.db.query(
                    "SELECT run_id, fetched_utc_ms, ts_utc, rms_delay_ns, k_factor_db,"
                    " tap_count, peak_db, noise_db, mean_delay_ns"
                    " FROM oai_channel WHERE run_id=? ORDER BY fetched_utc_ms", (rid,))
        # Latest CIR power-delay profile (|h(tau)|^2 in dB) for the PDP chart.
        cir = None
        if load_channel and run_ids:
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
        rc_samples = []
        if environment == "RC":
            for rid in run_ids:
                rc_samples += self.db.query(
                    "SELECT * FROM rc_samples WHERE run_id=? ORDER BY sample_index", (rid,))
        for sample in rc_samples:
            for key in ("gnb_summary", "analytics_json"):
                if sample.get(key):
                    try:
                        sample[key.removesuffix("_json")] = json.loads(sample[key])
                    except (TypeError, json.JSONDecodeError):
                        pass
            # A selected RC Sample needs its own PDP, not the latest arbitrary
            # CIR frame from the Run.
            raw_path = Path(sample.get("raw_json_path") or "")
            if raw_path.exists():
                try:
                    raw_sample = json.loads(raw_path.read_text(encoding="utf-8"))
                    raw_cir = raw_sample.get("cir") or {}
                    powers = []
                    re = raw_cir.get("cirRe") or []
                    im = raw_cir.get("cirIm") or []
                    n = min(len(re), len(im))
                    step = max(1, n // 512)
                    dt = float(raw_cir.get("dtNs") or 8.138)
                    sample["pdp_dt_ns"] = dt
                    full_powers = [10.0 * math.log10(
                        max(re[i] * re[i] + im[i] * im[i], 1e-30)) for i in range(n)]
                    for i in range(0, n, step):
                        power = max(full_powers[i:min(n, i + step)])
                        powers.append({"tau_ns": round(i * dt, 3),
                                       "power_db": round(power, 3)})
                    sample["pdp"] = powers
                    analysis = (sample.get("analytics") or {}).get("channel") or {}
                    if not analysis.get("analysis_delay_window_ns") and full_powers:
                        peak = max(full_powers)
                        threshold = analysis.get("detection_threshold_db")
                        cutoff = float(threshold) if threshold is not None else peak - 30.0
                        significant = [i for i, power in enumerate(full_powers[:max(1, n // 2)])
                                       if power >= cutoff]
                        if significant:
                            measured_end = significant[-1] * dt
                            padded = min((n - 1) * dt / 2,
                                         measured_end + max(4 * dt, measured_end * 0.15))
                            nice_step = max(10.0, 10 ** math.floor(
                                math.log10(max(padded, 1.0))) / 2.0)
                            sample["display_delay_window_ns"] = {
                                "start_ns": 0.0,
                                "end_ns": math.ceil(padded / nice_step) * nice_step,
                                "source": ("AUTO_MEASURED_ENERGY_ENVELOPE · detection threshold"
                                           if threshold is not None else
                                           "AUTO_MEASURED_ENERGY_ENVELOPE · peak - 30 dB fallback"),
                            }
                except Exception:
                    sample["pdp"] = None
        try:
            origin = self.clip_t0(experiment_id, run_id)
        except ValueError:
            origin = {"t0_utc_ms": None, "t0_source": None}
        timestamps = ([r.get("started_utc_ms") for r in runs] +
                      [r.get("ended_utc_ms") for r in runs] +
                      [r.get("utc_epoch_ms") for r in rows] +
                      [r.get("fetched_utc_ms") for r in gnb] +
                      [r.get("fetched_utc_ms") for r in channel])
        valid_times = [int(value) for value in timestamps if value is not None]
        master = {"start_utc_ms": min(valid_times) if valid_times else origin.get("t0_utc_ms"),
                  "end_utc_ms": max(valid_times) if valid_times else origin.get("t0_utc_ms")}
        sources = {}
        for name, values in (("phone", [r.get("utc_epoch_ms") for r in rows]),
                             ("gnb", [r.get("fetched_utc_ms") for r in gnb]),
                             ("cir", [r.get("fetched_utc_ms") for r in channel]),
                             ("rc", [r.get("started_utc_ms") for r in rc_samples])):
            present = [int(value) for value in values if value is not None]
            sources[name] = {"status": "ALIGNED" if present else "UNAVAILABLE",
                             "count": len(present), "first_utc_ms": min(present) if present else None,
                             "last_utc_ms": max(present) if present else None,
                             "raw_offset_ms": ((min(present) - master["start_utc_ms"])
                                               if present and master["start_utc_ms"] else None),
                             "applied_correction_ms": 0}
        events = []
        for rid in run_ids:
            events += [{"run_id": rid, "timestamp_utc_ms": row["utc_ms"],
                        "kind": "RUN_STATE", "label": row["to_state"], "detail": row.get("note")}
                       for row in self.db.query(
                           "SELECT utc_ms,to_state,note FROM run_transitions WHERE run_id=? ORDER BY utc_ms", (rid,))]
        for sample in rc_samples:
            events += [
                {"run_id": sample.get("run_id"), "timestamp_utc_ms": sample.get("started_utc_ms"),
                 "kind": "RC_SAMPLE_START", "label": f"Sample {sample.get('sample_index')} measurement start"},
                {"run_id": sample.get("run_id"), "timestamp_utc_ms": sample.get("ended_utc_ms"),
                 "kind": "RC_SAMPLE_COMPLETE", "label": f"Sample {sample.get('sample_index')} complete"},
            ]
        return {"environment": environment, "samples": rows, "acks": acks, "clips": clips, "runs": runs,
                "gnb": gnb, "channel": channel, "cir": cir, "rc_samples": rc_samples,
                "configuration_events": configuration_events,
                "events": [event for event in events if event.get("timestamp_utc_ms")],
                "master": master, "alignment": sources, **origin}

    def save_clip(self, experiment_id: str, run_id: str, name: str,
                  segments: list[dict], clip_id: Optional[int] = None) -> dict:
        """Persist one non-destructive, ordered, same-Run Clip composition."""
        import pandas as pd
        from datetime import datetime, timezone
        run = self.db.get_run(run_id)
        if not run or run["experiment_id"] != experiment_id:
            raise ValueError("Run not found in this Experiment")
        name = str(name or "").strip()
        if not name:
            raise ValueError("Clip name is required")
        if not segments:
            raise ValueError("Clip needs at least one Segment")
        t0_meta = self.clip_t0(experiment_id, run_id)
        t0 = int(t0_meta["t0_utc_ms"])
        normalized = []
        for order, segment in enumerate(segments):
            source_run = segment.get("source_run_id") or run_id
            if source_run != run_id:
                raise ValueError("Cross-Run Clips are not supported")
            start = float(segment.get("source_start_relative_ms", segment.get("start_ms", 0)))
            end = float(segment.get("source_end_relative_ms", segment.get("end_ms", 0)))
            if not math.isfinite(start) or not math.isfinite(end) or end <= start:
                raise ValueError(f"Segment {order + 1} has an invalid time range")
            normalized.append({"source_run_id": run_id, "start_ms": start, "end_ms": end,
                               "label": str(segment.get("label") or ""), "order": order})

        def rows_for(table: str, timestamp: str, columns: str, lo: float, hi: float) -> list[dict]:
            return self.db.query(
                f"SELECT {timestamp} AS ts,run_id,{columns} FROM {table} "
                f"WHERE run_id=? AND {timestamp} BETWEEN ? AND ? ORDER BY {timestamp}",
                (run_id, lo, hi))

        frames = []
        clip_cursor_s = 0.0
        for segment in normalized:
            lo, hi = t0 + segment["start_ms"], t0 + segment["end_ms"]
            sources = (
                ("phone", rows_for("phone_samples", "utc_epoch_ms",
                    "phase,battery_power_w,battery_current_now_ua,battery_voltage_mv,soc_percent,"
                    "ss_rsrp_dbm,ss_sinr_db,workload_actual_mbps", lo, hi)),
                ("gnb", rows_for("oai_snapshots", "fetched_utc_ms",
                    "ul_goodput_mbps,dl_goodput_mbps,pusch_snr_db,ul_mcs,n_prb,ul_bler,dl_bler,"
                    "ul_harq_retransmission_delta,dl_harq_retransmission_delta", lo, hi)),
                ("channel", rows_for("oai_channel", "fetched_utc_ms",
                    "rms_delay_ns,k_factor_db,tap_count,peak_db,noise_db", lo, hi)),
            )
            for source, rows in sources:
                if not rows:
                    continue
                frame = pd.DataFrame(rows)
                frame.insert(0, "clip_t_s", (clip_cursor_s + (frame["ts"] - lo) / 1000.0).round(3))
                frame.insert(1, "source_t_s", ((frame["ts"] - t0) / 1000.0).round(3))
                frame.insert(2, "segment_order", segment["order"] + 1)
                frame.insert(3, "segment_label", segment["label"])
                frame.insert(4, "source", source)
                frames.append(frame.drop(columns=["ts"]))
            clip_cursor_s += (segment["end_ms"] - segment["start_ms"]) / 1000.0
        fused = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame(
            columns=["clip_t_s", "source_t_s", "segment_order", "segment_label", "source"])
        out_dir = self.s.processed_dir / "clips"
        out_dir.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(char if char.isalnum() or char in "-_" else "_" for char in name)[:80]
        path = out_dir / f"{experiment_id}_{safe_name}_{int(time.time() * 1000)}.csv"
        fused.to_csv(path, index=False)
        self.db.record_file(path)
        now = datetime.now(timezone.utc).isoformat()
        start_ms = min(segment["start_ms"] for segment in normalized)
        end_ms = max(segment["end_ms"] for segment in normalized)
        if clip_id is None:
            clip_id = self.db.execute(
                "INSERT INTO clips(experiment_id,run_id,start_ms,end_ms,label,created_utc,updated_utc,"
                "output_path,configuration_snapshot_json,execution_mode,time_origin_type,time_origin_utc_ms,"
                "quality_status,alignment_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (experiment_id, run_id, start_ms, end_ms, name, now, now, str(path),
                 run.get("configuration_snapshot_json"), run.get("execution_mode"),
                 t0_meta["t0_source"], t0, run.get("quality_status"), run.get("alignment_json")))
        else:
            existing = self.db.query_one("SELECT id FROM clips WHERE id=? AND run_id=?", (clip_id, run_id))
            if not existing:
                raise ValueError("Clip not found")
            self.db.execute(
                "UPDATE clips SET start_ms=?,end_ms=?,label=?,updated_utc=?,output_path=? WHERE id=?",
                (start_ms, end_ms, name, now, str(path), clip_id))
            self.db.execute("DELETE FROM clip_segments WHERE clip_id=?", (clip_id,))
        self.db.executemany(
            "INSERT INTO clip_segments(clip_id,source_run_id,source_start_utc_ms,source_end_utc_ms,"
            "source_start_relative_ms,source_end_relative_ms,segment_order,label) VALUES(?,?,?,?,?,?,?,?)",
            [(clip_id, run_id, int(t0 + segment["start_ms"]), int(t0 + segment["end_ms"]),
              segment["start_ms"], segment["end_ms"], segment["order"], segment["label"])
             for segment in normalized])
        return {"ok": True, "clip_id": clip_id, "path": str(path), "n_rows": int(len(fused)),
                "duration_ms": round(clip_cursor_s * 1000, 3), "segments": len(normalized),
                "t0_utc_ms": t0}

    def clip(self, experiment_id: str, run_id: Optional[str], start_ms: float, end_ms: float,
             label: str) -> dict:
        if not run_id:
            raise ValueError("Run is required")
        return self.save_clip(experiment_id, run_id, label or "Clip", [
            {"source_run_id": run_id, "source_start_relative_ms": start_ms,
             "source_end_relative_ms": end_ms, "label": label}])


_flows: dict[str, TaskFlow] = {}


def get_flow(settings: Settings, db: Database, oai: OaiClient) -> TaskFlow:
    key = str(settings.db_path)
    if key not in _flows:
        _flows[key] = TaskFlow(settings, db, oai)
    return _flows[key]
