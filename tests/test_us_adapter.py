"""Tests for the Polymarket US adapter layer.

Covers: identifier registry (3-way mapping), US market-state model and
predicates, retail parsers (market/book/bbo/events/settlement/price-history),
price normalization (retail Amount + exchange scaled int), the price-history
spread warning, and the US phase state machine. All offline, using synthetic
payloads matching the documented Polymarket US schemas.
"""

import sys
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

sys.path.insert(0, "src")

from polyalpha.us.adapter import (  # noqa: E402
    amount_to_decimal,
    parse_bbo,
    parse_book,
    parse_events,
    parse_market,
    parse_price_history,
    parse_settlement,
    scaled_to_decimal,
)
from polyalpha.us.collector import UsRawCollector  # noqa: E402
from polyalpha.us.identifiers import UsIdentifierRegistry  # noqa: E402
from polyalpha.us.rest import PublicUsClient  # noqa: E402
from polyalpha.us.states import UsMarketState  # noqa: E402

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def _registry() -> UsIdentifierRegistry:
    reg = UsIdentifierRegistry()
    reg.register("mid-1001", "chiefs-super-bowl-lx", "SYMBOL_1001")
    return reg


def _market_raw() -> dict:
    return {
        "id": "1001",
        "slug": "chiefs-super-bowl-lx",
        "question": "Chiefs win Super Bowl LX?",
        "title": "Chiefs win Super Bowl LX?",
        "category": "sports",
        "endDate": "2027-02-14T00:00:00Z",
        "state": "OPEN",
        "marketSides": [
            {"id": "side-long", "long": True, "price": "0.555"},
            {"id": "side-short", "long": False, "price": "0.455"},
        ],
        "liquidity": {"value": "50000", "currency": "USD"},
        "volume": {"value": "100000", "currency": "USD"},
    }


def _book_raw() -> dict:
    return {
        "marketData": {
            "marketSlug": "chiefs-super-bowl-lx",
            "bids": [
                {"px": {"value": "0.550", "currency": "USD"}, "qty": "2.50"},
                {"px": {"value": "0.545", "currency": "USD"}, "qty": "1.50"},
            ],
            "offers": [
                {"px": {"value": "0.560", "currency": "USD"}, "qty": "0.80"},
                {"px": {"value": "0.565", "currency": "USD"}, "qty": "1.20"},
            ],
            "state": "MARKET_STATE_OPEN",
            "stats": {"lastTradePx": {"value": "0.55", "currency": "USD"}},
            "transactTime": "2026-09-15T12:00:00Z",
        }
    }


def _events_raw() -> dict:
    return {
        "events": [
            {
                "id": "7001",
                "slug": "chiefs-super-bowl-lx",
                "markets": [_market_raw()],
            }
        ]
    }


# ══════════════════════════════════════════════════════════════════════════
# IDENTIFIER REGISTRY (3-way mapping)
# ══════════════════════════════════════════════════════════════════════════


class TestUsIdentifiers:
    def test_three_way_mapping(self):
        reg = UsIdentifierRegistry()
        reg.register("mid-1", "slug-a", "SYMB_A")
        assert reg.by_internal("mid-1").us_market_slug == "slug-a"
        assert reg.by_slug("slug-a").internal_market_id == "mid-1"
        assert reg.by_symbol("SYMB_A").us_market_slug == "slug-a"
        assert len(reg) == 1

    def test_side_ids_are_venue_neutral(self):
        reg = _registry()
        identifier = reg.by_slug("chiefs-super-bowl-lx")
        assert identifier.long_side_id == "mid-1001:LONG"
        assert identifier.short_side_id == "mid-1001:SHORT"
        assert identifier.condition_id == "us:mid-1001"

    def test_duplicate_rejected(self):
        reg = UsIdentifierRegistry()
        reg.register("a", "slug-a")
        with pytest.raises(ValueError, match="duplicate"):
            reg.register("b", "slug-a")


# ══════════════════════════════════════════════════════════════════════════
# MARKET STATE MODEL
# ══════════════════════════════════════════════════════════════════════════


