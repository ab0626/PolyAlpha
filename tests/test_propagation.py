"""Tests for the point-in-time cross-market propagation estimator."""

import sys
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

sys.path.insert(0, "src")

from polyalpha.propagation import (
    PropagationSample,
    fit_propagation,
    predict_propagation,
)


def _samples(n=40, beta=0.5, seed=0):
    rng = np.random.default_rng(seed)
    samples = []
    for i in range(n):
        t = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i)
        src = rng.normal(0.0, 0.1)
        tgt = beta * src + rng.normal(0.0, 0.02)
        samples.append(PropagationSample(f"s{i}", t, f"c{i % 10}", float(src), float(tgt)))
    return samples


def test_fit_reconstructs_beta():
    fit = fit_propagation(_samples(), "A", "B", "macro", datetime(2026, 6, 1, tzinfo=UTC))
    assert fit is not None
    assert abs(fit.beta - 0.5) < 0.05
    assert fit.n_events == 40
    assert fit.n_clusters == 10
    assert fit.residual_scale > 0


def test_support_gate_insufficient_events():
    fit = fit_propagation(_samples(n=5), "A", "B", "macro", datetime(2026, 6, 1, tzinfo=UTC))
    assert fit is None


def test_point_in_time_excludes_future_shocks():
    samples = _samples()
    eval_time = datetime(2026, 1, 21, tzinfo=UTC)
    fit = fit_propagation(samples, "A", "B", "macro", eval_time)
    assert fit is not None
    assert fit.n_events == 20  # days 0..19, strictly before day 20 (Jan 21)
    assert fit.training_end < eval_time


def test_predict_and_z_lag():
    fit = fit_propagation(_samples(), "A", "B", "macro", datetime(2026, 6, 1, tzinfo=UTC))
    pred = predict_propagation(fit, source_delta=0.1, observed_delta=0.03)
    assert pred.expected_delta == pytest.approx(fit.beta * 0.1, abs=0.02)
    assert pred.lag_residual > 0  # B moved less than historical propagation
    assert pred.z_lag == pytest.approx(pred.lag_residual / fit.residual_scale, abs=1e-9)
    assert pred.support_ok is True
