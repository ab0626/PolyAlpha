"""Family E3 — physical-model residual + executable edge (both directions).

Implements ``docs/ENERGY_E3_SPEC.md`` v1. The model is a ``PhysicalPeakModel``
protocol supplying ``P(M_d > K | X_t)``; the market side is the Kalshi
executable quote. Edge is evaluated in BOTH directions so a large model-vs-mid
disagreement can never masquerade as tradeable alpha:

    Edge_YES = p_model - YES_ask - C
    Edge_NO  = (1 - p_model) - NO_ask - C
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from .kalshi import EnergyLadder
from .ledger import OpportunityRow

D = Decimal

# Frozen conservative placeholder (ENERGY_E3_SPEC v1); replace with the measured
# Kalshi fee schedule + adverse-selection buffer before any capital decision.
COST = D("0.02")


class PhysicalPeakModel(Protocol):
    def p_peak_gt(self, as_of: datetime, strike: Decimal) -> Decimal | None:
        """P(daily peak load > strike) using only information available at as_of."""
        ...


def residual(p_model: Decimal, p_mid: Decimal) -> Decimal:
    """Model-vs-mid residual (diagnostic reference, not an edge)."""
    return p_model - p_mid


def edge_yes(p_model: Decimal, yes_ask: Decimal, cost: Decimal = COST) -> Decimal:
    return p_model - yes_ask - cost


def edge_no(p_model: Decimal, no_ask: Decimal, cost: Decimal = COST) -> Decimal:
    return (D(1) - p_model) - no_ask - cost


def build_opportunities(
    model: PhysicalPeakModel,
    ladder: EnergyLadder,
    as_of: datetime,
    cost: Decimal = COST,
) -> list[OpportunityRow]:
    """One ledger row per strike, with both-direction executable edges."""
    rows: list[OpportunityRow] = []
    for r in ladder.rungs:
        if r.yes_bid is None or r.yes_ask is None:
            continue
        p_model = model.p_peak_gt(as_of, r.threshold)
        if p_model is None:
            continue
        e_yes = edge_yes(p_model, r.yes_ask, cost)
        e_no = edge_no(p_model, r.no_ask, cost) if r.no_ask is not None else None
        rows.append(OpportunityRow(
            observed_at=as_of.isoformat(),
            underlying_id=ladder.underlying.underlying_id,
            series_ticker=ladder.series_ticker,
            strike=str(r.threshold),
            direction=r.direction,
            p_model=str(p_model),
            yes_bid=str(r.yes_bid),
            yes_ask=str(r.yes_ask),
            no_bid=str(r.no_bid) if r.no_bid is not None else None,
            no_ask=str(r.no_ask) if r.no_ask is not None else None,
            yes_bid_size=str(r.yes_bid_size) if r.yes_bid_size is not None else "0",
            yes_ask_size=str(r.yes_ask_size) if r.yes_ask_size is not None else "0",
            cost=str(cost),
            edge_yes=str(e_yes),
            edge_no=str(e_no) if e_no is not None else None,
        ))
    return rows
