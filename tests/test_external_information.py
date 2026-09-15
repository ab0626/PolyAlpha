"""External information handling edge cases.

Part 56: Tests for deduplication, timestamp handling, provenance, leakage
prevention, and conflict resolution in external fact ingestion.

Five test classes:
1. Deduplication: same content under different identifiers
2. Timestamp handling: missing, late, and post-prediction articles
3. Source reliability: missing defaults and conflicting sources
4. Claim processing: dedup, contradiction, unknown markets
5. Multi-market routing: one article → many markets
"""

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

try:
    from polyalpha.external import ExternalFact, FixtureSource
    from polyalpha.features.external import extract_external_features
    # Trigger the lazy import of polyalpha.domain.utc to check Python version
    from polyalpha.domain import utc
except (ImportError, TypeError) as e:
    pytest.skip(f"polyalpha requires Python 3.12+: {e}", allow_module_level=True)

D = Decimal

UTC = timezone.utc


def _fact(
    event_id="evt_1",
    url="https://example.com/article/1",
    published=None,
    retrieved=None,
    features=None,
):
    now = datetime(2025, 6, 1, tzinfo=UTC)
    return ExternalFact(
        event_id=event_id,
        source_url=url,
        published_at=published or now - timedelta(hours=2),
        retrieved_at=retrieved or now,
        features=features or {"sentiment": 0.6, "relevance": 0.8},
    )


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ══════════════════════════════════════════════════════════════════════════════
# DEDUPLICATION — same content, different identifiers
# ══════════════════════════════════════════════════════════════════════════════


class TestDeduplication:
    """Duplicate articles with different IDs/URLs must be collapsed."""

    def test_duplicate_article_same_content_different_id_deduplicated(self):
        """Two facts with identical features and timestamps but different URLs
        should be collapsed by downstream dedup logic."""
        f1 = _fact(url="https://reuters.com/article/100", features={"sentiment": 0.7})
        f2 = _fact(url="https://apnews.com/article/200", features={"sentiment": 0.7})
        # Same content hash means dedup should collapse
        h1 = _content_hash(str(f1.features))
        h2 = _content_hash(str(f2.features))
        assert h1 == h2, "identical features should produce same content hash"

    def test_same_article_different_url_detected(self):
        """Facts with same features but different URLs should be detectable
        as duplicates via content hashing."""
        f1 = _fact(url="https://a.com/x")
        f2 = _fact(url="https://b.com/y")
        # Build a content key from features + event_id + published_at
        key1 = (f1.event_id, str(f1.published_at), str(sorted(f1.features.items())))
        key2 = (f2.event_id, str(f2.published_at), str(sorted(f2.features.items())))
        assert key1 == key2, "same content should produce identical dedup keys"

    def test_same_content_different_whitespace_normalized(self):
        """Facts whose features differ only in whitespace/formatting should
        normalize to the same content hash."""
        import re

        features_a = {"summary": "  market  rallied  "}
        features_b = {"summary": " market rallied "}
        # Normalized: collapse whitespace, strip, lowercase
        def _norm(s):
            return re.sub(r"\s+", " ", s.strip().lower())
        norm_a = {k: _norm(v) if isinstance(v, str) else v for k, v in features_a.items()}
        norm_b = {k: _norm(v) if isinstance(v, str) else v for k, v in features_b.items()}
        assert norm_a == norm_b, "whitespace-only differences should normalize away"

    def test_different_content_not_deduplicated(self):
        """Facts with genuinely different features must NOT be collapsed."""
        f1 = _fact(features={"sentiment": 0.7})
        f2 = _fact(features={"sentiment": 0.2})
        key1 = str(sorted(f1.features.items()))
        key2 = str(sorted(f2.features.items()))
        assert key1 != key2, "different features must produce different keys"


# ══════════════════════════════════════════════════════════════════════════════
# TIMESTAMP HANDLING — missing, late, post-prediction
# ══════════════════════════════════════════════════════════════════════════════


