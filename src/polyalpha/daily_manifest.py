"""Daily immutable manifests for the raw event layer.

Part of the v0.3.0 data-collection architecture. Every UTC day, produce a
manifest that fingerprints the day's raw files so any later mutation is
detectable. The manifest is itself written once (append-only) under
data/manifests/ and includes the deterministic raw-sha256-root, file list,
message counts, and collector/reconciliation counters.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .rawstore import RawStore


@dataclass(frozen=True)
class DailyManifest:
    date: date
    collector_commit: str
    config_hash: str
    raw_files: int
    raw_messages: int
    markets_observed: int
    resolved_markets: int
    raw_sha256_root: str
    dropped_connections: int
    reconciliations: int
    book_mismatches: int
    previous_manifest_sha256: str | None
    file_hashes: dict[str, str]

    def as_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "collector_commit": self.collector_commit,
            "config_hash": self.config_hash,
            "raw_files": self.raw_files,
            "raw_messages": self.raw_messages,
            "markets_observed": self.markets_observed,
            "resolved_markets": self.resolved_markets,
            "raw_sha256_root": self.raw_sha256_root,
            "dropped_connections": self.dropped_connections,
            "reconciliations": self.reconciliations,
            "book_mismatches": self.book_mismatches,
            "previous_manifest_sha256": self.previous_manifest_sha256,
            "file_hashes": self.file_hashes,
        }

    def combined_hash(self) -> str:
        canonical = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(1024 * 1024)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def build_daily_manifest(
    raw: RawStore,
    day: date,
    collector_commit: str,
    config_hash: str,
    markets_observed: int,
    resolved_markets: int,
    dropped_connections: int,
    reconciliations: int,
    book_mismatches: int,
    previous_manifest_sha256: str | None = None,
) -> DailyManifest:
    """Build the manifest for one UTC day by reading the raw store.

    The `previous_manifest_sha256` links this day to the prior day's manifest,
    forming a hash chain so mutation/deletion/reordering of historical
    manifests becomes detectable, not just mutation of each day's raw files.
    """
    directory = raw.day_dir(day)
    files = sorted(
        [p for p in directory.glob("*.jsonl*") if p.is_file()]
    )
    file_hashes = {p.name: file_sha256(p) for p in files}
    raw_messages = raw.count(day)
    return DailyManifest(
        date=day,
        collector_commit=collector_commit,
        config_hash=config_hash,
        raw_files=len(files),
        raw_messages=raw_messages,
        markets_observed=markets_observed,
        resolved_markets=resolved_markets,
        raw_sha256_root=raw.sha256_root(day),
        dropped_connections=dropped_connections,
        reconciliations=reconciliations,
        book_mismatches=book_mismatches,
        previous_manifest_sha256=previous_manifest_sha256,
        file_hashes=file_hashes,
    )


class ManifestWriter:
    """Append-only writer for daily manifests under a manifests directory.

    Each day's manifest records previous_manifest_sha256 (the combined hash of
    the prior day), forming a tamper-evident hash chain.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def previous_day_hash(self, day: date) -> str | None:
        """Combined hash of the most recent manifest strictly before `day`."""
        prev = None
        for p in sorted(self.root.glob("*.json")):
            try:
                d = date.fromisoformat(p.stem)
            except ValueError:
                continue
            if d < day and (prev is None or d > prev):
                prev = d
        if prev is None:
            return None
        m = self.read(prev)
        return m.combined_hash() if m else None

    def write(self, manifest: DailyManifest) -> Path:
        """Write the manifest file. Refuses to overwrite an existing day."""
        path = self.root / f"{manifest.date.isoformat()}.json"
        if path.exists():
            raise FileExistsError(
                f"manifest already exists for {manifest.date.isoformat()}: {path}"
            )
        # Auto-chain to the prior day if not already linked.
        if manifest.previous_manifest_sha256 is None:
            prev = self.previous_day_hash(manifest.date)
            manifest = DailyManifest(
                date=manifest.date,
                collector_commit=manifest.collector_commit,
                config_hash=manifest.config_hash,
                raw_files=manifest.raw_files,
                raw_messages=manifest.raw_messages,
                markets_observed=manifest.markets_observed,
                resolved_markets=manifest.resolved_markets,
                raw_sha256_root=manifest.raw_sha256_root,
                dropped_connections=manifest.dropped_connections,
                reconciliations=manifest.reconciliations,
                book_mismatches=manifest.book_mismatches,
                previous_manifest_sha256=prev,
                file_hashes=manifest.file_hashes,
            )
        payload = manifest.as_dict()
        payload["manifest_sha256"] = manifest.combined_hash()
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return path

    def read(self, day: date) -> DailyManifest | None:
        path = self.root / f"{day.isoformat()}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return DailyManifest(
            date=date.fromisoformat(data["date"]),
            collector_commit=data["collector_commit"],
            config_hash=data["config_hash"],
            raw_files=data["raw_files"],
            raw_messages=data["raw_messages"],
            markets_observed=data["markets_observed"],
            resolved_markets=data["resolved_markets"],
            raw_sha256_root=data["raw_sha256_root"],
            dropped_connections=data["dropped_connections"],
            reconciliations=data["reconciliations"],
            book_mismatches=data["book_mismatches"],
            previous_manifest_sha256=data.get("previous_manifest_sha256"),
            file_hashes=data["file_hashes"],
        )

    def verify(self, day: date, raw: RawStore) -> tuple[bool, str]:
        """Verify a day's manifest still matches the raw store."""
        manifest = self.read(day)
        if manifest is None:
            return False, f"no manifest for {day.isoformat()}"
        # 1) Per-file hashes: catches any byte-level mutation without parsing.
        directory = raw.day_dir(day)
        files = sorted([p for p in directory.glob("*.jsonl*") if p.is_file()])
        if [p.name for p in files] != sorted(manifest.file_hashes):
            return False, "raw file set changed since manifest"
        for path in files:
            if file_sha256(path) != manifest.file_hashes.get(path.name):
                return False, f"file {path.name} mutated"
        # 2) Aggregate count and root hash.
        actual_root = raw.sha256_root(day)
        if manifest.raw_sha256_root != actual_root:
            return False, "raw_sha256_root mismatch: raw layer mutated"
        actual_messages = raw.count(day)
        if manifest.raw_messages != actual_messages:
            return False, "raw_messages count mismatch"
        return True, "ok"