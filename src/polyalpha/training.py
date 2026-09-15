"""Full training pipeline: feature extraction, model training, ensemble optimization.

Pipeline stages:
1. Load historical data (resolved markets with book snapshots and outcomes)
2. Extract features from book snapshots (point-in-time only)
3. Train individual models (logistic regression, gradient-boosted trees)
4. Optimize ensemble weights on validation set
5. Save trained models + weights as JSON artifacts
6. Evaluate on held-out test set

All training uses chronological splits (no future data leakage).
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .calibration import metrics as cal_metrics

logger = logging.getLogger(__name__)

D = Decimal


@dataclass
class TrainingSample:
    """A single training observation with features and outcome."""

    market_id: str
    timestamp: datetime
    features: dict[str, float]
    outcome: int  # 0 or 1


@dataclass
class TrainingResult:
    """Result of a training run."""

    model_name: str
    train_samples: int
    test_samples: int
    train_brier: float
    test_brier: float
    test_ece: float
    test_log_loss: float
    feature_importances: dict[str, float] | None = None
    model_params: dict | None = None


def extract_features_from_records(records: list[dict]) -> list[TrainingSample]:
    """Extract training samples from raw record dicts.

    Each record must have:
    - market_id, timestamp, outcome (0 or 1)
    - book: dict with bids, asks, tick_size, min_order_size
    """
    samples = []
    for rec in records:
        try:
            from .parsing import parse_book

            book_raw = rec.get("book")
            if not book_raw:
                continue

            ts = rec["timestamp"]
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))

            book = parse_book(book_raw, ts, rec.get("entity_id", "unknown"))

            from .models.trained import _book_features

            features = _book_features(book)
            outcome = int(rec["outcome"])
            if outcome not in (0, 1):
                continue

            samples.append(
                TrainingSample(
                    market_id=rec["market_id"],
                    timestamp=ts,
                    features=features,
                    outcome=outcome,
                )
            )
        except (ValueError, KeyError, Exception) as e:
            logger.debug("skipping record: %s", e)
            continue
    return samples


def extract_features_from_db(db_path: str, cutoff: datetime | None = None) -> list[TrainingSample]:
    """Load and extract features from the SQLite database.

    Reads from book_snapshots joined with outcomes if available,
    or from calibration_observations with their associated book data.
    """
    import sqlite3

    conn = sqlite3.connect(db_path)

    # Try to get resolved markets with book data
    query = """
        SELECT market_id, entity_id, payload, received_at
        FROM receipts
        WHERE kind = 'book'
    """
    if cutoff:
        query += f" AND received_at < '{cutoff.isoformat()}'"
    query += " ORDER BY received_at"

    cursor = conn.execute(query)
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        logger.warning("no book snapshots found in %s", db_path)
        return []

    # Parse records and extract features
    records = []
    for market_id, entity_id, payload_json, received_at in rows:
        try:
            payload = json.loads(payload_json)
            records.append(
                {
                    "market_id": market_id,
                    "entity_id": entity_id,
                    "book": payload,
                    "timestamp": received_at,
                    "outcome": None,  # will be filled if available
                }
            )
        except (json.JSONDecodeError, TypeError):
            continue

    return extract_features_from_records(records)


def train_logistic_regression(
    train_samples: list[TrainingSample],
    test_samples: list[TrainingSample],
    C: float = 1.0,
) -> TrainingResult:
    """Train a logistic regression model and evaluate."""
    from .models.trained import _FEATURE_NAMES, LogisticRegressionModel

    X_train = [[s.features.get(name, 0.0) for name in _FEATURE_NAMES] for s in train_samples]
    y_train = [s.outcome for s in train_samples]
    y_test = [s.outcome for s in test_samples]

    model = LogisticRegressionModel(C=C)
    model.fit(X_train, y_train)

    # Evaluate
    train_proba = [
        float(model.predict(s.market_id, _fake_book(s.features), s.timestamp).probability)
        for s in train_samples
    ]
    test_proba = [
        float(model.predict(s.market_id, _fake_book(s.features), s.timestamp).probability)
        for s in test_samples
    ]

    train_metrics = cal_metrics(train_proba, y_train)
    test_metrics = cal_metrics(test_proba, y_test)

    # Feature importances (absolute coefficient values)
    coefs = model._model.coef_[0]
    importances = {name: abs(float(coefs[i])) for i, name in enumerate(_FEATURE_NAMES)}

    return TrainingResult(
        model_name="logistic_regression",
        train_samples=len(train_samples),
        test_samples=len(test_samples),
        train_brier=train_metrics["brier"],
        test_brier=test_metrics["brier"],
        test_ece=test_metrics["ece"],
        test_log_loss=test_metrics["log_loss"],
        feature_importances=importances,
        model_params=model.to_dict(),
    )


def train_gradient_boosted(
    train_samples: list[TrainingSample],
    test_samples: list[TrainingSample],
    n_estimators: int = 100,
    max_depth: int = 3,
) -> TrainingResult:
    """Train a gradient-boosted model and evaluate."""
    from .models.trained import _FEATURE_NAMES, GradientBoostedModel

    X_train = [[s.features.get(name, 0.0) for name in _FEATURE_NAMES] for s in train_samples]
    y_train = [s.outcome for s in train_samples]
    y_test = [s.outcome for s in test_samples]

    model = GradientBoostedModel(n_estimators=n_estimators, max_depth=max_depth)
    model.fit(X_train, y_train)

    # Evaluate
    train_proba = [
        float(model.predict(s.market_id, _fake_book(s.features), s.timestamp).probability)
        for s in train_samples
    ]
    test_proba = [
        float(model.predict(s.market_id, _fake_book(s.features), s.timestamp).probability)
        for s in test_samples
    ]

    train_metrics = cal_metrics(train_proba, y_train)
    test_metrics = cal_metrics(test_proba, y_test)

    # Feature importances from tree model
    importances_list = model._model.feature_importances_
    from .models.trained import _FEATURE_NAMES

    importances = {name: float(importances_list[i]) for i, name in enumerate(_FEATURE_NAMES)}

    return TrainingResult(
        model_name="gradient_boosted",
        train_samples=len(train_samples),
        test_samples=len(test_samples),
        train_brier=train_metrics["brier"],
        test_brier=test_metrics["brier"],
        test_ece=test_metrics["ece"],
        test_log_loss=test_metrics["log_loss"],
        feature_importances=importances,
        model_params=model.to_dict(),
    )


def optimize_ensemble_weights(
    components: dict[str, Any],
    val_samples: list[TrainingSample],
) -> dict[str, float]:
    """Optimize ensemble weights by minimizing Brier score on validation set.

    Uses grid search over weight space for 2-3 components, which is
    tractable and avoids overfitting compared to continuous optimization.
    """

    names = sorted(components.keys())
    n = len(names)

    if n == 0:
        return {}
    if n == 1:
        return {names[0]: 1.0}

    # Generate predictions from each component
    predictions = {}
    for name, model in components.items():
        preds = []
        for s in val_samples:
            try:
                forecast = model.predict(s.market_id, _fake_book(s.features), s.timestamp)
                preds.append(float(forecast.probability))
            except (ValueError, Exception):
                preds.append(0.5)  # fallback
        predictions[name] = preds

    outcomes = [s.outcome for s in val_samples]

    if n == 2:
        # Grid search over w1 in [0, 1]
        best_brier = float("inf")
        best_w = {names[0]: 0.5, names[1]: 0.5}
        for w1_pct in range(0, 101, 5):
            w1 = w1_pct / 100.0
            w2 = 1.0 - w1
            ensemble_pred = [
                w1 * predictions[names[0]][i] + w2 * predictions[names[1]][i]
                for i in range(len(outcomes))
            ]
            m = cal_metrics(ensemble_pred, outcomes)
            if m["brier"] < best_brier:
                best_brier = m["brier"]
                best_w = {names[0]: w1, names[1]: w2}
        return best_w

    elif n == 3:
        # Coarse grid search over w1, w2
        best_brier = float("inf")
        best_w = {name: 1.0 / n for name in names}
        for w1_pct in range(0, 101, 10):
            for w2_pct in range(0, 101 - w1_pct, 10):
                w1 = w1_pct / 100.0
                w2 = w2_pct / 100.0
                w3 = 1.0 - w1 - w2
                if w3 < -0.001:
                    continue
                w3 = max(0, w3)
                weights = [w1, w2, w3]
                ensemble_pred = [
                    sum(weights[j] * predictions[names[j]][i] for j in range(n))
                    for i in range(len(outcomes))
                ]
                m = cal_metrics(ensemble_pred, outcomes)
                if m["brier"] < best_brier:
                    best_brier = m["brier"]
                    best_w = {names[j]: weights[j] for j in range(n)}
        return best_w

    else:
        # For 4+ components, use equal weights (grid search is exponential)
        return {name: 1.0 / n for name in names}


def train_ensemble(
    train_samples: list[TrainingSample],
    val_samples: list[TrainingSample],
    test_samples: list[TrainingSample],
    config: dict | None = None,
) -> dict:
    """Full ensemble training pipeline.

    1. Train individual models on train_samples
    2. Optimize weights on val_samples
    3. Evaluate on test_samples
    4. Return trained artifacts
    """
    config = config or {}
    results = {}
    trained_models = {}

    # Stage 1: Train individual models
    if len(train_samples) >= 20:
        try:
            lr_result = train_logistic_regression(
                train_samples,
                test_samples,
                C=config.get("logistic_C", 1.0),
            )
            results["logistic_regression"] = lr_result
            from .models.trained import _FEATURE_NAMES, LogisticRegressionModel

            X_all = [[s.features.get(name, 0.0) for name in _FEATURE_NAMES] for s in train_samples]
            y_all = [s.outcome for s in train_samples]
            lr_model = LogisticRegressionModel(C=config.get("logistic_C", 1.0))
            lr_model.fit(X_all, y_all)
            trained_models["logistic_regression"] = lr_model
        except Exception as e:
            logger.warning("logistic regression training failed: %s", e)

    if len(train_samples) >= 30:
        try:
            gbm_result = train_gradient_boosted(
                train_samples,
                test_samples,
                n_estimators=config.get("gbm_n_estimators", 100),
                max_depth=config.get("gbm_max_depth", 3),
            )
            results["gradient_boosted"] = gbm_result
            from .models.trained import _FEATURE_NAMES, GradientBoostedModel

            X_all = [[s.features.get(name, 0.0) for name in _FEATURE_NAMES] for s in train_samples]
            y_all = [s.outcome for s in train_samples]
            gbm_model = GradientBoostedModel(
                n_estimators=config.get("gbm_n_estimators", 100),
                max_depth=config.get("gbm_max_depth", 3),
            )
            gbm_model.fit(X_all, y_all)
            trained_models["gradient_boosted"] = gbm_model
        except Exception as e:
            logger.warning("gradient-boosted training failed: %s", e)

    if not trained_models:
        return {"error": "no models trained", "results": results}

    # Stage 2: Optimize ensemble weights on validation set
    weights = optimize_ensemble_weights(trained_models, val_samples)

    # Stage 3: Evaluate ensemble on test set
    from .models.trained import _FEATURE_NAMES, TrainedEnsembleModel

    ensemble = TrainedEnsembleModel(
        components=[(n, m) for n, m in trained_models.items()],
        weights=weights,
    )

    ensemble_test_proba = []
    for s in test_samples:
        try:
            forecast = ensemble.predict(s.market_id, _fake_book(s.features), s.timestamp)
            ensemble_test_proba.append(float(forecast.probability))
        except (ValueError, Exception):
            ensemble_test_proba.append(0.5)

    ensemble_metrics = cal_metrics(ensemble_test_proba, [s.outcome for s in test_samples])

    return {
        "ensemble_weights": weights,
        "ensemble_metrics": ensemble_metrics,
        "component_results": {k: _result_to_dict(v) for k, v in results.items()},
        "trained_models": trained_models,
        "ensemble_model": ensemble,
    }


def save_artifacts(
    trained_models: dict,
    weights: dict,
    metrics: dict,
    output_dir: str | Path,
):
    """Save trained model artifacts to disk."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Save ensemble config
    ensemble_config = {
        "version": "trained-ensemble-v1",
        "weights": weights,
        "metrics": metrics,
        "saved_at": datetime.utcnow().isoformat() + "Z",
    }
    (output_dir / "ensemble.json").write_text(json.dumps(ensemble_config, indent=2, default=str))

    # Save individual models
    for name, model in trained_models.items():
        model_path = output_dir / f"{name}.json"
        model_path.write_text(json.dumps(model.to_dict(), indent=2, default=str))

    logger.info("saved artifacts to %s", output_dir)