class TestUsStates:
    def test_parse_both_spellings(self):
        assert UsMarketState.parse("MARKET_STATE_OPEN") == UsMarketState.OPEN
        assert UsMarketState.parse("OPEN") == UsMarketState.OPEN
        assert UsMarketState.parse("MARKET_STATE_MATCH_AND_CLOSE_AUCTION") == (
            UsMarketState.MATCH_AND_CLOSE_AUCTION
        )

    def test_predicates(self):
        assert UsMarketState.OPEN.is_tradable()
        assert UsMarketState.OPEN.accepts_orders()
        assert UsMarketState.MATCH_AND_CLOSE_AUCTION.is_tradable()
        assert not UsMarketState.SUSPENDED.is_tradable()
        assert UsMarketState.SUSPENDED.is_trading_paused()
        assert UsMarketState.HALTED.is_trading_paused()
        assert UsMarketState.EXPIRED.is_resolved()
        assert UsMarketState.TERMINATED.is_resolved()
        assert UsMarketState.CLOSED.is_closed()
        assert UsMarketState.PREOPEN.is_pre_trade()
        assert UsMarketState.PENDING.is_pre_trade()
        assert UsMarketState.OPEN.is_active()
        assert not UsMarketState.TERMINATED.is_active()

    def test_unknown_state_rejected(self):
        with pytest.raises(ValueError, match="unknown"):
            UsMarketState.parse("BOGUS")


# ══════════════════════════════════════════════════════════════════════════
# PRICE NORMALIZATION
# ══════════════════════════════════════════════════════════════════════════


class TestPriceNormalization:
    def test_retail_amount(self):
        assert amount_to_decimal({"value": "0.555", "currency": "USD"}) == D("0.555")

    def test_retail_amount_currency_mismatch(self):
        with pytest.raises(ValueError, match="currency"):
            amount_to_decimal({"value": "0.5", "currency": "EUR"}, currency="USD")

    def test_scaled_int(self):
        assert scaled_to_decimal(650, 1000) == D("0.650")
        assert scaled_to_decimal(555, 1000) == D("0.555")

    def test_scaled_int_requires_integer(self):
        with pytest.raises(ValueError, match="integer"):
            scaled_to_decimal("0.555", 1000)

    def test_scaled_int_bad_scale(self):
        with pytest.raises(ValueError, match="price_scale"):
            scaled_to_decimal(650, 0)


# ══════════════════════════════════════════════════════════════════════════
# PARSERS → CANONICAL DOMAIN
# ══════════════════════════════════════════════════════════════════════════


