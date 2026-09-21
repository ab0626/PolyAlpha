"""Tests for the information-propagation integration layer: deterministic ids,
restart-safe append, and the shock-time training freeze."""

import sys
from datetime import UTC, datetime, timedelta

import numpy as np

sys.path.insert(0, "src")

from polyalpha.integration import (
    EvidenceStore,
    compute_propagation_evidence,
    propagation_id,
    reaction_id,
    shock_id,
)
from polyalpha.propagation import PropagationSample

T = datetime(2026, 10, 14, 12, 30, tzinfo=UTC)


def test_deterministic_ids():
    a = shock_id("cpi-2026-10-14", T)
    assert a == shock_id("cpi-2026-10-14", T)
    assert a != shock_id("nfp-2026-10-02", T)
    assert reaction_id(a, "m1") == reaction_id(a, "m1")
    assert reaction_id(a, "m1") != reaction_id(a, "m2")
    assert propagation_id(a, "A", "B", 30, "v1") == propagation_id(a, "A", "B", 30, "v1")
    assert propagation_id(a, "A", "B", 30, "v1") != propagation_id(a, "A", "B", 60, "v1")


def test_evidence_store_append_dedup_restart(tmp_path):
    path = tmp_path / "ev.jsonl"
    store = EvidenceStore(path)
    assert store.append({"id": "a", "x": 1}) is True
    assert store.append({"id": "a", "x": 2}) is False  # duplicate id
    assert store.append({"id": "b", "x": 3}) is True
    assert store.count() == 2

    # Restart: a fresh store over the same file sees the persisted ids.
    store2 = EvidenceStore(path)
    assert store2.append({"id": "a", "x": 9}) is False
    assert store2.count() == 2


def _samples(n=40, seed=0):
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        t = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i)
        src = rng.normal(0.0, 0.1)
        tgt = 0.5 * src + rng.normal(0.0, 0.02)
        out.append(PropagationSample(f"s{i}", t, f"c{i % 10}", float(src), float(tgt)))
    return out


def test_propagation_freezes_training_at_shock_time():
    shock_t = datetime(2026, 1, 21, tzinfo=UTC)  # freeze: only days 0..19
    ev = compute_propagation_evidence(
        shock_t, "shk_x", "A", "B", 30, "propagation-v1",
        _samples(), source_delta=0.1, observed_delta=0.03,
    )
    assert ev is not None
    assert ev.edge_exec == "UNAVAILABLE"  # reachable-VWAP path not implemented
    assert ev.z_lag_raw is not None
    assert ev.edge_mid_raw is not None
    assert datetime.fromisoformat(ev.training_end) < shock_t
    assert ev.propagation_id == propagation_id("shk_x", "A", "B", 30, "propagation-v1")


def test_propagation_support_gate_returns_none():
    shock_t = datetime(2026, 1, 21, tzinfo=UTC)
    ev = compute_propagation_evidence(
        shock_t, "shk_x", "A", "B", 30, "propagation-v1",
        _samples(n=5), source_delta=0.1, observed_delta=0.03,
    )
    assert ev is None  # < 20 events -> no signal (support gate)
