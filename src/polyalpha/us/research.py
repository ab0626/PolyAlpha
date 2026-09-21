"""Polymarket US research dataset builder.

Replays the immutable US raw store and produces a canonical point-in-time
``ResearchDataset`` (``MarketSnapshot`` records) with mandatory parent-event
clustering, so the pre-registered forward-evidence families
(``docs/RESEARCH_AGENDA.md``) can be evaluated with correct cluster-level
inference.

The market price is the object under test: Families 1/2 ask whether the quoted
probability is calibrated against outcomes, so snapshots carry ``yes_mid``
(the quoted probability) and ``final_resolution`` (the outcome), with
``model_probability`` left ``None``.

Point-in-time discipline:
  - Features (book levels, metadata, stats) come from the record at each
    observation time — never from a later record.
  - The label (``final_resolution``) is the eventual settlement, attached
    together with ``resolution_timestamp`` (when it became known to this
    pipeline) so downstream code can reject predictions made after a known
    outcome.

Parent-event clustering: each retail event's ``id`` is the cluster key for all
its child markets; ``event_cluster`` on every snapshot is that parent event id
(falling back to the slug when the parent is unknown). Effective-N is computed
as the number of distinct parent-event clusters, never raw rows.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..rawstore import RawStore
from ..research_dataset import FeatureProvenance, MarketSnapshot, ResearchDataset
from .states import UsMarketState

D = Decimal

SOURCE_MARKETS = "polymarket_us_retail_markets"
SOURCE_BOOK = "polymarket_us_retail_book"
SOURCE_EVENTS = "polymarket_us_retail_events"
SOURCE_SETTLEMENT = "polymarket_us_retail_settlement"


def _amount(value: object) -> Decimal | None:
    """Normalize a retail Amount (dict or scalar) to Decimal, or None."""
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("value")
    if value is None or value == "":
        return None
    try:
        return D(str(value))
    except (InvalidOperation, ValueError):
        return None


def _ts_from_ns(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, UTC)


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _binary_label(value: object) -> int | None:
    """Binary outcome 0/1; anything else (split/void) is not a binary label."""
    if value is None:
        return None
    try:
        v = D(str(value))
    except (InvalidOperation, ValueError):
        return None
    if v == D(0):
        return 0
    if v == D(1):
        return 1
    return None


def _level(entries: list, index: int) -> dict:
    if index < len(entries) and entries[index] is not None:
        return entries[index]
    return {}


def _depth(entries: list, n: int) -> Decimal:
    total = D(0)
    for entry in entries[:n]:
        if entry is None:
            continue
        qty = _amount(entry.get("qty"))
        if qty is not None:
            total += qty
    return total


def _book_bbo(market_data: dict) -> dict:
    bids = market_data.get("bids") or []
    offers = market_data.get("offers") or []
    best_bid = _amount(_level(bids, 0).get("px"))
    best_ask = _amount(_level(offers, 0).get("px"))
    mid = (best_bid + best_ask) / D(2) if best_bid is not None and best_ask is not None else None
    spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
    return {
        "yes_best_bid": best_bid,
        "yes_best_ask": best_ask,
        "yes_mid": mid,
        "yes_spread": spread,
        "yes_bid_size": _amount(_level(bids, 0).get("qty")) or D(0),
        "yes_ask_size": _amount(_level(offers, 0).get("qty")) or D(0),
        "yes_depth_1": _depth(bids, 1),
        "yes_depth_5": _depth(bids, 5),
        "yes_depth_10": _depth(bids, 10),
    }


def build_us_research_dataset(
    raw_dir: str | Path, max_snapshots_per_market: int | None = None
) -> ResearchDataset:
    """Build the canonical US research dataset from the raw store."""
    raw = RawStore(raw_dir)
    markets_by_slug: dict[str, dict] = {}
    settlement_by_slug: dict[str, tuple[int | None, int]] = {}
    parent_event_by_slug: dict[str, str] = {}

    # Pass 1: metadata, parent-event clustering, and settlement labels.
    for record in raw.replay():
        if record.source == SOURCE_MARKETS:
            for market in record.payload.get("markets", []):
                slug = market.get("slug")
                if slug:
                    markets_by_slug[slug] = market  # replay is time-ordered; latest wins
        elif record.source == SOURCE_EVENTS:
            for event in record.payload.get("events", []):
                event_id = event.get("id")
                if event_id is None:
                    continue
                for market in event.get("markets", []):
                    slug = market.get("slug")
                    if slug:
                        parent_event_by_slug[slug] = str(event_id)
        elif record.source == SOURCE_SETTLEMENT:
            slug = record.payload.get("slug")
            if slug:
                settlement_by_slug[slug] = (
                    _binary_label(record.payload.get("settlement")),
                    record.received_at_ns,
                )

    # Pass 2: point-in-time snapshots from book records.
    snapshots: list[MarketSnapshot] = []
    counts: dict[str, int] = defaultdict(int)
    for record in raw.replay():
        if record.source != SOURCE_BOOK:
            continue
        market_data = record.payload.get("marketData", record.payload) or {}
        slug = market_data.get("marketSlug")
        if not slug:
            continue
        if max_snapshots_per_market is not None and counts[slug] >= max_snapshots_per_market:
            continue
        counts[slug] += 1

        # Only tradable (open) books carry a forward price path; skip books on
        # markets already closed/expired/terminated (price pinned to 0/1).
        state_raw = market_data.get("state")
        if state_raw:
            try:
                if not UsMarketState.parse(state_raw).is_tradable():
                    continue
            except ValueError:
                pass  # unknown state: keep rather than over-filter

        observed = _ts_from_ns(record.received_at_ns)
        bbo = _book_bbo(market_data)
        stats = market_data.get("stats") or {}
        meta = markets_by_slug.get(slug, {})
        end = _parse_iso(meta.get("endDate"))
        hours = (end - observed).total_seconds() / 3600.0 if end is not None else None

        settlement = settlement_by_slug.get(slug)
        final = settlement[0] if settlement else None
        resolution_ts = _ts_from_ns(settlement[1]) if settlement else None

        internal = f"us:{slug}"
        cluster = parent_event_by_slug.get(slug, slug)
        snapshots.append(
            MarketSnapshot(
                observation_timestamp=observed,
                market_id=internal,
                event_id=cluster,
                condition_id=internal,
                category=meta.get("category", "unknown"),
                question=meta.get("question", ""),
                yes_token_id=f"{internal}:LONG",
                no_token_id=f"{internal}:SHORT",
                yes_best_bid=bbo["yes_best_bid"],
                yes_best_ask=bbo["yes_best_ask"],
                yes_mid=bbo["yes_mid"],
                yes_spread=bbo["yes_spread"],
                yes_bid_size=bbo["yes_bid_size"],
                yes_ask_size=bbo["yes_ask_size"],
                yes_depth_1=bbo["yes_depth_1"],
                yes_depth_5=bbo["yes_depth_5"],
                yes_depth_10=bbo["yes_depth_10"],
                volume=_amount(stats.get("sharesTraded")) or D(0),
                liquidity=_amount(stats.get("openInterest")) or D(0),
                hours_to_resolution=hours,
                days_to_resolution=(hours / 24.0) if hours is not None else None,
                event_cluster=cluster,
                final_resolution=final,
                resolution_timestamp=resolution_ts,
                feature_provenance=FeatureProvenance(
                    source_timestamp=_parse_iso(market_data.get("transactTime")),
                    retrieval_timestamp=observed,
                ),
            )
        )

    resolved = [s for s in snapshots if s.final_resolution is not None]
    categories: dict[str, int] = defaultdict(int)
    for s in snapshots:
        categories[s.category] += 1

    digest = hashlib.sha256()
    for s in snapshots:
        digest.update(s.snapshot_id.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(s.final_resolution).encode("utf-8"))

    return ResearchDataset(
        snapshots=snapshots,
        created_at=datetime.now(UTC),
        source_reports=["us_raw_store"],
        data_hash=digest.hexdigest(),
        total_observations=len(snapshots),
        resolved_observations=len(resolved),
        unresolved_observations=len(snapshots) - len(resolved),
        unique_markets=len({s.market_id for s in snapshots}),
        unique_events=len({s.event_id for s in snapshots}),
        unique_clusters=len({s.event_cluster for s in snapshots}),
        categories=dict(categories),
    )


def sampling_hierarchy(dataset: ResearchDataset) -> dict:
    """Report the sampling hierarchy: raw rows -> markets -> clusters -> N_eff.

    Effective N is the number of distinct parent-event clusters — the
    independent-ish units the research agenda requires — never raw rows.
    """
    snaps = dataset.snapshots
    raw_obs = len(snaps)
    markets = {s.market_id for s in snaps}
    clusters = {s.event_cluster for s in snaps}
    resolved_markets = {s.market_id for s in snaps if s.final_resolution is not None}
    resolved_clusters = {s.event_cluster for s in snaps if s.final_resolution is not None}
    return {
        "raw_observations": raw_obs,
        "unique_markets": len(markets),
        "parent_event_clusters": len(clusters),
        "effective_n": len(clusters),
        "resolved_markets": len(resolved_markets),
        "resolved_clusters": len(resolved_clusters),
    }
