"""Prospective Kalshi energy ladder capture (point-in-time snapshots).

Records every active energy-ladder snapshot with the venue's own timestamps plus
a local receipt clock, as append-only JSONL. Nothing is overwritten. This is the
forward microstructure evidence that cannot be reconstructed later.

Each row stores the full raw market object (book, depth, sizes, strike identity,
rules/settlement text, venue timestamps) plus the receipt clock and the derived
semantic identity (underlying_id, threshold, direction).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .kalshi import KalshiPublicClient, parse_threshold, underlying_id_for

DEFAULT_SERIES = (
    "KXTXERCOTPEAKD",     # Tier-1 wedge: ERCOT daily peak load
    "KXUTILITYERCOT",     # Texas electricity prices
    "KXERCOTWESTM",       # ERCOT West Hub monthly power price
    "KXERCOTHOUM",        # ERCOT Houston Hub monthly power price
    "KXERCOTNORTHM",      # ERCOT North Hub monthly power price
    "KXNATGASD",          # Natural Gas Daily
    "KXWTI",              # WTI oil on day
)


def snapshot_energy_ladders(
    client: KalshiPublicClient,
    series_tickers: tuple[str, ...] = DEFAULT_SERIES,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Fetch active markets for the given energy series and return one snapshot
    record per active market (with receipt clock + derived strike identity)."""
    received = now or datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for series in series_tickers:
        try:
            body = client.markets(series_ticker=series, limit=1000)
        except Exception:  # noqa: BLE001 - capture is best-effort, never blocks other series
            continue
        for m in body.get("markets", []):
            if m.get("status") != "active":
                continue
            threshold, direction = parse_threshold(m)
            occurrence = m.get("occurrence_datetime")
            rows.append({
                "captured_at": received.isoformat(),
                "venue": "kalshi",
                "series_ticker": series,
                "ticker": m.get("ticker"),
                "event_ticker": m.get("event_ticker"),
                "underlying_id": underlying_id_for("ladder", occurrence),
                "threshold": str(threshold) if threshold is not None else None,
                "direction": direction,
                "strike_type": m.get("strike_type"),
                "status": m.get("status"),
                "raw": m,          # full point-in-time market object (book/sizes/rules/ts)
            })
    return rows


def append_jsonl(path: str | Path, rows: list[dict[str, Any]]) -> int:
    """Append snapshot rows to a JSONL file; returns bytes written."""
    path = Path(path)
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = "\n".join(json.dumps(r, sort_keys=True, default=str) for r in rows) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(lines)
    return len(lines.encode("utf-8"))


def ladder_raw_dir(root: str | Path = "data/energy/kalshi/ladders") -> Path:
    return Path(root)