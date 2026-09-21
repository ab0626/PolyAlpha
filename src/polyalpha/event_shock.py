"""EventShock and per-market reaction records (information-propagation engine).

``EventShock`` is an immutable *research record*, not the estimator: it carries
identity/provenance, the four clocks, bounded informational scores (with their
raw inputs preserved in ``score_provenance``), and the affected markets as
typed ``MarketLink`` objects. Latencies are derived properties, never populated.

``ShockMarketReaction`` is the per-market measurement: one shock maps to many
reactions. ``W_quote_ms`` / ``W_fill_ms`` are market-specific (a shock has no
single W), so they live here, not on the shock.

Clocks are kept separate because they answer different questions:
  L_source   = received - public      (data-ingestion lag)
  L_semantic = processed - received   (understand/map lag)
  L_market   = first_quote - public   (market reaction lag)
  W_quote    = first_quote - processed (tradable window after our interpretation)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from .domain import utc
from .information import logit
from .us.quote_race import Quote


class Relationship(str, Enum):
    PRIMARY = "PRIMARY"
    DIRECT = "DIRECT"
    IMPLIED = "IMPLIED"
    CORRELATED = "CORRELATED"


def _bounded_score(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be in [0,1], got {value}")
    return value


@dataclass(frozen=True)
class MarketLink:
    """One affected market and its relationship to the shock."""

    market_id: str
    relationship: Relationship
    relevance: float
    event_cluster_id: str | None = None

    def __post_init__(self):
        if not 0.0 <= self.relevance <= 1.0:
            raise ValueError(f"relevance must be in [0,1], got {self.relevance}")


@dataclass(frozen=True)
class EventShock:
    """Immutable information-event record with provenance and four clocks."""

    shock_id: str
    event_id: str
    category: str
    source: str
    source_authority: float
    primary_source: bool
    first_public_at: datetime
    first_received_at: datetime
    first_processed_at: datetime
    claim_id: str | None = None
    surprise_score: float | None = None
    novelty_score: float | None = None
    ambiguity_score: float | None = None
    relevance_score: float | None = None
    score_provenance: dict[str, dict[str, Any]] = field(default_factory=dict)
    affected_markets: tuple[MarketLink, ...] = ()

    def __post_init__(self):
        utc(self.first_public_at)
        utc(self.first_received_at)
        utc(self.first_processed_at)
        if not 0.0 <= self.source_authority <= 1.0:
            raise ValueError(f"source_authority must be in [0,1], got {self.source_authority}")
        _bounded_score(self.surprise_score, "surprise_score")
        _bounded_score(self.novelty_score, "novelty_score")
        _bounded_score(self.ambiguity_score, "ambiguity_score")
        _bounded_score(self.relevance_score, "relevance_score")

    @property
    def receive_latency_ms(self) -> int:
        return int((self.first_received_at - self.first_public_at).total_seconds() * 1000)

    @property
    def processing_latency_ms(self) -> int:
        return int((self.first_processed_at - self.first_received_at).total_seconds() * 1000)

    @property
    def total_internal_latency_ms(self) -> int:
        return int((self.first_processed_at - self.first_public_at).total_seconds() * 1000)


@dataclass(frozen=True)
class ShockMarketReaction:
    """Per-market measurement of how one shock was incorporated."""

    shock_id: str
    market_id: str
    pre_event_logit: float
    eventual_logit_delta: float
    first_quote_move_at: datetime | None
    first_fill_at: datetime | None
    t10: datetime | None = None
    t25: datetime | None = None
    t50: datetime | None = None
    t75: datetime | None = None
    t90: datetime | None = None
    spread_change: float | None = None
    depth_change: float | None = None
    shock_processed_at: datetime | None = None
    data_quality_flags: tuple[str, ...] = ()

    @property
    def W_quote_ms(self) -> int | None:
        if self.first_quote_move_at is None or self.shock_processed_at is None:
            return None
        return int((self.first_quote_move_at - self.shock_processed_at).total_seconds() * 1000)

    @property
    def W_fill_ms(self) -> int | None:
        if self.first_fill_at is None or self.shock_processed_at is None:
            return None
        return int((self.first_fill_at - self.shock_processed_at).total_seconds() * 1000)

    def summary(self) -> dict:
        def iso(t: datetime | None) -> str | None:
            return t.isoformat() if t else None

        return {
            "shock_id": self.shock_id,
            "market_id": self.market_id,
            "pre_event_logit": round(self.pre_event_logit, 6),
            "eventual_logit_delta": round(self.eventual_logit_delta, 6),
            "first_quote_move_at": iso(self.first_quote_move_at),
            "first_fill_at": iso(self.first_fill_at),
            "t10": iso(self.t10), "t25": iso(self.t25), "t50": iso(self.t50),
            "t75": iso(self.t75), "t90": iso(self.t90),
            "W_quote_ms": self.W_quote_ms,
            "W_fill_ms": self.W_fill_ms,
            "spread_change": self.spread_change,
            "depth_change": self.depth_change,
            "data_quality_flags": list(self.data_quality_flags),
        }


def measure_shock_reaction(
    shock: EventShock,
    market_id: str,
    quotes: list[Quote],
    fills: list,
    stable_seconds: int = 1800,
    move_threshold_logit: float = 0.02,
    feed_label: str = "REST_2S",
) -> ShockMarketReaction:
    """Compute the per-market reaction to a shock from its quote/fill series.

    ``quotes`` are (timestamp, best_bid, best_ask) snapshots for the market;
    ``fills`` are FillRecords. Both should span [t_public - pre, t_public + stable].
    """
    flags: list[str] = [feed_label]
    qs = sorted([q for q in quotes if q.market_slug == market_id], key=lambda q: q.timestamp)
    fs = [f for f in fills if getattr(f, "market_slug", None) == market_id]

    def quote_at(t: datetime) -> Quote | None:
        last = None
        for q in qs:
            if q.timestamp <= t:
                last = q
            else:
                break
        return last

    pre = quote_at(shock.first_public_at)
    stable = quote_at(shock.first_public_at + timedelta(seconds=stable_seconds))

    if pre is None:
        flags.append("NO_PRE_EVENT_QUOTE")
        return ShockMarketReaction(
            shock_id=shock.shock_id, market_id=market_id,
            pre_event_logit=0.0, eventual_logit_delta=0.0,
            first_quote_move_at=None, first_fill_at=None,
            shock_processed_at=shock.first_processed_at,
            data_quality_flags=tuple(flags),
        )

    pre_logit = logit(pre.mid)
    stable_logit = logit(stable.mid) if stable is not None else pre_logit
    total = stable_logit - pre_logit

    first_quote_move: datetime | None = None
    for q in qs:
        if q.timestamp >= shock.first_public_at and abs(logit(q.mid) - pre_logit) >= move_threshold_logit:
            first_quote_move = q.timestamp
            break

    first_fill: datetime | None = None
    for f in sorted(fs, key=lambda f: f.trade_time):
        if f.trade_time >= shock.first_public_at:
            first_fill = f.trade_time
            break

    def threshold_time(pct: float) -> datetime | None:
        if abs(total) < 1e-12:
            return None
        for q in qs:
            if q.timestamp >= shock.first_public_at:
                frac = (logit(q.mid) - pre_logit) / total
                if frac >= pct / 100.0:
                    return q.timestamp
        return None

    spread_change: float | None = None
    if stable is not None:
        spread_change = float((stable.best_ask - stable.best_bid) - (pre.best_ask - pre.best_bid))

    if abs(total) < 1e-12:
        flags.append("NO_MOVE")

    return ShockMarketReaction(
        shock_id=shock.shock_id,
        market_id=market_id,
        pre_event_logit=pre_logit,
        eventual_logit_delta=total,
        first_quote_move_at=first_quote_move,
        first_fill_at=first_fill,
        t10=threshold_time(10),
        t25=threshold_time(25),
        t50=threshold_time(50),
        t75=threshold_time(75),
        t90=threshold_time(90),
        spread_change=spread_change,
        depth_change=None,  # top-of-book Quote has no depth; extended feeds supply it
        shock_processed_at=shock.first_processed_at,
        data_quality_flags=tuple(flags),
    )
