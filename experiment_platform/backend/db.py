"""SQLite metadata/index store.

SQLite holds metadata + indexes only; raw experiment files live on the
filesystem (Level 0) and are tracked in the ``files`` table with SHA-256.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    environment TEXT NOT NULL,
    operator_name TEXT,
    notes TEXT,
    purpose TEXT,
    flow TEXT,
    initial_oai_config TEXT,
    created_utc TEXT NOT NULL,
    schema_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS oai_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    name TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiment_acks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    run_id TEXT,
    seq INTEGER NOT NULL,
    direction TEXT NOT NULL,
    pc_send_ms INTEGER,
    phone_recv_ms INTEGER,
    phone_send_ms INTEGER,
    pc_recv_ms INTEGER,
    rtt_ms REAL,
    gnb_data_timestamp_ms INTEGER
);

CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    device_id TEXT,
    hostname TEXT,
    collected_utc_ms INTEGER,
    files_json TEXT,
    count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    run_id TEXT,
    start_ms REAL,
    end_ms REAL,
    label TEXT,
    created_utc TEXT,
    output_path TEXT
);

CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    manufacturer TEXT, model TEXT, codename TEXT,
    android_version TEXT, sdk_int INTEGER, build_fingerprint TEXT,
    app_version TEXT, battery_health TEXT, battery_capacity_uah INTEGER
);

CREATE TABLE IF NOT EXISTS conditions (
    condition_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    environment TEXT,
    orientation_deg REAL,
    incident_power_density_wm2 REAL,
    stirrer_mode TEXT, stirrer_state TEXT,
    target_rsrp_dbm REAL,
    traffic_condition TEXT,
    frequency_mhz REAL, bandwidth_mhz REAL,
    tx_gain_db REAL, rx_gain_db REAL,
    pusch_target_snr_db REAL, pusch_target_snr_x10 INTEGER, pusch_target_mode TEXT,
    scheduler_mode TEXT, mcs INTEGER, qm INTEGER, n_prb INTEGER,
    chamber_metadata_json TEXT,
    FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    device_id TEXT,
    session_id TEXT,
    state TEXT NOT NULL DEFAULT 'DRAFT',
    planned_order INTEGER, actual_order INTEGER, random_seed INTEGER,
    planned_start_utc_ms INTEGER, start_delay_s REAL,
    requested_config_json TEXT, actual_config_json TEXT,
    started_utc_ms INTEGER, ended_utc_ms INTEGER,
    quality_status TEXT, quality_flags_json TEXT,
    FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id),
    FOREIGN KEY(condition_id) REFERENCES conditions(condition_id)
);

CREATE TABLE IF NOT EXISTS sync_anchors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    attempt_index INTEGER NOT NULL,
    t1_ms INTEGER, t2_elapsed_ns INTEGER, t2_utc_ms INTEGER, t3_ms INTEGER,
    rtt_ms REAL, offset_ms REAL, uncertainty_ms REAL
);

CREATE TABLE IF NOT EXISTS oai_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    fetched_utc_ms INTEGER NOT NULL,
    ts_epoch_ns INTEGER, ts_utc TEXT,
    rnti TEXT, imsi TEXT,
    rsrp_dbm REAL, ssb_sinr_db REAL,
    ph_raw_db REAL, ph_normalized_db REAL, pcmax_dbm REAL,
    pusch_snr_db REAL, pusch_rssi REAL, pusch_rssi_unit TEXT,
    ul_mcs INTEGER, dl_mcs INTEGER, qm INTEGER, n_prb INTEGER,
    cqi INTEGER, ri INTEGER, pmi INTEGER, ul_ri INTEGER, tpmi INTEGER,
    ul_bler REAL, dl_bler REAL,
    harq_initial_tx_delta REAL, harq_retransmission_delta REAL, harq_retransmission_ratio REAL,
    dtx INTEGER, ul_goodput_mbps REAL, dl_goodput_mbps REAL,
    collection_stale INTEGER, raw_json_path TEXT
);

CREATE TABLE IF NOT EXISTS oai_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts_epoch_ns INTEGER, ts_utc TEXT,
    rnti TEXT, frame INTEGER, slot INTEGER,
    pusch_snr_db REAL, ph_normalized_db REAL, tpc_pusch INTEGER,
    tb_size_bytes INTEGER, tpc_in_flight_db REAL, delta_mcs_db REAL,
    n_prb INTEGER, mcs INTEGER, rssi REAL, rssi_unit TEXT,
    dedup_key TEXT UNIQUE,
    raw_json_path TEXT
);

CREATE TABLE IF NOT EXISTS oai_channel (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    fetched_utc_ms INTEGER NOT NULL,
    ts_utc TEXT,
    n_samples INTEGER,
    dt_ns REAL,
    peak_db REAL, noise_db REAL,
    rms_delay_ns REAL, k_factor_db REAL,
    tap_count INTEGER, peak_idx INTEGER, mean_delay_ns REAL,
    raw_json_path TEXT
);

CREATE TABLE IF NOT EXISTS oai_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    config_json_path TEXT NOT NULL,
    sha256 TEXT
);

CREATE TABLE IF NOT EXISTS files (
    file_path TEXT PRIMARY KEY,
    size_bytes INTEGER,
    sha256 TEXT,
    created_utc TEXT
);

CREATE TABLE IF NOT EXISTS run_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    from_state TEXT, to_state TEXT,
    utc_ms INTEGER NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS phone_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    utc_epoch_ms INTEGER, elapsed_realtime_ns INTEGER, t_corrected_epoch_ms REAL,
    experiment_id TEXT, condition_id TEXT, session_id TEXT, device_id TEXT, phase TEXT,
    battery_current_now_ua REAL, battery_current_average_ua REAL,
    battery_voltage_mv REAL, battery_power_w REAL,
    charge_counter_uah REAL, soc_percent REAL, battery_temperature_c REAL,
    thermal_status INTEGER, thermal_headroom REAL,
    ss_rsrp_dbm REAL, ss_rsrq_db REAL, ss_sinr_db REAL,
    csi_rsrp_dbm REAL, csi_rsrq_db REAL, csi_sinr_db REAL, csi_cqi INTEGER,
    nrarfcn INTEGER, pci INTEGER, nci TEXT, tac INTEGER, network_type TEXT,
    screen_state TEXT, plugged INTEGER, charging INTEGER,
    wifi_state TEXT, bluetooth_state TEXT, airplane_mode INTEGER,
    workload_type TEXT, workload_target_mbps REAL, workload_actual_mbps REAL,
    app_tx_bytes INTEGER, app_rx_bytes INTEGER,
    sample_quality_flags TEXT
);

CREATE INDEX IF NOT EXISTS idx_phone_samples_run ON phone_samples(run_id);
CREATE INDEX IF NOT EXISTS idx_oai_snapshots_run ON oai_snapshots(run_id, ts_epoch_ns);
CREATE INDEX IF NOT EXISTS idx_oai_events_run ON oai_events(run_id, ts_epoch_ns);
CREATE INDEX IF NOT EXISTS idx_sync_run ON sync_anchors(run_id, direction);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        with self._lock:
            self._conn.executescript(SCHEMA_DDL)
            # migration: add task fields to pre-existing experiments table
            for col, typ in (("purpose", "TEXT"), ("flow", "TEXT"), ("initial_oai_config", "TEXT")):
                try:
                    self._conn.execute(f"ALTER TABLE experiments ADD COLUMN {col} {typ}")
                except Exception:
                    pass
            # migration: add gNB data timestamp to pre-existing experiment_acks table
            try:
                self._conn.execute("ALTER TABLE experiment_acks ADD COLUMN gnb_data_timestamp_ms INTEGER")
            except Exception:
                pass
            self._conn.commit()

    # -- low level --------------------------------------------------------- #
    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur.lastrowid or 0

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, [tuple(r) for r in rows])
            self._conn.commit()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return [dict(r) for r in cur.fetchall()]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- provenance -------------------------------------------------------- #
    @staticmethod
    def sha256_file(path: Path) -> Optional[str]:
        if not path.exists():
            return None
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def record_file(self, path: Path) -> dict:
        p = Path(path)
        st = p.stat() if p.exists() else None
        entry = {
            "file_path": str(p),
            "size_bytes": st.st_size if st else None,
            "sha256": self.sha256_file(p),
            "created_utc": None,
        }
        if st:
            from datetime import datetime, timezone
            entry["created_utc"] = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
        self.execute(
            "INSERT OR REPLACE INTO files(file_path,size_bytes,sha256,created_utc) VALUES(?,?,?,?)",
            (entry["file_path"], entry["size_bytes"], entry["sha256"], entry["created_utc"]),
        )
        return entry

    # -- runs -------------------------------------------------------------- #
    def transition(self, run_id: str, to_state: str, note: str = "", from_state: Optional[str] = None,
                   utc_ms: Optional[int] = None) -> None:
        import time as _t
        if utc_ms is None:
            utc_ms = int(_t.time() * 1000)
        if from_state is None:
            row = self.query_one("SELECT state FROM runs WHERE run_id=?", (run_id,))
            from_state = row["state"] if row else "DRAFT"
        self.execute("UPDATE runs SET state=? WHERE run_id=?", (to_state, run_id))
        self.execute(
            "INSERT INTO run_transitions(run_id,from_state,to_state,utc_ms,note) VALUES(?,?,?,?,?)",
            (run_id, from_state, to_state, utc_ms, note),
        )

    def get_run(self, run_id: str) -> Optional[dict]:
        return self.query_one("SELECT * FROM runs WHERE run_id=?", (run_id,))

    def upsert_run(self, run: dict) -> None:
        cols = list(run.keys())
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "run_id")
        sql = f"INSERT INTO runs({', '.join(cols)}) VALUES({placeholders}) ON CONFLICT(run_id) DO UPDATE SET {updates}"
        self.execute(sql, [run[c] for c in cols])

    def upsert_condition(self, cond: dict) -> None:
        cols = list(cond.keys())
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "condition_id")
        sql = f"INSERT INTO conditions({', '.join(cols)}) VALUES({placeholders}) ON CONFLICT(condition_id) DO UPDATE SET {updates}"
        self.execute(sql, [cond[c] for c in cols])

    def upsert_experiment(self, exp: dict) -> None:
        cols = list(exp.keys())
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "experiment_id")
        sql = f"INSERT INTO experiments({', '.join(cols)}) VALUES({placeholders}) ON CONFLICT(experiment_id) DO UPDATE SET {updates}"
        self.execute(sql, [exp[c] for c in cols])

    def upsert_device(self, dev: dict) -> None:
        cols = list(dev.keys())
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "device_id")
        sql = f"INSERT INTO devices({', '.join(cols)}) VALUES({placeholders}) ON CONFLICT(device_id) DO UPDATE SET {updates}"
        self.execute(sql, [dev[c] for c in cols])

    # -- helpers ----------------------------------------------------------- #
    def set_json(self, run_id: str, field: str, obj: Any) -> None:
        assert field in {"requested_config_json", "actual_config_json", "quality_flags_json"}
        self.execute(f"UPDATE runs SET {field}=? WHERE run_id=?", (json.dumps(obj, ensure_ascii=False), run_id))
