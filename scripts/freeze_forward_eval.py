#!/usr/bin/env python3
"""Freeze and verify the US forward-evaluation runtime.

FORWARD-EVALUATION FREEZE — a dedicated integrity domain for the research logic
that decides whether the Polymarket US forward-evidence gate opens and what
verdict each family receives. Separate from the international baseline freeze
(``freeze_baseline.py``), the US collector freeze (``freeze_us_collector.py``),
and the execution freezes.

``us-forward-eval-v0.1`` — frozen forward-evaluation implementation (post-v2.3).

Pins the family evaluators, the evaluation-boundary/gate logic, the dataset
builder, the statistical-test helper, and the authoritative preregistration spec
(which the v2.3 clarification addendum makes unambiguous). Only runtime drift
fails verification; tests are recorded for provenance but are non-blocking.

Usage:
    .venv/Scripts/python.exe scripts/freeze_forward_eval.py freeze
    .venv/Scripts/python.exe scripts/freeze_forward_eval.py verify
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
DEFAULT_CONFIG = ROOT / "config" / "frozen" / "us-forward-eval-v0.1.json"
DEFAULT_VERSION = "us-forward-eval-v0.1"

RUNTIME_FILES = (
    "src/polyalpha/us/families.py",
    "src/polyalpha/us/forward_evidence.py",
    "src/polyalpha/us/research.py",
    "src/polyalpha/research_dataset.py",
    "src/polyalpha/statistical_tests.py",
    "docs/RESEARCH_AGENDA.md",
)
TESTS_FILES = (
    "tests/test_us_families.py",
    "tests/test_us_research.py",
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
        "spec": "docs/RESEARCH_AGENDA.md v2.0 + v2.1 + v2.2 + v2.3",
        "runtime_files": list(RUNTIME_FILES),
        "frozen_at": datetime.now(UTC).isoformat(),
    }
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Frozen {version} -> {config}")
    for key in ("anchor_commit", "runtime_sha256", "spec"):
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
    print(f"info spec={data.get('spec')} anchor={data.get('anchor_commit')}")
    print(f"\n{version} — frozen forward-evaluation runtime: OK")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Freeze/verify the US forward-evaluation runtime")
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