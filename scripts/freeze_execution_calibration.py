#!/usr/bin/env python3
"""Freeze/verify EXECUTION_CALIBRATION_V1 (live taker calibration contract).

Separate from the terminal-alpha gate, the microstructure evidence freezes, and
the execution freezes. Pins the $2 taker-only Kalshi-energy calibration policy
before any systematic fill.

Usage:
    .venv/Scripts/python.exe scripts/freeze_execution_calibration.py freeze
    .venv/Scripts/python.exe scripts/freeze_execution_calibration.py verify
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from freeze_common import verify_anchor

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "frozen" / "execution-calibration-v1.json"
DEFAULT_VERSION = "execution-calibration-v1"

RUNTIME_FILES = (
    "docs/EXECUTION_CALIBRATION_V1.md",
    "scripts/execution_calibration_modeb.py",
)


def _sha256_of_files(paths):
    h = hashlib.sha256()
    for rel in sorted(paths):
        p = ROOT / rel
        if not p.exists():
            raise FileNotFoundError(f"missing pinned file: {rel}")
        h.update(p.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()


def _git_head():
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()


def freeze(version, commit, config):
    data = {
        "version": version,
        "anchor_commit": commit or _git_head(),
        "contract_sha256": _sha256_of_files(RUNTIME_FILES),
        "research_spec": "docs/EXECUTION_CALIBRATION_V1.md",
        "contract_files": list(RUNTIME_FILES),
        "frozen_at": datetime.now(UTC).isoformat(),
    }
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Frozen {version} -> {config}")
    for k in ("anchor_commit", "contract_sha256", "research_spec"):
        print(f"  {k}: {data[k]}")


def verify(config):
    if not config.exists():
        print(f"FAIL: no frozen config at {config} — run freeze first")
        return 1
    data = json.loads(config.read_text(encoding="utf-8"))
    expected = data.get("contract_sha256")
    actual = _sha256_of_files(RUNTIME_FILES)
    if actual != expected:
        print(f"FAIL contract_sha256: frozen={expected} actual={actual}")
        return 1
    print(f"ok   contract_sha256: {actual}")
    if not verify_anchor(RUNTIME_FILES, data.get("anchor_commit"), expected):
        return 1
    print(f"\n{data.get('version')} — frozen execution-calibration contract: OK")
    return 0


def main():
    ap = argparse.ArgumentParser()
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