#!/usr/bin/env python3
"""Demo smoke test for the Kalshi execution layer (read-only by default).

Proves the order lifecycle mechanics against the LIVE DEMO venue (fake money):
create -> amend -> cancel. By default it only READS account state and prints
the safety-gate status; it mutates nothing unless you pass BOTH
``--place-demo-order`` and ``--enable-execution``.

Requirements: a demo API key + private key path, ``KALSHI_ENV=demo``, and mock
funds in the demo account (see docs.kalshi.com — Demo environment).

Usage:
    .venv/Scripts/python.exe scripts/kalshi_exec_smoke.py --env-file <demo.env>
    .venv/Scripts/python.exe scripts/kalshi_exec_smoke.py --env-file <demo.env> \
        --place-demo-order --enable-execution [--market-ticker KX...]
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.energy.kalshi import ENERGY_SERIES, KalshiPublicClient  # noqa: E402
from polyalpha.kalshi_auth import BASE_URLS, KalshiAuthClient, load_env_file  # noqa: E402
from polyalpha.kalshi_exec import (  # noqa: E402
    KalshiConflict,
    KalshiExecClient,
    NewOrder,
    OrderOutcomeUnknown,
    OrderRejected,
    OrderSide,
    new_client_order_id,
)

DEFAULT_ENV_FILE = r"C:\Users\adith\powermarkets.env"


def discover_ticker(demo_base_url: str) -> tuple[str, str] | None:
    """First active energy market, else any active demo market.

    Demo may not mirror production's energy series, so fall back to any active
    market: the smoke test proves VENUE MECHANICS, not the energy strategy.
    """
    client = KalshiPublicClient(base_url=demo_base_url)
    for series in ENERGY_SERIES:
        try:
            body = client.markets(series_ticker=series, limit=100)
        except Exception:  # noqa: BLE001 — best-effort per series
            continue
        for market in body.get("markets", []):
            if market.get("status") == "active" and market.get("ticker"):
                return market["ticker"], series
    try:
        body = client.markets(limit=100)
    except Exception:  # noqa: BLE001
        return None
    for market in body.get("markets", []):
        if market.get("status") == "active" and market.get("ticker"):
            return market["ticker"], market.get("series_ticker") or "demo"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Kalshi execution layer demo smoke test")
    ap.add_argument("--env-file", default=DEFAULT_ENV_FILE, help="path to a .env file")
    ap.add_argument("--market-ticker", default=None, help="explicit market ticker to trade")
    ap.add_argument("--place-demo-order", action="store_true",
                    help="exercise create->amend->cancel (requires --enable-execution)")
    ap.add_argument("--enable-execution", action="store_true",
                    help="explicitly clear the kill switch (operator action)")
    args = ap.parse_args()

    load_env_file(args.env_file)

    try:
        exec_client = KalshiExecClient()
    except ValueError as exc:
        print(f"EXECUTION NOT AVAILABLE: {exc}")
        print("Set KALSHI_ENV=demo with a demo API key to use the execution layer.")
        return 1

    print("=== KALSHI DEMO EXECUTION SMOKE ===")
    print("kill switch:", exec_client.kill_switch_state.as_dict())

    try:
        auth = KalshiAuthClient(env="demo")
        print("balance:", auth.get_balance())
        print("orders:", auth.get_orders())
    except Exception as exc:  # noqa: BLE001 — read-only view is best-effort
        print(f"read-only account view unavailable: {exc}")

    if not args.place_demo_order:
        print("\nRead-only smoke complete. Pass --place-demo-order --enable-execution "
              "to exercise create->cancel.")
        return 0

    if not args.enable_execution:
        print("\nRefusing to place an order: pass --enable-execution to clear the kill switch.")
        return 1

    ticker = args.market_ticker
    series = ""
    if not ticker:
        found = discover_ticker(BASE_URLS["demo"])
        if found is None:
            print("\nNo active energy markets in demo (demo universe may not mirror production).")
            return 1
        ticker, series = found
    print(f"\nTarget market: {ticker} (series {series or 'provided'})")

    exec_client.enable(actor="smoke-test")

    order = NewOrder(
        ticker=ticker,
        side=OrderSide.BID,
        count=Decimal("1"),
        price=Decimal("0.01"),  # minimum price; post_only guarantees it cannot fill
        client_order_id=new_client_order_id(),
        post_only=True,
    )

    ack = None
    try:
        ack = exec_client.create_order(order)
        print("CREATE OK:", ack.order_id, "status", ack.status, "remaining", ack.remaining_count)
    except KalshiConflict as exc:
        print("CREATE: idempotent conflict (already exists):", exc)
        print("Reconcile:", exec_client.reconcile(order.client_order_id))
        return 0
    except OrderRejected as exc:
        print("CREATE REJECTED:", exc)
        return 1
    except OrderOutcomeUnknown as exc:
        print("CREATE UNKNOWN (reconcile required):", exc)
        print("Reconcile:", exec_client.reconcile(order.client_order_id))
        return 1

    if ack is None or not ack.order_id:
        print("No order_id in ack; aborting cancel.")
        return 1

    amended_cid = new_client_order_id()
    try:
        amend_ack = exec_client.amend_order(
            ack.order_id,
            ticker=ticker,
            side=OrderSide.BID,
            price=Decimal("0.02"),
            count=Decimal("1"),
            original_client_order_id=order.client_order_id,
            updated_client_order_id=amended_cid,
        )
        print("AMEND OK:", amend_ack.order_id, "status", amend_ack.status)
    except OrderRejected as exc:
        print("AMEND REJECTED:", exc)
        return 1
    except OrderOutcomeUnknown as exc:
        print("AMEND UNKNOWN (reconcile required):", exc)
        print("Reconcile:", exec_client.reconcile(amended_cid))
        return 1

    try:
        cancel = exec_client.cancel_order(ack.order_id, market_ticker=ticker)
        print("CANCEL OK:", cancel.order_id, "reduced_by", cancel.reduced_by)
    except OrderRejected as exc:
        print("CANCEL REJECTED:", exc)
        return 1
    except OrderOutcomeUnknown as exc:
        print("CANCEL UNKNOWN:", exc)
        return 1

    print("Reconcile:", exec_client.reconcile(amended_cid))
    print("\ncreate->amend->cancel lifecycle complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())