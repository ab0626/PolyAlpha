#!/usr/bin/env python3
"""Print the repo audit manifest (git SHA, phase, forward-evidence gate).

Keeps the README's audit manifest regenerable rather than hand-maintained.
The test count is omitted (run `pytest -q` for the exact number; collection is
slow) — this script covers the parts that drift fastest: commit, phase, and
the evidence gate.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()
    phase_path = ROOT / "data" / "us" / "phase.json"
    phase = json.loads(phase_path.read_text(encoding="utf-8"))["phase"] if phase_path.exists() else "unknown"

    from polyalpha.us.forward_evidence import forward_evidence_report  # noqa: E402
    from polyalpha.us.research import build_us_research_dataset  # noqa: E402

    dataset = build_us_research_dataset(ROOT / "data" / "us" / "retail" / "raw")
    report = forward_evidence_report(dataset)

    print(json.dumps({
        "git_sha": sha,
        "phase": phase,
        "alpha": "UNKNOWN",
        "venue": "polymarket_us",
        "gates": report["gates"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
