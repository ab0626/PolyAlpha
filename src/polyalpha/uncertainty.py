"""Explicit uncertainty estimation for probability forecasts.

Supports multiple methods:
- Bootstrap resampling for confidence intervals
- Model disagreement
- Historical error conditioning
- Spread-based market uncertainty
- Calibration-based uncertainty
"""

import math
import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

D = Decimal


@dataclass(frozen=True)
class UncertaintyEstimate:
    """Result of uncertainty estimation."""

    probability: Decimal
    lower_bound: Decimal
    upper_bound: Decimal
    uncertainty_score: Decimal
    method: str

    def __post_init__(self):
        if not (0 <= self.lower_bound <= self.probability <= self.upper_bound <= 1):
            raise ValueError("invalid uncertainty bounds")
        if not self.uncertainty_score.is_finite() or self.uncertainty_score < 0:
            raise ValueError("invalid uncertainty score")


class UncertaintyEstimator(Protocol):
    def estimate(
        self,
        probability: Decimal,
        features: dict,
        market_id: str,
    ) -> UncertaintyEstimate: ...


class HistoricalErrorUncertainty:
    """Uncertainty based on historical calibration error per category.

    Requires pre-computed calibration error statistics.
    """

    def __init__(self, default_buffer: Decimal = D("0.05"), category_errors: dict | None = None):
        self.default_buffer = default_buffer
        self.category_errors = category_errors or {}

    def estimate(self, probability, features, market_id="", category="unknown"):
        category_error = self.category_errors.get(category, self.default_buffer)
        # Scale uncertainty: higher near 0.5 (more uncertain), lower near extremes
        position_factor = 4 * probability * (1 - probability)  # peaks at 0.5
        scaled = category_error * (D("0.5") + D("0.5") * position_factor)
        lo = max(D(0), probability - scaled)
        hi = min(D(1), probability + scaled)
        return UncertaintyEstimate(
            probability=probability,
            lower_bound=lo,
            upper_bound=hi,
            uncertainty_score=scaled,
            method="historical-error",
        )


class ModelDisagreementUncertainty:
    """Uncertainty based on disagreement between multiple model components.

    If models disagree, uncertainty is high. If they agree, uncertainty is low.
    """

    def __init__(self, base_buffer: Decimal = D("0.03")):
        self.base_buffer = base_buffer

    def estimate(self, probability, features, model_probabilities=None):
        if not model_probabilities or len(model_probabilities) < 2:
            return UncertaintyEstimate(
                probability=probability,
                lower_bound=max(D(0), probability - self.base_buffer),
                upper_bound=min(D(1), probability + self.base_buffer),
                uncertainty_score=self.base_buffer,
                method="model-disagreement-fallback",
            )

        probs = [D(str(p)) for p in model_probabilities]
        mean_p = sum(probs) / len(probs)
        variance = sum((p - mean_p) ** 2 for p in probs) / len(probs)
        disagreement = D(str(math.sqrt(float(variance))))

        # Combine base buffer with disagreement
        total = self.base_buffer + disagreement
        lo = max(D(0), probability - total)
        hi = min(D(1), probability + total)

        return UncertaintyEstimate(
            probability=probability,
            lower_bound=lo,
            upper_bound=hi,
            uncertainty_score=total,
            method="model-disagreement",
        )


class SpreadBasedUncertainty:
    """Uncertainty derived from market spread as a proxy for consensus uncertainty.

    Wide spread suggests the market itself is uncertain about fair value.
    """

    def __init__(self, multiplier: Decimal = D("0.5")):
        self.multiplier = multiplier

    def estimate(self, probability, features, market_id=""):
        spread = features.get("spread")
        if spread is None or not spread.is_finite():
            buffer = D("0.05")
        else:
            buffer = spread * self.multiplier

        lo = max(D(0), probability - buffer)
        hi = min(D(1), probability + buffer)

        return UncertaintyEstimate(
            probability=probability,
            lower_bound=lo,
            upper_bound=hi,
            uncertainty_score=buffer,
            method="spread-based",
        )


class EnsembleUncertainty:
    """Combine multiple uncertainty estimators.

    Uses the maximum uncertainty across all estimators for conservatism.
    """

    def __init__(self, estimators: list[UncertaintyEstimator]):
        if not estimators:
            raise ValueError("at least one estimator required")
        self.estimators = estimators

    def estimate(self, probability, features, market_id="", **kwargs):
        estimates = [
            est.estimate(probability, features, market_id=market_id, **kwargs)
            for est in self.estimators
        ]
        # Take the most conservative (widest) interval
        max_lower = min(e.lower_bound for e in estimates)
        max_upper = max(e.upper_bound for e in estimates)
        max_uncertainty = max(e.uncertainty_score for e in estimates)

        return UncertaintyEstimate(
            probability=probability,
            lower_bound=max_lower,
            upper_bound=max_upper,
            uncertainty_score=max_uncertainty,
            method="ensemble-" + "+".join(e.method for e in estimates),
        )


