#!/usr/bin/env python3
"""Freeze and verify the Kalshi demo execution harness (control-plane mechanics).

EXECUTION-CONTROL-PLANE FREEZE — separate from the venue-adapter freeze
(``freeze_kalshi_exec.py``) and the research/collector freezes.

``kalshi-oms-demo-v0.5`` — frozen demo automated execution control plane.

Pins the harness state machine AND the CLI runner (both are runtime
control-plane code): the ambiguity invariant (UNKNOWN/unresolved state blocks
new intents, with reconcile-backoff and kill-switch halt), the interrupt policy
(interrupt during an active state arms the kill switch), the kill-switch clear
policy (--clear-kill-switch clears ONLY the initial fail-closed arm, never a
later MANUAL_OPERATOR arm), and the durability layer (OS-held process lease +
write-ahead execution journal + startup recovery). Only runtime drift fails
verification:

    src/polyalpha/kalshi_oms.py
    src/polyalpha/process_lease.py
    src/polyalpha/execution_journal.py
    scripts/kalshi_oms_demo.py

The venue adapter (``kalshi_exec.py`` / ``kalshi_auth.py`` / ``kill_switch.py``)
remains independently frozen at ``kalshi-exec-demo-v0.3``. Tests are operational
tooling and are not part of this runtime hash.

Usage:
    .venv/Scripts/python.exe scripts/freeze_kalshi_oms.py freeze
    .venv/Scripts/python.exe scripts/freeze_kalshi_oms.py verify
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
DEFAULT_CONFIG = ROOT / "config" / "frozen" / "kalshi-oms-demo-v0.5.json"
DEFAULT_VERSION = "kalshi-oms-demo-v0.5"

RUNTIME_FILES = (
    "src/polyalpha/kalshi_oms.py",
    "src/polyalpha/process_lease.py",
    "src/polyalpha/execution_journal.py",
    "scripts/kalshi_oms_demo.py",
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
        "environment": "demo",
        "production_execution": False,
        "signal": "NULL",
        "runtime_files": list(RUNTIME_FILES),
        "frozen_at": datetime.now(UTC).isoformat(),
    }
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Frozen {version} -> {config}")
    for key in ("anchor_commit", "runtime_sha256", "environment", "signal"):
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
    print(
        f"info environment={data.get('environment')} "
        f"production_execution={data.get('production_execution')} "
        f"signal={data.get('signal')} anchor={data.get('anchor_commit')}"
    )
    print(f"\n{version} — frozen demo automated execution control plane: OK")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Freeze/verify the Kalshi demo execution harness"
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