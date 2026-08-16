"""OAI control-center HTTP client.

Thin, explicit wrapper over the LAN REST API discovered in
``data/oai_schema/*.json``. GET methods return typed models; POST methods
return the full JSON response dict so callers never lose fields. All calls log
failures and support a configurable timeout.
"""
from __future__ import annotations

import time
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
        return r.json()

    # ---- GET ------------------------------------------------------------- #
    def health(self) -> dict:
        return self._get("/api/health")

    def status(self) -> m.Status:
        return m.Status.model_validate(self._get("/api/status"))

    def status_raw(self) -> dict:
        return self._get("/api/status")

    def gnb_controls(self) -> m.GnbControls:
        return m.GnbControls.model_validate(self._get("/api/gnb/controls"))

    def research_ues(self) -> m.ResearchUes:
        return m.ResearchUes.model_validate(self._get("/api/research/ues"))

    def research_ues_raw(self) -> dict:
        return self._get("/api/research/ues")

    def research_config(self) -> m.ResearchConfig:
        return m.ResearchConfig.model_validate(self._get("/api/research/config"))

    def research_config_raw(self) -> dict:
        return self._get("/api/research/config")

    def research_events(self, limit: int = 200) -> m.ResearchEvents:
        return m.ResearchEvents.model_validate(self._get("/api/research/events", limit=limit))

    def research_events_raw(self, limit: int = 200) -> dict:
        return self._get("/api/research/events", limit=limit)

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

    # ---- POST control ---------------------------------------------------- #
    def gnb_service(self, action: str) -> dict:
        return self._post("/api/gnb/service", {"action": action})

    def gnb_bandwidth(self, bandwidth_mhz: int, restart: bool) -> dict:
        return self._post("/api/gnb/bandwidth", {"bandwidthMHz": bandwidth_mhz, "restart": restart})

    def gnb_frequency(self, frequency_mhz: float, restart: bool) -> dict:
        return self._post("/api/gnb/frequency", {"frequencyMHz": frequency_mhz, "restart": restart})

    def gnb_gains(self, tx_gain_db: float, rx_gain_db: float, restart: bool) -> dict:
        return self._post("/api/gnb/gains", {"txGainDb": tx_gain_db, "rxGainDb": rx_gain_db, "restart": restart})

    def gnb_pusch_target_snr(self, mode: str, pusch_target_snr_x10: Optional[int], restart: bool) -> dict:
        payload: dict[str, Any] = {"mode": mode, "restart": restart}
        if pusch_target_snr_x10 is not None:
            payload["puschTargetSnrX10"] = pusch_target_snr_x10
        return self._post("/api/gnb/pusch-target-snr", payload)

    def gnb_ul_scheduler(self, mode: str, mcs: Optional[int] = None, qm: Optional[int] = None,
                         prb: Optional[int] = None, restart: bool = True) -> dict:
        payload: dict[str, Any] = {"mode": mode, "restart": restart}
        if mcs is not None:
            payload["mcs"] = mcs
        if qm is not None:
            payload["qm"] = qm
        if prb is not None:
            payload["prb"] = prb
        return self._post("/api/gnb/ul-scheduler", payload)

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
                        restart_timeout_s: float = 300.0) -> dict[str, Any]:
        """Apply a full condition while minimizing restarts (task §11).

        ``requested`` keys (all optional): ``bandwidthMHz``, ``txGainDb``,
        ``rxGainDb``, ``frequencyMHz``, ``puschTargetMode``,
        ``puschTargetSnrX10``, ``schedulerMode``, ``mcs``, ``qm``, ``nPrb``.

        Every parameter is persisted with ``restart:false`` first; if any
        effective change requires a restart, a single ``restart`` is issued and
        awaited via the progress API.
        """
        results: dict[str, Any] = {}
        needs_restart = False

        def _maybe_restart(resp: dict) -> None:
            nonlocal needs_restart
            if resp.get("restarted") or resp.get("restart") is True:
                needs_restart = True

        if requested.get("bandwidthMHz") is not None:
            r = self.gnb_bandwidth(int(requested["bandwidthMHz"]), restart=False)
            results["bandwidth"] = r
            _maybe_restart(r)
        if requested.get("frequencyMHz") is not None:
            r = self.gnb_frequency(float(requested["frequencyMHz"]), restart=False)
            results["frequency"] = r
            _maybe_restart(r)
        if requested.get("txGainDb") is not None or requested.get("rxGainDb") is not None:
            r = self.gnb_gains(float(requested.get("txGainDb", 60)), float(requested.get("rxGainDb", 40)), restart=False)
            results["gains"] = r
            _maybe_restart(r)
        if requested.get("puschTargetMode") is not None:
            x10 = requested.get("puschTargetSnrX10")
            r = self.gnb_pusch_target_snr(requested["puschTargetMode"], x10, restart=False)
            results["puschTarget"] = r
            _maybe_restart(r)
        if requested.get("schedulerMode") is not None:
            r = self.gnb_ul_scheduler(
                requested["schedulerMode"],
                mcs=requested.get("mcs"), qm=requested.get("qm"), prb=requested.get("nPrb"),
                restart=False,
            )
            results["ulScheduler"] = r
            _maybe_restart(r)

        if needs_restart:
            r = self.gnb_service("restart")
            results["restart"] = r
            rid = self.extract_request_id(r)
            results["progress"] = self.wait_for_restart(rid, timeout_s=restart_timeout_s, on_update=on_update)

        return results

    def ensure_gnb_running(self, timeout_s: float = 300.0, wait_ue: bool = True) -> bool:
        """Start the gNB if it is stopped, then (optionally) wait until gNB
        running + UE in-sync (research collection fresh).

        ``wait_ue=False`` returns as soon as the gNB process is running — use
        this on the experiment-start path: the air-interface handshake flow
        brings the UE in on its own, and blocking start for minutes when the
        UE has not (re)attached yet makes the platform look hung."""
        st = self.status()
        if not (st.gnb and st.gnb.running):
            try:
                resp = self.gnb_service("start")
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
                    ues = self.research_ues()
                    if ues.collection and ues.collection.available and not ues.collection.stale and ues.ues:
                        return True
                except Exception:
                    pass
            time.sleep(5)
        return bool(st.gnb and st.gnb.running)
