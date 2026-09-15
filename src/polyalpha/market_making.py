"""Market-making research: inventory skew, fill probability, adverse selection.

This module researches maker strategies separately from taker strategies.
Maker fills are intentionally pessimistic because naive maker backtests
massively overestimate performance due to adverse selection.
"""

from dataclasses import dataclass
from decimal import Decimal

D = Decimal


@dataclass(frozen=True)
class MakerQuote:
    """A maker quote with risk-adjusted pricing."""

    side: str  # BUY (bid) or SELL (ask)
    price: Decimal
    size: Decimal
    fair_value: Decimal
    inventory_skew: Decimal
    half_spread: Decimal
    risk_adjustment: Decimal

    def __post_init__(self):
        if self.side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        if not (0 < self.price < 1):
            raise ValueError("price must be in (0,1)")
        if self.size <= 0:
            raise ValueError("size must be positive")


@dataclass
class MakerInventory:
    """Tracks maker inventory for a single token."""

    token_id: str
    side: str  # net position: BUY=long, SELL=short, NONE=flat
    shares: Decimal = D(0)
    avg_entry: Decimal = D(0)
    total_spread_captured: Decimal = D(0)
    total_adverse_selection: Decimal = D(0)
    fill_count: int = 0

    @property
    def net_position(self) -> Decimal:
        """Signed position: positive = long, negative = short."""
        if self.side == "BUY":
            return self.shares
        elif self.side == "SELL":
            return -self.shares
        return D(0)


class MakerQuoter:
    """Generate risk-adjusted maker quotes.

    fair_value = model_probability
    bid_quote = fair - half_spread - inventory_skew_adjustment
    ask_quote = fair + half_spread + inventory_skew_adjustment

    Inventory adjustment:
    - If long: shift quotes downward (encourage selling)
    - If short: shift quotes upward (encourage buying)
    """

    def __init__(
        self,
        base_half_spread: Decimal = D("0.02"),
        inventory_skew_per_share: Decimal = D("0.001"),
        max_inventory: Decimal = D("1000"),
        risk_adjustment: Decimal = D("0.005"),
    ):
        self.base_half_spread = base_half_spread
        self.inventory_skew_per_share = inventory_skew_per_share
        self.max_inventory = max_inventory
        self.risk_adjustment = risk_adjustment

    def quote(
        self,
        fair_value: Decimal,
        inventory: MakerInventory,
        side: str = "BUY",
    ) -> MakerQuote:
        """Generate a risk-adjusted quote for either BUY or SELL side."""
        if side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        if not (0 <= fair_value <= 1):
            raise ValueError("fair value must be in [0,1]")

        # Inventory skew: positive when long (push price down), negative when short
        skew = inventory.net_position * self.inventory_skew_per_share

        # Effective half spread widens with inventory
        inventory_width = abs(skew)
        effective_half_spread = self.base_half_spread + inventory_width

        if side == "BUY":
            price = max(D("0.01"), fair_value - effective_half_spread - self.risk_adjustment)
        else:
            price = min(D("0.99"), fair_value + effective_half_spread + self.risk_adjustment)

        # Size limited by max inventory
        remaining_capacity = self.max_inventory - abs(inventory.net_position)
        size = max(D(0), remaining_capacity)

        return MakerQuote(
            side=side,
            price=price.quantize(D("0.01")),
            size=size,
            fair_value=fair_value,
            inventory_skew=skew,
            half_spread=effective_half_spread,
            risk_adjustment=self.risk_adjustment,
        )


