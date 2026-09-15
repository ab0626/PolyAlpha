"""Trained probability models using scikit-learn.

Implements the ProbabilityModel protocol with actual ML models:
- LogisticRegressionModel: regularized logistic regression on book features
- GradientBoostedModel: gradient-boosted decision trees on book features
- TrainedEnsembleModel: learned-weight ensemble of all components

All models:
- Extract features from Book objects (no future data)
- Convert Decimal features to float for scikit-learn
- Return Forecast objects with uncertainty bounds
- Are serializable to/from JSON for persistence
"""

import math
from datetime import datetime
from decimal import Decimal

from ..domain import Book
from ..forecasting import Forecast

D = Decimal


def _book_features(book: Book) -> dict[str, float]:
    """Extract numeric features from a Book object for ML models.

    All features use only point-in-time data from the book snapshot.
    """
    if book.mid is None or book.spread is None or book.spread <= 0:
        raise ValueError("unusable book for feature extraction")

    mid = float(book.mid)
    spread = float(book.spread)
    bid_size = float(book.bids[0].size) if book.bids else 0.0
    ask_size = float(book.asks[0].size) if book.asks else 0.0
    bid_price = float(book.bids[0].price) if book.bids else 0.0
    ask_price = float(book.asks[0].price) if book.asks else 1.0

    total_bid = sum(float(lvl.size) for lvl in book.bids[:5])
    total_ask = sum(float(lvl.size) for lvl in book.asks[:5])
    total = total_bid + total_ask

    imbalance = (total_bid - total_ask) / total if total > 0 else 0.0
    microprice = (
        (ask_price * bid_size + bid_price * ask_size) / (bid_size + ask_size)
        if (bid_size + ask_size) > 0
        else mid
    )
    relative_spread = spread / mid if mid > 0 else 0.0

    # Depth at top vs total
    depth_ratio_top = (bid_size + ask_size) / total if total > 0 else 0.0

    # Weighted mid (depth-weighted price)
    levels = min(5, len(book.bids), len(book.asks))
    if levels > 0:
        weighted_bid = sum(float(lvl.price) * float(lvl.size) for lvl in book.bids[:levels])
        weighted_ask = sum(float(lvl.price) * float(lvl.size) for lvl in book.asks[:levels])
        weighted_total = sum(float(lvl.size) for lvl in book.bids[:levels]) + sum(
            float(lvl.size) for lvl in book.asks[:levels]
        )
        weighted_mid = (weighted_bid + weighted_ask) / weighted_total if weighted_total > 0 else mid
    else:
        weighted_mid = mid

    return {
        "midpoint": mid,
        "spread": spread,
        "relative_spread": relative_spread,
        "imbalance": imbalance,
        "microprice": microprice,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "bid_price": bid_price,
        "ask_price": ask_price,
        "depth_ratio_top": depth_ratio_top,
        "weighted_mid": weighted_mid,
        "mid_to_micro": mid - microprice,
    }


_FEATURE_NAMES = [
    "midpoint",
    "spread",
    "relative_spread",
    "imbalance",
    "microprice",
    "bid_size",
    "ask_size",
    "bid_price",
    "ask_price",
    "depth_ratio_top",
    "weighted_mid",
    "mid_to_micro",
]


class SklearnModel:
    """Base class for scikit-learn based probability models.

    Subclasses set self._model to a fitted sklearn estimator.
    """

    def __init__(self, uncertainty: float = 0.05, version: str = "sklearn-v1"):
        self._model = None
        self._uncertainty = uncertainty
        self.version = version

    def _features_to_x(self, features: dict[str, float]) -> list[float]:
        return [features.get(name, 0.0) for name in _FEATURE_NAMES]

    def predict(self, market_id: str, book: Book, at: datetime) -> Forecast:
        if self._model is None:
            raise ValueError("model not trained")

        features = _book_features(book)
        x = [self._features_to_x(features)]

        try:
            proba = self._model.predict_proba(x)[0]
            prob = D(str(min(max(float(proba[1]), 0.01), 0.99)))
        except AttributeError:
            # No predict_proba — use decision function
            raw = self._model.decision_function(x)[0]
            prob = D(str(1.0 / (1.0 + math.exp(-raw))))
            prob = max(D("0.01"), min(D("0.99"), prob))

        uncertainty = D(str(self._uncertainty))

        return Forecast(
            market_id=market_id,
            timestamp=at,
            probability=prob,
            lower=max(D(0), prob - uncertainty),
            upper=min(D(1), prob + uncertainty),
            version=self.version,
        )

    def to_dict(self) -> dict:
        """Serialize model parameters to a dict."""
        return {
            "version": self.version,
            "uncertainty": self._uncertainty,
        }

    @classmethod
    def from_dict(cls, params: dict, model=None):
        """Reconstruct from saved parameters."""
        obj = cls(
            uncertainty=params.get("uncertainty", 0.05), version=params.get("version", "sklearn-v1")
        )
        obj._model = model
        return obj


