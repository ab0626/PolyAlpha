"""E3 Baseline v0: official ERCOT forecast + lead-matched error vs Kalshi.

    .venv/Scripts/python.exe scripts/energy_e3_forecast_baseline.py

Builds the forecast-error baseline (ENERGY_E3_BASELINE_V0) and compares its
survival function to the active Kalshi ERCOT peak ladder, both executable
directions. Measurement only; no execution, no alpha claim.
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
    SEVEN_DAY_LOAD_FORECAST_BY_FORECAST_ZONE,
    forecast_peaks,
    historical_daily_peaks,
)
from polyalpha.energy.kalshi import KalshiPublicClient, build_ladder  # noqa: E402
from polyalpha.energy.physical import ForecastErrorModel  # noqa: E402

CACHE_DIR = ROOT / "data" / "energy"
SERIES = "KXTXERCOTPEAKD"


def load_actuals() -> dict[str, Decimal]:
    p = CACHE_DIR / "ercot_daily_peaks.json"
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
    else:
        data = {k: str(v) for k, v in
                historical_daily_peaks(ACTUAL_SYSTEM_LOAD_BY_FORECAST_ZONE, limit=60).items()}
        p.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    return {k: Decimal(v) for k, v in data.items()}


def load_forecasts(limit: int) -> dict[tuple[str, str], Decimal]:
    p = CACHE_DIR / "ercot_forecast_peaks.json"
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        return {tuple(k.split("|")): Decimal(v) for k, v in data.items()}
    raw = forecast_peaks(SEVEN_DAY_LOAD_FORECAST_BY_FORECAST_ZONE, limit=limit)
    data = {f"{a}|{b}": str(v) for (a, b), v in raw.items()}
    p.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    return raw


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--forecast-limit", type=int, default=60)
    args = ap.parse_args()

    actuals = load_actuals()
    forecasts = load_forecasts(args.forecast_limit)
    model = ForecastErrorModel(actuals, forecasts)

    print(f"actual days: {len(actuals)}  |  forecast vintages: {len(forecasts)}")
    print("residual counts by lead (days):", {k: len(v) for k, v in sorted(model.residuals.items())})

    client = KalshiPublicClient()
    body = client.markets(series_ticker=SERIES, limit=1000)
    active = [m for m in body.get("markets", []) if m.get("status") == "active"]
    by_day: dict[str, list[dict]] = {}
    for m in active:
        by_day.setdefault(m.get("event_ticker"), []).append(m)
    nearest = min(by_day, key=lambda d: min(m.get("close_time") or "9999" for m in by_day[d]))
    ladder = build_ladder(SERIES, by_day[nearest])

    now = datetime.now(UTC)
    target = ladder.underlying.operating_day or ""
    fc = model._current_forecast(target, now)
    print(f"target operating day: {target}")
    print(f"current forecast peak: {fc[0]} MW  (lead {fc[1]}d)" if fc else "no current forecast")

    print()
    print(f"{'strike':>7} {'p_model':>8} {'yes_bid':>8} {'yes_ask':>8} {'no_ask':>8} "
          f"{'edge_YES':>9} {'edge_NO':>9}")
    for r in build_opportunities(model, ladder, now):
        pm = float(r.p_model)
        yb = float(r.yes_bid)
        ya = float(r.yes_ask)
        na = float(r.no_ask) if r.no_ask else float("nan")
        ey = float(r.edge_yes) if r.edge_yes else float("nan")
        en = float(r.edge_no) if r.edge_no else float("nan")
        print(f"{r.strike:>7} {pm*100:>7.1f}% {yb*100:>7.1f}% {ya*100:>7.1f}% {na*100:>7.1f}% "
              f"{ey*100:>+8.1f}% {en*100:>+8.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())