"""Position sizing with fractional Kelly and fixed fractional methods."""

from dataclasses import dataclass
from decimal import Decimal

from .expected_value import fractional_kelly, kelly_fraction

D = Decimal


@dataclass(frozen=True)
class SizingResult:
    """Result of position sizing calculation."""

    shares: Decimal
    notional: Decimal
    fraction_of_bankroll: Decimal
    method: str
    kelly_full: Decimal | None = None
    kelly_fractional: Decimal | None = None
    capped_by: str | None = None


def fixed_fractional_sizing(
    equity: Decimal,
    fraction: Decimal = D("0.005"),
    max_shares: Decimal = D("100000"),
) -> Decimal:
    """Fixed fractional position sizing.

    size = equity * fraction
    """
    if not equity.is_finite() or equity <= 0:
        raise ValueError("positive equity required")
    if not fraction.is_finite() or not 0 < fraction <= 1:
        raise ValueError("fraction must be in (0, 1]")
    size = equity * fraction
    return min(size, max_shares)


def kelly_sizing(
    probability: Decimal,
    price: Decimal,
    equity: Decimal,
    kelly_multiplier: Decimal = D("0.25"),
    max_fraction: Decimal = D("0.02"),
) -> SizingResult:
    """Kelly-criterion-based position sizing with conservative constraints.

    Args:
        probability: Estimated fair probability
        price: Execution price
        equity: Current portfolio equity
        kelly_multiplier: Fraction of full Kelly to use (default 0.25)
        max_fraction: Maximum fraction of equity for this position

    Returns:
        SizingResult with recommended position size
    """
    if not equity.is_finite() or equity <= 0:
        raise ValueError("positive equity required")

    full_kelly = kelly_fraction(probability, price)
    frac_kelly = fractional_kelly(probability, price, kelly_multiplier)

    # Cap by max fraction
    effective_fraction = min(frac_kelly, max_fraction)
    capped_by = None
    if effective_fraction == max_fraction and frac_kelly > max_fraction:
        capped_by = "max_fraction"

    notional = equity * effective_fraction
    shares = (notional / price).quantize(D("0.01")) if price > 0 else D(0)

    return SizingResult(
        shares=shares,
        notional=shares * price,
        fraction_of_bankroll=effective_fraction,
        method="fractional_kelly",
        kelly_full=full_kelly,
        kelly_fractional=frac_kelly,
        capped_by=capped_by,
    )


def constrained_sizing(
    equity: Decimal,
    risk_budget: Decimal,
    price: Decimal,
    normal_fraction: Decimal = D("0.005"),
    kelly_multiplier: Decimal = D("0.25"),
    max_market_fraction: Decimal = D("0.02"),
    use_kelly: bool = False,
    probability: Decimal | None = None,
) -> SizingResult:
    """Position sizing that respects all risk constraints.

    Args:
        equity: Current portfolio equity
        risk_budget: Maximum dollar amount from risk engine
        price: Execution price
        normal_fraction: Normal per-trade fraction
        kelly_multiplier: Kelly multiplier if using Kelly
        max_market_fraction: Maximum per-market fraction
        use_kelly: Whether to use Kelly sizing
        probability: Fair probability (required if use_kelly=True)

    Returns:
        SizingResult with constrained position size
    """
    if not equity.is_finite() or equity <= 0:
        raise ValueError("positive equity required")

    # Base size from fixed fractional
    base_notional = equity * normal_fraction

    # Kelly size if enabled
    if use_kelly and probability is not None:
        kelly = kelly_sizing(probability, price, equity, kelly_multiplier, max_market_fraction)
        base_notional = min(base_notional, kelly.notional)

    # Apply risk budget constraint
    effective_notional = min(base_notional, risk_budget)

    # Apply market cap constraint
    market_cap = equity * max_market_fraction
    effective_notional = min(effective_notional, market_cap)

    shares = (effective_notional / price).quantize(D("0.01")) if price > 0 else D(0)

    # Determine what capped us
    capped_by = None
    if effective_notional < base_notional:
        if effective_notional == risk_budget:
            capped_by = "risk_budget"
        elif effective_notional == market_cap:
            capped_by = "market_cap"

    fraction = effective_notional / equity if equity > 0 else D(0)

    return SizingResult(
        shares=shares,
        notional=shares * price,
        fraction_of_bankroll=fraction,
        method="kelly" if use_kelly else "fixed_fractional",
        kelly_full=kelly_sizing(probability, price, equity).kelly_full
        if (use_kelly and probability)
        else None,
        kelly_fractional=kelly_sizing(probability, price, equity, kelly_multiplier).kelly_fractional
        if (use_kelly and probability)
        else None,
        capped_by=capped_by,
    )
