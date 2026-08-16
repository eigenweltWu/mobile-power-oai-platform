"""OAI data collectors (task §34–36).

Each run gets independent collectors. Raw JSON is saved to Level 0 and
normalized rows are inserted into the index DB. Event collector dedups on
``timestampEpochNs + rnti + frame + slot`` so repeated polls never duplicate
CSV rows.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Optional

from .config import Settings
from .db import Database
from .oai_client import OaiClient


def _now_ms() -> int:
    return int(time.time() * 1000)


class Collector(threading.Thread):
    name = "collector"

    def __init__(self, run_id: str, settings: Settings, db: Database, client: OaiClient,
                 interval_s: float, daemon: bool = True):
        super().__init__(daemon=daemon, name=f"{self.name}-{run_id}")
        self.run_id = run_id
        self.settings = settings
        self.db = db
        self.client = client
        self.interval_s = interval_s
        self._stop = threading.Event()
        self.errors: list[str] = []

    def stop(self) -> None:
        self._stop.set()

    def _log_error(self, msg: str) -> None:
        self.errors.append(f"[{_now_ms()}] {msg}")

    def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self.collect_once()
                backoff = 1.0
            except Exception as e:  # noqa: BLE001 - collectors must survive API hiccups
                self._log_error(str(e))
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            self._stop.wait(self.interval_s)


class SnapshotCollector(Collector):
    name = "snapshot-collector"

    def __init__(self, run_id, settings, db, client, interval_s: float = 1.0):
        super().__init__(run_id, settings, db, client, interval_s)

    def collect_once(self) -> dict:
        raw = self.client.research_ues_raw()
        out_dir = self.settings.raw_dir / "oai" / "snapshots"
        out_dir.mkdir(parents=True, exist_ok=True)
        fetched = _now_ms()
        path = out_dir / f"{self.run_id}__{fetched}.json"
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        self.db.record_file(path)

        for ue in raw.get("ues", []):
            dl = ue.get("downlink") or {}
            ul = ue.get("uplink") or {}
            pc = ue.get("powerControl") or {}
            self.db.execute(
                """INSERT INTO oai_snapshots(
                   run_id, fetched_utc_ms, ts_epoch_ns, ts_utc, rnti, imsi,
                   rsrp_dbm, ssb_sinr_db, ph_raw_db, ph_normalized_db, pcmax_dbm,
                   pusch_snr_db, pusch_rssi, pusch_rssi_unit,
                   ul_mcs, dl_mcs, qm, n_prb, cqi, ri, pmi, ul_ri, tpmi,
                   ul_bler, dl_bler, harq_initial_tx_delta, harq_retransmission_delta,
                   harq_retransmission_ratio, dtx, ul_goodput_mbps, dl_goodput_mbps,
                   collection_stale, raw_json_path) VALUES(
                   ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    self.run_id, fetched, ue.get("timestampEpochNs"), ue.get("timestampUtc"),
                    ue.get("rnti"), ue.get("imsi"),
                    ue.get("rsrpDbm"), ue.get("ssbSinrDb"), pc.get("phRawDb"), pc.get("phNormalizedDb"), pc.get("pcmaxDbm"),
                    ul.get("puschSnrDb"), ul.get("puschRssi"), ul.get("puschRssiUnit"),
                    ul.get("mcs"), dl.get("mcs"), ul.get("qm"), ul.get("nPrb"),
                    dl.get("cqi"), dl.get("ri"), dl.get("pmi"), ul.get("ulRi"), ul.get("tpmi"),
                    ul.get("bler"), dl.get("bler"),
                    dl.get("harqInitialTxDelta"), dl.get("harqRetransmissionDelta"),
                    dl.get("harqRetransmissionRatio"), dl.get("dtx"),
                    ul.get("goodputMbps"), dl.get("goodputMbps"),
                    1 if (raw.get("collection") or {}).get("stale") else 0,
                    str(path),
                ),
            )
        return raw


