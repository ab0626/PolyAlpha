"""Paper trading runner — executes backtest and produces a structured report.

Usage:
    python -m polyalpha.paper_runner \\
        --config config/base.toml \\
        --db data/polyalpha.db \\
        --output data/paper_report.json

Section 39 Phase 9: Paper trading runner that produces a full report
with all metrics required by Section 54 for evaluating whether alpha exists.
"""

import argparse
import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

D = Decimal


def load_records(db_path: str, limit: int = 10000) -> list:
    """Load book records from SQLite for backtesting."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        """
        SELECT id, kind, entity_id, payload, received_at
        FROM receipts
        WHERE kind IN ('book', 'market', 'settlement')
        ORDER BY received_at
        LIMIT ?
        """,
        (limit,),
    )
    rows = cursor.fetchall()
    conn.close()

    from polyalpha.storage import Record

    records = []
    for rid, kind, entity_id, payload_json, received_at in rows:
        ts = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
        records.append(
            Record(
                id=rid,
                kind=kind,
                entity_id=entity_id,
                received_at=ts,
                source_at=None,
                payload=json.loads(payload_json),
            )
        )
    return records


def build_clusters(records: list) -> dict:
    """Build cluster assignments from market records."""
    from polyalpha.parsing import parse_market

    clusters = {}
    for rec in records:
        if rec.kind == "market":
            try:
                market = parse_market(rec.payload, rec.received_at)
                clusters[market.market_id] = {
                    "event": market.event_ids[0] if market.event_ids else "unknown",
                    "cluster": market.event_ids[0] if market.event_ids else market.market_id,
                    "category": market.category,
                }
            except (ValueError, KeyError):
                pass
    return clusters


def run_backtest(
    records: list,
    clusters: dict,
    initial_cash: Decimal = D(10000),
    min_edge: Decimal = D("0.025"),
    exit_policy: str = "hold",
) -> dict:
    """Run a full backtest and return the report."""
    from polyalpha.backtest import Engine
    from polyalpha.forecasting import Baseline

    engine = Engine(
        clusters=clusters,
        model=Baseline(D("0.05")),
        initial_cash=initial_cash,
        min_edge=min_edge,
        exit_policy=exit_policy,
    )

    report = engine.run(iter(records))
    return report


def compute_section54_metrics(report: dict, initial_cash: Decimal = D(10000)) -> dict:
    """Compute the full metrics required by Section 54.

    Returns a dict with every metric needed to evaluate whether alpha exists.
    """
    metrics = {}

    # Section 54: Sample size and testing period
    metrics["sample_size"] = report.get("input_records", 0)
    metrics["period_start"] = report.get("period_start")
    metrics["period_end"] = report.get("period_end")
    metrics["fills"] = report.get("fills", 0)
    metrics["resolved_markets"] = report.get("resolved_markets", 0)

    # Section 54: PnL
    metrics["initial_cash"] = str(initial_cash)
    metrics["final_cash"] = report.get("cash")
    metrics["net_pnl"] = report.get("net_pnl")
    metrics["gross_pnl"] = report.get("gross_pnl")
    metrics["fees"] = report.get("fees")
    metrics["estimated_exit_fees"] = report.get("estimated_exit_fees")
    metrics["transaction_costs"] = report.get("transaction_costs")
    metrics["depth_slippage"] = report.get("depth_slippage_diagnostic")

    # Section 54: Drawdown
    metrics["max_drawdown"] = report.get("max_drawdown")
    metrics["max_drawdown_observed"] = report.get("max_drawdown_observed")

    # Section 54: Calibration
    cal = report.get("calibration")
    if cal:
        metrics["brier_score"] = cal.get("brier")
        metrics["log_loss"] = cal.get("log_loss")
        metrics["ece"] = cal.get("ece")
        metrics["calibration_buckets"] = cal.get("buckets")
        metrics["independent_clusters"] = cal.get("independent_clusters")

    # Section 54: Risk state
    risk = report.get("risk_state", {})
    metrics["risk_halted"] = risk.get("halted")
    metrics["risk_reason"] = risk.get("reason")

    # Section 54: Exposure concentration
    metrics["cluster_exposure"] = report.get("cluster_exposure")
    metrics["category_exposure"] = report.get("category_exposure")

    # Section 54: Category performance
    metrics["category_performance"] = report.get("category_performance")

    # Section 54: Decision summary
    decisions = report.get("decisions", [])
    reasons = {}
    for d in decisions:
        r = d.get("reason", "unknown")
        reasons[r] = reasons.get(r, 0) + 1
    metrics["decision_summary"] = reasons

    # Section 54: Uncertainty statistics
    metrics["uncertainty_statistics"] = report.get("uncertainty_statistics")

    # Section 54: Valuation
    metrics["valuation_complete"] = report.get("valuation_complete")
    metrics["valuation_gap_count"] = report.get("valuation_gap_count")

    return metrics


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Run paper trading backtest")
    parser.add_argument("--db", required=True, help="SQLite database path")
    parser.add_argument("--output", default="data/paper_report.json", help="Output report path")
    parser.add_argument("--initial-cash", type=str, default="10000")
    parser.add_argument("--min-edge", type=str, default="0.025")
    parser.add_argument(
        "--exit-policy",
        choices=["hold", "edge", "time", "stop_loss", "trailing", "all"],
        default="hold",
    )
    parser.add_argument("--limit", type=int, default=10000)
    args = parser.parse_args(argv)

    if not Path(args.db).exists():
        print(json.dumps({"error": f"database not found: {args.db}"}))
        sys.exit(1)

    print(f"Loading records from {args.db}...", file=sys.stderr)
    records = load_records(args.db, limit=args.limit)
    if not records:
        print(json.dumps({"error": "no records found"}))
        sys.exit(1)

    print(f"Loaded {len(records)} records", file=sys.stderr)

    clusters = build_clusters(records)
    print(f"Built {len(clusters)} cluster assignments", file=sys.stderr)

    print(f"Running backtest (exit_policy={args.exit_policy})...", file=sys.stderr)
    report = run_backtest(
        records,
        clusters,
        initial_cash=D(args.initial_cash),
        min_edge=D(args.min_edge),
        exit_policy=args.exit_policy,
    )

    # Compute Section 54 metrics
    section54 = compute_section54_metrics(report, initial_cash=D(args.initial_cash))

    # Combine report with Section 54 metrics
    full_report = {
        **report,
        "section_54_metrics": section54,
    }

    # Save
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(full_report, f, indent=2, default=str)

    # Print summary to stdout
    summary = {
        "event": "paper_backtest_complete",
        "output": str(output_path),
        "records": len(records),
        "fills": report.get("fills", 0),
        "resolved_markets": report.get("resolved_markets", 0),
        "net_pnl": report.get("net_pnl"),
        "max_drawdown": report.get("max_drawdown"),
        "brier_score": section54.get("brier_score"),
        "calibration_error": section54.get("ece"),
        "exit_policy": args.exit_policy,
    }
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
