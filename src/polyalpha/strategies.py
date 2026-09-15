"""Strategy comparison framework for evaluating multiple approaches.

Compares separate backtest results with consistent metrics.
Helps determine which model components actually contribute value.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

D = Decimal


@dataclass(frozen=True)
class StrategyResult:
    """Result of a single strategy backtest."""

    name: str
    description: str
    brier_score: float | None
    log_loss: float | None
    net_pnl: float | None
    gross_pnl: float | None
    max_drawdown: float | None
    sharpe_ratio: float | None
    total_fills: int
    fill_rate: float | None
    average_predicted_edge: float | None
    calibration_error: float | None
    resolved_markets: int
    independent_clusters: int
    period_start: str | None
    period_end: str | None
    metadata: dict[str, Any] = field(default_factory=dict)


def compare_strategies(results: list[StrategyResult]) -> dict:
    """Compare multiple strategy results.

    Returns structured comparison with rankings and analysis.
    """
    if not results:
        return {"strategies": 0, "comparison": None}

    comparison = {
        "strategies": len(results),
        "names": [r.name for r in results],
        "rankings": {},
        "pairwise": [],
        "summary": {},
    }

    # Rank by each metric
    metrics_to_rank = [
        ("brier_score", "lower_is_better"),
        ("log_loss", "lower_is_better"),
        ("net_pnl", "higher_is_better"),
        ("max_drawdown", "lower_is_better"),
        ("sharpe_ratio", "higher_is_better"),
        ("calibration_error", "lower_is_better"),
        ("fill_rate", "higher_is_better"),
    ]

    for metric, direction in metrics_to_rank:
        values = [(r.name, getattr(r, metric)) for r in results if getattr(r, metric) is not None]
        if not values:
            continue
        reverse = direction == "higher_is_better"
        ranked = sorted(values, key=lambda x: x[1], reverse=reverse)
        comparison["rankings"][metric] = [
            {"name": name, "value": value, "rank": i + 1} for i, (name, value) in enumerate(ranked)
        ]

    # Pairwise Brier comparison
    for i, r1 in enumerate(results):
        for r2 in results[i + 1 :]:
            if r1.brier_score is not None and r2.brier_score is not None:
                brier_change = r2.brier_score - r1.brier_score
                comparison["pairwise"].append(
                    dict(
                        strategy_a=r1.name,
                        strategy_b=r2.name,
                        brier_change=brier_change,
                        interpretation=(
                            f"{r1.name} is better by {abs(brier_change):.4f}"
                            if brier_change > 0
                            else f"{r2.name} is better by {abs(brier_change):.4f}"
                            if brier_change < 0
                            else "identical Brier scores"
                        ),
                    )
                )

    # Summary
    total_fills = sum(r.total_fills for r in results)
    comparison["summary"] = {
        "total_fills_all_strategies": total_fills,
        "strategies_with_positive_pnl": sum(
            1 for r in results if r.net_pnl is not None and r.net_pnl > 0
        ),
        "strategies_with_edge": sum(
            1
            for r in results
            if r.average_predicted_edge is not None and r.average_predicted_edge > 0
        ),
        "warning": (
            "No strategy result establishes profitability;"
            " this is a research comparison only"
        ),
    }

    return comparison


def strategy_report(results: list[StrategyResult]) -> str:
    """Generate a human-readable comparison report."""
    if not results:
        return "No strategies to compare."

    lines = ["=" * 60, "STRATEGY COMPARISON REPORT", "=" * 60, ""]

    for r in results:
        lines.append(f"Strategy: {r.name}")
        lines.append(f"  Description: {r.description}")
        lines.append(
            f"  Brier Score: {r.brier_score:.4f}" if r.brier_score else "  Brier Score: N/A"
        )
        lines.append(f"  Log Loss: {r.log_loss:.4f}" if r.log_loss else "  Log Loss: N/A")
        lines.append(f"  Net PnL: ${r.net_pnl:.2f}" if r.net_pnl else "  Net PnL: N/A")
        lines.append(
            f"  Max Drawdown: {r.max_drawdown:.2%}" if r.max_drawdown else "  Max Drawdown: N/A"
        )
        lines.append(f"  Sharpe: {r.sharpe_ratio:.2f}" if r.sharpe_ratio else "  Sharpe: N/A")
        lines.append(f"  Fills: {r.total_fills}")
        lines.append(
            f"  Avg Predicted Edge: {r.average_predicted_edge:.4f}"
            if r.average_predicted_edge
            else "  Avg Predicted Edge: N/A"
        )
        lines.append(
            f"  Calibration Error: {r.calibration_error:.4f}"
            if r.calibration_error
            else "  Calibration Error: N/A"
        )
        lines.append(f"  Resolved Markets: {r.resolved_markets}")
        lines.append(f"  Independent Clusters: {r.independent_clusters}")
        lines.append("")

    comparison = compare_strategies(results)
    if comparison.get("rankings"):
        lines.append("Rankings:")
        for metric, ranked in comparison["rankings"].items():
            lines.append(f"  {metric}:")
            for entry in ranked:
                lines.append(f"    #{entry['rank']}: {entry['name']} = {entry['value']:.4f}")
        lines.append("")

    lines.append(
        "WARNING: No result establishes profitability. This comparison helps "
        "identify which components contribute value, not whether the system is profitable."
    )
    return "\n".join(lines)


def result_from_report(report: dict, name: str, description: str = "") -> StrategyResult:
    """Convert a backtest Engine.run() report dict to a StrategyResult."""
    report.get("fill_journal", [])
    report.get("equity", [])
    cal = report.get("calibration")

    # Compute fill rate
    decisions = report.get("decisions", [])
    queued = sum(1 for d in decisions if d.get("reason") == "queued")
    filled = sum(1 for d in decisions if d.get("reason") == "filled")
    fill_rate = filled / queued if queued else None

    # Average predicted edge
    edge_decisions = [d for d in decisions if d.get("reason") == "filled" and "net_edge" in d]
    avg_edge = (
        sum(float(d["net_edge"]) for d in edge_decisions) / len(edge_decisions)
        if edge_decisions
        else None
    )

    return StrategyResult(
        name=name,
        description=description,
        brier_score=cal["brier"] if cal else None,
        log_loss=cal["log_loss"] if cal else None,
        net_pnl=float(report["net_pnl"]) if report.get("net_pnl") is not None else None,
        gross_pnl=float(report["gross_pnl"]) if report.get("gross_pnl") is not None else None,
        max_drawdown=float(report["max_drawdown"])
        if report.get("max_drawdown") is not None
        else None,
        sharpe_ratio=float(report["sharpe_ratio"])
        if report.get("sharpe_ratio") is not None
        else None,
        total_fills=report.get("fills", 0),
        fill_rate=fill_rate,
        average_predicted_edge=avg_edge,
        calibration_error=cal["ece"] if cal else None,
        resolved_markets=report.get("resolved_markets", 0),
        independent_clusters=cal.get("independent_clusters", 0) if cal else 0,
        period_start=report.get("period_start"),
        period_end=report.get("period_end"),
    )


def main(argv: list[str] | None = None):
    """Entry point for standalone strategy comparison."""
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Compare strategy backtest results")
    parser.add_argument(
        "--reports", nargs="+", required=True, help="Paths to strategy report JSONs"
    )
    parser.add_argument("--output", help="Output path for results JSON")
    args = parser.parse_args(argv)

    results = []
    for path in args.reports:
        with open(path, encoding="utf-8") as f:
            report = json.load(f)
        results.append(result_from_report(report))

    comparison = compare_strategies(results)

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(comparison, f, indent=2, default=str)
        print(json.dumps({"event": "compare_complete", "output": args.output}))
    else:
        print(json.dumps(comparison, indent=2, default=str))
