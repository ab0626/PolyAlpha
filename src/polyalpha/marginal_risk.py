"""Marginal Expected Shortfall / CVaR risk attribution for portfolios of
correlated binary prediction-market contracts.

Implements the Euler (gradient) allocation of VaR/ES over a DISCRETE loss
distribution, following arXiv:2404.09646 ("Derivatives of Risk Measures").

Results used (paper's Section 2, discrete case):
  - dVaR/dx_i = E[L_i | L = VaR]                              (quantile conditioning)
  - dES/dx_i  = (1/(1-a)) E[L_i 1{L>=VaR}] - dVaR/dx_i (P[L>=VaR] - (1-a))  (eq 2.28)
  - d^2 ES / dx_i dx_j = 0  (ES is piecewise-linear in weights, eq 2.29)

Because each collateralized contract has Y_i in {0,1}, the per-unit loss is an
atom, so the DISCRETE formulas are the primary tool (not a fallback); the
continuous-case ``E[L_i | L >= VaR]`` is the conditional-tail-mean estimator,
and we cross-check it with a finite-difference derivative to surface the
quantile-atom correction term.

The joint-outcome model is a hierarchical latent-factor (Gaussian) model seeded
from the event-cluster hierarchy: a global factor (market-wide shock), a
per-cluster factor (shared event shock), and an idiosyncratic term. This is the
same discrete setting as credit-portfolio loss models (Glasserman 2005, which
the paper cites).

The risk-adjusted sizing step uses the Rockafellar-Uryasev CVaR linear program
(minimize CVaR under a linear-loss scenario set); its KKT conditions are exactly
the Euler marginal allocations above, so the marginal-ES attribution and the
sizing solve are mutually consistent.

This module is additive and is NOT wired into any live sizing path.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np
from scipy.optimize import linprog
from scipy.stats import norm


def _clamp_prob(p: float) -> float:
    return min(max(p, 1e-9), 1.0 - 1e-9)


@dataclass(frozen=True)
class BinaryContract:
    """A single binary contract and its risk-model inputs.

    ``probability`` is the fair P(Y=1); ``price`` is the cost per share for a
    long-YES position; ``rho_cluster`` is the loading on the contract's cluster
    factor (0..1, with the global loading reserved by the caller).
    """

    contract_id: str
    event_id: str
    cluster: str
    category: str
    probability: float
    price: float
    rho_cluster: float = 0.5

    @property
    def edge(self) -> float:
        """Expected PnL per share (positive = underpriced relative to fair)."""
        return self.probability - self.price


def simulate_loss_matrix(
    contracts: list[BinaryContract],
    n_scenarios: int,
    seed: int = 0,
    rho_global: float = 0.0,
) -> np.ndarray:
    """Simulate the per-unit loss matrix (shape (n_scenarios, n_contracts)).

    Per-unit loss for a long-YES position: L_i = price_i - Y_i, so L_i is in
    {price_i - 1, price_i}. Portfolio loss is linear in shares: L = sum x_i L_i.
    """
    if n_scenarios < 1:
        raise ValueError("n_scenarios must be positive")
    rng = np.random.default_rng(seed)
    d = len(contracts)

    clusters: dict[str, list[int]] = defaultdict(list)
    for idx, c in enumerate(contracts):
        clusters[c.cluster].append(idx)

    global_factor = rng.standard_normal(n_scenarios)
    sqrt_rho_global = np.sqrt(rho_global)
    thresholds = norm.ppf([_clamp_prob(c.probability) for c in contracts])

    latent = np.empty((n_scenarios, d))
    for cluster, idxs in clusters.items():
        cluster_factor = rng.standard_normal(n_scenarios)
        for idx in idxs:
            c = contracts[idx]
            rho_c = min(max(c.rho_cluster, 0.0), 1.0 - rho_global)
            idiosyncratic = rng.standard_normal(n_scenarios)
            residual_var = max(1.0 - rho_global - rho_c, 0.0)
            latent[:, idx] = (
                sqrt_rho_global * global_factor
                + np.sqrt(rho_c) * cluster_factor
                + np.sqrt(residual_var) * idiosyncratic
            )

    outcome = (latent <= thresholds[None, :]).astype(float)  # Y_i = 1[Z_i <= Phi^-1(p_i)]
    prices = np.array([c.price for c in contracts])
    return prices[None, :] - outcome  # L_i = price_i - Y_i


def _tail_stats(losses: np.ndarray, alpha: float) -> tuple[float, float, np.ndarray]:
    """Return (ES, VaR, tail_indices) for the empirical worst-(1-alpha) fraction."""
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    order = np.argsort(losses)[::-1]  # worst first
    k = max(1, int(np.ceil((1.0 - alpha) * len(losses))))
    tail_idx = order[:k]
    es = float(np.mean(losses[tail_idx]))
    var = float(losses[tail_idx[-1]])
    return es, var, tail_idx


def expected_shortfall(
    loss_matrix: np.ndarray, x: np.ndarray | list[float], alpha: float
) -> tuple[float, float]:
    """Empirical ES (CVaR) and VaR of the portfolio loss ``loss_matrix @ x``."""
    losses = loss_matrix @ np.asarray(x, dtype=float)
    es, var, _ = _tail_stats(losses, alpha)
    return es, var


def marginal_es_conditional_tail(
    contracts: list[BinaryContract],
    loss_matrix: np.ndarray,
    x: np.ndarray | list[float],
    alpha: float,
) -> dict[str, float]:
    """Euler marginal ES via the conditional-tail mean E[L_i | L >= VaR].

    This is the continuous-case estimator; for discrete atoms at the VaR the
    paper's eq. 2.28 adds a correction term. Cross-check with
    ``finite_difference_marginal_es``.
    """
    x = np.asarray(x, dtype=float)
    losses = loss_matrix @ x
    _, _, tail_idx = _tail_stats(losses, alpha)
    tail_losses = loss_matrix[tail_idx]
    return {c.contract_id: float(np.mean(tail_losses[:, i])) for i, c in enumerate(contracts)}


def finite_difference_marginal_es(
    contracts: list[BinaryContract],
    loss_matrix: np.ndarray,
    x: np.ndarray | list[float],
    alpha: float,
    h: float = 1e-4,
) -> dict[str, float]:
    """Central finite-difference marginal ES (discrete-robust ground truth)."""
    x = np.asarray(x, dtype=float)
    grads: dict[str, float] = {}
    for i, c in enumerate(contracts):
        xp = x.copy()
        xm = x.copy()
        xp[i] += h
        xm[i] -= h
        es_p, _ = expected_shortfall(loss_matrix, xp, alpha)
        es_m, _ = expected_shortfall(loss_matrix, xm, alpha)
        grads[c.contract_id] = (es_p - es_m) / (2.0 * h)
    return grads


def euler_contributions(
    contracts: list[BinaryContract],
    positions: dict[str, float],
    marginal: dict[str, float],
) -> dict[str, float]:
    """Risk contributions RC_i = x_i * dES/dx_i (sums to ES by positive homogeneity)."""
    return {c.contract_id: positions.get(c.contract_id, 0.0) * marginal.get(c.contract_id, 0.0)
            for c in contracts}


def rollup(
    contracts: list[BinaryContract], contributions: dict[str, float]
) -> dict[str, dict[str, float]]:
    """Aggregate risk contributions contract -> event -> cluster -> category."""
    by_event: dict[str, float] = defaultdict(float)
    by_cluster: dict[str, float] = defaultdict(float)
    by_category: dict[str, float] = defaultdict(float)
    for c in contracts:
        rc = contributions.get(c.contract_id, 0.0)
        by_event[c.event_id] += rc
        by_cluster[c.cluster] += rc
        by_category[c.category] += rc
    return {"event": dict(by_event), "cluster": dict(by_cluster), "category": dict(by_category)}


@dataclass(frozen=True)
class SizingResult:
    positions: dict[str, float]
    expected_pnl: float
    expected_shortfall: float
    objective: float

    def summary(self) -> dict:
        return {
            "positions": {k: round(v, 6) for k, v in self.positions.items()},
            "expected_pnl": round(self.expected_pnl, 6),
            "expected_shortfall": round(self.expected_shortfall, 6),
            "objective": round(self.objective, 6),
        }


def risk_adjusted_sizing(
    contracts: list[BinaryContract],
    loss_matrix: np.ndarray,
    alpha: float,
    lam: float,
    caps: dict[str, float] | None = None,
) -> SizingResult:
    """Risk-adjusted sizing: maximize E[PnL] - lam * ES via the CVaR LP.

    Rockafellar-Uryasev formulation (minimize over x, t, u):
        lam * (t + 1/(1-alpha) * 1/N * sum u_s) - sum edge_i x_i
        s.t. u_s >= sum_i x_i L_{i,s} - t,  u_s >= 0,  0 <= x_i <= cap_i
    """
    if lam < 0:
        raise ValueError("lam must be non-negative")
    d = len(contracts)
    n_scenarios = loss_matrix.shape[0]
    edges = np.array([c.edge for c in contracts], dtype=float)

    # Variable layout: [x (d), t (1), u (N)]
    c_obj = np.zeros(d + 1 + n_scenarios)
    c_obj[:d] = -edges
    c_obj[d] = lam
    c_obj[d + 1 :] = lam * (1.0 / (1.0 - alpha)) / n_scenarios

    A_ub = np.zeros((n_scenarios, d + 1 + n_scenarios))
    A_ub[:, :d] = loss_matrix
    A_ub[:, d] = -1.0
    A_ub[np.arange(n_scenarios), d + 1 + np.arange(n_scenarios)] = -1.0
    b_ub = np.zeros(n_scenarios)

    bounds = [(0.0, None)] * d
    for i, c in enumerate(contracts):
        cap = caps.get(c.contract_id) if caps else None
        bounds[i] = (0.0, cap)
    bounds.append((None, None))  # t free
    bounds.extend([(0.0, None)] * n_scenarios)  # u >= 0

    res = linprog(c_obj, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method="highs")
    if not res.success:
        raise RuntimeError(f"sizing LP failed: {res.message}")

    x = res.x[:d]
    positions = {c.contract_id: float(x[i]) for i, c in enumerate(contracts)}
    expected_pnl = float(np.dot(edges, x))
    es, _ = expected_shortfall(loss_matrix, x, alpha)
    objective = expected_pnl - lam * es
    return SizingResult(positions=positions, expected_pnl=expected_pnl,
                        expected_shortfall=es, objective=objective)


def min_es_sizing(
    contracts: list[BinaryContract],
    loss_matrix: np.ndarray,
    alpha: float,
    edge_target: float,
    caps: dict[str, float] | None = None,
) -> SizingResult:
    """Minimize ES subject to an expected-PnL floor (efficient-frontier form).

    Rockafellar-Uryasev LP with an added edge constraint:
        minimize t + 1/(1-a) * 1/N * sum u_s
        s.t. u_s >= sum_i x_i L_{i,s} - t,  u_s >= 0,  0 <= x_i <= cap_i,
             sum_i edge_i x_i >= edge_target

    This is the clean way to answer "for a fixed edge, what allocation
    minimizes tail risk?", which marginal-ES attribution (the paper's Euler
    gradients) implies: shift capital toward low-marginal-ES contracts.
    """
    d = len(contracts)
    n = loss_matrix.shape[0]
    edges = np.array([c.edge for c in contracts], dtype=float)

    c_obj = np.zeros(d + 1 + n)
    c_obj[d] = 1.0
    c_obj[d + 1 :] = (1.0 / (1.0 - alpha)) / n

    A_ub = np.zeros((n + 1, d + 1 + n))
    A_ub[:n, :d] = loss_matrix
    A_ub[:n, d] = -1.0
    A_ub[np.arange(n), d + 1 + np.arange(n)] = -1.0
    A_ub[n, :d] = -edges  # -sum edge_i x_i <= -edge_target  =>  sum edge_i x_i >= edge_target
    b_ub = np.zeros(n + 1)
    b_ub[n] = -edge_target

    bounds = [(0.0, caps.get(c.contract_id) if caps else None) for c in contracts]
    bounds.append((None, None))
    bounds.extend([(0.0, None)] * n)

    res = linprog(c_obj, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method="highs")
    if not res.success:
        raise RuntimeError(f"min-ES sizing LP failed: {res.message}")

    x = res.x[:d]
    positions = {c.contract_id: float(x[i]) for i, c in enumerate(contracts)}
    expected_pnl = float(np.dot(edges, x))
    es, _ = expected_shortfall(loss_matrix, x, alpha)
    objective = es
    return SizingResult(positions=positions, expected_pnl=expected_pnl,
                        expected_shortfall=es, objective=objective)