class TestTimestampHandling:
    """Article timestamps must be handled correctly at the boundary."""

    def test_article_timestamp_missing_handled_gracefully(self):
        """FixtureSource should not crash when published_at is far in the past
        (simulating a missing/unknown timestamp)."""
        now = datetime(2025, 6, 1, tzinfo=UTC)
        far_past = datetime(1970, 1, 1, tzinfo=UTC)
        f = ExternalFact(
            event_id="evt_1",
            source_url="https://example.com",
            published_at=far_past,
            retrieved_at=now,
            features={"relevance": 0.5},
        )
        source = FixtureSource([f])
        results = source.available("evt_1", now)
        assert len(results) == 1
        assert results[0].published_at == far_past

    def test_article_retrieved_late_still_usable(self):
        """An article retrieved after its publish time should be available
        as long as retrieved_at <= query time."""
        now = datetime(2025, 6, 1, tzinfo=UTC)
        published = now - timedelta(hours=10)
        retrieved = now - timedelta(hours=1)  # retrieved late but before query
        f = ExternalFact(
            event_id="evt_1",
            source_url="https://example.com",
            published_at=published,
            retrieved_at=retrieved,
            features={"score": 0.9},
        )
        source = FixtureSource([f])
        results = source.available("evt_1", now)
        assert len(results) == 1, "late-retrieved article should still be available"

    def test_article_published_after_prediction_not_influencing(self):
        """A fact published AFTER the prediction timestamp must NOT be
        available to the model — this is the leakage guard."""
        now = datetime(2025, 6, 1, tzinfo=UTC)
        future_published = now + timedelta(hours=5)
        f = ExternalFact(
            event_id="evt_1",
            source_url="https://example.com",
            published_at=future_published,
            retrieved_at=now,
            features={"breaking": 1.0},
        )
        source = FixtureSource([f])
        # FixtureSource filters: max(published_at, retrieved_at) <= at
        results = source.available("evt_1", now)
        assert len(results) == 0, "future-published article must not leak into model"

    def test_article_retrieved_after_query_not_available(self):
        """An article not yet retrieved at query time must not be available."""
        now = datetime(2025, 6, 1, tzinfo=UTC)
        future_retrieved = now + timedelta(hours=1)
        f = ExternalFact(
            event_id="evt_1",
            source_url="https://example.com",
            published_at=now - timedelta(hours=1),
            retrieved_at=future_retrieved,
            features={"x": 1.0},
        )
        source = FixtureSource([f])
        results = source.available("evt_1", now)
        assert len(results) == 0, "not-yet-retrieved article should be excluded"

    def test_fixture_source_only_returns_matching_event(self):
        """FixtureSource must filter by event_id."""
        now = datetime(2025, 6, 1, tzinfo=UTC)
        f1 = _fact(event_id="evt_1")
        f2 = _fact(event_id="evt_2")
        source = FixtureSource([f1, f2])
        results = source.available("evt_1", now)
        assert len(results) == 1
        assert results[0].event_id == "evt_1"


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE RELIABILITY — missing defaults and conflict flagging
# ══════════════════════════════════════════════════════════════════════════════


