"""End-to-end information-propagation pipeline records (append-only, restart-safe).

Deterministic ids make the pipeline idempotent: a restart mid-release cannot
duplicate observations or silently retrain with altered history.

  shock_id       = H(event_id, scheduled_at)
  reaction_id    = H(shock_id, market_id)
  propagation_id = H(shock_id, source, target, horizon, estimator_version)

The propagation estimator's training cutoff is frozen at the SHOCK's evaluation
timestamp (training_end < t_shock), never at processing time.

Observed vs executable is kept separate: ``edge_mid_raw`` is computed from
mid/BBO; ``edge_exec`` is "UNAVAILABLE" until the VWAP/depth/fee path exists.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .propagation import PropagationSample, fit_propagation, predict_propagation


def _hid(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def shock_id(event_id: str, scheduled_at: datetime) -> str:
    return "shk_" + _hid(event_id, scheduled_at.isoformat())


def reaction_id(shock_id: str, market_id: str) -> str:
    return "rxn_" + _hid(shock_id, market_id)


def propagation_id(
    shock_id: str, source: str, target: str, horizon: int, estimator_version: str
) -> str:
    return "prp_" + _hid(shock_id, source, target, str(horizon), estimator_version)


@dataclass(frozen=True)
class PropagationEvidence:
    """One observed (not yet executable) cross-market propagation record."""

    propagation_id: str
    shock_id: str
    reaction_id: str
    source_market: str
    target_market: str
    horizon: int
    estimator_version: str
    source_delta: float
    observed_delta: float | None
    expected_delta: float
    z_lag_raw: float | None
    edge_mid_raw: float | None
    edge_exec: str
    n_events: int
    n_clusters: int
    training_start: str
    training_end: str
    observed_at: str

    def to_record(self) -> dict:
        return {
            "id": self.propagation_id,
            "kind": "OBSERVED_PROPAGATION",
            "shock_id": self.shock_id,
            "reaction_id": self.reaction_id,
            "source_market": self.source_market,
            "target_market": self.target_market,
            "horizon": self.horizon,
            "estimator_version": self.estimator_version,
            "source_delta": round(self.source_delta, 8),
            "observed_delta": round(self.observed_delta, 8) if self.observed_delta is not None else None,
            "expected_delta": round(self.expected_delta, 8),
            "z_lag_raw": round(self.z_lag_raw, 8) if self.z_lag_raw is not None else None,
            "edge_mid_raw": round(self.edge_mid_raw, 8) if self.edge_mid_raw is not None else None,
            "edge_exec": self.edge_exec,
            "n_events": self.n_events,
            "n_clusters": self.n_clusters,
            "training_start": self.training_start,
            "training_end": self.training_end,
            "observed_at": self.observed_at,
        }


def compute_propagation_evidence(
    shock_timestamp: datetime,
    shock: str,
    source_market: str,
    target_market: str,
    horizon: int,
    estimator_version: str,
    samples: list[PropagationSample],
    source_delta: float,
    observed_delta: float | None,
    regime: str = "",
    min_events: int = 20,
    min_clusters: int = 5,
    observed_at: datetime | None = None,
) -> PropagationEvidence | None:
    """Fit A->B using only shocks before ``shock_timestamp``, and record evidence.

    Returns None when a support gate fails (insufficient history). ``edge_exec``
    is always UNAVAILABLE until the reachable-VWAP/fee layer is implemented.
    """
    fit = fit_propagation(
        samples, source_market, target_market, regime,
        eval_time=shock_timestamp,  # FREEZE: training_end < t_shock
        min_events=min_events, min_clusters=min_clusters,
        version=estimator_version,
    )
    if fit is None:
        return None

    pred = predict_propagation(fit, source_delta, observed_delta)
    edge_mid_raw = -pred.lag_residual if pred.lag_residual is not None else None

    pid = propagation_id(shock, source_market, target_market, horizon, estimator_version)
    rid = reaction_id(shock, target_market)
    now = (observed_at or datetime.now(fit.training_end.tzinfo)).isoformat()

    return PropagationEvidence(
        propagation_id=pid,
        shock_id=shock,
        reaction_id=rid,
        source_market=source_market,
        target_market=target_market,
        horizon=horizon,
        estimator_version=estimator_version,
        source_delta=source_delta,
        observed_delta=observed_delta,
        expected_delta=pred.expected_delta,
        z_lag_raw=pred.z_lag,
        edge_mid_raw=edge_mid_raw,
        edge_exec="UNAVAILABLE",
        n_events=fit.n_events,
        n_clusters=fit.n_clusters,
        training_start=fit.training_start.isoformat(),
        training_end=fit.training_end.isoformat(),
        observed_at=now,
    )


def raw_provenance(
    raw_dir: str | Path,
    window_start: datetime,
    window_end: datetime,
    market_ids: tuple[str, ...] = (),
    root_hash: str | None = None,
) -> dict:
    """Immutable raw provenance for an evidence record.

    ``raw_store_root_hash`` pins the entire raw store at the time of the
    reaction; ``segment_sha256`` hashes the canonical envelope hashes of every
    raw record inside the source window, so any future Z_lag / R(h) / reaction
    timestamp traces back to the exact bytes that produced it.
    """
    from .rawstore import RawStore

    store = RawStore(raw_dir)
    if root_hash is None:
        root_hash = store.sha256_root()
    segment = hashlib.sha256()
    n = 0
    for record in store.replay():
        ts = datetime.fromtimestamp(record.received_at_ns / 1e9, UTC)
        if window_start <= ts <= window_end:
            segment.update(record.sha256.encode("utf-8"))
            n += 1
    return {
        "raw_store_root_hash": root_hash,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "market_ids": list(market_ids),
        "segment_sha256": segment.hexdigest(),
        "segment_records": n,
    }


class EvidenceStore:
    """Append-only, restart-safe JSONL store keyed by deterministic record id."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._ids = self._load_ids()

    def _load_ids(self) -> set[str]:
        if not self.path.exists():
            return set()
        ids: set[str] = set()
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ids.add(json.loads(line)["id"])
            except (json.JSONDecodeError, KeyError):
                continue
        return ids

    def append(self, record: dict) -> bool:
        rid = record.get("id")
        if rid is None:
            raise ValueError("record requires an 'id'")
        if rid in self._ids:
            return False  # idempotent (restart-safe)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        self._ids.add(rid)
        return True

    def count(self) -> int:
        return len(self._ids)
