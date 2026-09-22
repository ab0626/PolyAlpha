"""Tests for the observation-only news ingestion layer."""

import sys
from datetime import UTC, datetime, timedelta

import pytest

sys.path.insert(0, "src")

from polyalpha.news import (
    NewsCollector,
    NewsObservation,
    NewsRole,
    RawNewsItem,
    content_sha256,
)
from polyalpha.news_rss import _parse_feed, _parse_rss_date
from polyalpha.rawstore import RawStore

T = datetime(2026, 10, 1, 8, 30, tzinfo=UTC)


def _item(eid, published, headline="h", body="b"):
    return RawNewsItem(
        provider_event_id=eid,
        publisher="p",
        url="http://x",
        headline=headline,
        body_or_summary=body,
        provider_published_at=published,
        provider_updated_at=None,
        source_claimed_publish_at=published,
    )


class _FakeProvider:
    name = "fake"
    role = NewsRole.FAST

    def __init__(self, items):
        self.items = items

    def fetch(self, since):
        return [
            i for i in self.items
            if since is None or (i.provider_published_at is None or i.provider_published_at > since)
        ]


def test_news_observation_requires_event_id():
    with pytest.raises(ValueError):
        NewsObservation(
            provider="p", provider_role=NewsRole.FAST, provider_event_id="",
            publisher=None, url=None, headline=None, body_or_summary=None,
            provider_published_at=None, provider_updated_at=None, source_claimed_publish_at=None,
            received_wall_ns=1, received_monotonic_ns=1,
            raw_payload_sha256="x", content_sha256="y", collector_session_id="s",
        )


def test_news_observation_provider_latency():
    obs = NewsObservation(
        provider="p", provider_role=NewsRole.FAST, provider_event_id="e",
        publisher=None, url=None, headline=None, body_or_summary=None,
        provider_published_at=T, provider_updated_at=None, source_claimed_publish_at=T,
        received_wall_ns=int((T + timedelta(seconds=4)).timestamp() * 1e9),
        received_monotonic_ns=1, raw_payload_sha256="x", content_sha256="y",
        collector_session_id="s",
    )
    assert obs.provider_latency_ms == 4000


def test_content_sha256_exact():
    a = content_sha256("Fed cuts 25bp", "rates down")
    b = content_sha256("Fed cuts 25bp", "rates down")
    assert a == b
    assert a != content_sha256("Fed cuts 50bp", "rates down")


def test_collector_incremental_and_watermark(tmp_path):
    raw = RawStore(tmp_path / "raw", "v1")
    provider = _FakeProvider([
        _item("e1", T),
        _item("e2", T + timedelta(minutes=1)),
    ])
    collector = NewsCollector([provider], raw, tmp_path / "wm.json", session_id="s1")

    stats1 = collector.collect()
    assert stats1["providers"]["fake"]["new"] == 2
    assert stats1["providers"]["fake"]["fetched"] == 2

    # Second pass: watermark advances, only strictly-newer items are new.
    provider.items.append(_item("e3", T + timedelta(minutes=2)))
    stats2 = collector.collect()
    assert stats2["providers"]["fake"]["new"] == 1

    # Raw lineage persists the records with three clocks.
    records = list(RawStore(tmp_path / "raw").replay())
    assert len(records) == 3
    assert records[0].source == "news_raw"
    assert records[0].connection_id == "fake"


def test_rss_parse_rss2():
    xml = (
        b'<rss version="2.0"><channel><title>BLS</title>'
        b'<item><guid>id1</guid><title>CPI released</title><link>http://x/1</link>'
        b'<description>desc</description>'
        b'<pubDate>Tue, 01 Oct 2026 08:30:00 -0400</pubDate></item>'
        b'</channel></rss>'
    )
    items = _parse_feed(xml)
    assert len(items) == 1
    assert items[0].provider_event_id == "id1"
    assert items[0].headline == "CPI released"
    assert items[0].provider_published_at is not None


def test_rss_parse_atom():
    xml = (
        b'<feed xmlns="http://www.w3.org/2005/Atom"><title>Fed</title>'
        b'<entry><id>id1</id><title>FOMC</title>'
        b'<published>2026-10-28T14:00:00Z</published>'
        b'<updated>2026-10-28T14:05:00Z</updated></entry></feed>'
    )
    items = _parse_feed(xml)
    assert len(items) == 1
    assert items[0].headline == "FOMC"
    assert items[0].provider_published_at == datetime(2026, 10, 28, 14, 0, tzinfo=UTC)


def test_parse_rss_date():
    assert _parse_rss_date("2026-10-28T14:00:00Z") == datetime(2026, 10, 28, 14, 0, tzinfo=UTC)
    assert _parse_rss_date("Tue, 01 Oct 2026 08:30:00 -0400") is not None
