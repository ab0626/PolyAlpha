"""Model degradation — deliberate perturbation tests.

Section 24 of the v0.3 spec: five degradation tests that verify model
sensitivity. Each test deliberately degrades predictions and checks that
Brier score worsens. If degradation *improves* performance, it flags
overfitting, leakage, inverse signal, or an implementation bug.
"""

import random
from dataclasses import dataclass

from .research_dataset import ResearchDataset


@dataclass(frozen=True)
class DegradationTest:
    name: str
    description: str
    baseline_brier: float
    degraded_brier: float
    delta_brier: float  # degraded - baseline (positive = worse)
    deteriorated: bool
    investigation_flag: str | None


@dataclass(frozen=True)
class DegradationResult:
    tests: list[DegradationTest]
    all_deteriorated: bool
    flagged_count: int

    def summary(self) -> dict:
        return {
            "test_count": len(self.tests),
            "all_deteriorated": self.all_deteriorated,
            "flagged_count": self.flagged_count,
            "tests": {
                t.name: {"delta": t.delta_brier, "flagged": t.investigation_flag is not None}
                for t in self.tests
            },
        }


def _brier(probs: list[float], outcomes: list[int]) -> float:
    if not probs:
        return 1.0
    return sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / len(probs)


_FLAG_REASONS = {
    "gaussian_noise": "overfitting or label leakage",
    "delay_predictions": "inverse temporal signal or data leakage",
    "remove_external_features": "external features are noise, not signal",
    "scramble_categories": "category labels are adversarial or misaligned",
    "stale_snapshots": "model overfits to recency, stale data leaks",
}


def _flag(name: str, baseline: float, degraded: float) -> str | None:
    return _FLAG_REASONS.get(name) if degraded < baseline - 0.005 else None


def run_degradation_tests(
    dataset: ResearchDataset,
    n_simulations: int = 50,
    delay_steps: int = 5,
    noise_std: float = 0.1,
    seed: int = 42,
) -> DegradationResult:
    records = sorted(
        (s for s in dataset.snapshots if s.final_resolution is not None),
        key=lambda s: s.observation_timestamp,
    )
    if len(records) < delay_steps + 2:
        return DegradationResult(tests=[], all_deteriorated=True, flagged_count=0)

    base_probs = [float(s.model_probability) for s in records]
    outcomes = [s.final_resolution for s in records]
    baseline = _brier(base_probs, outcomes)
    rng = random.Random(seed)
    tests: list[DegradationTest] = []

    # 1: Gaussian probability noise
    noise_briers = [
        _brier([max(0.01, min(0.99, p + rng.gauss(0, noise_std))) for p in base_probs], outcomes)
        for _ in range(n_simulations)
    ]
    avg_noise = sum(noise_briers) / len(noise_briers)
    tests.append(
        DegradationTest(
            "gaussian_noise",
            "Add Gaussian noise to probabilities",
            round(baseline, 6),
            round(avg_noise, 6),
            round(avg_noise - baseline, 6),
            avg_noise > baseline,
            _flag("gaussian_noise", baseline, avg_noise),
        )
    )

    # 2: Delay predictions by N steps
    delayed = [base_probs[max(0, i - delay_steps)] for i in range(len(base_probs))]
    del_brier = _brier(delayed, outcomes)
    tests.append(
        DegradationTest(
            "delay_predictions",
            f"Shift predictions back {delay_steps} steps",
            round(baseline, 6),
            round(del_brier, 6),
            round(del_brier - baseline, 6),
            del_brier > baseline,
            _flag("delay_predictions", baseline, del_brier),
        )
    )

    # 3: Remove external features (replace with market midpoint)
    mkt_probs = [float(s.yes_mid) if s.yes_mid else float(s.model_probability) for s in records]
    ext_brier = _brier(mkt_probs, outcomes)
    tests.append(
        DegradationTest(
            "remove_external_features",
            "Replace model probs with market midpoints",
            round(baseline, 6),
            round(ext_brier, 6),
            round(ext_brier - baseline, 6),
            ext_brier > baseline,
            _flag("remove_external_features", baseline, ext_brier),
        )
    )

    # 4: Scramble categories
    cats: dict[str, list[float]] = {}
    for s in records:
        cats.setdefault(s.category, []).append(float(s.model_probability))
    cat_avgs = {c: sum(v) / len(v) for c, v in cats.items()}
    cat_list = list(cat_avgs.keys())
    scramble_briers = []
    for _ in range(n_simulations):
        shuffled = list(cat_list)
        rng.shuffle(shuffled)
        mapping = dict(zip(cat_list, shuffled))
        scramble_briers.append(_brier([cat_avgs[mapping[s.category]] for s in records], outcomes))
    avg_scramble = sum(scramble_briers) / len(scramble_briers)
    tests.append(
        DegradationTest(
            "scramble_categories",
            "Replace probs with random-category averages",
            round(baseline, 6),
            round(avg_scramble, 6),
            round(avg_scramble - baseline, 6),
            avg_scramble > baseline,
            _flag("scramble_categories", baseline, avg_scramble),
        )
    )

    # 5: Stale snapshots (use earliest prediction for all)
    stale_brier = _brier([base_probs[0]] * len(base_probs), outcomes)
    tests.append(
        DegradationTest(
            "stale_snapshots",
            "Use the oldest prediction for every record",
            round(baseline, 6),
            round(stale_brier, 6),
            round(stale_brier - baseline, 6),
            stale_brier > baseline,
            _flag("stale_snapshots", baseline, stale_brier),
        )
    )

    flagged = sum(1 for t in tests if t.investigation_flag is not None)
    return DegradationResult(
        tests=tests, all_deteriorated=all(t.deteriorated for t in tests), flagged_count=flagged
    )
