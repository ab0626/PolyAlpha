#!/usr/bin/env python3
"""Freeze and verify the Kalshi PRODUCTION execution domain (LIVE_CALIBRATION_V0).

A separate integrity domain from the demo adapter (``freeze_kalshi_exec.py``)
and demo control plane (``freeze_kalshi_oms.py``).

``live-calibration-v0`` — frozen production execution scaffold.

Pins the production adapter, the production control plane, and every runtime
dependency they import (the shared auth/kill-switch/lease/journal modules plus
the demo adapter/OMS whose data classes and state machine the prod path reuses).
Only runtime drift fails verification; tests are recorded for provenance but are
non-blocking (we must be able to add safety tests without "changing the frozen
runtime").

Rule: a runtime change requires explicit review + re-freeze; docs/tests/smoke
improvements do not.

Usage:
    .venv/Scripts/python.exe scripts/freeze_kalshi_exec_prod.py freeze
    .venv/Scripts/python.exe scripts/freeze_kalshi_exec_prod.py verify
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "frozen" / "live-calibration-v0.json"
DEFAULT_VERSION = "live-calibration-v0"

# Runtime files whose drift BLOCKS verification (production execution semantics).
RUNTIME_FILES = (
    "src/polyalpha/kalshi_exec_prod.py",
    "src/polyalpha/kalshi_oms_prod.py",
    "src/polyalpha/kalshi_auth.py",
    "src/polyalpha/kill_switch.py",
    "src/polyalpha/process_lease.py",
    "src/polyalpha/execution_journal.py",
    "src/polyalpha/kalshi_exec.py",
    "src/polyalpha/kalshi_oms.py",
)
# Informational only — never blocking.
TESTS_FILES = (
    "tests/test_kalshi_exec_prod.py",
    "tests/test_kalshi_oms_prod.py",
)


def _sha256_of_files(paths: tuple[str, ...]) -> str:
    h = hashlib.sha256()
    for rel in sorted(paths):
        p = ROOT / rel
        if not p.exists():
            raise FileNotFoundError(f"missing pinned file: {rel}")
        h.update(p.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()


def _git_head() -> str:
    return (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT)
        .decode()
        .strip()
    )


def freeze(version: str, commit: str | None, config: Path) -> None:
    data = {
        "version": version,
        "anchor_commit": commit or _git_head(),
        "runtime_sha256": _sha256_of_files(RUNTIME_FILES),
        "tests_sha256": _sha256_of_files(TESTS_FILES),
        "environment": "production",
        "production_execution": True,
        "signal": "E3",
        "api_family": "kalshi_trade_api_v2",
        "caps": {
            "max_notional_per_order": "2.00",
            "min_conservative_edge_per_contract": "0.05",
            "daily_loss_stop": "10.00",
            "bankroll_cap": "50.00",
        },
        "runtime_files": list(RUNTIME_FILES),
        "frozen_at": datetime.now(UTC).isoformat(),
    }
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Frozen {version} -> {config}")
    for key in (
        "anchor_commit",
        "runtime_sha256",
        "environment",
        "production_execution",
        "signal",
        "api_family",
    ):
        print(f"  {key}: {data[key]}")


def verify(config: Path) -> int:
    if not config.exists():
        print(f"FAIL: no frozen config at {config} — run freeze first")
        return 1
    data = json.loads(config.read_text(encoding="utf-8"))
    version = data.get("version", "?")
    expected = data.get("runtime_sha256")
    if not expected:
        print("FAIL: frozen config has no runtime_sha256")
        return 1

    try:
        actual = _sha256_of_files(RUNTIME_FILES)
    except FileNotFoundError as exc:
        print(f"FAIL: {exc}")
        return 1

    if actual != expected:
        print(f"FAIL runtime_sha256: frozen={expected}")
        print(f"                     actual={actual}")
        print(f"  runtime drift in {version}: requires explicit review + re-freeze")
        return 1
    print(f"ok   runtime_sha256: {actual}")

    try:
        tests_actual = _sha256_of_files(TESTS_FILES)
    except FileNotFoundError:
        tests_actual = "<missing>"
    tests_expected = data.get("tests_sha256")
    tag = "ok" if tests_actual == tests_expected else "DRIFTED (non-blocking)"
    print(f"info tests_sha256: {tag}  {tests_actual}")
    print(
        f"info environment={data.get('environment')} "
        f"production_execution={data.get('production_execution')} "
        f"signal={data.get('signal')} api_family={data.get('api_family')} "
        f"anchor={data.get('anchor_commit')}"
    )
    print(f"info caps={data.get('caps')}")
    print(f"\n{version} — frozen production execution scaffold: OK")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Freeze/verify the Kalshi production execution domain"
    )
    ap.add_argument("action", choices=["freeze", "verify"])
    ap.add_argument("--version", default=DEFAULT_VERSION)
    ap.add_argument("--commit", default=None)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = ap.parse_args()
    config = Path(args.config)
    if args.action == "freeze":
        freeze(args.version, args.commit, config)
    else:
        sys.exit(verify(config))


if __name__ == "__main__":
    main()