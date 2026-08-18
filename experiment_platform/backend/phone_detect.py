"""Phone connection detection: OFFLINE / ATTACHED (USB) / CONNECTED (5G+ACK).

Every phone-touching operation calls ``detect_phone`` first. The detection:
  1. USB attached -> find the phone's PDU IP via ``adb shell ip route`` and cache it.
     Then open ``adb forward tcp:<pc_port> tcp:8420`` and probe the agent
     over the USB tunnel. If the agent answers -> CONNECTED (USB tunnel).
  2. If USB tunnel fails, try 5G reachable: GET the on-phone agent over the
     PDU IP (last-known if USB is gone). If answers -> CONNECTED (5G).
  3. USB present but no agent answer -> ATTACHED (USB wired, agent not started).
The cached PDU IP survives across detections so 5G reachability can be probed
even after USB is unplugged.
"""
from __future__ import annotations

import json
import re
import time

import httpx

from .config import Settings
from .phone_channel import AdbTransport

AGENT_PORT = 8420
DEFAULT_PC_PORT = 8420


def _state_file(settings: Settings):
    return settings.data_dir / "phone_state.json"


def _load(settings: Settings) -> dict:
    p = _state_file(settings)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save(settings: Settings, data: dict) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    _state_file(settings).write_text(json.dumps(data), encoding="utf-8")


def _probe_status(url: str, timeout: float = 3.0) -> dict | None:
    """Hit the on-phone /agent/status and return parsed JSON, or None on failure."""
    try:
        r = httpx.get(url, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


def refresh_pdu_ip(settings: Settings, pdu_ip: str) -> None:
    """Overwrite the cached UE PDU IP (used after /shake resolves a new one).

    After a gNB restart the UE re-registers and usually gets a DIFFERENT
    PDU address; without this refresh detect_phone keeps probing the stale
    address and the phone appears OFFLINE forever.
    """
    cached = _load(settings)
    cached["pdu_ip"] = pdu_ip
    cached["last_seen_ms"] = int(time.time() * 1000)
    _save(settings, cached)


def detect_phone(settings: Settings, serial: str = "53616213",
                 pc_port: int = DEFAULT_PC_PORT) -> dict:
    """Return {state, usb_attached, pdu_ip, agent_url, last_seen_ms,
    status (phone /agent/status payload)}.

    Resolution order: USB adb-forward tunnel (highest priority, works with
    phones without an active PDU session) -> 5G PDU IP -> ATTACHED if only USB
    is present -> OFFLINE otherwise.

    The probe uses its OWN local port (pc_port + 1000) so it never tears down
    the forward tunnel the TaskFlow downlink loop is actively using on pc_port
    — sharing the port made the two fight over the tunnel (each remove_forward
    killed the other's connection), which broke the handshake and made the
    phone flap to OFFLINE.
    """
    cached = _load(settings)
    probe_port = pc_port + 1000  # independent probe port — never touch pc_port
    result = {
        "state": "OFFLINE",
        "usb_attached": False,
        "pdu_ip": cached.get("pdu_ip"),
        "agent_url": None,
        "last_seen_ms": cached.get("last_seen_ms"),
        "status": None,
        "serial": serial,
        "pc_port": pc_port,
    }

    # 1. USB attached? If yes, use the adb-forward tunnel FIRST — this works
    # even when the phone has no 5G data connection (the usual offline bug).
    # NOTE: the USB probe tunnel is removed right after the probe, so its URL
    # must NOT be exposed as agent_url. During real experiments USB is
    # UNPLUGGED — live communication goes over the 5G PDU IP. The USB probe
    # only verifies the agent is alive and harvests pdu_ip + status.
    try:
        transport = AdbTransport()
        if serial in transport.devices():
            result["usb_attached"] = True
            out = transport.shell(serial, "ip route")
            m = re.search(r"src (\d+\.\d+\.\d+\.\d+)", out)
            if m:
                result["pdu_ip"] = m.group(1)
            # Build the adb forward tunnel and probe /agent/status.
            try:
                transport.forward(serial, probe_port, AGENT_PORT)
                usb_url = f"http://127.0.0.1:{probe_port}/agent/status"
                ps = _probe_status(usb_url, timeout=3.0)
                if ps is not None:
                    result["state"] = "CONNECTED"
                    result["last_seen_ms"] = int(time.time() * 1000)
                    result["status"] = ps
            except Exception:
                pass
            finally:
                try:
                    transport.remove_forward(serial, probe_port)
                except Exception:
                    pass
    except Exception:
        pass

    # 2. If USB tunnel didn't answer (agent not started yet, or no USB),
    # try the cached 5G PDU IP probe.
    if result["state"] != "CONNECTED" and result["pdu_ip"]:
        pdu_url = f"http://{result['pdu_ip']}:{AGENT_PORT}/agent/status"
        ps = _probe_status(pdu_url, timeout=3.0)
        if ps is not None:
            result["state"] = "CONNECTED"
            result["agent_url"] = pdu_url
            result["last_seen_ms"] = int(time.time() * 1000)
            result["status"] = ps

    # 3. USB attached but no agent reachable -> ATTACHED (wired, agent may not
    # be started yet; the UI keeps the user informed).
    if result["state"] == "OFFLINE" and result["usb_attached"]:
        result["state"] = "ATTACHED"

    latest = _load(settings)
    if result["state"] == "OFFLINE" and latest.get("pdu_ip") != result["pdu_ip"]:
        result["pdu_ip"] = latest.get("pdu_ip")
        result["last_seen_ms"] = latest.get("last_seen_ms")
    _save(settings, {"pdu_ip": result["pdu_ip"], "last_seen_ms": result["last_seen_ms"]})
    return result
