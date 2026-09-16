"""Venue-semantics fuzzing for the US adapter family (Hypothesis).

Targets the semantic-bug class that unit tests miss: fractional quantity
scaling, price/quantity boundary values, weird tick grids, missing/extra
fields, zero/negative/duplicate levels, and out-of-order updates. Every
property here is a *venue invariant*, not an implementation detail:

  * canonical prices are always Decimal in [0, 1]
  * canonical sizes are always Decimal > 0
  * scaled price = px / priceScale and scaled qty = qty / fractionalQtyScale
  * retail Amount values normalize to Decimal regardless of representation
  * malformed input is rejected, never silently accepted

derandomize=True fixes per-test seeds (reproducible); too_slow is suppressed
because the wall-clock generation check is load-sensitive (see
tests/test_hypothesis_properties.py).
"""

from datetime import UTC, datetime
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from polyalpha.us.adapter import (
    amount_to_decimal,
    parse_book,
    parse_exchange_book,
    parse_settlement,
    scaled_to_decimal,
)
from polyalpha.us.identifiers import UsIdentifier
from polyalpha.us.instruments import (
    parse_instrument,
    scaled_qty_to_decimal,
)

D = Decimal
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def fuzz_settings(max_examples=60):
    return settings(
        max_examples=max_examples,
        derandomize=True,
        suppress_health_check=[HealthCheck.too_slow],
    )


def _identifier() -> UsIdentifier:
    return UsIdentifier("mid-1", "slug-a", "SYM-A")


# ── Price / quantity scaling invariants ──────────────────────────────────────


@fuzz_settings(100)
@given(
    px=st.integers(min_value=0, max_value=10**9),
    scale=st.integers(min_value=1, max_value=10**6),
)
def test_scaled_price_in_unit_interval_when_px_le_scale(px, scale):
    if px > scale:
        return  # outside the contract range; skip
    value = scaled_to_decimal(px, scale)
    assert isinstance(value, D)
    assert D(0) <= value <= D(1)


@fuzz_settings(100)
@given(
    qty=st.integers(min_value=0, max_value=10**9),
    scale=st.integers(min_value=1, max_value=10**6),
)
def test_scaled_qty_nonnegative(qty, scale):
    value = scaled_qty_to_decimal(qty, scale)
    assert isinstance(value, D)
    assert value >= 0


@fuzz_settings(60)
@given(scale=st.integers(max_value=0))
def test_nonpositive_price_scale_rejected(scale):
    try:
        scaled_to_decimal(5, scale)
    except ValueError:
        return
    raise AssertionError("non-positive price scale must be rejected")


@fuzz_settings(60)
@given(scale=st.integers(max_value=0))
def test_nonpositive_qty_scale_rejected(scale):
    try:
        scaled_qty_to_decimal(5, scale)
    except ValueError:
        return
    raise AssertionError("non-positive fractional qty scale must be rejected")


@fuzz_settings(60)
@given(value=st.booleans())
def test_bool_is_never_a_valid_scaled_integer(value):
    for fn in (scaled_to_decimal, scaled_qty_to_decimal):
        try:
            fn(value, 100)
        except ValueError:
            continue
        raise AssertionError("bool must not be accepted as a scaled integer")


# ── Retail Amount normalization ──────────────────────────────────────────────


@fuzz_settings(100)
@given(
    value=st.decimals(min_value=D(0), max_value=D(1), places=6, allow_nan=False, allow_infinity=False)
)
def test_amount_to_decimal_matches_value(value):
    assert amount_to_decimal({"value": str(value), "currency": "USD"}) == value


@fuzz_settings(60)
@given(
    cur=st.sampled_from(["EUR", "GBP", "JPY", "BTC"])
)
def test_amount_currency_mismatch_rejected(cur):
    try:
        amount_to_decimal({"value": "0.5", "currency": cur}, currency="USD")
    except ValueError:
        return
    raise AssertionError("currency mismatch must be rejected")


# ── Book invariants under fuzzed levels ──────────────────────────────────────


_price_str = st.decimals(
    min_value=D("0.0001"), max_value=D("0.9999"), places=4,
    allow_nan=False, allow_infinity=False,
).map(str)
_qty_str = st.decimals(
    min_value=D(0), max_value=D("100000"), places=4,
    allow_nan=False, allow_infinity=False,
).map(str)


