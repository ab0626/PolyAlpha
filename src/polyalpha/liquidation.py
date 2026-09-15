"""Executable liquidation equity — realistic liquidation proceeds estimation.

For each position, walks the bid depth to estimate realistic liquidation
proceeds. Computes the gap between mid-based equity and actual executable
liquidation equity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

D = Decimal


@dataclass(frozen=True)
class PositionLiquidation:
    """Liquidation analysis for a single position."""

    token_id: str
    side: str
    shares: Decimal
    mid_price: Decimal
    mid_equity: Decimal
    liquidation_proceeds: Decimal
    liquidation_gap: Decimal
    slippage_pct: Decimal
    depth_levels_consumed: int
    avg_exit_price: Decimal
    worst_fill_price: Decimal
    best_fill_price: Decimal

    def summary(self) -> dict:
        return {
            "token_id": self.token_id,
            "side": self.side,
            "shares": str(self.shares),
            "mid_equity": str(self.mid_equity),
            "liquidation_proceeds": str(self.liquidation_proceeds),
            "liquidation_gap": str(self.liquidation_gap),
            "slippage_pct": str(self.slippage_pct),
        }


@dataclass(frozen=True)
class LiquidationReport:
    """Aggregate liquidation analysis across all positions."""

    positions: list[PositionLiquidation] = field(default_factory=list)
    total_mid_equity: Decimal = D(0)
    total_liquidation_equity: Decimal = D(0)
    total_gap: Decimal = D(0)
    avg_slippage_pct: Decimal = D(0)
    max_single_position_gap: Decimal = D(0)
    gap_to_equity_ratio: Decimal = D(0)

    def summary(self) -> dict:
        return {
            "total_mid_equity": str(self.total_mid_equity),
            "total_liquidation_equity": str(self.total_liquidation_equity),
            "total_gap": str(self.total_gap),
            "avg_slippage_pct": str(self.avg_slippage_pct),
            "gap_to_equity_ratio": str(self.gap_to_equity_ratio),
            "positions": [p.summary() for p in self.positions],
        }


def _walk_depth(
    shares: Decimal,
    side: str,
    book_levels: list[dict],
) -> tuple[Decimal, int, Decimal, Decimal, Decimal]:
    """Walk book depth to estimate realistic liquidation proceeds.

    For a SELL position: walk bid levels from best bid downward.
    For a BUY position: walk ask levels from best ask upward.

    Returns:
        (proceeds, levels_consumed, avg_price, worst_price, best_price)
    """
    remaining = shares
    proceeds = D(0)
    levels_consumed = 0
    fill_prices: list[Decimal] = []

    # Sort levels: bids descending (best first), asks ascending (best first)
    if side == "SELL":
        sorted_levels = sorted(book_levels, key=lambda lv: lv["price"], reverse=True)
    else:
        sorted_levels = sorted(book_levels, key=lambda lv: lv["price"])

    for level in sorted_levels:
        if remaining <= 0:
            break
        level_price = level["price"]
        level_size = level["size"]
        fill_amount = min(remaining, level_size)
        proceeds += fill_amount * level_price
        remaining -= fill_amount
        levels_consumed += 1
        fill_prices.append(level_price)

    avg_price = proceeds / shares if shares > 0 else D(0)
    worst_price = fill_prices[-1] if fill_prices else D(0)
    best_price = fill_prices[0] if fill_prices else D(0)

    return proceeds, levels_consumed, avg_price, worst_price, best_price


def _compute_slippage_pct(
    mid_price: Decimal,
    avg_exit_price: Decimal,
    side: str,
) -> Decimal:
    """Compute slippage as percentage of mid price."""
    if mid_price <= 0:
        return D(0)
    if side == "SELL":
        slippage = (mid_price - avg_exit_price) / mid_price
    else:
        slippage = (avg_exit_price - mid_price) / mid_price
    return (abs(slippage) * D(100)).quantize(D("0.01"))


def compute_liquidation_equity(
    positions: list[dict],
    books: dict[str, list[dict]],
) -> LiquidationReport:
    """Compute executable liquidation equity for all positions.

    For each position, walks the bid/ask depth to estimate realistic
    liquidation proceeds. The liquidation gap represents the difference
    between mid-based accounting and executable value.

    Args:
        positions: List of position dicts with keys:
            - token_id: str
            - side: str ("LONG" or "SHORT")
            - shares: Decimal
            - mid_price: Decimal
            - basis: Decimal (cost basis per share)
        books: Dict mapping token_id -> list of book level dicts with keys:
            - price: Decimal
            - size: Decimal
            - side: str ("bid" or "ask")

    Returns:
        LiquidationReport with per-position and aggregate analysis.
    """
    results: list[PositionLiquidation] = []
    total_mid = D(0)
    total_liq = D(0)
    max_gap = D(0)

    for pos in positions:
        token_id = pos["token_id"]
        shares = pos["shares"]
        mid_price = pos["mid_price"]
        # Map LONG/SHORT to BUY/SELL for liquidation direction
        side = "SELL" if pos.get("side", "LONG") == "LONG" else "BUY"

        mid_equity = shares * mid_price
        total_mid += mid_equity

        book_levels = books.get(token_id, [])
        if not book_levels or shares <= 0:
            liq_proceeds = mid_equity
            results.append(
                PositionLiquidation(
                    token_id=token_id,
                    side=side,
                    shares=shares,
                    mid_price=mid_price,
                    mid_equity=mid_equity.quantize(D("0.01")),
                    liquidation_proceeds=liq_proceeds.quantize(D("0.01")),
                    liquidation_gap=D(0),
                    slippage_pct=D(0),
                    depth_levels_consumed=0,
                    avg_exit_price=mid_price,
                    worst_fill_price=mid_price,
                    best_fill_price=mid_price,
                )
            )
            total_liq += liq_proceeds
            continue

        proceeds, levels, avg_price, worst, best = _walk_depth(shares, side, book_levels)
        gap = mid_equity - proceeds
        slippage = _compute_slippage_pct(mid_price, avg_price, side)

        total_liq += proceeds
        max_gap = max(max_gap, gap)

        results.append(
            PositionLiquidation(
                token_id=token_id,
                side=side,
                shares=shares,
                mid_price=mid_price,
                mid_equity=mid_equity.quantize(D("0.01")),
                liquidation_proceeds=proceeds.quantize(D("0.01")),
                liquidation_gap=gap.quantize(D("0.01")),
                slippage_pct=slippage,
                depth_levels_consumed=levels,
                avg_exit_price=avg_price.quantize(D("0.0001")),
                worst_fill_price=worst,
                best_fill_price=best,
            )
        )

    total_gap = total_mid - total_liq
    avg_slip = (
        sum(p.slippage_pct for p in results) / D(len(results)) if results else D(0)
    )
    gap_ratio = (total_gap / total_mid * D(100)) if total_mid > 0 else D(0)

    return LiquidationReport(
        positions=results,
        total_mid_equity=total_mid.quantize(D("0.01")),
        total_liquidation_equity=total_liq.quantize(D("0.01")),
        total_gap=total_gap.quantize(D("0.01")),
        avg_slippage_pct=avg_slip.quantize(D("0.01")),
        max_single_position_gap=max_gap.quantize(D("0.01")),
        gap_to_equity_ratio=gap_ratio.quantize(D("0.01")),
    )
