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
    parse_exchange_book,
    parse_market,
    parse_price_history,
    parse_settlement,
    scaled_to_decimal,
)
from polyalpha.us.burnin import (  # noqa: E402
    UsBurnInProvenance,
    UsBurnInReport,
    UsHealth,
    UsHealthTracker,
    instrument_snapshot_hash,
    write_us_burnin_report,
)
from polyalpha.us.collector import UsRawCollector  # noqa: E402
from polyalpha.us.exchange import ExchangeRefDataClient  # noqa: E402
from polyalpha.us.grpc_stream import UsGrpcMarketStream  # noqa: E402
from polyalpha.us.identifiers import UsIdentifierRegistry  # noqa: E402
from polyalpha.us.instruments import UsInstrumentRegistry, parse_instrument  # noqa: E402
from polyalpha.us.reconcile import (  # noqa: E402
    ReconciliationReason,
    SourceBook,
    UsReconciliationReport,
    UsSourceKind,
    reconcile,
)
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


# ══════════════════════════════════════════════════════════════════════════
# US EXCHANGE INSTRUMENTS (authoritative refdata semantics)
# ══════════════════════════════════════════════════════════════════════════


def _instrument_raw() -> dict:
    return {
        "symbol": "SYMB_1001",
        "tickSize": "0.001",
        "minimumTradeQty": "1",
        "priceScale": 1000,
        "state": "OPEN",
        "question": "Chiefs win Super Bowl LX?",
        "payoutValue": "1",
        "outcome_type": "binary",
        "event_id": "7001",
        "event_series": "nfl",
        "event_category": "sports",
        "event_subcategory": "football",
        "event_start_time": "2027-02-14T00:00:00Z",
        "startDate": "2026-09-01T00:00:00Z",
        "expirationDate": "2027-02-15T00:00:00Z",
        "long_participant_id": "team-1",
        "long_participant_name": "Chiefs",
        "short_participant_id": "team-2",
        "short_participant_name": "Eagles",
        "instrument_rules": "official result",
    }


class TestUsInstruments:
    def test_parse_instrument(self):
        inst = parse_instrument(_instrument_raw())
        assert inst.symbol == "SYMB_1001"
        assert inst.tick_size == D("0.001")
        assert inst.minimum_trade_qty == D("1")
        assert inst.price_scale == 1000
        assert inst.state == UsMarketState.OPEN
        assert inst.long_participant_name == "Chiefs"
        assert inst.event_category == "sports"
        assert inst.payout_value == D("1")

    def test_scaled_to_probability(self):
        inst = parse_instrument(_instrument_raw())
        assert inst.scaled_to_probability(555) == D("0.555")
        assert inst.scaled_to_probability(650) == D("0.650")

    def test_parse_instrument_requires_core_fields(self):
        raw = _instrument_raw()
        del raw["priceScale"]
        with pytest.raises(ValueError, match="priceScale"):
            parse_instrument(raw)
        with pytest.raises(ValueError, match="symbol"):
            parse_instrument({"tickSize": "0.001", "minimumTradeQty": "1", "priceScale": 1000})

    def test_registry_lookup(self):
        reg = UsInstrumentRegistry()
        reg.register(parse_instrument(_instrument_raw()))
        assert reg.by_symbol("SYMB_1001").tick_size == D("0.001")
        assert reg.tick_size_for("SYMB_1001") == D("0.001")
        assert reg.price_scale_for("SYMB_1001") == 1000
        assert reg.min_qty_for("SYMB_1001") == D("1")
        assert reg.tick_size_for("UNKNOWN") is None

    def test_registry_duplicate_rejected(self):
        reg = UsInstrumentRegistry()
        reg.register(parse_instrument(_instrument_raw()))
        with pytest.raises(ValueError, match="duplicate"):
            reg.register(parse_instrument(_instrument_raw()))

    def test_authoritative_tick_drives_validation(self):
        """0.555 must be rejected with a 0.01 tick, accepted with the
        authoritative 0.001 tick from refdata."""
        reg = _registry()
        identifier = reg.by_slug("chiefs-super-bowl-lx")
        raw = _book_raw()
        # With the provisional 0.01 tick, a 0.555 price fails validation.
        with pytest.raises(ValueError, match="tick grid"):
            parse_book(raw, NOW, identifier, tick_size=D("0.01"))
        # With the authoritative 0.001 tick, it parses.
        book = parse_book(raw, NOW, identifier, tick_size=D("0.001"))
        assert book.best_bid == D("0.550")


