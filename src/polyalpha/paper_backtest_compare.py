"""Paper vs Backtest comparison — identify divergence between modes.

Compares paper trading decisions with backtest decisions over overlapping
periods to detect discrepancies in signal count, entry prices, fill rates,
slippage, PnL, and edge realization.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

D = Decimal


@dataclass(frozen=True)
class PeriodComparison:
    """Comparison metrics for a specific time period."""

    period_start: str
    period_end: str
    paper_signal_count: int
    backtest_signal_count: int
    signal_count_diff: int
    signal_count_diff_pct: Decimal
    paper_avg_entry: Decimal
    backtest_avg_entry: Decimal
    entry_price_diff: Decimal
    paper_fill_rate: Decimal
    backtest_fill_rate: Decimal
    fill_rate_diff: Decimal
    paper_avg_slippage: Decimal
    backtest_avg_slippage: Decimal
    slippage_diff: Decimal
    paper_pnl: Decimal
    backtest_pnl: Decimal
    pnl_diff: Decimal
    paper_edge_realization: Decimal
    backtest_edge_realization: Decimal
    edge_diff: Decimal

    def summary(self) -> dict:
        return {
            "period": f"{self.period_start} to {self.period_end}",
            "signal_count_diff": self.signal_count_diff,
            "entry_price_diff": str(self.entry_price_diff),
            "fill_rate_diff": str(self.fill_rate_diff),
            "slippage_diff": str(self.slippage_diff),
            "pnl_diff": str(self.pnl_diff),
            "edge_diff": str(self.edge_diff),
        }


@dataclass(frozen=True)
class ComparisonReport:
    """Full comparison report between paper and backtest."""

    periods: list[PeriodComparison] = field(default_factory=list)
    total_paper_signals: int = 0
    total_backtest_signals: int = 0
    total_paper_pnl: Decimal = D(0)
    total_backtest_pnl: Decimal = D(0)
    total_pnl_diff: Decimal = D(0)
    avg_entry_discrepancy: Decimal = D(0)
    avg_slippage_discrepancy: Decimal = D(0)
    flags: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "total_paper_signals": self.total_paper_signals,
            "total_backtest_signals": self.total_backtest_signals,
            "total_paper_pnl": str(self.total_paper_pnl),
            "total_backtest_pnl": str(self.total_backtest_pnl),
            "total_pnl_diff": str(self.total_pnl_diff),
            "flags": self.flags,
            "periods": [p.summary() for p in self.periods],
        }


def _classify_decision(decision: dict) -> str:
    """Classify a decision into a time bucket for matching."""
    return decision.get("market_id", "") + "_" + decision.get("side", "")


def _match_decisions(
    paper: list[dict],
    backtest: list[dict],
) -> list[tuple[dict | None, dict | None]]:
    """Match paper and backtest decisions by signal/market alignment."""
    matched: list[tuple[dict | None, dict | None]] = []
    used_backtest: set[int] = set()

    for p in paper:
        p_market = p.get("market_id", "")
        p_side = p.get("side", "")
        p_time = p.get("timestamp", "")
        best_match = None
        best_idx = -1

        for i, b in enumerate(backtest):
            if i in used_backtest:
                continue
            if b.get("market_id") == p_market and b.get("side") == p_side:
                b_time = b.get("timestamp", "")
                # Prefer closest timestamp match
                if best_match is None or abs(hash(b_time) - hash(p_time)) < abs(
                    hash(best_match.get("timestamp", "")) - hash(p_time)
                ):
                    best_match = b
                    best_idx = i

        if best_idx >= 0:
            used_backtest.add(best_idx)
        matched.append((p, best_match))

    return matched


def _compute_edge_realization(decisions: list[dict]) -> Decimal:
    """Compute average edge realization: (forecast - entry) * direction."""
    if not decisions:
        return D(0)
    edges = []
    for d in decisions:
        forecast = D(str(d.get("forecast", "0.5")))
        entry = D(str(d.get("entry_price", "0.5")))
        side = d.get("side", "YES")
        if side in ("YES", "BUY"):
            edge = forecast - entry
        else:
            edge = entry - (D(1) - forecast)
        edges.append(edge)
    return sum(edges) / D(len(edges))


def compare_paper_backtest(
    paper_decisions: list[dict],
    backtest_decisions: list[dict],
    overlap_period: tuple[str, str] | None = None,
) -> ComparisonReport:
    """Compare paper trading decisions with backtest decisions.

    For overlapping periods, computes differences in signal count,
    entry prices, fill rates, slippage, PnL, and edge realization.
    Flags large discrepancies for investigation.

    Args:
        paper_decisions: List of paper decision dicts with keys:
            - market_id, side, entry_price, forecast, fill_rate,
              slippage, pnl, timestamp, edge
        backtest_decisions: List of backtest decision dicts with same keys.
        overlap_period: Optional (start, end) ISO timestamp strings
            to filter to overlapping period only.

    Returns:
        ComparisonReport with per-period metrics and flagged discrepancies.
    """
    # Filter to overlap period if specified
    if overlap_period:
        start, end = overlap_period
        paper_decisions = [
            d for d in paper_decisions if start <= d.get("timestamp", "") <= end
        ]
        backtest_decisions = [
            d for d in backtest_decisions if start <= d.get("timestamp", "") <= end
        ]

    # Group decisions into periods (by day)
    paper_by_day: dict[str, list[dict]] = {}
    backtest_by_day: dict[str, list[dict]] = {}

    for d in paper_decisions:
        day = d.get("timestamp", "")[:10]
        paper_by_day.setdefault(day, []).append(d)

    for d in backtest_decisions:
        day = d.get("timestamp", "")[:10]
        backtest_by_day.setdefault(day, []).append(d)

    all_days = sorted(set(list(paper_by_day.keys()) + list(backtest_by_day.keys())))
    if not all_days:
        return ComparisonReport()

    periods: list[PeriodComparison] = []
    total_paper_signals = 0
    total_bt_signals = 0
    total_paper_pnl = D(0)
    total_bt_pnl = D(0)
    entry_diffs: list[Decimal] = []
    slip_diffs: list[Decimal] = []
    flags: list[str] = []

    for day in all_days:
        p_day = paper_by_day.get(day, [])
        b_day = backtest_by_day.get(day, [])

        p_count = len(p_day)
        b_count = len(b_day)
        count_diff = p_count - b_count
        count_pct = (
            (D(count_diff) / D(b_count) * D(100)) if b_count > 0 else D(0)
        )

        # Entry price comparison
        p_entries = [D(str(d.get("entry_price", "0.5"))) for d in p_day]
        b_entries = [D(str(d.get("entry_price", "0.5"))) for d in b_day]
        p_avg_entry = sum(p_entries) / D(len(p_entries)) if p_entries else D(0)
        b_avg_entry = sum(b_entries) / D(len(b_entries)) if b_entries else D(0)
        entry_diff = p_avg_entry - b_avg_entry

        # Fill rate comparison
        p_fills = [D(str(d.get("fill_rate", "1"))) for d in p_day]
        b_fills = [D(str(d.get("fill_rate", "1"))) for d in b_day]
        p_fill_rate = sum(p_fills) / D(len(p_fills)) if p_fills else D(0)
        b_fill_rate = sum(b_fills) / D(len(b_fills)) if b_fills else D(0)
        fill_diff = p_fill_rate - b_fill_rate

        # Slippage comparison
        p_slips = [D(str(d.get("slippage", "0"))) for d in p_day]
        b_slips = [D(str(d.get("slippage", "0"))) for d in b_day]
        p_avg_slip = sum(p_slips) / D(len(p_slips)) if p_slips else D(0)
        b_avg_slip = sum(b_slips) / D(len(b_slips)) if b_slips else D(0)
        slip_diff = p_avg_slip - b_avg_slip

        # PnL comparison
        p_pnl = sum(D(str(d.get("pnl", "0"))) for d in p_day)
        b_pnl = sum(D(str(d.get("pnl", "0"))) for d in b_day)
        pnl_diff = p_pnl - b_pnl

        # Edge realization
        p_edge = _compute_edge_realization(p_day)
        b_edge = _compute_edge_realization(b_day)
        edge_diff = p_edge - b_edge

        periods.append(
            PeriodComparison(
                period_start=day,
                period_end=day,
                paper_signal_count=p_count,
                backtest_signal_count=b_count,
                signal_count_diff=count_diff,
                signal_count_diff_pct=count_pct.quantize(D("0.01")),
                paper_avg_entry=p_avg_entry.quantize(D("0.0001")),
                backtest_avg_entry=b_avg_entry.quantize(D("0.0001")),
                entry_price_diff=entry_diff.quantize(D("0.0001")),
                paper_fill_rate=p_fill_rate.quantize(D("0.0001")),
                backtest_fill_rate=b_fill_rate.quantize(D("0.0001")),
                fill_rate_diff=fill_diff.quantize(D("0.0001")),
                paper_avg_slippage=p_avg_slip.quantize(D("0.0001")),
                backtest_avg_slippage=b_avg_slip.quantize(D("0.0001")),
                slippage_diff=slip_diff.quantize(D("0.0001")),
                paper_pnl=p_pnl.quantize(D("0.01")),
                backtest_pnl=b_pnl.quantize(D("0.01")),
                pnl_diff=pnl_diff.quantize(D("0.01")),
                paper_edge_realization=p_edge.quantize(D("0.0001")),
                backtest_edge_realization=b_edge.quantize(D("0.0001")),
                edge_diff=edge_diff.quantize(D("0.0001")),
            )
        )

        total_paper_signals += p_count
        total_bt_signals += b_count
        total_paper_pnl += p_pnl
        total_bt_pnl += b_pnl
        entry_diffs.append(entry_diff)
        slip_diffs.append(slip_diff)

        # Flag large discrepancies
        if abs(count_diff) > max(p_count, b_count) * 0.2 and max(p_count, b_count) > 5:
            flags.append(f"Day {day}: signal count differs by {count_diff} ({count_pct}%)")
        if abs(pnl_diff) > D("10"):
            flags.append(f"Day {day}: PnL differs by {pnl_diff}")
        if abs(entry_diff) > D("0.05"):
            flags.append(f"Day {day}: avg entry price differs by {entry_diff}")

    total_pnl_diff = total_paper_pnl - total_bt_pnl
    avg_entry_disc = (
        sum(entry_diffs) / D(len(entry_diffs)) if entry_diffs else D(0)
    )
    avg_slip_disc = sum(slip_diffs) / D(len(slip_diffs)) if slip_diffs else D(0)

    return ComparisonReport(
        periods=periods,
        total_paper_signals=total_paper_signals,
        total_backtest_signals=total_bt_signals,
        total_paper_pnl=total_paper_pnl.quantize(D("0.01")),
        total_backtest_pnl=total_bt_pnl.quantize(D("0.01")),
        total_pnl_diff=total_pnl_diff.quantize(D("0.01")),
        avg_entry_discrepancy=avg_entry_disc.quantize(D("0.0001")),
        avg_slippage_discrepancy=avg_slip_disc.quantize(D("0.0001")),
        flags=flags,
    )
