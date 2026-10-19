#!/usr/bin/env python3
"""Freeze and verify the Kalshi execution adapter (demo venue mechanics).

EXECUTION FREEZE — a separate integrity domain from the research freeze
(``freeze_baseline.py``) and the collector freeze (``COLLECTION_IMPL_COMMIT``).

``kalshi-exec-demo-v0.3`` — frozen demo venue-adapter mechanics.

This pins the runtime behavior that determines whether and how an order reaches
Kalshi (including cursor-paginated order reconciliation). Only runtime drift
fails verification:

    src/polyalpha/kalshi_exec.py
    src/polyalpha/kalshi_auth.py
    src/polyalpha/kill_switch.py

The tests hash is recorded for provenance but is NOT blocking (we must be able
to add safety tests without "changing the frozen adapter"). The smoke script is
operational tooling and is not hashed.

Rule: a runtime change requires explicit review and a new execution version /
re-freeze; operational docs, tests, and smoke-script improvements do not.

Usage:
    .venv/Scripts/python.exe scripts/freeze_kalshi_exec.py freeze
    .venv/Scripts/python.exe scripts/freeze_kalshi_exec.py verify
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
DEFAULT_CONFIG = ROOT / "config" / "frozen" / "kalshi-exec-demo-v0.3.json"
DEFAULT_VERSION = "kalshi-exec-demo-v0.3"

# Runtime files whose drift BLOCKS verification (execution semantics).
RUNTIME_FILES = (
    "src/polyalpha/kalshi_exec.py",
    "src/polyalpha/kalshi_auth.py",
    "src/polyalpha/kill_switch.py",
)
# Informational only — never blocking.
TESTS_FILE = "tests/test_kalshi_exec.py"


def _sha256_of_files(paths: tuple[str, ...] | list[str]) -> str:
    h = hashlib.sha256()
    for rel in sorted(paths):
        p = ROOT / rel
        if not p.exists():
            raise FileNotFoundError(f"missing pinned file: {rel}")
        # CRLF -> LF normalization so core.autocrlf does not cause a false fail
        # (matches scripts/freeze_baseline.py).
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
        "tests_sha256": _sha256_of_files([TESTS_FILE]),
        "environment": "demo",
        "production_execution": False,
        "api_family": "kalshi_trade_api_v2",
        "runtime_files": list(RUNTIME_FILES),
        "frozen_at": datetime.now(UTC).isoformat(),
    }
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Frozen {version} -> {config}")
    for key in (
        "anchor_commit",
        "runtime_sha256",
        "tests_sha256",
        "environment",
        "production_execution",
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
    if not verify_anchor(RUNTIME_FILES, data.get("anchor_commit"), expected):
        return 1

    # Informational, non-blocking.
    try:
        tests_actual = _sha256_of_files([TESTS_FILE])
    except FileNotFoundError:
        tests_actual = "<missing>"
    tests_expected = data.get("tests_sha256")
    tag = "ok" if tests_actual == tests_expected else "DRIFTED (non-blocking)"
    print(f"info tests_sha256: {tag}  {tests_actual}")
    print(
        f"info environment={data.get('environment')} "
        f"production_execution={data.get('production_execution')} "
        f"api_family={data.get('api_family')} "
        f"anchor={data.get('anchor_commit')}"
    )
    print(f"\n{version} — frozen demo venue-adapter mechanics: OK")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Freeze/verify the Kalshi execution adapter"
    )
    ap.add_argument("action", choices=["freeze", "verify"])
    ap.add_argument("--version", default=DEFAULT_VERSION,
                    help="execution version tag (freeze only)")
    ap.add_argument("--commit", default=None,
                    help="anchor commit (freeze only; default HEAD)")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG),
                    help="path to the frozen execution config")
    args = ap.parse_args()
    config = Path(args.config)
    if args.action == "freeze":
        freeze(args.version, args.commit, config)
    else:
        sys.exit(verify(config))


if __name__ == "__main__":
    main()