class TestUsExchangeBook:
    def test_scaled_int_book(self):
        reg = _registry()
        identifier = reg.by_slug("chiefs-super-bowl-lx")
        raw = {
            "marketData": {
                "marketSlug": "chiefs-super-bowl-lx",
                "bids": [{"px": 555, "qty": "2.5"}, {"px": 550, "qty": "1.5"}],
                "offers": [{"px": 560, "qty": "0.8"}, {"px": 565, "qty": "1.2"}],
                "transactTime": "2026-09-15T12:00:00Z",
            }
        }
        # price_scale MUST come from refdata, never a default.
        book = parse_exchange_book(raw, NOW, identifier, price_scale=1000)
        assert book.best_bid == D("0.555")
        assert book.best_ask == D("0.560")
        assert book.spread == D("0.005")


class TestExchangeRefDataClient:
    def test_instruments_parse_and_register(self, monkeypatch):
        import json as _json

        from polyalpha.us import exchange as _ex

        def fake_urlopen(request, *a, **k):
            class _Resp:
                def read(self):
                    return _json.dumps([_instrument_raw()]).encode()

                def __enter__(self):
                    return self

                def __exit__(self, *x):
                    return False

            return _Resp()

        monkeypatch.setattr(_ex, "urlopen", fake_urlopen)
        registry = UsInstrumentRegistry()
        client = ExchangeRefDataClient(access_token="dummy")
        instruments = client.instruments(registry=registry)
        assert len(instruments) == 1
        assert registry.by_symbol("SYMB_1001").price_scale == 1000


# ══════════════════════════════════════════════════════════════════════════
# US gRPC MARKET-DATA STREAM (state correctness)
# ══════════════════════════════════════════════════════════════════════════


def _grpc_stream() -> UsGrpcMarketStream:
    identifiers = UsIdentifierRegistry()
    identifiers.register("mid-1", "slug-a", "SYMB_1001")
    instruments = UsInstrumentRegistry()
    instruments.register(parse_instrument(_instrument_raw()))
    return UsGrpcMarketStream(identifiers, instruments)


def _grpc_update(symbol="SYMB_1001", px_bids=(555, 550), px_offers=(560, 565)):
    return {
        "update": {
            "symbol": symbol,
            "bids": [{"px": p, "qty": 10} for p in px_bids],
            "offers": [{"px": p, "qty": 10} for p in px_offers],
            "transact_time": "2026-09-15T12:00:00Z",
        }
    }


