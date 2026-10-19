"""Kalshi execution-friction (fee) model — provenance-gated, fail-closed.

The conservative live edge is:

    Edge_live = p_conservative - p_executable - Fee_exact_venue_rule - B

where ``B`` is an explicit, frozen adverse-selection/slippage buffer (per
contract). The fee is Kalshi's published prediction-market shape:

    Fee = ceil_{$0.01}( r * C * P * (1 - P) )

with published general taker ``r = 0.07`` and maker ``r = 0.0175``. These are
the PUBLISHED numbers only; Kalshi directs users to the live fee schedule for
current market-specific treatment (product families can carry special rules).

Therefore nothing here is assumed live. A ``FeeRule`` is only usable for a
capital decision once its ``verified_at`` provenance is recorded (verified
against the authoritative rule in force for the exact series at trade time).
Until then, every edge computation returns ``None`` (fail-closed => no trade).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal

# Published general prediction-market schedule (docs.kalshi.com). NOT live-verified.
FEE_TAKER_RATE_PUBLISHED = Decimal("0.07")
FEE_MAKER_RATE_PUBLISHED = Decimal("0.0175")

# Frozen adverse-selection/slippage buffer, per contract. Explicit and fixed
# before trading; review before the first live order.
SLIPPAGE_ADVERSE_SELECTION_BUFFER = Decimal("0.01")


@dataclass(frozen=True)
class FeeRule:
    """A fee rule tied to an exact series/product at trade time, with provenance."""

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

    Used for the per-contract conservative-edge check; the exact per-order fee
    is ``prediction_market_fee`` (ceiling-quantized), applied at execution.
    """
    price = Decimal(price)
    rate = Decimal(rate)
    return rate * price * (Decimal(1) - price)


def conservative_edge_per_contract(
    p_model: Decimal,
    p_executable: Decimal,
    fee_rule: FeeRule | None,
    *,
    is_maker: bool = True,
) -> Decimal | None:
    """Net conservative edge per contract, after exact fee + frozen buffer.

    Returns ``None`` (fail-closed) when the fee rule is absent or unverified, so
    an unverified fee schedule can never produce a tradeable edge.
    """
    if fee_rule is None or not fee_rule.verified:
        return None
    rate = fee_rule.maker_rate if is_maker else fee_rule.taker_rate
    fee = fee_per_contract(p_executable, rate)
    return Decimal(p_model) - Decimal(p_executable) - fee - SLIPPAGE_ADVERSE_SELECTION_BUFFER