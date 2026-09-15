"""Comprehensive order-book microstructure feature extraction.

All formulas are documented. Features are deterministic given a Book snapshot.
No future information is used.
"""

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..domain import Book

D = Decimal


@dataclass
class BookFeatureState:
    """Rolling state for computing features that require history."""

    midpoints: deque = field(default_factory=lambda: deque(maxlen=200))
    spreads: deque = field(default_factory=lambda: deque(maxlen=200))
    volumes_at_touch: deque = field(default_factory=lambda: deque(maxlen=200))
    last_trade_time: Any = None


def best_bid(book: Book) -> Decimal | None:
    """Best bid price."""
    return book.best_bid


def best_ask(book: Book) -> Decimal | None:
    """Best ask price."""
    return book.best_ask


def midprice(book: Book) -> Decimal | None:
    """Midprice = (best_bid + best_ask) / 2."""
    return book.mid


def spread(book: Book) -> Decimal | None:
    """Spread = best_ask - best_bid."""
    return book.spread


def relative_spread(book: Book) -> Decimal | None:
    """Relative spread = spread / midprice."""
    mid = book.mid
    if mid is None or mid == 0:
        return None
    return book.spread / mid


def bid_depth(book: Book, levels: int = 5) -> Decimal:
    """Sum of top N bid sizes."""
    return sum((x.size for x in book.bids[:levels]), D(0))


def ask_depth(book: Book, levels: int = 5) -> Decimal:
    """Sum of top N ask sizes."""
    return sum((x.size for x in book.asks[:levels]), D(0))


def orderbook_imbalance(book: Book, levels: int = 5) -> Decimal | None:
    """Top-of-book imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth).

    Range: [-1, 1]. Positive means more bid-side depth (buy pressure).
    """
    bd = bid_depth(book, levels)
    ad = ask_depth(book, levels)
    total = bd + ad
    if total == 0:
        return D(0)
    return (bd - ad) / total


def microprice(book: Book) -> Decimal | None:
    """Microprice = (ask * bid_size + bid * ask_size) / (bid_size + ask_size).

    Weighted price reflecting the order-book imbalance at top of book.
    If bid_size > ask_size, microprice is pulled toward bid (and vice versa).
    """
    if book.best_bid is None or book.best_ask is None:
        return None
    bid_size = book.bids[0].size
    ask_size = book.asks[0].size
    total = bid_size + ask_size
    if total == 0:
        return book.mid
    return (book.best_ask * bid_size + book.best_bid * ask_size) / total


def depth_imbalance(book: Book, levels: int = 10) -> Decimal | None:
    """Depth imbalance at N levels."""
    return orderbook_imbalance(book, levels)


def weighted_mid(book: Book) -> Decimal | None:
    """Weighted midprice using microprice formula."""
    return microprice(book)


def spread_bps(book: Book) -> Decimal | None:
    """Spread in basis points = spread / mid * 10000."""
    mid = book.mid
    if mid is None or mid == 0:
        return None
    return (book.spread / mid) * D(10000)


def book_slope(book: Book, levels: int = 5) -> dict[str, Decimal | None]:
    """Approximate slope of the order book (price impact per unit size).

    Slope = (ask_N - ask_1) / ask_depth for asks, and similarly for bids.
    Higher slope means less liquidity per price unit.
    """
    result = {}
    if len(book.asks) >= 2 and book.asks[0].size > 0:
        ask_slope = (book.asks[min(levels, len(book.asks)) - 1].price - book.asks[0].price) / max(
            ask_depth(book, levels), D(1)
        )
        result["ask_slope"] = ask_slope
    else:
        result["ask_slope"] = None
    if len(book.bids) >= 2 and book.bids[0].size > 0:
        bid_slope = (book.bids[0].price - book.bids[min(levels, len(book.bids)) - 1].price) / max(
            bid_depth(book, levels), D(1)
        )
        result["bid_slope"] = bid_slope
    else:
        result["bid_slope"] = None
    return result


def liquidity_concentration(book: Book, levels: int = 5) -> dict[str, Decimal | None]:
    """Herfindahl-like concentration of liquidity.

    concentration = sum(size_i^2) / (sum(size_i))^2

    Higher value means liquidity is concentrated at fewer price levels.
    """
    result = {}
    for side_name, side_levels in [("bid", book.bids[:levels]), ("ask", book.asks[:levels])]:
        sizes = [x.size for x in side_levels]
        total = sum(sizes, D(0))
        if total == 0:
            result[side_name + "_concentration"] = None
            continue
        hhi = sum((s * s for s in sizes), D(0)) / (total * total)
        result[side_name + "_concentration"] = hhi
    return result


