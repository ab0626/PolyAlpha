"""Model ablation framework — Part 8: systematic component contribution testing.

Tests 14 predefined combinations of 6 model components to isolate which
subsystems contribute genuine predictive signal vs noise.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .research_dataset import ResearchDataset

COMPONENTS = [
    "market_prior",
    "microstructure",
    "fundamental",
    "logistic",
    "gradient_boosting",
    "relative_value",
]

COMBINATIONS: list[tuple[str, ...]] = [
    ("market_prior",),
    ("microstructure",),
    ("fundamental",),
    ("logistic",),
    ("gradient_boosting",),
    ("relative_value",),
    ("market_prior", "fundamental"),
    ("market_prior", "microstructure"),
    ("fundamental", "relative_value"),
    tuple(COMPONENTS),
    tuple(c for c in COMPONENTS if c != "microstructure"),
    tuple(c for c in COMPONENTS if c != "fundamental"),
    tuple(c for c in COMPONENTS if c != "relative_value"),
    tuple(c for c in COMPONENTS if c != "market_prior"),
]

EPS = 1e-15


@dataclass(frozen=True)
class CombinationResult:
    components: tuple[str, ...]
    brier_score: float
    log_loss: float
    calibration_error: float
    net_pnl: float
    max_drawdown: float
    profit_factor: float
    turnover: float
    average_edge: float
    realized_edge: float
    edge_realization_ratio: float
    sample_count: int
    brier_improvement: float = 0.0


@dataclass
class AblationReport:
    results: list[CombinationResult]
    brier_ranking: list[str] = field(default_factory=list)
    pnl_ranking: list[str] = field(default_factory=list)
    market_prior_brier: float = 0.0

    def summary(self) -> dict:
        return {
            "total_combinations": len(self.results),
            "market_prior_brier": self.market_prior_brier,
            "brier_ranking": self.brier_ranking,
            "pnl_ranking": self.pnl_ranking,
        }


def _brier(probs: list[float], outcomes: list[int]) -> float:
    if not probs:
        return 1.0
    return sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / len(probs)


def _log_loss_val(probs: list[float], outcomes: list[int]) -> float:
    if not probs:
        return 10.0
    return -sum(
        math.log(max(p, EPS) if o == 1 else max(1.0 - p, EPS)) for p, o in zip(probs, outcomes)
    ) / len(probs)


def _ece(probs: list[float], outcomes: list[int], n_buckets: int = 10) -> float:
    if not probs:
        return 1.0
    buckets: dict[int, list[tuple[float, int]]] = {}
    for p, o in zip(probs, outcomes):
        buckets.setdefault(min(int(p * n_buckets), n_buckets - 1), []).append((p, o))
    err = n = 0
    for items in buckets.values():
        if items:
            err += abs(
                sum(p for p, _ in items) / len(items) - sum(o for _, o in items) / len(items)
            ) * len(items)
            n += len(items)
    return err / n if n else 1.0


def _pnl_metrics(probs: list[float], outcomes: list[int], edge_threshold: float = 0.02) -> dict:
    pnl = peak = max_dd = wins = losses = n_bets = edge_sum = realized_sum = 0.0
    fee_rate = 0.02
    for p, o in zip(probs, outcomes):
        edge = p - 0.5
        if abs(edge) < edge_threshold:
            continue
        n_bets += 1
        edge_sum += abs(edge)
        side = "YES" if edge > 0 else "NO"
        bet_p = p if side == "YES" else 1.0 - p
        payout = (1.0 - bet_p) * (1.0 - fee_rate)
        won = (o == 1 and side == "YES") or (o == 0 and side == "NO")
        ret = payout - 1.0 if won else -1.0
        pnl += ret
        realized_sum += abs(edge) if won else 0.0
        if ret > 0:
            wins += ret
        else:
            losses += abs(ret)
        peak = max(peak, pnl)
        max_dd = max(max_dd, peak - pnl)
    return {
        "net_pnl": round(pnl, 6),
        "max_drawdown": round(max_dd, 6),
        "profit_factor": round(wins / losses, 4) if losses else (float("inf") if wins else 0.0),
        "turnover": round(n_bets / len(probs), 4) if probs else 0.0,
        "average_edge": round(edge_sum / n_bets, 6) if n_bets else 0.0,
        "realized_edge": round(realized_sum / n_bets, 6) if n_bets else 0.0,
        "edge_realization_ratio": round(realized_sum / edge_sum, 4) if edge_sum else 0.0,
    }


def run_ablation(
    dataset: ResearchDataset,
    component_forecasts: dict[str, dict[str, float]] | None = None,
) -> AblationReport:
    """Run ablation across 14 predefined combinations.

    Args:
        dataset: Research dataset with resolved records.
        component_forecasts: component_name -> {market_id: probability}.
            If None, uses each snapshot's model_probability for all components.
    """
    resolved = [s for s in dataset.snapshots if s.final_resolution is not None]
    if not resolved:
        return AblationReport(results=[], market_prior_brier=1.0)

    outcomes = [s.final_resolution for s in resolved]
    market_ids = [s.market_id for s in resolved]

    if component_forecasts is None:
        base = {
            s.market_id: float(s.model_probability) if s.model_probability is not None else 0.5
            for s in resolved
        }
        component_forecasts = {c: dict(base) for c in COMPONENTS}

    market_probs = [float(s.yes_mid) if s.yes_mid is not None else 0.5 for s in resolved]
    market_prior_brier = _brier(market_probs, outcomes)

    results: list[CombinationResult] = []
    for combo in COMBINATIONS:
        combo_probs = []
        for mid in market_ids:
            avail = [
                component_forecasts[c].get(mid, 0.5) for c in combo if c in component_forecasts
            ]
            combo_probs.append(sum(avail) / len(avail) if avail else 0.5)
        brier = _brier(combo_probs, outcomes)
        metrics = _pnl_metrics(combo_probs, outcomes)
        results.append(
            CombinationResult(
                components=combo,
                brier_score=round(brier, 6),
                log_loss=round(_log_loss_val(combo_probs, outcomes), 6),
                calibration_error=round(_ece(combo_probs, outcomes), 6),
                sample_count=len(resolved),
                brier_improvement=round(market_prior_brier - brier, 6),
                **metrics,
            )
        )

    brier_rank = sorted(results, key=lambda r: r.brier_score)
    pnl_rank = sorted(results, key=lambda r: r.net_pnl, reverse=True)
    return AblationReport(
        results=results,
        brier_ranking=["+".join(r.components) for r in brier_rank],
        pnl_ranking=["+".join(r.components) for r in pnl_rank],
        market_prior_brier=market_prior_brier,
    )
