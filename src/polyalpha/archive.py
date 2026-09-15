"""Write-once JSONL export for portable receipt-time replay."""

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .storage import Record


def export_jsonl(store, path, as_of):
    count = 0
    with Path(path).open("x", encoding="utf-8") as output:
        for record in store.replay(as_of):
            row = asdict(record)
            row["received_at"] = record.received_at.isoformat()
            row["source_at"] = record.source_at.isoformat() if record.source_at else None
            output.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
            count += 1
    return count


def read_jsonl(path):
    prior = None
    with open(path, encoding="utf-8") as source:
        for line in source:
            row = json.loads(line)
            row["received_at"] = datetime.fromisoformat(row["received_at"])
            row["source_at"] = (
                datetime.fromisoformat(row["source_at"]) if row["source_at"] else None
            )
            record = Record(**row)
            if prior is not None and record.received_at < prior:
                raise ValueError("unordered receipt export")
            prior = record.received_at
            yield record
