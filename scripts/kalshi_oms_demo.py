#!/usr/bin/env python3
"""Demo-only automated execution harness — CLI wrapper (NULL signal).

Thin wrapper around ``DemoExecutionHarness`` (src/polyalpha/kalshi_oms.py),
which implements the state machine and the ambiguity invariant:

    UNKNOWN or unresolved state  ==>  no new intents.

The runner NEVER clears the kill switch on startup. The operator clears it in a
separate, explicit step (--clear-kill-switch, which only clears the initial
MANUAL_OPERATOR arm). If the kill switch is armed — or trips mid-loop — the
runner halts.

Usage:
    # explicit, separate clear (only the initial MANUAL_OPERATOR arm)
    .venv/Scripts/python.exe scripts/kalshi_oms_demo.py --clear-kill-switch \
        --env-file C:\\Users\\adith\\powerdemoaccount.env

    # run the loop (refuses if the kill switch is armed)
    .venv/Scripts/python.exe scripts/kalshi_oms_demo.py \
        --env-file C:\\Users\\adith\\powerdemoaccount.env --interval 8 --max-cycles 3
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.energy.kalshi import ENERGY_SERIES, KalshiPublicClient  # noqa: E402
from polyalpha.kalshi_auth import BASE_URLS, load_env_file  # noqa: E402
from polyalpha.kalshi_exec import KalshiExecClient  # noqa: E402
from polyalpha.kalshi_oms import DemoExecutionHarness, HarnessState  # noqa: E402
from polyalpha.process_lease import ProcessLease  # noqa: E402

DEFAULT_ENV_FILE = r"C:\Users\adith\powerdemoaccount.env"
DEFAULT_AUDIT_FILE = ROOT / "data" / "energy" / "kalshi" / "oms_demo_audit.jsonl"
DEFAULT_LEASE_FILE = ROOT / "data" / "energy" / "kalshi" / "oms_demo_lease.lock"
DEFAULT_JOURNAL_FILE = ROOT / "data" / "energy" / "kalshi" / "oms_demo_journal.jsonl"


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


def make_audit(path: Path):
    def audit(event: str, fields: dict) -> None:
        row = {"ts": datetime.now(UTC).isoformat(), "event": event, **fields}
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    return audit


def main() -> int:
    ap = argparse.ArgumentParser(description="Demo-only automated execution harness (NULL signal)")
    ap.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    ap.add_argument("--market-ticker", default=None)
    ap.add_argument("--interval", type=float, default=8.0, help="seconds between cycles")
    ap.add_argument("--max-cycles", type=int, default=0, help="0 = run forever")
    ap.add_argument("--clear-kill-switch", action="store_true",
                    help="explicitly clear ONLY the initial MANUAL_OPERATOR arm, then exit")
    ap.add_argument("--reconcile-timeout", type=float, default=30.0)
    ap.add_argument("--reconcile-poll", type=float, default=1.0)
    ap.add_argument("--audit-file", default=str(DEFAULT_AUDIT_FILE))
    ap.add_argument("--lease-file", default=str(DEFAULT_LEASE_FILE),
                    help="OS-held process lease (singleton guard)")
    ap.add_argument("--journal-file", default=str(DEFAULT_JOURNAL_FILE),
                    help="durable write-ahead execution journal")
    args = ap.parse_args()

    load_env_file(args.env_file)

    try:
        client = KalshiExecClient()
    except ValueError as exc:
        print(f"EXECUTION NOT AVAILABLE: {exc}")
        return 1

    st = client.kill_switch_state
    print("=== KALSHI DEMO AUTOMATED EXECUTION HARNESS (NULL signal) ===")
    print("kill switch:", st.as_dict())

    if args.clear_kill_switch:
        # Only the INITIAL fail-closed arm (activated_by="init-fail-closed") may
        # be cleared through this convenience path. A later MANUAL_OPERATOR arm
        # (an operator intentionally stopping the system) must NOT be cleared
        # here — it requires explicit manual review.
        if (st.active and st.reason == "MANUAL_OPERATOR"
                and st.activated_by == "init-fail-closed"):
            client.enable(actor="oms-operator")
            print("kill switch cleared (initial MANUAL_OPERATOR arm). Run the loop now.")
            return 0
        if st.active:
            print(f"REFUSING: kill switch armed with reason {st.reason!r}; requires manual review.")
            return 1
        print("kill switch already clear.")
        return 0

    if st.active:
        print(f"REFUSING to run: kill switch armed (reason {st.reason!r}). "
              "Clear it separately with --clear-kill-switch.")
        return 1

    # Singleton guard: the OS-held lease fails fast if another harness instance
    # is running, before any discovery or venue mutation.
    lease = ProcessLease(args.lease_file)
    if not lease.acquire():
        print(f"REFUSING: another harness instance holds the lease ({args.lease_file}).")
        return 1

    ticker = args.market_ticker or discover_ticker(BASE_URLS["demo"])
    if not ticker:
        lease.release()
        print("No active demo market found.")
        return 1

    audit_path = Path(args.audit_file)
    audit = make_audit(audit_path)
    harness = DemoExecutionHarness(
        client,
        ticker,
        reconcile_timeout=args.reconcile_timeout,
        reconcile_poll=args.reconcile_poll,
        audit=audit,
        journal_path=args.journal_file,
    )

    print(f"target: {ticker}  interval: {args.interval}s  max_cycles: {args.max_cycles or 'infinite'}")
    audit("loop_start", {"ticker": ticker, "interval": args.interval, "max_cycles": args.max_cycles})

    cycle = 0
    safety_halt = False
    halt_reason = None
    try:
        while True:
            if client.kill_switch_state.active:
                halt_reason = client.kill_switch_state.reason
                audit("kill_switch_tripped", {"cycle": cycle, "reason": halt_reason})
                safety_halt = True
                print("kill switch armed; halting loop.")
                break
            cycle += 1
            try:
                state = harness.run_cycle()
            except Exception as exc:  # noqa: BLE001 — arm the kill switch before exit
                halt_reason = f"unhandled: {type(exc).__name__}"
                audit("unhandled_error", {"error": f"{type(exc).__name__}: {exc}"})
                try:
                    client.disable("ABNORMAL_RECONCILIATION", actor="oms-harness")
                except Exception:
                    pass
                safety_halt = True
                break
            print(f"cycle {cycle} -> {state.value}")
            if state == HarnessState.HALTED:
                halt_reason = harness.halt_reason or "ABNORMAL_RECONCILIATION"
                safety_halt = True
                break
            if args.max_cycles and cycle >= args.max_cycles:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        if harness.state != HarnessState.IDLE:
            halt_reason = f"operator interrupt during {harness.state.value}"
            audit("operator_interrupt_active", {"state": harness.state.value, "reason": halt_reason})
            try:
                client.disable("ABNORMAL_RECONCILIATION", actor="oms-harness")
            except Exception:
                pass
            safety_halt = True
            print(f"\ninterrupted during {harness.state.value}; arming kill switch.")
        else:
            print("\nclean operator stop.")
    finally:
        audit("loop_stop", {"cycles": cycle, "safety_halt": safety_halt, "reason": halt_reason})
        lease.release()

    print(f"done. audit: {audit_path}")
    return 2 if safety_halt else 0


if __name__ == "__main__":
    raise SystemExit(main())