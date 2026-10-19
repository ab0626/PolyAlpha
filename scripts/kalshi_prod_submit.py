#!/usr/bin/env python3
"""LIVE_CALIBRATION_V0 production submit runner (restrictive, one-intent-per-run).

Default is dry-run (log-only). Mutation requires BOTH ``--execute`` AND
``--ticker`` AND ``--side``. The operator explicitly selects the ticker + side
surfaced by the dry-run.

Safety invariants:
  * one invocation => at most ONE mutation attempt;
  * never clears the operator gate or the kill switch (operator does that);
  * fresh GEN-533-UI realized running max fetched immediately before mutation;
  * fresh Kalshi book fetched immediately before mutation (revalidate_quote);
  * lead-0 signal timestamp + 6h age rule + verified taker fee rule;
  * same 5c screen / 2c buffer / $2 cap;
  * after a mutation attempt the kill switch is re-armed (never auto-cleared);
  * an ambiguous outcome arms the kill switch and stops (no second candidate).

Usage:
    dry-run:  .venv/Scripts/python.exe scripts/kalshi_prod_submit.py [--ticker KX...]
    execute:  .venv/Scripts/python.exe scripts/kalshi_prod_submit.py \
                  --ticker KX... --side NO --execute
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from energy_e3_runner import ACTUALS, FORECAST_VINTAGES, _load_actuals, _load_forecast  # noqa: E402

from polyalpha.energy.intraday_actuals import fetch_intraday_actuals, running_max  # noqa: E402
from polyalpha.energy.kalshi import KalshiPublicClient, ercot_operating_day  # noqa: E402
from polyalpha.energy.physical import ForecastErrorModel  # noqa: E402
from polyalpha.energy.shadow_observation import MAX_LEAD, MIN_RESIDUALS  # noqa: E402
from polyalpha.kalshi_auth import load_env_file  # noqa: E402
from polyalpha.kalshi_exec import OrderSide  # noqa: E402
from polyalpha.kalshi_exec_prod import (  # noqa: E402
    CALIBRATION_PROTOCOL_VERSION,
    KalshiExecProdClient,
)
from polyalpha.kalshi_fees import FeeRule, verify_fee_rule  # noqa: E402
from polyalpha.kalshi_oms import HarnessState  # noqa: E402
from polyalpha.kalshi_oms_prod import ProdExecutionHarness  # noqa: E402
from polyalpha.kalshi_signal_e3 import (  # noqa: E402
    build_single_day_ladder,
    evaluate_ladder,
    revalidate_quote,
)
from polyalpha.process_lease import ProcessLease  # noqa: E402

SERIES = "KXTXERCOTPEAKD"
DEFAULT_ENV_FILE = "C:/Users/adith/powermarkets.env"

# Durable OMS episode + cumulative risk state + singleton lease. These make the
# frozen daily-loss stop / bankroll cap and startup recovery HARD across
# one-intent process invocations, instead of resetting on every run.
PROD_JOURNAL = ROOT / "data" / "energy" / "kalshi" / "oms_prod_journal.jsonl"
PROD_RISK = ROOT / "data" / "energy" / "kalshi" / "experiment_risk.json"
PROD_LEASE = ROOT / "data" / "energy" / "kalshi" / "oms_prod_submit.lock"


def _parse_iso(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _pick_upcoming_day(markets: list[dict], now: datetime) -> tuple[str, list[dict]]:
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


def _verified_ercot_rule() -> FeeRule:
    """Verify the ERCOT fee rule against the live venue (fail closed).

    Fetches the live series fee schedule and records the returned schedule's
    identity (hash) plus the verification timestamp. On ANY fetch failure the
    rule is returned UNVERIFIED (verified_at=None), so every edge computation
    fails closed (no trade). Never mints a fresh verified_at from the wall clock.
    """
    taker = Decimal("0.07")
    maker = Decimal("0")
    try:
        client = KalshiPublicClient()
        body = client._get("/series/fee_changes", {"series_ticker": SERIES})
        schedule_hash = hashlib.sha256(
            json.dumps(body, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        source = (
            f"live GET /series/fee_changes {SERIES} (schedule_hash={schedule_hash[:12]})"
        )
        verified_at = datetime.now(UTC).isoformat()
    except Exception as exc:  # noqa: BLE001 — unverified on any fetch error
        schedule_hash = None
        source = f"UNVERIFIED (live fee schedule fetch failed: {type(exc).__name__})"
        verified_at = None
    return verify_fee_rule(
        FeeRule(series_ticker=SERIES, taker_rate=taker, maker_rate=maker, source=source),
        verified_at=verified_at,
        schedule_hash=schedule_hash,
    )


def _build_context(as_of: datetime):
    """Build model + single-day ladder + realized running max + signal timestamp."""
    actuals = _load_actuals(ACTUALS)
    forecast_peaks, first = _load_forecast(FORECAST_VINTAGES)
    model = ForecastErrorModel(actuals, forecast_peaks, max_lead=MAX_LEAD, min_residuals=MIN_RESIDUALS)

    markets = KalshiPublicClient().markets(series_ticker=SERIES, limit=1000).get("markets", [])
    day, day_markets = _pick_upcoming_day(markets, as_of)
    _, ladder = build_single_day_ladder(day_markets, SERIES, target_day=day)

    snap = fetch_intraday_actuals()
    realized = running_max(snap.rows, day) if snap is not None else None

    f0 = first.get((day, day))
    signal_ts = None
    if f0:
        pub = _parse_iso(f0.get("publish_at"))
        recv = _parse_iso(f0.get("received_at"))
        if pub is not None and recv is not None:
            signal_ts = max(pub, recv)

    return model, ladder, day, realized, signal_ts


def _print_evals(evals, ticker=None) -> None:
    hdr = f"{'strike':>8} {'side':>4} {'ticker':<28} {'p_hat':>7} {'exec_px':>8} {'size':>8} {'edge':>8}  reason"
    print(hdr)
    for e in evals:
        if ticker and e.ticker != ticker:
            continue

        def f(v, w=7):
            return "     -" if v is None else f"{v:{w}.4f}"

        print(
            f"{e.strike:>8} {e.side:>4} {e.ticker:<28} {f(e.p_hat_e3)} "
            f"{f(e.executable_price)} {f(e.executable_size)} {f(e.screen_edge)}  {e.eligibility_reason}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="LIVE_CALIBRATION_V0 one-intent submit runner")
    ap.add_argument("--ticker", default=None, help="market ticker (required with --execute)")
    ap.add_argument("--side", choices=["YES", "NO"], default=None, help="signal side (required with --execute)")
    ap.add_argument("--execute", action="store_true", help="enable mutation (one intent max)")
    ap.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    args = ap.parse_args()

    if args.execute and (not args.ticker or not args.side):
        print("REFUSING: --execute requires both --ticker and --side")
        return 1

    load_env_file(args.env_file)
    as_of = datetime.now(UTC)

    try:
        model, ladder, day, realized, signal_ts = _build_context(as_of)
    except Exception as exc:  # noqa: BLE001 — fail-closed on any setup error
        print(f"SETUP FAILED (fail-closed, no trade): {type(exc).__name__}: {exc}")
        return 1

    fee_rule = _verified_ercot_rule()
    evals = evaluate_ladder(
        model, ladder, fee_rule, as_of,
        realized_running_max=realized, signal_timestamp=signal_ts,
    )

    print("=== LIVE_CALIBRATION_V0 SUBMIT RUNNER ===")
    print(f"operating_day: {day}")
    print(f"realized running max: {realized if realized is not None else 'FEED UNVERIFIED (fail-closed)'}")
    print(f"signal timestamp: {signal_ts.isoformat() if signal_ts else 'UNKNOWN'}")
    print(f"fee rule: taker {fee_rule.taker_rate} / maker {fee_rule.maker_rate} (verified={fee_rule.verified})")
    print()

    if not args.execute:
        _print_evals(evals, ticker=args.ticker)
        print("\ndry-run complete (no mutation). Pass --ticker --side --execute to submit ONE intent.")
        return 0

    # Singleton the entire mutation/reconciliation lifecycle: a second operator
    # invocation must not race this one while armed/kill-clear.
    lease = ProcessLease(PROD_LEASE)
    if not lease.acquire():
        print("REFUSING: another submit/reconcile process holds the OMS lease")
        return 1

    # Exactly one intent for the selected (ticker, side).
    matches = [e for e in evals if e.ticker == args.ticker and e.side == args.side]
    if not matches:
        print(f"NO_TRADE: no evaluation for ticker={args.ticker} side={args.side} (wrong operating day?)")
        return 0
    e = matches[0]
    if e.intent is None:
        print(f"NO_TRADE: ticker={args.ticker} side={args.side} -> {e.eligibility_reason}")
        return 0
    intent = e.intent

    client = KalshiExecProdClient(expected_version=CALIBRATION_PROTOCOL_VERSION)
    if client.kill_switch_state.active:
        print("REFUSING: production kill switch is armed; operator must clear it first")
        return 1
    if not client.armed_state.armed:
        print("REFUSING: operator gate is not armed; operator must arm it first")
        return 1

    # Fresh Kalshi book revalidation immediately before mutation.
    fresh = KalshiPublicClient().market(args.ticker).get("market", {})
    if intent.side == OrderSide.BID:
        fresh_price = Decimal(str(fresh.get("yes_ask_dollars") or "0"))
        fresh_size = Decimal(str(fresh.get("yes_ask_size_fp") or "0"))
    else:
        fresh_price = Decimal(str(fresh.get("yes_bid_dollars") or "0"))
        fresh_size = Decimal(str(fresh.get("yes_bid_size_fp") or "0"))
    if not revalidate_quote(intent, fresh_price, fresh_size):
        print("NO_TRADE: fresh book moved against us; not submitting")
        return 0

    print(f"submitting ONE intent: {intent.side.value} {intent.count} @ {intent.price} {intent.ticker}")
    harness = ProdExecutionHarness(
        client,
        args.ticker,
        frozen_version=CALIBRATION_PROTOCOL_VERSION,
        journal_path=str(PROD_JOURNAL),
        risk_path=str(PROD_RISK),
    )
    state = harness.submit_intent(intent)
    print(f"harness state: {state.value}")
    print(f"client_order_id: {intent.client_order_id}")
    print(f"venue order_id: {harness.order_id}")

    # Re-arm the kill switch after the mutation attempt (never auto-clear).
    try:
        client.disable("MANUAL_OPERATOR", actor="submit-runner")
        print("kill switch re-armed")
    except Exception as exc:  # noqa: BLE001
        print(f"kill switch re-arm failed: {exc}")

    if state == HarnessState.HALTED:
        print("ambiguous outcome: halted (kill switch armed); reconcile via the durable OMS path")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())