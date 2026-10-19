#!/usr/bin/env python3
"""E3 -> screen-edge dry-run (LOG-ONLY, never submits).

Builds the frozen E3 model from local ERCOT data and the current Kalshi energy
ladder, computes screen edges, and logs the FULL denominator (every strike,
whether or not it would be traded). It never places an order.

The fee rule is UNVERIFIED by default, so ``build_signal_intents`` emits nothing
(fail-closed). The diagnostic table below shows the pre-fee raw edge and the
would-be screen edge under the *inferred* (unverified) ERCOT fee rule, so one
eligible candidate can be inspected end-to-end WITHOUT any money moving.

Usage:
    .venv/Scripts/python.exe scripts/kalshi_prod_signal_dryrun.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from energy_e3_runner import ACTUALS, FORECAST_VINTAGES, _load_actuals, _load_forecast  # noqa: E402

from polyalpha.energy.kalshi import KalshiPublicClient, build_ladder  # noqa: E402
from polyalpha.energy.physical import ForecastErrorModel  # noqa: E402
from polyalpha.energy.shadow_observation import MAX_LEAD, MIN_RESIDUALS  # noqa: E402
from polyalpha.kalshi_exec_prod import MAX_NOTIONAL_PER_ORDER, MIN_SCREEN_EDGE  # noqa: E402
from polyalpha.kalshi_fees import (  # noqa: E402
    FEE_MAKER_RATE_PUBLISHED,
    FEE_TAKER_RATE_PUBLISHED,
    SLIPPAGE_ADVERSE_SELECTION_BUFFER,
    FeeRule,
    fee_per_contract,
)
from polyalpha.kalshi_signal_e3 import build_signal_intents  # noqa: E402

SERIES = "KXTXERCOTPEAKD"
D = Decimal


def inferred_ercot_rule() -> FeeRule:
    """UNVERIFIED inferred ERCOT fee rule: taker M=1, maker M=0 (default)."""
    return FeeRule(
        series_ticker=SERIES,
        taker_rate=FEE_TAKER_RATE_PUBLISHED * D(1),
        maker_rate=FEE_MAKER_RATE_PUBLISHED * D(0),
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
        print("no active ERCOT markets found.")
        return 1

    ladder = build_ladder(SERIES, markets)
    fee_rule = inferred_ercot_rule()

    # Fail-closed path: unverified fee rule => no intents.
    intents = build_signal_intents(model, ladder, fee_rule, as_of)
    print("=== E3 -> SCREEN-EDGE DRY-RUN (log-only) ===")
    print(f"operating_day: {ladder.underlying.operating_day or '?'}")
    print(f"rungs: {len(ladder.rungs)}")
    print(f"fee rule verified: {fee_rule.verified}  (unverified => 0 tradeable intents)")
    print(f"tradeable intents (fail-closed): {len(intents)}")
    print()

    print("diagnostic (pre-fee raw edge + would-be screen edge, UNVERIFIED — not tradeable):")
    print(f"{'strike':>8} {'p_model':>8} {'yes_ask':>8} {'raw_yes':>8} {'screen_yes':>10} {'no_ask':>8} {'raw_no':>8}")
    maker_rate = fee_rule.maker_rate
    any_eligible = False
    for r in ladder.rungs:
        p_model = model.p_peak_gt(ladder.underlying.operating_day or "", as_of, r.threshold)
        if p_model is None:
            continue
        raw_yes = p_model - r.yes_ask if r.yes_ask is not None else None
        raw_no = (D(1) - p_model) - r.no_ask if r.no_ask is not None else None
        screen_yes = (
            p_model - r.yes_ask - fee_per_contract(r.yes_ask, maker_rate) - SLIPPAGE_ADVERSE_SELECTION_BUFFER
            if r.yes_ask is not None
            else None
        )
        if (screen_yes is not None and screen_yes >= MIN_SCREEN_EDGE) or (
            raw_no is not None and (D(1) - p_model) - r.no_ask - SLIPPAGE_ADVERSE_SELECTION_BUFFER >= MIN_SCREEN_EDGE
        ):
            any_eligible = True
        def f(v):  # noqa: E306
            return "      -" if v is None else f"{v:8.4f}"
        print(
            f"{r.threshold:>8} {f(p_model)} {f(r.yes_ask)} {f(raw_yes)} {f(screen_yes)} {f(r.no_ask)} {f(raw_no)}"
        )

    print()
    print(f"min screen edge: ${MIN_SCREEN_EDGE}/contract  "
          f"buffer B: ${SLIPPAGE_ADVERSE_SELECTION_BUFFER}/contract  "
          f"max notional: ${MAX_NOTIONAL_PER_ORDER}")
    print(f"eligible under inferred (unverified) rule: {'YES' if any_eligible else 'no'}")
    print("\nNo order placed. Verify the fee rule against the live venue before any intent is tradeable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())