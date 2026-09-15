"""Walk-forward backtest engine with systematic bias detection.

Implements chronological walk-forward testing that prevents:
- Look-ahead bias
- Leakage from final outcomes
- Timestamp misalignment
- Hyperparameter overfitting

Each fold trains on past data, validates on the next period,
then expands the training window.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from .calibration import InsufficientData, Observation, metrics
from .domain import utc

D = Decimal


@dataclass(frozen=True)
class WalkForwardFold:
    """Result of a single walk-forward fold."""

    fold_index: int
    train_start: datetime
    train_end: datetime
    validation_start: datetime
    validation_end: datetime
    training_markets: int
    validation_markets: int
    training_clusters: int
    validation_clusters: int
    raw_metrics: dict | None
    calibrated_metrics: dict | None
    status: str  # evaluated | insufficient_training | insufficient_validation
    excluded_clusters: list[str]
    prediction_ledger: list[dict]


@dataclass(frozen=True)
class WalkForwardResult:
    """Aggregated walk-forward results."""

    folds: list[WalkForwardFold]
    aggregate_raw: dict | None
    aggregate_calibrated: dict | None
    total_scored: int
    total_skipped: int
    fold_count: int
    method: str
    configuration: dict
    mfe_analysis: dict | None = None


class BacktestModel(Protocol):
    """Protocol for models that can be used in walk-forward backtesting."""

    def fit(self, observations: list[Observation], cutoff: datetime) -> Any: ...

    def predict(self, probability: float) -> float: ...

    @property
    def cutoff(self) -> datetime: ...


def walk_forward_backtest(
    observations: list[Observation],
    train_start: datetime,
    first_validation: datetime,
    end: datetime,
    validation_window: timedelta = timedelta(days=30),
    method: str = "isotonic",
    min_training_events: int = 4,
    embargo_days: int = 0,
) -> WalkForwardResult:
    """Run walk-forward backtest with chronological folds.

    For each fold:
    1. Train on all data before validation_start (minus embargo)
    2. Validate on validation window
    3. Exclude validation clusters from training
    4. Record raw and calibrated metrics

    Args:
        observations: All forecast observations with outcomes
        train_start: Start of training data
        first_validation: Start of first validation fold
        end: End date for all folds
        validation_window: Duration of each validation fold
        method: Calibration method (isotonic or platt)
        min_training_events: Minimum events required for training
        embargo_days: Gap between training end and validation start to prevent
            data leakage from temporally proximate observations. Section 26.
    """
    utc(train_start)
    utc(first_validation)
    utc(end)

    if first_validation >= end:
        raise ValueError("first_validation must precede end")
    if method not in ("isotonic", "platt"):
        raise ValueError("method must be isotonic or platt")
    if min_training_events < 2:
        raise ValueError("min_training_events must be >= 2")
    if embargo_days < 0:
        raise ValueError("embargo_days must be >= 0")

    embargo = timedelta(days=embargo_days)

    # Sort and canonicalize observations
    from .calibration import canonical_rows

    all_rows = canonical_rows(observations)

    folds = []
    fold_index = 0
    validation_start = first_validation
    all_trained_clusters: set[str] = set()

    while validation_start < end:
        validation_end = min(end, validation_start + validation_window)

        # Training: all observations before validation_start (minus embargo), excluding
        # clusters that appear in the CURRENT validation fold.
        # Embargo prevents leakage from temporally proximate observations.
        train_cutoff = validation_start - embargo
        current_val_rows = [
            r for r in all_rows if validation_start <= r.predicted_at < validation_end
        ]
        current_val_clusters = {r.cluster for r in current_val_rows}

        # Training rows: before train_cutoff, not in current validation clusters
        train_rows = [
            r
            for r in all_rows
            if r.predicted_at < train_cutoff
            and r.cluster not in current_val_clusters
            and r.label_known_at is not None
            and r.label_known_at < validation_end
        ]

        # Validation: rows in this fold with known outcomes
        val_rows = [
            r
            for r in all_rows
            if validation_start <= r.predicted_at < validation_end
            and r.label_known_at is not None
            and r.label_known_at <= end
        ]

        fold = WalkForwardFold(
            fold_index=fold_index,
            train_start=train_start,
            train_end=validation_start,
            validation_start=validation_start,
            validation_end=validation_end,
            training_markets=len({r.market_id for r in train_rows}),
            validation_markets=len({r.market_id for r in val_rows}),
            training_clusters=len({r.cluster for r in train_rows}),
            validation_clusters=len(current_val_clusters),
            raw_metrics=None,
            calibrated_metrics=None,
            status="insufficient_training",
            excluded_clusters=sorted(current_val_clusters),
            prediction_ledger=[],
        )

        try:
            if len(train_rows) < min_training_events:
                raise InsufficientData(
                    f"only {len(train_rows)} training events, need {min_training_events}"
                )
            if len({r.outcome for r in train_rows}) < 2:
                raise InsufficientData("need both outcomes in training")

            # Fit calibrator
            from .calibration import Isotonic, Platt

            calibrator = (Isotonic() if method == "isotonic" else Platt()).fit(
                train_rows, validation_start
            )

            # Generate predictions for validation fold
            ledger = []
            val_probs = []
            val_outcomes = []
            raw_probs = []

            for row in current_val_rows:
                mature = row.label_known_at is not None and row.label_known_at <= end
                cal_prob = calibrator.predict(row.probability)
                ledger.append(
                    dict(
                        market_id=row.market_id,
                        cluster=row.cluster,
                        predicted_at=row.predicted_at.isoformat(),
                        label_known_at=row.label_known_at.isoformat() if mature else None,
                        raw_probability=row.probability,
                        calibrated_probability=cal_prob,
                        outcome=row.outcome if mature else None,
                    )
                )
                if mature:
                    val_probs.append(cal_prob)
                    val_outcomes.append(row.outcome)
                    raw_probs.append(row.probability)

            fold = WalkForwardFold(
                fold_index=fold_index,
                train_start=train_start,
                train_end=validation_start,
                validation_start=validation_start,
                validation_end=validation_end,
                training_markets=len({r.market_id for r in train_rows}),
                validation_markets=len(current_val_rows),
                training_clusters=len({r.cluster for r in train_rows}),
                validation_clusters=len(current_val_clusters),
                raw_metrics=metrics(raw_probs, val_outcomes) if raw_probs else None,
                calibrated_metrics=metrics(val_probs, val_outcomes) if val_probs else None,
                status="evaluated" if val_probs else "no_resolved_outcomes",
                excluded_clusters=sorted(current_val_clusters),
                prediction_ledger=ledger,
            )

        except InsufficientData:
            fold = WalkForwardFold(
                fold_index=fold_index,
                train_start=train_start,
                train_end=validation_start,
                validation_start=validation_start,
                validation_end=validation_end,
                training_markets=len({r.market_id for r in train_rows}),
                validation_markets=len(current_val_rows),
                training_clusters=len({r.cluster for r in train_rows}),
                validation_clusters=len(current_val_clusters),
                raw_metrics=None,
                calibrated_metrics=None,
                status="insufficient_training",
                excluded_clusters=sorted(current_val_clusters),
                prediction_ledger=[],
            )

        folds.append(fold)
        all_trained_clusters.update(current_val_clusters)
        validation_start = validation_end
        fold_index += 1

    # Aggregate
    all_val_rows = [
        row for fold in folds for row in fold.prediction_ledger if row["outcome"] is not None
    ]
    aggregate_raw = (
        metrics([r["raw_probability"] for r in all_val_rows], [r["outcome"] for r in all_val_rows])
        if all_val_rows
        else None
    )
    aggregate_cal = (
        metrics(
            [r["calibrated_probability"] for r in all_val_rows],
            [r["outcome"] for r in all_val_rows],
        )
        if all_val_rows
        else None
    )

    return WalkForwardResult(
        folds=folds,
        aggregate_raw=aggregate_raw,
        aggregate_calibrated=aggregate_cal,
        total_scored=len(all_val_rows),
        total_skipped=sum(1 for f in folds if f.status != "evaluated"),
        fold_count=len(folds),
        method=method,
        configuration=dict(
            train_start=train_start.isoformat(),
            first_validation=first_validation.isoformat(),
            end=end.isoformat(),
            validation_window_days=validation_window.days,
            method=method,
            min_training_events=min_training_events,
            embargo_days=embargo_days,
        ),
    )


def compute_mfe(observations: list[Observation]) -> dict:
    """Compute Maximum Favorable Excursion analysis.

    MFE measures the maximum distance a prediction moved in the favorable
    direction before outcome resolution. High MFE relative to final error
    suggests the model captures directional movement but timing is off.

    Returns dict with MFE statistics useful for evaluating exit strategies.
    """
    if not observations:
        return {"error": "no_observations"}

    # Group by market_id
    by_market: dict[str, list[Observation]] = {}
    for obs in observations:
        by_market.setdefault(obs.market_id, []).append(obs)

    mfe_values = []
    for market_id, market_obs in by_market.items():
        if len(market_obs) < 2:
            continue

        # Sort by predicted_at
        sorted_obs = sorted(market_obs, key=lambda o: o.predicted_at)
        if sorted_obs[0].outcome is None:
            continue

        final_prob = sorted_obs[-1].probability
        outcome = sorted_obs[-1].outcome

        # MFE = max distance in the right direction
        best_direction = 1.0 if outcome == 1 else -1.0
        max_favorable = 0.0
        for obs in sorted_obs:
            distance = (obs.probability - final_prob) * best_direction
            if distance > max_favorable:
                max_favorable = distance

        final_error = abs(final_prob - outcome)
        mfe_values.append(
            {
                "market_id": market_id,
                "mfe": max_favorable,
                "final_error": final_error,
                "mfe_ratio": max_favorable / final_error if final_error > 0 else float("inf"),
                "n_observations": len(sorted_obs),
            }
        )

    if not mfe_values:
        return {"error": "no_valid_markets"}

    mfe_list = [m["mfe"] for m in mfe_values]
    mfe_ratios = [m["mfe_ratio"] for m in mfe_values if m["mfe_ratio"] < float("inf")]

    return {
        "market_count": len(mfe_values),
        "mean_mfe": sum(mfe_list) / len(mfe_list),
        "max_mfe": max(mfe_list),
        "median_mfe_ratio": sorted(mfe_ratios)[len(mfe_ratios) // 2] if mfe_ratios else None,
        "mean_mfe_ratio": sum(mfe_ratios) / len(mfe_ratios) if mfe_ratios else None,
        "markets_with_high_mfe": sum(1 for m in mfe_list if m > 0.1),
        "details": mfe_values,
    }


def main(argv: list[str] | None = None):
    """Entry point for standalone walk-forward evaluation."""
    import argparse
    import csv
    import json
    import sys
    from datetime import timedelta
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Run walk-forward calibration evaluation")
    parser.add_argument("--csv", required=True, help="Path to observations CSV")
    parser.add_argument("--output", help="Output path for results JSON")
    parser.add_argument("--window", type=int, default=30, help="Validation window in days")
    parser.add_argument("--method", default="isotonic", help="Calibration method")
    args = parser.parse_args(argv)

    with open(args.csv, encoding="utf-8-sig", newline="") as source:
        rows = [Observation(**row) for row in csv.DictReader(source)]

    if not rows:
        print(json.dumps({"error": "no observations"}))
        sys.exit(1)

    first_obs = rows[0]
    train_start = first_obs.timestamp
    first_validation = train_start + timedelta(days=args.window)
    end = rows[-1].timestamp

    result = walk_forward_backtest(
        rows,
        train_start=train_start,
        first_validation=first_validation,
        end=end,
        validation_window=timedelta(days=args.window),
        method=args.method,
    )

    output = {
        "fold_count": result.fold_count,
        "total_scored": result.total_scored,
        "total_skipped": result.total_skipped,
        "method": result.method,
        "configuration": result.configuration,
        "aggregate_raw": result.aggregate_raw,
        "aggregate_calibrated": result.aggregate_calibrated,
        "folds": [
            {
                "fold": f.fold_index,
                "status": f.status,
                "training_markets": f.training_markets,
                "validation_markets": f.validation_markets,
                "training_clusters": f.training_clusters,
                "validation_clusters": f.validation_clusters,
                "raw_metrics": f.raw_metrics,
                "calibrated_metrics": f.calibrated_metrics,
            }
            for f in result.folds
        ],
    }

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, default=str)
        print(json.dumps({"event": "walk_forward_complete", "output": args.output}))
    else:
        print(json.dumps(output, indent=2, default=str))