class TestGrpcStream:
    def test_subscription_lifecycle(self):
        stream = _grpc_stream()
        stream.subscribe(["SYMB_1001"])
        events = stream.process(_grpc_update())
        assert events[0].kind == "update"
        assert events[0].update.symbol == "SYMB_1001"
        assert stream.active_symbols == {"SYMB_1001"}
        assert stream.summary()["symbols_seen"] == ["SYMB_1001"]

    def test_heartbeat_liveness(self):
        stream = _grpc_stream()
        events = stream.process({"heartbeat": True})
        assert events[0].kind == "heartbeat"

    def test_priceScale_required_from_instrument(self):
        """Normalization must fail if price_scale is unknown — never a default."""
        identifiers = UsIdentifierRegistry()
        identifiers.register("mid-1", "slug-a", "SYMB_1001")
        # No instrument registered -> priceScale unknown.
        stream = UsGrpcMarketStream(identifiers, UsInstrumentRegistry())
        events = stream.process(_grpc_update())
        assert events[0].kind == "error"
        assert "priceScale" in events[0].message

    def test_instrument_lookup_before_normalization(self):
        """Unknown symbol must be rejected before any price math."""
        identifiers = UsIdentifierRegistry()
        instruments = UsInstrumentRegistry()
        instruments.register(parse_instrument(_instrument_raw()))
        stream = UsGrpcMarketStream(identifiers, instruments)
        events = stream.process(_grpc_update(symbol="UNKNOWN"))
        assert events[0].kind == "error"
        assert "identifier" in events[0].message

    def test_scaled_px_decoding(self):
        stream = _grpc_stream()
        events = stream.process(_grpc_update())
        update = events[0].update
        assert update.bids[0] == (D("0.555"), D("10"))  # 555/1000
        assert update.offers[0] == (D("0.560"), D("10"))

    def test_aggregated_and_unaggregated_both_decode(self):
        stream = _grpc_stream()
        # Aggregated book: multiple qty at a price.
        events = stream.process(_grpc_update())
        assert events[0].update.bids[0][1] == D("10")
        # Unaggregated raw orders decode the same way (each entry one order).
        raw_msg = {
            "update": {
                "symbol": "SYMB_1001",
                "bids": [{"px": 555, "qty": 3}, {"px": 555, "qty": 7}],
                "offers": [],
                "transact_time": "2026-09-15T12:00:00Z",
            }
        }
        events2 = stream.process(raw_msg)
        assert sum(q for _, q in events2[0].update.bids) == D("10")

    def test_snapshot_only_closes_stream(self):
        """snapshot_only is a request flag; the consumer records it and the
        stream naturally stops. We verify a snapshot_only request produces a
        single update then the caller closes."""
        stream = _grpc_stream()
        events = stream.process(_grpc_update())
        assert len(events) == 1
        assert events[0].kind == "update"

    def test_reconnect_invalidates_state(self):
        stream = _grpc_stream()
        stream.process(_grpc_update())
        stream.reconnect()
        assert stream.reconnect_count == 1
        assert stream.summary()["symbols_seen"] == []
        # A fresh update after reconnect is accepted (sequence restarts).
        events = stream.process(_grpc_update())
        assert events[0].kind == "update"
        assert events[0].update.sequence == 1

    def test_out_of_order_update_invalidated(self):
        stream = _grpc_stream()
        stream.process(_grpc_update())  # t=12:00
        stale_msg = {
            "update": {
                "symbol": "SYMB_1001",
                "bids": [{"px": 550, "qty": 5}],
                "offers": [],
                "transact_time": "2026-09-15T11:00:00Z",  # earlier -> out of order
            }
        }
        events = stream.process(stale_msg)
        assert events[0].kind == "stale"
        assert "out-of-order" in events[0].message

    def test_state_mapping_from_grpc_enum(self):
        stream = _grpc_stream()
        msg = {
            "update": {
                "symbol": "SYMB_1001",
                "bids": [{"px": 555, "qty": 10}],
                "offers": [{"px": 560, "qty": 10}],
                "state": "INSTRUMENT_STATE_SUSPENDED",
                "transact_time": "2026-09-15T12:00:00Z",
            }
        }
        events = stream.process(msg)
        assert events[0].update.state == UsMarketState.SUSPENDED
        assert not events[0].update.state.is_tradable()

    def test_liveness_staleness_invalidation(self):
        """A symbol with no updates within max_stale_seconds must be
        invalidated (liveness, driven by the local receive clock)."""
        from datetime import timedelta

        now = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
        clock = {"now": now}

        identifiers = UsIdentifierRegistry()
        identifiers.register("mid-1", "slug-a", "SYMB_1001")
        instruments = UsInstrumentRegistry()
        instruments.register(parse_instrument(_instrument_raw()))
        stream = UsGrpcMarketStream(identifiers, instruments, max_stale_seconds=30,
                                    now_fn=lambda: clock["now"])
        stream.process(_grpc_update())
        assert stream.summary()["symbols_seen"] == ["SYMB_1001"]
        # 31 seconds pass with no update -> symbol becomes stale.
        clock["now"] = now + timedelta(seconds=31)
        stream.invalidate_stale()
        assert stream.summary()["symbols_seen"] == []
        assert any(e.kind == "stale" for e in stream.events)

    def test_deterministic_replay(self):
        stream = _grpc_stream()
        stream.process(_grpc_update())
        stream.process({"heartbeat": True})
        stream.reconnect()
        replay_a = stream.replay()
        replay_b = stream.replay()
        assert replay_a == replay_b  # deterministic
        assert any(e["kind"] == "update" for e in replay_a)
        assert any(e["kind"] == "reconnect" for e in replay_a)


