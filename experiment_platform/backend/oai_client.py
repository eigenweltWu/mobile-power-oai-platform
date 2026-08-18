"""OAI control-center HTTP client.

Thin, explicit wrapper over the LAN REST API discovered in
``data/oai_schema/*.json``. GET methods return typed models; POST methods
return the full JSON response dict so callers never lose fields. All calls log
failures and support a configurable timeout.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

import httpx

from . import oai_models as m
from .config import Settings


class OaiError(RuntimeError):
    """Raised when the OAI API returns a non-2xx status."""

    def __init__(self, method: str, url: str, status: int, body: str):
        super().__init__(f"OAI {method} {url} -> HTTP {status}: {body}")
        self.status = status
        self.body = body
        try:
            self.code = json.loads(body).get("code", "")
        except Exception:
            self.code = ""


class OaiClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self._client = httpx.Client(
            base_url=settings.oai_base_url,
            timeout=settings.oai_timeout_s,
            headers=settings.control_headers,
        )

    def close(self) -> None:
        self._client.close()

    # ---- low-level ------------------------------------------------------- #
    def _get(self, path: str, **params: Any) -> Any:
        r = self._client.get(path, params=params)
        if r.status_code >= 400:
            raise OaiError("GET", path, r.status_code, r.text)
        return r.json()

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        r = self._client.post(path, json=payload)
        if r.status_code >= 400:
            raise OaiError("POST", path, r.status_code, r.text)
        result = r.json()
        if isinstance(result, dict) and result.get("ok") is False:
            raise OaiError("POST", path, r.status_code, r.text)
        return result

    # ---- GET ------------------------------------------------------------- #
    def health(self) -> dict:
        return self._get("/api/health")

    def status(self) -> m.Status:
        return m.Status.model_validate(self._get("/api/status"))

    def status_raw(self) -> dict:
        return self._get("/api/status")

    def gnb_controls(self) -> m.GnbControls:
        return m.GnbControls.model_validate(self._get("/api/gnb/controls"))

    def telemetry_ues(self) -> m.ResearchUes:
        return m.ResearchUes.model_validate(self._get("/api/telemetry/ues"))

    def telemetry_ues_raw(self) -> dict:
        return self._get("/api/telemetry/ues")

    def telemetry_config(self) -> m.ResearchConfig:
        return m.ResearchConfig.model_validate(self._get("/api/telemetry/config"))

    def telemetry_config_raw(self) -> dict:
        return self._get("/api/telemetry/config")

    def telemetry_events(self, limit: int = 200) -> m.ResearchEvents:
        return m.ResearchEvents.model_validate(self._get("/api/telemetry/events", limit=limit))

    def telemetry_events_raw(self, limit: int = 200) -> dict:
        return self._get("/api/telemetry/events", limit=limit)

    def fresh_ues(self, max_age_s: float = 5.0) -> list[m.ResearchUe]:
        payload = self.telemetry_ues()
        if not payload.collection or payload.collection.stale:
            raise RuntimeError("OAI telemetry is stale")
        return [ue for ue in payload.ues
                if ue.ageSeconds is not None and ue.ageSeconds <= max_age_s]

    # Compatibility names for older internal callers and third-party imports.
    research_ues = telemetry_ues
    research_ues_raw = telemetry_ues_raw
    research_config = telemetry_config
    research_config_raw = telemetry_config_raw
    research_events = telemetry_events
    research_events_raw = telemetry_events_raw

    def nettest_status(self) -> dict:
        return self._get("/api/nettest/status")

    def nettest_start(self, direction: str, protocol: str = "udp",
                      rate_mbps: float = 0.0) -> dict:
        payload: dict[str, Any] = {
            "action": "start", "direction": direction, "protocol": protocol,
        }
        if protocol == "udp":
            payload["rateMbps"] = rate_mbps
        return self._post("/api/nettest", payload)

    def nettest_stop(self) -> dict:
        return self._post("/api/nettest", {"action": "stop"})

    def configuration_history(self, limit: int = 100) -> dict:
        return self._get("/api/history/configuration", limit=limit)

    def rf_calibration(self, device: Optional[str] = None, frequencyMHz: Optional[float] = None) -> dict:
        params: dict[str, Any] = {}
        if device:
            params["device"] = device
        if frequencyMHz is not None:
            params["frequencyMHz"] = frequencyMHz
        return self._get("/api/rf/calibration", **params)

    def progress(self, request_id: Optional[str] = None) -> m.Progress:
        params = {"id": request_id} if request_id else {}
        return m.Progress.model_validate(self._get("/api/gnb/progress", **params))

    def channel_cir(self) -> dict:
        """Read the control backend's cached PHY snapshot.

        External clients must never connect to WebScope (8090) or its legacy
        8091 proxy.  The 8787 backend owns the sole WebSocket and refreshes its
        in-memory snapshot at 1 Hz; repeated reads here never trigger capture.
        """
        raw = self._get("/api/scope/snapshot")
        age = raw.get("ageSeconds")
        fresh = (raw.get("connection") == "live" and age is not None and
                 float(age) <= 3.0)
        return {
            **raw,
            "ok": fresh,
            "tsUtc": raw.get("updatedAt"),
            "nSamples": raw.get("cirPointCount"),
            "error": "" if fresh else "PHY snapshot is not live/fresh",
        }

    # ---- POST control ---------------------------------------------------- #
    def gnb_service(self, action: str, request_id: Optional[str] = None) -> dict:
        """POST /api/gnb/service and preserve its synchronous success semantics.

        The OAI handler executes service actions SYNCHRONOUSLY (quiesce UE,
        docker stop, host preflight, docker start, NG-setup wait, UE restore
        — easily 30 s+), far beyond the shared client timeout
        (``oai_timeout_s`` = 8 s). So we submit our own ``requestId`` and
        TOLERATE the transport timeout: the OAI handler thread keeps running
        regardless of the dropped connection. The requestId is returned so
        the caller follows the action via :meth:`wait_for_restart` polling
        ``/api/gnb/progress`` instead of holding the POST open.
        """
        rid = request_id or f"pc-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        try:
            r = self._client.post("/api/gnb/service",
                                  json={"action": action, "requestId": rid},
                                  timeout=max(70.0, self.s.oai_timeout_s))
            if r.status_code != 200:
                raise OaiError("POST", "/api/gnb/service", r.status_code, r.text)
            resp = r.json()
            if resp.get("ok") is False:
                raise OaiError("POST", "/api/gnb/service", r.status_code, r.text)
            # Fast path (< timeout): the action already finished server-side.
            if not resp.get("requestId"):
                resp["requestId"] = rid
            return resp
        except httpx.ReadTimeout:
            # The request reached the OAI host; its handler thread continues
            # the (long) action. Surface the requestId so wait_for_restart()
            # can follow the progress.
            return {"ok": None, "pending": True,
                    "message": f"{action} pending; follow progress", "requestId": rid}

    def shake(self, n_exchanges: int = 3) -> dict:
        """POST /api/shake — OAI 主机代发 UE downlink 探测.

        The OAI host sits INSIDE the PDU subnet (10.0.1.0/24) while the PC
        does not, so only the OAI host can reach the phone's agent over 5G.
        The route resolves the current UE PDU IP from the oai-upf session
        table (USB-free, independent of the phone app) and performs
        NTP-style timestamp exchanges against ue_ip:8420/agent/downlink.

        Returns the raw JSON: on success ``{ok, ue_ip, rtt_ms, offset_ms,
        exchanges[]}``; on failure the OAI side answers HTTP 502/503 with a
        JSON body (``{ok:false, ue_ip?, error}``) which is returned as-is
        instead of raising — callers decide whether the error is fatal.
        """
        import json as _json
        try:
            return self._post("/api/shake", {"n_exchanges": n_exchanges})
        except OaiError as e:
            try:
                return _json.loads(e.body)
            except Exception:
                return {"ok": False, "error": str(e)}

    def oai_pc_offset_ms(self) -> float:
        """PC 时钟 - OAI 主机时钟 (ms)，用 /api/status 的 timestamp 以 RTT 中点估算.

        Needed because /shake stamps its exchanges with the OAI host clock;
        adding this offset converts them to the PC clock base the platform
        stores in experiment_acks / sync_anchors.
        """
        from datetime import datetime
        t0 = time.time() * 1000.0
        raw = self.status_raw()
        t1 = time.time() * 1000.0
        ts = raw.get("timestamp")
        if not ts:
            return 0.0
        try:
            oai_ms = datetime.fromisoformat(str(ts)).timestamp() * 1000.0
        except ValueError:
            return 0.0
        return ((t0 + t1) / 2.0) - oai_ms

    @staticmethod
    def _with_request_id(payload: dict[str, Any], request_id: Optional[str]) -> dict[str, Any]:
        payload["requestId"] = request_id or OaiClient.new_request_id("pc", "control")
        return payload

    @staticmethod
    def new_request_id(prefix: Optional[str], action: str) -> str:
        root = prefix or "pc"
        return f"{root}-{action}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"

    def gnb_bandwidth(self, bandwidth_mhz: int, restart: bool,
                      request_id: Optional[str] = None) -> dict:
        return self._post("/api/gnb/bandwidth", self._with_request_id(
            {"bandwidthMHz": bandwidth_mhz, "restart": restart}, request_id))

    def gnb_frequency(self, frequency_mhz: float, restart: bool,
                      request_id: Optional[str] = None) -> dict:
        return self._post("/api/gnb/frequency", self._with_request_id(
            {"frequencyMHz": frequency_mhz, "restart": restart}, request_id))

    def gnb_gains(self, tx_gain_db: float, rx_gain_db: float, restart: bool,
                  request_id: Optional[str] = None) -> dict:
        return self._post("/api/gnb/gains", self._with_request_id(
            {"txGainDb": tx_gain_db, "rxGainDb": rx_gain_db, "restart": restart}, request_id))

    def gnb_pusch_target_snr(self, mode: str, pusch_target_snr_x10: Optional[int], restart: bool,
                             request_id: Optional[str] = None) -> dict:
        payload: dict[str, Any] = {"mode": mode, "restart": restart}
        if pusch_target_snr_x10 is not None:
            payload["puschTargetSnrX10"] = pusch_target_snr_x10
        return self._post("/api/gnb/pusch-target-snr", self._with_request_id(payload, request_id))

    def gnb_ul_scheduler(self, mode: str, mcs: Optional[int] = None, qm: Optional[int] = None,
                         prb: Optional[int] = None, restart: bool = True,
                         request_id: Optional[str] = None) -> dict:
        payload: dict[str, Any] = {"mode": mode, "restart": restart}
        if mcs is not None:
            payload["mcs"] = mcs
        if qm is not None:
            payload["qm"] = qm
        if prb is not None:
            payload["prb"] = prb
        return self._post("/api/gnb/ul-scheduler", self._with_request_id(payload, request_id))

    # ---- helpers --------------------------------------------------------- #
    def extract_request_id(self, resp: dict) -> Optional[str]:
        """A control POST that triggers a restart returns a ``requestId``;
        a no-restart change returns ``restarted:false`` with no id."""
        return resp.get("requestId") if isinstance(resp.get("requestId"), str) and resp.get("requestId") else None

    def wait_for_restart(self, request_id: Optional[str], timeout_s: float = 300.0,
                         poll_s: float = 2.0, on_update=None) -> m.Progress:
        """Poll /api/gnb/progress until completion, failure or timeout.

        Returns the last observed :class:`Progress`. Does not raise on timeout
        (caller inspects the returned object), raises :class:`OaiError` on
        transport errors.
        """
        deadline = time.monotonic() + timeout_s
        last: Optional[m.Progress] = None
        while time.monotonic() < deadline:
            last = self.progress(request_id)
            if on_update:
                on_update(last)
            if last.failed:
                return last
            if last.done:
                return last
            time.sleep(poll_s)
        # Timeout — return whatever we last saw.
        if last is None:
            last = m.Progress(requestId=request_id, active=True, action="", phase="timeout",
                              message="wait_for_restart timed out", progress=-1, error="timeout", updatedAt="")
        return last

    def apply_condition(self, requested: "dict[str, Any]", on_update=None,
                        restart_timeout_s: float = 300.0,
                        force_restart: bool = False,
                        request_prefix: Optional[str] = None) -> dict[str, Any]:
        """Apply a full condition while minimizing restarts (task §11).

        ``requested`` keys (all optional): ``bandwidthMHz``, ``txGainDb``,
        ``rxGainDb``, ``frequencyMHz``, ``puschTargetMode``,
        ``puschTargetSnrX10``, ``schedulerMode``, ``mcs``, ``qm``, ``nPrb``.

        Every parameter is persisted with ``restart:false`` first; if any
        effective change requires a restart, a single ``restart`` is issued and
        awaited via the progress API.

        ``force_restart=True`` ALWAYS issues the real restart and awaits it —
        used by template switching, where the gNB must come back up running
        the template's full RF configuration. The per-parameter responses are
        not a reliable restart signal: a ``restart:false`` write just answers
        ``restarted:false`` with no hint that a restart would be needed, so
        relying on them silently degrades into "parameters submitted, gNB
        never restarted".
        """
        results: dict[str, Any] = {}
        operation_id = self.new_request_id(request_prefix, "apply")
        needs_restart = False

        def _maybe_restart(resp: dict) -> None:
            nonlocal needs_restart
            if resp.get("restarted") or resp.get("restart") is True:
                needs_restart = True

        if requested.get("bandwidthMHz") is not None:
            r = self.gnb_bandwidth(int(requested["bandwidthMHz"]), restart=False,
                                   request_id=f"{operation_id}-bandwidth")
            results["bandwidth"] = r
            _maybe_restart(r)
        if requested.get("frequencyMHz") is not None:
            r = self.gnb_frequency(float(requested["frequencyMHz"]), restart=False,
                                   request_id=f"{operation_id}-frequency")
            results["frequency"] = r
            _maybe_restart(r)
        if requested.get("txGainDb") is not None or requested.get("rxGainDb") is not None:
            r = self.gnb_gains(float(requested.get("txGainDb", 60)), float(requested.get("rxGainDb", 40)),
                               restart=False, request_id=f"{operation_id}-gains")
            results["gains"] = r
            _maybe_restart(r)
        if requested.get("puschTargetMode") is not None:
            x10 = requested.get("puschTargetSnrX10")
            r = self.gnb_pusch_target_snr(
                requested["puschTargetMode"], x10, restart=False,
                request_id=f"{operation_id}-pusch-target")
            results["puschTarget"] = r
            _maybe_restart(r)
        if requested.get("schedulerMode") is not None:
            r = self.gnb_ul_scheduler(
                requested["schedulerMode"],
                mcs=requested.get("mcs"), qm=requested.get("qm"), prb=requested.get("nPrb"),
                restart=False,
                request_id=f"{operation_id}-scheduler",
            )
            results["ulScheduler"] = r
            _maybe_restart(r)

        if needs_restart or force_restart:
            # Objective before-evidence: the gNB process start timestamp.
            before = self._gnb_started_at() if force_restart else None
            r = self.gnb_service(
                "restart", request_id=f"{operation_id}-restart")
            results["restart"] = r
            rid = self.extract_request_id(r)
            prog = self.wait_for_restart(rid, timeout_s=restart_timeout_s, on_update=on_update)
            results["progress"] = prog
            if force_restart:
                # A forced restart must be REAL — fail loudly instead of
                # silently continuing with "parameters submitted".
                if prog.failed:
                    raise OaiError("POST", "/api/gnb/service", 0,
                                   f"gNB restart FAILED: {prog.error or prog.message or 'failed'}")
                if not prog.done:
                    raise OaiError("POST", "/api/gnb/service", 0,
                                   f"gNB restart did not complete: phase={prog.phase} error={prog.error}")
                ok, after, running = self.verify_restarted(before)
                results["restart_verified"] = ok
                results["startedAt"] = {"before": before, "after": after}
                if not ok:
                    detail = ("gNB NOT running after restart"
                              if not running else
                              f"gNB did NOT actually restart — process startedAt unchanged ({before})")
                    raise OaiError("POST", "/api/gnb/service", 0, detail)

        return results

    def _gnb_started_at(self) -> Optional[str]:
        try:
            st = self.status()
            return st.gnb.startedAt if st.gnb else None
        except Exception:
            return None

    def verify_restarted(self, before: Optional[str], timeout_s: float = 60.0,
                         poll_s: float = 3.0) -> tuple[bool, Optional[str], bool]:
        """Prove the gNB process was actually replaced: it must be running
        AND (when the previous ``startedAt`` was known) the start timestamp
        must differ from ``before``. Returns (ok, startedAt_after, running)."""
        deadline = time.monotonic() + timeout_s
        after: Optional[str] = None
        running = False
        while time.monotonic() < deadline:
            try:
                st = self.status()
            except Exception:
                time.sleep(poll_s)
                continue
            running = bool(st.gnb and st.gnb.running)
            after = st.gnb.startedAt if st.gnb else None
            if running and (before is None or (after is not None and after != before)):
                return True, after, running
            time.sleep(poll_s)
        return False, after, running

    def ensure_gnb_running(self, timeout_s: float = 300.0, wait_ue: bool = True,
                           request_prefix: Optional[str] = None) -> bool:
        """Start the gNB if it is stopped, then (optionally) wait until gNB
        running + UE in-sync (research collection fresh).

        ``wait_ue=False`` returns as soon as the gNB process is running — use
        this on the experiment-start path: the air-interface handshake flow
        brings the UE in on its own, and blocking start for minutes when the
        UE has not (re)attached yet makes the platform look hung."""
        st = self.status()
        if not (st.gnb and st.gnb.running):
            try:
                resp = self.gnb_service(
                    "start", request_id=self.new_request_id(request_prefix, "start"))
                rid = self.extract_request_id(resp)
                try:
                    self.wait_for_restart(rid, timeout_s=timeout_s)
                except Exception:
                    pass
            except Exception:
                # POST may block/time out during gNB start; the action is already
                # initiated on the OAI host — fall through to polling.
                pass
        st = self.status()
        if not wait_ue:
            return bool(st.gnb and st.gnb.running)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            st = self.status()
            if st.gnb and st.gnb.running:
                try:
                    if self.fresh_ues():
                        return True
                except Exception:
                    pass
            time.sleep(5)
        return bool(st.gnb and st.gnb.running)
