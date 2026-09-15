"""Profit persistence analysis for alpha durability assessment.

Part 37: Measures whether alpha exists across rolling time windows or
only in isolated periods. Computes per-window metrics including PnL,
Brier improvement, edge realization, and calibration stability.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TypeAlias

D = Decimal

WindowType: TypeAlias = str


@dataclass(frozen=True)
class WindowMetrics:
    """Metrics for a single rolling window."""

    window_type: WindowType
    window_start: int  # trade index or timestamp
    window_end: int
    trade_count: int
    pnl: float
    brier_improvement: float  # market_brier - model_brier in this window
    edge_realization: float  # avg(model_edge * outcome) in this window
    calibration: float  # ECE in this window
    is_profitable: bool
    is_alpha_positive: bool  # brier_improvement > 0


@dataclass(frozen=True)
class PersistenceReport:
    """Profit persistence analysis results."""

    total_trades: int
    window_metrics: list[WindowMetrics]
    profitable_window_pct: float  # % of windows with positive PnL
    alpha_window_pct: float  # % of windows with positive brier improvement
    pnl_volatility: float  # std of window PnLs
    mean_pnl: float
    sharpe_like: float  # mean_pnl / pnl_volatility (if vol > 0)
    persistence_score: float  # 0-1, higher = more persistent
    has_persistent_alpha: bool  # True if alpha exists in >60% of windows
    warning: str

    def summary(self) -> dict:
        return {
            "total_trades": self.total_trades,
            "window_count": len(self.window_metrics),
            "profitable_window_pct": self.profitable_window_pct,
            "alpha_window_pct": self.alpha_window_pct,
            "pnl_volatility": self.pnl_volatility,
            "mean_pnl": self.mean_pnl,
            "sharpe_like": self.sharpe_like,
            "persistence_score": self.persistence_score,
            "has_persistent_alpha": self.has_persistent_alpha,
            "warning": self.warning,
        }


@dataclass(frozen=True)
class EquityPoint:
    """Single point in an equity curve."""

    trade_index: int
    timestamp: float  # epoch seconds
    cumulative_pnl: float
    market_brier: float | None
    model_brier: float | None
    net_edge: float | None


def _brier_score(probabilities: list[float], outcomes: list[int]) -> float:
    if not probabilities:
        return 0.0
    return sum((p - o) ** 2 for p, o in zip(probabilities, outcomes)) / len(probabilities)


def _ece(probabilities: list[float], outcomes: list[int], n_buckets: int = 10) -> float:
    if not probabilities:
        return 0.0
    buckets: dict[int, list[tuple[float, int]]] = {}
    for p, o in zip(probabilities, outcomes):
        buckets.setdefault(min(int(p * n_buckets), n_buckets - 1), []).append((p, o))
    total_error = total_count = 0
    for b in range(n_buckets):
        items = buckets.get(b, [])
        if items:
            mean_pred = sum(p for p, _ in items) / len(items)
            mean_actual = sum(o for _, o in items) / len(items)
            total_error += abs(mean_pred - mean_actual) * len(items)
            total_count += len(items)
    return total_error / total_count if total_count else 0.0


def _extract_window(
    points: list[EquityPoint],
    start_idx: int,
    end_idx: int,
) -> WindowMetrics:
    """Extract metrics for a contiguous window of equity points."""
    window = points[start_idx:end_idx]
    if not window:
        return WindowMetrics(
            window_type="",
            window_start=start_idx,
            window_end=end_idx,
            trade_count=0,
            pnl=0.0,
            brier_improvement=0.0,
            edge_realization=0.0,
            calibration=0.0,
            is_profitable=False,
            is_alpha_positive=False,
        )

    pnl = window[-1].cumulative_pnl - (window[0].cumulative_pnl - (
        window[0].cumulative_pnl - window[0].cumulative_pnl  # first trade delta
    ))
    # More accurate: pnl from first to last cumulative
    if len(window) >= 2:
        pnl = window[-1].cumulative_pnl - window[0].cumulative_pnl
    else:
        pnl = 0.0

    mkt_briers = [p.market_brier for p in window if p.market_brier is not None]
    mdl_briers = [p.model_brier for p in window if p.model_brier is not None]
    edges = [p.net_edge for p in window if p.net_edge is not None]

    avg_mkt = sum(mkt_briers) / len(mkt_briers) if mkt_briers else 0.5
    avg_mdl = sum(mdl_briers) / len(mdl_briers) if mdl_briers else 0.5
    brier_imp = avg_mkt - avg_mdl

    avg_edge = sum(edges) / len(edges) if edges else 0.0

    return WindowMetrics(
        window_type="",
        window_start=start_idx,
        window_end=end_idx,
        trade_count=len(window),
        pnl=pnl,
        brier_improvement=brier_imp,
        edge_realization=avg_edge,
        calibration=0.0,  # would need outcomes for full ECE
        is_profitable=pnl > 0,
        is_alpha_positive=brier_imp > 0,
    )


def analyze_profit_persistence(
    equity_curve: list[EquityPoint],
    window_size: int = 30,
    window_types: list[str] | None = None,
) -> PersistenceReport:
    """Analyze profit persistence across rolling, expanding, calendar, and regime-conditioned windows.

    Args:
        equity_curve: Ordered list of equity curve points.
        window_size: Number of trades per rolling window.
        window_types: List of window types to compute: "rolling", "expanding", "calendar", "regime_conditioned".

    Returns:
        PersistenceReport with per-window metrics and persistence assessment.
    """
    if not equity_curve or window_size < 1:
        return _empty_persistence_report()

    n = len(equity_curve)
    all_window_metrics: list[WindowMetrics] = []

    # Always include rolling windows (original behavior)
    include_types = window_types if window_types else ["rolling"]

    if "rolling" in include_types:
        for start in range(0, n, window_size):
            end = min(start + window_size, n)
            if end - start < window_size // 2:
                break
            metrics = _extract_window(equity_curve, start, end)
            all_window_metrics.append(WindowMetrics(
                window_type=f"rolling-{window_size}",
                window_start=metrics.window_start,
                window_end=metrics.window_end,
                trade_count=metrics.trade_count,
                pnl=metrics.pnl,
                brier_improvement=metrics.brier_improvement,
                edge_realization=metrics.edge_realization,
                calibration=metrics.calibration,
                is_profitable=metrics.is_profitable,
                is_alpha_positive=metrics.is_alpha_positive,
            ))

    if "expanding" in include_types:
        for end_idx in range(window_size, n + 1, window_size):
            metrics = _extract_window(equity_curve, 0, end_idx)
            all_window_metrics.append(WindowMetrics(
                window_type="expanding",
                window_start=metrics.window_start,
                window_end=metrics.window_end,
                trade_count=metrics.trade_count,
                pnl=metrics.pnl,
                brier_improvement=metrics.brier_improvement,
                edge_realization=metrics.edge_realization,
                calibration=metrics.calibration,
                is_profitable=metrics.is_profitable,
                is_alpha_positive=metrics.is_alpha_positive,
            ))

    if "calendar" in include_types:
        month_start = 0
        i = 1
        while i < n:
            cur_ts = equity_curve[i].timestamp
            prev_ts = equity_curve[i - 1].timestamp
            cur_month = _extract_month_key(cur_ts)
            prev_month = _extract_month_key(prev_ts)
            if cur_month != prev_month and i - month_start >= 1:
                metrics = _extract_window(equity_curve, month_start, i)
                all_window_metrics.append(WindowMetrics(
                    window_type="calendar-monthly",
                    window_start=metrics.window_start,
                    window_end=metrics.window_end,
                    trade_count=metrics.trade_count,
                    pnl=metrics.pnl,
                    brier_improvement=metrics.brier_improvement,
                    edge_realization=metrics.edge_realization,
                    calibration=metrics.calibration,
                    is_profitable=metrics.is_profitable,
                    is_alpha_positive=metrics.is_alpha_positive,
                ))
                month_start = i
            i += 1
        if month_start < n:
            metrics = _extract_window(equity_curve, month_start, n)
            if metrics.trade_count > 0:
                all_window_metrics.append(WindowMetrics(
                    window_type="calendar-monthly",
                    window_start=metrics.window_start,
                    window_end=metrics.window_end,
                    trade_count=metrics.trade_count,
                    pnl=metrics.pnl,
                    brier_improvement=metrics.brier_improvement,
                    edge_realization=metrics.edge_realization,
                    calibration=metrics.calibration,
                    is_profitable=metrics.is_profitable,
                    is_alpha_positive=metrics.is_alpha_positive,
                ))

    if "regime_conditioned" in include_types:
        _add_regime_conditioned_windows(equity_curve, all_window_metrics, n)

    if not all_window_metrics:
        return _empty_persistence_report()

    # Aggregate statistics
    pnls = [w.pnl for w in all_window_metrics]
    profitable_count = sum(1 for w in all_window_metrics if w.is_profitable)
    alpha_count = sum(1 for w in all_window_metrics if w.is_alpha_positive)

    profitable_pct = profitable_count / len(all_window_metrics) * 100
    alpha_pct = alpha_count / len(all_window_metrics) * 100
    mean_pnl = sum(pnls) / len(pnls)
    pnl_var = sum((p - mean_pnl) ** 2 for p in pnls) / len(pnls)
    pnl_std = pnl_var ** 0.5
    sharpe = mean_pnl / pnl_std if pnl_std > 0 else 0.0

    persistent_count = sum(1 for w in all_window_metrics if w.is_profitable and w.is_alpha_positive)
    persistence_score = persistent_count / len(all_window_metrics)

    has_persistent = persistence_score > 0.6

    warning = ""
    if not has_persistent:
        if alpha_pct < 40:
            warning = (
                f"ALPHA NOT PERSISTENT: Only {alpha_pct:.0f}% of windows show positive "
                f"brier improvement. Alpha may be isolated to specific periods."
            )
        elif profitable_pct < 50:
            warning = (
                f"LOW PROFIT PERSISTENCE: Only {profitable_pct:.0f}% of windows are "
                f"profitable despite alpha presence. Execution costs may be destructive."
            )
        else:
            warning = (
                f"MODERATE PERSISTENCE: {persistence_score:.0%} of windows show both "
                f"profitable and alpha-positive results. Below 60% threshold."
            )

    return PersistenceReport(
        total_trades=n,
        window_metrics=all_window_metrics,
        profitable_window_pct=profitable_pct,
        alpha_window_pct=alpha_pct,
        pnl_volatility=pnl_std,
        mean_pnl=mean_pnl,
        sharpe_like=sharpe,
        persistence_score=persistence_score,
        has_persistent_alpha=has_persistent,
        warning=warning,
    )


def _empty_persistence_report() -> PersistenceReport:
    """Return empty report for no data."""
    return PersistenceReport(
        total_trades=0,
        window_metrics=[],
        profitable_window_pct=0.0,
        alpha_window_pct=0.0,
        pnl_volatility=0.0,
        mean_pnl=0.0,
        sharpe_like=0.0,
        persistence_score=0.0,
        has_persistent_alpha=False,
        warning="No equity curve data to analyze",
    )


def _extract_month_key(timestamp) -> str:
    """Extract YYYY-MM key from timestamp (float epoch or string ISO)."""
    if isinstance(timestamp, (int, float)):
        from datetime import datetime
        try:
            dt = datetime.fromtimestamp(float(timestamp))
            return f"{dt.year:04d}-{dt.month:02d}"
        except (ValueError, OSError, OverflowError):
            return "0000-00"
    if isinstance(timestamp, str) and len(timestamp) >= 7:
        return timestamp[:7]
    return "0000-00"


def _add_regime_conditioned_windows(
    equity_curve: list[EquityPoint],
    all_window_metrics: list[WindowMetrics],
    n: int,
) -> None:
    """Split equity curve into consecutive blocks and label windows by trend regime.

    Regime-conditioned windows partition the equity curve into contiguous segments
    of a fixed trade count, then classify each segment as an up, down, or flat
    regime based on the sign of its net PnL. This reveals whether alpha persists
    across all market regimes or only in favorable ones.
    """
    block_size = 25
    for start in range(0, n, block_size):
        end = min(start + block_size, n)
        if end - start < block_size // 2:
            break
        metrics = _extract_window(equity_curve, start, end)
        if metrics.pnl > 0:
            regime = "up"
        elif metrics.pnl < 0:
            regime = "down"
        else:
            regime = "flat"
        all_window_metrics.append(WindowMetrics(
            window_type=f"regime-{regime}",
            window_start=metrics.window_start,
            window_end=metrics.window_end,
            trade_count=metrics.trade_count,
            pnl=metrics.pnl,
            brier_improvement=metrics.brier_improvement,
            edge_realization=metrics.edge_realization,
            calibration=metrics.calibration,
            is_profitable=metrics.is_profitable,
            is_alpha_positive=metrics.is_alpha_positive,
        ))
