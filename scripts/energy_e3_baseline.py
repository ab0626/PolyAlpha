"""First E3 baseline: climatology P(ERCOT peak > K) vs live Kalshi ladder.

    .venv/Scripts/python.exe scripts/energy_e3_baseline.py [--history-days 180] [--window 60]

Fetches historical daily peak load (ERCOT actual system load, TOTAL column) via
the anonymous MIS path, builds the climatology baseline, then compares the model
survival function to the active Kalshi ERCOT peak ladder, reporting both
directions of executable edge per ENERGY_E3_SPEC.

This is a measurement spike, not a strategy: no execution, no alpha claim.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.energy.e3 import build_opportunities  # noqa: E402
from polyalpha.energy.ercot_mis import (  # noqa: E402
    ACTUAL_SYSTEM_LOAD_BY_FORECAST_ZONE,
    historical_daily_peaks,
)
from polyalpha.energy.kalshi import (  # noqa: E402
    KalshiPublicClient,
    build_ladder,
)
from polyalpha.energy.physical import ClimatologyPeakModel  # noqa: E402

CACHE = ROOT / "data" / "energy" / "ercot_daily_peaks.json"
SERIES = "KXTXERCOTPEAKD"


def load_peaks(history_days: int) -> dict[str, str]:
    if CACHE.exists():
        data = json.loads(CACHE.read_text(encoding="utf-8"))
        if len(data) >= history_days:
            return data
    peaks = historical_daily_peaks(ACTUAL_SYSTEM_LOAD_BY_FORECAST_ZONE, limit=history_days)
    data = {k: str(v) for k, v in peaks.items()}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--history-days", type=int, default=180)
    ap.add_argument("--window", type=int, default=60)
    args = ap.parse_args()

    peaks = load_peaks(args.history_days)
    print(f"historical daily peaks: {len(peaks)} days")

    model = ClimatologyPeakModel(
        {k: Decimal(v) for k, v in peaks.items()}, window_days=args.window
    )

    client = KalshiPublicClient()
    body = client.markets(series_ticker=SERIES, limit=1000)
    active = [m for m in body.get("markets", []) if m.get("status") == "active"]
    by_day: dict[str, list[dict]] = {}
    for m in active:
        by_day.setdefault(m.get("event_ticker"), []).append(m)
    nearest = min(by_day, key=lambda d: min(m.get("close_time") or "9999" for m in by_day[d]))
    ladder = build_ladder(SERIES, by_day[nearest])

    now = datetime.now(UTC)
    print(f"target operating day: {ladder.underlying.operating_day}")
    print(f"resolution: {(ladder.underlying.resolution_source or '')[:90]}".replace("\n", " "))
    print()
    print(f"{'strike':>7} {'p_model':>8} {'yes_bid':>8} {'yes_ask':>8} {'no_ask':>8} "
          f"{'edge_YES':>9} {'edge_NO':>9}")
    rows = build_opportunities(model, ladder, now)
    for r in rows:
        pm = float(r.p_model) if r.p_model else float("nan")
        yb = float(r.yes_bid); ya = float(r.yes_ask)
        na = float(r.no_ask) if r.no_ask else float("nan")
        ey = float(r.edge_yes) if r.edge_yes else float("nan")
        en = float(r.edge_no) if r.edge_no else float("nan")
        print(f"{r.strike:>7} {pm*100:>7.1f}% {yb*100:>7.1f}% {ya*100:>7.1f}% {na*100:>7.1f}% "
              f"{ey*100:>+8.1f}% {en*100:>+8.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())