"""Shadow opportunity ledger for energy E3.

Append-only record of every candidate opportunity at a point in time:

    p_model, yes_bid, yes_ask, no_bid, no_ask, edge_buy_yes, edge_buy_no,
    depth (sizes), fees (cost), strike identity.

This is a measurement record, not an order. It exists so we can later compute
``E[net executable PnL | R]`` and, separately, model alpha vs. capturable alpha.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class OpportunityRow:
    observed_at: str
    underlying_id: str
    series_ticker: str
    strike: str
    direction: str
    p_model: str | None
    yes_bid: str
    yes_ask: str
    no_bid: str | None
    no_ask: str | None
    yes_bid_size: str
    yes_ask_size: str
    cost: str
    edge_yes: str | None
    edge_no: str | None

    def as_dict(self) -> dict:
        return asdict(self)


def append_opportunities(path: str | Path, rows: list[OpportunityRow]) -> int:
    """Append opportunity rows to JSONL; returns bytes written."""
    path = Path(path)
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = "\n".join(json.dumps(r.as_dict(), sort_keys=True) for r in rows) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(lines)
    return len(lines.encode("utf-8"))
