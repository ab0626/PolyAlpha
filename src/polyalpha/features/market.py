"""Market-level features derived from metadata."""

from decimal import Decimal

from ..domain import Market

D = Decimal


def liquidity_score(market: Market) -> Decimal:
    """Log-scaled liquidity: log(1 + liquidity)."""
    liq = market.liquidity or D(0)
    return (D(1) + liq).ln()


def volume_score(market: Market) -> Decimal:
    """Log-scaled volume: log(1 + volume)."""
    vol = market.volume or D(0)
    return (D(1) + vol).ln()


def liquidity_volume_ratio(market: Market) -> Decimal | None:
    """Volume-to-liquidity ratio. High ratio means active trading relative to depth."""
    liq = market.liquidity
    vol = market.volume
    if liq is None or vol is None or liq == 0:
        return None
    return vol / liq


def has_fee_schedule(market: Market) -> int:
    """1 if market has explicit fee schedule, 0 otherwise."""
    return 1 if market.fees_enabled is True and market.fee_parameters_json else 0


def has_resolution_source(market: Market) -> int:
    """1 if market has a defined resolution source."""
    return 1 if market.resolution_source else 0


def extract_market_features(market: Market) -> dict[str, Decimal | int | None]:
    """Extract all market-level features."""
    return {
        "liquidity_score": liquidity_score(market),
        "volume_score": volume_score(market),
        "liquidity_volume_ratio": liquidity_volume_ratio(market),
        "has_fee_schedule": has_fee_schedule(market),
        "has_resolution_source": has_resolution_source(market),
    }
