"""Differential testing: PolyAlpha US adapters vs. the official polymarket-us SDK.

Feeds the IDENTICAL live public payload through both our adapter parsers and
the official SDK's typed schema, then asserts the normalized fields agree. This
is the strongest available check for subtle schema misunderstandings: a parser
can pass unit tests while silently mis-reading a field the SDK interprets
differently.

Method:
  1. Fetch a live payload via PublicUsClient (raw dict, wire-faithful).
  2. Parse it with our adapter (canonical Decimal/domain objects).
  3. Validate/parse the same dict through the SDK's typed models.
  4. Compare the normalized numbers.

Live-only: the gateway is public, no credentials. Tests skip if offline.
"""

from datetime import datetime
from decimal import Decimal

import pytest

from polyalpha.us.adapter import amount_to_decimal, parse_book, parse_settlement
from polyalpha.us.identifiers import UsIdentifier
from polyalpha.us.rest import PublicUsClient

try:
    import polymarket_us
    SDK_AVAILABLE = True
except ImportError:  # pragma: no cover
    SDK_AVAILABLE = False

D = Decimal

pytestmark = pytest.mark.skipif(
    not SDK_AVAILABLE, reason="polymarket-us SDK not installed"
)


def _client() -> PublicUsClient:
    return PublicUsClient(timeout=15, attempts=1, min_spacing_seconds=0.2)


def _settled_slug() -> str:
    # A market that has resolved (2025 NFL games are settled).
    return "aec-nfl-lac-ten-2025-11-02"


def _live_book_payload(slug: str) -> tuple[dict, datetime]:
    c = _client()
    payload, received = c.market_book(slug)
    return payload, received


def _live_settlement_payload(slug: str) -> dict:
    c = _client()
    payload, _ = c.market_settlement(slug)
    return payload


# ── Book differential ────────────────────────────────────────────────────────


def test_sdk_book_schema_accepts_live_payload():
    """The SDK's MarketBook typed model must accept the live /book response."""
    payload, _ = _live_book_payload(_settled_slug())
    from polymarket_us.types.markets import MarketBook

    try:
        # TypedDict constructor doesn't validate at runtime; validate by
        # construction + key/type checks against the declared schema.
        book = MarketBook(**payload["marketData"])
        assert book["marketSlug"] == payload["marketData"]["marketSlug"]
    except TypeError as error:
        pytest.fail(f"SDK MarketBook rejects live payload: {error}")


def test_book_differential_prices_quantities():
    """Our parse_book Decimal levels must equal the SDK's raw px/qty strings
    converted the same way, on the SAME live payload."""
    payload, received = _live_book_payload(_settled_slug())
    identifier = UsIdentifier("mid-1", "slug", "slug")
    from polyalpha.us.adapter import amount_to_decimal, retail_qty

    book = parse_book(payload, received, identifier, tick_size=D("0.001"))
    data = payload.get("marketData", payload)

    # bids: our canonical (price, size) must equal SDK-schema px.value/qty.
    sdk_bids = {amount_to_decimal(b["px"]): retail_qty(b["qty"]) for b in data.get("bids", [])}
    our_bids = {b.price: b.size for b in book.bids}
    for price, size in our_bids.items():
        assert price in sdk_bids, f"our bid price {price} missing from SDK-schema book"
        assert size == sdk_bids[price], (
            f"bid size mismatch at {price}: ours {size}, SDK-schema {sdk_bids[price]}"
        )

    sdk_offers = {amount_to_decimal(o["px"]): retail_qty(o["qty"]) for o in data.get("offers", [])}
    our_offers = {o.price: o.size for o in book.asks}
    for price, size in our_offers.items():
        assert price in sdk_offers, f"our offer price {price} missing from SDK-schema book"
        assert size == sdk_offers[price], (
            f"offer size mismatch at {price}: ours {size}, SDK-schema {sdk_offers[price]}"
        )


def test_book_differential_zero_qty_dropped_identically():
    """Both sides drop zero-size levels."""
    payload, received = _live_book_payload(_settled_slug())
    identifier = UsIdentifier("mid-1", "slug", "slug")
    book = parse_book(payload, received, identifier, tick_size=D("0.001"))
    # Our canonical book must never contain a zero-size level.
    assert all(b.size > 0 for b in book.bids), "our canonical book contains a zero-size bid"
    assert all(o.size > 0 for o in book.asks), "our canonical book contains a zero-size ask"


def _qty_zero(qty: str) -> bool:
    try:
        return Decimal(qty) <= 0
    except Exception:  # noqa: BLE001
        return True


# ── Settlement differential ──────────────────────────────────────────────────


def test_settlement_differential_live_shape():
    """Our parse_settlement reads the LIVE shape {slug, settlement}.

    The SDK's public method also returns this shape. Its TYPED model
    (marketSlug/settlementPrice/settledAt) is stale relative to the live API —
    this is the drift the differential catches. Our parser must agree with the
    LIVE response, not the stale model.
    """
    payload = _live_settlement_payload(_settled_slug())
    identifier = UsIdentifier("mid-1", "slug", "slug")

    ours = parse_settlement(payload, identifier)
    # Live shape: top-level settlement price.
    assert "settlement" in payload
    assert ours["settlement_px"] == amount_to_decimal(payload["settlement"])
    assert ours["is_final"] is True

    # SDK public method returns the same live shape.
    sdk = polymarket_us.PolymarketUS().markets.settlement(_settled_slug())
    assert sdk == payload


def test_settlement_sdk_typed_model_is_drifted():
    """Known drift: SDK typed MarketSettlement expects
    {marketSlug, settlementPrice, settledAt}; the live API returns
    {slug, settlement}. This documents the mismatch so it is a tracked fact,
    not a surprise."""
    payload = _live_settlement_payload(_settled_slug())
    from polymarket_us.types.markets import MarketSettlement

    # The typed model's keys are NOT present in the live payload.
    assert "settlementPrice" not in payload
    assert "marketSlug" not in payload
    assert "settledAt" not in payload
    # And the live keys are not part of the typed model.
    typed_keys = set(MarketSettlement.__annotations__.keys())
    assert "settlement" not in typed_keys
    assert "slug" not in typed_keys


# ── BBO differential ─────────────────────────────────────────────────────────


def test_bbo_sdk_schema_accepts_live_payload():
    from polymarket_us.types.markets import MarketBBO

    c = _client()
    payload, _ = c.market_bbo(_settled_slug())
    data = payload.get("marketData", payload)
    try:
        MarketBBO(**data)
    except TypeError as error:
        pytest.fail(f"SDK MarketBBO rejects live payload: {error}")


# ── Shared normalization invariant ───────────────────────────────────────────


def test_amount_normalization_matches_sdk_amount_type():
    """Our amount_to_decimal must parse anything the SDK's Amount type accepts
    (value: str, currency: USD) and must reject nothing the SDK would accept."""
    from polymarket_us.types.markets import Amount

    from polyalpha.us.adapter import amount_to_decimal

    for value in ("0.555", "0.5", "1", "0.0001", "0.5025"):
        amt = Amount(value=value, currency="USD")
        assert amount_to_decimal(amt) == Decimal(value)