@dataclass
class MakerFillAnalysis:
    """Analysis of a single maker fill for adverse selection measurement."""

    token_id: str
    side: str
    fill_price: Decimal
    fill_size: Decimal
    pre_fill_mid: Decimal
    post_fill_mid_1s: Decimal | None
    post_fill_mid_5s: Decimal | None
    post_fill_mid_60s: Decimal | None

    @property
    def adverse_selection_1s(self) -> Decimal | None:
        """Price movement against us 1 second after fill."""
        if self.post_fill_mid_1s is None:
            return None
        if self.side == "BUY":
            # We bought; adverse = price went down
            return self.pre_fill_mid - self.post_fill_mid_1s
        else:
            # We sold; adverse = price went up
            return self.post_fill_mid_1s - self.pre_fill_mid

    @property
    def adverse_selection_5s(self) -> Decimal | None:
        if self.post_fill_mid_5s is None:
            return None
        if self.side == "BUY":
            return self.pre_fill_mid - self.post_fill_mid_5s
        else:
            return self.post_fill_mid_5s - self.pre_fill_mid

    @property
    def adverse_selection_60s(self) -> Decimal | None:
        if self.post_fill_mid_60s is None:
            return None
        if self.side == "BUY":
            return self.pre_fill_mid - self.post_fill_mid_60s
        else:
            return self.post_fill_mid_60s - self.pre_fill_mid


class MakerPnLEstimator:
    """Estimate maker PnL with adverse selection.

    maker_PnL = spread_capture + rebates - adverse_selection - inventory_loss
    """

    def __init__(
        self,
        rebate_rate: Decimal = D("0"),
        adverse_selection_multiplier: Decimal = D("1.5"),
    ):
        self.rebate_rate = rebate_rate
        self.adverse_selection_multiplier = adverse_selection_multiplier

    def estimate_fill_pnl(
        self,
        fill: MakerFillAnalysis,
        spread_at_fill: Decimal,
    ) -> dict:
        """Estimate PnL for a single fill."""
        spread_capture = spread_at_fill * fill.fill_size

        # Adverse selection: conservatively use the worst available horizon
        adverse = D(0)
        for horizon in [
            fill.adverse_selection_60s,
            fill.adverse_selection_5s,
            fill.adverse_selection_1s,
        ]:
            if horizon is not None:
                adverse = max(adverse, horizon * self.adverse_selection_multiplier)
                break

        adverse_cost = adverse * fill.fill_size
        rebate = self.rebate_rate * fill.fill_size

        net_pnl = spread_capture + rebate - adverse_cost

        return dict(
            token_id=fill.token_id,
            side=fill.side,
            fill_price=str(fill.fill_price),
            fill_size=str(fill.fill_size),
            spread_capture=str(spread_capture),
            rebate=str(rebate),
            adverse_selection=str(adverse_cost),
            net_pnl=str(net_pnl),
            pre_fill_mid=str(fill.pre_fill_mid),
        )

    def aggregate_pnl(self, fill_pnls: list[dict]) -> dict:
        """Aggregate PnL across multiple fills."""
        total_capture = D(0)
        total_adverse = D(0)
        total_rebate = D(0)
        total_net = D(0)
        count = len(fill_pnls)

        for fp in fill_pnls:
            total_capture += D(fp["spread_capture"])
            total_adverse += D(fp["adverse_selection"])
            total_rebate += D(fp["rebate"])
            total_net += D(fp["net_pnl"])

        return dict(
            fill_count=count,
            total_spread_capture=str(total_capture),
            total_adverse_selection=str(total_adverse),
            total_rebates=str(total_rebate),
            total_net_pnl=str(total_net),
            average_net_pnl=str(total_net / count) if count else "0",
            adverse_selection_ratio=str(total_adverse / total_capture)
            if total_capture > 0
            else None,
        )


@dataclass
class MakerQueue:
    """Conservative maker queue: only fills via observed aggressive volume."""

    side: str
    price: Decimal
    remaining: Decimal
    queue_ahead: Decimal

    def __post_init__(self):
        if (
            self.side not in ("BUY", "SELL")
            or not 0 < self.price < 1
            or self.remaining <= 0
            or self.queue_ahead < 0
        ):
            raise ValueError("invalid maker queue")

    def on_trade(self, aggressor, price, quantity):
        """Only fill when matching aggressive volume exceeds queue-ahead."""
        if quantity < 0:
            raise ValueError("negative volume")
        expected = "SELL" if self.side == "BUY" else "BUY"
        if aggressor != expected or price != self.price:
            return Decimal(0)
        queue_consumed = min(quantity, self.queue_ahead)
        self.queue_ahead -= queue_consumed
        fill = min(self.remaining, quantity - queue_consumed)
        self.remaining -= fill
        return fill
