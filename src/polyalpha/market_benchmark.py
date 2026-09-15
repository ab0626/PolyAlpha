"""Market benchmark — strict market baseline for Brier/log_loss/calibration.

delta_brier = brier_market - brier_model  (positive = model beats market)
delta_brier_microprice = microprice_brier - model_brier  (positive = model beats microprice)
"""

import math
from dataclasses import dataclass

from .research_dataset import ResearchDataset

_ZERO = dict(
    market_brier=0,
    model_brier=0,
    delta_brier=0,
    market_log_loss=0,
    model_log_loss=0,
    delta_log_loss=0,
    market_ece=0,
    model_ece=0,
    delta_ece=0,
    market_microprice=0,
    delta_brier_microprice=0,
    resolved_count=0,
    market_wins=0,
    model_wins=0,
    ties=0,
    market_predicted_mean=0,
    model_predicted_mean=0,
    outcome_mean=0,
)


@dataclass(frozen=True)
class BenchmarkResult:
    """Market benchmark comparison results."""

    market_brier: float
    model_brier: float
    delta_brier: float  # positive = model beats market
    market_log_loss: float
    model_log_loss: float
    delta_log_loss: float  # positive = model beats market
    market_ece: float
    model_ece: float
    delta_ece: float  # negative = model better calibrated
    market_microprice: float  # average volume-weighted mid across records
    delta_brier_microprice: float  # microprice_brier - model_brier
    resolved_count: int
    market_wins: int
    model_wins: int
    ties: int
    market_predicted_mean: float
    model_predicted_mean: float
    outcome_mean: float

    def summary(self) -> dict:
        return {
            "market_brier": self.market_brier,
            "model_brier": self.model_brier,
            "delta_brier": self.delta_brier,
            "model_beats_market": self.delta_brier > 0,
            "market_log_loss": self.market_log_loss,
            "model_log_loss": self.model_log_loss,
            "delta_log_loss": self.delta_log_loss,
            "market_ece": self.market_ece,
            "model_ece": self.model_ece,
            "market_microprice": self.market_microprice,
            "delta_brier_microprice": self.delta_brier_microprice,
            "model_beats_microprice": self.delta_brier_microprice > 0,
            "resolved_count": self.resolved_count,
            "market_wins": self.market_wins,
            "model_wins": self.model_wins,
            "ties": self.ties,
        }


def _brier_score(probabilities: list[float], outcomes: list[int]) -> float:
    if not probabilities:
        return 0.0
    return sum((p - o) ** 2 for p, o in zip(probabilities, outcomes)) / len(probabilities)


def _log_loss(probabilities: list[float], outcomes: list[int]) -> float:
    if not probabilities:
        return 0.0
    eps, total = 1e-15, 0.0
    for p, o in zip(probabilities, outcomes):
        p = max(eps, min(1.0 - eps, p))
        total -= math.log(p if o == 1 else 1.0 - p)
    return total / len(probabilities)


def _expected_calibration_error(
    probabilities: list[float], outcomes: list[int], n_buckets: int = 10
) -> float:
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


def _microprice(snap) -> float:
    """Compute microprice from snapshot order-book data."""
    bid = getattr(snap, "yes_best_bid", None)
    ask = getattr(snap, "yes_best_ask", None)
    bid_size = getattr(snap, "yes_bid_size", None)
    ask_size = getattr(snap, "yes_ask_size", None)
    if bid is not None and ask is not None and bid_size is not None and ask_size is not None:
        total = float(bid_size) + float(ask_size)
        if total > 0:
            return (float(ask) * float(bid_size) + float(bid) * float(ask_size)) / total
    mid = getattr(snap, "yes_mid", None)
    return float(mid) if mid is not None else 0.5


def evaluate_market_benchmark(dataset: ResearchDataset) -> BenchmarkResult:
    """Compare model against market midpoint and microprice baselines."""
    model_probs, market_probs, micro_probs, outcomes = [], [], [], []
    for snap in dataset.snapshots:
        if snap.final_resolution is None or snap.yes_mid is None or snap.model_probability is None:
            continue
        model_probs.append(float(snap.model_probability))
        market_probs.append(float(snap.yes_mid))
        micro_probs.append(_microprice(snap))
        outcomes.append(snap.final_resolution)
    if not model_probs:
        return BenchmarkResult(**_ZERO)
    mkt_brier = _brier_score(market_probs, outcomes)
    model_brier = _brier_score(model_probs, outcomes)
    mkt_micro_brier = _brier_score(micro_probs, outcomes)
    mkt_ll = _log_loss(market_probs, outcomes)
    model_ll = _log_loss(model_probs, outcomes)
    mkt_ece = _expected_calibration_error(market_probs, outcomes)
    model_ece = _expected_calibration_error(model_probs, outcomes)
    market_wins = model_wins = ties = 0
    for mp, mdp, o in zip(market_probs, model_probs, outcomes):
        mkt_err = abs(mp - o)
        mdl_err = abs(mdp - o)
        if mkt_err < mdl_err:
            market_wins += 1
        elif mdl_err < mkt_err:
            model_wins += 1
        else:
            ties += 1
    return BenchmarkResult(
        market_brier=mkt_brier,
        model_brier=model_brier,
        delta_brier=mkt_brier - model_brier,
        market_log_loss=mkt_ll,
        model_log_loss=model_ll,
        delta_log_loss=mkt_ll - model_ll,
        market_ece=mkt_ece,
        model_ece=model_ece,
        delta_ece=mkt_ece - model_ece,
        market_microprice=sum(micro_probs) / len(micro_probs),
        delta_brier_microprice=mkt_micro_brier - model_brier,
        resolved_count=len(model_probs),
        market_wins=market_wins,
        model_wins=model_wins,
        ties=ties,
        market_predicted_mean=sum(market_probs) / len(market_probs),
        model_predicted_mean=sum(model_probs) / len(model_probs),
        outcome_mean=sum(outcomes) / len(outcomes),
    )