class LogisticRegressionModel(SklearnModel):
    """Logistic regression on order-book features.

    Pros: fast, interpretable, well-calibrated with enough data.
    Cons: linear decision boundary, misses non-linear interactions.
    """

    def __init__(
        self,
        C: float = 1.0,
        uncertainty: float = 0.05,
        version: str = "logistic-regression-v1",
    ):
        super().__init__(uncertainty=uncertainty, version=version)
        self.C = C

    def fit(self, X: list[list[float]], y: list[int]):
        from sklearn.linear_model import LogisticRegression

        self._model = LogisticRegression(C=self.C, max_iter=1000, solver="lbfgs")
        self._model.fit(X, y)
        return self

    def to_dict(self) -> dict:
        base = super().to_dict()
        base["C"] = self.C
        base["model_type"] = "logistic_regression"
        return base


class GradientBoostedModel(SklearnModel):
    """Gradient-boosted decision trees on order-book features.

    Pros: captures non-linear interactions, robust to outliers.
    Cons: slower, requires more data, prone to overfitting on small datasets.
    """

    def __init__(
        self,
        n_estimators: int = 100,
        max_depth: int = 3,
        learning_rate: float = 0.1,
        uncertainty: float = 0.05,
        version: str = "gradient-boosted-v1",
    ):
        super().__init__(uncertainty=uncertainty, version=version)
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self.learning_rate = learning_rate

    def fit(self, X: list[list[float]], y: list[int]):
        from sklearn.ensemble import GradientBoostingClassifier

        self._model = GradientBoostingClassifier(
            n_estimators=self.n_estimators,
            max_depth=self.max_depth,
            learning_rate=self.learning_rate,
            random_state=42,
        )
        self._model.fit(X, y)
        return self

    def to_dict(self) -> dict:
        base = super().to_dict()
        base["n_estimators"] = self.n_estimators
        base["max_depth"] = self.max_depth
        base["learning_rate"] = self.learning_rate
        base["model_type"] = "gradient_boosted"
        return base


class TrainedEnsembleModel:
    """Ensemble of trained component models with learned weights.

    Weights are optimized on a validation set to maximize calibration quality.
    Falls back to equal weights if no optimization has been performed.
    """

    def __init__(
        self,
        components: list[tuple[str, object]],
        weights: dict[str, float] | None = None,
        uncertainty: float = 0.05,
        version: str = "trained-ensemble-v1",
    ):
        if not components:
            raise ValueError("at least one component required")
        self.components = {name: model for name, model in components}
        n = len(components)
        self.weights = weights or {name: 1.0 / n for name, _ in components}
        self.uncertainty = uncertainty
        self.version = version

    def predict(self, market_id: str, book: Book, at: datetime) -> Forecast:
        """Generate ensemble forecast from all trained components."""
        forecasts = []
        total_weight = 0.0

        for name, model in self.components.items():
            try:
                forecast = model.predict(market_id, book, at)
                w = self.weights.get(name, 0.0)
                if w > 0:
                    forecasts.append((name, forecast, w))
                    total_weight += w
            except (ValueError, Exception):
                continue

        if not forecasts:
            raise ValueError("all ensemble components failed")

        if total_weight <= 0:
            total_weight = sum(self.weights.get(n, 0) for n, _, _ in forecasts)
            if total_weight <= 0:
                total_weight = 1.0

        weighted_p = sum(float(f.probability) * w for _, f, w in forecasts) / total_weight

        # Conservative bounds: widest interval from any component
        lowers = [f.lower for _, f, _ in forecasts]
        uppers = [f.upper for _, f, _ in forecasts]

        prob = D(str(min(max(weighted_p, 0.01), 0.99)))

        return Forecast(
            market_id=market_id,
            timestamp=at,
            probability=prob,
            lower=min(lowers) if lowers else max(D(0), prob - D(str(self.uncertainty))),
            upper=max(uppers) if uppers else min(D(1), prob + D(str(self.uncertainty))),
            version=self.version,
        )

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "uncertainty": self.uncertainty,
            "weights": self.weights,
            "components": {
                name: model.to_dict() if hasattr(model, "to_dict") else {}
                for name, model in self.components.items()
            },
        }

    @classmethod
    def from_dict(cls, params: dict, models: dict | None = None):
        """Reconstruct from saved parameters."""
        weights = params.get("weights", {})
        components = []
        for name in weights:
            model = (models or {}).get(name)
            if model is not None:
                components.append((name, model))
        return cls(
            components=components,
            weights=weights,
            uncertainty=params.get("uncertainty", 0.05),
            version=params.get("version", "trained-ensemble-v1"),
        )
