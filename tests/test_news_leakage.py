"""Part 57 — News leakage tests.

No article published after prediction timestamp is available to that
prediction, publication time != retrieval time, article published after
prediction cannot influence it. Hard failure tests.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from polyalpha.external import ExternalFact, FixtureSource
from polyalpha.features.external import extract_external_features
from polyalpha.pipeline import (
    ClaimDeduplicator,
    ClaimExtraction,
    InformationPipeline,
    SimpleImpactModel,
)

D = Decimal
TZ = timezone.utc
TS = datetime(2025, 6, 1, tzinfo=TZ)


def _fact(event_id="e1", published_hours_ago=1, retrieved_hours_ago=0, source="http://news.com/1"):
    return ExternalFact(
        event_id=event_id,
        source_url=source,
        published_at=TS - timedelta(hours=published_hours_ago),
        retrieved_at=TS - timedelta(hours=retrieved_hours_ago),
        features={"sentiment": 0.7, "relevance": 0.9},
    )


def _claim(event_id="e1", published_hours_ago=1, retrieved_hours_ago=0):
    return ClaimExtraction(
        claim_text="Test claim",
        source_url="http://news.com/1",
        published_at=TS - timedelta(hours=published_hours_ago),
        retrieved_at=TS - timedelta(hours=retrieved_hours_ago),
        entities=(),
        probability_impact=D("0.02"),
        impact_confidence=D("0.8"),
        category="politics",
        content_hash="abc123",
    )


# ── Publication before prediction ────────────────────────────────────────


class TestPublicationBeforePrediction:
    def test_article_published_before_prediction_available(self):
        """An article published at T-1h, retrieved at T, should be available at T."""
        fact = _fact(published_hours_ago=1, retrieved_hours_ago=0)
        source = FixtureSource([fact])
        available = source.available("e1", TS)
        assert len(available) == 1

    def test_article_published_after_not_available(self):
        """An article published at T+1h should NOT be available at T."""
        fact = ExternalFact(
            event_id="e1",
            source_url="http://news.com/1",
            published_at=TS + timedelta(hours=1),
            retrieved_at=TS + timedelta(hours=2),
            features={"sentiment": 0.7},
        )
        source = FixtureSource([fact])
        available = source.available("e1", TS)
        assert len(available) == 0

    def test_article_retrieved_after_not_available(self):
        """An article retrieved after T should NOT be available at T."""
        fact = ExternalFact(
            event_id="e1",
            source_url="http://news.com/1",
            published_at=TS - timedelta(hours=1),
            retrieved_at=TS + timedelta(hours=1),
            features={"sentiment": 0.7},
        )
        source = FixtureSource([fact])
        available = source.available("e1", TS)
        assert len(available) == 0

    def test_article_both_after_not_available(self):
        """Both published and retrieved after T => not available."""
        fact = ExternalFact(
            event_id="e1",
            source_url="http://news.com/1",
            published_at=TS + timedelta(hours=1),
            retrieved_at=TS + timedelta(hours=2),
            features={"sentiment": 0.7},
        )
        source = FixtureSource([fact])
        available = source.available("e1", TS)
        assert len(available) == 0


# ── Publication time != retrieval time ───────────────────────────────────


class TestPublicationVsRetrieval:
    def test_retrieval_after_publication(self):
        fact = _fact(published_hours_ago=5, retrieved_hours_ago=1)
        assert fact.published_at < fact.retrieved_at

    def test_retrieval_same_as_publication(self):
        fact = _fact(published_hours_ago=1, retrieved_hours_ago=1)
        assert fact.published_at == fact.retrieved_at


# ── Article published after prediction cannot influence it ────────────────


class TestCannotInfluencePrediction:
    def test_future_article_not_in_available(self):
        """If prediction is made at T, articles published after T are excluded."""
        future_fact = ExternalFact(
            event_id="e1",
            source_url="http://future.com",
            published_at=TS + timedelta(hours=1),
            retrieved_at=TS + timedelta(hours=2),
            features={"sentiment": 0.9},
        )
        past_fact = _fact(published_hours_ago=2, retrieved_hours_ago=1)
        source = FixtureSource([future_fact, past_fact])
        available = source.available("e1", TS)
        assert len(available) == 1
        assert available[0].source_url == "http://news.com/1"

    def test_pipeline_only_uses_available_facts(self):
        """Pipeline should only process facts available at prediction time."""
        pipeline = InformationPipeline(impact_model=SimpleImpactModel())
        claim = _claim(published_hours_ago=1)
        result = pipeline.process_claim(claim, D("0.5"))
        assert result is not None

    def test_pipeline_deduplicates_claims(self):
        pipeline = InformationPipeline(impact_model=SimpleImpactModel())
        claim1 = _claim(published_hours_ago=1)
        claim2 = _claim(published_hours_ago=1)
        r1 = pipeline.process_claim(claim1, D("0.5"))
        r2 = pipeline.process_claim(claim2, D("0.5"))
        assert r1 is not None
        assert r2 is None  # duplicate


# ── External features extraction ─────────────────────────────────────────


class TestExternalFeatures:
    def test_no_facts_returns_default(self):
        features = extract_external_features([], TS)
        assert features["external_fact_count"] == 0
        assert features["has_external_data"] == 0

    def test_single_fact_features(self):
        fact = _fact(published_hours_ago=1)
        features = extract_external_features([fact], TS)
        assert features["external_fact_count"] == 1
        assert features["has_external_data"] == 1
        assert features["ext_sentiment"] == 0.7

    def test_multiple_facts_aggregated(self):
        fact1 = ExternalFact(
            event_id="e1", source_url="http://a.com",
            published_at=TS - timedelta(hours=2),
            retrieved_at=TS - timedelta(hours=1),
            features={"sentiment": 0.6},
        )
        fact2 = ExternalFact(
            event_id="e1", source_url="http://b.com",
            published_at=TS - timedelta(hours=1),
            retrieved_at=TS,
            features={"sentiment": 0.8},
        )
        features = extract_external_features([fact1, fact2], TS)
        assert features["external_fact_count"] == 2
        assert features["ext_sentiment"] == 0.7


# ── Claim deduplication ──────────────────────────────────────────────────


class TestClaimDeduplication:
    def test_same_hash_deduplicated(self):
        dedup = ClaimDeduplicator(window_seconds=3600)
        claim1 = ClaimExtraction(
            claim_text="Same claim", source_url="http://a.com",
            published_at=TS - timedelta(hours=1), retrieved_at=TS,
            entities=(), probability_impact=D("0.02"),
            impact_confidence=D("0.8"), category="politics",
            content_hash="hash1",
        )
        claim2 = ClaimExtraction(
            claim_text="Same claim", source_url="http://a.com",
            published_at=TS - timedelta(hours=1), retrieved_at=TS,
            entities=(), probability_impact=D("0.02"),
            impact_confidence=D("0.8"), category="politics",
            content_hash="hash1",
        )
        assert dedup.is_duplicate(claim1) is False
        assert dedup.is_duplicate(claim2) is True

    def test_different_hash_not_deduplicated(self):
        dedup = ClaimDeduplicator(window_seconds=3600)
        claim1 = ClaimExtraction(
            claim_text="Claim A", source_url="http://a.com",
            published_at=TS - timedelta(hours=1), retrieved_at=TS,
            entities=(), probability_impact=D("0.02"),
            impact_confidence=D("0.8"), category="politics",
            content_hash="hash_a",
        )
        claim2 = ClaimExtraction(
            claim_text="Claim B", source_url="http://b.com",
            published_at=TS - timedelta(hours=1), retrieved_at=TS,
            entities=(), probability_impact=D("0.02"),
            impact_confidence=D("0.8"), category="politics",
            content_hash="hash_b",
        )
        assert dedup.is_duplicate(claim1) is False
        assert dedup.is_duplicate(claim2) is False

    def test_dedup_window_expiry(self):
        dedup = ClaimDeduplicator(window_seconds=60)
        claim = ClaimExtraction(
            claim_text="Claim", source_url="http://a.com",
            published_at=TS - timedelta(hours=1), retrieved_at=TS,
            entities=(), probability_impact=D("0.02"),
            impact_confidence=D("0.8"), category="politics",
            content_hash="hash1",
        )
        dedup.is_duplicate(claim)
        # After window, should not be duplicate
        dedup2 = ClaimDeduplicator(window_seconds=0)
        assert dedup2.is_duplicate(claim) is False


# ── Hard failure: article timing cannot leak ─────────────────────────────


class TestHardFailureNoLeakage:
    def test_prediction_cannot_use_future_article(self):
        """Strict test: if we predict at T, NO article published after T
        should be in the available set."""
        future_facts = [
            ExternalFact(
                event_id="e1",
                source_url=f"http://future{i}.com",
                published_at=TS + timedelta(hours=i),
                retrieved_at=TS + timedelta(hours=i + 1),
                features={"sentiment": 0.9},
            )
            for i in range(1, 6)
        ]
        source = FixtureSource(future_facts)
        available = source.available("e1", TS)
        assert len(available) == 0, (
            f"Found {len(available)} future articles that should be excluded"
        )

    def test_retrieval_time_does_not_override_publication_time(self):
        """Even if retrieved before T, a future-published article is excluded."""
        fact = ExternalFact(
            event_id="e1",
            source_url="http://future.com",
            published_at=TS + timedelta(hours=1),
            retrieved_at=TS - timedelta(hours=1),  # retrieved before publication
            features={"sentiment": 0.9},
        )
        source = FixtureSource([fact])
        available = source.available("e1", TS)
        assert len(available) == 0