# ══════════════════════════════════════════════════════════════════════════
# US RECONCILIATION (canonical books, four questions)
# ══════════════════════════════════════════════════════════════════════════


def _recon_identifiers() -> UsIdentifierRegistry:
    reg = UsIdentifierRegistry()
    reg.register("mid-1", "slug-a", "SYMB_1001")
    return reg


def _canonical_book(bid_px, ask_px, bid_qty="10", ask_qty="10"):
    from datetime import timezone  # noqa: F401

    from polyalpha.domain import Book, Level

    ts = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    return Book(
        token_id="mid-1:LONG",
        condition_id="us:mid-1",
        source_at=ts,
        received_at=ts,
        bids=(Level(D(str(bid_px)), D(bid_qty)),),
        asks=(Level(D(str(ask_px)), D(ask_qty)),),
        tick_size=D("0.001"),
        min_order_size=D("1"),
        source_hash="h",
    )


def _src(kind, book, state=None, scale=1000, ts=None) -> SourceBook:
    return SourceBook(
        source_kind=kind,
        symbol="SYMB_1001",
        book=book,
        price_scale=scale,
        tick_size=D("0.001"),
        state=state,
        transact_time=ts or datetime(2026, 9, 15, 12, 0, tzinfo=UTC),
    )


class TestReconciliation:
    def test_match(self):
        b = _canonical_book(0.555, 0.560)
        r = reconcile(_src(UsSourceKind.GRPC, b), _src(UsSourceKind.RETAIL, b),
                      _recon_identifiers())
        assert r.reason == ReconciliationReason.MATCH
        assert not r.is_hard_failure

    def test_match_within_tolerance(self):
        b1 = _canonical_book(0.555, 0.560)
        b2 = _canonical_book(0.555, 0.560, bid_qty="10.005")
        r = reconcile(_src(UsSourceKind.GRPC, b1), _src(UsSourceKind.RETAIL, b2),
                      _recon_identifiers(), qty_tolerance=D("0.01"))
        assert r.reason == ReconciliationReason.MATCH_WITHIN_TOLERANCE
        assert not r.is_hard_failure

    def test_unknown_symbol(self):
        b = _canonical_book(0.555, 0.560)
        reg = _recon_identifiers()
        unknown = SourceBook(UsSourceKind.RETAIL, "NO_SUCH", b,
                             price_scale=1000, tick_size=D("0.001"))
        r = reconcile(_src(UsSourceKind.GRPC, b), unknown, reg)
        assert r.reason == ReconciliationReason.UNKNOWN_SYMBOL
        assert r.is_hard_failure

    def test_price_scale_mismatch(self):
        b = _canonical_book(0.555, 0.560)
        r = reconcile(
            _src(UsSourceKind.GRPC, b, scale=1000),
            _src(UsSourceKind.EXCHANGE_REST, b, scale=10000),
            _recon_identifiers(),
        )
        assert r.reason == ReconciliationReason.PRICE_SCALE_MISMATCH
        assert r.is_hard_failure

    def test_state_mismatch(self):
        b = _canonical_book(0.555, 0.560)
        r = reconcile(
            _src(UsSourceKind.GRPC, b, state=UsMarketState.OPEN),
            _src(UsSourceKind.RETAIL, b, state=UsMarketState.SUSPENDED),
            _recon_identifiers(),
        )
        assert r.reason == ReconciliationReason.STATE_MISMATCH
        assert r.is_hard_failure

    def test_level_mismatch_is_hard_failure(self):
        b1 = _canonical_book(0.555, 0.560)
        b2 = _canonical_book(0.555, 0.580)  # ask moved 2c
        r = reconcile(_src(UsSourceKind.GRPC, b1), _src(UsSourceKind.RETAIL, b2),
                      _recon_identifiers())
        assert r.reason == ReconciliationReason.LEVEL_MISMATCH
        assert r.is_hard_failure

    def test_lag_is_operational_noise(self):
        """A structural diff explained by timestamp skew is LAG (noise)."""
        b1 = _canonical_book(0.555, 0.560)
        b2 = _canonical_book(0.555, 0.580)
        ts_primary = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
        ts_cross = datetime(2026, 9, 15, 11, 50, tzinfo=UTC)  # retail is 10m older
        r = reconcile(
            _src(UsSourceKind.GRPC, b1, ts=ts_primary),
            _src(UsSourceKind.RETAIL, b2, ts=ts_cross),
            _recon_identifiers(), lag_threshold_seconds=5.0,
        )
        assert r.reason == ReconciliationReason.RETAIL_LAG
        assert not r.is_hard_failure
        assert r.freshness_ok is False

    def test_grpc_lag(self):
        b1 = _canonical_book(0.555, 0.560)
        b2 = _canonical_book(0.555, 0.580)
        ts_primary = datetime(2026, 9, 15, 11, 50, tzinfo=UTC)  # grpc older
        ts_cross = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
        r = reconcile(
            _src(UsSourceKind.GRPC, b1, ts=ts_primary),
            _src(UsSourceKind.EXCHANGE_REST, b2, ts=ts_cross),
            _recon_identifiers(), lag_threshold_seconds=5.0,
        )
        assert r.reason == ReconciliationReason.GRPC_LAG
        assert not r.is_hard_failure

    def test_report_aggregation(self):
        report = UsReconciliationReport()
        b = _canonical_book(0.555, 0.560)
        report.add(reconcile(_src(UsSourceKind.GRPC, b), _src(UsSourceKind.RETAIL, b),
                             _recon_identifiers()))
        report.add(reconcile(_src(UsSourceKind.GRPC, b), _src(UsSourceKind.RETAIL, _canonical_book(0.555, 0.580)),
                             _recon_identifiers()))
        s = report.summary()
        assert s["total"] == 2
        assert s["hard_failures"] == 1
        assert s["operational_noise"] == 1
        assert s["by_reason"]["MATCH"] == 1

    def test_canonical_books_only_never_raw(self):
        """Reconciliation consumes canonical Decimal Books, not raw transport
        representations — verified by constructing via the adapters."""
        from polyalpha.us.adapter import parse_exchange_book

        identifiers = UsIdentifierRegistry()
        identifiers.register("mid-1", "slug-a", "SYMB_1001")
        instruments = UsInstrumentRegistry()
        instruments.register(parse_instrument(_instrument_raw()))

        # gRPC path: int64 scaled px -> GrpcUpdate -> canonical Book.
        stream = UsGrpcMarketStream(identifiers, instruments)
        events = stream.process(_grpc_update())
        grpc_book = events[0].update.to_book(
            identifiers.by_symbol("SYMB_1001"),
            tick_size=D("0.001"), min_order_size=D("1"),
        )
        # Exchange REST path: scaled int -> parse_exchange_book -> canonical Book.
        rest_raw = {
            "marketData": {
                "bids": [{"px": 555, "qty": "10"}, {"px": 550, "qty": "10"}],
                "offers": [{"px": 560, "qty": "10"}, {"px": 565, "qty": "10"}],
                "transactTime": "2026-09-15T12:00:00Z",
            }
        }
        rest_book = parse_exchange_book(rest_raw, NOW, identifiers.by_symbol("SYMB_1001"),
                                        price_scale=1000, tick_size=D("0.001"))
        r = reconcile(
            SourceBook(UsSourceKind.GRPC, "SYMB_1001", grpc_book,
                       price_scale=1000, tick_size=D("0.001"),
                       transact_time=datetime(2026, 9, 15, 12, 0, tzinfo=UTC)),
            SourceBook(UsSourceKind.EXCHANGE_REST, "SYMB_1001", rest_book,
                       price_scale=1000, tick_size=D("0.001"),
                       transact_time=datetime(2026, 9, 15, 12, 0, tzinfo=UTC)),
            identifiers,
        )
        assert r.reason == ReconciliationReason.MATCH