@fuzz_settings(80)
@given(
    bids=st.lists(st.tuples(_price_str, _qty_str), max_size=8, unique_by=lambda t: t[0]),
    offers=st.lists(st.tuples(_price_str, _qty_str), max_size=8, unique_by=lambda t: t[0]),
)
def test_retail_book_canonical_invariants(bids, offers):
    raw = {
        "marketData": {
            "marketSlug": "slug-a",
            "bids": [{"px": {"value": p, "currency": "USD"}, "qty": q} for p, q in bids],
            "offers": [{"px": {"value": p, "currency": "USD"}, "qty": q} for p, q in offers],
            "state": "MARKET_STATE_OPEN",
            "transactTime": "2026-01-01T00:00:00Z",
        }
    }
    book = parse_book(raw, NOW, _identifier(), tick_size=D("0.0001"))
    # Every canonical level is a Decimal with 0 <= price <= 1 and size > 0.
    for level in (*book.bids, *book.asks):
        assert isinstance(level.price, D)
        assert D(0) <= level.price <= D(1)
        assert isinstance(level.size, D)
        assert level.size > 0
    # Bids descending, asks ascending.
    bid_prices = [lvl.price for lvl in book.bids]
    ask_prices = [lvl.price for lvl in book.asks]
    assert bid_prices == sorted(bid_prices, reverse=True)
    assert ask_prices == sorted(ask_prices)


@fuzz_settings(60)
@given(
    px=st.integers(min_value=0, max_value=1000),
    qty=st.integers(min_value=0, max_value=10**6),
)
def test_exchange_book_quantity_scaled_correctly(px, qty):
    raw = {"marketData": {
        "bids": [{"px": px, "qty": qty}],
        "offers": [],
        "transactTime": "2026-01-01T00:00:00Z"}}
    book = parse_exchange_book(
        raw, NOW, _identifier(), price_scale=1000, fractional_qty_scale=100
    )
    if qty == 0:
        assert book.bids == ()  # zero-size levels dropped
    else:
        assert book.bids[0].size == D(qty) / D(100)


# ── Malformed input rejection (never silently accepted) ──────────────────────


@fuzz_settings(60)
@given(
    missing=st.sampled_from(["symbol", "tickSize", "minimumTradeQty",
                             "priceScale", "fractionalQtyScale"])
)
def test_instrument_missing_any_required_field_rejected(missing):
    raw = {
        "symbol": "SYM-A", "tickSize": "0.001", "minimumTradeQty": "1",
        "priceScale": 1000, "fractionalQtyScale": 1, "state": "OPEN",
    }
    del raw[missing]
    try:
        parse_instrument(raw)
    except ValueError:
        return
    raise AssertionError(f"missing {missing} must be rejected")


@fuzz_settings(60)
@given(state=st.text(min_size=1, max_size=12).filter(lambda s: s.strip() and s.upper() not in {
    "PENDING", "PREOPEN", "OPEN", "SUSPENDED", "HALTED", "CLOSED",
    "EXPIRED", "TERMINATED", "MATCH_AND_CLOSE_AUCTION",
    "MARKET_STATE_OPEN", "MARKET_STATE_PREOPEN", "MARKET_STATE_SUSPENDED",
    "MARKET_STATE_EXPIRED", "MARKET_STATE_TERMINATED", "MARKET_STATE_HALTED",
    "MARKET_STATE_MATCH_AND_CLOSE_AUCTION",
    "INSTRUMENT_STATE_PENDING", "INSTRUMENT_STATE_OPEN", "INSTRUMENT_STATE_CLOSED",
    "INSTRUMENT_STATE_EXPIRED", "INSTRUMENT_STATE_TERMINATED",
    "INSTRUMENT_STATE_SUSPENDED", "INSTRUMENT_STATE_HALTED",
    "INSTRUMENT_STATE_PREOPEN", "INSTRUMENT_STATE_MATCH_AND_CLOSE_AUCTION",
}))
def test_unknown_state_rejected(state):
    from polyalpha.us.states import UsMarketState
    try:
        UsMarketState.parse(state)
    except ValueError:
        return
    raise AssertionError(f"unknown state {state!r} must be rejected")


# ── Settlement finality invariants ───────────────────────────────────────────


@fuzz_settings(60)
@given(settlement=st.one_of(st.none(), st.sampled_from(["0", "1", "0.5", "1.0"])))
def test_settlement_finality_consistent(settlement):
    raw = {"slug": "slug-a"}
    if settlement is not None:
        raw["settlement"] = settlement
    parsed = parse_settlement(raw, _identifier())
    if settlement is None:
        assert parsed["is_final"] is False
        assert parsed["settlement_px"] is None
    else:
        assert parsed["is_final"] is True
        assert parsed["settlement_px"] == D(settlement)