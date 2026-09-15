"""Negative controls — Section 25 spec."""

from __future__ import annotations

import random
import warnings
from collections import defaultdict
from dataclasses import dataclass

from .research_dataset import MarketSnapshot, ResearchDataset


@dataclass(frozen=True)
class NegativeControlResult:
    control_type: str
    description: str
    brier_score: float
    real_brier: float
    delta_brier: float
    is_significant: bool
    warning: str | None = None

    def summary(self) -> dict:
        d = {
            "control_type": self.control_type,
            "brier_score": self.brier_score,
            "real_brier": self.real_brier,
            "delta_brier": self.delta_brier,
            "is_significant": self.is_significant,
        }
        if self.warning:
            d["warning"] = self.warning
        return d


def _resolved(ds: ResearchDataset) -> list[MarketSnapshot]:
    return [
        s
        for s in ds.snapshots
        if s.final_resolution is not None and s.model_probability is not None
    ]


def _brier(probs: list[float], outcomes: list[int]) -> float:
    return sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / len(probs)


def _check(real: float, ctrl: float, label: str) -> str | None:
    if ctrl <= real:
        return (
            f"INTEGRITY WARNING: {label} — model retains advantage"
            f" (control={ctrl:.6f} <= real={real:.6f})"
        )
    return None


def _shuffle_within(snaps: list[MarketSnapshot], rng: random.Random, key: str) -> list[int]:
    by_group: dict[str, list[int]] = defaultdict(list)
    for i, s in enumerate(snaps):
        by_group[getattr(s, key)].append(i)
    labels = [s.final_resolution for s in snaps]
    for indices in by_group.values():
        vals = [labels[i] for i in indices]
        rng.shuffle(vals)
        for i, v in zip(indices, vals):
            labels[i] = v
    return labels


def _shift_external(snaps: list[MarketSnapshot], rng: random.Random, shift: int = 7) -> list[float]:
    s_sorted = sorted(snaps, key=lambda s: s.observation_timestamp)
    n = len(s_sorted)
    return [
        float(s_sorted[(i + rng.randint(1, min(shift, n - 1))) % n].model_probability)
        for i in range(n)
    ]


def run_negative_controls(
    dataset: ResearchDataset,
    n_simulations: int = 100,
    seed: int = 42,
) -> list[NegativeControlResult]:
    snaps = _resolved(dataset)
    if len(snaps) < 5:
        return []
    real_probs = [float(s.model_probability) for s in snaps]
    real_outcomes = [s.final_resolution for s in snaps]
    real_brier = _brier(real_probs, real_outcomes)
    rng = random.Random(seed)
    results: list[NegativeControlResult] = []

    # C1: Future labels randomly permuted (breaks temporal signal)
    briers = []
    for _ in range(n_simulations):
        perm = list(real_outcomes)
        rng.shuffle(perm)
        briers.append(_brier(real_probs, perm))
    avg = sum(briers) / len(briers)
    w = _check(real_brier, avg, "permuted_labels")
    results.append(
        NegativeControlResult(
            "permuted_labels",
            "Future labels randomly permuted — breaks temporal signal",
            round(avg, 6),
            round(real_brier, 6),
            round(real_brier - avg, 6),
            real_brier < avg - 0.01,
            w,
        )
    )

    # C2: Outcomes permuted within category (tests category overfitting)
    briers = []
    for _ in range(n_simulations):
        briers.append(_brier(real_probs, _shuffle_within(snaps, rng, "category")))
    avg = sum(briers) / len(briers)
    w = _check(real_brier, avg, "category_permutation")
    results.append(
        NegativeControlResult(
            "category_permutation",
            "Outcomes permuted within category — tests category overfitting",
            round(avg, 6),
            round(real_brier, 6),
            round(real_brier - avg, 6),
            real_brier < avg - 0.01,
            w,
        )
    )

    # C3: Outcomes shuffled within event clusters (tests cluster structure)
    briers = []
    for _ in range(n_simulations):
        briers.append(_brier(real_probs, _shuffle_within(snaps, rng, "event_cluster")))
    avg = sum(briers) / len(briers)
    w = _check(real_brier, avg, "cluster_shuffle")
    results.append(
        NegativeControlResult(
            "cluster_shuffle",
            "Outcomes shuffled within event clusters — tests cluster structure",
            round(avg, 6),
            round(real_brier, 6),
            round(real_brier - avg, 6),
            real_brier < avg - 0.01,
            w,
        )
    )

    # C4: External features shifted by inappropriate time window
    briers = []
    for _ in range(n_simulations):
        briers.append(_brier(_shift_external(snaps, rng), real_outcomes))
    avg = sum(briers) / len(briers)
    w = _check(real_brier, avg, "temporal_shift")
    results.append(
        NegativeControlResult(
            "temporal_shift",
            "External features shifted by inappropriate time window",
            round(avg, 6),
            round(real_brier, 6),
            round(real_brier - avg, 6),
            real_brier < avg - 0.01,
            w,
        )
    )

    for r in results:
        if r.warning:
            warnings.warn(r.warning, stacklevel=2)
    return results
