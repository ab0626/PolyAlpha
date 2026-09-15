"""Parameter sensitivity analysis for robustness assessment.

Part 38: Grid search over parameter space with plateau detection.
Identifies stable parameter regions vs. sharp peaks that suggest
overfitting to specific parameter values.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, TypeAlias

D = Decimal

ParamValue: TypeAlias = float | int | str


@dataclass(frozen=True)
class ValueResult:
    """Result for a single parameter value."""

    param_value: ParamValue
    metric_value: float
    is_profitable: bool
    is_plateau: bool  # True if all neighbors have similar performance
    neighboring_values: list[ParamValue]


@dataclass(frozen=True)
class SensitivityResult:
    """Full sensitivity analysis result."""

    param_name: str
    values_tested: int
    value_results: list[ValueResult]
    best_value: ParamValue
    best_metric: float
    worst_value: ParamValue
    worst_metric: float
    metric_range: float  # best - worst
    plateau_values: list[ParamValue]  # values in stable plateaus
    peak_values: list[ParamValue]  # isolated sharp peaks
    is_sensitive: bool  # True if metric varies significantly
    sensitivity_warning: str
    summary: dict[str, Any]

    def summary_dict(self) -> dict:
        return {
            "param_name": self.param_name,
            "values_tested": self.values_tested,
            "best_value": self.best_value,
            "best_metric": self.best_metric,
            "worst_value": self.worst_value,
            "worst_metric": self.worst_metric,
            "metric_range": self.metric_range,
            "plateau_count": len(self.plateau_values),
            "peak_count": len(self.peak_values),
            "is_sensitive": self.is_sensitive,
            "sensitivity_warning": self.sensitivity_warning,
        }


@dataclass(frozen=True)
class PlateauRegion:
    """A contiguous region of stable performance."""

    start_value: ParamValue
    end_value: ParamValue
    mean_metric: float
    std_metric: float
    value_count: int


def _sort_values(values: list[ParamValue]) -> list[ParamValue]:
    """Sort parameter values, handling mixed types."""
    try:
        return sorted(values, key=lambda v: float(v))
    except (TypeError, ValueError):
        return sorted(values, key=lambda v: str(v))


def _neighbors(
    idx: int, values: list[ParamValue], radius: int = 1
) -> list[ParamValue]:
    """Get neighboring values within radius."""
    start = max(0, idx - radius)
    end = min(len(values), idx + radius + 1)
    return [v for i, v in enumerate(values) if start <= i < end and i != idx]


def run_sensitivity_grid(
    param_name: str,
    param_values: list[ParamValue],
    eval_fn: Callable[[ParamValue], float],
    profitability_threshold: float = 0.0,
    plateau_threshold: float = 0.05,
) -> SensitivityResult:
    """Run grid search over parameter space.

    Args:
        param_name: Name of the parameter being tested.
        param_values: List of parameter values to test.
        eval_fn: Function that takes a parameter value and returns a metric.
        profitability_threshold: Metric value above which is considered profitable.
        plateau_threshold: Max relative variation to consider values part of a plateau.

    Returns:
        SensitivityResult with per-value metrics and plateau detection.
    """
    if not param_values:
        return _empty_sensitivity(param_name)

    sorted_vals = _sort_values(param_values)
    results: list[tuple[ParamValue, float]] = []

    for val in sorted_vals:
        metric = eval_fn(val)
        results.append((val, metric))

    # Compute plateaus
    metrics = [m for _, m in results]
    overall_std = _safe_std(metrics)
    overall_mean = sum(metrics) / len(metrics) if metrics else 0.0

    value_results: list[ValueResult] = []
    plateau_values: list[ParamValue] = []
    peak_values: list[ParamValue] = []

    for i, (val, metric) in enumerate(results):
        nbrs = _neighbors(i, sorted_vals)
        nbr_metrics = [m for v, m in results if v in nbrs]

        if nbr_metrics:
            local_mean = sum(nbr_metrics) / len(nbr_metrics)
            local_std = _safe_std(nbr_metrics)
            # Value is on a plateau if its neighbors have similar performance
            is_plateau = (
                local_std < plateau_threshold * abs(local_mean) + 1e-10
                and abs(metric - local_mean) < plateau_threshold * abs(local_mean) + 1e-10
            )
        else:
            is_plateau = True  # single value is trivially a plateau

        # Check if isolated peak: higher than all neighbors by > 2*std
        is_peak = False
        if nbr_metrics and overall_std > 0:
            is_peak = all(
                metric > nm + 2 * overall_std for nm in nbr_metrics
            ) and metric > overall_mean + 2 * overall_std

        if is_plateau:
            plateau_values.append(val)
        if is_peak:
            peak_values.append(val)

        value_results.append(
            ValueResult(
                param_value=val,
                metric_value=metric,
                is_profitable=metric > profitability_threshold,
                is_plateau=is_plateau,
                neighboring_values=nbrs,
            )
        )

    # Find best/worst
    best_val, best_metric = max(results, key=lambda x: x[1])
    worst_val, worst_metric = min(results, key=lambda x: x[1])
    metric_range = best_metric - worst_metric

    # Sensitivity: metric range > 20% of mean or > 0.05 absolute
    is_sensitive = (
        metric_range > 0.05
        or (abs(overall_mean) > 0 and metric_range / abs(overall_mean) > 0.2)
    )

    warning = ""
    if len(peak_values) > 0 and len(plateau_values) < len(sorted_vals) * 0.3:
        warning = (
            f"SENSITIVITY WARNING: Parameter '{param_name}' shows {len(peak_values)} "
            f"isolated peak(s) and only {len(plateau_values)} plateau values. "
            f"Metric range={metric_range:.4f}. Model may be overfit to specific "
            f"parameter values."
        )
    elif is_sensitive:
        warning = (
            f"Parameter '{param_name}' is sensitive: metric ranges from "
            f"{worst_metric:.4f} to {best_metric:.4f} (range={metric_range:.4f})."
        )

    return SensitivityResult(
        param_name=param_name,
        values_tested=len(sorted_vals),
        value_results=value_results,
        best_value=best_val,
        best_metric=best_metric,
        worst_value=worst_val,
        worst_metric=worst_metric,
        metric_range=metric_range,
        plateau_values=plateau_values,
        peak_values=peak_values,
        is_sensitive=is_sensitive,
        sensitivity_warning=warning,
        summary={
            "param_name": param_name,
            "best": float(best_metric),
            "worst": float(worst_metric),
            "range": float(metric_range),
            "plateaus": len(plateau_values),
            "peaks": len(peak_values),
        },
    )


def sensitivity_summary(results: SensitivityResult) -> dict[str, Any]:
    """Find stable regions where all adjacent values are profitable.

    Returns a summary identifying robust parameter ranges.
    """
    if not results.value_results:
        return {"stable_regions": [], "recommendation": "no data"}

    profitable_values = [
        vr.param_value for vr in results.value_results if vr.is_profitable
    ]

    # Find contiguous profitable regions
    sorted_profitable = _sort_values(profitable_values)
    sorted_all = _sort_values([vr.param_value for vr in results.value_results])

    # Build index map for adjacency
    val_to_idx = {v: i for i, v in enumerate(sorted_all)}

    regions: list[PlateauRegion] = []
    current_region: list[ParamValue] = []

    for val in sorted_profitable:
        idx = val_to_idx.get(val)
        if idx is None:
            continue
        if current_region:
            prev_idx = val_to_idx.get(current_region[-1])
            if prev_idx is not None and idx == prev_idx + 1:
                current_region.append(val)
                continue
            else:
                # End current region
                region_metrics = [
                    vr.metric_value
                    for vr in results.value_results
                    if vr.param_value in current_region
                ]
                regions.append(
                    PlateauRegion(
                        start_value=current_region[0],
                        end_value=current_region[-1],
                        mean_metric=sum(region_metrics) / len(region_metrics),
                        std_metric=_safe_std(region_metrics),
                        value_count=len(current_region),
                    )
                )
                current_region = [val]
        else:
            current_region = [val]

    # Close final region
    if current_region:
        region_metrics = [
            vr.metric_value
            for vr in results.value_results
            if vr.param_value in current_region
        ]
        regions.append(
            PlateauRegion(
                start_value=current_region[0],
                end_value=current_region[-1],
                mean_metric=sum(region_metrics) / len(region_metrics),
                std_metric=_safe_std(region_metrics),
                value_count=len(current_region),
            )
        )

    # Recommendation: largest stable region
    if regions:
        best_region = max(regions, key=lambda r: r.value_count)
        recommendation = (
            f"Use {results.param_name} in range [{best_region.start_value}, "
            f"{best_region.end_value}] ({best_region.value_count} values, "
            f"mean metric={best_region.mean_metric:.4f})"
        )
    else:
        recommendation = (
            f"No stable profitable region found for {results.param_name}. "
            f"Consider wider grid or different parameterization."
        )

    return {
        "stable_regions": [
            {
                "start": r.start_value,
                "end": r.end_value,
                "mean": r.mean_metric,
                "std": r.std_metric,
                "count": r.value_count,
            }
            for r in regions
        ],
        "recommendation": recommendation,
        "best_region_count": max((r.value_count for r in regions), default=0),
    }


def _safe_std(values: list[float]) -> float:
    """Compute std deviation, returning 0 for empty or single-value lists."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return var ** 0.5


def _empty_sensitivity(param_name: str) -> SensitivityResult:
    """Return empty result for no data."""
    return SensitivityResult(
        param_name=param_name,
        values_tested=0,
        value_results=[],
        best_value=0,
        best_metric=0.0,
        worst_value=0,
        worst_metric=0.0,
        metric_range=0.0,
        plateau_values=[],
        peak_values=[],
        is_sensitive=False,
        sensitivity_warning="No parameter values tested",
        summary={},
    )
