"""Freeze and verify the PolyAlpha research baseline.

Part of the v0.3.0 RESEARCH BASELINE FREEZE. Records integrity hashes for
the frozen configuration and can verify that a working tree still matches
the frozen baseline (model source, feature schema, config file).

Usage:
    python scripts/freeze_baseline.py freeze   # (re)compute + write hashes
    python scripts/freeze_baseline.py verify   # verify tree against frozen hashes
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "frozen" / "v0.3.0-baseline.yaml"
INTEGRITY = "integrity:"

# ── Explicit hash domains ─────────────────────────────────────────────────
# The model/research hash must NOT depend on every file under src/polyalpha.
# Define three disjoint domains with a clean invariant:
#
#   collector may evolve for correctness fixes
#   interface may evolve
#   BUT research_logic_sha256 must equal the REAL_DATA_START value.
#
# research_logic_sha256 : the frozen research decision logic
# collector_sha256      : data-acquisition infra (fixable during burn-in)
# interface_sha256      : CLI / dashboard / reporting glue
RESEARCH_PATHS = (
    "src/polyalpha/models",
    "src/polyalpha/features",
    "src/polyalpha/forecasting.py",
    "src/polyalpha/calibration.py",
    "src/polyalpha/uncertainty.py",
    "src/polyalpha/signals.py",
    "src/polyalpha/sizing.py",
    "src/polyalpha/expected_value.py",
    "src/polyalpha/risk.py",
    "src/polyalpha/relative_value.py",
    "src/polyalpha/graph.py",
    "src/polyalpha/execution.py",
    "src/polyalpha/backtest.py",
    "src/polyalpha/walk_forward.py",
    "src/polyalpha/domain.py",
    "src/polyalpha/research_dataset.py",
    "src/polyalpha/parsing.py",
    "config/frozen/v0.3.0-baseline.yaml",
)
COLLECTOR_PATHS = (
    "src/polyalpha/rawstore.py",
    "src/polyalpha/market_collector.py",
    "src/polyalpha/reconciler.py",
    "src/polyalpha/market_metadata.py",
    "src/polyalpha/collector_health.py",
    "src/polyalpha/daily_manifest.py",
    "src/polyalpha/collection.py",
)
INTERFACE_PATHS = (
    "src/polyalpha/cli.py",
    "src/polyalpha/dashboard",
    "src/polyalpha/dashboard.py",
)


def _config_content_without_self_hash() -> bytes:
    """Config bytes with the integrity section removed.

    research_logic_sha256 covers the *frozen parameters* of the config, not
    the integrity metadata block. Hashing the parameters without the integrity
    block avoids the circular dependency where writing the hash changes it.
    """
    text = CONFIG.read_text(encoding="utf-8")
    stripped = re.sub(re.escape(INTEGRITY) + r"[\s\S]*$", "", text, count=1)
    return stripped.encode("utf-8")


def _sha256_of_files(rel_paths: list[Path], exclude: set[str] | None = None) -> str:
    h = hashlib.sha256()
    for p in sorted(rel_paths, key=lambda x: str(x)):
        if exclude and p.name in exclude:
            continue
        if p == CONFIG:
            # The frozen config hash must not depend on its own mutable
            # config_sha256 line; otherwise writing the hash changes it.
            h.update(_config_content_without_self_hash())
        else:
            h.update(p.read_bytes())
    return h.hexdigest()


def _collect(paths: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for entry in paths:
        p = ROOT / entry
        if p.is_dir():
            files.extend(sorted(p.rglob("*.py")))
        elif p.exists():
            files.append(p)
    return files


def _git_commit() -> str:
    return (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT)
        .decode()
        .strip()
    )


def _config_sha256() -> str:
    # Hash the config file with the self-referential config_sha256 line
    # removed, so writing the hash does not change what is hashed.
    text = CONFIG.read_text(encoding="utf-8")
    stripped = re.sub(r'^  config_sha256:.*$', "", text, count=1, flags=re.MULTILINE)
    return hashlib.sha256(stripped.encode("utf-8")).hexdigest()


def _parse_integrity() -> dict[str, str]:
    text = CONFIG.read_text(encoding="utf-8")
    section = text.split(INTEGRITY, 1)[1]
    values: dict[str, str] = {}
    for line in section.splitlines():
        m = re.match(r"\s{2}(\w+):\s*\"?([^\"]*)\"?$", line)
        if m:
            values[m.group(1)] = m.group(2).strip()
    return values


def _write_config(integrity: dict[str, str]) -> None:
    text = CONFIG.read_text(encoding="utf-8")
    block = "\n".join(
        [INTEGRITY]
        + [f'  {k}: "{v}"' for k, v in integrity.items()]
    )
    new_text = re.sub(
        re.escape(INTEGRITY) + r"[\s\S]*$",
        block + "\n",
        text,
        count=1,
    )
    CONFIG.write_text(new_text, encoding="utf-8")


def freeze() -> None:
    # Write all integrity fields with config_sha256 blank first, because the
    # config hash must cover the written values (git_commit etc.), not the
    # stale values already on disk from a previous freeze.
    integrity = {
        "git_commit": _git_commit(),
        "git_tag": "v0.3.0-research-baseline",
        "research_logic_sha256": _sha256_of_files(_collect(RESEARCH_PATHS)),
        "collector_sha256": _sha256_of_files(_collect(COLLECTOR_PATHS)),
        "interface_sha256": _sha256_of_files(_collect(INTERFACE_PATHS)),
        "config_sha256": "",
    }
    _write_config(integrity)
    integrity["config_sha256"] = _config_sha256()
    _write_config(integrity)
    print("Frozen baseline written to", CONFIG)
    for k, v in integrity.items():
        print(f"  {k}: {v}")


def verify() -> int:
    frozen = _parse_integrity()
    # The frozen commit records what the model/schema state was when frozen.
    # HEAD legitimately moves when the freeze config itself is committed, so
    # the git_commit check only requires that the frozen commit still exists.
    frozen_commit = frozen.get("git_commit", "")
    checks = [
        (
            "git_commit",
            frozen_commit,
            "working tree HEAD has changed since freeze",
        ),
        (
            "research_logic_sha256",
            _sha256_of_files(_collect(RESEARCH_PATHS)),
            "research logic differs from frozen baseline",
        ),
        ("config_sha256", _config_sha256(), "frozen config file has been modified"),
    ]
    failures = 0
    for key, actual, msg in checks:
        expected = frozen.get(key)
        if expected is None or expected in ("PENDING", "SEE scripts/freeze_baseline.py"):
            print(f"  MISSING frozen {key} — run freeze first")
            failures += 1
            continue
        if key == "git_commit":
            # Verify the frozen commit exists; do not require it to equal HEAD,
            # because committing the freeze config legitimately advances HEAD.
            try:
                subprocess.check_call(
                    ["git", "cat-file", "-e", f"{actual}^{{commit}}"],
                    cwd=ROOT,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                print(f"  ok   {key}: {actual}")
            except subprocess.CalledProcessError:
                print(f"  FAIL {key}: frozen commit {actual} no longer exists")
                failures += 1
            continue
        if actual != expected:
            print(f"  FAIL {key}: frozen={expected} actual={actual} ({msg})")
            failures += 1
        else:
            print(f"  ok   {key}: {actual}")
    if failures:
        print(f"\n{failures} integrity check(s) failed.")
        return 1
    print("\nAll integrity checks passed.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Freeze/verify research baseline")
    parser.add_argument("action", choices=["freeze", "verify"])
    args = parser.parse_args()
    if args.action == "freeze":
        freeze()
    else:
        sys.exit(verify())


if __name__ == "__main__":
    main()