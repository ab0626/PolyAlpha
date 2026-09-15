"""Null/sanity strategies — baseline strategies for verifying model value.

Section 6 of the v0.3 spec: if the model cannot beat these trivial
baselines, it has no edge.

Strategies:
- 0A: Uniform(0.5) — always predict 50%
- 0B: Market midpoint — use market price as forecast
- 0C: Constant class prior — use overall outcome frequency
- 0D: Random forecast — uniform random in (0,1)
- 0E: Inverse — flip the market midpoint (1 - mid)
"""

import math
import random
import warnings
from dataclasses import dataclass

from .research_dataset import ResearchDataset


@dataclass(frozen=True)
class NullStrategyResult:
    """Result of a null strategy evaluation."""

    name: str
    brier_score: float
    log_loss: float
    ece: float
    resolved_count: int


def _brier(probs: list[float], outcomes: list[int]) -> float:
    if not probs:
        return 0.0
    return sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / len(probs)


def _log_loss_val(probs: list[float], outcomes: list[int]) -> float:
    if not probs:
        return 0.0
    eps = 1e-15
    total = 0.0
    for p, o in zip(probs, outcomes):
        p = max(eps, min(1.0 - eps, p))
        total -= math.log(p if o == 1 else 1.0 - p)
    return total / len(probs)


def _ece(probs: list[float], outcomes: list[int], n_buckets: int = 10) -> float:
    if not probs:
        return 0.0
    buckets: dict[int, list[tuple[float, int]]] = {}
    for p, o in zip(probs, outcomes):
        b = min(int(p * n_buckets), n_buckets - 1)
        buckets.setdefault(b, []).append((p, o))
    total_error = 0.0
    total_count = 0
    for b in range(n_buckets):
        items = buckets.get(b, [])
        if not items:
            continue
        mean_pred = sum(p for p, _ in items) / len(items)
        mean_actual = sum(o for _, o in items) / len(items)
        total_error += abs(mean_pred - mean_actual) * len(items)
        total_count += len(items)
    return total_error / total_count if total_count else 0.0


def evaluate_null_strategies(
    dataset: ResearchDataset,
    seed: int = 42,
) -> list[NullStrategyResult]:
    """Evaluate all null strategies against the dataset."""
    resolved = [
        (s.model_probability, s.final_resolution)
        for s in dataset.snapshots
        if s.final_resolution is not None
    ]
    if not resolved:
        return []

    outcomes = [o for _, o in resolved]
    n = len(outcomes)
    class_prior = sum(outcomes) / n if n else 0.5

    rng = random.Random(seed)

    results = []

    # 0A: Uniform(0.5)
    uniform_probs = [0.5] * n
    results.append(
        NullStrategyResult(
            name="0A_uniform",
            brier_score=_brier(uniform_probs, outcomes),
            log_loss=_log_loss_val(uniform_probs, outcomes),
            ece=_ece(uniform_probs, outcomes),
            resolved_count=n,
        )
    )

    # 0B: Market midpoint (use yes_mid when available, fall back to model probability)
    market_probs = [
        float(s.yes_mid) if s.yes_mid is not None else float(s.model_probability)
        for s in dataset.snapshots
        if s.final_resolution is not None
    ]
    results.append(
        NullStrategyResult(
            name="0B_market_midpoint",
            brier_score=_brier(market_probs, outcomes),
            log_loss=_log_loss_val(market_probs, outcomes),
            ece=_ece(market_probs, outcomes),
            resolved_count=n,
        )
    )

    # 0C: Constant class prior
    prior_probs = [class_prior] * n
    results.append(
        NullStrategyResult(
            name="0C_class_prior",
            brier_score=_brier(prior_probs, outcomes),
            log_loss=_log_loss_val(prior_probs, outcomes),
            ece=_ece(prior_probs, outcomes),
            resolved_count=n,
        )
    )

    # 0D: Random forecast (reproducible)
    random_probs = [rng.random() for _ in range(n)]
    results.append(
        NullStrategyResult(
            name="0D_random",
            brier_score=_brier(random_probs, outcomes),
            log_loss=_log_loss_val(random_probs, outcomes),
            ece=_ece(random_probs, outcomes),
            resolved_count=n,
        )
    )

    # 0E: Inverse market midpoint
    inverse_probs = [1.0 - p for p in market_probs]
    results.append(
        NullStrategyResult(
            name="0E_inverse_midpoint",
            brier_score=_brier(inverse_probs, outcomes),
            log_loss=_log_loss_val(inverse_probs, outcomes),
            ece=_ece(inverse_probs, outcomes),
            resolved_count=n,
        )
    )

    # Check if any null strategy beats the model — possible backtest artifact
    model_probs = [float(s.model_probability) for s in dataset.snapshots if s.final_resolution is not None]
    if model_probs:
        model_brier = _brier(model_probs, outcomes)
        for r in results:
            if r.brier_score < model_brier:
                warnings.warn(
                    f"POSSIBLE_BACKTEST_ARTIFACT: null strategy '{r.name}' "
                    f"(Brier={r.brier_score:.6f}) beats model (Brier={model_brier:.6f}). "
                    f"The model may not have real edge.",
                    stacklevel=2,
                )

    return results
