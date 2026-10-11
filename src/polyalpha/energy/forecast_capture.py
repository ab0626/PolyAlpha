"""Point-in-time ERCOT forecast-vintage capture (append-only, full intraday path).

Persists EVERY forecast vintage we actually observe — the full hourly intraday
path — never only the daily winner. E3-v0 today consumes a single
``(publish_day, delivery_day)`` value, but the raw evidence keeps the whole path
so future alpha (e.g. the forecast-revision delta
``Mhat_new - Mhat_old``) can be tested without replaying ERCOT.

Storage (all append-only; nothing is overwritten):
    data/energy/ercot/forecast_vintages.jsonl   one normalized row per (doc, day, hour)
    data/energy/ercot/receipts.jsonl            one raw-report receipt per downloaded doc
    data/energy/ercot/status.json               atomic freshness heartbeat

``publish_timestamp`` is the real observation identity (never ``publish_day``
alone), so intraday revisions never collapse. "Latest vintage as of t" is a
read/query concern, not a destructive write.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .ercot_mis import SEVEN_DAY_LOAD_FORECAST_BY_FORECAST_ZONE, parse_forecast_rows

SOURCE_PRODUCT = "NP3-560-CD"
FORECAST_REPORT_TYPE_ID = SEVEN_DAY_LOAD_FORECAST_BY_FORECAST_ZONE


def vintage_dir(root: str | Path = "data/energy/ercot") -> Path:
    return Path(root)


def vintages_path(root: str | Path = "data/energy/ercot") -> Path:
    return vintage_dir(root) / "forecast_vintages.jsonl"


def receipts_path(root: str | Path = "data/energy/ercot") -> Path:
    return vintage_dir(root) / "receipts.jsonl"


def status_path(root: str | Path = "data/energy/ercot") -> Path:
    return vintage_dir(root) / "status.json"


def sha256_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def load_captured_doc_ids(path: str | Path) -> set[str]:
    """doc_ids already captured, read from the append-only receipts store."""
    path = Path(path)
    if not path.exists():
        return set()
    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        doc_id = data.get("doc_id")
        if doc_id:
            ids.add(str(doc_id))
    return ids


def append_jsonl(path: str | Path, rows: list[dict]) -> int:
    """Append rows to a JSONL file; returns bytes written. No-op if empty."""
    path = Path(path)
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(r, sort_keys=True, default=str) for r in rows) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)
    return len(text.encode("utf-8"))


def build_doc_records(
    doc,
    blob: bytes,
    received_at: datetime,
    collector_version: str,
) -> tuple[dict, list[dict]]:
    """Return ``(receipt, vintage_rows)`` for one downloaded forecast document.

    ``doc`` is a ``MisDocument`` (doc_id, friendly_name, publish_date,
    constructed_name). The receipt is the raw-report provenance record; the
    vintage rows are the normalized hourly observations derived from it.
    """
    rows = parse_forecast_rows(blob)
    raw_hash = sha256_bytes(blob)
    receipt = {
        "source_product": SOURCE_PRODUCT,
        "report_type_id": str(FORECAST_REPORT_TYPE_ID),
        "doc_id": str(doc.doc_id),
        "publish_timestamp": doc.publish_date,
        "received_at": received_at.isoformat(),
        "friendly_name": doc.friendly_name,
        "constructed_name": doc.constructed_name,
        "raw_source_hash": raw_hash,
        "n_rows": len(rows),
        "collector_version": collector_version,
    }
    vintage_rows: list[dict] = []
    for delivery_day, hour_ending, total_mw in rows:
        vintage_rows.append({
            "source_product": SOURCE_PRODUCT,
            "report_type_id": str(FORECAST_REPORT_TYPE_ID),
            "doc_id": str(doc.doc_id),
            "publish_timestamp": doc.publish_date,
            "received_at": received_at.isoformat(),
            "operating_day": delivery_day,      # ERCOT operating day being forecast
            "delivery_day": delivery_day,
            "forecast_hour": hour_ending,       # HourEnding, e.g. "1:00".."24:00"
            "system_total_mw": str(total_mw),
            "raw_source_hash": raw_hash,
            "collector_version": collector_version,
        })
    return receipt, vintage_rows


def write_status(path: str | Path, status: dict) -> None:
    """Atomically write the freshness heartbeat (tmp + rename)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(p)