class TestUsAdapters:
    def test_parse_market(self):
        reg = _registry()
        market = parse_market(_market_raw(), NOW, reg.by_slug("chiefs-super-bowl-lx"))
        assert market.market_id == "mid-1001"
        assert market.yes_token_id == "mid-1001:LONG"
        assert market.no_token_id == "mid-1001:SHORT"
        assert market.active is True
        assert market.closed is False
        assert market.accepting_orders is True
        assert market.liquidity == D("50000")
        assert market.volume == D("100000")
        assert market.category == "sports"

    def test_parse_market_suspended_not_active(self):
        reg = _registry()
        raw = _market_raw()
        raw["state"] = "MARKET_STATE_SUSPENDED"
        market = parse_market(raw, NOW, reg.by_slug("chiefs-super-bowl-lx"))
        assert market.active is False
        assert market.accepting_orders is False

    def test_parse_book(self):
        reg = _registry()
        identifier = reg.by_slug("chiefs-super-bowl-lx")
        book = parse_book(_book_raw(), NOW, identifier)
        assert book.token_id == "mid-1001:LONG"
        assert book.condition_id == "us:mid-1001"
        assert book.best_bid == D("0.550")
        assert book.best_ask == D("0.560")
        assert book.spread == D("0.010")
        assert book.bids[0].size == D("2.50")
        assert len(book.asks) == 2

    def test_parse_book_zero_qty_skipped(self):
        reg = _registry()
        raw = _book_raw()
        raw["marketData"]["bids"].append(
            {"px": {"value": "0.540", "currency": "USD"}, "qty": "0"}
        )
        identifier = reg.by_slug("chiefs-super-bowl-lx")
        book = parse_book(raw, NOW, identifier)
        assert len(book.bids) == 2  # zero-qty level dropped

    def test_parse_events(self):
        reg = _registry()
        markets = parse_events(_events_raw(), NOW, reg)
        assert len(markets) == 1
        assert markets[0].question == "Chiefs win Super Bowl LX?"

    def test_parse_bbo(self):
        reg = _registry()
        raw = {
            "marketData": {
                "marketSlug": "chiefs-super-bowl-lx",
                "bestBid": {"value": "0.54", "currency": "USD"},
                "bestAsk": {"value": "0.56", "currency": "USD"},
                "currentPx": {"value": "0.55", "currency": "USD"},
                "longQuote": {"value": "0.555", "currency": "USD"},
                "shortQuote": {"value": "0.455", "currency": "USD"},
                "bidDepth": 5,
                "askDepth": 4,
                "sharesTraded": "150000",
                "openInterest": "500000",
            }
        }
        bbo = parse_bbo(raw, reg.by_slug("chiefs-super-bowl-lx"))
        assert bbo["best_bid"] == D("0.54")
        assert bbo["best_ask"] == D("0.56")
        assert bbo["bid_depth"] == 5

    def test_parse_settlement_finality(self):
        reg = _registry()
        raw = {
            "marketData": {
                "stats": {
                    "settlementPx": {"value": "0.55", "currency": "USD"},
                    "settlementPreliminaryFlag": True,
                }
            }
        }
        settlement = parse_settlement(raw, reg.by_slug("chiefs-super-bowl-lx"))
        assert settlement["is_final"] is False
        assert settlement["settlement_preliminary"] is True
        # Final settlement must flip the flag.
        raw["marketData"]["stats"]["settlementPreliminaryFlag"] = False
        final = parse_settlement(raw, reg.by_slug("chiefs-super-bowl-lx"))
        assert final["is_final"] is True

    def test_price_history_preserves_spread(self):
        """longPrice + shortPrice can sum > 1 — this is NOT a trade tape."""
        raw = {
            "history": [
                {"timestamp": 1700000000, "longPrice": 0.555, "shortPrice": 0.455},
                {"timestamp": 1700000060, "longPrice": 0.560, "shortPrice": 0.450},
            ]
        }
        points = parse_price_history(raw)
        assert points[0].long_price == D("0.555")
        assert points[0].short_price == D("0.455")
        # Second point: 0.560 + 0.450 = 1.010 > 1 (spread preserved)
        assert points[1].sum_prices > D("1")


# ══════════════════════════════════════════════════════════════════════════
# US RAW COLLECTOR (offline, synthetic)
# ══════════════════════════════════════════════════════════════════════════


class _FakeClient:
    def __init__(self, markets=None, book=None, events=None):
        self._markets = markets or {"markets": [_market_raw()]}
        self._book = book or _book_raw()
        self._events = events or _events_raw()
        self.calls = []

    def markets(self, params=None):
        self.calls.append(("markets", params))
        return self._markets, NOW

    def market_book(self, slug):
        self.calls.append(("book", slug))
        return self._book, NOW

    def market_bbo(self, slug):
        self.calls.append(("bbo", slug))
        return {"marketData": {}}, NOW

    def events(self, params=None):
        self.calls.append(("events", params))
        return self._events, NOW


class TestUsRawCollector:
    def test_discover_and_book(self, tmp_path):
        from polyalpha.rawstore import RawStore

        client = _FakeClient()
        reg = _registry()
        with RawStore(tmp_path / "us" / "retail" / "raw", collector_version="test") as raw:
            collector = UsRawCollector(client, raw, reg, collector_version="test")
            markets = collector.discover_markets()
            assert len(markets) == 1
            collector.collect_books(["chiefs-super-bowl-lx"])
            assert collector.stats.books == 1
            assert raw.count() == 2  # markets page + book
            # Source labels are US-specific.
            sources = {r.source for r in raw.replay()}
            assert "polymarket_us_retail_markets" in sources
            assert "polymarket_us_retail_book" in sources

    def test_collector_uses_venue_specific_lineage(self, tmp_path):
        from polyalpha.rawstore import RawStore

        client = _FakeClient()
        reg = _registry()
        with RawStore(tmp_path / "us" / "retail" / "raw", collector_version="test") as raw:
            collector = UsRawCollector(client, raw, reg)
            collector.discover_markets()
            # The raw store day-dir is under data/us/... not data/raw/
            assert (tmp_path / "us" / "retail" / "raw").exists()


