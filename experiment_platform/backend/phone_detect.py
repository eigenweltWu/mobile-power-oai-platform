"""Phone connection detection: OFFLINE / ATTACHED (USB) / CONNECTED (5G+ACK).

Every phone-touching operation calls ``detect_phone`` first. The detection:
  1. USB attached -> find the phone's PDU IP via ``adb shell ip route`` and cache it.
  2. 5G reachable -> GET the on-phone agent over the PDU IP (last-known if USB is gone).
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


def detect_phone(settings: Settings, serial: str = "53616213") -> dict:
    """Return {state, usb_attached, pdu_ip, agent_url, last_seen_ms}."""
    cached = _load(settings)
    result = {
        "state": "OFFLINE",
        "usb_attached": False,
        "pdu_ip": cached.get("pdu_ip"),
        "agent_url": None,
        "last_seen_ms": cached.get("last_seen_ms"),
    }

    # 1. USB attached?
    try:
        t = AdbTransport()
        if serial in t.devices():
            result["usb_attached"] = True
            out = t.shell(serial, "ip route")
            m = re.search(r"src (\d+\.\d+\.\d+\.\d+)", out)
            if m:
                result["pdu_ip"] = m.group(1)
    except Exception:
        pass

    # 2. 5G reachable? (probe the agent over the PDU IP)
    if result["pdu_ip"]:
        try:
            r = httpx.get(f"http://{result['pdu_ip']}:{AGENT_PORT}/agent/status", timeout=3.0)
            if r.status_code == 200:
                result["state"] = "CONNECTED"
                result["agent_url"] = f"http://{result['pdu_ip']}:{AGENT_PORT}"
                result["last_seen_ms"] = int(time.time() * 1000)
        except Exception:
            pass

    # 3. Fallback: USB attached but not 5G-reachable -> ATTACHED
    if result["state"] == "OFFLINE" and result["usb_attached"]:
        result["state"] = "ATTACHED"

    _save(settings, {"pdu_ip": result["pdu_ip"], "last_seen_ms": result["last_seen_ms"]})
    return result
