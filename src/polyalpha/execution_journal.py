"""Durable write-ahead execution journal for the demo control plane.

Append-only JSONL. Every safety transition is flushed + fsync'd BEFORE the
corresponding venue side effect, so a crash after a side effect but before its
response still leaves a recoverable (client_order_id, state) pair.

Reading tolerates a truncated trailing line (a crash mid-write); a malformed
line in the MIDDLE of the file is corruption and fails closed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class JournalRecord:
    run_id: str
    sequence: int
    state: str
    ticker: str
    client_order_id: str | None = None
    order_id: str | None = None
    timestamp: str = ""
    last_reconcile: dict | None = None
    halt_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "sequence": self.sequence,
            "state": self.state,
            "ticker": self.ticker,
            "client_order_id": self.client_order_id,
            "order_id": self.order_id,
            "timestamp": self.timestamp,
            "last_reconcile": self.last_reconcile,
            "halt_reason": self.halt_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "JournalRecord":
        return cls(
            run_id=str(d.get("run_id", "")),
            sequence=int(d.get("sequence", 0)),
            state=str(d.get("state", "")),
            ticker=str(d.get("ticker", "")),
            client_order_id=d.get("client_order_id"),
            order_id=d.get("order_id"),
            timestamp=str(d.get("timestamp", "")),
            last_reconcile=d.get("last_reconcile"),
            halt_reason=d.get("halt_reason"),
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ExecutionJournal:
    """Append-only, fsync-per-transition execution journal."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(self, record: JournalRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record.to_dict(), sort_keys=True) + "\n"
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def records(self) -> tuple[list[JournalRecord], bool]:
        """Return (complete_records, truncated_tail).

        A malformed line is tolerated ONLY as a trailing tail (crash mid-write);
        a malformed line followed by a valid one is corruption and fails closed.
        """
        if not self.path.exists():
            return [], False
        lines = self.path.read_text(encoding="utf-8").splitlines()
        records: list[JournalRecord] = []
        truncated_tail = False
        for idx, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if not isinstance(data, dict):
                    raise ValueError("journal line is not an object")
                records.append(JournalRecord.from_dict(data))
            except (json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
                # Tolerable only if no valid line follows (i.e. it is the tail).
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

    def latest(self) -> JournalRecord | None:
        records, _ = self.records()
        return records[-1] if records else None