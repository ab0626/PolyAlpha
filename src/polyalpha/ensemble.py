"""Model ensemble implementing the ProbabilityModel protocol.

Combines multiple component models with configurable weights.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .forecasting import Forecast, ProbabilityModel

D = Decimal


@dataclass
class EnsembleModel:
    """Ensemble of probability models with configurable weights.

    Combines component model predictions using weighted averaging.
    Weights must sum to 1.0 and be non-negative.

    Supports both static weights and dynamic weight updates.
    """

    components: list[tuple[str, ProbabilityModel, Decimal]]
    name: str = "ensemble"

    def __post_init__(self):
        if not self.components:
            raise ValueError("at least one component required")
        total = sum(w for _, _, w in self.components)
        if abs(total - D(1)) > D("0.001"):
            raise ValueError(f"weights must sum to 1.0, got {total}")
        for _, _, w in self.components:
            if w < 0:
                raise ValueError("weights must be non-negative")

    def predict(self, market_id: str, book, at: datetime) -> Forecast:
        """Generate ensemble forecast from all component models."""
        forecasts = []
        for name, model, weight in self.components:
            try:
                forecast = model.predict(market_id, book, at)
                forecasts.append((name, forecast, weight))
            except ValueError:
                continue  # skip failing components

        if not forecasts:
            raise ValueError("all component models failed")

        # Weighted average probability
        total_weight = sum(w for _, _, w in forecasts)
        weighted_p = sum(f.probability * w for _, f, w in forecasts) / total_weight

        # Conservative: widen interval using max uncertainty from any component
        lowers = [f.lower for _, f, _ in forecasts]
        uppers = [f.upper for _, f, _ in forecasts]
        ensemble_lower = min(lowers) if lowers else weighted_p
        ensemble_upper = max(uppers) if uppers else weighted_p

        return Forecast(
            market_id=market_id,
            timestamp=at,
            probability=weighted_p,
            lower=ensemble_lower,
            upper=ensemble_upper,
            version=f"{self.name}({'+'.join(n for n, _, _ in forecasts)})",
        )

    def update_weights(self, new_weights: dict[str, Decimal]):
        """Update component weights by name."""
        weight_map = {name: w for name, _, w in self.components}
        for name, w in new_weights.items():
            if name not in weight_map:
                raise ValueError(f"unknown component: {name}")
            if w < 0:
                raise ValueError("weights must be non-negative")

        total = sum(new_weights.get(name, w) for name, _, w in self.components)
        if abs(total - D(1)) > D("0.001"):
            raise ValueError(f"new weights must sum to 1.0, got {total}")

        self.components = [
            (name, model, new_weights.get(name, w)) for name, model, w in self.components
        ]

    @property
    def component_names(self) -> list[str]:
        return [name for name, _, _ in self.components]

    @property
    def weight_dict(self) -> dict[str, Decimal]:
        return {name: w for name, _, w in self.components}
