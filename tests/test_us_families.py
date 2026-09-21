"""Tests for the pre-registered forward-evidence family evaluations.

Covers: the cluster-bootstrap p-value (null-centered), the eval-set temporal
split, and end-to-end verdicts for Family 1 (calibration) on synthetic data.
"""

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

sys.path.insert(0, "src")

from polyalpha.research_dataset import MarketSnapshot, ResearchDataset
from polyalpha.us import families
from polyalpha.us.families import (
    _cluster_bootstrap_p,
    _resolved_eval,
    default_split_date,
    evaluate_family_1_calibration,
)
from polyalpha.us.forward_evidence import forward_evidence_report

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def _snap(market_id, mid, outcome, cluster, observed, hours=None, res_ts=None):
    return MarketSnapshot(
        observation_timestamp=observed,
        market_id=market_id,
        event_id=cluster,
        condition_id=market_id,
        category="sports",
        question="Q",
        yes_token_id=f"{market_id}:LONG",
        no_token_id=f"{market_id}:SHORT",
        yes_mid=D(str(mid)),
        final_resolution=outcome,
        event_cluster=cluster,
        hours_to_resolution=hours,
        resolution_timestamp=res_ts,
    )


def _dataset(snaps):
    return ResearchDataset(
        snapshots=snaps,
        created_at=NOW,
        source_reports=["test"],
        data_hash="x",
    )


def test_cluster_bootstrap_p_detects_effect():
    # All clusters agree on a positive residual -> tiny p-value.
    est, p, n = _cluster_bootstrap_p({"a": [1.0, 1.0], "b": [1.0, 1.0]})
    assert abs(est - 1.0) < 1e-9
    assert p < 0.01
    assert n == 2


def test_cluster_bootstrap_p_null_is_flat():
    # Residuals cancel to zero -> p near 1.
    est, p, n = _cluster_bootstrap_p({"a": [0.5], "b": [-0.5]})
    assert abs(est) < 1e-9
    assert p > 0.9


def test_split_date_is_pre_registered():
    assert default_split_date().isoformat() == "2026-10-31"


def test_resolved_eval_temporal_split():
    split = default_split_date()
    # endDate = observed + hours.
    eval_snap = _snap("m1", 0.5, 1, "c1", NOW, hours=2000, res_ts=NOW + timedelta(days=90))
    train_snap = _snap("m2", 0.5, 1, "c2", NOW, hours=-100, res_ts=NOW + timedelta(days=90))
    post_res = _snap("m3", 0.5, 1, "c3", NOW, hours=2000, res_ts=NOW - timedelta(days=1))

    ds = _dataset([eval_snap, train_snap, post_res])
    out = _resolved_eval(ds, split)
    ids = {s.market_id for s in out}
    assert "m1" in ids       # eval-period, pre-resolution
    assert "m2" not in ids   # resolved before split (train)
    assert "m3" not in ids   # prediction after known outcome


def test_family_1_supported_on_biased_data(monkeypatch):
    monkeypatch.setattr(families, "MIN_EFFECTIVE_N", 2)
    snaps = [
        _snap("m1", 0.40, 1, "c1", NOW, hours=2000, res_ts=NOW + timedelta(days=90)),
        _snap("m2", 0.40, 1, "c2", NOW, hours=2000, res_ts=NOW + timedelta(days=90)),
        _snap("m3", 0.40, 1, "c3", NOW, hours=2000, res_ts=NOW + timedelta(days=90)),
    ]
    d = evaluate_family_1_calibration(_dataset(snaps))
    assert d.verdict == families.VERDICT_SUPPORTED
    assert d.n_eff == 3


def test_family_1_not_supported_on_calibrated_data(monkeypatch):
    monkeypatch.setattr(families, "MIN_EFFECTIVE_N", 2)
    snaps = [
        _snap("m1", 0.50, 1, "c1", NOW, hours=2000, res_ts=NOW + timedelta(days=90)),
        _snap("m2", 0.50, 0, "c2", NOW, hours=2000, res_ts=NOW + timedelta(days=90)),
        _snap("m3", 0.50, 1, "c3", NOW, hours=2000, res_ts=NOW + timedelta(days=90)),
        _snap("m4", 0.50, 0, "c4", NOW, hours=2000, res_ts=NOW + timedelta(days=90)),
    ]
    d = evaluate_family_1_calibration(_dataset(snaps))
    assert d.verdict == families.VERDICT_NOT_SUPPORTED


def test_forward_evidence_pending_on_small_sample():
    snaps = [
        _snap("m1", 0.5, 1, "c1", NOW, hours=2000, res_ts=NOW + timedelta(days=90)),
    ]
    report = forward_evidence_report(_dataset(snaps), as_of=NOW)
    assert report["evaluation_boundary_met"] is False
    assert report["status"] == "PENDING_EVIDENCE"
    assert report["families"] == []
    assert report["gates"]["settled_markets"]["met"] is False
