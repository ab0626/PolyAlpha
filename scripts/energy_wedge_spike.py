"""Alpha-first spike: Kalshi ERCOT peak-load ladder -> implied distribution.

Read-only analysis. This is a spike to confirm the wedge surface, NOT a trading
strategy. It pulls the active ``KXTXERCOTPEAKD`` ladder, groups it by operating
day, and prints the implied survival function P(peak > K) at bid/mid/ask plus the
per-strike spread (the first-order cost of capturing any physical-vs-market gap).

Run:  .venv/Scripts/python.exe scripts/energy_wedge_spike.py
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.energy.kalshi import (  # noqa: E402
    KalshiPublicClient,
    build_ladder,
)

SERIES = "KXTXERCOTPEAKD"


def main() -> int:
    client = KalshiPublicClient()
    body = client.markets(series_ticker=SERIES, limit=1000)
    markets = [m for m in body.get("markets", []) if m.get("status") == "active"]

    # Group by operating day (event_ticker), keep only days with a real book.
    by_day: dict[str, list[dict]] = defaultdict(list)
    for m in markets:
        by_day[m.get("event_ticker") or m.get("occurrence_datetime") or "?"].append(m)

    # Nearest day = the one whose markets close soonest (still open).
    def close(m):
        return m.get("close_time") or "9999"
    nearest = min(by_day, key=lambda d: min(close(m) for m in by_day[d]))
    day_markets = by_day[nearest]

    ladder = build_ladder(SERIES, day_markets)
    print("underlying:", ladder.underlying.underlying_id)
    print("series:", ladder.series_ticker, "| rungs:", len(ladder.rungs))
    print("resolution:", (ladder.underlying.resolution_source or "")[:120].replace("\n", " "))
    print()
    print(f"{'strike (MW)':>12} {'P(>K) bid':>10} {'mid':>7} {'ask':>7} {'spread':>7} {'bid_sz':>8} {'ask_sz':>8}")
    for r in ladder.rungs:
        print(f"{r.threshold:>12} {_pct(r.yes_bid):>10} {_pct(r.mid):>7} "
              f"{_pct(r.yes_ask):>7} {_pct(r.spread):>7} "
              f"{_sz(r.yes_bid_size):>8} {_sz(r.yes_ask_size):>8}")

    # Monotonicity checks. Midpoint ordering is DIAGNOSTIC only (wide spreads);
    # the executable condition is yes_bid(K2) > yes_ask(K1), still before
    # fees/leg risk.
    mid_viol = ladder.midpoint_violations()
    exec_viol = ladder.executable_violations()
    print()
    print("midpoint monotonicity violations  (DIAGNOSTIC, not alpha):", len(mid_viol))
    print("executable monotonicity violations (yes_bid(K2)>yes_ask(K1), pre-fees):",
          len(exec_viol))
    for k, edge in exec_viol:
        print(f"    cross at K={k}  gross_edge={edge}")
    return 0


def _pct(d) -> str:
    return f"{float(d) * 100:>6.1f}%" if d is not None else "   -"


def _sz(d) -> str:
    return f"{float(d):>7.0f}" if d is not None else "   -"


if __name__ == "__main__":
    raise SystemExit(main())