"""Concentration analysis for profit attribution and risk assessment.

Part 36: Measures how concentrated profits are across markets, events,
clusters, categories, or strategies. Flags when a small number of entities
dominate total performance.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import TypeAlias

D = Decimal

GroupKey: TypeAlias = str


@dataclass(frozen=True)
class GroupContribution:
    """Profit contribution of a single group."""

    group: GroupKey
    total_pnl: float
    trade_count: int
    avg_pnl_per_trade: float
    contribution_pct: float  # percentage of total absolute PnL
    cumulative_pct: float  # running cumulative percentage


@dataclass(frozen=True)
class ConcentrationReport:
    """Concentration analysis results."""

    group_by: str
    total_groups: int
    total_pnl: float
    top_1_contribution: float  # % of total abs PnL from largest group
    top_5_contribution: float
    top_10_contribution: float
    herfindahl_index: float  # sum of squared market shares (0-1)
    groups: list[GroupContribution]
    is_concentrated: bool  # True if top-1 > 50% of profits
    concentration_warning: str

    def summary(self) -> dict:
        return {
            "group_by": self.group_by,
            "total_groups": self.total_groups,
            "total_pnl": self.total_pnl,
            "top_1_contribution": self.top_1_contribution,
            "top_5_contribution": self.top_5_contribution,
            "top_10_contribution": self.top_10_contribution,
            "herfindahl_index": self.herfindahl_index,
            "is_concentrated": self.is_concentrated,
            "concentration_warning": self.concentration_warning,
        }


@dataclass(frozen=True)
class Decision:
    """A trading decision with outcome."""

    group_key: GroupKey  # market_id, event_id, cluster, category, or strategy
    pnl: float  # realized profit/loss
    trade_count: int = 1


def analyze_concentration(
    decisions: list[Decision],
    group_by: str = "market",
) -> ConcentrationReport:
    """Analyze profit concentration across groups.

    Args:
        decisions: List of trading decisions with PnL.
        group_by: Dimension to group by: market/event/cluster/category/strategy.

    Returns:
        ConcentrationReport with per-group metrics and concentration flags.
    """
    if not decisions:
        return _empty_report(group_by)

    # Aggregate PnL by group
    group_pnl: dict[GroupKey, float] = defaultdict(float)
    group_trades: dict[GroupKey, int] = defaultdict(int)

    for d in decisions:
        group_pnl[d.group_key] += d.pnl
        group_trades[d.group_key] += d.trade_count

    total_pnl = sum(group_pnl.values())
    total_abs_pnl = sum(abs(v) for v in group_pnl.values())

    # Sort by absolute PnL descending
    sorted_groups = sorted(group_pnl.items(), key=lambda x: -abs(x[1]))

    # Compute contributions
    contributions: list[GroupContribution] = []
    cumulative = 0.0
    for group, pnl in sorted_groups:
        abs_pnl = abs(pnl)
        contrib_pct = (abs_pnl / total_abs_pnl * 100) if total_abs_pnl > 0 else 0.0
        cumulative += contrib_pct
        trades = group_trades[group]
        contributions.append(
            GroupContribution(
                group=group,
                total_pnl=pnl,
                trade_count=trades,
                avg_pnl_per_trade=pnl / trades if trades > 0 else 0.0,
                contribution_pct=contrib_pct,
                cumulative_pct=cumulative,
            )
        )

    # Top-k contributions
    top_1 = contributions[0].cumulative_pct if len(contributions) >= 1 else 0.0
    top_5 = contributions[min(4, len(contributions) - 1)].cumulative_pct if contributions else 0.0
    top_10 = contributions[min(9, len(contributions) - 1)].cumulative_pct if contributions else 0.0

    # Herfindahl-Hirschman Index (on absolute PnL shares)
    hhi = 0.0
    if total_abs_pnl > 0:
        for _, pnl in group_pnl.items():
            share = abs(pnl) / total_abs_pnl
            hhi += share * share

    # Concentration flag: top-1 contributes >50% of absolute PnL
    is_concentrated = top_1 > 50.0
    warning = ""
    if is_concentrated:
        top_group = contributions[0].group
        warning = (
            f"CONCENTRATION WARNING: Top group '{top_group}' contributes "
            f"{top_1:.1f}% of total absolute PnL. "
            f"HHI={hhi:.4f} (0=uniform, 1=single group)."
        )
    elif hhi > 0.25:
        warning = (
            f"Moderate concentration: HHI={hhi:.4f}. "
            f"Top group '{contributions[0].group}' contributes {top_1:.1f}%."
        )

    return ConcentrationReport(
        group_by=group_by,
        total_groups=len(group_pnl),
        total_pnl=total_pnl,
        top_1_contribution=top_1,
        top_5_contribution=top_5,
        top_10_contribution=top_10,
        herfindahl_index=hhi,
        groups=contributions,
        is_concentrated=is_concentrated,
        concentration_warning=warning,
    )


def _empty_report(group_by: str) -> ConcentrationReport:
    """Return an empty report for no data."""
    return ConcentrationReport(
        group_by=group_by,
        total_groups=0,
        total_pnl=0.0,
        top_1_contribution=0.0,
        top_5_contribution=0.0,
        top_10_contribution=0.0,
        herfindahl_index=0.0,
        groups=[],
        is_concentrated=False,
        concentration_warning="No decisions to analyze",
    )