class BootstrapUncertainty:
    """Bootstrap resampling to estimate prediction intervals.

    Given a set of historical predictions and outcomes, resamples to produce
    confidence intervals around the point estimate. This provides distribution-
    free uncertainty quantification that doesn't assume normality.
    """

    def __init__(
        self,
        n_bootstrap: int = 200,
        confidence_level: Decimal = D("0.90"),
        min_samples: int = 20,
        seed: int | None = None,
    ):
        if n_bootstrap < 10:
            raise ValueError("n_bootstrap must be >= 10")
        if not D("0.50") <= confidence_level <= D("0.99"):
            raise ValueError("confidence_level must be in [0.50, 0.99]")
        if min_samples < 5:
            raise ValueError("min_samples must be >= 5")
        self.n_bootstrap = n_bootstrap
        self.confidence_level = confidence_level
        self.min_samples = min_samples
        self._rng = random.Random(seed)
        self._history: list[tuple[float, float]] = []  # (prediction, outcome)

    def observe(self, prediction: float, outcome: float):
        """Record a prediction-outcome pair."""
        if not (0 <= prediction <= 1 and outcome in (0, 1)):
            raise ValueError("invalid prediction or outcome")
        self._history.append((prediction, outcome))

    def observe_batch(self, predictions: list[float], outcomes: list[float]):
        """Record a batch of prediction-outcome pairs."""
        if len(predictions) != len(outcomes):
            raise ValueError("mismatched lengths")
        for p, o in zip(predictions, outcomes):
            self.observe(p, o)

    @property
    def sample_count(self) -> int:
        return len(self._history)

    def estimate(
        self,
        probability: Decimal,
        features: dict,
        market_id: str = "",
    ) -> UncertaintyEstimate:
        """Estimate uncertainty via bootstrap resampling.

        Resamples the historical prediction-error distribution to produce
        a confidence interval around the current point estimate.
        """
        prob_f = float(probability)

        if len(self._history) < self.min_samples:
            # Not enough history: use a wide default
            buffer = D("0.10")
            return UncertaintyEstimate(
                probability=probability,
                lower_bound=max(D(0), probability - buffer),
                upper_bound=min(D(1), probability + buffer),
                uncertainty_score=buffer,
                method="bootstrap-insufficient-data",
            )

        # Bootstrap: resample prediction errors
        errors = [p - o for p, o in self._history]
        bootstrap_means = []
        for _ in range(self.n_bootstrap):
            sample = self._rng.choices(errors, k=len(errors))
            bootstrap_means.append(sum(sample) / len(sample))

        bootstrap_means.sort()
        # Confidence interval endpoints
        alpha = 1 - float(self.confidence_level)
        lo_idx = max(0, int(alpha / 2 * self.n_bootstrap))
        hi_idx = min(self.n_bootstrap - 1, int((1 - alpha / 2) * self.n_bootstrap))
        margin = D(str(max(abs(bootstrap_means[lo_idx]), abs(bootstrap_means[hi_idx]))))

        # Add model noise: uncertainty increases away from extremes
        position_factor = 4 * prob_f * (1 - prob_f)
        noise = D("0.02") * D(str(position_factor))
        total = margin + noise

        return UncertaintyEstimate(
            probability=probability,
            lower_bound=max(D(0), probability - total),
            upper_bound=min(D(1), probability + total),
            uncertainty_score=total,
            method="bootstrap",
        )


def conservative_probability(
    probability: Decimal,
    uncertainty: UncertaintyEstimate,
    confidence: Decimal = D("1.0"),
) -> Decimal:
    """Conservative probability for YES side.

    Uses the lower bound of the confidence interval.
    confidence=1.0 uses full uncertainty, 0.0 uses point estimate.
    """
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be in [0, 1]")
    buffer = uncertainty.uncertainty_score * confidence
    return max(D(0), probability - buffer)


def conservative_no_probability(
    probability: Decimal,
    uncertainty: UncertaintyEstimate,
    confidence: Decimal = D("1.0"),
) -> Decimal:
    """Conservative probability for NO side.

    Uses 1 - upper_bound of the confidence interval.
    """
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be in [0, 1]")
    buffer = uncertainty.uncertainty_score * confidence
    return max(D(0), D(1) - probability - buffer)
