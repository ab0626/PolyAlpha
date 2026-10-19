"""E3 -> screen-edge -> TAKER ProdOrderIntent (LIVE_CALIBRATION_V0).

Taker-only calibration: the experiment is whether an observed executable edge
survives an actual cross. post_only=False and the VERIFIED TAKER fee rule.

    Edge_screen_YES = p_hat_E3       - yes_ask       - Fee_taker - B
    Edge_screen_NO  = (1 - p_hat_E3) - (1 - yes_bid) - Fee_taker - B

The Kalshi V2 adapter is single-book YES-leg ("bid" = buy YES, "ask" = sell
YES). So the NO taker is expressed NATIVELY as a marketable SELL YES at the YES
bid (buying NO at ``1 - yes_bid``), never a fragile no_ask->complement chain.
This mapping is proven by ``tests/test_kalshi_signal_e3.py``.

Single operating-day scope: E3's residual distribution is day-specific. The
caller must build a ladder for exactly ONE operating day via
``build_single_day_ladder`` (which rejects mixed-day market lists), and the
signal builder evaluates ``p_hat_E3`` for that same day.

Fail-closed per side (no intent unless ALL hold):
  - executable quote exists, 0 < price < 1, size > 0;
  - market is open at ``as_of`` (within [open, close));
  - fee rule is verified (taker rate);
  - screen edge >= MIN_SCREEN_EDGE.

Price protection: the intent is always a LIMIT at the observed executable price
(never an unbounded market order), so a book move beyond the measured price
never chases — it just does not fill / must be recomputed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from .energy.e3 import PhysicalPeakModel
from .energy.kalshi import EnergyLadder, LadderRung, build_ladder, ercot_operating_day
from .kalshi_exec import OrderSide, new_client_order_id
from .kalshi_exec_prod import MAX_NOTIONAL_PER_ORDER, MIN_SCREEN_EDGE
from .kalshi_fees import (
    SLIPPAGE_ADVERSE_SELECTION_BUFFER,
    FeeRule,
    fee_per_contract,
    screen_edge_per_contract,
)
from .kalshi_oms_prod import ProdOrderIntent

# Eligibility reasons (dry-run reports these; they are not tradeable labels).
REASON_ELIGIBLE = "ELIGIBLE"
REASON_WOULD_BE_ELIGIBLE = "WOULD_BE_ELIGIBLE_IF_FEE_VERIFIED"
REASON_NO_QUOTE = "NO_EXECUTABLE_QUOTE"
REASON_ZERO_SIZE = "ZERO_SIZE"
REASON_FEE_UNVERIFIED = "FEE_UNVERIFIED"
REASON_EDGE_BELOW = "EDGE_BELOW_MIN"
REASON_NO_MODEL = "NO_MODEL_PROBABILITY"
REASON_STALE = "STALE_MARKET"


@dataclass(frozen=True)
class SignalEval:
    """One per (strike, side) — the full fail-closed evaluation + optional intent."""

    operating_day: str
    ticker: str
    strike: Decimal
    side: str  # "YES" | "NO"
    p_hat_e3: Decimal | None
    executable_price: Decimal | None
    executable_size: Decimal | None
    taker_fee: Decimal | None
    buffer: Decimal
    screen_edge: Decimal | None
    eligibility_reason: str
    intent: ProdOrderIntent | None = None


def build_single_day_ladder(
    markets: list[dict], series_ticker: str, target_day: str | None = None
) -> tuple[str, EnergyLadder]:
    """Group markets by operating day and build a SINGLE-day ladder.

    Raises ``ValueError`` on a mixed-day list (or when ``target_day`` is absent),
    so a signal can never be evaluated with day-d1 strikes under day-d2 E3.
    """
    by_day: dict[str, list[dict]] = defaultdict(list)
    for m in markets:
        close = m.get("close_time")
        if not close:
            continue
        by_day[ercot_operating_day(close)].append(m)
    if not by_day:
        raise ValueError("no markets with a close_time")
    if target_day is not None:
        if target_day not in by_day:
            raise ValueError(f"target operating day {target_day!r} not present in markets")
        return target_day, build_ladder(series_ticker, by_day[target_day])
    days = sorted(by_day)
    if len(days) > 1:
        raise ValueError(f"mixed operating days in ladder: {days}")
    return days[0], build_ladder(series_ticker, by_day[days[0]])


def _floor_count(ask_size: Decimal | None, max_notional: Decimal) -> Decimal | None:
    if ask_size is None:
        return None
    count = min(max_notional, ask_size).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    return count if count >= Decimal("0.01") else None


def _market_open(ladder: EnergyLadder, as_of: datetime) -> bool:
    start = ladder.underlying.observation_window_start
    end = ladder.underlying.observation_window_end
    return (start is None or as_of >= start) and (end is None or as_of < end)


def _eval_side(
    ladder: EnergyLadder,
    rung: LadderRung,
    side: str,
    p_model: Decimal | None,
    fee_rule: FeeRule | None,
    market_open: bool,
    as_of: datetime,
    *,
    min_edge: Decimal,
    max_notional: Decimal,
    buffer: Decimal,
) -> SignalEval:
    target_day = ladder.underlying.operating_day or ""
    close_iso = (
        ladder.underlying.observation_window_end.isoformat()
        if ladder.underlying.observation_window_end is not None
        else None
    )

    # YES-leg native executable quote for each side.
    if side == "YES":
        exec_price = rung.yes_ask          # marketable BUY YES at the ask
        exec_size = rung.yes_ask_size
        model_prob = p_model
        order_side = OrderSide.BID
        order_price = exec_price
    else:  # NO
        # Effective buy-NO price = 1 - yes_bid; cross by SELLING YES at yes_bid.
        exec_price = Decimal(1) - rung.yes_bid if rung.yes_bid is not None else None
        exec_size = rung.yes_bid_size
        model_prob = (Decimal(1) - p_model) if p_model is not None else None
        order_side = OrderSide.ASK
        order_price = rung.yes_bid

    def make(reason, *, edge=None, taker_fee=None, intent=None):
        return SignalEval(
            operating_day=target_day,
            ticker=rung.ticker,
            strike=rung.threshold,
            side=side,
            p_hat_e3=p_model,
            executable_price=exec_price,
            executable_size=exec_size,
            taker_fee=taker_fee,
            buffer=buffer,
            screen_edge=edge,
            eligibility_reason=reason,
            intent=intent,
        )

    if p_model is None:
        return make(REASON_NO_MODEL)
    if not market_open:
        return make(REASON_STALE)
    if exec_price is None or order_price is None:
        return make(REASON_NO_QUOTE)
    # A displayed 0.00 / 1.00 quote is a degenerate/non-executable side.
    if not (Decimal("0") < exec_price < Decimal("1")) or not (
        Decimal("0") < order_price < Decimal("1")
    ):
        return make(REASON_NO_QUOTE)
    if exec_size is None or exec_size <= 0:
        return make(REASON_ZERO_SIZE)
    if fee_rule is None or not fee_rule.verified:
        return make(REASON_FEE_UNVERIFIED)

    taker_fee = fee_per_contract(exec_price, fee_rule.taker_rate)
    edge = screen_edge_per_contract(model_prob, exec_price, fee_rule, is_maker=False)
    if edge is None:
        return make(REASON_FEE_UNVERIFIED, taker_fee=taker_fee)
    if edge < min_edge:
        return make(REASON_EDGE_BELOW, edge=edge, taker_fee=taker_fee)

    count = _floor_count(exec_size, max_notional)
    if count is None:
        return make(REASON_ZERO_SIZE, edge=edge, taker_fee=taker_fee)

    intent = ProdOrderIntent(
        ticker=rung.ticker,
        side=order_side,
        price=order_price,
        count=count,
        client_order_id=new_client_order_id(),
        signal_timestamp=as_of.isoformat(),
        screen_edge=edge,
        expiry_deadline=close_iso,
        post_only=False,  # taker: cross deliberately
    )
    return make(REASON_ELIGIBLE, edge=edge, taker_fee=taker_fee, intent=intent)


def evaluate_ladder(
    model: PhysicalPeakModel,
    ladder: EnergyLadder,
    fee_rule: FeeRule | None,
    as_of: datetime,
    *,
    min_edge: Decimal = MIN_SCREEN_EDGE,
    max_notional: Decimal = MAX_NOTIONAL_PER_ORDER,
    buffer: Decimal = SLIPPAGE_ADVERSE_SELECTION_BUFFER,
) -> list[SignalEval]:
    """One SignalEval per (strike, side), with the full fail-closed reason."""
    target_day = ladder.underlying.operating_day or ""
    if not target_day:
        raise ValueError("ladder has no operating_day (single-day scope required)")
    market_open = _market_open(ladder, as_of)
    evals: list[SignalEval] = []
    for rung in ladder.rungs:
        p_model = model.p_peak_gt(target_day, as_of, rung.threshold)
        evals.append(
            _eval_side(ladder, rung, "YES", p_model, fee_rule, market_open, as_of,
                       min_edge=min_edge, max_notional=max_notional, buffer=buffer)
        )
        evals.append(
            _eval_side(ladder, rung, "NO", p_model, fee_rule, market_open, as_of,
                       min_edge=min_edge, max_notional=max_notional, buffer=buffer)
        )
    return evals


def build_signal_intents(
    model: PhysicalPeakModel,
    ladder: EnergyLadder,
    fee_rule: FeeRule | None,
    as_of: datetime,
    *,
    min_edge: Decimal = MIN_SCREEN_EDGE,
    max_notional: Decimal = MAX_NOTIONAL_PER_ORDER,
    buffer: Decimal = SLIPPAGE_ADVERSE_SELECTION_BUFFER,
) -> list[ProdOrderIntent]:
    """Emit only ELIGIBLE taker intents (unverified fee => [])."""
    return [
        e.intent
        for e in evaluate_ladder(
            model, ladder, fee_rule, as_of,
            min_edge=min_edge, max_notional=max_notional, buffer=buffer,
        )
        if e.intent is not None
    ]


def revalidate_quote(
    intent: ProdOrderIntent,
    fresh_exec_price: Decimal | None,
    fresh_size: Decimal | None = None,
) -> bool:
    """Pre-submit freshness check: reject if the book moved against the intent.

    ``fresh_exec_price`` is the current executable price in the intent's own
    direction: the YES ask for a BID (buy YES), the YES bid for an ASK (sell
    YES). Returns True only when the fresh quote is same-or-better AND genuine
    (0 < price < 1, size > 0 when provided). A moved book must NOT be chased —
    reject and recompute.
    """
    if fresh_exec_price is None:
        return False
    if not (Decimal("0") < fresh_exec_price < Decimal("1")):
        return False
    if fresh_size is not None and fresh_size <= 0:
        return False
    if intent.side == OrderSide.BID:
        return fresh_exec_price <= intent.price  # ask did not move up
    return fresh_exec_price >= intent.price      # bid did not move down