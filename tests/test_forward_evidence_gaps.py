"""Adversarial tests for gap-ledger fail-closed behavior + coverage provenance."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta

sys.path.insert(0, "src")

from polyalpha.us.forward_evidence import (  # noqa: E402
    REAL_DATA_START_US,
    _ledger_sha256,
    _load_known_gaps,
    _observed_coverage,
    _union_unobserved_seconds,
)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _gap(start: datetime, end: datetime) -> dict:
    return {"start": _iso(start), "end": _iso(end)}


def _valid_gap() -> dict:
    return _gap(REAL_DATA_START_US + timedelta(hours=1), REAL_DATA_START_US + timedelta(hours=5))


def test_malformed_row_rejects_entire_ledger(tmp_path):
    # 23 valid rows + 1 malformed row => None (coverage UNKNOWN, fail-closed).
    gaps = [_valid_gap() for _ in range(23)]
    gaps.append({"start": "not-a-timestamp", "end": _iso(REAL_DATA_START_US + timedelta(hours=2))})
    p = tmp_path / "ledger.json"
    p.write_text(json.dumps({"gaps": gaps}))
    assert _load_known_gaps(p) is None


def test_naive_timestamp_rejects_ledger(tmp_path):
    gaps = [_valid_gap(), {"start": "2026-10-14T05:00:00", "end": "2026-10-14T06:00:00"}]  # naive
    p = tmp_path / "ledger.json"
    p.write_text(json.dumps({"gaps": gaps}))
    assert _load_known_gaps(p) is None


def test_end_le_start_rejects_ledger(tmp_path):
    gaps = [_valid_gap(), _gap(REAL_DATA_START_US + timedelta(hours=5), REAL_DATA_START_US + timedelta(hours=3))]
    p = tmp_path / "ledger.json"
    p.write_text(json.dumps({"gaps": gaps}))
    assert _load_known_gaps(p) is None


def test_overlapping_gaps_union():
    as_of = REAL_DATA_START_US + timedelta(days=10)
    gaps = [
        _gap(REAL_DATA_START_US + timedelta(hours=1), REAL_DATA_START_US + timedelta(hours=5)),
        _gap(REAL_DATA_START_US + timedelta(hours=3), REAL_DATA_START_US + timedelta(hours=7)),
    ]
    assert abs(_union_unobserved_seconds(as_of, gaps) - 6 * 3600) < 1


def test_future_gaps_do_not_reduce_coverage():
    as_of = REAL_DATA_START_US + timedelta(days=1)
    gaps = [_gap(REAL_DATA_START_US + timedelta(days=2), REAL_DATA_START_US + timedelta(days=3))]
    assert _union_unobserved_seconds(as_of, gaps) == 0.0
    assert _observed_coverage(as_of, gaps) == 1.0


def test_pre_start_gaps_clip_at_real_data_start():
    as_of = REAL_DATA_START_US + timedelta(days=1)
    gaps = [_gap(REAL_DATA_START_US - timedelta(days=5), REAL_DATA_START_US + timedelta(hours=2))]
    assert abs(_union_unobserved_seconds(as_of, gaps) - 2 * 3600) < 1


def test_ledger_byte_change_changes_hash(tmp_path):
    p = tmp_path / "ledger.json"
    p.write_text(json.dumps({"gaps": []}))
    h1 = _ledger_sha256(p)
    p.write_text(json.dumps({"gaps": [_valid_gap()]}))
    h2 = _ledger_sha256(p)
    assert h1 != h2


def test_observed_coverage_consistent_with_union():
    as_of = REAL_DATA_START_US + timedelta(days=10)
    gaps = [_valid_gap()]
    elapsed = (as_of - REAL_DATA_START_US).total_seconds()
    unobs = _union_unobserved_seconds(as_of, gaps)
    assert abs(_observed_coverage(as_of, gaps) - (1.0 - unobs / elapsed)) < 1e-9