"""Order-flow signals derived from FillRecords (families A6/A8).

Signed flow is computed from the venue's explicit taker intent (never inferred
from the book feed). Pure functions, offline-testable.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable

from .fills import FillRecord

D = Decimal


def signed_flow_series(
    records: list[FillRecord], window_seconds: int = 60
) -> list[tuple[datetime, Decimal]]:
    """Time-bucketed signed YES flow (order-flow imbalance).

    OFI_t = sum_j s_j q_j, where s_j = +1 for taker-buy-YES and -1 for
    taker-sell-YES. Buckets are aligned to the first record's timestamp.
    """
    if not records:
        return []
    ordered = sorted(records, key=lambda r: r.trade_time)
    start = ordered[0].trade_time
    buckets: dict[int, Decimal] = defaultdict(lambda: D(0))
    for record in ordered:
        idx = int((record.trade_time - start).total_seconds() // window_seconds)
        buckets[idx] += record.signed_yes_flow
    return [
        (start + timedelta(seconds=idx * window_seconds), buckets[idx])
        for idx in sorted(buckets)
    ]


def mean_markout(
    records: list[FillRecord],
    price_fn: Callable[[datetime], Decimal | None],
    horizon_seconds: int = 60,
) -> Decimal:
    """Mean aggressor markout: E[ sign * (p_{t+h} - p_t) ].

    ``price_fn(timestamp) -> mid``. A negative mean is adverse selection (the
    aggressor pays a price that moves against them over the horizon); positive
    is favorable. Returns Decimal('0') when no price is available.
    """
    if not records:
        return D(0)
    total = D(0)
    n = 0
    horizon = timedelta(seconds=horizon_seconds)
    for record in records:
        p0 = price_fn(record.trade_time)
        ph = price_fn(record.trade_time + horizon)
        if p0 is None or ph is None:
            continue
        sign = D(1) if record.aggressor_side == "BUY" else D(-1)
        total += sign * (ph - p0)
        n += 1
    return total / D(n) if n else D(0)


def kyle_lambda_ols(flow: list[Decimal], dp: list[Decimal]) -> float:
    """OLS estimate of Kyle's lambda: dp = lambda * flow + eps.

    ``flow`` and ``dp`` must be aligned (same time buckets). Returns 0.0 when
    the flow is degenerate (no variation).
    """
    if len(flow) != len(dp):
        raise ValueError("flow and dp must be aligned")
    if len(flow) < 2:
        return 0.0
    fx = [float(f) for f in flow]
    fy = [float(d) for d in dp]
    mx = sum(fx) / len(fx)
    my = sum(fy) / len(fy)
    num = sum((x - mx) * (y - my) for x, y in zip(fx, fy))
    den = sum((x - mx) ** 2 for x in fx)
    return num / den if den > 0 else 0.0


def side_agreement_rate(a: list[str], b: list[str]) -> float:
    """Fraction of aligned positions where two side sequences agree.

    The Dubach negative-control metric: compare feed-inferred direction against
    venue maker/taker ground truth (the paper reports ~59% agreement).
    """
    if len(a) != len(b):
        raise ValueError("sequences must be aligned")
    if not a:
        return 1.0
    return sum(1 for x, y in zip(a, b) if x == y) / len(a)
