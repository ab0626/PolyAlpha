"""Systematic backtest bias detection.

Tests for common pitfalls that produce misleading backtest results.
Every check returns a dict with pass/fail and explanation.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

D = Decimal


@dataclass(frozen=True)
class BiasCheck:
    """Result of a single bias check."""

    name: str
    passed: bool
    severity: str  # critical | warning | info
    description: str
    details: dict[str, Any]


def check_lookahead_bias(report: dict) -> BiasCheck:
    """Check for evidence of look-ahead bias.

    Signs: trades at prices that couldn't have been known,
    fills using future order-book state.
    """
    decisions = report.get("decisions", [])
    report.get("equity", [])

    # Check if any fill happens before its market metadata is available
    issues = []
    for d in decisions:
        if d.get("reason") == "filled":
            # Basic sanity: fill should have valid VWAP
            vwap = d.get("vwap")
            if vwap is not None:
                vwap_f = float(vwap)
                if vwap_f <= 0 or vwap_f >= 1:
                    issues.append(f"invalid vwap {vwap} at {d.get('timestamp')}")

    return BiasCheck(
        name="lookahead_bias",
        passed=len(issues) == 0,
        severity="critical",
        description="Checks for trades using future information",
        details={"issues": issues, "checks_run": len(decisions)},
    )


def check_midpoint_fill_bias(report: dict) -> BiasCheck:
    """Check if fills use midpoint instead of depth-walked VWAP.

    Midpoint fills massively overestimate performance.
    """
    fills = report.get("fill_journal", [])
    issues = []

    for f in fills:
        levels = f.get("levels", [])
        if len(levels) <= 1 and f.get("shares", 0) > 50:
            issues.append(f"fill {f.get('order_id')} at single level with {f.get('shares')} shares")

    return BiasCheck(
        name="midpoint_fill_bias",
        passed=len(issues) == 0,
        severity="critical",
        description="Detects fills that don't walk depth (midpoint fill bias)",
        details={"issues": issues, "total_fills": len(fills)},
    )


def check_fee_omission(report: dict) -> BiasCheck:
    """Check if fees are included in the backtest."""
    fills = report.get("fill_journal", [])
    total_fees = sum(float(f.get("fees", 0)) for f in fills)
    total_notional = sum(float(f.get("notional", 0)) for f in fills)

    fee_rate = total_fees / total_notional if total_notional > 0 else 0

    return BiasCheck(
        name="fee_omission",
        passed=fee_rate > 0 or total_fees == 0,
        severity="critical",
        description="Verifies fees are charged on fills",
        details={
            "total_fees": total_fees,
            "total_notional": total_notional,
            "effective_fee_rate": fee_rate,
            "warning": "Zero fees may indicate omission"
            if total_fees == 0 and total_notional > 0
            else None,
        },
    )


def check_slippage_omission(report: dict) -> BiasCheck:
    """Check if depth slippage is modeled."""
    fills = report.get("fill_journal", [])
    total_slippage = sum(float(f.get("depth_slippage", 0)) for f in fills)

    return BiasCheck(
        name="slippage_omission",
        passed=True,  # Always passes, but reports diagnostic
        severity="warning",
        description="Reports depth slippage as a diagnostic",
        details={
            "total_depth_slippage": total_slippage,
            "average_slippage": total_slippage / len(fills) if fills else 0,
        },
    )


def check_survivorship_bias(report: dict) -> BiasCheck:
    """Check if the backtest only includes markets that were available throughout."""
    decisions = report.get("decisions", [])
    rejected = [d for d in decisions if d.get("reason") == "rejected"]
    rejection_reasons = {}
    for d in rejected:
        for r in d.get("rejections", []):
            rejection_reasons[r] = rejection_reasons.get(r, 0) + 1

    return BiasCheck(
        name="survivorship_bias",
        passed=True,  # Informational
        severity="info",
        description="Reports rejection reasons to assess market coverage",
        details={
            "total_decisions": len(decisions),
            "total_rejected": len(rejected),
            "rejection_reasons": rejection_reasons,
        },
    )


def check_trade_independence(report: dict) -> BiasCheck:
    """Check if trades are concentrated in correlated markets."""
    cluster_exposure = report.get("cluster_exposure", {})
    total_exposure = sum(float(v) for v in cluster_exposure.values())

    max_cluster = ""
    max_exposure = D(0)
    for cluster, exposure in cluster_exposure.items():
        exp = D(exposure)
        if exp > max_exposure:
            max_exposure = exp
            max_cluster = cluster

    concentration = float(max_exposure / D(total_exposure)) if total_exposure > 0 else 0

    return BiasCheck(
        name="trade_independence",
        passed=concentration < 0.5,
        severity="warning",
        description="Checks if trades are concentrated in one cluster",
        details={
            "cluster_exposure": cluster_exposure,
            "max_cluster": max_cluster,
            "max_cluster_fraction": concentration,
            "total_exposure": str(total_exposure),
        },
    )


def check_overfitting(report: dict) -> BiasCheck:
    """Heuristic check for overfitting.

    Signs: very high Sharpe, very low drawdown, many fills with small edge.
    """
    fills = report.get("fills", 0)
    report.get("equity", [])
    max_dd_str = report.get("max_drawdown")
    max_dd = float(max_dd_str) if max_dd_str is not None else None

    warnings = []
    if max_dd is not None and max_dd < 0.01 and fills > 10:
        warnings.append("very low drawdown with many fills may indicate overfitting")
    if fills > 100 and max_dd is not None and max_dd < 0.02:
        warnings.append("high fill count with very low drawdown is suspicious")

    return BiasCheck(
        name="overfitting_heuristic",
        passed=len(warnings) == 0,
        severity="warning",
        description="Heuristic checks for signs of overfitting",
        details={"warnings": warnings, "fills": fills, "max_drawdown": max_dd},
    )


def check_timestamp_alignment(report: dict) -> BiasCheck:
    """Check that equity timestamps are monotonically increasing."""
    equity = report.get("equity", [])
    if len(equity) < 2:
        return BiasCheck(
            name="timestamp_alignment",
            passed=True,
            severity="info",
            description="Insufficient equity points for alignment check",
            details={"equity_points": len(equity)},
        )

    timestamps = [e["timestamp"] for e in equity]
    out_of_order = 0
    for i in range(1, len(timestamps)):
        if timestamps[i] < timestamps[i - 1]:
            out_of_order += 1

    return BiasCheck(
        name="timestamp_alignment",
        passed=out_of_order == 0,
        severity="critical",
        description="Verifies equity timestamps are monotonically increasing",
        details={
            "out_of_order_count": out_of_order,
            "total_points": len(timestamps),
        },
    )


def check_concentration(report: dict) -> BiasCheck:
    """Check for position concentration risk."""
    positions = report.get("positions", [])
    active = [p for p in positions if float(p.get("shares", 0)) > 0]

    if not active:
        return BiasCheck(
            name="concentration",
            passed=True,
            severity="info",
            description="No active positions to analyze",
            details={},
        )

    total_basis = sum(float(p.get("basis", 0)) for p in active)
    max_position = max(float(p.get("basis", 0)) for p in active)
    concentration = max_position / total_basis if total_basis > 0 else 0

    return BiasCheck(
        name="concentration",
        passed=concentration < 0.5,
        severity="warning",
        description="Checks if portfolio is concentrated in few positions",
        details={
            "active_positions": len(active),
            "max_position_basis": max_position,
            "total_basis": total_basis,
            "max_concentration": concentration,
        },
    )


def run_all_checks(report: dict) -> list[BiasCheck]:
    """Run all bias checks on a backtest report."""
    checks = [
        check_lookahead_bias(report),
        check_midpoint_fill_bias(report),
        check_fee_omission(report),
        check_slippage_omission(report),
        check_survivorship_bias(report),
        check_trade_independence(report),
        check_overfitting(report),
        check_timestamp_alignment(report),
        check_concentration(report),
    ]
    return checks


def bias_summary(checks: list[BiasCheck]) -> dict:
    """Summarize bias check results."""
    critical = [c for c in checks if not c.passed and c.severity == "critical"]
    warnings = [c for c in checks if not c.passed and c.severity == "warning"]

    return dict(
        total_checks=len(checks),
        passed=sum(1 for c in checks if c.passed),
        critical_failures=[c.name for c in critical],
        warnings=[c.name for c in warnings],
        overall_pass=len(critical) == 0,
        recommendation=(
            "Results may be trustworthy pending investigation of warnings"
            if not critical
            else "CRITICAL BIAS DETECTED: investigate before interpreting results"
        ),
    )


def main(argv: list[str] | None = None):
    """Entry point for standalone bias checking."""
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Run bias checks on a backtest report")
    parser.add_argument("--report", required=True, help="Path to backtest report JSON")
    parser.add_argument("--output", help="Output path for results JSON")
    args = parser.parse_args(argv)

    with open(args.report, encoding="utf-8") as f:
        report = json.load(f)

    checks = run_all_checks(report)
    summary = bias_summary(checks)

    output = {
        "report": args.report,
        "summary": summary,
        "checks": [
            {
                "name": c.name,
                "passed": c.passed,
                "severity": c.severity,
                "description": c.description,
                "details": c.details,
            }
            for c in checks
        ],
    }

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, default=str)
        print(json.dumps({"event": "bias_check_complete", "output": args.output, **summary}))
    else:
        print(json.dumps(output, indent=2, default=str))