# ══════════════════════════════════════════════════════════════════════════
# US PUBLIC REST CLIENT (offline)
# ══════════════════════════════════════════════════════════════════════════


class TestUsRestClient:
    def test_client_uses_gateway_and_paces(self, monkeypatch):
        import json as _json

        from polyalpha.us import rest as _rest

        calls = []

        class _Response:
            def read(self):
                return _json.dumps({"ok": True}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(request, timeout):
            calls.append(request.full_url)
            return _Response()

        monkeypatch.setattr(_rest, "urlopen", fake_urlopen)
        client = PublicUsClient(min_spacing_seconds=0.0001)
        payload, _ = client.markets({"limit": 5})
        assert payload == {"ok": True}
        assert any("/v1/markets" in url for url in calls)
        assert any("limit=5" in url for url in calls)
        assert any("gateway.polymarket.us" in url for url in calls)

    def test_429_raises_after_attempts(self, monkeypatch):
        from urllib.error import HTTPError

        from polyalpha.us import rest as _rest

        attempts = [0]

        def fake_urlopen(request, timeout):
            attempts[0] += 1
            raise HTTPError(request.full_url, 429, "Too Many Requests", {}, None)

        monkeypatch.setattr(_rest, "urlopen", fake_urlopen)
        client = PublicUsClient(attempts=3, min_spacing_seconds=0.0001)
        from polyalpha.us.rest import UsRateLimited
        with pytest.raises(UsRateLimited):
            client.markets({})
        assert attempts[0] == 3


# ══════════════════════════════════════════════════════════════════════════
# US PHASE MACHINE
# ══════════════════════════════════════════════════════════════════════════


class TestUsPhases:
    def test_us_phase_sequence(self, tmp_path):
        from polyalpha.phases import UsPhaseStore, initialize_us_phase

        path = tmp_path / "us_phase.json"
        state = initialize_us_phase(path, "us-baseline-abc")
        assert state.phase == "US_BASELINE_FROZEN"
        store = UsPhaseStore(path, "us-baseline-abc")
        store.transition("US_BURNIN_RUNNING")
        store.transition("US_BURNIN_PASSED")
        store.transition("REAL_DATA_START_US")
        store.transition("US_COLLECTION_RUNNING")
        assert store.read().phase == "US_COLLECTION_RUNNING"
        assert len(store.read().transition_log) == 5

    def test_us_phase_rejects_foreign_phase(self, tmp_path):
        from polyalpha.phases import UsPhaseStore, initialize_us_phase

        path = tmp_path / "us_phase.json"
        initialize_us_phase(path, "b")
        store = UsPhaseStore(path, "b")
        # US phases must not collide with International phases.
        with pytest.raises(ValueError, match="invalid phase"):
            store.transition("BURNIN_RUNNING")
        with pytest.raises(ValueError, match="invalid transition"):
            store.transition("US_BURNIN_PASSED")  # can't skip to passed

    def test_us_phase_isolation_from_international(self, tmp_path):
        """US phase file is a separate lineage; International is untouched."""
        from polyalpha.phases import (
            PhaseStore,
            UsPhaseStore,
            initialize_phase,
            initialize_us_phase,
        )

        us_path = tmp_path / "us_phase.json"
        intl_path = tmp_path / "intl_phase.json"
        initialize_us_phase(us_path, "us-b")
        initialize_phase(intl_path, "intl-b")
        store = PhaseStore(intl_path, "intl-b")
        store.transition("BURNIN_RUNNING")
        assert store.read().phase == "BURNIN_RUNNING"
        assert UsPhaseStore(us_path, "us-b").read().phase == "US_BASELINE_FROZEN"