"""Capacity curves — how much capital the strategy can absorb.

Part of the live-ready execution architecture. Replays identical signals at
increasing notional sizes through the same book states and measures how the
executable edge decays with size. Alpha does not have unlimited capacity.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

D = Decimal
DEFAULT_NOTIONALS = (100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000)


@dataclass(frozen=True)
class CapacityPoint:
    notional_usd: Decimal
    executable_quantity: Decimal
    vwap: Decimal | None
    slippage: Decimal
    fees: Decimal
    remaining_edge: Decimal
    fill_fraction: Decimal

    def as_dict(self) -> dict:
        return {
            "notional_usd": str(self.notional_usd),
            "executable_quantity": str(self.executable_quantity),
            "vwap": str(self.vwap) if self.vwap is not None else None,
            "slippage": str(self.slippage),
            "fees": str(self.fees),
            "remaining_edge": str(self.remaining_edge),
            "fill_fraction": str(self.fill_fraction),
        }


@dataclass(frozen=True)
class CapacityCurve:
    token_id: str
    side: str
    fair_probability: Decimal
    points: list[CapacityPoint]

    def as_dict(self) -> dict:
        return {
            "token_id": self.token_id,
            "side": self.side,
            "fair_probability": str(self.fair_probability),
            "points": [p.as_dict() for p in self.points],
        }

    def capital_to_net_edge(self) -> list[tuple[str, str]]:
        return [(p.notional_usd.__str__(), p.remaining_edge.__str__()) for p in self.points]

    def capital_to_fill_rate(self) -> list[tuple[str, str]]:
        return [(p.notional_usd.__str__(), p.fill_fraction.__str__()) for p in self.points]


def _walk_depth(book: dict, side: str, limit_price: Decimal, shares: Decimal, fee_rate: Decimal):
    levels = book.get("asks" if side == "BUY" else "bids", [])
    remaining = shares
    legs: list[tuple[Decimal, Decimal]] = []
    for level in levels:
        price = D(str(level["price"]))
        if side == "BUY" and price > limit_price:
            break
        if side == "SELL" and price < limit_price:
            break
        available = D(str(level["size"]))
        qty = min(remaining, available)
        if qty > 0:
            legs.append((price, qty))
            remaining -= qty
        if remaining <= 0:
            break
    filled = shares - remaining
    if filled == 0:
        return D(0), None, D(0), D(0)
    notional = sum((p * q for p, q in legs), D(0))
    vwap = notional / filled
    fees = sum(
        ((q * fee_rate * p * (1 - p)).quantize(D("0.00001"), rounding=ROUND_HALF_UP) for p, q in legs),
        D(0),
    )
    best = D(str(levels[0]["price"])) if levels else vwap
    slippage = abs(vwap - best)
    return filled, vwap, fees, slippage


def capacity_curve(
    book: dict,
    token_id: str,
    side: str,
    fair_probability: Decimal,
    limit_price: Decimal,
    fee_rate: Decimal = D("0"),
    notionals: tuple[int, ...] = DEFAULT_NOTIONALS,
) -> CapacityCurve:
    """Compute the capital -> edge curve for one book snapshot."""
    points: list[CapacityPoint] = []
    for notional in notionals:
        n = D(str(notional))
        # Requested shares at the best price.
        levels = book.get("asks" if side == "BUY" else "bids", [])
        best = D(str(levels[0]["price"])) if levels else None
        if best is None or best == 0:
            continue
        requested = (n / best).quantize(D("0.01"))
        filled, vwap, fees, slippage = _walk_depth(book, side, limit_price, requested, fee_rate)
        fill_fraction = (filled / requested) if requested > 0 else D(0)
        if vwap is None:
            points.append(CapacityPoint(n, D(0), None, D(0), D(0), D(0), D(0)))
            continue
        # Remaining edge per share after execution costs.
        gross = (fair_probability - vwap) if side == "BUY" else (vwap - (1 - fair_probability))
        remaining_edge = gross - (fees / filled if filled else D(0)) - slippage
        points.append(
            CapacityPoint(
                notional_usd=n,
                executable_quantity=filled,
                vwap=vwap,
                slippage=slippage,
                fees=fees,
                remaining_edge=remaining_edge,
                fill_fraction=fill_fraction,
            )
        )
    return CapacityCurve(token_id=token_id, side=side, fair_probability=fair_probability, points=points)