"""Reproducibility, experiment manifest, and dataset fingerprinting.

Parts 66-68: Ensures experiments are reproducible via deterministic seeds,
dataset fingerprinting via SHA-256 hashing, and experiment manifests
that capture full provenance for comparing runs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, TypeAlias

D = Decimal

ExperimentId: TypeAlias = str
DatasetHash: TypeAlias = str


@dataclass(frozen=True)
class ExperimentManifest:
    """Complete manifest for an experiment run."""

    experiment_id: str
    config_hash: str
    dataset_fingerprint: str
    model_version: str
    git_commit: str
    created_at: str
    python_version: str
    platform: str
    base_seed: int
    deterministic_seed: int
    dataset_size: int
    feature_count: int
    categories: list[str]
    parameters: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to JSON-serializable dict."""
        return {
            "experiment_id": self.experiment_id,
            "config_hash": self.config_hash,
            "dataset_fingerprint": self.dataset_fingerprint,
            "model_version": self.model_version,
            "git_commit": self.git_commit,
            "created_at": self.created_at,
            "python_version": self.python_version,
            "platform": self.platform,
            "base_seed": self.base_seed,
            "deterministic_seed": self.deterministic_seed,
            "dataset_size": self.dataset_size,
            "feature_count": self.feature_count,
            "categories": self.categories,
            "parameters": self.parameters,
            "tags": self.tags,
        }

    def to_json(self, indent: int = 2) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict) -> ExperimentManifest:
        """Deserialize from dict."""
        return cls(
            experiment_id=data["experiment_id"],
            config_hash=data["config_hash"],
            dataset_fingerprint=data["dataset_fingerprint"],
            model_version=data["model_version"],
            git_commit=data["git_commit"],
            created_at=data["created_at"],
            python_version=data.get("python_version", ""),
            platform=data.get("platform", ""),
            base_seed=data.get("base_seed", 0),
            deterministic_seed=data.get("deterministic_seed", 0),
            dataset_size=data.get("dataset_size", 0),
            feature_count=data.get("feature_count", 0),
            categories=data.get("categories", []),
            parameters=data.get("parameters", {}),
            tags=data.get("tags", []),
        )


@dataclass(frozen=True)
class ReproducibilityResult:
    """Result of comparing two experiment manifests."""

    match: bool
    fields_matched: list[str]
    fields_differed: list[str]
    config_identical: bool
    dataset_identical: bool
    model_identical: bool
    seed_identical: bool
    summary: str


def _sha256(data: str) -> str:
    """Compute SHA-256 hex digest of a string."""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def deterministic_seed(base_seed: int, experiment_id: str) -> int:
    """Compute a deterministic seed from base seed and experiment ID.

    Ensures the same experiment_id always produces the same seed,
    regardless of when or where it's run.
    """
    seed_str = f"{base_seed}:{experiment_id}"
    hash_hex = hashlib.sha256(seed_str.encode("utf-8")).hexdigest()
    # Take first 8 hex chars -> 32-bit int
    return int(hash_hex[:8], 16)


def compute_dataset_fingerprint(
    observation_ids: list[str],
    timestamps: list[str],
    labels: list[int],
    schema_fields: list[str],
) -> DatasetHash:
    """Compute SHA-256 fingerprint of a dataset.

    Normalizes the data before hashing:
    - Observation IDs sorted
    - Timestamps sorted
    - Labels in corresponding order
    - Schema fields sorted

    Returns hex digest string.
    """
    # Sort observation IDs for determinism
    sorted_ids = sorted(observation_ids)
    sorted_schema = sorted(schema_fields)

    # Build normalized representation
    normalized = {
        "observation_ids": sorted_ids,
        "timestamps": sorted(timestamps),
        "labels": labels,
        "schema": sorted_schema,
        "count": len(observation_ids),
    }

    # Deterministic JSON serialization
    canonical = json.dumps(normalized, sort_keys=True, ensure_ascii=True)
    return _sha256(canonical)


def create_experiment_manifest(
    config: dict[str, Any],
    dataset_size: int,
    feature_count: int,
    categories: list[str],
    model_version: str = "v1.0",
    git_commit: str = "unknown",
    base_seed: int = 42,
    experiment_id: str | None = None,
    tags: list[str] | None = None,
) -> ExperimentManifest:
    """Create a complete experiment manifest.

    Args:
        config: Experiment configuration dict (will be hashed).
        dataset_size: Number of observations in dataset.
        feature_count: Number of features.
        categories: List of category labels in dataset.
        model_version: Version identifier for the model.
        git_commit: Git commit hash.
        base_seed: Base random seed.
        experiment_id: Unique experiment identifier (auto-generated if None).
        tags: Optional tags for categorization.

    Returns:
        ExperimentManifest with all provenance captured.
    """
    import platform as _platform
    import sys

    if experiment_id is None:
        # Generate deterministic experiment ID from config and timestamp
        config_str = json.dumps(config, sort_keys=True, ensure_ascii=True)
        ts = datetime.utcnow().isoformat()
        experiment_id = _sha256(f"{config_str}:{ts}")[:12]

    det_seed = deterministic_seed(base_seed, experiment_id)
    config_hash = _sha256(json.dumps(config, sort_keys=True, ensure_ascii=True))

    return ExperimentManifest(
        experiment_id=experiment_id,
        config_hash=config_hash,
        dataset_fingerprint="",  # caller should set this
        model_version=model_version,
        git_commit=git_commit,
        created_at=datetime.utcnow().isoformat(),
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        platform=_platform.platform(),
        base_seed=base_seed,
        deterministic_seed=det_seed,
        dataset_size=dataset_size,
        feature_count=feature_count,
        categories=sorted(categories),
        parameters=config,
        tags=tags or [],
    )


def verify_reproducibility(
    manifest1: ExperimentManifest,
    manifest2: ExperimentManifest,
) -> ReproducibilityResult:
    """Compare two experiment manifests for reproducibility.

    Determines whether two runs used identical configuration, data,
    and seeds. Differences in timestamps or platform are informational.
    """
    fields_matched: list[str] = []
    fields_differed: list[str] = []

    # Core reproducibility fields
    check_fields = [
        ("config_hash", "config"),
        ("dataset_fingerprint", "dataset"),
        ("model_version", "model"),
        ("deterministic_seed", "seed"),
        ("git_commit", "code"),
        ("feature_count", "features"),
        ("dataset_size", "data_size"),
    ]

    config_identical = manifest1.config_hash == manifest2.config_hash
    dataset_identical = manifest1.dataset_fingerprint == manifest2.dataset_fingerprint
    model_identical = manifest1.model_version == manifest2.model_version
    seed_identical = manifest1.deterministic_seed == manifest2.deterministic_seed

    for field_name, category in check_fields:
        v1 = getattr(manifest1, field_name)
        v2 = getattr(manifest2, field_name)
        if v1 == v2:
            fields_matched.append(field_name)
        else:
            fields_differed.append(field_name)

    all_identical = len(fields_differed) == 0
    if all_identical:
        summary = "Fully reproducible: all core fields match."
    else:
        summary = (
            f"NOT fully reproducible. Differing fields: {', '.join(fields_differed)}. "
            f"Matching fields: {', '.join(fields_matched)}."
        )

    return ReproducibilityResult(
        match=all_identical,
        fields_matched=fields_matched,
        fields_differed=fields_differed,
        config_identical=config_identical,
        dataset_identical=dataset_identical,
        model_identical=model_identical,
        seed_identical=seed_identical,
        summary=summary,
    )
