"""Ensemble consensus analysis — Part 15 spec.

Compute ensemble statistics from multiple model component probabilities,
falling back to spread-based disagreement for single predictions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional, Tuple

from .research_dataset import MarketSnapshot, ResearchDataset

D = Decimal
_EPSILON = 1e-9


@dataclass(frozen=True)
class EnsembleObservation:
    market_id: str
    observation_timestamp: object
    model_probability: Optional[float]
    component_count: int
    mean_prob: Optional[float]
    median_prob: Optional[float]
    std_dev: Optional[float]
    mad: Optional[float]
    min_prob: Optional[float]
    max_prob: Optional[float]
    ensemble_disagreement: float
    net_edge: Optional[float]
    confidence_score: Optional[float]


@dataclass(frozen=True)
class ConsensusAnalysis:
    observations: List[EnsembleObservation] = field(default_factory=list)

    def summary(self) -> dict:
        confs = [o.confidence_score for o in self.observations if o.confidence_score is not None]
        disgs = [o.ensemble_disagreement for o in self.observations]
        return {
            "observation_count": len(self.observations),
            "mean_confidence": round(sum(confs) / len(confs), 6) if confs else 0.0,
            "median_confidence": round(sorted(confs)[len(confs) // 2], 6) if confs else 0.0,
            "mean_disagreement": round(sum(disgs) / len(disgs), 6) if disgs else 0.0,
            "high_confidence_frac": round(sum(1 for c in confs if c > 0.05) / len(confs), 4)
            if confs
            else 0.0,
        }


def _stats(values: List[float]) -> Tuple[float, float, float, float, float, float]:
    n = len(values)
    mean = sum(values) / n
    srt = sorted(values)
    median = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2
    var = sum((v - mean) ** 2 for v in values) / max(n - 1, 1)
    std = math.sqrt(var)
    abs_devs = sorted(abs(v - median) for v in values)
    mad = abs_devs[len(abs_devs) // 2] if abs_devs else 0.0
    return mean, median, std, mad, srt[0], srt[-1]


def analyze_consensus(dataset: ResearchDataset) -> ConsensusAnalysis:
    """Analyze ensemble agreement and compute per-observation confidence.

    Groups snapshots by market_id. If multiple snapshots have valid
    model_probability, computes ensemble stats and uses max-min range
    as disagreement. Otherwise falls back to spread (yes_ask - yes_bid).
    confidence_score = net_edge / (ensemble_disagreement + epsilon).
    """
    market_groups: dict = {}
    for snap in dataset.snapshots:
        market_groups.setdefault(snap.market_id, []).append(snap)

    results: List[EnsembleObservation] = []
    for market_id, snaps in market_groups.items():
        multi = [s for s in snaps if s.model_probability is not None]
        if len(multi) > 1:
            probs = [float(s.model_probability) for s in multi]
            mean, median, std, mad, lo, hi = _stats(probs)
            disagreement = hi - lo
            for s in snaps:
                results.append(_obs(s, len(multi), mean, median, std, mad, lo, hi, disagreement))
        else:
            for s in snaps:
                spread = (
                    float(s.yes_best_ask - s.yes_best_bid)
                    if (s.yes_best_ask is not None and s.yes_best_bid is not None)
                    else 0.0
                )
                results.append(_obs(s, len(multi), None, None, None, None, None, None, spread))
    return ConsensusAnalysis(observations=results)


def _obs(
    snap: MarketSnapshot,
    n: int,
    mean: Optional[float],
    median: Optional[float],
    std: Optional[float],
    mad: Optional[float],
    lo: Optional[float],
    hi: Optional[float],
    disagreement: float,
) -> EnsembleObservation:
    ne = float(snap.net_edge) if snap.net_edge is not None else None
    mp = float(snap.model_probability) if snap.model_probability is not None else None
    conf = ne / (disagreement + _EPSILON) if ne is not None else None
    return EnsembleObservation(
        market_id=snap.market_id,
        observation_timestamp=snap.observation_timestamp,
        model_probability=mp,
        component_count=n,
        mean_prob=mean,
        median_prob=median,
        std_dev=std,
        mad=mad,
        min_prob=lo,
        max_prob=hi,
        ensemble_disagreement=disagreement,
        net_edge=ne,
        confidence_score=conf,
    )
