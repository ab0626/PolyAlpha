"""Fundamental probability model with Bayesian updating from external data.

Component C in the ensemble: uses external data (news, polls, fundamentals)
to estimate fair probability independently of market price.

Architecture:
1. Category-specific priors based on historical base rates
2. Bayesian updating from external fact features
3. Multi-source aggregation with source-quality weighting
4. Uncertainty shrinking with more independent sources

ExternalSource implementations provide category-specific features:
- Politics: polling_averages, fundamentals_score, economic_indicators
- Crypto: spot_price_ratio, derivatives_skew, on_chain_flow
- Sports: team_rating_diff, injury_impact, historical_matchup
- Macro: cpi_surprise, rate_expectation, yield_spread
"""

import math
from datetime import datetime
from decimal import Decimal

from ..domain import Book
from ..external import ExternalSource
from ..forecasting import Forecast

D = Decimal


# Category-specific base rates: historical YES rates for "will X happen" markets.
# These encode the prior probability that a random market in this category resolves YES.
CATEGORY_Priors = {
    "politics": D("0.45"),
    "crypto": D("0.50"),
    "sports": D("0.50"),
    "macro": D("0.40"),
    "entertainment": D("0.55"),
    "science": D("0.35"),
    "other": D("0.50"),
}

# Source reliability weights: higher = more trusted.
# A polling aggregator is more reliable than a single tweet.
SOURCE_WEIGHTS = {
    "polling_aggregator": D("1.5"),
    "economic_data": D("1.2"),
    "news_aggregator": D("1.0"),
    "social_signal": D("0.6"),
    "on_chain": D("1.1"),
    "default": D("1.0"),
}

# Feature keys mapped to probability directions (True = positive signal for YES).
FEATURE_DIRECTION = {
    "polling_average": True,  # higher polling average -> more likely YES
    "fundamentals_score": True,
    "economic_sentiment": True,
    "team_rating_diff": True,  # higher diff -> more likely YES for favored team
    "spot_price_ratio": True,  # > 1 means crypto price rising -> YES
    "yield_spread": True,
    "cpi_surprise": True,
}


def _bayesian_update(prior: D, likelihood_ratio: D) -> D:
    """Bayesian update: posterior = prior * lr / (prior * lr + (1 - prior)).

    likelihood_ratio = P(data | YES) / P(data | NO)
    lr > 1 -> evidence supports YES
    lr < 1 -> evidence supports NO
    lr = 1 -> no information
    """
    if not likelihood_ratio.is_finite() or likelihood_ratio <= 0:
        return prior
    prior = max(D("0.01"), min(D("0.99"), prior))
    lr = max(D("0.01"), min(D("100"), likelihood_ratio))
    posterior = (prior * lr) / (prior * lr + (1 - prior))
    return max(D("0.01"), min(D("0.99"), posterior))


def _feature_to_likelihood(feature_name: str, value: float) -> D:
    """Convert an external feature value to a likelihood ratio.

    Features are normalized to [-1, 1] range. Maps to a sigmoid-like
    likelihood ratio centered at 1.0.
    """
    direction = FEATURE_DIRECTION.get(feature_name)
    if direction is None:
        return D(1)  # unknown feature provides no information

    # Map value through sigmoid: v in [-1, 1] -> lr in [0.3, 3.3]
    val = max(-1.0, min(1.0, value))
    raw_lr = math.exp(2.0 * val)  # e^(-2) to e^(2) ~ 0.135 to 7.39
    lr = D(str(max(0.135, min(7.39, raw_lr))))
    if not direction:
        lr = D(1) / lr  # invert for negative-direction features
    return lr


def _source_weight(source_name: str) -> D:
    """Get reliability weight for a source."""
    for key, weight in SOURCE_WEIGHTS.items():
        if key in source_name.lower():
            return weight
    return SOURCE_WEIGHTS["default"]


class FundamentalModel:
    """Probability model based on external/fundamental data.

    Combines:
    1. Category-specific prior (historical base rate)
    2. Bayesian updating from each external fact's features
    3. Source-quality weighting
    4. Uncertainty shrinking with more independent sources

    Without real sources, returns the category prior with wide uncertainty.
    """

    def __init__(
        self,
        sources: list[ExternalSource] | None = None,
        category_priors: dict[str, Decimal] | None = None,
        min_uncertainty: Decimal = D("0.08"),
        max_uncertainty: Decimal = D("0.25"),
        version: str = "fundamental-v2",
    ):
        self.sources = list(sources or [])
        self.priors = dict(category_priors or CATEGORY_Priors)
        self.min_uncertainty = min_uncertainty
        self.max_uncertainty = max_uncertainty
        self.version = version

    def predict(
        self, market_id: str, book: Book, at: datetime, category: str = "other"
    ) -> Forecast:
        """Generate forecast from external data.

        Steps:
        1. Start with category prior
        2. For each available source, extract features and apply Bayesian updates
        3. Combine sources with quality weighting
        4. Compute uncertainty based on source count and agreement
        """
        prior = self.priors.get(category, D("0.50"))
        posterior = prior
        total_weight = D(0)
        source_predictions = []

        for source in self.sources:
            try:
                facts = source.available(market_id, at)
            except Exception:
                continue

            if not facts:
                continue

            # Aggregate features from this source
            source_lr = D(1)
            feature_count = 0
            for fact in facts:
                for fname, fval in fact.features.items():
                    if isinstance(fval, (int, float)) and math.isfinite(fval):
                        lr = _feature_to_likelihood(fname, fval)
                        source_lr = source_lr * lr
                        feature_count += 1

            if feature_count == 0:
                continue

            # Apply source quality weight
            weight = _source_weight(getattr(source, "name", "default"))
            # Clamp extreme likelihood ratios
            source_lr = max(D("0.1"), min(D("10"), source_lr))
            weighted_lr = source_lr**weight  # weight exponentiates the LR

            posterior = _bayesian_update(posterior, weighted_lr)
            total_weight += weight
            source_predictions.append(float(posterior))

        # Compute uncertainty
        if len(source_predictions) == 0:
            # No sources available: use category prior with wide uncertainty
            uncertainty = self.max_uncertainty
        elif len(source_predictions) == 1:
            # Single source: moderate uncertainty
            uncertainty = D("0.15")
        else:
            # Multiple sources: shrink uncertainty based on agreement
            mean_p = sum(source_predictions) / len(source_predictions)
            variance = sum((p - mean_p) ** 2 for p in source_predictions) / len(source_predictions)
            spread = math.sqrt(variance) if variance > 0 else 0
            # More agreement + more sources = less uncertainty
            agreement_factor = D(str(1.0 - min(1.0, spread * 3)))
            count_factor = min(D(1), D(str(len(source_predictions))) / D("5"))
            uncertainty = self.max_uncertainty - (self.max_uncertainty - self.min_uncertainty) * (
                D("0.6") * agreement_factor + D("0.4") * count_factor
            )
            uncertainty = max(self.min_uncertainty, min(self.max_uncertainty, uncertainty))

        probability = max(D("0.01"), min(D("0.99"), posterior))
        lower = max(D(0), probability - uncertainty)
        upper = min(D(1), probability + uncertainty)

        return Forecast(
            market_id=market_id,
            timestamp=at,
            probability=probability,
            lower=lower,
            upper=upper,
            version=self.version,
        )
