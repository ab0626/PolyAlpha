"""E4 evidence classifier — derived, read-only, future-only cutover (E4-v0).

Classifies already-captured ERCOT SPP rows (``observations.jsonl``) against the
exact E4 spec commit time WITHOUT rewriting the store. The first post-cutover
API pull may contain intervals that were economically observable before the
preregistration, so prospectivity is the conservative conjunction:

    PROSPECTIVE  <=>  is_first_seen AND received_at > t_c AND interval_end > t_c

Anything whose interval ENDS at or before the cutover is bootstrap, even if we
downloaded it afterward. Closed taxonomy (fail-closed where time is ambiguous):

    PROSPECTIVE_FIRST_SEEN         is_first_seen AND received>t_c AND interval_end>t_c
    BOOTSTRAP_PRE_CUTOVER_INTERVAL interval_end <= t_c (regardless of receipt/revision)
    RECEIPT_PRE_CUTOVER            interval_end > t_c but received_at <= t_c
    REVISION_POST_CUTOVER          not first-seen, post-cutover interval + receipt
    UNRESOLVED_TIME_SEMANTICS      interval ending / receipt cannot be resolved

UNRESOLVED_TIME_SEMANTICS never enters an E4 empirical denominator. No model, no
signal, no execution authority.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ERCOT_TZ = ZoneInfo("America/Chicago")

# E4 preregistration commit (docs/ENERGY_E4_SPEC.md) and its commit time.
E4_SPEC_COMMIT = "7b4d3e7781113e9f94f1a4c7222b55602fcd65ed"
E4_CUTOVER_AT = datetime(2026, 10, 19, 15, 5, 32, tzinfo=UTC)

EVIDENCE_PROSPECTIVE = "PROSPECTIVE_FIRST_SEEN"
EVIDENCE_BOOTSTRAP = "BOOTSTRAP_PRE_CUTOVER_INTERVAL"
EVIDENCE_REVISION = "REVISION_POST_CUTOVER"
EVIDENCE_RECEIPT_PRE = "RECEIPT_PRE_CUTOVER"
EVIDENCE_UNRESOLVED = "UNRESOLVED_TIME_SEMANTICS"


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def interval_end_utc(obs: dict) -> datetime | None:
    """ERCOT interval-ending time in UTC, or None when unresolvable.

    ERCOT convention: ``delivery_hour`` (1..24) is the ENDING hour and
    ``delivery_interval`` (1..4) is the 15-minute sub-interval, so the interval
    ends at ``(hour-1)*60 + interval*15`` minutes after Central midnight. A
    repeated DST hour (``repeated_hour_flag == "Y"``) is ambiguous and fails
    closed (None), as does any malformed/missing field.
    """
    try:
        day = date.fromisoformat(str(obs.get("operating_day") or "").strip())
    except (ValueError, TypeError):
        return None
    try:
        hour = int(obs.get("delivery_hour"))
        interval = int(obs.get("delivery_interval"))
    except (ValueError, TypeError):
        return None
    if not (1 <= hour <= 24) or not (1 <= interval <= 4):
        return None
    flag = str(obs.get("repeated_hour_flag") or "").strip().upper()
    if flag != "N":
        # 'Y' (repeated DST hour) or unknown => ambiguous interval ending.
        return None
    minutes = (hour - 1) * 60 + interval * 15
    local = datetime(day.year, day.month, day.day, 0, 0, tzinfo=ERCOT_TZ) + timedelta(minutes=minutes)
    return local.astimezone(UTC)


def _source_row_hash(obs: dict) -> str:
    return hashlib.sha256(
        json.dumps(obs, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def classify(
    received: datetime | None,
    interval_end: datetime | None,
    is_first: bool,
) -> tuple[str, str]:
    """Pure classification decision (evidence_class, reason).

    Decision order is conservative: bootstrap (interval_end <= cutover) before
    receipt-pre-cutover before revision, and unresolved fails closed.
    """
    if interval_end is None:
        return EVIDENCE_UNRESOLVED, "interval ending unrecoverable (malformed/missing field or DST-repeated hour)"
    if interval_end <= E4_CUTOVER_AT:
        return EVIDENCE_BOOTSTRAP, f"interval_end {interval_end.isoformat()} <= cutover (bootstrap, even if downloaded later)"
    if received is None:
        return EVIDENCE_UNRESOLVED, "received_at malformed/unparseable"
    if received <= E4_CUTOVER_AT:
        return EVIDENCE_RECEIPT_PRE, f"received_at {received.isoformat()} <= cutover (pre-registration receipt)"
    if not is_first:
        return EVIDENCE_REVISION, "post-cutover revision (not first-seen)"
    return EVIDENCE_PROSPECTIVE, "prospective first-seen (received > cutover, interval_end > cutover)"


def classify_observation(obs: dict) -> dict:
    """Classify one already-captured SPP row (read-only; returns a derived record)."""
    received = _parse_iso(obs.get("received_at"))
    interval_end = interval_end_utc(obs)
    is_first = bool(obs.get("is_first_seen"))
    evidence_class, reason = classify(received, interval_end, is_first)

    return {
        "e4_spec_commit": E4_SPEC_COMMIT,
        "e4_cutover_at": E4_CUTOVER_AT.isoformat(),
        "natural_key": obs.get("natural_key"),
        "interval_end_utc": interval_end.isoformat() if interval_end else None,
        "received_at": received.isoformat() if received else obs.get("received_at"),
        "is_first_seen": is_first,
        "evidence_class": evidence_class,
        "qualifies_forward_e4": evidence_class == EVIDENCE_PROSPECTIVE,
        "reason": reason,
        "source_row_hash": _source_row_hash(obs),
        "classified_at": datetime.now(UTC).isoformat(),
    }


def classify_store(observations_path: str | Path) -> list[dict]:
    """Classify every row of an observations.jsonl store (read-only)."""
    p = Path(observations_path)
    out: list[dict] = []
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obs = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obs, dict):
            continue
        out.append(classify_observation(obs))
    return out


def append_evidence(path: str | Path, records: list[dict]) -> tuple[int, int]:
    """Append derived evidence records idempotently (keyed by source_row_hash).

    Returns (appended, seen). Never rewrites an existing row.
    """
    p = Path(path)
    existing: set[str] = set()
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("source_row_hash"):
                existing.add(d["source_row_hash"])
    fresh = [r for r in records if r["source_row_hash"] not in existing]
    if fresh:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            for r in fresh:
                fh.write(json.dumps(r, sort_keys=True, default=str) + "\n")
    return len(fresh), len(records)