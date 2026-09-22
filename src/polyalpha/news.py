"""Provider-neutral news ingestion (observation-only, NOT methodology).

Accumulates raw unscheduled-information observations into a SEPARATE raw
lineage during the v0.4 freeze, so v0.5 has an honest historical corpus to
preregister transformations against. This module deliberately captures NO
sentiment, surprise, relevance, market_id, or predicted impact — those are
methodology and belong to a future preregistration.

The clock discipline is the point:
    provider_published_at    t2/t3  (the vendor's publishedAt claim)
    source_claimed_publish_at t1    (the underlying source's claimed time)
    received_wall_ns / monotonic_ns t4 (our receipt)

A vendor can only give us (t4 - t3); the true primary clock t1 comes from a
primary-source feed. Never conflate them, or "the market beat our news" will be
confused with "our vendor delivered 4 seconds late".
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from .domain import utc


class NewsRole(str, Enum):
    FAST = "FAST"        # low-latency breaking wire (e.g. Benzinga)
    BROAD = "BROAD"      # breadth/corroboration (e.g. NewsAPI)
    CLUSTER = "CLUSTER"  # entity/event clustering (e.g. Event Registry)
    PRIMARY = "PRIMARY"  # authoritative first publication (gov/agency/league)


@dataclass(frozen=True)
class NewsObservation:
    """One raw news item, normalized for provenance. No model fields."""

    provider: str
    provider_role: NewsRole
    provider_event_id: str
    publisher: str | None
    url: str | None
    headline: str | None
    body_or_summary: str | None
    provider_published_at: datetime | None
    provider_updated_at: datetime | None
    source_claimed_publish_at: datetime | None
    received_wall_ns: int
    received_monotonic_ns: int
    raw_payload_sha256: str
    content_sha256: str
    collector_session_id: str

    def __post_init__(self):
        for ts in (self.provider_published_at, self.provider_updated_at,
                   self.source_claimed_publish_at):
            if ts is not None:
                utc(ts)
        if not self.provider_event_id:
            raise ValueError("provider_event_id required")

    @property
    def provider_latency_ms(self) -> int | None:
        """t4 - t3: how late the vendor delivered relative to its own publishedAt."""
        if self.provider_published_at is None:
            return None
        wall = datetime.fromtimestamp(self.received_wall_ns / 1e9, UTC)
        return int((wall - self.provider_published_at).total_seconds() * 1000)

    def as_dict(self) -> dict:
        def iso(ts: datetime | None) -> str | None:
            return ts.isoformat() if ts else None

        return {
            "provider": self.provider,
            "provider_role": self.provider_role.value,
            "provider_event_id": self.provider_event_id,
            "publisher": self.publisher,
            "url": self.url,
            "headline": self.headline,
            "body_or_summary": self.body_or_summary,
            "provider_published_at": iso(self.provider_published_at),
            "provider_updated_at": iso(self.provider_updated_at),
            "source_claimed_publish_at": iso(self.source_claimed_publish_at),
            "received_wall_ns": self.received_wall_ns,
            "received_monotonic_ns": self.received_monotonic_ns,
            "raw_payload_sha256": self.raw_payload_sha256,
            "content_sha256": self.content_sha256,
            "collector_session_id": self.collector_session_id,
        }


@dataclass(frozen=True)
class RawNewsItem:
    """A provider's normalized item, before raw storage and observation derivation."""

    provider_event_id: str
    publisher: str | None
    url: str | None
    headline: str | None
    body_or_summary: str | None
    provider_published_at: datetime | None
    provider_updated_at: datetime | None
    source_claimed_publish_at: datetime | None
    raw_payload: dict[str, Any] | None = None


class NewsProvider(Protocol):
    """A news source adapter. ``fetch(since)`` is incremental."""

    name: str
    role: NewsRole

    def fetch(self, since: datetime | None) -> list[RawNewsItem]: ...