def load_artifacts(artifacts_dir: str | Path) -> dict:
    """Load trained model artifacts from disk."""
    artifacts_dir = Path(artifacts_dir)
    result = {}

    ensemble_path = artifacts_dir / "ensemble.json"
    if ensemble_path.exists():
        result["ensemble_config"] = json.loads(ensemble_path.read_text())

    # Load individual models
    from .models.trained import GradientBoostedModel, LogisticRegressionModel

    model_classes = {
        "logistic_regression": LogisticRegressionModel,
        "gradient_boosted": GradientBoostedModel,
    }
    for name, cls in model_classes.items():
        model_path = artifacts_dir / f"{name}.json"
        if model_path.exists():
            params = json.loads(model_path.read_text())
            result[name] = cls.from_dict(params)

    return result


def _fake_book(features: dict[str, float]):
    """Create a minimal Book-like object from features for model.predict()."""
    from .domain import Book, Level

    mid = features.get("midpoint", 0.5)
    spread = features.get("spread", 0.02)
    bid_price = features.get("bid_price", mid - spread / 2)
    ask_price = features.get("ask_price", mid + spread / 2)
    bid_size = features.get("bid_size", 100.0)
    ask_size = features.get("ask_size", 100.0)

    return Book(
        token_id="训练",
        condition_id="训练",
        source_at=datetime(2020, 1, 1, tzinfo=__import__("datetime").timezone.utc),
        received_at=datetime(2020, 1, 1, tzinfo=__import__("datetime").timezone.utc),
        bids=(Level(D(str(bid_price)), D(str(bid_size))),),
        asks=(Level(D(str(ask_price)), D(str(ask_size))),),
        tick_size=D("0.01"),
        min_order_size=D(1),
        source_hash="training",
    )


