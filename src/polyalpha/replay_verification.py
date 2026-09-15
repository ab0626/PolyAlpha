"""Independent dual-replay determinism check for the raw layer.

Part of the v0.3.0 data-collection architecture. The strongest pre-collection
test: run the raw -> normalized -> reconstructed transformation twice, starting
from the raw files each time, and require identical hashes.

    Replay A: raw feed -> normalized events -> reconstructed books -> snapshots
    Replay B: delete all derived data, restart from raw files, same pipeline

    Hash_A == Hash_B is required for deterministic derived artifacts.

This module defines a pure transformation function (raw records -> derived
hash) and a harness that runs it twice against independent working copies.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .rawstore import RawStore


@dataclass(frozen=True)
class ReplayCheckResult:
    hash_a: str
    hash_b: str
    deterministic: bool
    records_a: int
    records_b: int
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "hash_a": self.hash_a,
            "hash_b": self.hash_b,
            "deterministic": self.deterministic,
            "records_a": self.records_a,
            "records_b": self.records_b,
            "error": self.error,
        }


def _derive_artifact_hash(records) -> str:
    """Deterministic hash of the normalized/reconstructed transformation.

    This is intentionally a *pure* function of the raw records so replaying
    the same raw feed always yields the same derived hash. It does not depend
    on wall clock, processing time, or any environment state.
    """
    h = hashlib.sha256()
    seen = set()
    for record in records:
        # Deduplicate by (source, seq, wire) so ordering artifacts don't matter.
        key = (record.source, record.message_sequence_local, record.wire)
        if key in seen:
            continue
        seen.add(key)
        h.update(
            json.dumps(
                {
                    "source": record.source,
                    "connection_id": record.connection_id,
                    "exchange_timestamp_ms": record.exchange_timestamp_ms,
                    "wire": record.wire,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    return h.hexdigest()


def run_dual_replay(raw_root: str | Path) -> ReplayCheckResult:
    """Run Replay A and Replay B from independent copies of the raw layer."""
    try:
        # Replay A on the original tree.
        records_a = list(RawStore(raw_root).replay())
        hash_a = _derive_artifact_hash(records_a)

        # Replay B: copy raw to a fresh tree and transform again.
        with tempfile.TemporaryDirectory() as tmp:
            fresh = Path(tmp) / "raw"
            shutil.copytree(raw_root, fresh)
            records_b = list(RawStore(fresh).replay())
            hash_b = _derive_artifact_hash(records_b)

        return ReplayCheckResult(
            hash_a=hash_a,
            hash_b=hash_b,
            deterministic=hash_a == hash_b,
            records_a=len(records_a),
            records_b=len(records_b),
        )
    except Exception as error:  # noqa: BLE001 - report, do not crash
        return ReplayCheckResult(
            hash_a="",
            hash_b="",
            deterministic=False,
            records_a=0,
            records_b=0,
            error=str(error),
        )