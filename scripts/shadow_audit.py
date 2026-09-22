#!/usr/bin/env python3
"""External acquisition audit for shadow-external-v0.

Operational only — counts, revisions, retrievals, bytes, gaps, ratios. No
predictive/relationship inspection.
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polyalpha.rawstore import RawStore  # noqa: E402


def _pct(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * (len(s) - 1)))]


def _audit_loop(raw_dir: Path) -> dict[str, dict]:
    if not raw_dir.exists():
        return {}
    store = RawStore(raw_dir)
    by_provider: dict[str, dict] = defaultdict(lambda: {
        "records": 0, "unique": set(), "external_ids": defaultdict(set),
        "bytes": 0, "receipts": [], "provider_minus_receipt": [],
    })
    for r in store.replay():
        p = r.connection_id
        rec = by_provider[p]
        rec["records"] += 1
        rec["bytes"] += len(r.wire)
        rec["receipts"].append(r.received_at_ns)
        payload = r.payload if isinstance(r.payload, dict) else {}
        oid = payload.get("observation_id")
        ext = payload.get("external_id")
        if oid:
            rec["unique"].add(oid)
        if ext and oid:
            rec["external_ids"][ext].add(oid)
        pub = payload.get("provider_published_at")
        if pub:
            try:
                pt = datetime.fromisoformat(pub)
                rec["provider_minus_receipt"].append(
                    max(0.0, (datetime.fromtimestamp(r.received_at_ns / 1e9, UTC) - pt).total_seconds() * 1000)
                )
            except ValueError:
                pass

    out = {}
    for p, rec in by_provider.items():
        receipts = sorted(rec["receipts"])
        gaps = [receipts[i] - receipts[i - 1] for i in range(1, len(receipts))]
        unique = len(rec["unique"])
        n_external = len(rec["external_ids"])
        revised = sum(1 for ids in rec["external_ids"].values() if len(ids) > 1)
        out[p] = {
            "unique_observations": unique,
            "records": rec["records"],
            "duplicate_ratio": round((rec["records"] - unique) / rec["records"], 4) if rec["records"] else 0.0,
            "revision_ratio": round(revised / n_external, 4) if n_external else 0.0,
            "raw_bytes": rec["bytes"],
            "largest_gap_seconds": round(max(gaps) / 1e9, 1) if gaps else 0.0,
            "provider_minus_receipt_p50_ms": round(_pct(rec["provider_minus_receipt"], 0.5), 1),
            "provider_minus_receipt_p95_ms": round(_pct(rec["provider_minus_receipt"], 0.95), 1),
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Shadow external acquisition audit")
    parser.add_argument("--base-dir", default="data/shadow")
    parser.add_argument("--status-dir", default="data/shadow/status")
    args = parser.parse_args()

    base = Path(args.base_dir)
    merged: dict[str, dict] = {}
    for loop_dir in sorted((base / "raw").iterdir()) if (base / "raw").exists() else []:
        if loop_dir.is_dir():
            for p, rec in _audit_loop(loop_dir).items():
                merged[p] = rec

    # RTT / health from the per-loop status files.
    for status_file in sorted((Path(args.status_dir)).glob("*.json")):
        data = json.loads(status_file.read_text(encoding="utf-8"))
        loop = data.get("loop", status_file.stem)
        merged.setdefault(loop, {}).update({
            "last_status": data.get("last_success") or data.get("last_error"),
            "status_ok": "last_success" in data,
        })

    total_bytes = sum(r.get("raw_bytes", 0) for r in merged.values())

    # Growth rate from the previous persisted snapshot (a true rate, not
    # cumulative bytes mislabeled as per-day).
    snapshot_path = Path(args.base_dir) / "audit-snapshot.json"
    prev = {}
    if snapshot_path.exists():
        try:
            prev = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            prev = {}

    now = datetime.now(UTC)
    growth_mb_per_day = None
    if prev.get("ts") and prev.get("total_raw_bytes") is not None:
        dt = (now - datetime.fromisoformat(prev["ts"])).total_seconds() / 86400
        if dt > 0:
            growth_mb_per_day = round((total_bytes - prev["total_raw_bytes"]) / 1024 / 1024 / dt, 2)

    snapshot_path.write_text(json.dumps(
        {"ts": now.isoformat(), "total_raw_bytes": total_bytes}, sort_keys=True
    ), encoding="utf-8")

    print(json.dumps({
        "providers": merged,
        "total_raw_bytes": total_bytes,
        "total_raw_mb": round(total_bytes / 1024 / 1024, 2),
        "growth_mb_per_day": growth_mb_per_day,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