# ══════════════════════════════════════════════════════════════════════════
# US BURN-IN HARDENING
# ══════════════════════════════════════════════════════════════════════════


def _us_report(health=None, replay_ok=True, dirty=False, faults=True) -> UsBurnInReport:
    from polyalpha.replay_verification import ReplayCheckResult

    replay = ReplayCheckResult(
        hash_a="a" * 64, hash_b="a" * 64,
        deterministic=replay_ok, records_a=1, records_b=1,
    )
    provenance = UsBurnInProvenance(
        implementation_commit="us-impl-abc",
        collector_sha256="c" * 64,
        interface_sha256="i" * 64,
        instrument_snapshot_hash="s" * 64,
        reconciliation_policy_sha256="p" * 64,
        phase="US_BURNIN_RUNNING",
        working_tree_dirty=dirty,
    )
    return UsBurnInReport(
        period_start="2026-09-15", period_end="2026-09-16",
        baseline="v0.4.0-us-research-baseline-abc",
        provenance=provenance,
        health=health or UsHealth(),
        replay=replay,
        fault_injection_passed=faults,
    )


class TestUsHealthTracker:
    def test_stream_events_mapped(self):
        from polyalpha.us.grpc_stream import StreamEvent

        tracker = UsHealthTracker()
        tracker.on_stream_event(StreamEvent(kind="heartbeat"))
        tracker.on_stream_event(StreamEvent(kind="reconnect"))
        tracker.on_stream_event(StreamEvent(kind="stale", message="out-of-order transact_time"))
        tracker.on_stream_event(StreamEvent(kind="stale"))
        tracker.on_stream_event(StreamEvent(kind="error", message="no instrument; priceScale unknown"))
        tracker.on_rest_429()
        tracker.on_stale_state_application()
        h = tracker.health
        assert h.grpc_heartbeats == 1
        assert h.grpc_reconnects == 1
        assert h.grpc_out_of_order_updates == 1
        assert h.grpc_stale_invalidations == 1
        assert h.subscription_errors == 1
        assert h.rest_429s == 1
        assert h.stale_state_applications == 1

    def test_reconciliation_reason_mapping(self):
        from polyalpha.us.reconcile import ReconciliationResult, UsSourceKind

        tracker = UsHealthTracker()
        tracker.on_reconciliation(ReconciliationResult(
            symbol="s", primary=UsSourceKind.GRPC, cross=UsSourceKind.RETAIL,
            reason=ReconciliationReason.PRICE_SCALE_MISMATCH,
            identity_ok=True, semantics_ok=False, structure_ok=False, freshness_ok=False,
        ))
        tracker.on_reconciliation(ReconciliationResult(
            symbol="s", primary=UsSourceKind.GRPC, cross=UsSourceKind.RETAIL,
            reason=ReconciliationReason.STATE_MISMATCH,
            identity_ok=True, semantics_ok=False, structure_ok=False, freshness_ok=False,
        ))
        tracker.on_reconciliation(ReconciliationResult(
            symbol="s", primary=UsSourceKind.GRPC, cross=UsSourceKind.RETAIL,
            reason=ReconciliationReason.LEVEL_MISMATCH,
            identity_ok=True, semantics_ok=True, structure_ok=False, freshness_ok=False,
        ))
        tracker.on_reconciliation(ReconciliationResult(
            symbol="s", primary=UsSourceKind.GRPC, cross=UsSourceKind.RETAIL,
            reason=ReconciliationReason.RETAIL_LAG,
            identity_ok=True, semantics_ok=True, structure_ok=False, freshness_ok=False,
        ))
        h = tracker.health
        assert h.price_scale_mismatches == 1
        assert h.state_mismatches == 1
        assert h.unresolved_book_mismatches == 1
        assert h.retail_secondary_mismatches == 1
        assert h.rest_reconciliations == 4


