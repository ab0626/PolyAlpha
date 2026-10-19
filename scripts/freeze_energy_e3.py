#!/usr/bin/env python3
"""Freeze and verify the E3 (physical-model residual) research methodology.

ENERGY-E3-RESEARCH FREEZE — separate from the demo execution freezes
(``freeze_kalshi_exec.py`` / ``freeze_kalshi_oms.py``) and the US research /
collector freezes.

``energy-e3-research-v0.1`` — frozen E3 runtime methodology.

The E3 observation rule, residual construction, edge definition, and resolution
logic are pre-registered in ``docs/ENERGY_E3_SPEC.md`` (v3) and
``docs/ENERGY_E3_BASELINE_V0.md``. ``MODEL_VERSION = "e3-baseline-v0.1"`` pins
the *identity* of observations, but not the code that produces them — so this
domain hashes the runtime files so a silent methodology edit (sampling rule,
residual smoothing, fees, observation identity, or resolution) fails
verification even though the frozen model version string is unchanged:

    scripts/energy_e3_runner.py
    src/polyalpha/energy/shadow_observation.py
    src/polyalpha/energy/physical.py
    src/polyalpha/energy/e3.py
    src/polyalpha/energy/kalshi.py

Shared schema/taxonomy/ledger modules (``energy/schemas.py``,
``energy/taxonomy.py``, ``energy/ledger.py``) are deliberately NOT pinned here:
they are shared across E1/capture/WS and are not E3-specific methodology. Any
drift in the pinned set fails verification and requires explicit review plus a
new E3 research version (re-freeze). The E3 runner is measurement-only: it never
trades and never emits an alpha conclusion.

Usage:
    .venv/Scripts/python.exe scripts/freeze_energy_e3.py freeze
    .venv/Scripts/python.exe scripts/freeze_energy_e3.py verify
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
DEFAULT_CONFIG = ROOT / "config" / "frozen" / "energy-e3-research-v0.2.json"
DEFAULT_VERSION = "energy-e3-research-v0.2"

# E3 runtime methodology: the runner plus the physical model, observation
# identity, edge definition, and market-side semantics it executes.
RUNTIME_FILES = (
    "scripts/energy_e3_runner.py",
    "src/polyalpha/energy/shadow_observation.py",
    "src/polyalpha/energy/physical.py",
    "src/polyalpha/energy/e3.py",
    "src/polyalpha/energy/kalshi.py",
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
        "model_version": "e3-baseline-v0.2",
        "research_spec": "docs/ENERGY_E3_SPEC.md (v3)",
        "runtime_files": list(RUNTIME_FILES),
        "frozen_at": datetime.now(UTC).isoformat(),
    }
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Frozen {version} -> {config}")
    for key in ("anchor_commit", "runtime_sha256", "model_version", "research_spec"):
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
    print(
        f"info model_version={data.get('model_version')} "
        f"spec={data.get('research_spec')} anchor={data.get('anchor_commit')}"
    )
    print(f"\n{version} — frozen E3 research methodology: OK")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Freeze/verify the E3 research methodology"
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