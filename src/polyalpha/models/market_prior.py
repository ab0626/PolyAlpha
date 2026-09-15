"""Market-prior probability model.

Component A in the ensemble: uses the market's own order book as a
Bayesian prior for probability estimation.

The market price reflects the collective wisdom of all participants.
It is NOT the fair probability, but it is a useful starting point.
This model adjusts the market price using:
- Spread information (wider spread = more uncertainty)
- Depth-weighted price (microprice vs midpoint)
- Imbalance as a directional correction

This is explicitly NOT an independent information source.
It is infrastructure for validating the pipeline and as a baseline
component in the ensemble.
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
class MarketPriorModel:
    """Uses market price as a prior with order-book adjustments.

    fair_probability = mid + alpha * (microprice - mid) + beta * spread * imbalance

    This is a structural prior, not an independent forecast.
    The alpha and beta parameters are illustrative defaults, NOT
    empirically optimized values.
    """

    alpha: Decimal = D("0.25")
    beta: Decimal = D("0.20")
    uncertainty_base: Decimal = D("0.05")
    version: str = "market-prior-v1"

    def predict(self, market_id: str, book: Book, at: datetime) -> Forecast:
        """Generate forecast from market data alone."""
        if book.mid is None or book.spread is None or book.spread <= 0:
            raise ValueError("unusable book for market prior model")

        mid = midprice(book)
        mp = microprice(book)
        imb = orderbook_imbalance(book, levels=5)
        sp = spread(book)

        # Microprice captures volume-weighted fair value
        # Use it as a correction to the midpoint
        micro_correction = self.alpha * (mp - mid)

        # Spread-imbalance interaction: when spread is wide and
        # book is imbalanced, the midpoint is less reliable
        spread_imb_correction = self.beta * sp * imb

        fair_p = mid + micro_correction + spread_imb_correction

        # Clamp to valid range
        fair_p = max(D("0.01"), min(D("0.99"), fair_p))

        # Uncertainty: wider spread = more uncertainty
        rel_spread = sp / mid if mid > 0 else D(0)
        uncertainty = max(self.uncertainty_base, rel_spread * D("0.5"))

        return Forecast(
            market_id=market_id,
            timestamp=at,
            probability=fair_p,
            lower=max(D(0), fair_p - uncertainty),
            upper=min(D(1), fair_p + uncertainty),
            version=self.version,
        )
