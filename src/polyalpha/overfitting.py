"""Overfitting detection and multiple testing correction.

Part 39: Detects overfitting through train/test gap analysis, tracks
holdout set usage to prevent data snooping, and computes deflation
ratios for multiple strategy testing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TypeAlias

D = Decimal

HoldoutId: TypeAlias = str


@dataclass(frozen=True)
class TrainTestMetrics:
    """Performance metrics for train or test set."""

    brier_score: float
    log_loss: float
    ece: float
    sample_count: int
    accuracy: float | None = None


@dataclass(frozen=True)
class OverfittingWarning:
    """A single overfitting warning."""

    warning_type: str  # gap, complexity, holdout_abuse, deflation
    severity: str  # low, medium, high, critical
    message: str
    details: dict[str, float | str] = field(default_factory=dict)


@dataclass(frozen=True)
class OverfittingReport:
    """Complete overfitting detection report."""

    train_test_gap: float  # test_brier - train_brier (positive = overfit)
    complexity_ratio: float  # n_parameters / n_observations
    holdout_evaluations: int
    deflation_ratio: float
    warnings: list[OverfittingWarning]
    severity: str  # worst severity across all warnings
    is_overfit: bool  # True if any high/critical warning

    def summary(self) -> dict:
        return {
            "train_test_gap": self.train_test_gap,
            "complexity_ratio": self.complexity_ratio,
            "holdout_evaluations": self.holdout_evaluations,
            "deflation_ratio": self.deflation_ratio,
            "warning_count": len(self.warnings),
            "severity": self.severity,
            "is_overfit": self.is_overfit,
            "warnings": [
                {"type": w.warning_type, "severity": w.severity, "message": w.message}
                for w in self.warnings
            ],
        }


@dataclass
class HoldoutTracker:
    """Tracks holdout set evaluation counts to prevent snooping."""

    _evaluations: dict[HoldoutId, int] = field(default_factory=dict)
    _first_use: dict[HoldoutId, datetime] = field(default_factory=dict)
    _last_use: dict[HoldoutId, datetime] = field(default_factory=dict)

    def record_evaluation(self, holdout_id: HoldoutId) -> OverfittingWarning | None:
        """Record an evaluation on a holdout set. Returns warning if abused."""
        now = datetime.utcnow()
        self._evaluations[holdout_id] = self._evaluations.get(holdout_id, 0) + 1
        if holdout_id not in self._first_use:
            self._first_use[holdout_id] = now
        self._last_use[holdout_id] = now

        count = self._evaluations[holdout_id]
        if count > 10:
            return OverfittingWarning(
                warning_type="holdout_abuse",
                severity="critical",
                message=(
                    f"Holdout '{holdout_id}' evaluated {count} times. "
                    f"Repeated evaluation degrades holdout validity."
                ),
                details={"evaluation_count": count},
            )
        if count > 5:
            return OverfittingWarning(
                warning_type="holdout_abuse",
                severity="high",
                message=(
                    f"Holdout '{holdout_id}' evaluated {count} times. "
                    f"Consider using a fresh holdout set."
                ),
                details={"evaluation_count": count},
            )
        if count > 3:
            return OverfittingWarning(
                warning_type="holdout_abuse",
                severity="medium",
                message=(
                    f"Holdout '{holdout_id}' evaluated {count} times. "
                    f"Monitor for data snooping."
                ),
                details={"evaluation_count": count},
            )
        return None

    def get_count(self, holdout_id: HoldoutId) -> int:
        return self._evaluations.get(holdout_id, 0)

    def get_all(self) -> dict[HoldoutId, int]:
        return dict(self._evaluations)


# Module-level tracker instance
_default_tracker = HoldoutTracker()


def track_holdout_usage(
    holdout_id: HoldoutId,
    evaluation_count: int,
) -> OverfittingWarning | None:
    """Track holdout usage and warn on repeated evaluations.

    Args:
        holdout_id: Identifier for the holdout set.
        evaluation_count: How many times this holdout has been evaluated.

    Returns:
        Warning if usage is excessive, None otherwise.
    """
    _default_tracker._evaluations[holdout_id] = evaluation_count
    if evaluation_count > 10:
        return OverfittingWarning(
            warning_type="holdout_abuse",
            severity="critical",
            message=(
                f"Holdout '{holdout_id}' has been evaluated {evaluation_count} times. "
                f"Statistical validity is severely compromised."
            ),
            details={"evaluation_count": evaluation_count},
        )
    if evaluation_count > 5:
        return OverfittingWarning(
            warning_type="holdout_abuse",
            severity="high",
            message=(
                f"Holdout '{holdout_id}' has been evaluated {evaluation_count} times. "
                f"Results may be unreliable."
            ),
            details={"evaluation_count": evaluation_count},
        )
    if evaluation_count > 3:
        return OverfittingWarning(
            warning_type="holdout_abuse",
            severity="medium",
            message=(
                f"Holdout '{holdout_id}' evaluated {evaluation_count} times. "
                f"Approaching snooping threshold."
            ),
            details={"evaluation_count": evaluation_count},
        )
    return None


def compute_deflation_ratio(
    n_strategies_tested: int,
    best_p_value: float,
) -> float:
    """Compute deflation ratio for multiple testing correction.

    The deflation ratio adjusts for the fact that testing many strategies
    inflates the chance of finding a "significant" result by luck.

    Uses the Bonferroni-like correction: adjusted_p = min(1, p * n_tested)
    Returns the ratio of deflated significance to raw significance.

    Args:
        n_strategies_tested: Number of strategies/parameters tested.
        best_p_value: Best observed p-value across all tests.

    Returns:
        Deflation ratio (0-1). Lower = more deflated (less significant).
    """
    if n_strategies_tested <= 0 or best_p_value <= 0:
        return 1.0
    # Bonferroni correction
    adjusted_p = min(1.0, best_p_value * n_strategies_tested)
    # Ratio: how much of original significance survives
    if best_p_value >= 1.0:
        return 1.0
    return adjusted_p


def detect_overfitting(
    train_metrics: TrainTestMetrics,
    test_metrics: TrainTestMetrics,
    n_parameters: int,
    n_observations: int,
    holdout_evaluations: int = 0,
    n_strategies_tested: int = 1,
    best_p_value: float = 1.0,
) -> OverfittingReport:
    """Detect overfitting through multiple signals.

    Args:
        train_metrics: Training set performance.
        test_metrics: Test set performance.
        n_parameters: Number of model parameters.
        n_observations: Number of training observations.
        holdout_evaluations: How many times holdout was evaluated.
        n_strategies_tested: Total strategies/parameters tested.
        best_p_value: Best p-value observed.

    Returns:
        OverfittingReport with warnings and severity assessment.
    """
    warnings: list[OverfittingWarning] = []

    # 1. Train/test gap
    gap = test_metrics.brier_score - train_metrics.brier_score
    if gap > 0.10:
        warnings.append(
            OverfittingWarning(
                warning_type="gap",
                severity="critical",
                message=(
                    f"Severe train/test gap: train_brier={train_metrics.brier_score:.4f}, "
                    f"test_brier={test_metrics.brier_score:.4f}, gap={gap:.4f}"
                ),
                details={
                    "gap": gap,
                    "train": train_metrics.brier_score,
                    "test": test_metrics.brier_score,
                },
            )
        )
    elif gap > 0.05:
        warnings.append(
            OverfittingWarning(
                warning_type="gap",
                severity="high",
                message=(
                    f"Large train/test gap: train_brier={train_metrics.brier_score:.4f}, "
                    f"test_brier={test_metrics.brier_score:.4f}, gap={gap:.4f}"
                ),
                details={"gap": gap},
            )
        )
    elif gap > 0.02:
        warnings.append(
            OverfittingWarning(
                warning_type="gap",
                severity="medium",
                message=f"Moderate train/test gap: {gap:.4f}",
                details={"gap": gap},
            )
        )

    # 2. Complexity ratio
    complexity = n_parameters / max(n_observations, 1)
    if complexity > 0.5:
        warnings.append(
            OverfittingWarning(
                warning_type="complexity",
                severity="critical",
                message=(
                    f"Model is severely overparameterized: {n_parameters} params / "
                    f"{n_observations} observations = {complexity:.2f}"
                ),
                details={"ratio": complexity, "params": n_parameters, "obs": n_observations},
            )
        )
    elif complexity > 0.1:
        warnings.append(
            OverfittingWarning(
                warning_type="complexity",
                severity="high",
                message=(
                    f"High parameter/observation ratio: {n_parameters}/{n_observations} "
                    f"= {complexity:.2f}"
                ),
                details={"ratio": complexity},
            )
        )
    elif complexity > 0.05:
        warnings.append(
            OverfittingWarning(
                warning_type="complexity",
                severity="medium",
                message=f"Moderate complexity ratio: {complexity:.3f}",
                details={"ratio": complexity},
            )
        )

    # 3. Holdout abuse
    if holdout_evaluations > 10:
        warnings.append(
            OverfittingWarning(
                warning_type="holdout_abuse",
                severity="critical",
                message=(
                    f"Holdout evaluated {holdout_evaluations} times. "
                    f"Statistical validity compromised."
                ),
                details={"count": holdout_evaluations},
            )
        )
    elif holdout_evaluations > 5:
        warnings.append(
            OverfittingWarning(
                warning_type="holdout_abuse",
                severity="high",
                message=f"Holdout evaluated {holdout_evaluations} times.",
                details={"count": holdout_evaluations},
            )
        )

    # 4. Deflation ratio
    deflation = compute_deflation_ratio(n_strategies_tested, best_p_value)
    if deflation < 0.01 and n_strategies_tested > 100:
        warnings.append(
            OverfittingWarning(
                warning_type="deflation",
                severity="high",
                message=(
                    f"Multiple testing severely deflates significance: "
                    f"{n_strategies_tested} strategies tested, deflation_ratio={deflation:.4f}"
                ),
                details={"deflation": deflation, "n_tested": n_strategies_tested},
            )
        )
    elif deflation < 0.1 and n_strategies_tested > 20:
        warnings.append(
            OverfittingWarning(
                warning_type="deflation",
                severity="medium",
                message=(
                    f"Multiple testing deflates significance: "
                    f"{n_strategies_tested} strategies, deflation_ratio={deflation:.4f}"
                ),
                details={"deflation": deflation, "n_tested": n_strategies_tested},
            )
        )

    # Determine overall severity
    severity_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    worst_severity = "low"
    for w in warnings:
        if severity_order.get(w.severity, 0) > severity_order.get(worst_severity, 0):
            worst_severity = w.severity

    is_overfit = worst_severity in ("high", "critical")

    return OverfittingReport(
        train_test_gap=gap,
        complexity_ratio=complexity,
        holdout_evaluations=holdout_evaluations,
        deflation_ratio=deflation,
        warnings=warnings,
        severity=worst_severity,
        is_overfit=is_overfit,
    )
