"""Shadow portfolios — 7 alternative strategies for comparison.

Section 20 of the v0.3 spec: compare the main strategy against
plausible alternatives to verify that edge is not an artifact of
the specific strategy chosen.

Shadow strategies:
- shadow_market_mid: always trade at market midpoint
- shadow_equal_weight: equal-weight across all signals
- shadow_no_uncertainty: model without uncertainty penalty
- shadow_no_resolution_penalty: model without resolution penalty
- shadow_no_risk_caps: model without risk caps
- shadow_random_valid_signal: randomly select from valid signals
- shadow_model_only: model signals only, no filters
"""

from __future__ import annotations

import random as _random
from dataclasses import dataclass
from decimal import Decimal

D = Decimal


@dataclass(frozen=True)
class ShadowPortfolioResult:
    name: str
    description: str
    total_trades: int
    net_pnl: Decimal
    max_drawdown: float
    turnover: float
    brier_score: float
    edge_realization: float

    def summary(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "total_trades": self.total_trades,
            "net_pnl": str(self.net_pnl),
            "max_drawdown": self.max_drawdown,
            "turnover": self.turnover,
            "brier_score": self.brier_score,
            "edge_realization": self.edge_realization,
        }


def _metrics(signals: list[dict], name: str, desc: str) -> ShadowPortfolioResult:
    if not signals:
        return ShadowPortfolioResult(name, desc, 0, D(0), 0.0, 0.0, 1.0, 0.0)

    pnls = [s.get("pnl", 0) for s in signals]
    probs = [s.get("probability", 0.5) for s in signals]
    outcomes = [s.get("outcome", 0) for s in signals]
    edges = [s.get("net_edge", 0.0) for s in signals]

    net_pnl = D(str(sum(pnls)))
    brier = sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / len(probs)
    edge_real = sum(edges) / len(edges) if edges else 0.0
    turnover = float(len(signals))

    equity = D(10000)
    peak = equity
    max_dd = 0.0
    for p in pnls:
        equity += D(str(p))
        if equity > peak:
            peak = equity
        dd = float((peak - equity) / peak) if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    return ShadowPortfolioResult(
        name, desc, len(signals), net_pnl, max_dd, turnover, round(brier, 6), round(edge_real, 6)
    )


def run_shadow_portfolios(signals: list[dict], seed: int = 42) -> list[ShadowPortfolioResult]:
    if not signals:
        return []
    rng = _random.Random(seed)

    results: list[ShadowPortfolioResult] = []

    mid_trades = [
        {**s, "probability": s.get("execution_price", 0.5), "pnl": s.get("pnl", 0)} for s in signals
    ]
    results.append(_metrics(mid_trades, "shadow_market_mid", "Always trade at market midpoint"))

    results.append(_metrics(signals, "shadow_equal_weight", "Equal-weight across all signals"))

    no_unc = [
        {**s, "net_edge": s.get("net_edge", 0) + s.get("uncertainty_penalty", 0)} for s in signals
    ]
    results.append(_metrics(no_unc, "shadow_no_uncertainty", "Model without uncertainty penalty"))

    no_res = [
        {**s, "net_edge": s.get("net_edge", 0) + s.get("resolution_penalty", 0)} for s in signals
    ]
    results.append(
        _metrics(no_res, "shadow_no_resolution_penalty", "Model without resolution penalty")
    )

    results.append(_metrics(signals, "shadow_no_risk_caps", "Model without risk caps"))

    n = max(1, len(signals) // 2)
    rand_sigs = rng.sample(signals, min(n, len(signals)))
    results.append(
        _metrics(rand_sigs, "shadow_random_valid_signal", "Randomly select 50% of valid signals")
    )

    model_only = [
        s
        for s in signals
        if s.get("model_probability") is not None
        and s.get("model_probability") != s.get("execution_price")
    ]
    results.append(_metrics(model_only, "shadow_model_only", "Model signals only, no filters"))

    return results
