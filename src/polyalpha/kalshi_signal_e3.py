"""E3 -> screen-edge -> ProdOrderIntent wiring (LIVE_CALIBRATION_V0).

No modification to E3: ``p_hat_E3 = model.p_peak_gt(...)`` is used verbatim as
the empirical model probability (NOT a lower-confidence bound). The frozen
5c/contract screen hurdle (after the exact fee + frozen buffer) does the
screening:

    Edge_screen_YES = p_hat_E3       - yes_ask - fee - B
    Edge_screen_NO  = (1 - p_hat_E3) - no_ask  - fee - B

An intent is emitted only when the screen edge >= ``MIN_SCREEN_EDGE`` AND the
fee rule is verified. YES maps to ``side=BID`` at ``yes_ask``; NO maps to
``side=ASK`` at ``(1 - no_ask)`` (Kalshi V2 single-book YES-leg quoting). Intents
are post-only (maker/resting), matching the resting-fill lifecycle.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from .energy.e3 import PhysicalPeakModel
from .energy.kalshi import EnergyLadder, LadderRung
from .kalshi_exec import OrderSide, new_client_order_id
from .kalshi_exec_prod import MAX_NOTIONAL_PER_ORDER, MIN_SCREEN_EDGE
from .kalshi_fees import FeeRule, screen_edge_per_contract
from .kalshi_oms_prod import ProdOrderIntent


def _floor_count(ask_size: Decimal | None, max_notional: Decimal) -> Decimal | None:
    if ask_size is None:
        return None
    count = min(max_notional, ask_size).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    return count if count >= Decimal("0.01") else None


def screen_edges(
    model: PhysicalPeakModel,
    rung: LadderRung,
    target_day: str,
    as_of: datetime,
    fee_rule: FeeRule | None,
    *,
    is_maker: bool = True,
) -> tuple[Decimal | None, Decimal | None]:
    """Return (edge_yes, edge_no) per contract for one rung, or (None, None)."""
    p_model = model.p_peak_gt(target_day, as_of, rung.threshold)
    if p_model is None:
        return None, None
    edge_yes = None
    edge_no = None
    if rung.yes_ask is not None:
        edge_yes = screen_edge_per_contract(p_model, rung.yes_ask, fee_rule, is_maker=is_maker)
    if rung.no_ask is not None:
        edge_no = screen_edge_per_contract(
            Decimal(1) - p_model, rung.no_ask, fee_rule, is_maker=is_maker
        )
    return edge_yes, edge_no


def build_signal_intents(
    model: PhysicalPeakModel,
    ladder: EnergyLadder,
    fee_rule: FeeRule | None,
    as_of: datetime,
    *,
    min_edge: Decimal = MIN_SCREEN_EDGE,
    max_notional: Decimal = MAX_NOTIONAL_PER_ORDER,
    is_maker: bool = True,
) -> list[ProdOrderIntent]:
    """Emit a post-only intent for every rung whose screen edge clears the hurdle.

    An unverified/missing fee rule yields ``[]`` (fail-closed: no trade).
    """
    target_day = ladder.underlying.operating_day or ""
    close_iso = (
        ladder.underlying.observation_window_end.isoformat()
        if ladder.underlying.observation_window_end is not None
        else None
    )
    out: list[ProdOrderIntent] = []
    for rung in ladder.rungs:
        edge_yes, edge_no = screen_edges(
            model, rung, target_day, as_of, fee_rule, is_maker=is_maker
        )
        if edge_yes is not None and edge_yes >= min_edge:
            count = _floor_count(rung.yes_ask_size, max_notional)
            if count is not None and rung.ticker and rung.yes_ask is not None:
                out.append(
                    ProdOrderIntent(
                        ticker=rung.ticker,
                        side=OrderSide.BID,
                        price=rung.yes_ask,
                        count=count,
                        client_order_id=new_client_order_id(),
                        signal_timestamp=as_of.isoformat(),
                        screen_edge=edge_yes,
                        expiry_deadline=close_iso,
                        post_only=True,
                    )
                )
        if edge_no is not None and edge_no >= min_edge:
            # LadderRung carries no NO-side size; use the frozen micro-size
            # (post-only resting, so an unfilled tail is a measured outcome).
            count = max_notional
            if rung.ticker and rung.no_ask is not None:
                out.append(
                    ProdOrderIntent(
                        ticker=rung.ticker,
                        side=OrderSide.ASK,
                        price=Decimal(1) - rung.no_ask,
                        count=count,
                        client_order_id=new_client_order_id(),
                        signal_timestamp=as_of.isoformat(),
                        screen_edge=edge_no,
                        expiry_deadline=close_iso,
                        post_only=True,
                    )
                )
    return out