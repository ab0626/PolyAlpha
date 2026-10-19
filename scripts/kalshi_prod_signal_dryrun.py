#!/usr/bin/env python3
"""E3 -> screen-edge dry-run (LOG-ONLY, never submits).

Builds the frozen E3 model from local ERCOT data and a SINGLE operating day's
Kalshi ERCOT ladder, evaluates every (strike, side) with the full fail-closed
checks, and prints the per-strike table. It never places an order.

The fee rule is UNVERIFIED, so ``build_signal_intents`` emits ZERO intents
(fail-closed). The table below is computed with a display-only verified copy of
the inferred ERCOT rule so ``taker_fee`` / ``screen_edge`` are visible for
inspection — those rows are ILLUSTRATIVE and not tradeable until the fee rule is
verified against the live venue.

Usage:
    .venv/Scripts/python.exe scripts/kalshi_prod_signal_dryrun.py
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from energy_e3_runner import ACTUALS, FORECAST_VINTAGES, _load_actuals, _load_forecast  # noqa: E402

from polyalpha.energy.kalshi import KalshiPublicClient, ercot_operating_day  # noqa: E402
from polyalpha.energy.physical import ForecastErrorModel  # noqa: E402
from polyalpha.energy.shadow_observation import MAX_LEAD, MIN_RESIDUALS  # noqa: E402
from polyalpha.kalshi_exec_prod import MAX_NOTIONAL_PER_ORDER, MIN_SCREEN_EDGE  # noqa: E402
from polyalpha.kalshi_fees import (  # noqa: E402
    FEE_MAKER_RATE_PUBLISHED,
    FEE_TAKER_RATE_PUBLISHED,
    SLIPPAGE_ADVERSE_SELECTION_BUFFER,
    FeeRule,
    verify_fee_rule,
)
from polyalpha.kalshi_signal_e3 import (  # noqa: E402
    build_signal_intents,
    build_single_day_ladder,
    evaluate_ladder,
)

SERIES = "KXTXERCOTPEAKD"


def _pick_upcoming_day(markets: list[dict], now: datetime) -> tuple[str, list[dict]]:
    """Pick the operating day with the nearest future close time (active day)."""
    by_day: dict[str, list[dict]] = defaultdict(list)
    day_close: dict[str, datetime] = {}
    for m in markets:
        close = m.get("close_time")
        if not close:
            continue
        day = ercot_operating_day(close)
        by_day[day].append(m)
        try:
            dt = datetime.fromisoformat(close.replace("Z", "+00:00"))
        except ValueError:
            continue
        day_close[day] = max(day_close.get(day, dt), dt)
    future = [d for d in day_close if day_close[d] > now]
    if future:
        day = min(future, key=lambda d: day_close[d])
    elif day_close:
        day = max(day_close, key=lambda d: day_close[d])
    else:
        raise ValueError("no markets with a close_time")
    return day, by_day[day]


def inferred_ercot_rule() -> FeeRule:
    """UNVERIFIED inferred ERCOT fee rule: taker M=1, maker M=0 (default)."""
    return FeeRule(
        series_ticker=SERIES,
        taker_rate=FEE_TAKER_RATE_PUBLISHED,
        maker_rate=FEE_MAKER_RATE_PUBLISHED * Decimal(0),
        source="docs.kalshi.com fee schedule (inferred; NOT verified against live venue)",
    )


def main() -> int:
    as_of = datetime.now(UTC)
    actuals = _load_actuals(ACTUALS)
    forecast_peaks, _first = _load_forecast(FORECAST_VINTAGES)
    model = ForecastErrorModel(actuals, forecast_peaks, max_lead=MAX_LEAD, min_residuals=MIN_RESIDUALS)

    try:
        client = KalshiPublicClient()
        body = client.markets(series_ticker=SERIES, limit=1000)
        markets = body.get("markets", [])
    except Exception as exc:  # noqa: BLE001 — read-only best-effort
        print(f"ladder fetch failed: {exc}")
        return 1
    if not markets:
        print("no ERCOT markets found.")
        return 1

    unverified = inferred_ercot_rule()
    day, day_markets = _pick_upcoming_day(markets, as_of)
    _, ladder = build_single_day_ladder(day_markets, SERIES, target_day=day)

    # Fail-closed: unverified fee => 0 tradeable intents.
    intents = build_signal_intents(model, ladder, unverified, as_of)

    # Display-only: a verified copy of the inferred rule so the table is readable.
    display_rule = verify_fee_rule(unverified)
    evals = evaluate_ladder(model, ladder, display_rule, as_of)

    print("=== E3 -> SCREEN-EDGE DRY-RUN (log-only, taker calibration) ===")
    print(f"operating_day: {day}")
    print(f"rungs: {len(ladder.rungs)}")
    print(f"fee rule verified (live): {unverified.verified}")
    print(f"tradeable intents (fail-closed): {len(intents)}")
    print()
    print("NOTE: table uses a display-only verified copy of the inferred fee rule.")
    print("      ELIGIBLE rows are ILLUSTRATIVE and NOT tradeable until live verification.")
    print()
    hdr = (
        f"{'strike':>8} {'side':>4} {'p_hat':>7} {'exec_px':>8} {'size':>8} "
        f"{'fee':>6} {'buffer':>6} {'edge':>8}  reason"
    )
    print(hdr)
    for e in evals:
        def f(v, w=7):
            return "     -" if v is None else f"{v:{w}.4f}"
        print(
            f"{e.strike:>8} {e.side:>4} {f(e.p_hat_e3, 7)} {f(e.executable_price, 8)} "
            f"{f(e.executable_size, 8)} {f(e.taker_fee, 6)} {f(e.buffer, 6)} "
            f"{f(e.screen_edge, 8)}  {e.eligibility_reason}"
        )

    print()
    print(f"min screen edge: ${MIN_SCREEN_EDGE}/contract  buffer B: ${SLIPPAGE_ADVERSE_SELECTION_BUFFER}/contract  "
          f"max notional: ${MAX_NOTIONAL_PER_ORDER}")
    print("\nNo order placed. Verify the fee rule against the live venue before any intent is tradeable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())