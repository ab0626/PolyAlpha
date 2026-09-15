#!/usr/bin/env python3
"""Run backtest on collected data.

Usage:
    python scripts/backtest.py --database data/polyalpha.sqlite
        --config config/base.toml --output data/backtest_report.json
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from polyalpha.backtest import Engine
from polyalpha.config import load_config
from polyalpha.parsing import parse_market
from polyalpha.performance import (
    conditional_value_at_risk,
    drawdown_series,
    edge_metrics,
    trade_metrics,
    value_at_risk,
)
from polyalpha.storage import Store


def main():
    parser = argparse.ArgumentParser(description="Run backtest")
    parser.add_argument("--database", default="data/polyalpha.sqlite")
    parser.add_argument("--config", default="config/base.toml")
    parser.add_argument("--output", default="data/backtest_report.json")
    parser.add_argument("--clusters", default="config/clusters.json")
    parser.add_argument("--initial-cash", type=str, default="10000")
    parser.add_argument("--min-edge", type=str, default="0.025")
    args = parser.parse_args()

    load_config(args.config)

    # Load clusters
    clusters_path = Path(args.clusters)
    if clusters_path.exists():
        with open(clusters_path) as f:
            clusters = json.load(f)
    else:
        clusters = {}

    # Load settlement labels
    labels = {}
    label_times = {}
    with Store(args.database) as store:
        for receipt in store.replay(datetime.now(UTC), "settlement"):
            p = receipt.payload
            token_id = p["token_id"]
            payout = Decimal(str(p["payout"]))
            if payout in (0, 1):
                # Find market for this token
                for mr in store.replay(datetime.now(UTC), "market"):
                    m = parse_market(mr.payload, mr.received_at)
                    if m.yes_token_id == token_id:
                        outcome = int(payout)
                        labels[m.market_id] = outcome
                        label_times[m.market_id] = receipt.received_at
                        break
                    elif m.no_token_id == token_id:
                        outcome = 1 - int(payout)
                        labels[m.market_id] = outcome
                        label_times[m.market_id] = receipt.received_at
                        break

        # Run engine
        engine = Engine(
            clusters=clusters,
            initial_cash=Decimal(args.initial_cash),
            min_edge=Decimal(args.min_edge),
        )

        records = list(store.replay(datetime.now(UTC)))
        report = engine.run(records)

    # Add performance metrics
    equity_data = [
        (e["timestamp"], float(e["equity"])) for e in report["equity"] if not e.get("unliquidated")
    ]

    if len(equity_data) > 1:
        equities = [e[1] for e in equity_data]
        dd_series = drawdown_series(equities)
        report["drawdown_series"] = dd_series

        # Compute returns for tail risk
        returns = [equities[i] / equities[i - 1] - 1 for i in range(1, len(equities))]
        report["tail_risk"] = {
            "var_95": value_at_risk(returns, 0.95),
            "cvar_95": conditional_value_at_risk(returns, 0.95),
        }

    # Add trade metrics
    report["trade_metrics"] = trade_metrics(report.get("fill_journal", []))
    report["edge_metrics"] = edge_metrics(report.get("decisions", []))

    # Save report
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"Backtest complete: {report['fills']} fills, net PnL: {report.get('net_pnl', 'N/A')}")
    print(f"Report saved to {output_path}")


if __name__ == "__main__":
    main()
