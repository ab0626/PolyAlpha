"""Exposure caps operate on gross fee-inclusive capital at risk, without netting.

Supports correlation-adjusted exposure when correlation data is available.
"""

from dataclasses import dataclass
from decimal import Decimal

D = Decimal


@dataclass(frozen=True)
class Limits:
    normal: Decimal = D("0.005")
    market: Decimal = D("0.02")
    event: Decimal = D("0.05")
    cluster: Decimal = D("0.05")
    category: Decimal = D("0.10")
    total: Decimal = D("0.20")
    daily_loss: Decimal = D("0.02")
    drawdown: Decimal = D("0.08")

    def __post_init__(self):
        for value in vars(self).values():
            if not value.is_finite() or not 0 < value <= 1:
                raise ValueError("invalid risk fraction")


class Risk:
    def __init__(self, initial_equity, limits=Limits()):
        self.limits = limits
        self.peak = self.day_start = initial_equity
        self.day = None
        self.halted = False
        self.reason = None
        self.correlation_registry = None  # Set externally for correlation-adjusted exposure

    def observe(self, equity, at):
        if self.day != at.date():
            self.day, self.day_start = at.date(), equity
        self.peak = max(self.peak, equity)
        if equity <= 0 or equity <= self.peak * (1 - self.limits.drawdown):
            self.halted, self.reason = True, "drawdown"
        elif equity <= self.day_start * (1 - self.limits.daily_loss):
            self.halted, self.reason = True, "daily_loss"

    def budget(self, portfolio, equity, market, event, cluster, category):
        if self.halted or not event or not cluster or equity <= 0:
            return D(0)
        capacities = [
            portfolio.cash,
            equity * self.limits.normal,
            equity * self.limits.total - sum(portfolio.exposures().values()),
        ]
        for dimension, identity, fraction in (
            ("market_id", market, self.limits.market),
            ("event_id", event, self.limits.event),
            ("cluster", cluster, self.limits.cluster),
            ("category", category, self.limits.category),
        ):
            capacities.append(
                equity * fraction - portfolio.exposures(dimension).get(identity, D(0))
            )
        return max(D(0), min(capacities))

    def correlation_adjusted_budget(self, portfolio, equity, market, event, cluster, category):
        """Budget with correlation-adjusted cluster exposure.

        If correlation data is available, adjusts cluster capacity
        based on average internal correlation.
        """
        base_budget = self.budget(portfolio, equity, market, event, cluster, category)
        if self.correlation_registry is None or base_budget <= 0:
            return base_budget

        # Get cluster exposure with correlation adjustment
        positions = {
            p.token_id: p.shares * (p.basis / p.shares if p.shares > 0 else D(0))
            for p in portfolio.positions.values()
            if p.shares > 0
        }
        adjusted = self.correlation_registry.correlation_adjusted_exposure(positions)
        cluster_raw = sum(v for v in adjusted.values())
        cluster_cap = equity * self.limits.cluster

        if cluster_raw >= cluster_cap:
            return D(0)

        # Reduce budget if correlation-adjusted exposure is high
        utilization = cluster_raw / cluster_cap if cluster_cap > 0 else D(0)
        correlation_factor = D(1) - utilization
        return max(D(0), (base_budget * correlation_factor).quantize(D("0.01")))
