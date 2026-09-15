"""Collector health SLO dashboard.

Part of the v0.3.0 data-collection architecture. Aggregates collector stats,
raw-store counts, reconciliation outcomes, and receive-latency distribution
into the health surface that must be monitored for the entire collection
window. Data-quality observability matters more than another forecasting
model once a 60-90 day run begins.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .market_collector import CollectorStats
from .rawstore import RawStore


@dataclass
class HealthReport:
    collected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    uptime_seconds: float = 0.0
    ws_connections: int = 0
    reconnects: int = 0
    messages_today: int = 0
    book_events: int = 0
    price_change_events: int = 0
    trades: int = 0
    resolutions: int = 0
    markets_tracked: int = 0
    tokens_tracked: int = 0
    reconcile_total: int = 0
    reconcile_matches: int = 0
    reconcile_mismatches: int = 0
    median_receive_lag_ms: float | None = None
    p95_receive_lag_ms: float | None = None
    p99_receive_lag_ms: float | None = None
    oldest_stale_book_seconds: float | None = None
    disk_bytes: int = 0
    compression_ratio: float | None = None

    def as_dict(self) -> dict:
        return {
            "collected_at": self.collected_at.isoformat(),
            "uptime_seconds": round(self.uptime_seconds, 1),
            "ws_connections": self.ws_connections,
            "reconnects": self.reconnects,
            "messages_today": self.messages_today,
            "book_events": self.book_events,
            "price_change_events": self.price_change_events,
            "trades": self.trades,
            "resolutions": self.resolutions,
            "markets_tracked": self.markets_tracked,
            "tokens_tracked": self.tokens_tracked,
            "reconciliation_matches": self.reconcile_matches,
            "reconciliation_mismatches": self.reconcile_mismatches,
            "reconciliation_match_rate": (
                round(self.reconcile_matches / self.reconcile_total, 4)
                if self.reconcile_total
                else None
            ),
            "median_receive_lag_ms": self.median_receive_lag_ms,
            "p95_receive_lag_ms": self.p95_receive_lag_ms,
            "p99_receive_lag_ms": self.p99_receive_lag_ms,
            "oldest_stale_book_seconds": self.oldest_stale_book_seconds,
            "disk_bytes": self.disk_bytes,
            "compression_ratio": self.compression_ratio,
        }


def _percentile(values: list[int], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * pct))
    return round(ordered[index] / 1e6, 1)  # ns -> ms


def build_health_report(
    stats: CollectorStats,
    raw: RawStore,
    reconcile_total: int = 0,
    reconcile_matches: int = 0,
    markets_tracked: int = 0,
    tokens_tracked: int = 0,
) -> HealthReport:
    """Assemble a HealthReport from collector stats + raw store state."""
    report = HealthReport(
        uptime_seconds=time.monotonic() - stats.started_at,
        ws_connections=max(1, stats.reconnect_count + 1),
        reconnects=stats.reconnect_count,
        messages_today=stats.messages_received,
        book_events=stats.book_events,
        price_change_events=stats.price_change_events,
        trades=stats.last_trade_events,
        resolutions=stats.market_resolved_events,
        markets_tracked=markets_tracked,
        tokens_tracked=tokens_tracked,
        reconcile_total=reconcile_total,
        reconcile_matches=reconcile_matches,
        reconcile_mismatches=reconcile_total - reconcile_matches,
        median_receive_lag_ms=_percentile(stats.receive_lag_ns, 0.50),
        p95_receive_lag_ms=_percentile(stats.receive_lag_ns, 0.95),
        p99_receive_lag_ms=_percentile(stats.receive_lag_ns, 0.99),
    )
    if stats.last_book_snapshot_at:
        report.oldest_stale_book_seconds = round(
            time.time() - stats.last_book_snapshot_at, 1
        )
    report.disk_bytes = _disk_bytes(raw)
    return report


def _disk_bytes(raw: RawStore) -> int:
    total = 0
    for path in raw.root.rglob("*.jsonl*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return total


def render_terminal(report: HealthReport) -> str:
    lines = [
        "COLLECTOR HEALTH",
        "=" * 40,
        f"Uptime                  {report.uptime_seconds:>10.1f}s",
        f"WS connections              {report.ws_connections:>10}",
        f"Reconnects                  {report.reconnects:>10}",
        f"Messages today              {report.messages_today:>10,}",
        f"Book events                 {report.book_events:>10,}",
        f"Price changes               {report.price_change_events:>10,}",
        f"Trades                      {report.trades:>10,}",
        f"Resolutions                 {report.resolutions:>10}",
        f"Markets tracked             {report.markets_tracked:>10}",
        f"Tokens tracked              {report.tokens_tracked:>10}",
        f"Reconcile match rate        {report.reconcile_matches:>10} / {report.reconcile_total}",
        f"Median receive lag          {report.median_receive_lag_ms or 0:>10.1f} ms",
        f"P95 receive lag             {report.p95_receive_lag_ms or 0:>10.1f} ms",
        f"P99 receive lag             {report.p99_receive_lag_ms or 0:>10.1f} ms",
        f"Oldest stale book           {report.oldest_stale_book_seconds or 0:>10.1f} s",
        f"Disk                        {report.disk_bytes:>10,} bytes",
    ]
    return "\n".join(lines)