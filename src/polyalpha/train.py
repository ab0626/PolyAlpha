"""Model training pipeline.

Trains probability models on historical data and saves calibrated parameters.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from .calibration import Isotonic, Platt, TemperatureScaling, metrics
from .config import load_env, load_toml

logger = logging.getLogger(__name__)


def load_training_data(db_path: str, cutoff: datetime) -> list[dict]:
    """Load resolved markets with model forecasts for training."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        """
        SELECT market_id, predicted_at, probability, outcome, cluster
        FROM calibration_observations
        WHERE predicted_at < ? AND outcome IS NOT NULL
        ORDER BY predicted_at
        """,
        (cutoff.isoformat(),),
    )
    rows = [
        dict(zip(["market_id", "predicted_at", "probability", "outcome", "cluster"], r))
        for r in cursor.fetchall()
    ]
    conn.close()
    return rows


def train_calibrator(
    observations: list[dict],
    method: str = "isotonic",
    timestamp_key: str = "predicted_at",
) -> dict:
    """Train a calibrator and return its parameters."""
    from .calibration import Observation

    obs = []
    for row in observations:
        pred_at = row[timestamp_key]
        if isinstance(pred_at, str):
            pred_at = datetime.fromisoformat(pred_at.replace("Z", "+00:00"))

        obs.append(
            Observation(
                market_id=row["market_id"],
                cluster=row.get("cluster", "default"),
                predicted_at=pred_at,
                label_known_at=pred_at,
                probability=float(row["probability"]),
                outcome=row["outcome"],
            )
        )

    if not obs:
        raise ValueError("no training observations")

    cutoff = max(o.predicted_at for o in obs)

    if method == "isotonic":
        calibrator = Isotonic()
        calibrator.fit(obs, cutoff)
        params = {
            "method": "isotonic",
            "starts": calibrator.starts,
            "values": calibrator.values,
            "cutoff": cutoff.isoformat(),
        }
    elif method == "platt":
        calibrator = Platt()
        calibrator.fit(obs, cutoff)
        params = {
            "method": "platt",
            "cutoff": cutoff.isoformat(),
        }
    elif method == "temperature":
        calibrator = TemperatureScaling()
        probs = [o.probability for o in obs]
        outcomes = [o.outcome for o in obs]
        calibrator.fit(probs, outcomes)
        params = {
            "method": "temperature",
            "temperature": calibrator.temperature,
        }
    else:
        raise ValueError(f"unknown method: {method}")

    return params


def evaluate_calibrator(observations: list[dict], params: dict) -> dict:
    """Evaluate a trained calibrator on held-out data."""
    probs = [float(o["probability"]) for o in observations]
    outcomes = [o["outcome"] for o in observations]
    return metrics(probs, outcomes)


def main(argv: list[str] | None = None):
    """CLI entry point for model training."""
    import argparse

    parser = argparse.ArgumentParser(description="Train probability calibrators")
    parser.add_argument("--config", default="config/base.toml")
    parser.add_argument("--db", help="SQLite database path")
    parser.add_argument(
        "--method", choices=["isotonic", "platt", "temperature"], default="isotonic"
    )
    parser.add_argument("--output", help="Output JSON path for trained parameters")
    parser.add_argument("--evaluate-only", action="store_true")
    args = parser.parse_args(argv)

    load_env()
    config = load_toml(args.config)

    db_path = args.db or config.get("database", {}).get("path", "data/polyalpha.db")
    output = args.output or f"data/calibrator_{args.method}.json"

    if not Path(db_path).exists():
        logger.error("database not found: %s", db_path)
        sys.exit(1)

    cutoff = datetime.now(UTC)
    observations = load_training_data(db_path, cutoff)

    if not observations:
        logger.error("no training data available")
        sys.exit(1)

    logger.info("loaded %d training observations", len(observations))

    if args.evaluate_only:
        params_path = Path(output)
        if not params_path.exists():
            logger.error("parameter file not found: %s", output)
            sys.exit(1)
        params = json.loads(params_path.read_text())
    else:
        params = train_calibrator(observations, method=args.method)
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(json.dumps(params, indent=2))
        logger.info("saved calibrator to %s", output)

    report = evaluate_calibrator(observations, params)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
