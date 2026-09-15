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
MODEL_PATHS = ("src/polyalpha",)
# Collection-layer modules are data-acquisition infrastructure, not model
# logic. They are intentionally EXCLUDED from the frozen model hash so that
# fixing collector bugs during the burn-in does not invalidate the baseline.
MODEL_EXCLUDE = {
    "rawstore.py",
    "market_collector.py",
    "reconciler.py",
    "market_metadata.py",
    "collector_health.py",
    "daily_manifest.py",
    "collection.py",
}
FEATURE_PATHS = (
    "src/polyalpha/features",
    "src/polyalpha/domain.py",
    "src/polyalpha/research_dataset.py",
    "src/polyalpha/parsing.py",
)


def _sha256_of_files(rel_paths: list[Path], exclude: set[str] | None = None) -> str:
    h = hashlib.sha256()
    for p in sorted(rel_paths, key=lambda x: str(x)):
        if exclude and p.name in exclude:
            continue
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
        "model_source_sha256": _sha256_of_files(_collect(MODEL_PATHS), MODEL_EXCLUDE),
        "feature_schema_sha256": _sha256_of_files(_collect(FEATURE_PATHS)),
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
    checks = [
        ("git_commit", _git_commit(), "working tree HEAD has changed since freeze"),
        (
            "model_source_sha256",
            _sha256_of_files(_collect(MODEL_PATHS), MODEL_EXCLUDE),
            "model source differs from frozen baseline",
        ),
        (
            "feature_schema_sha256",
            _sha256_of_files(_collect(FEATURE_PATHS)),
            "feature schema differs from frozen baseline",
        ),
        ("config_sha256", _config_sha256(), "frozen config file has been modified"),
    ]
    failures = 0
    for key, actual, msg in checks:
        expected = frozen.get(key)
        if expected is None or expected in ("PENDING", "SEE scripts/freeze_baseline.py"):
            print(f"  MISSING frozen {key} — run freeze first")
            failures += 1
        elif actual != expected:
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