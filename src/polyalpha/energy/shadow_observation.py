"""E3 shadow observation ledger — append-only, idempotent, full denominator.

Frozen by ``docs/ENERGY_E3_SPEC.md`` v2 observation rule. Every qualifying
prospective Kalshi snapshot is recorded once (deterministic ``observation_id``),
NOT only positive-edge ones: ``is_candidate`` is a derived flag, so later
inference has the full denominator for both

    P(win | Edge > x)   and   E[PnL | Edge > x, size >= q].

Resolution is a separate append-only stream keyed by ``observation_id`` so the
physical outcome and the venue settlement are both preserved (and can be checked
against each other) without mutating the observation rows.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

PROSPECTIVE_CUTOVER = datetime(2026, 10, 11, 3, 15, 0, tzinfo=UTC)
MIN_RESIDUALS = 5
MAX_LEAD = 6
FEES = Decimal("0.02")  # frozen conservative placeholder (ENERGY_E3_SPEC v1)

DEFAULT_OBSERVATIONS = "data/energy/shadow_observations.jsonl"
DEFAULT_RESOLUTIONS = "data/energy/shadow_resolutions.jsonl"


def _d(x: object) -> str:
    """Serialize a Decimal (or None) to a stable string."""
    return "" if x is None else str(x)


def build_observation_id(
    model_version: str,
    lead: int,
    market_ticker: str,
    strike: Decimal,
    kalshi_captured_at: str,
) -> str:
    """Deterministic idempotency key (frozen in the E3 spec v2 observation rule)."""
    raw = "|".join([
        model_version,
        str(lead),
        market_ticker,
        _d(strike),
        kalshi_captured_at,
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def observation_to_dict(obs: dict) -> dict:
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in obs.items()}


def append_jsonl(path: str | Path, rows: list[dict]) -> int:
    """Append rows to a JSONL file; returns bytes written. No-op if empty."""
    path = Path(path)
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(r, sort_keys=True, default=str) for r in rows) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)
    return len(text.encode("utf-8"))


def load_existing_ids(path: str | Path) -> set[str]:
    """observation_ids already recorded (idempotency set)."""
    path = Path(path)
    if not path.exists():
        return set()
    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("observation_id"):
            ids.add(data["observation_id"])
    return ids


def load_resolved_ids(path: str | Path) -> set[str]:
    """observation_ids that already have a resolution record."""
    path = Path(path)
    if not path.exists():
        return set()
    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("observation_id"):
            ids.add(data["observation_id"])
    return ids