"""Evaluation utilities; risk-adjusted ratios require an explicit regular sampling grid.

Comprehensive performance metrics for strategy evaluation including tail risk.
"""

import math
from statistics import mean, stdev


def performance(equities, times, periods_per_year=None):
    if len(equities) != len(times) or not equities:
        raise ValueError("aligned equity observations required")
    if any(not math.isfinite(x) or x <= 0 for x in equities):
        raise ValueError("positive finite equity required")
    if any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("timestamps must strictly increase")
    peak = equities[0]
    drawdowns = []
    for equity in equities:
        peak = max(peak, equity)
        drawdowns.append(1 - equity / peak)
    returns = [b / a - 1 for a, b in zip(equities, equities[1:])]
    sharpe = None
    if periods_per_year is not None:
        steps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
        if len(returns) < 2 or not steps or max(steps) != min(steps) or periods_per_year <= 0:
            raise ValueError(
                "annualization requires a regular explicit grid and sufficient observations"
            )
        volatility = stdev(returns)
        if volatility:
            sharpe = mean(returns) / volatility * math.sqrt(periods_per_year)
    return dict(
        return_on_capital=equities[-1] / equities[0] - 1,
        max_drawdown=max(drawdowns),
        sample_size=len(equities),
        sharpe_like=sharpe,
    )


def alpha_decay(signal_time, entry_price, observations, horizons):
    ordered = sorted(observations, key=lambda x: x[0])
    result = {}
    for seconds in horizons:
        if seconds <= 0:
            raise ValueError("positive horizon required")
        eligible = [
            (at, price) for at, price in ordered if (at - signal_time).total_seconds() >= seconds
        ]
        result[str(seconds)] = (
            None
            if not eligible
            else dict(
                observed_at=eligible[0][0].isoformat(),
                price_change=eligible[0][1] - entry_price,
            )
        )
    return result


def trade_metrics(fills: list[dict]) -> dict:
    """Compute trade-level performance metrics from fill journal.

    Args:
        fills: List of fill dictionaries with 'side', 'notional', 'fees', 'vwap', 'shares' keys.

    Returns:
        Dictionary with trade-level metrics.
    """
    if not fills:
        return {
            "total_trades": 0,
            "total_fees": 0,
            "total_notional": 0,
            "average_vwap": None,
        }

    buys = [f for f in fills if f.get("side") == "BUY"]
    sells = [f for f in fills if f.get("side") == "SELL"]

    total_fees = sum(float(f.get("fees", 0)) for f in fills)
    total_notional = sum(float(f.get("notional", 0)) for f in fills)

    buy_vwaps = [float(f["vwap"]) for f in buys if f.get("vwap") is not None]
    sell_vwaps = [float(f["vwap"]) for f in sells if f.get("vwap") is not None]

    return {
        "total_trades": len(fills),
        "total_buys": len(buys),
        "total_sells": len(sells),
        "total_fees": total_fees,
        "total_notional": total_notional,
        "average_buy_vwap": mean(buy_vwaps) if buy_vwaps else None,
        "average_sell_vwap": mean(sell_vwaps) if sell_vwaps else None,
    }


def edge_metrics(decisions: list[dict]) -> dict:
    """Compute edge-related metrics from decision log.

    Tracks predicted vs realized edge for filled trades.
    """
    filled = [d for d in decisions if d.get("reason") == "filled"]
    queued = [d for d in decisions if d.get("reason") == "queued"]
    rejected = [d for d in decisions if d.get("reason") == "rejected"]

    predicted_edges = []
    for d in filled:
        if "net_edge" in d:
            predicted_edges.append(float(d["net_edge"]))

    return {
        "total_signals": len(queued),
        "total_fills": len(filled),
        "total_rejections": len(rejected),
        "fill_rate": len(filled) / len(queued) if queued else 0,
        "average_predicted_edge": mean(predicted_edges) if predicted_edges else None,
    }


def brier_score(probabilities: list[float], outcomes: list[int]) -> float:
    """Brier score: mean((p - y)^2).

    Lower is better. Range [0, 1].
    """
    if len(probabilities) != len(outcomes) or not probabilities:
        raise ValueError("nonempty aligned observations required")
    return sum((p - y) ** 2 for p, y in zip(probabilities, outcomes)) / len(outcomes)


def log_loss(probabilities: list[float], outcomes: list[int]) -> float:
    """Log loss: -mean(y*log(p) + (1-y)*log(1-p)).

    Lower is better. Uses clipping to avoid log(0).
    """
    if len(probabilities) != len(outcomes) or not probabilities:
        raise ValueError("nonempty aligned observations required")
    clipped = [min(1 - 1e-12, max(1e-12, p)) for p in probabilities]
    return -sum(
        y * math.log(p) + (1 - y) * math.log1p(-p) for p, y in zip(clipped, outcomes)
    ) / len(outcomes)


