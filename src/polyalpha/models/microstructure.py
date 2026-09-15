"""Microstructure-based probability model.

Uses order-book dynamics to estimate fair probability.
This is Component B in the model ensemble - independent of market price.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..domain import Book
from ..features.orderbook import (
    microprice,
    midprice,
    orderbook_imbalance,
    spread,
)
from ..forecasting import Forecast

D = Decimal


@dataclass
class MicrostructureModel:
    """Probability model based on order-book microstructure.

    Uses the premise that microprice (volume-weighted mid) is a better
    estimate of fair value than the simple mid, and that order-book
    dynamics contain predictive information.

    fair_probability = microprice
                     + alpha * imbalance_momentum
                     + beta * spread_signal

    Where:
    - microprice captures volume-weighted fair value
    - imbalance_momentum captures directional pressure
    - spread_signal captures market uncertainty

    This is NOT a proven alpha source. It is an infrastructure component
    for testing the pipeline.
    """

    imbalance_weight: Decimal = D("0.10")
    spread_weight: Decimal = D("0.05")
    volatility_damping: Decimal = D("0.50")
    uncertainty_floor: Decimal = D("0.03")
    version: str = "microstructure-v1"

    def predict(self, market_id: str, book: Book, at: datetime) -> Forecast:
        """Generate forecast from microstructure features."""
        if book.mid is None or book.spread is None or book.spread <= 0:
            raise ValueError("unusable book for microstructure model")

        mp = microprice(book)
        mid = midprice(book)

        imb = orderbook_imbalance(book, levels=5)

        rel_spread = spread(book) / mid if mid > 0 else D(0)

        imbalance_signal = imb * self.imbalance_weight

        raw_p = mp + imbalance_signal

        fair_p = max(D("0.01"), min(D("0.99"), raw_p))
        uncertainty = max(self.uncertainty_floor, rel_spread * D("0.5"))

        return Forecast(
            market_id=market_id,
            timestamp=at,
            probability=fair_p,
            lower=max(D(0), fair_p - uncertainty),
            upper=min(D(1), fair_p + uncertainty),
            version=self.version,
        )
