"""Long-only cash-equivalent accounting; entry fees are included in cost basis."""

from dataclasses import dataclass
from decimal import Decimal

from .execution import Fill, Order, walk

D = Decimal


@dataclass
class Position:
    token_id: str
    market_id: str
    event_id: str
    cluster: str
    category: str
    shares: Decimal = D(0)
    basis: Decimal = D(0)

    @property
    def average_cost(self):
        return self.basis / self.shares if self.shares else D(0)


class Portfolio:
    def __init__(self, cash=D("10000")):
        if not cash.is_finite() or cash <= 0:
            raise ValueError("invalid initial cash")
        self.initial_cash = self.cash = cash
        self.realized = self.fees = D(0)
        self.positions = {}
        self.applied = set()

    def apply(
        self,
        fill: Fill,
        market_id: str,
        event_id: str,
        cluster: str,
        category="unknown",
    ):
        if fill.order_id in self.applied:
            raise ValueError("duplicate fill")
        if fill.shares <= 0:
            raise ValueError("empty fill")
        position = self.positions.get(fill.token_id) or Position(
            fill.token_id, market_id, event_id, cluster, category
        )
        if (
            position.market_id,
            position.event_id,
            position.cluster,
            position.category,
        ) != (market_id, event_id, cluster, category):
            raise ValueError("position identity changed")
        if fill.side == "BUY":
            cost = fill.notional + fill.fees
            if cost > self.cash:
                raise ValueError("insufficient cash")
            self.cash -= cost
            position.shares += fill.shares
            position.basis += cost
        else:
            if fill.shares > position.shares:
                raise ValueError("cannot sell unowned shares")
            basis = position.basis * fill.shares / position.shares
            self.cash += fill.notional - fill.fees
            self.realized += fill.notional - fill.fees - basis
            position.shares -= fill.shares
            position.basis -= basis
        self.fees += fill.fees
        self.positions[fill.token_id] = position
        self.applied.add(fill.order_id)

    def settle(self, settlement_id, token, payout, known_at, at):
        if known_at > at or not payout.is_finite() or not 0 <= payout <= 1:
            raise ValueError("invalid or future settlement")
        if settlement_id in self.applied:
            raise ValueError("duplicate settlement")
        position = self.positions[token]
        proceeds = position.shares * payout
        self.cash += proceeds
        self.realized += proceeds - position.basis
        position.shares = position.basis = D(0)
        self.applied.add(settlement_id)

    def exposures(self, dimension=None):
        totals = {}
        for p in self.positions.values():
            key = getattr(p, dimension) if dimension else "total"
            totals[key] = totals.get(key, D(0)) + p.basis
        return totals

    def mark(self, books, schedules, at, consumed=None):
        midpoint, liquidation = self.cash, self.cash
        exit_fees = D(0)
        unliquidated = {}
        for token, p in self.positions.items():
            if not p.shares:
                continue
            book = books.get(token)
            if book is not None and book.received_at <= at and book.mid is not None:
                midpoint += p.shares * book.mid
            try:
                if book is None or token not in schedules:
                    raise ValueError("missing book or fee")
                fill = walk(
                    book,
                    Order("mark", token, "SELL", p.shares, at, allow_partial=True),
                    schedules[token],
                    at,
                    consumed=(consumed or {}).get(token),
                )
                liquidation += fill.notional - fill.fees
                exit_fees += fill.fees
                if fill.shares < p.shares:
                    unliquidated[token] = str(p.shares - fill.shares)
            except ValueError:
                unliquidated[token] = str(p.shares)
        return dict(
            midpoint_equity=midpoint,
            liquidation_equity=liquidation,
            estimated_exit_fees=exit_fees,
            unliquidated=unliquidated,
            unrealized=liquidation - self.cash - sum(self.exposures().values()),
        )
