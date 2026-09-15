"""Live-readiness promotion gate.

Part of the live-ready execution architecture. A strategy is NOT promoted
merely because it is profitable. Promotion requires evidence across
forecasting, execution, statistics, robustness, risk, forward validation,
holdout, and data integrity. This gate is advisory and produces an auditable
report; deployment still requires explicit operator approval.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PromotionCriterion:
    domain: str
    name: str
    passed: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {"domain": self.domain, "name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class PromotionReport:
    criteria: list[PromotionCriterion]

    @property
    def promoted(self) -> bool:
        return all(c.passed for c in self.criteria)

    @property
    def failed_count(self) -> int:
        return sum(1 for c in self.criteria if not c.passed)

    def by_domain(self) -> dict[str, list[PromotionCriterion]]:
        out: dict[str, list[PromotionCriterion]] = {}
        for c in self.criteria:
            out.setdefault(c.domain, []).append(c)
        return out

    def as_dict(self) -> dict:
        return {
            "promoted": self.promoted,
            "failed_count": self.failed_count,
            "criteria": [c.as_dict() for c in self.criteria],
        }


@dataclass
class LiveReadinessGate:
    """Evaluates the evidence required before live deployment is considered."""

    criteria: list[PromotionCriterion] = field(default_factory=list)

    def add(self, domain: str, name: str, passed: bool, detail: str = "") -> None:
        self.criteria.append(PromotionCriterion(domain, name, passed, detail))

    def evaluate(
        self,
        delta_brier_positive: bool,
        calibration_acceptable: bool,
        disagreement_stable: bool,
        edge_realization_acceptable: bool,
        paper_vs_backtest_discrepancy_ok: bool,
        execution_model_calibrated: bool,
        effective_n_adequate: bool,
        bootstrap_evidence_positive: bool,
        multiple_testing_aware: bool,
        cost_ladder_survives: bool,
        latency_survives: bool,
        parameter_stable: bool,
        monte_carlo_survives: bool,
        drawdown_acceptable: bool,
        concentration_acceptable: bool,
        exposure_caps_functioning: bool,
        forward_shadow_positive: bool,
        forward_paper_positive: bool,
        final_holdout_passed: bool,
        collector_fidelity_healthy: bool,
        no_integrity_failures: bool,
    ) -> PromotionReport:
        self.criteria = []
        # Forecasting
        self.add("forecasting", "positive_delta_brier", delta_brier_positive)
        self.add("forecasting", "calibration_acceptable", calibration_acceptable)
        self.add("forecasting", "disagreement_stable", disagreement_stable)
        # Execution
        self.add("execution", "edge_realization_acceptable", edge_realization_acceptable)
        self.add("execution", "paper_vs_backtest_ok", paper_vs_backtest_discrepancy_ok)
        self.add("execution", "execution_model_calibrated", execution_model_calibrated)
        # Statistics
        self.add("statistics", "effective_n_adequate", effective_n_adequate)
        self.add("statistics", "bootstrap_evidence_positive", bootstrap_evidence_positive)
        self.add("statistics", "multiple_testing_aware", multiple_testing_aware)
        # Robustness
        self.add("robustness", "cost_ladder_survives", cost_ladder_survives)
        self.add("robustness", "latency_survives", latency_survives)
        self.add("robustness", "parameter_stable", parameter_stable)
        self.add("robustness", "monte_carlo_survives", monte_carlo_survives)
        # Risk
        self.add("risk", "drawdown_acceptable", drawdown_acceptable)
        self.add("risk", "concentration_acceptable", concentration_acceptable)
        self.add("risk", "exposure_caps_functioning", exposure_caps_functioning)
        # Forward validation
        self.add("forward", "shadow_positive", forward_shadow_positive)
        self.add("forward", "paper_positive", forward_paper_positive)
        # Holdout
        self.add("holdout", "final_holdout_passed", final_holdout_passed)
        # Data
        self.add("data", "collector_fidelity_healthy", collector_fidelity_healthy)
        self.add("data", "no_integrity_failures", no_integrity_failures)
        return PromotionReport(criteria=list(self.criteria))