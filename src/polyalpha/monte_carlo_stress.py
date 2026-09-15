"""Monte Carlo robustness — stress testing via PnL simulation.

Section 23 of the v0.3 spec: verify edge is robust to perturbations in
execution, pricing, and model accuracy.
"""

import random
from dataclasses import dataclass

from .research_dataset import MarketSnapshot, ResearchDataset


@dataclass(frozen=True)
class MonteCarloResult:
    n_simulations: int
    n_snapshots: int
    prob_positive_pnl: float
    prob_drawdown_gt_5pct: float
    prob_drawdown_gt_10pct: float
    pnl_5th: float
    pnl_50th: float
    pnl_95th: float
    mean_final_equity: float
    mean_win_rate: float

    def summary(self) -> dict[str, float]:
        return {
            "n_simulations": self.n_simulations,
            "n_snapshots": self.n_snapshots,
            "prob_positive_pnl": self.prob_positive_pnl,
            "prob_drawdown_gt_5pct": self.prob_drawdown_gt_5pct,
            "prob_drawdown_gt_10pct": self.prob_drawdown_gt_10pct,
            "pnl_5th": round(self.pnl_5th, 4),
            "pnl_50th": round(self.pnl_50th, 4),
            "pnl_95th": round(self.pnl_95th, 4),
            "mean_final_equity": round(self.mean_final_equity, 4),
            "mean_win_rate": round(self.mean_win_rate, 4),
        }


@dataclass(frozen=True)
class _Snapshot:
    model_probability: float
    execution_price: float
    final_resolution: int
    yes_best_bid: float
    yes_best_ask: float


def _record_to_snapshot(r: MarketSnapshot) -> _Snapshot | None:
    if r.final_resolution is None:
        return None
    mid = (float(r.yes_best_bid or 0) + float(r.yes_best_ask or 0)) / 2
    return _Snapshot(
        float(r.model_probability),
        mid if mid > 0 else float(r.model_probability),
        r.final_resolution,
        float(r.yes_best_bid or 0),
        float(r.yes_best_ask or 0),
    )


def _sim_trade(s: _Snapshot, rng: random.Random) -> tuple[float, bool]:
    slippage = rng.uniform(0.005, 0.03)
    fill_rate = rng.uniform(0.7, 1.0)
    prob_err = rng.gauss(0, 0.05)
    unc_mult = rng.uniform(0.8, 1.2)
    spread = s.yes_best_ask - s.yes_best_bid
    spread_cost = max(spread * rng.uniform(0.1, 0.5), 0) if spread > 0 else 0
    adj_p = max(0.01, min(0.99, s.model_probability + prob_err * unc_mult))
    bp = max(0.01, min(0.99, s.execution_price + slippage + spread_cost))
    pnl = (
        (s.final_resolution - bp) if adj_p > 0.5 else (1 - s.final_resolution - (1 - bp))
    ) * fill_rate
    return pnl - abs(pnl) * 0.01, pnl > 0


def run_monte_carlo_stress(
    dataset: ResearchDataset | None = None,
    *,
    snapshots: list[_Snapshot] | None = None,
    n_simulations: int = 1000,
    seed: int = 42,
) -> MonteCarloResult:
    if snapshots is None:
        if dataset is None:
            return MonteCarloResult(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 100.0, 0.0)
        snapshots = [s for r in dataset.snapshots if (s := _record_to_snapshot(r)) is not None]
    if not snapshots:
        return MonteCarloResult(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 100.0, 0.0)

    rng = random.Random(seed)
    equities, win_counts, pnls = [], [], []
    pos_pnl = dd5 = dd10 = 0

    for _ in range(n_simulations):
        eq, peak, dd, w = 100.0, 100.0, 0.0, 0
        for snap in snapshots:
            pnl, won = _sim_trade(snap, rng)
            eq += pnl
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak if peak > 0 else 0)
            if won:
                w += 1
        equities.append(eq)
        win_counts.append(w)
        pnls.append(eq - 100.0)
        if eq > 100:
            pos_pnl += 1
        if dd > 0.05:
            dd5 += 1
        if dd > 0.10:
            dd10 += 1

    n = n_simulations
    sp = sorted(pnls)

    def _ix(p: float) -> int:
        return min(int(p * n), n - 1)

    return MonteCarloResult(
        n_simulations=n,
        n_snapshots=len(snapshots),
        prob_positive_pnl=round(pos_pnl / n, 4),
        prob_drawdown_gt_5pct=round(dd5 / n, 4),
        prob_drawdown_gt_10pct=round(dd10 / n, 4),
        pnl_5th=round(sp[_ix(0.05)], 4),
        pnl_50th=round(sp[_ix(0.50)], 4),
        pnl_95th=round(sp[_ix(0.95)], 4),
        mean_final_equity=round(sum(equities) / n, 4),
        mean_win_rate=round(sum(win_counts) / (n * len(snapshots)), 4),
    )