def profit_factor(gross_wins: float, gross_losses: float) -> float | None:
    """Profit factor = gross_wins / abs(gross_losses).

    > 1.0 means strategy is profitable before costs.
    """
    if gross_losses == 0:
        return float("inf") if gross_wins > 0 else None
    return gross_wins / abs(gross_losses)


def sortino_ratio(returns: list[float], periods_per_year: float = 365) -> float | None:
    """Sortino ratio using downside deviation only.

    Requires regular sampling grid.
    """
    if len(returns) < 2:
        return None
    mean_r = mean(returns)
    downside = [min(0, r) for r in returns]
    downside_var = sum(d**2 for d in downside) / len(downside)
    downside_std = math.sqrt(downside_var)
    if downside_std == 0:
        return None
    return mean_r / downside_std * math.sqrt(periods_per_year)


def drawdown_series(equities: list[float]) -> list[float]:
    """Compute drawdown series from equity curve.

    Returns list of drawdowns (0 to 1) at each point.
    """
    if not equities:
        return []
    peak = equities[0]
    result = []
    for eq in equities:
        peak = max(peak, eq)
        dd = 1 - eq / peak if peak > 0 else 0
        result.append(dd)
    return result


def holding_period_stats(fills: list[dict]) -> dict:
    """Compute holding period statistics from fills.

    Requires fills with 'filled_at' timestamps and 'side' field.
    """
    from datetime import datetime

    buys = {}
    holding_times = []

    for f in fills:
        token = f.get("token_id", "")
        side = f.get("side", "")
        ts = f.get("filled_at", "")

        if not ts:
            continue

        try:
            if isinstance(ts, str):
                t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            else:
                t = ts
        except (ValueError, TypeError):
            continue

        if side == "BUY":
            buys[token] = t
        elif side == "SELL" and token in buys:
            delta = (t - buys[token]).total_seconds()
            holding_times.append(delta)
            del buys[token]

    if not holding_times:
        return {"average_holding_seconds": None, "max_holding_seconds": None, "sample_size": 0}

    return {
        "average_holding_seconds": mean(holding_times),
        "max_holding_seconds": max(holding_times),
        "min_holding_seconds": min(holding_times),
        "sample_size": len(holding_times),
    }


# --- Tail Risk Metrics ---


def value_at_risk(returns: list[float], confidence: float = 0.95) -> float | None:
    """Historical Value at Risk (VaR).

    Returns the loss threshold at the given confidence level.
    VaR = -percentile(returns, 1-confidence)
    """
    if len(returns) < 10:
        return None
    sorted_returns = sorted(returns)
    idx = int((1 - confidence) * len(sorted_returns))
    idx = max(0, min(idx, len(sorted_returns) - 1))
    return -sorted_returns[idx]


def conditional_value_at_risk(returns: list[float], confidence: float = 0.95) -> float | None:
    """Conditional VaR (Expected Shortfall).

    Average loss in the worst (1-confidence) fraction of scenarios.
    """
    if len(returns) < 10:
        return None
    sorted_returns = sorted(returns)
    cutoff = int((1 - confidence) * len(sorted_returns))
    cutoff = max(1, cutoff)
    tail = sorted_returns[:cutoff]
    return -mean(tail)


def tail_ratio(returns: list[float]) -> float | None:
    """Tail ratio = 95th percentile gain / 5th percentile loss.

    > 1.0 means upside tail is larger than downside tail.
    """
    if len(returns) < 20:
        return None
    sorted_returns = sorted(returns)
    p95 = sorted_returns[int(0.95 * len(sorted_returns))]
    p05 = sorted_returns[int(0.05 * len(sorted_returns))]
    if p05 >= 0:
        return None  # no losses
    return p95 / abs(p05)


def calmar_ratio(
    returns: list[float], max_drawdown: float, periods_per_year: float = 365
) -> float | None:
    """Calmar ratio = annualized return / max drawdown."""
    if not returns or max_drawdown <= 0:
        return None
    annual_return = mean(returns) * periods_per_year
    return annual_return / max_drawdown


def information_ratio(returns: list[float], benchmark_return: float = 0.0) -> float | None:
    """Information ratio = (mean return - benchmark) / tracking error."""
    if len(returns) < 2:
        return None
    excess = [r - benchmark_return for r in returns]
    tracking_error = stdev(excess)
    if tracking_error == 0:
        return None
    return mean(excess) / tracking_error