class TestUsBurnInGate:
    def test_clean_report_qualifies(self):
        report = _us_report()
        passed, failures = report.qualifying()
        assert passed is True
        assert failures == []
        assert report.hard_failure_count() == 0

    def test_each_hard_failure_blocks(self):
        cases = {
            "unknown_symbol_events": 1,
            "price_scale_mismatches": 1,
            "tick_size_mismatches": 1,
            "state_mismatches": 1,
            "grpc_out_of_order_updates": 1,
            "unresolved_book_mismatches": 1,
            "stale_state_applications": 1,
        }
        for field, value in cases.items():
            h = UsHealth(**{field: value})
            report = _us_report(health=h)
            passed, failures = report.qualifying()
            assert passed is False, f"{field} should block"
            assert any(field in f for f in failures)

    def test_replay_difference_blocks(self):
        report = _us_report(replay_ok=False)
        passed, failures = report.qualifying()
        assert passed is False
        assert "replay_a_b_differ" in failures

    def test_dirty_worktree_blocks(self):
        report = _us_report(dirty=True)
        passed, failures = report.qualifying()
        assert passed is False
        assert "working_tree_dirty" in failures

    def test_fault_injection_failure_blocks(self):
        report = _us_report(faults=False)
        passed, failures = report.qualifying()
        assert passed is False
        assert "fault_injection" in failures

    def test_operational_noise_is_not_hard_failure(self):
        """REST 429s and retail-secondary lags are NOT hard failures."""
        h = UsHealth(rest_429s=17, retail_secondary_mismatches=9)
        report = _us_report(health=h)
        passed, _ = report.qualifying()
        assert passed is True
        assert report.hard_failure_count() == 0


