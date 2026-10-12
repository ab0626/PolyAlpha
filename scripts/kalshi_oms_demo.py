#!/usr/bin/env python3
"""Demo-only automated execution loop (NULL signal).

Proves the AUTOMATION MECHANICS end-to-end against live demo: a scheduler loop
that, each cycle, posts a deterministic 1-contract post-only bid, cancels it,
reconciles, and writes an append-only audit trail. The signal is NULL — it
carries no edge, performs no tuning, and makes no alpha claim.

Guards:
  * demo-only (production hard-blocked at construction)
  * kill-switch armed by default; --enable-execution clears it explicitly
  * fail-closed: any guard trip aborts the cycle
  * append-only audit log

Usage:
    .venv/Scripts/python.exe scripts/kalshi_oms_demo.py \
        --env-file C:\\Users\\adith\\powerdemoaccount.env \
        --enable-execution --interval 8 --max-cycles 3
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.energy.kalshi import ENERGY_SERIES, KalshiPublicClient  # noqa: E402
from polyalpha.kalshi_auth import BASE_URLS, load_env_file  # noqa: E402
from polyalpha.kalshi_exec import (  # noqa: E402
    ExecutionBlocked,
    KalshiConflict,
    KalshiExecClient,
    NewOrder,
    OrderOutcomeUnknown,
    OrderRejected,
    OrderSide,
    new_client_order_id,
)

DEFAULT_ENV_FILE = r"C:\Users\adith\powerdemoaccount.env"
DEFAULT_AUDIT_FILE = ROOT / "data" / "energy" / "kalshi" / "oms_demo_audit.jsonl"


def discover_ticker(demo_base_url: str) -> str | None:
    """First active energy market, else any active demo market."""
    client = KalshiPublicClient(base_url=demo_base_url)
    for series in ENERGY_SERIES:
        try:
            body = client.markets(series_ticker=series, limit=100)
        except Exception:  # noqa: BLE001
            continue
        for market in body.get("markets", []):
            if market.get("status") == "active" and market.get("ticker"):
                return market["ticker"]
    try:
        body = client.markets(limit=100)
    except Exception:  # noqa: BLE001
        return None
    for market in body.get("markets", []):
        if market.get("status") == "active" and market.get("ticker"):
            return market["ticker"]
    return None


def audit(path: Path, event: str, **fields) -> None:
    row = {"ts": datetime.now(UTC).isoformat(), "event": event, **fields}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def run_cycle(client: KalshiExecClient, ticker: str, audit_path: Path, cycle: int) -> None:
    audit(audit_path, "cycle_start", cycle=cycle, ticker=ticker)
    cid = new_client_order_id()
    order = NewOrder(
        ticker=ticker,
        side=OrderSide.BID,
        count=Decimal("1"),
        price=Decimal("0.01"),
        client_order_id=cid,
        post_only=True,
    )
    order_id = None
    try:
        ack = client.create_order(order)
        order_id = ack.order_id
        audit(audit_path, "order_placed", cycle=cycle, order_id=order_id,
              client_order_id=cid, remaining=str(ack.remaining_count))
    except (OrderRejected, OrderOutcomeUnknown, KalshiConflict, ExecutionBlocked) as exc:
        audit(audit_path, "order_place_failed", cycle=cycle,
              error=f"{type(exc).__name__}: {exc}")
    if order_id:
        try:
            cancel = client.cancel_order(order_id, market_ticker=ticker)
            audit(audit_path, "order_canceled", cycle=cycle, order_id=order_id,
                  reduced_by=str(cancel.reduced_by))
        except (OrderRejected, OrderOutcomeUnknown) as exc:
            audit(audit_path, "order_cancel_failed", cycle=cycle,
                  error=f"{type(exc).__name__}: {exc}")
    rec = client.reconcile(cid)
    audit(audit_path, "reconcile", cycle=cycle, client_order_id=cid,
          found=rec.found, status=rec.status, remaining=str(rec.remaining_count),
          filled=str(rec.filled_count))
    audit(audit_path, "cycle_end", cycle=cycle)


def main() -> int:
    ap = argparse.ArgumentParser(description="Demo-only automated execution loop (NULL signal)")
    ap.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    ap.add_argument("--market-ticker", default=None)
    ap.add_argument("--interval", type=float, default=8.0, help="seconds between cycles")
    ap.add_argument("--max-cycles", type=int, default=0, help="0 = run forever")
    ap.add_argument("--enable-execution", action="store_true",
                    help="explicitly clear the kill switch (operator action)")
    ap.add_argument("--audit-file", default=str(DEFAULT_AUDIT_FILE))
    args = ap.parse_args()

    load_env_file(args.env_file)

    try:
        client = KalshiExecClient()
    except ValueError as exc:
        print(f"EXECUTION NOT AVAILABLE: {exc}")
        return 1

    audit_path = Path(args.audit_file)
    print("=== KALSHI DEMO AUTOMATED EXECUTION LOOP (NULL signal) ===")
    print("kill switch:", client.kill_switch_state.as_dict())

    if not args.enable_execution:
        print("Refusing to run: pass --enable-execution to clear the kill switch.")
        return 1

    ticker = args.market_ticker or discover_ticker(BASE_URLS["demo"])
    if not ticker:
        print("No active demo market found.")
        return 1

    client.enable(actor="oms-demo")
    print(f"target: {ticker}  interval: {args.interval}s  max_cycles: {args.max_cycles or 'infinite'}")
    audit(audit_path, "loop_start", ticker=ticker, interval=args.interval, max_cycles=args.max_cycles)

    cycle = 0
    try:
        while True:
            cycle += 1
            if client.kill_switch_state.active:
                audit(audit_path, "kill_switch_tripped", cycle=cycle)
                print("kill switch re-armed; stopping loop.")
                break
            run_cycle(client, ticker, audit_path, cycle)
            print(f"cycle {cycle} complete")
            if args.max_cycles and cycle >= args.max_cycles:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\ninterrupted; stopping.")
    finally:
        audit(audit_path, "loop_stop", cycles=cycle)

    print(f"done. audit: {audit_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())