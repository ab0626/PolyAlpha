"""Adversarial tests for the E4 derived evidence classifier (future-only cutover)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from polyalpha.energy.e4_evidence import (
    E4_CUTOVER_AT,
    E4_SPEC_COMMIT,
    EVIDENCE_BOOTSTRAP,
    EVIDENCE_PROSPECTIVE,
    EVIDENCE_RECEIPT_PRE,
    EVIDENCE_REVISION,
    EVIDENCE_UNRESOLVED,
    classify,
    classify_observation,
    interval_end_utc,
)

C = E4_CUTOVER_AT


def test_interval_one_second_before_cutover_is_bootstrap():
    # Downloaded afterward, but the interval ended 1s before the cutover.
    cls, _ = classify(C + timedelta(minutes=30), C - timedelta(seconds=1), True)
    assert cls == EVIDENCE_BOOTSTRAP


def test_interval_after_cutover_first_seen_is_prospective():
    cls, _ = classify(C + timedelta(minutes=30), C + timedelta(seconds=1), True)
    assert cls == EVIDENCE_PROSPECTIVE


def test_exact_equality_to_cutover_is_non_prospective():
    cls, _ = classify(C + timedelta(minutes=30), C, True)
    assert cls == EVIDENCE_BOOTSTRAP


def test_pre_cutover_interval_revised_afterward_is_non_prospective():
    # A revision (not first-seen) of an interval that ended before the cutover.
    cls, _ = classify(C + timedelta(hours=1), C - timedelta(minutes=5), False)
    assert cls == EVIDENCE_BOOTSTRAP


def test_post_cutover_revision_is_non_prospective():
    cls, _ = classify(C + timedelta(hours=1), C + timedelta(minutes=30), False)
    assert cls == EVIDENCE_REVISION


def test_receipt_pre_cutover_is_non_prospective():
    cls, _ = classify(C - timedelta(seconds=1), C + timedelta(hours=1), True)
    assert cls == EVIDENCE_RECEIPT_PRE


def test_unresolved_interval_fails_closed():
    cls, _ = classify(C + timedelta(hours=1), None, True)
    assert cls == EVIDENCE_UNRESOLVED


def test_unresolved_receipt_fails_closed():
    cls, _ = classify(None, C + timedelta(hours=1), True)
    assert cls == EVIDENCE_UNRESOLVED


def test_interval_end_mapping_before_cutover():
    obs = {"operating_day": "2026-10-19", "delivery_hour": "10",
           "delivery_interval": "4", "repeated_hour_flag": "N"}
    assert interval_end_utc(obs) == datetime(2026, 10, 19, 15, 0, 0, tzinfo=UTC)


def test_interval_end_mapping_after_cutover():
    obs = {"operating_day": "2026-10-19", "delivery_hour": "11",
           "delivery_interval": "1", "repeated_hour_flag": "N"}
    assert interval_end_utc(obs) == datetime(2026, 10, 19, 15, 15, 0, tzinfo=UTC)


def test_dst_repeated_hour_is_unresolved():
    obs = {"operating_day": "2026-11-01", "delivery_hour": "1",
           "delivery_interval": "1", "repeated_hour_flag": "Y"}
    assert interval_end_utc(obs) is None


def test_malformed_hour_is_unresolved():
    obs = {"operating_day": "2026-10-19", "delivery_hour": "25",
           "delivery_interval": "1", "repeated_hour_flag": "N"}
    assert interval_end_utc(obs) is None


def test_classify_observation_produces_full_record():
    obs = {
        "operating_day": "2026-10-19",
        "delivery_hour": "11",
        "delivery_interval": "1",
        "repeated_hour_flag": "N",
        "hub": "HB_WEST",
        "price": "24.34",
        "natural_key": "2026-10-19|11|1|N|HB_WEST",
        "received_at": "2026-10-19T16:00:00+00:00",
        "is_first_seen": True,
        "raw_payload_sha256": "abc",
    }
    rec = classify_observation(obs)
    assert rec["evidence_class"] == EVIDENCE_PROSPECTIVE
    assert rec["qualifies_forward_e4"] is True
    assert rec["e4_spec_commit"] == E4_SPEC_COMMIT
    assert rec["interval_end_utc"] == "2026-10-19T15:15:00+00:00"
    assert rec["source_row_hash"]