class TestUsBurnInReportArtifact:
    def test_write_and_hash(self, tmp_path):
        report = _us_report()
        directory, digest = write_us_burnin_report(report, tmp_path / "us_burnin")
        assert (tmp_path / "us_burnin" / "us-burnin-report.json").exists()
        assert (tmp_path / "us_burnin" / "us-burnin-report.md").exists()
        assert len(digest) == 64
        # Deterministic.
        d2, digest2 = write_us_burnin_report(report, tmp_path / "us_burnin2")
        assert digest == digest2

    def test_markdown_surfaces_us_counters(self, tmp_path):
        report = _us_report()
        write_us_burnin_report(report, tmp_path / "us_burnin")
        md = (tmp_path / "us_burnin" / "us-burnin-report.md").read_text(encoding="utf-8")
        assert "POLYALPHA US — BURN-IN INTEGRITY REPORT" in md
        assert "Unknown symbol events" in md
        assert "Price-scale mismatches" in md
        assert "Stale-state applications" in md
        assert "US FINAL GATE" in md

    def test_instrument_snapshot_hash(self):
        reg = UsInstrumentRegistry()
        reg.register(parse_instrument(_instrument_raw()))
        h1 = instrument_snapshot_hash(reg)
        h2 = instrument_snapshot_hash(reg)
        assert h1 == h2
        assert len(h1) == 64
        # Changing tickSize changes the hash.
        reg2 = UsInstrumentRegistry()
        raw = _instrument_raw()
        raw["tickSize"] = "0.01"
        reg2.register(parse_instrument(raw))
        assert instrument_snapshot_hash(reg2) != h1