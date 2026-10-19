#!/usr/bin/env python3
"""Production Kalshi smoke — READ-ONLY by default (LIVE_CALIBRATION_V0 scaffold).

Reads account state against the PRODUCTION venue and prints the safety-gate
status. It places NO orders.

Operator actions are explicit, separate subcommands and never part of a trading
path:
    --operator-arm       write the operator-armed state (armed=true)
    --operator-disarm    write the operator-armed state (armed=false)
    --clear-kill-switch  clear ONLY the initial fail-closed arm

Usage:
    .venv/Scripts/python.exe scripts/kalshi_prod_smoke.py --env-file <prod.env>
    .venv/Scripts/python.exe scripts/kalshi_prod_smoke.py --operator-arm
    .venv/Scripts/python.exe scripts/kalshi_prod_smoke.py --clear-kill-switch
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.kalshi_auth import KalshiAuthClient, load_env_file  # noqa: E402
from polyalpha.kalshi_exec_prod import (  # noqa: E402
    DEFAULT_ARMED_PATH,
    DEFAULT_KILL_SWITCH_PATH,
    KalshiExecProdClient,
    arm_operator,
    disarm_operator,
)
from polyalpha.kill_switch import TradingKillSwitch  # noqa: E402


def _read_only_account_view(env_file: str | None) -> int:
    if env_file:
        load_env_file(env_file)
    print("=== KALSHI PRODUCTION READ-ONLY SMOKE ===")
    try:
        auth = KalshiAuthClient(env="production")
        print("balance:", auth.get_balance())
        print("positions:", auth.get_positions())
        print("orders:", auth.get_orders())
        print("fills:", auth.get_fills())
    except Exception as exc:  # noqa: BLE001 — read-only view is best-effort
        print(f"read-only account view unavailable: {exc}")
        return 1

    print("\n--- production execution safety state ---")
    try:
        exec_client = KalshiExecProdClient()
        print("kill switch:", exec_client.kill_switch_state.as_dict())
        print("operator armed:", exec_client.armed_state.as_dict())
    except Exception as exc:  # noqa: BLE001
        print(f"execution client unavailable: {exc}")

    print("\nRead-only smoke complete. No order was placed.")
    return 0


def _clear_initial_arm() -> int:
    kill = TradingKillSwitch(DEFAULT_KILL_SWITCH_PATH)
    st = kill.state()
    if st.active and st.reason == "MANUAL_OPERATOR" and st.activated_by == "init-fail-closed":
        kill.deactivate(actor="operator-cli")
        print("kill switch cleared (initial fail-closed arm).")
        return 0
    if st.active:
        print(f"REFUSING: kill switch armed with reason {st.reason!r}; requires manual review.")
        return 1
    print("kill switch already clear.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Production Kalshi read-only smoke / operator CLI")
    ap.add_argument("--env-file", default=None, help="path to a production .env file")
    ap.add_argument("--operator-arm", action="store_true",
                    help="explicitly set operator-armed state (armed=true), then exit")
    ap.add_argument("--operator-disarm", action="store_true",
                    help="explicitly set operator-armed state (armed=false), then exit")
    ap.add_argument("--clear-kill-switch", action="store_true",
                    help="clear ONLY the initial fail-closed kill-switch arm, then exit")
    ap.add_argument("--actor", default="operator", help="operator identity for audit")
    args = ap.parse_args()

    if args.operator_arm and args.operator_disarm:
        print("refusing: --operator-arm and --operator-disarm are mutually exclusive")
        return 1
    if args.operator_arm:
        st = arm_operator(DEFAULT_ARMED_PATH, actor=args.actor)
        print("operator armed:", st.as_dict())
        return 0
    if args.operator_disarm:
        st = disarm_operator(DEFAULT_ARMED_PATH, actor=args.actor)
        print("operator disarmed:", st.as_dict())
        return 0
    if args.clear_kill_switch:
        return _clear_initial_arm()

    return _read_only_account_view(args.env_file)


if __name__ == "__main__":
    raise SystemExit(main())