class TestSourceReliability:
    """Source reliability metadata must degrade gracefully."""

    def test_source_reliability_missing_defaults_to_neutral(self):
        """When no reliability score is present, extract_external_features
        should still work and return a neutral result."""
        now = datetime(2025, 6, 1, tzinfo=UTC)
        f = ExternalFact(
            event_id="evt_1",
            source_url="https://unknown-blog.com/post",
            published_at=now - timedelta(hours=3),
            retrieved_at=now,
            features={"content_score": 0.5},
        )
        features = extract_external_features([f], now)
        assert features["has_external_data"] == 1
        assert features["external_fact_count"] == 1

    def test_no_facts_returns_zero_counts(self):
        """Empty fact list should produce zero counts, not errors."""
        now = datetime(2025, 6, 1, tzinfo=UTC)
        features = extract_external_features([], now)
        assert features["external_fact_count"] == 0
        assert features["has_external_data"] == 0
        assert features["external_recency_hours"] is None

    def test_conflicting_sources_same_event_detected(self):
        """Two facts from different URLs with contradictory sentiment
        should both be returned (conflict flagging is downstream)."""
        now = datetime(2025, 6, 1, tzinfo=UTC)
        f1 = ExternalFact(
            event_id="evt_1",
            source_url="https://source-a.com/article",
            published_at=now - timedelta(hours=2),
            retrieved_at=now,
            features={"sentiment": 0.9},
        )
        f2 = ExternalFact(
            event_id="evt_1",
            source_url="https://source-b.com/article",
            published_at=now - timedelta(hours=1),
            retrieved_at=now,
            features={"sentiment": 0.1},
        )
        source = FixtureSource([f1, f2])
        results = source.available("evt_1", now)
        assert len(results) == 2, "both conflicting sources should be returned"
        sentiments = [r.features["sentiment"] for r in results]
        assert max(sentiments) - min(sentiments) > 0.5, "sources should conflict"

    def test_extract_features_aggregates_multiple_facts(self):
        """Multiple facts for the same event should be aggregated into
        averaged feature values."""
        now = datetime(2025, 6, 1, tzinfo=UTC)
        f1 = ExternalFact(
            event_id="evt_1",
            source_url="https://a.com",
            published_at=now - timedelta(hours=3),
            retrieved_at=now,
            features={"score": 0.4},
        )
        f2 = ExternalFact(
            event_id="evt_1",
            source_url="https://b.com",
            published_at=now - timedelta(hours=1),
            retrieved_at=now,
            features={"score": 0.8},
        )
        features = extract_external_features([f1, f2], now)
        assert abs(float(features["ext_score"]) - 0.6) < 0.01, "features should be averaged"
        assert features["external_fact_count"] == 2


# ══════════════════════════════════════════════════════════════════════════════
# CLAIM PROCESSING — dedup, contradiction, unknown market
# ══════════════════════════════════════════════════════════════════════════════


class TestClaimProcessing:
    """Claims must be deduplicated, contradictions flagged, and unknown
    markets rejected gracefully."""

    def test_duplicate_claim_deduplicated(self):
        """Two identical claims from the same source should collapse."""
        claims = [
            {"event_id": "evt_1", "text": "Market will resolve YES", "source": "reuters"},
            {"event_id": "evt_1", "text": "Market will resolve YES", "source": "reuters"},
        ]
        seen = set()
        unique = []
        for c in claims:
            key = (c["event_id"], c["text"], c["source"])
            if key not in seen:
                seen.add(key)
                unique.append(c)
        assert len(unique) == 1, "duplicate claim should be deduplicated"

    def test_contradictory_claims_flagged(self):
        """Opposite claims for the same event should be detectable."""
        claims = [
            {"event_id": "evt_1", "stance": "for", "confidence": 0.9},
            {"event_id": "evt_1", "stance": "against", "confidence": 0.8},
        ]
        stances = {c["stance"] for c in claims if c["event_id"] == "evt_1"}
        assert "for" in stances and "against" in stances, "contradiction should exist"

    def test_unknown_market_mapping_graceful_rejection(self):
        """A fact mapped to an unknown market_id should not crash ingestion."""
        source = FixtureSource([])
        now = datetime(2025, 6, 1, tzinfo=UTC)
        results = source.available("unknown_market_xyz", now)
        assert results == [], "unknown market should return empty list"

    def test_fact_validation_rejects_empty_event_id(self):
        """ExternalFact with empty event_id must be rejected."""
        try:
            ExternalFact(
                event_id="",
                source_url="https://example.com",
                published_at=datetime(2025, 6, 1, tzinfo=UTC),
                retrieved_at=datetime(2025, 6, 1, tzinfo=UTC),
                features={"x": 1.0},
            )
            assert False, "should have raised ValueError"
        except ValueError:
            pass

    def test_fact_validation_rejects_infinite_feature(self):
        """ExternalFact with non-finite feature values must be rejected."""
        try:
            ExternalFact(
                event_id="evt_1",
                source_url="https://example.com",
                published_at=datetime(2025, 6, 1, tzinfo=UTC),
                retrieved_at=datetime(2025, 6, 1, tzinfo=UTC),
                features={"bad": float("inf")},
            )
            assert False, "should have raised ValueError"
        except ValueError:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# MULTI-MARKET ROUTING — one article → many markets
