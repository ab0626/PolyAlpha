"""Experiment tracking framework for reproducible research.

Records git commit, model version, feature version, data hash,
configuration, and metrics for every experiment run.
"""

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class ExperimentRecord:
    """A single experiment record."""

    experiment_id: str
    git_commit: str | None
    source_sha256: str
    model_version: str
    feature_version: str
    training_window: str | None
    validation_window: str | None
    configuration: dict[str, Any]
    data_sha256: str
    metrics: dict[str, Any]
    created_at: str

    def to_dict(self) -> dict:
        return asdict(self)


class ExperimentTracker:
    """Track experiments with full provenance."""

    def __init__(self, output_dir: str | Path):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _git_commit(self) -> str | None:
        try:
            return subprocess.check_output(
                ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

    def _source_hash(self) -> str:
        package = Path(__file__).resolve().parent
        digest = hashlib.sha256()
        for path in sorted(package.rglob("*.py")):
            digest.update(path.relative_to(package).as_posix().encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def _data_hash(self, data_path: str | Path) -> str:
        digest = hashlib.sha256()
        with open(data_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def record(
        self,
        config: dict,
        metrics: dict,
        data_path: str | Path | None = None,
        model_version: str = "unknown",
        feature_version: str = "unknown",
        training_window: str | None = None,
        validation_window: str | None = None,
    ) -> ExperimentRecord:
        """Record a new experiment."""
        exp = ExperimentRecord(
            experiment_id=str(uuid4()),
            git_commit=self._git_commit(),
            source_sha256=self._source_hash(),
            model_version=model_version,
            feature_version=feature_version,
            training_window=training_window,
            validation_window=validation_window,
            configuration=config,
            data_sha256=self._data_hash(data_path) if data_path else "no_data",
            metrics=metrics,
            created_at=datetime.now(UTC).isoformat(),
        )

        path = self.output_dir / f"{exp.experiment_id}.json"
        with path.open("w", encoding="utf-8") as f:
            json.dump(exp.to_dict(), f, indent=2, default=str)

        return exp

    def list_experiments(self) -> list[dict]:
        """List all experiments in the output directory."""
        experiments = []
        for path in sorted(self.output_dir.glob("*.json")):
            try:
                with path.open(encoding="utf-8") as f:
                    experiments.append(json.load(f))
            except (json.JSONDecodeError, OSError):
                continue
        return experiments

    def load(self, experiment_id: str) -> dict | None:
        """Load a specific experiment by ID."""
        path = self.output_dir / f"{experiment_id}.json"
        if not path.exists():
            return None
        with path.open(encoding="utf-8") as f:
            return json.load(f)
