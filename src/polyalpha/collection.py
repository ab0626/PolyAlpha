"""Collection orchestrator — ties raw store, WS collector, reconciler,
metadata store, health reporting, and daily manifests into one runnable flow.

Part of the v0.3.0 data-collection architecture. This is the operational
entry point: start the market collector against a RawStore, periodically
reconcile reconstructed books against REST, and produce health reports and
daily manifests. The model/strategy logic is NOT invoked here; collection
only gathers data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .collector_health import HealthReport, build_health_report
from .daily_manifest import ManifestWriter, build_daily_manifest
from .market_collector import MarketCollector, CollectorStats, run_collector
from .market_metadata import MetadataStore
from .rawstore import RawStore
from .reconciler import Reconciler
from .transport import Transport


@dataclass
class CollectionResult:
    stats: CollectorStats
    health: HealthReport
    reconcile_summary: dict
    metadata_summary: dict
    raw_count: int

    def as_dict(self) -> dict:
        return {
            "stats": self.stats.as_dict(),
            "health": self.health.as_dict(),
            "reconcile": self.reconcile_summary,
            "metadata": self.metadata_summary,
            "raw_records": self.raw_count,
        }


def run_collection(
    raw: RawStore,
    metadata: MetadataStore,
    token_ids: list[str],
    transport: Transport | None = None,
    duration_seconds: float | None = None,
    reconcile_interval_seconds: float = 30.0,
    reconcile_max_tokens: int = 25,
    collector_version: str = "unknown",
    markets_tracked: int = 0,
) -> CollectionResult:
    """Run the full collection loop for duration_seconds (None = indefinitely).

    Returns a CollectionResult with stats, health, reconciliation, metadata,
    and raw-record counts. Designed to be called from the CLI or a scheduler.
    """
    collector = MarketCollector(
        raw=raw,
        token_ids=token_ids,
        metadata_store=metadata,
        collector_version=collector_version,
    )
    reconciler = Reconciler(transport) if transport is not None else None
    reconcile_total = 0
    reconcile_matches = 0

    def reconcile_tick() -> None:
        nonlocal reconcile_total, reconcile_matches
        if reconciler is not None:
            results = reconciler.reconcile(collector.books, max_tokens=reconcile_max_tokens)
            reconcile_total += len(results)
            reconcile_matches += sum(1 for r in results if r.matches)
        else:
            reconcile_total += 1
        collector.stats.reconciliation_count = reconcile_total
        collector.stats.book_mismatch_count = reconcile_total - reconcile_matches

    stats = run_collector(
        collector,
        duration_seconds=duration_seconds,
        interval_seconds=reconcile_interval_seconds,
        on_interval=reconcile_tick if transport is not None else None,
    )

    health = build_health_report(
        stats,
        raw,
        reconcile_total=reconcile_total,
        reconcile_matches=reconcile_matches,
        markets_tracked=markets_tracked,
        tokens_tracked=len(token_ids),
    )
    return CollectionResult(
        stats=stats,
        health=health,
        reconcile_summary={
            "total": reconcile_total,
            "matches": reconcile_matches,
            "mismatches": reconcile_total - reconcile_matches,
        },
        metadata_summary=metadata.summary(),
        raw_count=raw.count(),
    )


def write_daily_manifest(
    raw: RawStore,
    writer: ManifestWriter,
    collector_version: str,
    config_hash: str,
    markets_observed: int,
    resolved_markets: int,
    dropped_connections: int,
    reconciliations: int,
    book_mismatches: int,
    day: date | None = None,
) -> dict:
    """Build and write today's (or a given day's) immutable manifest."""
    day = day or date.today()
    manifest = build_daily_manifest(
        raw=raw,
        day=day,
        collector_commit=collector_version,
        config_hash=config_hash,
        markets_observed=markets_observed,
        resolved_markets=resolved_markets,
        dropped_connections=dropped_connections,
        reconciliations=reconciliations,
        book_mismatches=book_mismatches,
    )
    path = writer.write(manifest)
    return {"manifest": manifest.as_dict(), "path": str(path), "sha256": manifest.combined_hash()}