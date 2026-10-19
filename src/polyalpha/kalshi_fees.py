"""Kalshi execution-friction (fee) model — provenance-gated, fail-closed.

The screen edge is:

    Edge_screen = p_model - p_executable - Fee_exact_venue_rule - B

where ``B`` is an explicit, frozen adverse-selection/slippage buffer (per
contract). ``p_model`` is the EMPIRICAL model probability used verbatim — it is
NOT a lower-confidence bound, so "screen" (an operational hurdle) is the honest
name, never "conservative".

Kalshi's published prediction-market fee shape is:

    Fee = ceil_{$0.01}( M * 0.07 * C * P * (1 - P) )      taker
    Fee = ceil_{$0.01}( M * 0.0175 * C * P * (1 - P) )    maker (only when M != 0)

Resting orders are not charged the general taker fee; the schedule's DEFAULT
maker multiplier is 0 unless a series is otherwise indicated. For the ERCOT
series (KXTXERCOTPEAKD) the natural reading of the current published schedule is
taker M=1 and maker M=0, but this is an INFERENCE FROM A TABLE'S ABSENCE, not an
authoritative rule. A ``FeeRule`` is only usable for a capital decision once its
``verified_at`` provenance is recorded against the live venue. Until then every
edge computation returns ``None`` (fail-closed => no trade).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal

# Published general prediction-market rates (docs.kalshi.com). NOT live-verified.
FEE_TAKER_RATE_PUBLISHED = Decimal("0.07")
FEE_MAKER_RATE_PUBLISHED = Decimal("0.0175")

# Frozen adverse-selection/slippage buffer, per contract (LIVE_CALIBRATION_V0).
# Raised from 0.01 to 0.02 for the first calibration tranche: 1c does not buy
# much protection against stale-book/adverse-selection error while we are
# explicitly trying to MEASURE real friction.
SLIPPAGE_ADVERSE_SELECTION_BUFFER = Decimal("0.02")


@dataclass(frozen=True)
class FeeRule:
    """A fee rule tied to an exact series/product at trade time, with provenance.

    ``taker_rate`` / ``maker_rate`` are the effective per-contract rates (the
    published rate times the series multiplier). For the ERCOT series the
    inferred defaults are taker=0.07 (M=1) and maker=0.00 (M=0) — UNVERIFIED.
    """

    series_ticker: str
    taker_rate: Decimal
    maker_rate: Decimal
    source: str
    verified_at: str | None = None  # ISO timestamp; None => NOT verified (unusable)

    @property
    def verified(self) -> bool:
        return self.verified_at is not None


def verify_fee_rule(rule: FeeRule, source: str | None = None) -> FeeRule:
    """Return a copy of ``rule`` marked verified with the current timestamp.

    This is an OPERATOR action: it records that the rule was checked against the
    authoritative live schedule. Strategy code must never call it.
    """
    return FeeRule(
        series_ticker=rule.series_ticker,
        taker_rate=rule.taker_rate,
        maker_rate=rule.maker_rate,
        source=source or rule.source,
        verified_at=datetime.now(UTC).isoformat(),
    )


def prediction_market_fee(count: Decimal, price: Decimal, rate: Decimal) -> Decimal:
    """Exact per-order fee = ceil_{$0.01}(rate * count * price * (1 - price)).

    ``count`` is contracts; ``price`` is the executable contract price in (0,1).
    The result is a non-negative dollar amount.
    """
    count = Decimal(count)
    price = Decimal(price)
    rate = Decimal(rate)
    raw = rate * count * price * (Decimal(1) - price)
    return raw.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def fee_per_contract(price: Decimal, rate: Decimal) -> Decimal:
    """Approximate fee per contract (dollars) = rate * P * (1 - P).

    Used for the per-contract screen-edge check; the exact per-order fee is
    ``prediction_market_fee`` (ceiling-quantized), applied at execution.
    """
    price = Decimal(price)
    rate = Decimal(rate)
    return rate * price * (Decimal(1) - price)


def screen_edge_per_contract(
    p_model: Decimal,
    p_executable: Decimal,
    fee_rule: FeeRule | None,
    *,
    is_maker: bool = True,
) -> Decimal | None:
    """Net screen edge per contract, after exact fee + frozen buffer.

    Returns ``None`` (fail-closed) when the fee rule is absent or unverified, so
    an unverified fee schedule can never produce a tradeable edge. ``is_maker``
    selects the maker rate (resting order) vs taker rate (marketable order).
    """
    if fee_rule is None or not fee_rule.verified:
        return None
    rate = fee_rule.maker_rate if is_maker else fee_rule.taker_rate
    fee = fee_per_contract(p_executable, rate)
    return Decimal(p_model) - Decimal(p_executable) - fee - SLIPPAGE_ADVERSE_SELECTION_BUFFER