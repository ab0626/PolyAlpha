"""Tests for marginal Expected Shortfall / CVaR risk attribution.

Covers: the Euler full-allocation identity, the finite-difference cross-check
against the conditional-tail estimator, the diversification insight (correlated
contracts carry higher marginal ES), and the risk-adjusted sizing LP versus
naive per-market caps under a shared-shock (cluster-correlation) scenario.
"""

import sys

sys.path.insert(0, "src")

import numpy as np

from polyalpha.marginal_risk import (
    BinaryContract,
    euler_contributions,
    expected_shortfall,
    finite_difference_marginal_es,
    marginal_es_conditional_tail,
    min_es_sizing,
    risk_adjusted_sizing,
    rollup,
    simulate_loss_matrix,
)

ALPHA = 0.95


def _contracts():
    """5 correlated contracts (clusterA) + 5 independent (clusterB), same edge/price."""
    contracts = []
    for i in range(5):
        contracts.append(
            BinaryContract(f"a{i}", f"evA{i}", "clusterA", "politics", 0.5, 0.45, rho_cluster=0.7)
        )
    for i in range(5):
        contracts.append(
            BinaryContract(f"b{i}", f"evB{i}", "clusterB", "sports", 0.5, 0.45, rho_cluster=0.0)
        )
    return contracts


def test_euler_full_allocation():
    contracts = _contracts()
    loss_matrix = simulate_loss_matrix(contracts, 20000, seed=0)
    x = np.full(len(contracts), 100.0)
    es, _ = expected_shortfall(loss_matrix, x, ALPHA)
    marginal = marginal_es_conditional_tail(contracts, loss_matrix, x, ALPHA)
    positions = {c.contract_id: 100.0 for c in contracts}
    rc = euler_contributions(contracts, positions, marginal)
    # Positive homogeneity: sum_i x_i * dES/dx_i == ES (identity for the tail-mean).
    assert abs(sum(rc.values()) - es) < 1e-9 * max(1.0, abs(es))


def test_finite_difference_matches_conditional_tail():
    contracts = _contracts()
    loss_matrix = simulate_loss_matrix(contracts, 20000, seed=1)
    x = np.full(len(contracts), 100.0)
    cond = marginal_es_conditional_tail(contracts, loss_matrix, x, ALPHA)
    fd = finite_difference_marginal_es(contracts, loss_matrix, x, ALPHA, h=1e-4)
    for c in contracts:
        assert abs(fd[c.contract_id] - cond[c.contract_id]) < 0.05


def test_correlated_contracts_carry_higher_marginal_es():
    contracts = _contracts()
    loss_matrix = simulate_loss_matrix(contracts, 20000, seed=2)
    x = np.full(len(contracts), 100.0)
    marginal = marginal_es_conditional_tail(contracts, loss_matrix, x, ALPHA)
    # Both clusters contribute to the tail, but the correlated contract's
    # tail-conditional loss is higher: it loads onto the shared cluster shock.
    assert marginal["a0"] > marginal["b0"]
    # The correlated marginal ES sits near its full-stake loss (0.45); the
    # independent one is materially lower (its loss is diversified in the tail).
    assert marginal["a0"] > 0.4
    assert marginal["b0"] < 0.4


def test_rollup_aggregates_by_cluster_and_category():
    contracts = _contracts()
    rc = {c.contract_id: float(i + 1) for i, c in enumerate(contracts)}
    agg = rollup(contracts, rc)
    assert agg["cluster"]["clusterA"] == sum(range(1, 6))
    assert agg["cluster"]["clusterB"] == sum(range(6, 11))
    assert agg["category"]["politics"] == sum(range(1, 6))
    assert agg["category"]["sports"] == sum(range(6, 11))


def test_marginal_es_sizing_beats_naive_caps_under_shared_shock():
    """For a fixed expected-PnL, marginal-ES sizing minimizes tail risk by
    de-concentrating from the correlated cluster (the shared-shock scenario)."""
    contracts = _contracts()
    loss_matrix = simulate_loss_matrix(contracts, 3000, seed=3)
    caps = {c.contract_id: 100.0 for c in contracts}

    # Naive: equal per-market caps (50 shares each = 500 total shares).
    naive_x = np.full(len(contracts), 50.0)
    naive_es, _ = expected_shortfall(loss_matrix, naive_x, ALPHA)
    naive_pnl = float(sum(c.edge * 50.0 for c in contracts))

    # Marginal-ES sizing: same edge target, minimize ES (efficient frontier).
    result = min_es_sizing(contracts, loss_matrix, ALPHA, edge_target=naive_pnl, caps=caps)

    assert result.expected_pnl >= naive_pnl - 1e-6  # meets the edge floor
    assert result.expected_shortfall < naive_es      # lower tail risk at same edge
    # De-concentrates from the correlated cluster.
    cluster_a_lp = sum(result.positions[f"a{i}"] for i in range(5))
    assert cluster_a_lp < 5 * 50.0


def test_risk_adjusted_sizing_is_bounded_and_consistent():
    contracts = _contracts()
    loss_matrix = simulate_loss_matrix(contracts, 3000, seed=4)
    caps = {c.contract_id: 100.0 for c in contracts}
    lam = 0.01
    result = risk_adjusted_sizing(contracts, loss_matrix, ALPHA, lam=lam, caps=caps)
    for c in contracts:
        assert 0.0 <= result.positions[c.contract_id] <= 100.0 + 1e-9
    # Objective consistency: E[PnL] - lam * ES.
    assert abs(result.objective - (result.expected_pnl - lam * result.expected_shortfall)) < 1e-9
