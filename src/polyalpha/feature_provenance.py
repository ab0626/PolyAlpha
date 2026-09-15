"""Feature provenance tracking — records which features drive each decision.

Section 16 of the v0.3 spec: every trade decision must record which
features contributed and their importance.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class FeatureContribution:
    """A single feature's contribution to a decision."""

    feature_name: str
    value: float
    importance: float  # permutation importance or similar
    direction: str  # "positive" or "negative" (toward/away from signal)
    source_module: str  # which module computed this feature


@dataclass
class DecisionProvenance:
    """Full provenance for a single trading decision."""

    market_id: str
    token_id: str
    decision_time: datetime
    side: str  # BUY/SELL
    fair_probability: Decimal
    execution_price: Decimal
    net_edge: Decimal
    features: list[FeatureContribution] = field(default_factory=list)
    model_version: str = ""
    cluster: str = ""
    category: str = ""

    @property
    def top_features(self) -> list[FeatureContribution]:
        """Top 5 features by absolute importance."""
        return sorted(self.features, key=lambda f: abs(f.importance), reverse=True)[:5]

    @property
    def total_importance(self) -> float:
        return sum(abs(f.importance) for f in self.features)

    def summary(self) -> dict:
        return {
            "market_id": self.market_id,
            "side": self.side,
            "fair_probability": str(self.fair_probability),
            "execution_price": str(self.execution_price),
            "net_edge": str(self.net_edge),
            "top_features": [
                {"name": f.feature_name, "importance": f.importance, "direction": f.direction}
                for f in self.top_features
            ],
            "model_version": self.model_version,
        }


def track_decision_provenance(
    market_id: str,
    token_id: str,
    decision_time: datetime,
    side: str,
    fair_probability: Decimal,
    execution_price: Decimal,
    net_edge: Decimal,
    feature_values: dict[str, float] | None = None,
    feature_importances: dict[str, float] | None = None,
    model_version: str = "",
    cluster: str = "",
    category: str = "",
) -> DecisionProvenance:
    """Build a DecisionProvenance with feature contributions."""
    features = []
    if feature_values and feature_importances:
        for name, value in feature_values.items():
            imp = feature_importances.get(name, 0.0)
            direction = "positive" if imp > 0 else "negative" if imp < 0 else "neutral"
            source = _infer_source(name)
            features.append(
                FeatureContribution(
                    feature_name=name,
                    value=value,
                    importance=imp,
                    direction=direction,
                    source_module=source,
                )
            )

    return DecisionProvenance(
        market_id=market_id,
        token_id=token_id,
        decision_time=decision_time,
        side=side,
        fair_probability=fair_probability,
        execution_price=execution_price,
        net_edge=net_edge,
        features=features,
        model_version=model_version,
        cluster=cluster,
        category=category,
    )


def _infer_source(feature_name: str) -> str:
    """Infer the source module from feature name prefix."""
    prefix_map = {
        "ob_": "features.orderbook",
        "spread_": "features.orderbook",
        "depth_": "features.orderbook",
        "imbalance": "features.orderbook",
        "vwap": "features.orderbook",
        "mkt_": "features.market",
        "volume_": "features.market",
        "liq_": "features.market",
        "temp_": "features.temporal",
        "time_": "features.temporal",
        "deadline_": "features.temporal",
        "cross_": "features.cross_market",
        "relative_": "features.cross_market",
        "ext_": "features.external",
        "news_": "features.external",
    }
    for prefix, source in prefix_map.items():
        if feature_name.startswith(prefix):
            return source
    return "unknown"
