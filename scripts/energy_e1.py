"""Run Family E1 (energy threshold calibration) once per the frozen spec.

    .venv/Scripts/python.exe scripts/energy_e1.py

Writes the report to data/energy/reports/e1-<timestamp>.json and prints it.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.energy.e1 import evaluate  # noqa: E402
from polyalpha.energy.kalshi import KalshiPublicClient  # noqa: E402


def main() -> int:
    client = KalshiPublicClient()
    report = evaluate(client)

    out_dir = ROOT / "data" / "energy" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"e1-{stamp}.json"
    path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")

    print(json.dumps(report, indent=2, default=str))
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())