def price_distance_from_extremes(book: Book, state: BookFeatureState) -> dict[str, Decimal | None]:
    """Distance of current mid from recent high/low midpoints."""
    if not state.midpoints:
        return {"dist_from_high": None, "dist_from_low": None}
    mid = book.mid
    if mid is None:
        return {"dist_from_high": None, "dist_from_low": None}
    recent_high = max(state.midpoints)
    recent_low = min(state.midpoints)
    range_ = recent_high - recent_low
    if range_ == 0:
        return {"dist_from_high": D(0), "dist_from_low": D(0)}
    return {
        "dist_from_high": (recent_high - mid) / range_,
        "dist_from_low": (mid - recent_low) / range_,
    }


def momentum(book: Book, state: BookFeatureState) -> Decimal | None:
    """Short-term momentum = current_mid - oldest_mid in window."""
    mid = book.mid
    if mid is None or not state.midpoints:
        return D(0)
    return mid - state.midpoints[0]


def realized_volatility(state: BookFeatureState) -> Decimal | None:
    """Short-term realized volatility from midpoint returns.

    Uses log returns: std(log(p_t / p_{t-1})).
    """
    if len(state.midpoints) < 3:
        return None
    mids = list(state.midpoints)
    log_returns = []
    for i in range(1, len(mids)):
        if mids[i - 1] > 0 and mids[i] > 0:
            log_returns.append((mids[i] / mids[i - 1]).ln())
    if len(log_returns) < 2:
        return None
    mean_r = sum(log_returns) / len(log_returns)
    variance = sum((r - mean_r) ** 2 for r in log_returns) / (len(log_returns) - 1)
    return variance.sqrt()


def spread_change(book: Book, state: BookFeatureState) -> Decimal | None:
    """Current spread minus previous spread."""
    if book.spread is None or not state.spreads:
        return None
    return book.spread - state.spreads[-1]


def time_since_last_trade(state: BookFeatureState, now) -> Decimal | None:
    """Seconds since last observed trade (if available)."""
    if state.last_trade_time is None:
        return None
    delta = (now - state.last_trade_time).total_seconds()
    return D(str(delta))


def extract_all_features(
    book: Book,
    state: BookFeatureState | None = None,
    levels: int = 5,
) -> dict[str, Any]:
    """Extract all order-book features from a single Book snapshot.

    Returns a flat dictionary of feature_name -> Decimal value.
    None values indicate features that could not be computed.
    """
    if state is None:
        state = BookFeatureState()

    features = {}

    # Core price features
    features["midpoint"] = midprice(book)
    features["best_bid"] = best_bid(book)
    features["best_ask"] = best_ask(book)
    features["spread"] = spread(book)
    features["relative_spread"] = relative_spread(book)
    features["microprice"] = microprice(book)
    features["weighted_mid"] = weighted_mid(book)
    features["spread_bps"] = spread_bps(book)

    # Depth features
    features["bid_depth"] = bid_depth(book, levels)
    features["ask_depth"] = ask_depth(book, levels)
    features["imbalance"] = orderbook_imbalance(book, levels)
    features["depth_imbalance_10"] = depth_imbalance(book, 10)

    # Slope features
    slopes = book_slope(book, levels)
    features["ask_slope"] = slopes["ask_slope"]
    features["bid_slope"] = slopes["bid_slope"]

    # Concentration features
    conc = liquidity_concentration(book, levels)
    features["bid_concentration"] = conc["bid_concentration"]
    features["ask_concentration"] = conc["ask_concentration"]

    # Rolling features (require state)
    if state is not None:
        features["momentum"] = momentum(book, state)
        features["realized_volatility"] = realized_volatility(state)
        features["spread_change"] = spread_change(book, state)
        dist = price_distance_from_extremes(book, state)
        features["dist_from_high"] = dist["dist_from_high"]
        features["dist_from_low"] = dist["dist_from_low"]

    # Update state
    if state is not None and book.mid is not None:
        if state.midpoints and book.mid == state.midpoints[-1]:
            pass  # no update for same price
        else:
            state.midpoints.append(book.mid)
        if book.spread is not None:
            state.spreads.append(book.spread)

    return features