def _result_to_dict(result: TrainingResult) -> dict:
    return {
        "model_name": result.model_name,
        "train_samples": result.train_samples,
        "test_samples": result.test_samples,
        "train_brier": result.train_brier,
        "test_brier": result.test_brier,
        "test_ece": result.test_ece,
        "test_log_loss": result.test_log_loss,
        "feature_importances": result.feature_importances,
        "model_params": result.model_params,
    }


def main(argv: list[str] | None = None):
    """CLI entry point for full model training."""
    import argparse

    from .config import load_env, load_toml

    parser = argparse.ArgumentParser(description="Train ML probability models")
    parser.add_argument("--config", default="config/base.toml")
    parser.add_argument("--db", help="SQLite database path")
    parser.add_argument("--output-dir", default="data/trained_models")
    parser.add_argument("--train-ratio", type=float, default=0.6)
    parser.add_argument("--val-ratio", type=float, default=0.2)
    parser.add_argument("--logistic-C", type=float, default=1.0)
    parser.add_argument("--gbm-estimators", type=int, default=100)
    parser.add_argument("--gbm-depth", type=int, default=3)
    args = parser.parse_args(argv)

    load_env()
    config = load_toml(args.config)

    db_path = args.db or config.get("database", {}).get("path", "data/polyalpha.db")
    if not Path(db_path).exists():
        logger.error("database not found: %s", db_path)
        return

    logger.info("loading training data from %s", db_path)
    samples = extract_features_from_db(db_path)
    if not samples:
        logger.error("no training samples found")
        return

    logger.info("extracted %d training samples", len(samples))

    # Chronological split
    samples.sort(key=lambda s: s.timestamp)
    n = len(samples)
    n_train = int(n * args.train_ratio)
    n_val = int(n * args.val_ratio)

    train = samples[:n_train]
    val = samples[n_train : n_train + n_val]
    test = samples[n_train + n_val :]

    if not val:
        val = train[-max(1, len(train) // 5) :]
        train = train[: -len(val)]
    if not test:
        test = val[-max(1, len(val) // 3) :]
        val = val[: -len(test)]

    logger.info("split: train=%d, val=%d, test=%d", len(train), len(val), len(test))

    result = train_ensemble(
        train,
        val,
        test,
        config={
            "logistic_C": args.logistic_C,
            "gbm_n_estimators": args.gbm_estimators,
            "gbm_max_depth": args.gbm_depth,
        },
    )

    if "error" in result:
        logger.error("training failed: %s", result["error"])
        return

    # Save artifacts
    save_artifacts(
        result["trained_models"],
        result["ensemble_weights"],
        result["ensemble_metrics"],
        args.output_dir,
    )

    # Print summary
    output = {
        "event": "training_complete",
        "ensemble_weights": result["ensemble_weights"],
        "ensemble_metrics": result["ensemble_metrics"],
        "component_results": {
            k: {kk: vv for kk, vv in v.items() if kk != "model_params"}
            for k, v in result["component_results"].items()
        },
    }
    print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()
