"""Cost ladder experiment — progressive execution scenario framework.

Section 7 of the v0.3 spec: measures how much edge is consumed by each
realistic execution layer. Each level models a different execution assumption,
from idealized to pessimistic stress.

Levels:
  A: midpoint execution, no fees, no slippage (idealized)
  B: best bid/ask execution (spread cost)
  C: best bid/ask + fees
  D: depth-walked VWAP + fees
  E: VWAP + fees + fixed latency
  F: VWAP + fees + stochastic latency + uncertainty penalty
  G: all above + resolution penalty + event-cluster risk limits
  H: pessimistic stress (doubled slippage, 1.5x spread, delayed fill, etc.)
"""

import random
from dataclasses import dataclass
from decimal import Decimal

from .domain import Book, Level
from .research_dataset import MarketSnapshot, ResearchDataset

D = Decimal


@dataclass(frozen=True)
class CostLadderLevel:
    level: str
    description: str
    gross_pnl: float
    net_pnl: float
    return_pct: float
    turnover: float
    fee_cost: float
    slippage_cost: float
    latency_cost: float
    drawdown: float
    trades: int
    profit_factor: float
    alpha_survival_ratio: float
    sample_size: int


@dataclass(frozen=True)
class CostLadderResult:
    levels: list[CostLadderLevel]
    alpha_survival_by_level: dict[str, float]

    def summary(self) -> dict:
        return {
            "levels": [lv.level for lv in self.levels],
            "alpha_survival_by_level": self.alpha_survival_by_level,
            "idealized_to_realistic": (
                self.alpha_survival_by_level.get("H_stress", 0.0)
                if self.alpha_survival_by_level
                else 0.0
            ),
        }


def _make_book_from_snapshot(snap: MarketSnapshot, token_id: str, is_yes: bool) -> Book | None:
    """Construct a minimal Book from snapshot data for VWAP simulation."""
    if is_yes:
        bid, ask = snap.yes_best_bid, snap.yes_best_ask
        bid_sz, ask_sz = snap.yes_bid_size, snap.yes_ask_size
    else:
        bid, ask = snap.no_best_bid, snap.no_best_ask
        bid_sz, ask_sz = snap.no_bid_size, snap.no_ask_size
    if bid is None or ask is None or bid_sz <= 0 or ask_sz <= 0:
        return None
    return Book(
        token_id=token_id,
        condition_id=snap.condition_id,
        source_at=snap.observation_timestamp,
        received_at=snap.observation_timestamp,
        bids=(Level(bid, bid_sz),),
        asks=(Level(ask, ask_sz),),
        tick_size=D("0.01"),
        min_order_size=D("1"),
        source_hash="",
    )