def content_sha256(headline: str | None, body: str | None) -> str:
    """Exact-content hash (headline + body), NOT semantic. Semantic event
    clustering belongs to v0.5."""
    text = "|".join([(headline or "").strip(), (body or "").strip()])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _iso_parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class NewsCollector:
    """Fetch providers incrementally, store raw wire-exact, derive observations.

    Observation-only: writes raw to a dedicated RawStore and returns
    NewsObservation records; it never feeds any model, signal, or market path.
    """

    def __init__(
        self,
        providers: list[NewsProvider],
        raw,  # RawStore
        watermark_path: str | Path,
        source: str = "news_raw",
        session_id: str | None = None,
    ):
        self.providers = providers
        self.raw = raw
        self.watermark_path = Path(watermark_path)
        self.source = source
        self.session_id = session_id or f"news-{int(time.time())}"

    def _watermarks(self) -> dict[str, str]:
        if not self.watermark_path.exists():
            return {}
        try:
            return json.loads(self.watermark_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_watermarks(self, watermarks: dict[str, str]) -> None:
        self.watermark_path.parent.mkdir(parents=True, exist_ok=True)
        self.watermark_path.write_text(json.dumps(watermarks, indent=2, sort_keys=True), encoding="utf-8")

    def _observed(self, provider: NewsProvider, item: RawNewsItem) -> NewsObservation:
        wall_ns = time.time_ns()
        mono_ns = time.monotonic_ns()
        payload = {
            "provider": provider.name,
            "provider_role": provider.role.value,
            "provider_event_id": item.provider_event_id,
            "publisher": item.publisher,
            "url": item.url,
            "headline": item.headline,
            "body_or_summary": item.body_or_summary,
            "provider_published_at": item.provider_published_at.isoformat() if item.provider_published_at else None,
            "provider_updated_at": item.provider_updated_at.isoformat() if item.provider_updated_at else None,
            "source_claimed_publish_at": item.source_claimed_publish_at.isoformat() if item.source_claimed_publish_at else None,
            "content_sha256": content_sha256(item.headline, item.body_or_summary),
        }
        wire = json.dumps(item.raw_payload if item.raw_payload is not None else payload, sort_keys=True, default=str)
        exchange_ms = int(item.provider_published_at.timestamp() * 1000) if item.provider_published_at else None
        self.raw.append(
            self.source, provider.name, payload, wire=wire,
            received_at_ns=wall_ns, received_monotonic_ns=mono_ns,
            exchange_timestamp_ms=exchange_ms,
        )
        return NewsObservation(
            provider=provider.name,
            provider_role=provider.role,
            provider_event_id=item.provider_event_id,
            publisher=item.publisher,
            url=item.url,
            headline=item.headline,
            body_or_summary=item.body_or_summary,
            provider_published_at=item.provider_published_at,
            provider_updated_at=item.provider_updated_at,
            source_claimed_publish_at=item.source_claimed_publish_at,
            received_wall_ns=wall_ns,
            received_monotonic_ns=mono_ns,
            raw_payload_sha256=hashlib.sha256(wire.encode("utf-8")).hexdigest(),
            content_sha256=content_sha256(item.headline, item.body_or_summary),
            collector_session_id=self.session_id,
        )

    def collect(self) -> dict[str, Any]:
        """One incremental pass over every provider. Returns per-provider stats."""
        watermarks = self._watermarks()
        stats: dict[str, Any] = {"session_id": self.session_id, "providers": {}}
        for provider in self.providers:
            since = _iso_parse(watermarks.get(provider.name))
            try:
                items = provider.fetch(since)
            except Exception as error:  # noqa: BLE001
                stats["providers"][provider.name] = {"error": type(error).__name__, "fetched": 0}
                continue

            new = 0
            max_published = since
            for item in items:
                # Incremental dedup: only items strictly newer than the watermark.
                if item.provider_published_at is not None and since is not None:
                    if item.provider_published_at <= since:
                        continue
                self._observed(provider, item)
                new += 1
                if item.provider_published_at is not None and (
                    max_published is None or item.provider_published_at > max_published
                ):
                    max_published = item.provider_published_at

            if max_published is not None:
                watermarks[provider.name] = max_published.isoformat()
            stats["providers"][provider.name] = {
                "role": provider.role.value,
                "fetched": len(items),
                "new": new,
                "watermark": watermarks.get(provider.name),
            }

        self._save_watermarks(watermarks)
        return stats
