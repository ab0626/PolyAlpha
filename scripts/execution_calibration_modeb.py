#!/usr/bin/env python3
"""EXECUTION_CALIBRATION_V1.1 — Mode B sampler (READ-ONLY, never transmits).

Discovers the eligible ERCOT universe, applies frozen eligibility, deterministically
selects ticker + side via sha256(timestamp_bucket, ticker, FIXED_SEED), constructs
a $2 taker intent, writes an immutable candidate record, and STOPS.

This module imports NO order-submission / auth / OMS code — it is physically
incapable of transmitting an order. mode=EXECUTION_ONLY, alpha_eligible=false.

Usage:
    .venv/Scripts/python.exe scripts/execution_calibration_modeb.py
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.energy.kalshi import ENERGY_SERIES, KalshiPublicClient  # noqa: E402

CONFIG = ROOT / "config" / "frozen" / "execution-calibration-v1.json"
OUT = ROOT / "data" / "execution_calibration" / "modeb_candidates.jsonl"
LATEST = ROOT / "data" / "execution_calibration" / "latest_candidate.json"

FIXED_SEED = "execution-calibration-v1.1-modeb"
BUCKET_SECONDS = 900
MAX_SPREAD = Decimal("0.05")
MIN_DEPTH = Decimal("100")
MAX_NOTIONAL = Decimal("2")


def _dec(v):
    try:
        return Decimal(str(v))
    except Exception:
        return None


def contract_sha() -> str:
    return json.loads(CONFIG.read_text(encoding="utf-8")).get("contract_sha256", "")


def boundary_eligible(side: str, yes_ask: Decimal, yes_bid: Decimal) -> bool:
    """v1.2: exclude payoff-boundary executions on the selected side."""
    if side == "YES":
        return yes_ask <= Decimal("0.98")
    return yes_bid >= Decimal("0.02")


def main() -> int:
    sha = contract_sha()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    bucket = int(time.time()) // BUCKET_SECONDS
    client = KalshiPublicClient()

    eligible = []
    skip_reasons = []
    for series in ENERGY_SERIES:
        try:
            markets = client.markets(series_ticker=series, limit=1000).get("markets", [])
        except Exception as exc:  # noqa: BLE001
            skip_reasons.append(f"{series}:fetch_failed:{type(exc).__name__}")
            continue
        for m in markets:
            if m.get("status") != "active":
                continue
            ticker = m.get("ticker")
            yes_bid = _dec(m.get("yes_bid_dollars"))
            yes_ask = _dec(m.get("yes_ask_dollars"))
            bid_size = _dec(m.get("yes_bid_size_fp"))
            ask_size = _dec(m.get("yes_ask_size_fp"))
            if not ticker or yes_bid is None or yes_ask is None:
                continue
            if yes_bid <= 0 or yes_ask <= 0:
                skip_reasons.append(f"{ticker}:one_sided")
                continue
            spread = yes_ask - yes_bid
            depth = (bid_size or Decimal(0)) + (ask_size or Decimal(0))
            if spread > MAX_SPREAD:
                skip_reasons.append(f"{ticker}:spread")
                continue
            if depth < MIN_DEPTH:
                skip_reasons.append(f"{ticker}:depth")
                continue
            eligible.append({
                "ticker": ticker, "yes_bid": yes_bid, "yes_ask": yes_ask,
                "bid_size": bid_size, "ask_size": ask_size, "spread": spread, "depth": depth,
            })

    if not eligible:
        record = {
            "candidate_id": None, "mode": "EXECUTION_ONLY", "alpha_eligible": False,
            "contract_sha256": sha, "timestamp_bucket": bucket, "seed": FIXED_SEED,
            "eligible_universe": [], "selected_ticker": None, "selected_side": None,
            "status": "SKIP", "reason": "no_eligible_market", "skip_reasons": skip_reasons,
            "generated_at": datetime.now(UTC).isoformat(),
        }
        with OUT.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
        print("SKIP: no eligible market", file=sys.stderr)
        return 0

    def hsh(ticker: str) -> str:
        return hashlib.sha256(f"{sha}|{bucket}|{ticker}|{FIXED_SEED}".encode()).hexdigest()

    selected = min(eligible, key=lambda e: hsh(e["ticker"]))
    side = "YES" if int(hsh(selected["ticker"])[0], 16) % 2 == 0 else "NO"

    # v1.2 boundary guard: exclude payoff-boundary executions on the selected side.
    if not boundary_eligible(side, selected["yes_ask"], selected["yes_bid"]):
        skip = {
            "candidate_id": None, "mode": "EXECUTION_ONLY", "alpha_eligible": False,
            "contract_sha256": sha, "timestamp_bucket": bucket, "seed": FIXED_SEED,
            "selected_ticker": selected["ticker"], "selected_side": side,
            "decision_quote": {
                "yes_bid": str(selected["yes_bid"]), "yes_ask": str(selected["yes_ask"]),
                "spread": str(selected["spread"]), "depth": str(selected["depth"]),
            },
            "status": "SKIP", "reason": "boundary_execution",
            "generated_at": datetime.now(UTC).isoformat(),
        }
        with OUT.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(skip, sort_keys=True) + "\n")
        print(f"SKIP: boundary execution ({selected['ticker']} {side} ask={selected['yes_ask']} bid={selected['yes_bid']})", file=sys.stderr)
        return 0

    candidate_id = hashlib.sha256(
        f"{sha}|{bucket}|{selected['ticker']}|{side}|{FIXED_SEED}".encode()
    ).hexdigest()

    record = {
        "candidate_id": candidate_id, "mode": "EXECUTION_ONLY", "alpha_eligible": False,
        "contract_sha256": sha, "timestamp_bucket": bucket, "seed": FIXED_SEED,
        "eligible_universe": [e["ticker"] for e in eligible],
        "selected_ticker": selected["ticker"], "selected_side": side,
        "decision_quote": {
            "yes_bid": str(selected["yes_bid"]), "yes_ask": str(selected["yes_ask"]),
            "bid_size": str(selected["bid_size"]), "ask_size": str(selected["ask_size"]),
            "spread": str(selected["spread"]), "depth": str(selected["depth"]),
        },
        "notional": str(MAX_NOTIONAL), "intent_ttl_seconds": 60,
        "status": "CANDIDATE", "generated_at": datetime.now(UTC).isoformat(),
    }
    with OUT.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")

    print(f"MODE B CANDIDATE (read-only, no order transmitted)")
    print(f"  candidate_id: {candidate_id}")
    print(f"  ticker: {selected['ticker']}   side: {side}")
    print(f"  bid={selected['yes_bid']} ask={selected['yes_ask']} spread={selected['spread']} depth={selected['depth']}")
    print(f"  eligible universe: {len(eligible)} tickers")
    print(f"  notional: $2   intent TTL: 60s")
    print(f"  operator must still arm + clear kill switch + submit explicitly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())