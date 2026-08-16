"""Entrypoint: run the platform API server.

Usage:
    python -m experiment_platform.backend.server            # serve API + built web (if present)
"""
from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="5G Energy Experiment Platform")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    from .config import load_settings
    s = load_settings()
    host = args.host or s.listen_host
    port = args.port or s.listen_port
    print(f"5G Energy Experiment Platform -> http://{host}:{port}")
    print(f"  data dir: {s.data_dir}")
    print(f"  OAI base : {s.oai_base_url}")
    uvicorn.run("experiment_platform.backend.api:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
