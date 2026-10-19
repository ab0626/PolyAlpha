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
    _binned_ece,
    _cluster_bootstrap_p,
    _price_bucket,
    _resolved_eval,
    default_split_date,
    evaluate_family_1_calibration,
    evaluate_family_1_ttr,
    evaluate_family_2_longshot,
    evaluate_family_3_microstructure,
)
from polyalpha.us.forward_evidence import forward_evidence_report

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
NEAR_SPLIT = datetime(2026, 10, 31, 0, 0, tzinfo=UTC)


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
    monkeypatch.setattr(families, "MIN_CELL_CLUSTERS", 2)
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
    monkeypatch.setattr(families, "MIN_CELL_CLUSTERS", 2)
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


# ── Adversarial tests (would have caught the v2.3-corrected bugs) ───────────

def _n_snaps(n, mid, outcome, hours=2000):
    return [
        _snap(f"m{i}", mid, outcome, f"c{i}", NOW, hours=hours, res_ts=NOW + timedelta(days=90))
        for i in range(n)
    ]


def test_f1_ece_is_zero_for_calibrated_market(monkeypatch):
    # A perfectly calibrated 50/50 market must report ECE ~ 0, not mean|Y-p| = 0.5.
    monkeypatch.setattr(families, "MIN_EFFECTIVE_N", 2)
    monkeypatch.setattr(families, "MIN_CELL_CLUSTERS", 2)
    snaps = [
        _snap(f"m{i}", 0.50, i % 2, f"c{i}", NOW, hours=2000, res_ts=NOW + timedelta(days=90))
        for i in range(20)
    ]
    d = evaluate_family_1_calibration(_dataset(snaps))
    assert d.primary_metric.startswith("ECE=0.0000")
    assert d.verdict == families.VERDICT_NOT_SUPPORTED


def test_f1_binned_ece_not_row_mae():
    snaps = [
        _snap("a", 0.50, 1, "c1", NOW, hours=2000),
        _snap("b", 0.50, 0, "c2", NOW, hours=2000),
    ]
    ece = _binned_ece(snaps, lambda s: _price_bucket(float(s.yes_mid)))
    # mean|Y-p| would be 0.5; binned ECE (both in same cell) is 0.0.
    assert abs(ece) < 1e-9


def test_f2_positive_low_price_does_not_support_flb(monkeypatch):
    monkeypatch.setattr(families, "MIN_EFFECTIVE_N", 2)
    monkeypatch.setattr(families, "MIN_CELL_CLUSTERS", 2)
    snaps = _n_snaps(20, 0.05, 1)  # low price resolves YES => positive residual
    d = evaluate_family_2_longshot(_dataset(snaps))
    assert d.verdict == families.VERDICT_NOT_SUPPORTED


def test_f2_negative_low_price_supports_flb(monkeypatch):
    monkeypatch.setattr(families, "MIN_EFFECTIVE_N", 2)
    monkeypatch.setattr(families, "MIN_CELL_CLUSTERS", 2)
    snaps = _n_snaps(20, 0.05, 0)  # low price resolves NO => negative residual
    d = evaluate_family_2_longshot(_dataset(snaps))
    assert d.verdict == families.VERDICT_SUPPORTED


def test_f2_high_price_residual_does_not_support_flb(monkeypatch):
    monkeypatch.setattr(families, "MIN_EFFECTIVE_N", 2)
    monkeypatch.setattr(families, "MIN_CELL_CLUSTERS", 2)
    snaps = _n_snaps(20, 0.70, 1)  # high price resolves YES => positive residual
    d = evaluate_family_2_longshot(_dataset(snaps))
    assert d.verdict == families.VERDICT_NOT_SUPPORTED


def test_ttr_decreasing_does_not_support(monkeypatch):
    monkeypatch.setattr(families, "MIN_EFFECTIVE_N", 2)
    monkeypatch.setattr(families, "MIN_CELL_CLUSTERS", 2)
    snaps = []
    # near (<1h): large |residual|; far (>7d): small |residual| => decreasing.
    for i in range(20):
        snaps.append(_snap(f"n{i}", 0.90, 0, f"cn{i}", NEAR_SPLIT, hours=0.5, res_ts=NEAR_SPLIT + timedelta(days=1)))
    for i in range(20):
        snaps.append(_snap(f"f{i}", 0.20, 0, f"cf{i}", NEAR_SPLIT, hours=200, res_ts=NEAR_SPLIT + timedelta(days=10)))
    d = evaluate_family_1_ttr(_dataset(snaps))
    assert d.verdict == families.VERDICT_NOT_SUPPORTED


def test_ttr_increasing_supports(monkeypatch):
    monkeypatch.setattr(families, "MIN_EFFECTIVE_N", 2)
    monkeypatch.setattr(families, "MIN_CELL_CLUSTERS", 2)
    snaps = []
    # near (<1h): small |residual|; far (>7d): large |residual| => increasing.
    for i in range(20):
        snaps.append(_snap(f"n{i}", 0.20, 0, f"cn{i}", NEAR_SPLIT, hours=0.5, res_ts=NEAR_SPLIT + timedelta(days=1)))
    for i in range(20):
        snaps.append(_snap(f"f{i}", 0.90, 0, f"cf{i}", NEAR_SPLIT, hours=200, res_ts=NEAR_SPLIT + timedelta(days=10)))
    d = evaluate_family_1_ttr(_dataset(snaps))
    assert d.verdict == families.VERDICT_SUPPORTED


def test_ttr_empty_bucket_is_insufficient(monkeypatch):
    monkeypatch.setattr(families, "MIN_EFFECTIVE_N", 2)
    monkeypatch.setattr(families, "MIN_CELL_CLUSTERS", 2)
    snaps = _n_snaps(20, 0.20, 0, hours=0.5)  # only <1h present
    d = evaluate_family_1_ttr(_dataset(snaps))
    assert d.verdict == families.VERDICT_INSUFFICIENT


def test_f3_is_not_run():
    d = evaluate_family_3_microstructure(_dataset([]))
    assert d.verdict == families.VERDICT_NOT_RUN


def test_settled_gate_counts_eval_set_not_all_resolved():
    # 5 resolved TRAIN markets (endDate < split), zero eval-period resolved.
    snaps = [
        _snap(f"m{i}", 0.5, 1, f"c{i}", NOW, hours=-100, res_ts=NOW + timedelta(days=90))
        for i in range(5)
    ]
    report = forward_evidence_report(_dataset(snaps), as_of=NOW)
    assert report["gates"]["settled_markets"]["value"] == 0