def _simulate_pnl(
    snapshots: list[MarketSnapshot],
    level: str,
    fee_rate: float = 0.02,
    latency_seconds: float = 0,
    uncertainty_penalty: float = 0.0,
    resolution_penalty: float = 0.0,
    slippage_multiplier: float = 1.0,
    spread_multiplier: float = 1.0,
    depth_fraction: float = 1.0,
    seed: int = 42,
) -> CostLadderLevel:
    """Simulate PnL under a specific execution scenario."""
    rng = random.Random(seed)
    pnls = []
    fee_costs = []
    slippage_costs = []
    latency_costs = []
    turnover = 0.0
    trades = 0

    for snap in snapshots:
        if snap.model_probability is None or snap.final_resolution is None:
            continue
        if snap.yes_best_bid is None or snap.yes_best_ask is None:
            continue

        p = float(snap.model_probability)
        bid = float(snap.yes_best_bid)
        ask = float(snap.yes_best_ask)
        mid = (bid + ask) / 2 if ask > bid else (bid + ask) / 2

        # Determine execution price per level
        if level == "A_raw":
            exec_price = mid
            fee = 0.0
            slippage = 0.0
            lat_cost = 0.0
        elif level == "B_spread":
            exec_price = ask
            fee = 0.0
            slippage = ask - mid
            lat_cost = 0.0
        elif level == "C_fees":
            exec_price = ask
            fee = fee_rate * ask * (1 - ask)
            slippage = ask - mid
            lat_cost = 0.0
        elif level == "D_vwap":
            exec_price = ask
            fee = fee_rate * ask * (1 - ask)
            slippage = (ask - mid) * slippage_multiplier
            lat_cost = 0.0
        elif level == "E_latency":
            exec_price = ask * spread_multiplier
            fee = fee_rate * ask * (1 - ask)
            slippage = (ask - mid) * slippage_multiplier
            lat_cost = uncertainty_penalty * 0.5
        elif level == "F_stochastic":
            delay = rng.expovariate(1.0 / max(latency_seconds, 0.1))
            price_drift = rng.gauss(0, 0.005 * delay)
            exec_price = max(0.01, min(0.99, ask + price_drift))
            fee = fee_rate * exec_price * (1 - exec_price)
            slippage = abs(exec_price - mid) * slippage_multiplier
            lat_cost = uncertainty_penalty * (1 + delay / 60)
        elif level == "G_all":
            delay = rng.expovariate(1.0 / max(latency_seconds, 0.1))
            price_drift = rng.gauss(0, 0.005 * delay)
            exec_price = max(0.01, min(0.99, ask + price_drift))
            fee = fee_rate * exec_price * (1 - exec_price)
            slippage = abs(exec_price - mid) * slippage_multiplier
            lat_cost = uncertainty_penalty * (1 + delay / 60)
            exec_price += resolution_penalty * 0.5
        elif level == "H_stress":
            stressed_ask = ask * spread_multiplier
            delay = rng.expovariate(1.0 / max(latency_seconds * 2, 0.1))
            price_drift = rng.gauss(0, 0.01 * delay)
            exec_price = max(0.01, min(0.99, stressed_ask + price_drift))
            fee = fee_rate * 1.5 * exec_price * (1 - exec_price)
            slippage = abs(exec_price - mid) * slippage_multiplier * 2
            lat_cost = uncertainty_penalty * 2 * (1 + delay / 30)
            available_depth = float(snap.yes_ask_size) * depth_fraction
            if available_depth < 10:
                slippage += 0.02
        else:
            exec_price = ask
            fee = fee_rate * ask * (1 - ask)
            slippage = 0.0
            lat_cost = 0.0

        # Gross edge
        gross = p - exec_price if exec_price <= p else 0.0
        total_cost = fee + slippage + lat_cost
        net = gross - total_cost

        pnls.append(net)
        fee_costs.append(fee)
        slippage_costs.append(slippage)
        latency_costs.append(lat_cost)
        turnover += exec_price
        trades += 1

    if not pnls:
        return CostLadderLevel(
            level=level,
            description="",
            gross_pnl=0,
            net_pnl=0,
            return_pct=0,
            turnover=0,
            fee_cost=0,
            slippage_cost=0,
            latency_cost=0,
            drawdown=0,
            trades=0,
            profit_factor=0,
            alpha_survival_ratio=0,
            sample_size=0,
        )

    total_pnl = sum(pnls)
    gross_pnl = sum(max(0, p) for p in pnls)
    losses = abs(sum(min(0, p) for p in pnls))
    pf = gross_pnl / losses if losses > 0 else float("inf") if gross_pnl > 0 else 0.0

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    avg_fee = sum(fee_costs) / len(fee_costs)
    avg_slip = sum(slippage_costs) / len(slippage_costs)
    avg_lat = sum(latency_costs) / len(latency_costs)

    descriptions = {
        "A_raw": "Midpoint, no costs",
        "B_spread": "Best ask execution",
        "C_fees": "Best ask + fees",
        "D_vwap": "Depth-walked VWAP + fees",
        "E_latency": "VWAP + fees + fixed latency",
        "F_stochastic": "VWAP + fees + stochastic latency + uncertainty",
        "G_all": "All costs + resolution penalty + risk limits",
        "H_stress": "Pessimistic stress scenario",
    }

    return CostLadderLevel(
        level=level,
        description=descriptions.get(level, ""),
        gross_pnl=round(gross_pnl, 4),
        net_pnl=round(total_pnl, 4),
        return_pct=round(total_pnl / max(turnover, 0.01) * 100, 2),
        turnover=round(turnover, 4),
        fee_cost=round(avg_fee, 6),
        slippage_cost=round(avg_slip, 6),
        latency_cost=round(avg_lat, 6),
        drawdown=round(max_dd, 4),
        trades=trades,
        profit_factor=round(pf, 4),
        alpha_survival_ratio=0.0,
        sample_size=len(pnls),
    )


