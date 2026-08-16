"""System acceptance check.

Verifies the whole deliverable is in place without needing hardware:
- design docs present
- backend imports + unit tests pass
- OAI API reachable (optional, skips cleanly if not)
- frontend build artifacts present (or reports "build needed")
- Android source tree present

Usage:  python verify_system.py
Exit 0 = all checks pass (or only soft-skips), non-zero = a hard check failed.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

HARD_CHECKS = {
    "SYSTEM_DESIGN.md": ROOT / "SYSTEM_DESIGN.md",
    "DATA_SCHEMA.md": ROOT / "DATA_SCHEMA.md",
    "EXPERIMENT_WORKFLOW.md": ROOT / "EXPERIMENT_WORKFLOW.md",
    "RUNBOOK.md": ROOT / "RUNBOOK.md",
    "backend/__init__.py": ROOT / "experiment_platform" / "backend" / "__init__.py",
    "android/README.md": ROOT / "android" / "README.md",
}

SOFT_CHECKS = {
    "frontend dist (build with: cd experiment_platform/web && npm run build)":
        ROOT / "experiment_platform" / "web" / "dist" / "index.html",
}


def main() -> int:
    failures = 0
    print("== 5G Energy Experiment System — acceptance check ==\n")

    print("[docs & structure]")
    for label, p in HARD_CHECKS.items():
        ok = p.exists()
        print(f"  {'OK ' if ok else 'MISS'}  {label}  ({p})")
        if not ok:
            failures += 1

    print("\n[backend import]")
    try:
        subprocess.run([sys.executable, "-c", "import experiment_platform.backend.api"],
                       cwd=ROOT, check=True, capture_output=True)
        print("  OK   experiment_platform.backend.api imports")
    except subprocess.CalledProcessError as e:
        print("  FAIL backend import:\n", e.stderr.decode(errors="replace"))
        failures += 1

    print("\n[backend unit tests]")
    r = subprocess.run([sys.executable, "-m", "pytest", "experiment_platform/backend/tests", "-q"],
                       cwd=ROOT, capture_output=True, text=True)
    print("  " + ("OK   " if r.returncode == 0 else "FAIL ") + (r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""))
    if r.returncode != 0:
        print(r.stdout[-1500:])
        failures += 1

    print("\n[soft checks (not required for backend correctness)]")
    for label, p in SOFT_CHECKS.items():
        ok = p.exists()
        print(f"  {'OK ' if ok else 'NOTE'}  {label}")

    print("\n[OAI reachability]")
    try:
        import experiment_platform.backend.oai_client as oc
        from experiment_platform.backend.config import load_settings
        cli = oc.OaiClient(load_settings())
        cli.health()
        print("  OK   OAI /api/health reachable")
        cli.close()
    except Exception as e:  # noqa: BLE001
        print(f"  SKIP OAI not reachable ({e})")

    print(f"\n== result: {'PASS' if failures == 0 else f'{failures} hard failure(s)'} ==")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
