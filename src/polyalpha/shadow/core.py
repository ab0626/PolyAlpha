"""shadow-external-v0 core: ExternalObservation, tagged value union, collector.

The generalized observation envelope. News is one source_class, not a special
snowflake. Revision identity is content-based (never includes local observation
time), so re-polling an unchanged datum is one immutable observation, while a
revised value is a distinct, additionally-retained observation.

Clock discipline: preserve every clock actually known (request start, receipt
wall, receipt monotonic, provider publish, source-claimed publish). Never
collapse them into a fake single t0.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from ..domain import utc


class SourceClass(str, Enum):
    NEWS = "NEWS"
    MACRO = "MACRO"
    ENERGY = "ENERGY"
    WEATHER = "WEATHER"
    REGULATORY = "REGULATORY"
    GOVERNMENT = "GOVERNMENT"
    EQUITY = "EQUITY"
    COMMODITY = "COMMODITY"


class Freshness(str, Enum):
    REAL_TIME_FULL = "REAL_TIME_FULL"
    REAL_TIME_PARTIAL = "REAL_TIME_PARTIAL"
    NEAR_REAL_TIME = "NEAR_REAL_TIME"
    DELAYED = "DELAYED"
    HISTORICAL = "HISTORICAL"
    BACKFILL = "BACKFILL"


class Coverage(str, Enum):
    FULL = "FULL"
    PARTIAL_EXCHANGE = "PARTIAL_EXCHANGE"
    PARTIAL = "PARTIAL"


# ── Tagged value union ────────────────────────────────────────────────────
@dataclass(frozen=True)
class ScalarValue:
    value: float
    units: str | None = None
    frequency: str | None = None
    period: str | None = None
    as_of: datetime | None = None
    revision: str | None = None


@dataclass(frozen=True)
class MarketValue:
    price: float | None = None
    bid: float | None = None
    ask: float | None = None
    volume: float | None = None
    instrument: str | None = None


@dataclass(frozen=True)
class AlertValue:
    severity: str | None = None
    certainty: str | None = None
    urgency: str | None = None
    effective: datetime | None = None
    expires: datetime | None = None


@dataclass(frozen=True)
class FilingValue:
    accession: str | None = None
    form: str | None = None
    cik: str | None = None
    filing_date: datetime | None = None


@dataclass(frozen=True)
class TextValue:
    headline: str | None = None
    summary: str | None = None
    language: str | None = None


ExternalValue = ScalarValue | MarketValue | AlertValue | FilingValue | TextValue


def _value_dict(value: ExternalValue) -> dict:
    d = {"kind": type(value).__name__}
    for name in value.__dataclass_fields__:  # type: ignore[attr-defined]
        v = getattr(value, name)
        d[name] = v.isoformat() if isinstance(v, datetime) else v
    return d


def value_hash(value: ExternalValue) -> str:
    payload = json.dumps(_value_dict(value), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def revision_key(
    provider: str,
    external_id: str,
    event_time: datetime | None,
    provider_revision: str | None,
    value_hash: str,
) -> str:
    parts = [
        provider,
        external_id,
        event_time.isoformat() if event_time else "",
        provider_revision or "",
        value_hash,
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def observation_id(schema_version: str, revision_key: str) -> str:
    return hashlib.sha256(f"{schema_version}|{revision_key}".encode("utf-8")).hexdigest()


SCHEMA_VERSION = "shadow-external-v0"


@dataclass(frozen=True)
class ExternalObservation:
    """One immutable external fact. observation_id is content-based (no local time)."""

    observation_id: str
    provider: str
    source_class: SourceClass
    external_id: str
    value: ExternalValue
    publisher_or_authority: str | None
    event_time: datetime | None
    provider_published_at: datetime | None
    provider_updated_at: datetime | None
    source_claimed_publish_at: datetime | None
    request_started_wall_ns: int
    received_wall_ns: int
    received_monotonic_ns: int
    region: str | None
    source_url: str | None
    raw_payload_sha256: str
    content_sha256: str
    collector_session_id: str
    fetch_sequence: int
    transport: str
    freshness_class: Freshness
    coverage_class: Coverage
    provider_revision: str | None
    value_hash: str

    def __post_init__(self):
        for ts in (self.event_time, self.provider_published_at,
                   self.provider_updated_at, self.source_claimed_publish_at):
            if ts is not None:
                utc(ts)
        if not self.observation_id or not self.external_id:
            raise ValueError("observation_id and external_id required")

    def as_dict(self) -> dict:
        def iso(ts: datetime | None) -> str | None:
            return ts.isoformat() if ts else None

        return {
            "observation_id": self.observation_id,
            "schema_version": SCHEMA_VERSION,
            "provider": self.provider,
            "source_class": self.source_class.value,
            "external_id": self.external_id,
            "value": _value_dict(self.value),
            "publisher_or_authority": self.publisher_or_authority,
            "event_time": iso(self.event_time),
            "provider_published_at": iso(self.provider_published_at),
            "provider_updated_at": iso(self.provider_updated_at),
            "source_claimed_publish_at": iso(self.source_claimed_publish_at),
            "request_started_wall_ns": self.request_started_wall_ns,
            "received_wall_ns": self.received_wall_ns,
            "received_monotonic_ns": self.received_monotonic_ns,
            "region": self.region,
            "source_url": self.source_url,
            "raw_payload_sha256": self.raw_payload_sha256,
            "content_sha256": self.content_sha256,
            "collector_session_id": self.collector_session_id,
            "fetch_sequence": self.fetch_sequence,
            "transport": self.transport,
            "freshness_class": self.freshness_class.value,
            "coverage_class": self.coverage_class.value,
            "provider_revision": self.provider_revision,
            "value_hash": self.value_hash,
        }


class ExternalProvider(Protocol):
    """A source adapter. ``fetch(since)`` returns normalized items."""

    name: str
    source_class: SourceClass
    freshness_class: Freshness
    coverage_class: Coverage

    def fetch(self, since: datetime | None) -> list[ExternalObservation]: ...


@dataclass
class CollectorSessionMetrics:
    source: str
    session_id: str
    poll_interval_ms: int = 0
    requests: int = 0
    responses: int = 0
    api_rtt_ms: list[int] = field(default_factory=list)
    provider_minus_receipt_ms: list[int] = field(default_factory=list)
    collection_gaps: int = 0
    rate_limit_events: int = 0
    freshness_class: str = ""
    coverage_class: str = ""

    def summary(self) -> dict:
        def pct(xs: list[int], q: float) -> int:
            if not xs:
                return 0
            s = sorted(xs)
            return s[min(len(s) - 1, int(q * (len(s) - 1)))]

        return {
            "source": self.source,
            "session_id": self.session_id,
            "poll_interval_ms": self.poll_interval_ms,
            "requests": self.requests,
            "responses": self.responses,
            "api_rtt_p50_ms": pct(self.api_rtt_ms, 0.50),
            "api_rtt_p95_ms": pct(self.api_rtt_ms, 0.95),
            "api_rtt_p99_ms": pct(self.api_rtt_ms, 0.99),
            "provider_minus_receipt_p50_ms": pct(self.provider_minus_receipt_ms, 0.50),
            "provider_minus_receipt_p95_ms": pct(self.provider_minus_receipt_ms, 0.95),
            "collection_gaps": self.collection_gaps,
            "rate_limit_events": self.rate_limit_events,
            "freshness_class": self.freshness_class,
            "coverage_class": self.coverage_class,
        }


class ExternalCollector:
    """Fetch providers, store raw wire-exact, dedup by content-based observation id.

    Retrieval metadata (first_seen_live, last_seen_live, retrieval_count) is
    tracked separately from the immutable observation identity.
    """

    def __init__(
        self,
        providers: list[ExternalProvider],
        raw,  # RawStore
        watermark_path: str | Path,
        source: str = "shadow_external",
        session_id: str | None = None,
    ):
        self.providers = providers
        self.raw = raw
        self.watermark_path = Path(watermark_path)
        self.seen_path = Path(watermark_path).with_name(Path(watermark_path).stem + ".seen.json")
        self.source = source
        self.session_id = session_id or f"shadow-{int(time.time())}"
        # observation_id -> [first_ns, last_ns, count]; persisted for restart-safety.
        self._seen: dict[str, list[int]] = self._load_seen()

    def _load_seen(self) -> dict[str, list[int]]:
        if not self.seen_path.exists():
            return {}
        try:
            return json.loads(self.seen_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_seen(self) -> None:
        self.seen_path.parent.mkdir(parents=True, exist_ok=True)
        self.seen_path.write_text(json.dumps(self._seen), encoding="utf-8")

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

    def collect(self) -> dict[str, Any]:
        """One pass over every provider. Returns observations + per-source metrics."""
        watermarks = self._watermarks()
        result: dict[str, Any] = {"session_id": self.session_id, "providers": {}}
        for provider in self.providers:
            since = datetime.fromisoformat(watermarks[provider.name]) if watermarks.get(provider.name) else None
            metrics = CollectorSessionMetrics(
                source=provider.name, session_id=self.session_id,
                freshness_class=provider.freshness_class.value,
                coverage_class=provider.coverage_class.value,
            )
            started = time.monotonic_ns()
            try:
                items = provider.fetch(since)
            except Exception as error:  # noqa: BLE001
                result["providers"][provider.name] = {
                    "error": type(error).__name__, "new": 0, "metrics": metrics.summary(),
                }
                continue
            elapsed_ms = int((time.monotonic_ns() - started) / 1e6)
            metrics.requests = 1
            metrics.responses = 1
            metrics.api_rtt_ms.append(elapsed_ms)

            new = 0
            max_published = since
            for obs in items:
                now_ns = time.time_ns()
                is_new = obs.observation_id not in self._seen
                if is_new:
                    # Raw is appended ONLY for new observations: re-polling an
                    # unchanged datum must not grow the raw store unboundedly.
                    self.raw.append(
                        self.source, provider.name, obs.as_dict(),
                        wire=json.dumps(obs.as_dict(), sort_keys=True, default=str),
                        received_at_ns=now_ns, received_monotonic_ns=time.monotonic_ns(),
                        exchange_timestamp_ms=(
                            int(obs.provider_published_at.timestamp() * 1000)
                            if obs.provider_published_at else None
                        ),
                    )
                    if obs.provider_published_at is not None:
                        metrics.provider_minus_receipt_ms.append(
                            max(0, int((datetime.fromtimestamp(now_ns / 1e9, UTC) - obs.provider_published_at).total_seconds() * 1000))
                        )
                    self._seen[obs.observation_id] = [now_ns, now_ns, 1]
                    new += 1
                else:
                    self._seen[obs.observation_id][1] = now_ns
                    self._seen[obs.observation_id][2] += 1
                if obs.provider_published_at is not None and (
                    max_published is None or obs.provider_published_at > max_published
                ):
                    max_published = obs.provider_published_at

            if max_published is not None:
                watermarks[provider.name] = max_published.isoformat()
            result["providers"][provider.name] = {
                "source_class": provider.source_class.value,
                "new": new,
                "seen": len(self._seen),
                "metrics": metrics.summary(),
            }

        self._save_seen()
        self._save_watermarks(watermarks)
        return result