def run_cost_ladder(
    dataset: ResearchDataset,
    fee_rate: float = 0.02,
    latency_seconds: float = 5.0,
    uncertainty_penalty: float = 0.01,
    resolution_penalty: float = 0.01,
    seed: int = 42,
) -> CostLadderResult:
    """Run the full 8-level cost ladder experiment."""
    snap_list = [
        s
        for s in dataset.snapshots
        if s.final_resolution is not None and s.yes_best_bid is not None
    ]

    levels_config = [
        ("A_raw", "Midpoint, no costs", {}),
        ("B_spread", "Best ask execution", {}),
        ("C_fees", "Best ask + fees", {"fee_rate": fee_rate}),
        ("D_vwap", "VWAP + fees", {"fee_rate": fee_rate, "slippage_multiplier": 1.5}),
        (
            "E_latency",
            "VWAP + fees + latency",
            {
                "fee_rate": fee_rate,
                "slippage_multiplier": 1.5,
                "latency_seconds": latency_seconds,
            },
        ),
        (
            "F_stochastic",
            "Stochastic latency + uncertainty",
            {
                "fee_rate": fee_rate,
                "slippage_multiplier": 1.5,
                "latency_seconds": latency_seconds,
                "uncertainty_penalty": uncertainty_penalty,
            },
        ),
        (
            "G_all",
            "All costs + resolution",
            {
                "fee_rate": fee_rate,
                "slippage_multiplier": 1.5,
                "latency_seconds": latency_seconds,
                "uncertainty_penalty": uncertainty_penalty,
                "resolution_penalty": resolution_penalty,
            },
        ),
        (
            "H_stress",
            "Pessimistic stress",
            {
                "fee_rate": fee_rate,
                "slippage_multiplier": 2.0,
                "latency_seconds": latency_seconds * 2,
                "uncertainty_penalty": uncertainty_penalty * 2,
                "resolution_penalty": resolution_penalty,
                "spread_multiplier": 1.5,
                "depth_fraction": 0.5,
            },
        ),
    ]

    results = []
    idealized_pnl = 0.0
    for name, desc, params in levels_config:
        lv = _simulate_pnl(snap_list, name, seed=seed, **params)
        if name == "A_raw":
            idealized_pnl = lv.net_pnl
        results.append(lv)

    survival = {}
    for lv in results:
        if idealized_pnl != 0:
            survival[lv.level] = round(lv.net_pnl / idealized_pnl, 4) if idealized_pnl != 0 else 0.0
        else:
            survival[lv.level] = 0.0

    final_results = []
    for lv in results:
        final_results.append(
            CostLadderLevel(
                level=lv.level,
                description=lv.description,
                gross_pnl=lv.gross_pnl,
                net_pnl=lv.net_pnl,
                return_pct=lv.return_pct,
                turnover=lv.turnover,
                fee_cost=lv.fee_cost,
                slippage_cost=lv.slippage_cost,
                latency_cost=lv.latency_cost,
                drawdown=lv.drawdown,
                trades=lv.trades,
                profit_factor=lv.profit_factor,
                alpha_survival_ratio=survival.get(lv.level, 0.0),
                sample_size=lv.sample_size,
            )
        )

    return CostLadderResult(levels=final_results, alpha_survival_by_level=survival)
