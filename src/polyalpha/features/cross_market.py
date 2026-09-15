"""Cross-market features for related contracts within the same event."""

from decimal import Decimal
from typing import Any

from ..domain import Book

D = Decimal


def price_correlation_features(
    yes_books: dict[str, Book],
    no_books: dict[str, Book],
) -> dict[str, Any]:
    """Features comparing prices across related markets.

    For markets in the same event cluster, price relationships can
    reveal relative-value opportunities.
    """
    if len(yes_books) < 2:
        return {"cross_market_spread_dispersion": None, "cross_market_mid_dispersion": None}

    mids = []
    spreads = []
    for token_id, book in yes_books.items():
        if book.mid is not None:
            mids.append(book.mid)
        if book.spread is not None:
            spreads.append(book.spread)

    if len(mids) < 2:
        return {"cross_market_spread_dispersion": None, "cross_market_mid_dispersion": None}

    mid_mean = sum(mids) / len(mids)
    mid_var = sum((m - mid_mean) ** 2 for m in mids) / len(mids)

    result = {
        "cross_market_mid_dispersion": mid_var.sqrt() if mid_var > 0 else D(0),
        "cross_market_count": len(yes_books),
    }

    if len(spreads) >= 2:
        spread_mean = sum(spreads) / len(spreads)
        spread_var = sum((s - spread_mean) ** 2 for s in spreads) / len(spreads)
        result["cross_market_spread_dispersion"] = spread_var.sqrt() if spread_var > 0 else D(0)
    else:
        result["cross_market_spread_dispersion"] = None

    return result


def relative_value_signals(
    yes_books: dict[str, Book],
    forecasts: dict[str, D],
) -> dict[str, Any]:
    """Compare model forecasts against market-implied probabilities across related markets.

    For partition constraints where P(A) + P(B) + ... should equal 1,
    deviations between model and market reveal potential edge.
    """
    if len(forecasts) < 2:
        return {"partition_deviation": None, "max_relative_mispricing": None}

    model_sum = sum(forecasts.values())
    deviation = abs(model_sum - D(1))

    # Find largest model-market discrepancy
    max_mispricing = D(0)
    for market_id, model_p in forecasts.items():
        book = yes_books.get(market_id)
        if book is not None and book.mid is not None:
            mispricing = abs(model_p - book.mid)
            max_mispricing = max(max_mispricing, mispricing)

    return {
        "partition_deviation": deviation,
        "max_relative_mispricing": max_mispricing,
        "model_partition_sum": model_sum,
    }
