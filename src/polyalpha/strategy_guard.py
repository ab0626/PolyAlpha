"""Strategy mining guardrails — detect overfitting and selection pressure.

Part 74: Warns when strategy search results show signs of data snooping,
excessive selection pressure, or train-test generalization gaps.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GuardrailResult:
    """Result of strategy mining guardrail checks."""

    warnings: list[str]
    severity: str  # "ok", "caution", "warning", "critical"

    def summary(self) -> dict:
        return {
            "warnings": self.warnings,
            "severity": self.severity,
            "warning_count": len(self.warnings),
        }


def _severity_from_warnings(warnings: list[str]) -> str:
    """Determine severity from accumulated warnings."""
    if not warnings:
        return "ok"
    if any("SELECTION PRESSURE" in w for w in warnings):
        return "critical"
    if any("train/test gap" in w for w in warnings):
        return "warning"
    return "caution"


def guardrail(
    n_strategies_tested: int,
    best_train_metric: float,
    median_train_metric: float,
    best_validation_metric: float,
    final_oos_metric: float,
) -> GuardrailResult:
    """Evaluate strategy mining results for overfitting and selection bias.

    Args:
        n_strategies_tested: Total number of candidate strategies evaluated.
        best_train_metric: Best in-sample metric (e.g. Sharpe) across strategies.
        median_train_metric: Median in-sample metric across strategies.
        best_validation_metric: Best out-of-sample validation metric.
        final_oos_metric: Final true out-of-sample metric after selection.

    Returns:
        GuardrailResult with warnings and severity level.
    """
    warnings: list[str] = []

    # Check 1: Selection pressure — too many strategies tested
    if n_strategies_tested > 50:
        warnings.append(
            f"SELECTION PRESSURE: {n_strategies_tested} strategies tested. "
            f"Probability of finding a spurious 'good' strategy increases with more tests. "
            f"Apply multiple-testing correction."
        )
    elif n_strategies_tested > 20:
        warnings.append(
            f"Moderate selection pressure: {n_strategies_tested} strategies tested. "
            f"Consider using Benjamini-Hochberg or Bonferroni correction."
        )

    # Check 2: Train-test generalization gap
    if best_train_metric > 0:
        train_val_gap = best_train_metric - best_validation_metric
        rel_gap = train_val_gap / abs(best_train_metric)
        if rel_gap > 0.5:
            warnings.append(
                f"LARGE train/test gap: best train {best_train_metric:.4f} vs "
                f"best validation {best_validation_metric:.4f} "
                f"({rel_gap:.0%} relative decline). Strategy may be overfit."
            )
        elif rel_gap > 0.3:
            warnings.append(
                f"Moderate train/test gap: best train {best_train_metric:.4f} vs "
                f"best validation {best_validation_metric:.4f} "
                f"({rel_gap:.0%} relative decline)."
            )

    # Check 3: Validation much worse than train — generalization failure
    if best_train_metric > 0 and best_validation_metric > 0:
        ratio = best_validation_metric / best_train_metric
        if ratio < 0.3:
            warnings.append(
                f"GENERALIZATION FAILURE: validation metric is only {ratio:.1%} of "
                f"train metric. Strategy likely not actionable."
            )

    # Check 4: Final OOS degrades vs validation
    if best_validation_metric > 0:
        oos_gap = best_validation_metric - final_oos_metric
        rel_oos = oos_gap / abs(best_validation_metric)
        if rel_oos > 0.4:
            warnings.append(
                f"OOS DECAY: validation {best_validation_metric:.4f} vs "
                f"final OOS {final_oos_metric:.4f} "
                f"({rel_oos:.0%} decline). Likely overfit to validation set."
            )

    # Check 5: Median is close to best — no clear signal
    if best_train_metric > 0 and median_train_metric > 0:
        spread = best_train_metric / median_train_metric
        if spread < 1.1:
            warnings.append(
                f"Weak signal: best train ({best_train_metric:.4f}) is only "
                f"{spread:.1%}x median ({median_train_metric:.4f}). "
                f"No strategy clearly outperforms random."
            )

    severity = _severity_from_warnings(warnings)

    return GuardrailResult(warnings=warnings, severity=severity)
