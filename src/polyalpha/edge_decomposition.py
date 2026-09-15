"""Edge decomposition — ex-ante and realized tracking per Part 18 spec."""

from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .research_dataset import MarketSnapshot

D = Decimal
_Q = "0.000001"
COST_FIELDS = (
    "spread_cost",
    "depth_slippage",
    "fee_cost",
    "latency_cost",
    "uncertainty_penalty",
    "resolution_penalty",
    "liquidity_penalty",
    "stale_data_penalty",
)


def _q(v: Decimal) -> Decimal:
    return v.quantize(D(_Q))


def _safe_ratio(num: Decimal, den: Decimal) -> Decimal | None:
    if den == 0:
        return None
    try:
        return _q(num / den)
    except InvalidOperation:
        return None


def _bucket_time(h: float | None) -> str:
    if h is None:
        return "unknown"
    return "<1d" if h < 24 else "1-3d" if h < 72 else "3-7d" if h < 168 else ">7d"


def _bucket_liq(liq: Decimal) -> str:
    return "thin" if liq < D("1000") else "medium" if liq < D("10000") else "deep"


def _bucket_conf(c: float) -> str:
    return "low" if c < 0.3 else "medium" if c < 0.7 else "high"


@dataclass
class EdgeRecord:
    signal_id: str
    market_id: str
    strategy: str = ""
    category: str = ""
    confidence: float = 0.0
    disagreement_bucket: str = ""
    liquidity_bucket: str = ""
    time_to_resolution_bucket: str = ""
    model_version: str = ""
    raw_model_edge: Decimal = D(0)
    spread_cost: Decimal = D(0)
    depth_slippage: Decimal = D(0)
    fee_cost: Decimal = D(0)
    latency_cost: Decimal = D(0)
    uncertainty_penalty: Decimal = D(0)
    resolution_penalty: Decimal = D(0)
    liquidity_penalty: Decimal = D(0)
    stale_data_penalty: Decimal = D(0)
    predicted_net_edge: Decimal = D(0)
    realized_edge: Decimal | None = None
    edge_realization_ratio: Decimal | None = None
    outcome: int | None = None

    @property
    def total_costs(self) -> Decimal:
        return sum((getattr(self, f) for f in COST_FIELDS), D(0))

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "signal_id": self.signal_id,
            "market_id": self.market_id,
            "strategy": self.strategy,
            "category": self.category,
            "confidence": self.confidence,
            "disagreement_bucket": self.disagreement_bucket,
            "liquidity_bucket": self.liquidity_bucket,
            "time_to_resolution_bucket": self.time_to_resolution_bucket,
            "model_version": self.model_version,
            "raw_model_edge": str(self.raw_model_edge),
            "predicted_net_edge": str(self.predicted_net_edge),
            "realized_edge": str(self.realized_edge) if self.realized_edge is not None else None,
            "edge_realization_ratio": str(self.edge_realization_ratio)
            if self.edge_realization_ratio is not None
            else None,
            "outcome": self.outcome,
        }
        for f in COST_FIELDS:
            d[f] = str(getattr(self, f))
        return d


@dataclass
class AggregateResult:
    group_key: dict[str, Any]
    count: int
    sum_predicted: Decimal
    sum_realized: Decimal
    mean_realization_ratio: Decimal | None
    cost_breakdown: dict[str, Decimal]


def track_edge(
    signal_id: str,
    market: MarketSnapshot,
    raw_model_edge: Decimal,
    spread_cost: Decimal = D(0),
    depth_slippage: Decimal = D(0),
    fee_cost: Decimal = D(0),
    latency_cost: Decimal = D(0),
    uncertainty_penalty: Decimal = D(0),
    resolution_penalty: Decimal = D(0),
    liquidity_penalty: Decimal = D(0),
    stale_data_penalty: Decimal = D(0),
    strategy: str = "",
    model_version: str = "",
) -> EdgeRecord:
    costs = (
        spread_cost
        + depth_slippage
        + fee_cost
        + latency_cost
        + uncertainty_penalty
        + resolution_penalty
        + liquidity_penalty
        + stale_data_penalty
    )
    predicted = _q(raw_model_edge - costs)
    conf = float(market.ambiguity_score) if market.ambiguity_score else 0.0
    return EdgeRecord(
        signal_id=signal_id,
        market_id=market.market_id,
        strategy=strategy,
        category=market.category,
        confidence=conf,
        disagreement_bucket=_bucket_conf(conf),
        liquidity_bucket=_bucket_liq(market.liquidity),
        time_to_resolution_bucket=_bucket_time(market.hours_to_resolution),
        model_version=model_version,
        raw_model_edge=_q(raw_model_edge),
        spread_cost=_q(spread_cost),
        depth_slippage=_q(depth_slippage),
        fee_cost=_q(fee_cost),
        latency_cost=_q(latency_cost),
        uncertainty_penalty=_q(uncertainty_penalty),
        resolution_penalty=_q(resolution_penalty),
        liquidity_penalty=_q(liquidity_penalty),
        stale_data_penalty=_q(stale_data_penalty),
        predicted_net_edge=predicted,
    )


def realize_edge(record: EdgeRecord, outcome: int, exec_price: Decimal | None = None) -> EdgeRecord:
    record.outcome = outcome
    record.realized_edge = _q(D(outcome) - (exec_price if exec_price is not None else D(0)))
    record.edge_realization_ratio = _safe_ratio(record.realized_edge, record.predicted_net_edge)
    return record


def aggregate_by(records: list[EdgeRecord], group_fields: list[str]) -> list[AggregateResult]:
    groups: dict[tuple, list[EdgeRecord]] = defaultdict(list)
    for r in records:
        groups[tuple(getattr(r, f) for f in group_fields)].append(r)
    results = []
    for key, recs in sorted(groups.items()):
        sum_pred = sum((r.predicted_net_edge for r in recs), D(0))
        realized = [r.realized_edge for r in recs if r.realized_edge is not None]
        sum_rea = sum(realized, D(0)) if realized else D(0)
        ratios = [r.edge_realization_ratio for r in recs if r.edge_realization_ratio is not None]
        costs = {f: sum((getattr(r, f) for r in recs), D(0)) for f in COST_FIELDS}
        results.append(
            AggregateResult(
                group_key=dict(zip(group_fields, key)),
                count=len(recs),
                sum_predicted=sum_pred,
                sum_realized=sum_rea,
                mean_realization_ratio=_q(sum(ratios) / D(len(ratios))) if ratios else None,
                cost_breakdown=costs,
            )
        )
    return results
