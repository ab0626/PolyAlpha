"""Cross-market propagation estimator (point-in-time, log-odds space).

For a directed edge A -> B, estimate the linear relation

    dLogit(B) = alpha + beta * dLogit(A) + eps

using ONLY shocks whose time is strictly before the evaluation timestamp, with
event-cluster weighting (a cluster with many shocks does not dominate) and ridge
regularization. The hierarchy is shock -> primary market -> directed event-graph
neighbors; this module never does all-pairs correlation mining.

Output is not merely the lag residual; it is the standardized

    Z_lag = (expected - observed) / residual_scale,

with support gates (min events, min clusters) and full training metadata, so the
family harness gets a statistically interpretable quantity to test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np


@dataclass(frozen=True)
class PropagationSample:
    """One (source move, target move) observation at one shock."""

    shock_id: str
    shock_time: datetime
    cluster_id: str
    source_delta: float  # dLogit(A)
    target_delta: float  # dLogit(B)


@dataclass(frozen=True)
class PropagationFit:
    source_market: str
    target_market: str
    regime: str
    alpha: float
    beta: float
    std_error: float       # standard error of beta
    residual_scale: float  # weighted residual sigma (denominator of Z_lag)
    n_events: int
    n_clusters: int
    training_start: datetime
    training_end: datetime
    estimator_version: str

    def predict(self, source_delta: float) -> float:
        return self.alpha + self.beta * source_delta


@dataclass(frozen=True)
class PropagationPrediction:
    fit: PropagationFit
    source_delta: float
    observed_delta: float | None
    expected_delta: float
    lag_residual: float | None     # expected - observed
    z_lag: float | None            # (expected - observed) / residual_scale
    support_ok: bool
    support_reasons: tuple[str, ...] = ()

    def summary(self) -> dict:
        return {
            "source_market": self.fit.source_market,
            "target_market": self.fit.target_market,
            "regime": self.fit.regime,
            "source_delta": round(self.source_delta, 6),
            "observed_delta": round(self.observed_delta, 6) if self.observed_delta is not None else None,
            "expected_delta": round(self.expected_delta, 6),
            "lag_residual": round(self.lag_residual, 6) if self.lag_residual is not None else None,
            "z_lag": round(self.z_lag, 6) if self.z_lag is not None else None,
            "beta": round(self.fit.beta, 6),
            "n_events": self.fit.n_events,
            "n_clusters": self.fit.n_clusters,
            "support_ok": self.support_ok,
            "support_reasons": list(self.support_reasons),
            "training_start": self.fit.training_start.isoformat(),
            "training_end": self.fit.training_end.isoformat(),
            "estimator_version": self.fit.estimator_version,
        }


def fit_propagation(
    samples: list[PropagationSample],
    source_market: str,
    target_market: str,
    regime: str,
    eval_time: datetime,
    min_events: int = 20,
    min_clusters: int = 5,
    ridge: float = 1e-3,
    version: str = "propagation-v1",
) -> PropagationFit | None:
    """Fit A -> B using only shocks strictly before ``eval_time``.

    Returns None when a support gate fails (too few events/clusters).
    """
    train = [s for s in samples if s.shock_time < eval_time]
    if len(train) < min_events or len({s.cluster_id for s in train}) < min_clusters:
        return None

    clusters: dict[str, list[PropagationSample]] = {}
    for s in train:
        clusters.setdefault(s.cluster_id, []).append(s)

    # Event-cluster weighting: each cluster contributes equal total weight.
    cluster_weight = {c: 1.0 / len(v) for c, v in clusters.items()}
    weights = np.array([cluster_weight[s.cluster_id] for s in train])
    weights = weights / weights.sum()  # normalize

    X = np.column_stack([np.ones(len(train)), np.array([s.source_delta for s in train])])
    y = np.array([s.target_delta for s in train])
    W = np.diag(weights)

    XtW = X.T @ W
    A = XtW @ X + ridge * np.eye(2)
    try:
        beta = np.linalg.solve(A, XtW @ y)
    except np.linalg.LinAlgError:
        return None

    residuals = y - X @ beta
    residual_scale = float(np.sqrt(np.sum(weights * residuals**2) / max(1.0, weights.sum())))

    # Covariance of beta under the weighted OLS approximation.
    cov = np.linalg.inv(A) * (residual_scale**2) if residual_scale > 0 else np.linalg.inv(A)
    std_error = float(np.sqrt(max(0.0, cov[1, 1])))

    return PropagationFit(
        source_market=source_market,
        target_market=target_market,
        regime=regime,
        alpha=float(beta[0]),
        beta=float(beta[1]),
        std_error=std_error,
        residual_scale=residual_scale if residual_scale > 0 else 0.0,
        n_events=len(train),
        n_clusters=len(clusters),
        training_start=min(s.shock_time for s in train),
        training_end=max(s.shock_time for s in train),
        estimator_version=version,
    )


def predict_propagation(
    fit: PropagationFit,
    source_delta: float,
    observed_delta: float | None,
) -> PropagationPrediction:
    expected = fit.predict(source_delta)
    if observed_delta is None:
        return PropagationPrediction(
            fit=fit, source_delta=source_delta, observed_delta=None,
            expected_delta=expected, lag_residual=None, z_lag=None,
            support_ok=True,
        )
    lag_residual = expected - observed_delta
    z_lag = (lag_residual / fit.residual_scale) if fit.residual_scale > 0 else None
    return PropagationPrediction(
        fit=fit, source_delta=source_delta, observed_delta=observed_delta,
        expected_delta=expected, lag_residual=lag_residual, z_lag=z_lag,
        support_ok=True,
    )
