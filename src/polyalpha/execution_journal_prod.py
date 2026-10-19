"""Durable write-ahead execution journal for the PRODUCTION control plane.

Separate from ``execution_journal.py`` (the frozen demo journal) so the demo
domain stays untouched while production persists the richer intent needed to
recover a RESTING order correctly. Every safety transition is flushed + fsync'd
BEFORE the corresponding venue side effect, so a crash after a side effect but
before its response leaves a recoverable (client_order_id, state, intent) row.

On recovery the harness must be able to distinguish an intended resting order
from execution ambiguity, so each record carries the full order intent:
calibration version, ticker, side, limit price, quantity, signal timestamp,
conservative edge at submission, and the cancel/expiry deadline — plus the
observed filled/remaining counts.

Reading tolerates a truncated trailing line (crash mid-write); a malformed line
in the MIDDLE of the file is corruption and fails closed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class ProdJournalRecord:
    run_id: str
    sequence: int
    state: str
    ticker: str
    client_order_id: str | None = None
    order_id: str | None = None
    timestamp: str = ""
    halt_reason: str | None = None
    # ── Production order intent (persisted so recovery can branch correctly) ──
    calibration_version: str | None = None
    side: str | None = None
    price: str | None = None
    count: str | None = None
    signal_timestamp: str | None = None
    conservative_edge: str | None = None
    expiry_deadline: str | None = None
    filled_count: str | None = None
    remaining_count: str | None = None

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "sequence": self.sequence,
            "state": self.state,
            "ticker": self.ticker,
            "client_order_id": self.client_order_id,
            "order_id": self.order_id,
            "timestamp": self.timestamp,
            "halt_reason": self.halt_reason,
            "calibration_version": self.calibration_version,
            "side": self.side,
            "price": self.price,
            "count": self.count,
            "signal_timestamp": self.signal_timestamp,
            "conservative_edge": self.conservative_edge,
            "expiry_deadline": self.expiry_deadline,
            "filled_count": self.filled_count,
            "remaining_count": self.remaining_count,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ProdJournalRecord":
        return cls(
            run_id=str(d.get("run_id", "")),
            sequence=int(d.get("sequence", 0)),
            state=str(d.get("state", "")),
            ticker=str(d.get("ticker", "")),
            client_order_id=d.get("client_order_id"),
            order_id=d.get("order_id"),
            timestamp=str(d.get("timestamp", "")),
            halt_reason=d.get("halt_reason"),
            calibration_version=d.get("calibration_version"),
            side=d.get("side"),
            price=d.get("price"),
            count=d.get("count"),
            signal_timestamp=d.get("signal_timestamp"),
            conservative_edge=d.get("conservative_edge"),
            expiry_deadline=d.get("expiry_deadline"),
            filled_count=d.get("filled_count"),
            remaining_count=d.get("remaining_count"),
        )


class ProdExecutionJournal:
    """Append-only, fsync-per-transition production execution journal."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(self, record: ProdJournalRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record.to_dict(), sort_keys=True) + "\n"
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def records(self) -> tuple[list[ProdJournalRecord], bool]:
        """Return (complete_records, truncated_tail)."""
        if not self.path.exists():
            return [], False
        lines = self.path.read_text(encoding="utf-8").splitlines()
        records: list[ProdJournalRecord] = []
        truncated_tail = False
        for idx, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if not isinstance(data, dict):
                    raise ValueError("journal line is not an object")
                records.append(ProdJournalRecord.from_dict(data))
            except (json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
                rest_valid = False
                for later in lines[idx + 1 :]:
                    later = later.strip()
                    if not later:
                        continue
                    try:
                        json.loads(later)
                        rest_valid = True
                        break
                    except (json.JSONDecodeError, ValueError):
                        continue
                if rest_valid:
                    raise RuntimeError(f"corrupt journal (line {idx}): {exc}") from exc
                truncated_tail = True
                break
        return records, truncated_tail

    def latest(self) -> ProdJournalRecord | None:
        records, _ = self.records()
        return records[-1] if records else None


def _now() -> str:
    return datetime.now(UTC).isoformat()