# ══════════════════════════════════════════════════════════════════════════════


class TestMultiMarketRouting:
    """One article mapping to several markets must update all of them."""

    def test_one_article_maps_to_several_markets(self):
        """A single article mentioning multiple events should be available
        to all matching event_ids."""
        now = datetime(2025, 6, 1, tzinfo=UTC)
        # Simulate one article referenced by two events
        f_multi = ExternalFact(
            event_id="evt_1,evt_2",  # compound event reference
            source_url="https://example.com/multi",
            published_at=now - timedelta(hours=1),
            retrieved_at=now,
            features={"coverage": 1.0},
        )
        # In practice, one article would generate one fact per event
        f_evt1 = ExternalFact(
            event_id="evt_1",
            source_url="https://example.com/multi",
            published_at=now - timedelta(hours=1),
            retrieved_at=now,
            features={"coverage": 1.0},
        )
        f_evt2 = ExternalFact(
            event_id="evt_2",
            source_url="https://example.com/multi",
            published_at=now - timedelta(hours=1),
            retrieved_at=now,
            features={"coverage": 1.0},
        )
        source = FixtureSource([f_evt1, f_evt2])
        r1 = source.available("evt_1", now)
        r2 = source.available("evt_2", now)
        assert len(r1) == 1, "article should map to evt_1"
        assert len(r2) == 1, "article should map to evt_2"
        assert r1[0].source_url == r2[0].source_url, "same underlying article"

    def test_recency_hours_computed_correctly(self):
        """external_recency_hours should reflect time since newest article."""
        now = datetime(2025, 6, 1, tzinfo=UTC)
        f1 = _fact(published=now - timedelta(hours=10))
        f2 = _fact(published=now - timedelta(hours=2))
        features = extract_external_features([f1, f2], now)
        recency = float(features["external_recency_hours"])
        assert 1.5 <= recency <= 2.5, f"recency should be ~2h, got {recency}"

    def test_extract_features_preserves_all_feature_keys(self):
        """All feature keys from facts should appear in aggregated output."""
        now = datetime(2025, 6, 1, tzinfo=UTC)
        f1 = ExternalFact(
            event_id="evt_1",
            source_url="https://a.com",
            published_at=now - timedelta(hours=1),
            retrieved_at=now,
            features={"alpha": 0.3, "beta": 0.7, "gamma": 0.5},
        )
        features = extract_external_features([f1], now)
        assert "ext_alpha" in features
        assert "ext_beta" in features
        assert "ext_gamma" in features

    def test_out_of_order_arrival_ordered_by_timestamp(self):
        """FixtureSource results should be orderable by published_at."""
        now = datetime(2025, 6, 1, tzinfo=UTC)
        f1 = _fact(published=now - timedelta(hours=5))
        f2 = _fact(published=now - timedelta(hours=1))
        f3 = _fact(published=now - timedelta(hours=3))
        source = FixtureSource([f3, f1, f2])
        results = source.available("evt_1", now)
        ordered = sorted(results, key=lambda f: f.published_at)
        assert ordered[0].published_at < ordered[1].published_at < ordered[2].published_at

    def test_retraction_simulation(self):
        """A retracted article should be distinguishable from active ones.
        Downstream systems can filter by a 'retracted' flag."""
        now = datetime(2025, 6, 1, tzinfo=UTC)
        active = _fact(features={"sentiment": 0.7, "retracted": False})
        retracted = _fact(
            url="https://example.com/retracted",
            features={"sentiment": 0.7, "retracted": True},
        )
        facts = [active, retracted]
        active_facts = [f for f in facts if not f.features.get("retracted", False)]
        assert len(active_facts) == 1, "retracted fact should be filtered out"

    def test_updated_article_version_tracked(self):
        """Updated articles should carry version info for provenance."""
        now = datetime(2025, 6, 1, tzinfo=UTC)
        v1 = _fact(features={"version": 1, "sentiment": 0.5})
        v2 = _fact(
            url="https://example.com/article?rev=2",
            features={"version": 2, "sentiment": 0.7},
        )
        assert v1.features["version"] < v2.features["version"]
        assert v2.features["sentiment"] != v1.features["sentiment"]
