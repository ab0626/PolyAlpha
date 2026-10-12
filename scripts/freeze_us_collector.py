#!/usr/bin/env python3
"""Freeze and verify the US collection runtime (acquisition integrity).

US-COLLECTOR FREEZE — a separate integrity domain from the research freeze,
the execution freezes, and the E3 research freeze.

``us-collector-v0.1`` — frozen US retail collection runtime.

``COLLECTION_IMPL_COMMIT`` (in ``us_collection_supervisor.py``) byte-compares
only ``scripts/run_us_collection.py`` at launch — a single-file pin. The actual
acquisition behavior also depends on the collector, REST client, adapter
parsing, identifiers, release coverage, the raw store, and the collection
config. This domain hashes that full runtime so a silent change to any of it
fails verification.

Collector/infrastructure changes are Class A (allowed) but must be committed and
re-frozen here; research/strategy changes are Class C and are NOT in this domain
(``us/research.py``, ``us/families.py``, ``us/forward_evidence.py``, etc. are
deliberately excluded). Shared cross-cutting modules (``domain.py``,
``external.py``, ``phases.py``) are also not pinned here.

Usage:
    .venv/Scripts/python.exe scripts/freeze_us_collector.py freeze
    .venv/Scripts/python.exe scripts/freeze_us_collector.py verify
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
DEFAULT_CONFIG = ROOT / "config" / "frozen" / "us-collector-v0.1.json"
DEFAULT_VERSION = "us-collector-v0.1"

# The full US collection runtime: runner + collector + adapter + transport +
# identifiers + states + release coverage + raw store + collection config.
RUNTIME_FILES = (
    "scripts/run_us_collection.py",
    "src/polyalpha/us/collector.py",
    "src/polyalpha/us/adapter.py",
    "src/polyalpha/us/rest.py",
    "src/polyalpha/us/identifiers.py",
    "src/polyalpha/us/states.py",
    "src/polyalpha/us/release_coverage.py",
    "src/polyalpha/gov_releases.py",
    "src/polyalpha/rawstore.py",
    "config/gov_releases.json",
    "config/collection_strata.json",
)


def _sha256_of_files(paths: tuple[str, ...]) -> str:
    h = hashlib.sha256()
    for rel in sorted(paths):
        p = ROOT / rel
        if not p.exists():
            raise FileNotFoundError(f"missing pinned file: {rel}")
        # CRLF -> LF normalization so core.autocrlf does not cause a false fail.
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
        "change_class": "A",
        "change_class_note": "collector/infra only; re-freeze after any committed Class-A change",
        "runtime_files": list(RUNTIME_FILES),
        "frozen_at": datetime.now(UTC).isoformat(),
    }
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Frozen {version} -> {config}")
    for key in ("anchor_commit", "runtime_sha256", "change_class"):
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
        print(f"  runtime drift in {version}: commit + re-freeze (Class-A change)")
        return 1
    print(f"ok   runtime_sha256: {actual}")
    print(
        f"info change_class={data.get('change_class')} "
        f"anchor={data.get('anchor_commit')}"
    )
    print(f"\n{version} — frozen US collection runtime: OK")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Freeze/verify the US collection runtime"
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