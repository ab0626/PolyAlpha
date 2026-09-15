"""Temporal features derived from market metadata and timestamps."""

from datetime import datetime
from decimal import Decimal

from ..domain import Market, utc

D = Decimal


def time_until_resolution(market: Market, at: datetime) -> Decimal | None:
    """Seconds until resolution deadline. None if no deadline."""
    if market.deadline is None:
        return None
    at = utc(at)
    delta = (market.deadline - at).total_seconds()
    return D(str(max(0, delta)))


def market_age(market: Market, at: datetime) -> Decimal:
    """Seconds since market metadata was received."""
    at = utc(at)
    return D(str((at - market.received_at).total_seconds()))


def resolution_urgency(market: Market, at: datetime) -> Decimal | None:
    """Urgency score = 1 / (1 + days_until_resolution / 7).

    Approaches 1 as deadline nears, 0 as deadline is far.
    Useful as a feature for models that behave differently near expiry.
    """
    secs = time_until_resolution(market, at)
    if secs is None:
        return D("0.5")  # unknown urgency
    days = secs / D(86400)
    return D(1) / (D(1) + days / D(7))


def is_weekend(at: datetime) -> int:
    """1 if Saturday/Sunday, 0 otherwise. Some markets resolve on weekdays."""
    return 1 if at.weekday() >= 5 else 0


def hour_of_day(at: datetime) -> int:
    """UTC hour 0-23. Liquidity patterns vary by time of day."""
    return at.hour


def days_in_week(at: datetime) -> int:
    """Day of week 0=Monday, 6=Sunday."""
    return at.weekday()


def extract_temporal_features(market: Market, at: datetime) -> dict[str, Decimal | int | None]:
    """Extract all temporal features from market metadata."""
    return {
        "time_until_resolution": time_until_resolution(market, at),
        "market_age_seconds": market_age(market, at),
        "resolution_urgency": resolution_urgency(market, at),
        "is_weekend": is_weekend(at),
        "hour_of_day": hour_of_day(at),
        "day_of_week": days_in_week(at),
    }