class EventCollector(Collector):
    name = "event-collector"

    def __init__(self, run_id, settings, db, client, interval_s: float = 2.0, limit: int = 200):
        super().__init__(run_id, settings, db, client, interval_s)
        self.limit = limit

    @staticmethod
    def dedup_key(ev: dict) -> str:
        ts = ev.get("timestampEpochNs")
        return f"{ts}:{ev.get('rnti')}:{ev.get('frame')}:{ev.get('slot')}"

    def collect_once(self) -> int:
        raw = self.client.research_events_raw(limit=self.limit)
        out_dir = self.settings.raw_dir / "oai" / "events"
        out_dir.mkdir(parents=True, exist_ok=True)
        fetched = _now_ms()
        path = out_dir / f"{self.run_id}__{fetched}.json"
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        self.db.record_file(path)

        inserted = 0
        for ev in raw.get("events", []):
            self.db.execute(
                """INSERT OR IGNORE INTO oai_events(
                   run_id, ts_epoch_ns, ts_utc, rnti, frame, slot,
                   pusch_snr_db, ph_normalized_db, tpc_pusch, tb_size_bytes,
                   tpc_in_flight_db, delta_mcs_db, n_prb, mcs, rssi, rssi_unit,
                   dedup_key, raw_json_path) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    self.run_id, ev.get("timestampEpochNs"), ev.get("timestampUtc"),
                    ev.get("rnti"), ev.get("frame"), ev.get("slot"),
                    ev.get("puschSnrDb"), ev.get("phNormalizedDb"), ev.get("tpcPusch"), ev.get("tbSizeBytes"),
                    ev.get("tpcInFlightDb"), ev.get("deltaMcsDb"), ev.get("nPrb"), ev.get("mcs"),
                    ev.get("rssi"), ev.get("rssiUnit"), self.dedup_key(ev), str(path),
                ),
            )
            inserted += 1
        return inserted


class ChannelCollector(Collector):
    name = "channel-collector"

    def __init__(self, run_id: str, settings: Settings, db: Database, client: OaiClient,
                 interval_s: float = 1.0):
        super().__init__(run_id, settings, db, client, interval_s)

    def collect_once(self) -> dict:
        raw = self.client.channel_cir()
        out_dir = self.settings.raw_dir / "oai" / "channel"
        out_dir.mkdir(parents=True, exist_ok=True)
        fetched = _now_ms()
        path = out_dir / f"{self.run_id}__{fetched}.json"
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        self.db.record_file(path)

        if not raw.get("ok"):
            return raw
        m = raw.get("metrics") or {}
        self.db.execute(
            """INSERT INTO oai_channel(
               run_id, fetched_utc_ms, ts_utc, n_samples, dt_ns,
               peak_db, noise_db, rms_delay_ns, k_factor_db,
               tap_count, peak_idx, mean_delay_ns, raw_json_path)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                self.run_id, fetched, raw.get("tsUtc"), raw.get("nSamples"), raw.get("dtNs"),
                m.get("peakDb"), m.get("noiseDb"), m.get("rmsDelayNs"), m.get("kFactorDb"),
                m.get("tapCount"), m.get("peakIdx"), m.get("meanDelayNs"), str(path),
            ),
        )
        return raw


def save_config_provenance(run_id: str, stage: str, settings: Settings, db: Database, client: OaiClient) -> dict:
    """Read status/controls/config (and rf calibration) and save to Level 0 (task §36)."""
    out_dir = settings.raw_dir / "oai" / ("config" if "config" in stage else "status")
    out_dir.mkdir(parents=True, exist_ok=True)
    payloads = {
        "status": client.status_raw(),
        "controls": client.gnb_controls().model_dump(),
        "config": client.research_config_raw(),
    }
    path = out_dir / f"gnb_{stage}.json"
    path.write_text(json.dumps(payloads, ensure_ascii=False, indent=2), encoding="utf-8")
    db.record_file(path)
    db.execute(
        "INSERT INTO oai_config(run_id, stage, config_json_path, sha256) VALUES(?,?,?,?)",
        (run_id, stage, str(path), db.sha256_file(path)),
    )
    return payloads
