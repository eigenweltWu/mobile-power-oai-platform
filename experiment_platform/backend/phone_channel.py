"""Phone control channel (task §15, Phase B).

Preferred path: the Android agent listens on a local HTTP server on the phone
(127.0.0.1), and the PC reaches it through an ``adb forward`` USB-only tunnel.
Fallback: ``adb pull`` of the exported files (no live control channel).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

import httpx

# The Android agent's local control port (must match the app).
AGENT_PHONE_PORT = 8420


class AdbNotFound(RuntimeError):
    pass


def find_adb() -> str:
    candidates = []
    sdk = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
    if sdk:
        candidates.append(Path(sdk) / "platform-tools" / "adb.exe")
    local = Path(__file__).resolve().parents[2] / ".tools" / "platform-tools" / "adb.exe"
    candidates.append(local)
    for c in candidates:
        if Path(c).exists():
            return str(c)
    which = shutil.which("adb")
    if which:
        return which
    raise AdbNotFound("adb not found; set ANDROID_HOME or place platform-tools in .tools")


class AdbTransport:
    def __init__(self, adb: Optional[str] = None):
        self.adb = adb or find_adb()

    def _run(self, *args: str, timeout: float = 30.0) -> str:
        p = subprocess.run([self.adb, *args], capture_output=True, text=True, timeout=timeout)
        return (p.stdout or "") + (p.stderr or "")

    def devices(self) -> list[str]:
        out = self._run("devices")
        serials = []
        for line in out.splitlines()[1:]:
            line = line.strip()
            if line and "\tdevice" in line:
                serials.append(line.split("\t")[0])
        return serials

    def forward(self, serial: str, pc_port: int, phone_port: int = AGENT_PHONE_PORT) -> None:
        self._run("-s", serial, "forward", f"tcp:{pc_port}", f"tcp:{phone_port}")

    def remove_forward(self, serial: str, pc_port: int) -> None:
        self._run("-s", serial, "forward", "--remove", f"tcp:{pc_port}")

    def pull(self, serial: str, remote: str, local: str) -> None:
        self._run("-s", serial, "pull", remote, local)

    def shell(self, serial: str, cmd: str) -> str:
        return self._run("-s", serial, "shell", cmd)


class PhoneAgent:
    """HTTP client for the on-phone agent control server.

    Reached either via an ``adb forward`` USB tunnel (127.0.0.1:<port>) or
    directly over the 5G PDU IP (``base_url``)."""

    def __init__(self, base_port: Optional[int] = None, base_url: Optional[str] = None,
                 timeout: float = 10.0):
        if base_url:
            self.base = base_url.rstrip("/")
        elif base_port is not None:
            self.base = f"http://127.0.0.1:{base_port}"
        else:
            raise ValueError("PhoneAgent needs base_port or base_url")
        self.timeout = timeout

    def _get(self, path: str) -> dict:
        r = httpx.get(self.base + path, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, payload: dict) -> dict:
        r = httpx.post(self.base + path, json=payload, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def status(self) -> dict:
        return self._get("/agent/status")

    def time(self) -> dict:
        """Return the phone's current clock tuple (t2)."""
        return self._get("/agent/time")

    def session(self, plan: dict) -> dict:
        return self._post("/agent/session", plan)

    def arm(self, run_id: str) -> dict:
        return self._post("/agent/arm", {"runId": run_id})

    def abort(self) -> dict:
        return self._post("/agent/abort", {})

    def downlink(self, seq: int, pc_send_ms: float) -> dict:
        """One downlink ping; the phone replies with its recv/send timestamps (uplink ACK)."""
        return self._post("/agent/downlink", {"seq": seq, "pcSendMs": pc_send_ms})

    def list_tasks(self) -> dict:
        return self._get("/agent/tasks")

    def push_task(self, task: dict) -> dict:
        return self._post("/agent/tasks", task)

    def start_task(self, experiment_id: str) -> dict:
        return self._post("/agent/task/start", {"experimentId": experiment_id})

    def stop_task(self) -> dict:
        return self._post("/agent/task/stop", {})

    def mark_collected(self, experiment_id: str, hostname: str) -> dict:
        return self._post("/agent/collected", {"experimentId": experiment_id, "hostname": hostname})

    def export(self, dest_dir: Path) -> list[Path]:
        """Pull the agent's export payload (JSON lines) and write files locally."""
        r = httpx.get(self.base + "/agent/export", timeout=60.0)
        r.raise_for_status()
        data = r.json()
        files = data.get("files", {}) if isinstance(data, dict) else {}
        written: list[Path] = []
        for name, content in files.items():
            p = dest_dir / name
            p.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, str):
                p.write_text(content, encoding="utf-8")
            else:
                p.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
            written.append(p)
        return written


class PhoneChannel:
    """High-level phone operations used by the experiment manager."""

    def __init__(self, serial: str, pc_port: int = 8420):
        self.serial = serial
        self.pc_port = pc_port
        self.transport = AdbTransport()
        self.agent = PhoneAgent(pc_port)

    def connect(self) -> None:
        self.transport.forward(self.serial, self.pc_port)

    def disconnect(self) -> None:
        self.transport.remove_forward(self.serial, self.pc_port)

    def time_exchange(self) -> dict:
        """One NTP-style exchange; returns {t1_ms, t2, t3_ms}."""
        t1 = time.time() * 1000.0
        t2 = self.agent.time()
        t3 = time.time() * 1000.0
        return {"t1_ms": t1, "t2": t2, "t3_ms": t3}

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.disconnect()
        return False
