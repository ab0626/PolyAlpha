"""Market metadata versioning and lifecycle tracking.

Part of the v0.3.0 data-collection architecture. Polymarket market metadata
(question wording, resolution description, state, fees, liquidity, end date,
tags) can change over a market's lifetime. Instead of overwriting the newest
state, each observed change is recorded as a MarketMetadataVersion with a
valid_from / valid_until window and a content hash. This lets a backtest ask
"what did we actually know about this contract at time T?" rather than using
its final metadata.

Lifecycle events from the market stream (new_market, market_resolved) are
also recorded so the complete Market(t) history from discovery through
resolution is preserved.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _content_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MarketMetadataVersion:
    market_id: str
    metadata: dict
    metadata_hash: str
    valid_from: datetime
    valid_until: datetime | None = None

    def summary(self) -> dict:
        return {
            "market_id": self.market_id,
            "metadata_hash": self.metadata_hash,
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
        }


@dataclass
class MetadataStore:
    """In-memory metadata + lifecycle store (persist to raw store by caller).

    Keeps the latest metadata per market and a full version history so that
    point-in-time queries are possible.
    """

    versions: dict[str, list[MarketMetadataVersion]] = field(default_factory=dict)
    lifecycle: list[dict] = field(default_factory=list)

    def observe(self, market_id: str, metadata: dict, at: datetime | None = None) -> MarketMetadataVersion:
        """Record a metadata observation, closing the prior version if changed."""
        at = at or _now()
        history = self.versions.setdefault(market_id, [])
        content_hash = _content_hash(metadata)
        if history and history[-1].metadata_hash == content_hash:
            # No change: leave the current version open.
            return history[-1]
        if history:
            history[-1] = MarketMetadataVersion(
                market_id=market_id,
                metadata=history[-1].metadata,
                metadata_hash=history[-1].metadata_hash,
                valid_from=history[-1].valid_from,
                valid_until=at,
            )
        version = MarketMetadataVersion(
            market_id=market_id,
            metadata=metadata,
            metadata_hash=content_hash,
            valid_from=at,
        )
        history.append(version)
        return version

    def latest(self, market_id: str) -> MarketMetadataVersion | None:
        history = self.versions.get(market_id)
        return history[-1] if history else None

    def as_of(self, market_id: str, at: datetime) -> MarketMetadataVersion | None:
        """Return the metadata valid at time `at`, or None if not yet known."""
        history = self.versions.get(market_id, [])
        for version in reversed(history):
            if version.valid_from <= at:
                if version.valid_until is None or version.valid_until >= at:
                    return version
        return None

    def record_lifecycle(self, kind: str, event: dict, at: datetime | None = None) -> None:
        """Record a raw lifecycle event (new_market / market_resolved)."""
        at = at or _now()
        self.lifecycle.append(
            {
                "kind": kind,
                "market": event.get("market") or event.get("id") or "",
                "received_at": at.isoformat(),
                "event": event,
            }
        )

    def lifecycle_for(self, market: str) -> list[dict]:
        return [e for e in self.lifecycle if e["market"] == market]

    def summary(self) -> dict:
        return {
            "markets_tracked": len(self.versions),
            "total_versions": sum(len(v) for v in self.versions.values()),
            "lifecycle_events": len(self.lifecycle),
        }