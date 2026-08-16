"""Platform configuration.

Settings come from environment variables (OAI_*), an optional local
``platform/config.local.json``, then defaults. The OAI control token is only
ever read from env or the local secret config — never hard-coded, never stored
in the DB, and never exported.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1
PLATFORM_VERSION = "0.1.0"

# Repository root (this file is at <root>/experiment_platform/backend/config.py).
ROOT = Path(__file__).resolve().parents[2]

DEFAULTS = {
    "oai_host": "192.168.31.119",
    "oai_port": 8787,
    "oai_channel_port": 8091,         # oai_channel_daemon (scope 8090 -> HTTP 8091)
    "oai_control_token": "",          # optional; empty == not sent
    "oai_timeout_s": 8.0,
    "data_dir": str(ROOT / "experiment_platform" / "data"),
    "web_dist_dir": str(ROOT / "experiment_platform" / "web" / "dist"),
    "listen_host": "127.0.0.1",
    "listen_port": 8900,
}


def _load_local_config() -> dict:
    cfg: dict = {}
    p = ROOT / "experiment_platform" / "config.local.json"
    if p.exists():
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    return cfg


@dataclass
class Settings:
    oai_host: str = DEFAULTS["oai_host"]
    oai_port: int = DEFAULTS["oai_port"]
    oai_channel_port: int = DEFAULTS["oai_channel_port"]
    oai_control_token: str = DEFAULTS["oai_control_token"]
    oai_timeout_s: float = DEFAULTS["oai_timeout_s"]
    data_dir: Path = Path(DEFAULTS["data_dir"])
    web_dist_dir: Path = Path(DEFAULTS["web_dist_dir"])
    listen_host: str = DEFAULTS["listen_host"]
    listen_port: int = DEFAULTS["listen_port"]
    schema_version: int = SCHEMA_VERSION
    platform_version: str = PLATFORM_VERSION

    @property
    def oai_base_url(self) -> str:
        return f"http://{self.oai_host}:{self.oai_port}"

    @property
    def channel_base_url(self) -> str:
        return f"http://{self.oai_host}:{self.oai_channel_port}"

    @property
    def control_headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.oai_control_token:
            h["X-OAI-Control-Token"] = self.oai_control_token
        return h

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.raw_dir, self.processed_dir, self.db_path.parent):
            p.mkdir(parents=True, exist_ok=True)

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "platform.db"

    @property
    def redacted(self) -> dict:
        """Settings safe to put into manifests (no token)."""
        return {
            "oai_base_url": self.oai_base_url,
            "oai_host": self.oai_host,
            "oai_port": self.oai_port,
            "oai_timeout_s": self.oai_timeout_s,
            "oai_control_token_configured": bool(self.oai_control_token),
            "schema_version": self.schema_version,
            "platform_version": self.platform_version,
        }


def load_settings() -> Settings:
    local = _load_local_config()
    return Settings(
        oai_host=os.environ.get("OAI_HOST", local.get("oai_host", DEFAULTS["oai_host"])),
        oai_port=int(os.environ.get("OAI_PORT", local.get("oai_port", DEFAULTS["oai_port"]))),
        oai_channel_port=int(os.environ.get("OAI_CHANNEL_PORT", local.get("oai_channel_port", DEFAULTS["oai_channel_port"]))),
        oai_control_token=os.environ.get("OAI_CONTROL_TOKEN", local.get("oai_control_token", "")),
        oai_timeout_s=float(os.environ.get("OAI_TIMEOUT_S", local.get("oai_timeout_s", DEFAULTS["oai_timeout_s"]))),
        data_dir=Path(os.environ.get("PLATFORM_DATA_DIR", local.get("data_dir", DEFAULTS["data_dir"]))),
        web_dist_dir=Path(os.environ.get("PLATFORM_WEB_DIST", local.get("web_dist_dir", DEFAULTS["web_dist_dir"]))),
        listen_host=os.environ.get("PLATFORM_HOST", local.get("listen_host", DEFAULTS["listen_host"])),
        listen_port=int(os.environ.get("PLATFORM_PORT", local.get("listen_port", DEFAULTS["listen_port"]))),
    )
