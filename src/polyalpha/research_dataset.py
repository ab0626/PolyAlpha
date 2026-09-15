"""Canonical point-in-time research dataset with full provenance.

Every observation captures a complete market microstructure snapshot at a
specific point in time. The invariant feature_timestamp <= observation_timestamp
is enforced automatically.

Section 3 of the v0.3 spec.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

D = Decimal


@dataclass(frozen=True)
class FeatureProvenance:
    """Timestamps for a single feature."""

    source_timestamp: datetime | None = None
    retrieval_timestamp: datetime | None = None
    feature_timestamp: datetime | None = None


@dataclass(frozen=True)
class MarketSnapshot:
    """Complete point-in-time market microstructure snapshot.

    Contains 33+ fields covering order-book state, market metadata,
    resolution information, and full provenance.
    """

    # ── Identity ────────────────────────────────────────────────────────────
    observation_timestamp: datetime
    market_id: str
    event_id: str
    condition_id: str
    category: str
    question: str

    # ── Token IDs ───────────────────────────────────────────────────────────
    yes_token_id: str
    no_token_id: str

    # ── YES order-book ──────────────────────────────────────────────────────
    yes_best_bid: Decimal | None = None
    yes_best_ask: Decimal | None = None
    yes_mid: Decimal | None = None
    yes_spread: Decimal | None = None
    yes_depth_1: Decimal = D(0)
    yes_depth_5: Decimal = D(0)
    yes_depth_10: Decimal = D(0)
    yes_bid_size: Decimal = D(0)
    yes_ask_size: Decimal = D(0)

    # ── NO order-book ───────────────────────────────────────────────────────
    no_best_bid: Decimal | None = None
    no_best_ask: Decimal | None = None
    no_mid: Decimal | None = None
    no_spread: Decimal | None = None
    no_depth_1: Decimal = D(0)
    no_depth_5: Decimal = D(0)
    no_depth_10: Decimal = D(0)
    no_bid_size: Decimal = D(0)
    no_ask_size: Decimal = D(0)

    # ── Market metadata ─────────────────────────────────────────────────────
    volume: Decimal = D(0)
    liquidity: Decimal = D(0)

    # ── Temporal ────────────────────────────────────────────────────────────
    days_to_resolution: float | None = None
    hours_to_resolution: float | None = None

    # ── Fees ────────────────────────────────────────────────────────────────
    fees_enabled: bool = False
    fee_rate: Decimal = D(0)

    # ── Resolution ──────────────────────────────────────────────────────────
    resolution_quality: str = "unknown"
    ambiguity_score: float = 0.0
    dispute_risk: float = 0.0

    # ── Clustering ──────────────────────────────────────────────────────────
    event_cluster: str = ""
    semantic_cluster: str = ""

    # ── Outcome ─────────────────────────────────────────────────────────────
    final_resolution: int | None = None
    resolution_timestamp: datetime | None = None

    # ── Model outputs ───────────────────────────────────────────────────────
    model_probability: Decimal | None = None
    conservative_probability: Decimal | None = None
    execution_price: Decimal | None = None
    side: str | None = None
    net_edge: Decimal | None = None

    # ── Provenance ──────────────────────────────────────────────────────────
    feature_provenance: FeatureProvenance = field(default_factory=FeatureProvenance)

    def __post_init__(self) -> None:
        if self.feature_provenance.feature_timestamp is not None:
            if self.feature_provenance.feature_timestamp > self.observation_timestamp:
                raise ValueError(
                    f"INVARIANT VIOLATION: feature_timestamp "
                    f"({self.feature_provenance.feature_timestamp}) > "
                    f"observation_timestamp ({self.observation_timestamp})"
                )

    @property
    def yes_executable_price(self) -> Decimal | None:
        """Executable price for BUY YES = best ask."""
        return self.yes_best_ask

    @property
    def no_executable_price(self) -> Decimal | None:
        """Executable price for BUY NO = best ask."""
        return self.no_best_ask

    @property
    def microprice_yes(self) -> Decimal | None:
        """Volume-weighted mid for YES token."""
        if self.yes_best_bid is None or self.yes_best_ask is None:
            return None
        total = self.yes_bid_size + self.yes_ask_size
        if total == 0:
            return self.yes_mid
        return (
            self.yes_best_ask * self.yes_bid_size + self.yes_best_bid * self.yes_ask_size
        ) / total

    @property
    def microprice_no(self) -> Decimal | None:
        """Volume-weighted mid for NO token."""
        if self.no_best_bid is None or self.no_best_ask is None:
            return None
        total = self.no_bid_size + self.no_ask_size
        if total == 0:
            return self.no_mid
        return (self.no_best_ask * self.no_bid_size + self.no_best_bid * self.no_ask_size) / total

    @property
    def cluster(self) -> str:
        return self.event_cluster if self.event_cluster else self.market_id

    @property
    def snapshot_id(self) -> str:
        ts = self.observation_timestamp
        return f"{self.market_id}:{ts.isoformat() if ts else 'no-ts'}"


@dataclass
class ResearchDataset:
    """Immutable research dataset with full provenance."""

    snapshots: list[MarketSnapshot]
    created_at: datetime
    source_reports: list[str]
    data_hash: str
    period_start: str | None = None
    period_end: str | None = None
    total_observations: int = 0
    resolved_observations: int = 0
    unresolved_observations: int = 0
    unique_markets: int = 0
    unique_events: int = 0
    unique_clusters: int = 0
    categories: dict[str, int] = field(default_factory=dict)

    @property
    def resolved_fraction(self) -> float:
        if not self.snapshots:
            return 0.0
        return self.resolved_observations / len(self.snapshots)

    def brier_score(self) -> float | None:
        resolved = [
            s
            for s in self.snapshots
            if s.final_resolution is not None and s.model_probability is not None
        ]
        if not resolved:
            return None
        return sum((float(s.model_probability) - s.final_resolution) ** 2 for s in resolved) / len(
            resolved
        )

    def log_loss(self) -> float | None:
        import math

        resolved = [
            s
            for s in self.snapshots
            if s.final_resolution is not None and s.model_probability is not None
        ]
        if not resolved:
            return None
        eps = 1e-15
        total = 0.0
        for s in resolved:
            p = max(eps, min(1.0 - eps, float(s.model_probability)))
            total -= math.log(p if s.final_resolution == 1 else 1.0 - p)
        return total / len(resolved)

    def market_brier(self) -> float | None:
        resolved = [
            s for s in self.snapshots if s.final_resolution is not None and s.yes_mid is not None
        ]
        if not resolved:
            return None
        return sum((float(s.yes_mid) - s.final_resolution) ** 2 for s in resolved) / len(resolved)

    def to_csv(self, path: str) -> None:
        import csv

        fields = [
            "observation_timestamp",
            "market_id",
            "event_id",
            "condition_id",
            "category",
            "question",
            "yes_token_id",
            "no_token_id",
            "yes_best_bid",
            "yes_best_ask",
            "yes_mid",
            "yes_spread",
            "yes_depth_1",
            "yes_depth_5",
            "yes_depth_10",
            "yes_bid_size",
            "yes_ask_size",
            "no_best_bid",
            "no_best_ask",
            "no_mid",
            "no_spread",
            "no_depth_1",
            "no_depth_5",
            "no_depth_10",
            "no_bid_size",
            "no_ask_size",
            "volume",
            "liquidity",
            "days_to_resolution",
            "hours_to_resolution",
            "fees_enabled",
            "fee_rate",
            "resolution_quality",
            "ambiguity_score",
            "dispute_risk",
            "event_cluster",
            "semantic_cluster",
            "final_resolution",
            "resolution_timestamp",
            "model_probability",
            "execution_price",
            "side",
            "net_edge",
        ]
        with Path(path).open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for s in self.snapshots:
                writer.writerow(
                    {
                        "observation_timestamp": s.observation_timestamp.isoformat(),
                        "market_id": s.market_id,
                        "event_id": s.event_id,
                        "condition_id": s.condition_id,
                        "category": s.category,
                        "question": s.question,
                        "yes_token_id": s.yes_token_id,
                        "no_token_id": s.no_token_id,
                        "yes_best_bid": str(s.yes_best_bid) if s.yes_best_bid is not None else "",
                        "yes_best_ask": str(s.yes_best_ask) if s.yes_best_ask is not None else "",
                        "yes_mid": str(s.yes_mid) if s.yes_mid is not None else "",
                        "yes_spread": str(s.yes_spread) if s.yes_spread is not None else "",
                        "yes_depth_1": str(s.yes_depth_1),
                        "yes_depth_5": str(s.yes_depth_5),
                        "yes_depth_10": str(s.yes_depth_10),
                        "yes_bid_size": str(s.yes_bid_size),
                        "yes_ask_size": str(s.yes_ask_size),
                        "no_best_bid": str(s.no_best_bid) if s.no_best_bid is not None else "",
                        "no_best_ask": str(s.no_best_ask) if s.no_best_ask is not None else "",
                        "no_mid": str(s.no_mid) if s.no_mid is not None else "",
                        "no_spread": str(s.no_spread) if s.no_spread is not None else "",
                        "no_depth_1": str(s.no_depth_1),
                        "no_depth_5": str(s.no_depth_5),
                        "no_depth_10": str(s.no_depth_10),
                        "no_bid_size": str(s.no_bid_size),
                        "no_ask_size": str(s.no_ask_size),
                        "volume": str(s.volume),
                        "liquidity": str(s.liquidity),
                        "days_to_resolution": s.days_to_resolution
                        if s.days_to_resolution is not None
                        else "",
                        "hours_to_resolution": s.hours_to_resolution
                        if s.hours_to_resolution is not None
                        else "",
                        "fees_enabled": s.fees_enabled,
                        "fee_rate": str(s.fee_rate),
                        "resolution_quality": s.resolution_quality,
                        "ambiguity_score": s.ambiguity_score,
                        "dispute_risk": s.dispute_risk,
                        "event_cluster": s.event_cluster,
                        "semantic_cluster": s.semantic_cluster,
                        "final_resolution": s.final_resolution
                        if s.final_resolution is not None
                        else "",
                        "resolution_timestamp": s.resolution_timestamp.isoformat()
                        if s.resolution_timestamp
                        else "",
                        "model_probability": str(s.model_probability)
                        if s.model_probability is not None
                        else "",
                        "execution_price": str(s.execution_price)
                        if s.execution_price is not None
                        else "",
                        "side": s.side or "",
                        "net_edge": str(s.net_edge) if s.net_edge is not None else "",
                    }
                )

    def to_json(self, path: str) -> None:
        data = {
            "created_at": self.created_at.isoformat(),
            "source_reports": self.source_reports,
            "data_hash": self.data_hash,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "total_observations": self.total_observations,
            "resolved_observations": self.resolved_observations,
            "unique_markets": self.unique_markets,
            "unique_events": self.unique_events,
            "unique_clusters": self.unique_clusters,
            "categories": self.categories,
            "snapshots": [_snapshot_to_dict(s) for s in self.snapshots],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)


def _snapshot_to_dict(s: MarketSnapshot) -> dict:
    return {
        "observation_timestamp": s.observation_timestamp.isoformat(),
        "market_id": s.market_id,
        "event_id": s.event_id,
        "condition_id": s.condition_id,
        "category": s.category,
        "question": s.question,
        "yes_token_id": s.yes_token_id,
        "no_token_id": s.no_token_id,
        "yes_best_bid": str(s.yes_best_bid) if s.yes_best_bid is not None else None,
        "yes_best_ask": str(s.yes_best_ask) if s.yes_best_ask is not None else None,
        "yes_mid": str(s.yes_mid) if s.yes_mid is not None else None,
        "yes_spread": str(s.yes_spread) if s.yes_spread is not None else None,
        "yes_depth_1": str(s.yes_depth_1),
        "yes_depth_5": str(s.yes_depth_5),
        "yes_depth_10": str(s.yes_depth_10),
        "yes_bid_size": str(s.yes_bid_size),
        "yes_ask_size": str(s.yes_ask_size),
        "no_best_bid": str(s.no_best_bid) if s.no_best_bid is not None else None,
        "no_best_ask": str(s.no_best_ask) if s.no_best_ask is not None else None,
        "no_mid": str(s.no_mid) if s.no_mid is not None else None,
        "no_spread": str(s.no_spread) if s.no_spread is not None else None,
        "no_depth_1": str(s.no_depth_1),
        "no_depth_5": str(s.no_depth_5),
        "no_depth_10": str(s.no_depth_10),
        "no_bid_size": str(s.no_bid_size),
        "no_ask_size": str(s.no_ask_size),
        "volume": str(s.volume),
        "liquidity": str(s.liquidity),
        "days_to_resolution": s.days_to_resolution,
        "hours_to_resolution": s.hours_to_resolution,
        "fees_enabled": s.fees_enabled,
        "fee_rate": str(s.fee_rate),
        "resolution_quality": s.resolution_quality,
        "ambiguity_score": s.ambiguity_score,
        "dispute_risk": s.dispute_risk,
        "event_cluster": s.event_cluster,
        "semantic_cluster": s.semantic_cluster,
        "final_resolution": s.final_resolution,
        "resolution_timestamp": s.resolution_timestamp.isoformat()
        if s.resolution_timestamp
        else None,
        "model_probability": str(s.model_probability) if s.model_probability is not None else None,
        "conservative_probability": str(s.conservative_probability)
        if s.conservative_probability is not None
        else None,
        "execution_price": str(s.execution_price) if s.execution_price is not None else None,
        "side": s.side,
        "net_edge": str(s.net_edge) if s.net_edge is not None else None,
        "feature_provenance": {
            "source_timestamp": s.feature_provenance.source_timestamp.isoformat()
            if s.feature_provenance.source_timestamp
            else None,
            "retrieval_timestamp": s.feature_provenance.retrieval_timestamp.isoformat()
            if s.feature_provenance.retrieval_timestamp
            else None,
            "feature_timestamp": s.feature_provenance.feature_timestamp.isoformat()
            if s.feature_provenance.feature_timestamp
            else None,
        },
    }


def _snapshot_from_dict(d: dict) -> MarketSnapshot:
    def _dec(v):
        return D(str(v)) if v is not None else None

    def _ts(v):
        return datetime.fromisoformat(v) if v else None

    prov = d.get("feature_provenance", {})
    return MarketSnapshot(
        observation_timestamp=_ts(d["observation_timestamp"]),
        market_id=d["market_id"],
        event_id=d.get("event_id", ""),
        condition_id=d["condition_id"],
        category=d.get("category", "unknown"),
        question=d.get("question", ""),
        yes_token_id=d.get("yes_token_id", ""),
        no_token_id=d.get("no_token_id", ""),
        yes_best_bid=_dec(d.get("yes_best_bid")),
        yes_best_ask=_dec(d.get("yes_best_ask")),
        yes_mid=_dec(d.get("yes_mid")),
        yes_spread=_dec(d.get("yes_spread")),
        yes_depth_1=_dec(d.get("yes_depth_1")) or D(0),
        yes_depth_5=_dec(d.get("yes_depth_5")) or D(0),
        yes_depth_10=_dec(d.get("yes_depth_10")) or D(0),
        yes_bid_size=_dec(d.get("yes_bid_size")) or D(0),
        yes_ask_size=_dec(d.get("yes_ask_size")) or D(0),
        no_best_bid=_dec(d.get("no_best_bid")),
        no_best_ask=_dec(d.get("no_best_ask")),
        no_mid=_dec(d.get("no_mid")),
        no_spread=_dec(d.get("no_spread")),
        no_depth_1=_dec(d.get("no_depth_1")) or D(0),
        no_depth_5=_dec(d.get("no_depth_5")) or D(0),
        no_depth_10=_dec(d.get("no_depth_10")) or D(0),
        no_bid_size=_dec(d.get("no_bid_size")) or D(0),
        no_ask_size=_dec(d.get("no_ask_size")) or D(0),
        volume=_dec(d.get("volume")) or D(0),
        liquidity=_dec(d.get("liquidity")) or D(0),
        days_to_resolution=d.get("days_to_resolution"),
        hours_to_resolution=d.get("hours_to_resolution"),
        fees_enabled=d.get("fees_enabled", False),
        fee_rate=_dec(d.get("fee_rate")) or D(0),
        resolution_quality=d.get("resolution_quality", "unknown"),
        ambiguity_score=d.get("ambiguity_score", 0.0),
        dispute_risk=d.get("dispute_risk", 0.0),
        event_cluster=d.get("event_cluster", ""),
        semantic_cluster=d.get("semantic_cluster", ""),
        final_resolution=d.get("final_resolution"),
        resolution_timestamp=_ts(d.get("resolution_timestamp")),
        model_probability=_dec(d.get("model_probability")),
        conservative_probability=_dec(d.get("conservative_probability")),
        execution_price=_dec(d.get("execution_price")),
        side=d.get("side"),
        net_edge=_dec(d.get("net_edge")),
        feature_provenance=FeatureProvenance(
            source_timestamp=_ts(prov.get("source_timestamp")),
            retrieval_timestamp=_ts(prov.get("retrieval_timestamp")),
            feature_timestamp=_ts(prov.get("feature_timestamp")),
        ),
    )


def build_dataset_from_reports(
    reports: list[dict], source_labels: list[str] | None = None
) -> ResearchDataset:
    """Build dataset from backtest reports (backward-compatible path)."""
    source_labels = source_labels or [f"report_{i}" for i in range(len(reports))]
    seen: dict[str, MarketSnapshot] = {}
    for report in reports:
        for fc in report.get("forecasts", []):
            market_id = fc["market_id"]
            label = report.get("outcome_labels", {}).get(market_id)
            snap = MarketSnapshot(
                observation_timestamp=datetime.fromisoformat(fc["at"]),
                market_id=market_id,
                event_id=fc.get("event_id", ""),
                condition_id=fc.get("condition_id", market_id),
                category=fc.get("category", "unknown"),
                question=fc.get("question", ""),
                yes_token_id=fc.get("yes_token_id", ""),
                no_token_id=fc.get("no_token_id", ""),
                yes_mid=D(str(fc["market_mid"])) if fc.get("market_mid") else None,
                model_probability=D(str(fc["p"])),
                execution_price=D(str(fc["execution_price"]))
                if fc.get("execution_price")
                else None,
                side=fc.get("side"),
                net_edge=D(str(fc["net_edge"])) if fc.get("net_edge") else None,
                final_resolution=int(label["outcome"])
                if label and label.get("outcome") is not None
                else None,
                resolution_timestamp=datetime.fromisoformat(label["known_at"])
                if label and label.get("known_at")
                else None,
                event_cluster=fc.get("cluster", market_id),
            )
            seen[market_id] = snap
    snapshots = list(seen.values())
    return _build_dataset_from_snapshots(snapshots, source_labels)


def build_dataset_from_snapshots(
    snapshots: list[MarketSnapshot],
    source_labels: list[str] | None = None,
) -> ResearchDataset:
    """Build dataset from raw MarketSnapshot list."""
    return _build_dataset_from_snapshots(snapshots, source_labels or [])


def _build_dataset_from_snapshots(
    snapshots: list[MarketSnapshot], source_labels: list[str]
) -> ResearchDataset:
    markets = set()
    events = set()
    clusters = set()
    cats: dict[str, int] = {}
    resolved = 0
    for s in snapshots:
        markets.add(s.market_id)
        if s.event_id:
            events.add(s.event_id)
        if s.event_cluster:
            clusters.add(s.event_cluster)
        cats[s.category] = cats.get(s.category, 0) + 1
        if s.final_resolution is not None:
            resolved += 1

    timestamps = [s.observation_timestamp for s in snapshots]
    content = json.dumps(
        [_snapshot_to_dict(s) for s in sorted(snapshots, key=lambda x: x.observation_timestamp)],
        default=str,
    )
    data_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

    return ResearchDataset(
        snapshots=snapshots,
        created_at=datetime.utcnow().replace(tzinfo=None),
        source_reports=source_labels,
        data_hash=data_hash,
        period_start=min(timestamps).isoformat() if timestamps else None,
        period_end=max(timestamps).isoformat() if timestamps else None,
        total_observations=len(snapshots),
        resolved_observations=resolved,
        unresolved_observations=len(snapshots) - resolved,
        unique_markets=len(markets),
        unique_events=len(events),
        unique_clusters=len(clusters),
        categories=cats,
    )
