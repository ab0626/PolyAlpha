"""Independent reference implementations for high-risk math.

For the highest-risk calculations — price/quantity scaling, gross edge, Kelly
sizing, fractional Kelly, and fee-per-share — this module implements a SECOND,
deliberately simple reference calculation from first principles and asserts it
agrees with the production path over fuzzed inputs.

These references are written independently (not copied from production), so an
agreement is evidence the production formula is right; a disagreement is a
real bug in one of the two. This is the cheapest way to catch the semantic
errors unit tests miss.
"""

from datetime import UTC, datetime
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from polyalpha.execution import Fill
from polyalpha.expected_value import (
    calculate_fee_per_share,
    calculate_gross_edge,
    fractional_kelly,
    kelly_fraction,
)
from polyalpha.us.instruments import scaled_qty_to_decimal, scaled_to_decimal

D = Decimal
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def ref_settings(max_examples=120):
    return settings(
        max_examples=max_examples,
        derandomize=True,
        suppress_health_check=[HealthCheck.too_slow],
    )


# ── Independent reference implementations ────────────────────────────────────


def ref_scaled(px: int, scale: int) -> Decimal:
    """Reference: scaled integer / scale, computed with Decimal directly."""
    return D(px) / D(scale)


def ref_qty(qty: int, scale: int) -> Decimal:
    return D(qty) / D(scale)


def ref_gross_edge(fair: Decimal, vwap: Decimal, side: str) -> Decimal:
    if side == "YES":
        return fair - vwap
    return (D(1) - fair) - vwap


def ref_kelly(probability: Decimal, price: Decimal) -> Decimal:
    """Reference binary Kelly for a $1-payout contract, from first principles:
        f* = (p - price) / (1 - price), clipped to [0, 1].
    """
    if not (D(0) < price < D(1)):
        return D(0)
    if not (D(0) <= probability <= D(1)):
        return D(0)
    edge = probability - price
    if edge <= 0:
        return D(0)
    f = edge / (D(1) - price)
    return max(D(0), min(f, D(1)))


def ref_fee_per_share(fees: Decimal, shares: Decimal) -> Decimal:
    if shares <= 0:
        return D(0)
    return fees / shares


# ── Comparisons ──────────────────────────────────────────────────────────────

_prob = st.decimals(min_value=D(0), max_value=D(1), places=4,
                    allow_nan=False, allow_infinity=False)
_price = st.decimals(min_value=D("0.0001"), max_value=D("0.9999"), places=4,
                     allow_nan=False, allow_infinity=False)


@ref_settings()
@given(
    px=st.integers(min_value=0, max_value=10**9),
    scale=st.integers(min_value=1, max_value=10**6),
)
def test_price_scaling_matches_reference(px, scale):
    assert scaled_to_decimal(px, scale) == ref_scaled(px, scale)


@ref_settings()
@given(
    qty=st.integers(min_value=0, max_value=10**9),
    scale=st.integers(min_value=1, max_value=10**6),
)
def test_qty_scaling_matches_reference(qty, scale):
    assert scaled_qty_to_decimal(qty, scale) == ref_qty(qty, scale)


@ref_settings()
@given(fair=_prob, vwap=_price, side=st.sampled_from(["YES", "NO"]))
def test_gross_edge_matches_reference(fair, vwap, side):
    assert calculate_gross_edge(fair, vwap, side) == ref_gross_edge(fair, vwap, side)


@ref_settings()
@given(p=_prob, price=_price)
def test_kelly_matches_reference(p, price):
    assert kelly_fraction(p, price) == ref_kelly(p, price)


@ref_settings()
@given(
    p=_prob,
    price=_price,
    mult=st.decimals(min_value=D(0), max_value=D(1), places=2,
                     allow_nan=False, allow_infinity=False),
)
def test_fractional_kelly_matches_reference(p, price, mult):
    assert fractional_kelly(p, price, mult) == ref_kelly(p, price) * mult


@ref_settings(80)
@given(
    fees=st.decimals(min_value=D(0), max_value=D("1000"), places=4,
                     allow_nan=False, allow_infinity=False),
    shares=st.decimals(min_value=D(1), max_value=D("10000"), places=0,
                       allow_nan=False, allow_infinity=False),
)
def test_fee_per_share_matches_reference(fees, shares):
    # Price 0.5 keeps notional exact for whole-share quantities, so the Fill
    # quantity/notional conservation check is satisfied.
    fill = Fill(
        order_id="o1", token_id="t1", side="BUY",
        requested=shares, shares=shares,
        notional=shares * D("0.5"),
        fees=fees, depth_slippage=D(0), filled_at=NOW,
        levels=((D("0.5"), shares),),
    )
    assert calculate_fee_per_share(fill) == ref_fee_per_share(fees, shares)


# ── Algebraic invariants ─────────────────────────────────────────────────────


@ref_settings()
@given(p=_prob, price=_price)
def test_kelly_never_negative_or_above_one(p, price):
    f = kelly_fraction(p, price)
    assert D(0) <= f <= D(1)


@ref_settings()
@given(fair=_prob, vwap=_price)
def test_yes_no_edge_symmetry(fair, vwap):
    # yes_edge + no_edge = 1 - 2*vwap  (an exact algebraic identity)
    yes = calculate_gross_edge(fair, vwap, "YES")
    no = calculate_gross_edge(fair, vwap, "NO")
    assert yes + no == D(1) - D(2) * vwap


@ref_settings()
@given(p=_prob, price=_price)
def test_fractional_kelly_le_full_kelly(p, price):
    assert fractional_kelly(p, price, D("0.25")) <= kelly_fraction(p, price)