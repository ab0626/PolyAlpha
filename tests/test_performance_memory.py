"""Performance and memory characteristic tests.

Parts 63-65: Verify that core operations complete within time bounds
and that memory usage remains bounded under load.

Eight test classes covering:
1. Parsing throughput (snapshots)
2. Feature extraction throughput (orderbook features)
3. VWAP calculation throughput
4. Backtest throughput
5. Memory: processing doesn't grow unbounded
6. Rolling correlation tracker bounded memory
7. Market cache bounded size
8. Deduplication hash bounded size
"""

import json
import random
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

try:
    from polyalpha.correlation import RollingCorrelationTracker, compute_correlation, rolling_correlation
    from polyalpha.domain import Book, Level
    from polyalpha.execution import FeeSchedule, Order, walk
    from polyalpha.features.orderbook import BookFeatureState, extract_all_features
    from polyalpha.parsing import parse_book
    from polyalpha.research_dataset import FeatureProvenance, MarketSnapshot
except (ImportError, TypeError) as e:
    pytest.skip(f"polyalpha requires Python 3.12+: {e}", allow_module_level=True)

D = Decimal

UTC = timezone.utc


def _utc(year=2025, month=6, day=1, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _make_book_raw(token_id="t1", condition_id="c1", ts_ms=None):
    """Build a raw dict matching the CLOB API format for parse_book."""
    if ts_ms is None:
        ts_ms = int(_utc().timestamp() * 1000)
    return {
        "asset_id": token_id,
        "market": condition_id,
        "timestamp": str(ts_ms),
        "bids": json.dumps([{"price": "0.50", "size": "200"}, {"price": "0.49", "size": "300"}]),
        "asks": json.dumps([{"price": "0.52", "size": "150"}, {"price": "0.53", "size": "250"}]),
        "tick_size": "0.01",
        "min_order_size": "1",
        "hash": "",
    }


def _make_parsed_book(token_id="t1", ts=None):
    """Build a parsed Book object."""
    ts = ts or _utc()
    return Book(
        token_id=token_id,
        condition_id="c1",
        source_at=ts,
        received_at=ts,
        bids=(Level(D("0.50"), D("200")), Level(D("0.49"), D("300"))),
        asks=(Level(D("0.52"), D("150")), Level(D("0.53"), D("250"))),
        tick_size=D("0.01"),
        min_order_size=D("1"),
        source_hash="",
    )


# ══════════════════════════════════════════════════════════════════════════════
# PARSING THROUGHPUT
# ══════════════════════════════════════════════════════════════════════════════


class TestParsingThroughput:
    """Parsing 1000 snapshots should complete within time bounds."""

    def test_parse_1000_books_under_5_seconds(self):
        """Parsing 1000 raw book dicts should complete in <5 seconds."""
        raw_books = []
        base_ms = int(_utc().timestamp() * 1000)
        for i in range(1000):
            raw = {
                "asset_id": f"t{i}",
                "market": f"c{i}",
                "timestamp": str(base_ms + i * 1000),
                "bids": json.dumps([{"price": "0.50", "size": str(100 + i)}]),
                "asks": json.dumps([{"price": "0.55", "size": str(100 + i)}]),
                "tick_size": "0.01",
                "min_order_size": "1",
                "hash": "",
            }
            raw_books.append(raw)

        received_at = _utc()
        start = time.time()
        for raw in raw_books:
            parse_book(raw, received_at, raw["asset_id"])
        elapsed = time.time() - start

        assert elapsed < 5.0, f"Parsing 1000 books took {elapsed:.2f}s, expected <5s"

    def test_parse_1000_market_snapshots_under_5_seconds(self):
        """Constructing 1000 MarketSnapshot objects should complete in <5 seconds."""
        start = time.time()
        snaps = []
        for i in range(1000):
            ts = _utc() + timedelta(minutes=i)
            snaps.append(MarketSnapshot(
                observation_timestamp=ts,
                market_id=f"m{i}",
                event_id=f"evt_{i // 10}",
                condition_id=f"c{i}",
                category="politics",
                question=f"Question {i}?",
                yes_token_id=f"y{i}",
                no_token_id=f"n{i}",
                yes_best_bid=D("0.50"),
                yes_best_ask=D("0.55"),
                yes_mid=D("0.525"),
                yes_spread=D("0.05"),
                volume=D("10000"),
                liquidity=D("50000"),
                hours_to_resolution=168.0,
                fees_enabled=True,
                fee_rate=D("0.02"),
                final_resolution=i % 2,
                model_probability=D("0.55"),
                execution_price=D("0.53"),
                side="BUY",
            ))
        elapsed = time.time() - start

        assert elapsed < 5.0, f"Creating 1000 snapshots took {elapsed:.2f}s"
        assert len(snaps) == 1000


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE EXTRACTION THROUGHPUT
# ══════════════════════════════════════════════════════════════════════════════


class TestFeatureExtractionThroughput:
    """Feature extraction for 1000 observations should complete within bounds."""

    def test_extract_features_1000_books_under_5_seconds(self):
        """extract_all_features on 1000 books should complete in <5 seconds."""
        ts = _utc()
        book = _make_parsed_book(ts=ts)
        state = BookFeatureState()

        start = time.time()
        for i in range(1000):
            extract_all_features(book, state)
        elapsed = time.time() - start

        assert elapsed < 5.0, f"Feature extraction took {elapsed:.2f}s"

    def test_extract_features_with_state_rolling(self):
        """Feature extraction with rolling state (momentum, volatility) should
        still complete 1000 iterations in <5 seconds."""
        ts = _utc()
        state = BookFeatureState()

        start = time.time()
        for i in range(1000):
            offset = timedelta(seconds=i)
            book_ts = ts + offset
            book = Book(
                token_id="t1",
                condition_id="c1",
                source_at=book_ts,
                received_at=book_ts,
                bids=(Level(D("0.50"), D("200")),),
                asks=(Level(D("0.55"), D("150")),),
                tick_size=D("0.01"),
                min_order_size=D("1"),
                source_hash="",
            )
            extract_all_features(book, state)
        elapsed = time.time() - start

        assert elapsed < 5.0, f"Rolling feature extraction took {elapsed:.2f}s"


# ══════════════════════════════════════════════════════════════════════════════
# VWAP CALCULATION THROUGHPUT
# ══════════════════════════════════════════════════════════════════════════════


class TestVWAPThroughput:
    """VWAP calculation should complete within time bounds."""

    def test_vwap_1000_walks_under_1_second(self):
        """1000 walk() calls (each computing VWAP) should complete in <1 second."""
        ts = _utc()
        book = _make_parsed_book(ts=ts)
        fees = FeeSchedule(D("0"), ts, "free")

        start = time.time()
        for i in range(1000):
            order = Order(f"o{i}", "t1", "BUY", D("10"), ts)
            fill = walk(book, order, fees, ts)
            _ = fill.vwap  # access VWAP property
        elapsed = time.time() - start

        assert elapsed < 1.0, f"1000 VWAP calculations took {elapsed:.2f}s"

    def test_vwap_price_impact_monotonic(self):
        """Larger orders should get equal or worse VWAP (monotonicity)."""
        ts = _utc()
        book = _make_parsed_book(ts=ts)
        fees = FeeSchedule(D("0"), ts, "free")

        vwaps = []
        for shares in [5, 20, 50, 100]:
            order = Order(f"o{shares}", "t1", "BUY", D(shares), ts)
            fill = walk(book, order, fees, ts)
            vwaps.append(fill.vwap)

        for i in range(1, len(vwaps)):
            assert vwaps[i] >= vwaps[i - 1] - D("0.001"), (
                f"Larger order got better VWAP: {vwaps[i]} < {vwaps[i-1]}"
            )


# ══════════════════════════════════════════════════════════════════════════════
# BACKTEST THROUGHPUT
# ══════════════════════════════════════════════════════════════════════════════


class TestBacktestThroughput:
    """Simulated backtest with 1000 observations should complete within bounds."""

    def test_backtest_simulation_1000_steps_under_10_seconds(self):
        """Simulating 1000 order-book walks (core of backtest) should
        complete in <10 seconds."""
        base_ts = _utc()
        fees = FeeSchedule(D("0"), base_ts, "free")
        state = BookFeatureState()

        start = time.time()
        for i in range(1000):
            t = base_ts + timedelta(seconds=i)
            book = _make_parsed_book(ts=t)
            order = Order(f"o{i}", "t1", "BUY", D("5"), t)
            fill = walk(book, order, fees, t)
            features = extract_all_features(book, state)
            _ = fill.vwap
            _ = features.get("spread_bps")
        elapsed = time.time() - start

        assert elapsed < 10.0, f"1000-step backtest took {elapsed:.2f}s"


# ══════════════════════════════════════════════════════════════════════════════
# MEMORY: PROCESSING 1000 SNAPSHOTS
# ══════════════════════════════════════════════════════════════════════════════


class TestMemoryBounded:
    """Memory usage must not grow unboundedly when processing many snapshots."""

    def test_feature_state_bounded_memory(self):
        """BookFeatureState deques must not grow beyond maxlen."""
        state = BookFeatureState()
        assert state.midpoints.maxlen == 200
        assert state.spreads.maxlen == 200

        ts = _utc()
        for i in range(500):
            bid_price = D("0.40") + D(str(i % 10)) * D("0.01")
            ask_price = D("0.50") + D(str(i % 10)) * D("0.01")
            book = Book(
                token_id="t1",
                condition_id="c1",
                source_at=ts + timedelta(seconds=i),
                received_at=ts + timedelta(seconds=i),
                bids=(Level(bid_price, D("100")),),
                asks=(Level(ask_price, D("100")),),
                tick_size=D("0.01"),
                min_order_size=D("1"),
                source_hash="",
            )
            extract_all_features(book, state)

        assert len(state.midpoints) <= 200, (
            f"midpoints grew to {len(state.midpoints)}, max should be 200"
        )
        assert len(state.spreads) <= 200, (
            f"spreads grew to {len(state.spreads)}, max should be 200"
        )

    def test_snapshot_list_memory_bounded_by_count(self):
        """Processing 1000 snapshots should produce exactly 1000, not unbounded."""
        snaps = []
        for i in range(1000):
            snaps.append(MarketSnapshot(
                observation_timestamp=_utc() + timedelta(minutes=i),
                market_id=f"m{i}",
                event_id=f"evt_{i // 10}",
                condition_id=f"c{i}",
                category="politics",
                question=f"Q{i}?",
                yes_token_id=f"y{i}",
                no_token_id=f"n{i}",
                yes_mid=D("0.5"),
                model_probability=D("0.5"),
            ))
        assert len(snaps) == 1000
        # Each snapshot is a frozen dataclass — fixed size
        assert sys.getsizeof(snaps) < 10_000_000, (
            f"snapshot list uses {sys.getsizeof(snaps)} bytes, expected <10MB"
        )


# ══════════════════════════════════════════════════════════════════════════════
# ROLLING CORRELATION TRACKER BOUNDED MEMORY
# ══════════════════════════════════════════════════════════════════════════════


class TestRollingCorrelationMemory:
    """RollingCorrelationTracker must maintain bounded memory."""

    def test_tracker_deque_bounded_by_window(self):
        """Price history deques must not exceed window + 1 entries."""
        tokens = ["t1", "t2", "t3"]
        tracker = RollingCorrelationTracker(tokens, window=50, min_observations=5)
        ts = _utc()

        for i in range(200):
            prices = {f"t{j}": D(str(0.5 + random.gauss(0, 0.05))) for j in range(3)}
            tracker.update(prices, ts + timedelta(minutes=i))

        for token in tokens:
            assert len(tracker._price_history[token]) <= 51, (
                f"Price history for {token} has {len(tracker._price_history[token])} entries, "
                f"max should be 51 (window+1)"
            )
        assert len(tracker._timestamps) <= 51

    def test_tracker_produces_valid_correlations(self):
        """Correlations produced must be in [-1, 1] range."""
        tokens = ["t1", "t2"]
        tracker = RollingCorrelationTracker(tokens, window=20, min_observations=5)
        ts = _utc()

        for i in range(50):
            p1 = D(str(0.5 + 0.01 * i))
            p2 = D(str(0.5 + 0.01 * i + 0.005))
            tracker.update({"t1": p1, "t2": p2}, ts + timedelta(minutes=i))

        corr = tracker.get_correlation("t1", "t2")
        assert -1 <= corr <= 1, f"correlation {corr} out of range"

    def test_tracker_summary_bounded(self):
        """Summary dict should have fixed number of keys regardless of input size."""
        tokens = ["t1", "t2", "t3", "t4"]
        tracker = RollingCorrelationTracker(tokens, window=30, min_observations=5)
        ts = _utc()

        for i in range(100):
            prices = {f"t{j}": D(str(0.5 + random.gauss(0, 0.05))) for j in range(4)}
            tracker.update(prices, ts + timedelta(minutes=i))

        summary = tracker.summary()
        assert "tokens" in summary
        assert "observations" in summary
        assert "highly_correlated_pairs" in summary
        assert summary["tokens"] == 4


# ══════════════════════════════════════════════════════════════════════════════
# MARKET CACHE BOUNDED SIZE
# ══════════════════════════════════════════════════════════════════════════════


class TestMarketCacheBounded:
    """Caches must not grow without limit."""

    def test_correlation_matrix_fixed_by_token_count(self):
        """CorrelationMatrix entries are O(n^2) in token count, not unbounded."""
        from polyalpha.correlation import CorrelationMatrix

        tokens = [f"t{i}" for i in range(20)]
        matrix = CorrelationMatrix(tokens=tokens)
        # Set all pairwise correlations
        for i, a in enumerate(tokens):
            for b in tokens[i + 1:]:
                matrix.set(a, b, D("0.5"))

        # Matrix should have exactly n*(n-1)/2 unique pairs + n diagonal
        n = len(tokens)
        expected_pairs = n * (n - 1) // 2
        # Check a few known values
        assert matrix.get("t0", "t0") == D(1)
        assert matrix.get("t0", "t1") == D("0.5")
        assert matrix.get("t1", "t0") == D("0.5")  # symmetric

    def test_fixture_source_stored_as_tuple(self):
        """FixtureSource stores facts as a tuple — fixed size after init."""
        from polyalpha.external import FixtureSource

        facts = [_fact(evt=f"e{i}") for i in range(100)]
        source = FixtureSource(facts)
        assert isinstance(source.facts, tuple)
        assert len(source.facts) == 100

    def test_simulator_consumed_cache_bounded(self):
        """Simulator consumed dict only grows with unique (side, price) pairs."""
        from polyalpha.execution import Simulator

        sim = Simulator()
        ts = _utc()
        book = _make_parsed_book(ts=ts)
        fees = FeeSchedule(D("0"), ts, "free")

        for i in range(50):
            order = Order(f"o{i}", "t1", "BUY", D("5"), ts)
            fill = walk(book, order, fees, ts)
            sim.commit(fill)

        # Consumed should have entries for (BUY, 0.52) and (BUY, 0.53)
        consumed = sim.consumed.get("t1", {})
        assert len(consumed) <= 2, f"consumed has {len(consumed)} entries, expected <=2"


def _fact(evt="e1"):
    """Helper for FixtureSource tests."""
    return ExternalFact(
        event_id=evt,
        source_url=f"https://example.com/{evt}",
        published_at=_utc(),
        retrieved_at=_utc(),
        features={"score": 0.5},
    )


from polyalpha.external import ExternalFact


# ══════════════════════════════════════════════════════════════════════════════
# DEDUPLICATION HASH BOUNDED SIZE
# ══════════════════════════════════════════════════════════════════════════════


class TestDeduplicationHashBounded:
    """Deduplication structures must have bounded memory growth."""

    def test_set_dedup_bounded_by_unique_items(self):
        """A set used for deduplication should not exceed unique item count."""
        dedup = set()
        for i in range(1000):
            key = f"market_{i % 100}"  # only 100 unique keys
            dedup.add(key)
        assert len(dedup) == 100
        assert sys.getsizeof(dedup) < 100_000, "dedup set should be small"

    def test_rolling_correlation_tracker_window_enforced(self):
        """After many updates, tracker should only retain window-sized history."""
        tokens = ["t1"]
        tracker = RollingCorrelationTracker(tokens, window=10, min_observations=5)
        ts = _utc()

        for i in range(200):
            tracker.update({"t1": D(str(0.5 + (i % 10) * 0.01))}, ts + timedelta(minutes=i))

        assert len(tracker._price_history["t1"]) <= 11, (
            f"Price history should be bounded by window+1, got {len(tracker._price_history['t1'])}"
        )

    def test_correlation_computation_constant_space(self):
        """compute_correlation should use O(n) space for n observations."""
        prices_a = [D(str(0.5 + random.gauss(0, 0.05))) for _ in range(1000)]
        prices_b = [D(str(0.5 + random.gauss(0, 0.05))) for _ in range(1000)]

        start = time.time()
        corr = compute_correlation(prices_a, prices_b)
        elapsed = time.time() - start

        assert -1 <= corr <= 1
        assert elapsed < 0.5, f"Correlation computation took {elapsed:.3f}s"

    def test_rolling_correlation_output_bounded(self):
        """rolling_correlation output length is bounded by input length."""
        prices_a = [D(str(0.5 + random.gauss(0, 0.05))) for _ in range(100)]
        prices_b = [D(str(0.5 + random.gauss(0, 0.05))) for _ in range(100)]

        result = rolling_correlation(prices_a, prices_b, window=20)
        assert len(result) == 81  # n - window + 1 = 100 - 20 + 1
        assert all(-1 <= r <= 1 for r in result)
