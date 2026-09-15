"""Quality checks return all reasons, including timing and missing metadata.

Market quality scoring provides a continuous ranking of market suitability.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .domain import Book, Market, utc

D = Decimal


@dataclass(frozen=True)
class Filter:
    min_liquidity: Decimal = Decimal(5000)
    min_volume: Decimal = Decimal(25000)
    max_spread: Decimal = Decimal("0.05")
    max_age_seconds: int = 30
    min_depth_shares: Decimal = Decimal(100)

    def __post_init__(self):
        for value in (
            self.min_liquidity,
            self.min_volume,
            self.max_spread,
            self.min_depth_shares,
        ):
            if not value.is_finite() or value < 0:
                raise ValueError("invalid filter threshold")
        if self.max_age_seconds <= 0:
            raise ValueError("invalid maximum age")

    def market_reasons(self, market: Market, now: datetime) -> list[str]:
        reasons = []
        if not market.active or market.closed:
            reasons.append("inactive_or_closed")
        if not market.accepting_orders or not market.enable_order_book:
            reasons.append("orders_unavailable")
        if market.received_at > utc(now):
            reasons.append("future_metadata")
        if market.liquidity is None or market.liquidity < self.min_liquidity:
            reasons.append("liquidity_missing_or_low")
        if market.volume is None or market.volume < self.min_volume:
            reasons.append("volume_missing_or_low")
        if market.deadline is None or market.deadline <= utc(now):
            reasons.append("deadline_missing_or_elapsed")
        return reasons

    def book_reasons(self, book: Book, now: datetime) -> list[str]:
        now = utc(now)
        reasons = []
        if book.received_at > now or book.source_at > now:
            reasons.append("future_book")
        if (now - book.source_at).total_seconds() > self.max_age_seconds:
            reasons.append("stale_book")
        if book.spread is None:
            reasons.append("one_sided_book")
        elif book.spread <= 0:
            reasons.append("locked_or_crossed_book")
        elif book.spread > self.max_spread:
            reasons.append("spread_too_wide")
        if any(
            sum((x.size for x in side), Decimal(0)) < self.min_depth_shares
            for side in (book.bids, book.asks)
        ):
            reasons.append("insufficient_depth")
        return reasons


def market_quality_score(
    market: Market,
    book: Book | None = None,
    weights: dict[str, Decimal] | None = None,
) -> Decimal:
    """Compute a continuous quality score for market suitability.

    Higher is better. Used for ranking markets, not binary inclusion/exclusion.

    score = w1 * log(1 + liquidity)
          + w2 * log(1 + volume)
          - w3 * spread
          + w4 * depth_score
          + w5 * resolution_clarity

    Weights are configurable. Defaults are illustrative, not proven optimal.
    """
    if weights is None:
        weights = {
            "liquidity": D("0.30"),
            "volume": D("0.25"),
            "spread": D("0.20"),
            "depth": D("0.15"),
            "resolution": D("0.10"),
        }

    score = D(0)

    # Liquidity component (log-scaled)
    liq = market.liquidity or D(0)
    score += weights["liquidity"] * (D(1) + liq).ln()

    # Volume component (log-scaled)
    vol = market.volume or D(0)
    score += weights["volume"] * (D(1) + vol).ln()

    # Spread component (negative: wider spread = lower score)
    if book is not None and book.spread is not None:
        score -= weights["spread"] * book.spread * D(100)  # scale spread

    # Depth component
    if book is not None:
        total_depth = sum((x.size for x in book.bids[:5]), D(0)) + sum(
            (x.size for x in book.asks[:5]), D(0)
        )
        depth_score = (D(1) + total_depth).ln() / D(10)  # normalize
        score += weights["depth"] * min(depth_score, D(1))

    # Resolution clarity (presence of resolution source)
    if market.resolution_source:
        score += weights["resolution"]

    return score


def rank_markets(
    markets: list[tuple[Market, Book | None]],
    min_score: Decimal | None = None,
) -> list[tuple[Market, Book | None, Decimal]]:
    """Rank markets by quality score, descending.

    Returns list of (market, book, score) tuples.
    """
    scored = [(market, book, market_quality_score(market, book)) for market, book in markets]
    scored.sort(key=lambda x: x[2], reverse=True)
    if min_score is not None:
        scored = [(m, b, s) for m, b, s in scored if s >= min_score]
    return scored
