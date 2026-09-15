"""Liquidity and spread regime analysis for execution quality assessment.

Parts 29-30: Buckets markets by liquidity and spread regimes to test the
hypothesis that low-liquidity markets appear inefficient but execution
destroys alpha through spreads and slippage.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TypeAlias

D = Decimal

RegimeType: TypeAlias = str


@dataclass(frozen=True)
class RegimeBucket:
    """Metrics for a single liquidity or spread regime bucket."""

    regime: RegimeType
    lower_bound: float
    upper_bound: float  # inf for open-ended
    count: int
    model_brier: float
    market_brier: float
    delta_brier: float  # market_brier - model_brier (positive = model beats market)
    avg_spread: float
    avg_depth: float
    net_edge: float  # average model edge over market
    realized_pnl: float  # estimated P&L after execution costs
    is_efficient: bool  # True if market is well-priced (small delta)


@dataclass(frozen=True)
class RegimeReport:
    """Complete regime analysis report."""

    regime_type: str  # "liquidity" or "spread"
    buckets: list[RegimeBucket]
    hypothesis_supported: bool  # low-liquidity appears inefficient but alpha destroyed
    hypothesis_evidence: str
    summary: dict[str, float]

    def summary_dict(self) -> dict:
        return {
            "regime_type": self.regime_type,
            "bucket_count": len(self.buckets),
            "hypothesis_supported": self.hypothesis_supported,
            "hypothesis_evidence": self.hypothesis_evidence,
            **self.summary,
        }


@dataclass(frozen=True)
class RegimeSnapshot:
    """Minimal snapshot for regime analysis."""

    market_id: str
    yes_mid: float | None = None
    yes_spread: float | None = None
    yes_depth_5: float = 0.0
    yes_depth_10: float = 0.0
    volume: float = 0.0
    liquidity: float = 0.0
    model_probability: float | None = None
    final_resolution: int | None = None
    net_edge: float | None = None


# ── Liquidity Buckets ───────────────────────────────────────────────────────

_LIQUIDITY_BUCKETS: list[tuple[str, float, float]] = [
    ("<$1k", 0, 1_000),
    ("$1k-5k", 1_000, 5_000),
    ("$5k-25k", 5_000, 25_000),
    ("$25k-100k", 25_000, 100_000),
    (">$100k", 100_000, float("inf")),
]

# ── Spread Buckets ──────────────────────────────────────────────────────────

_SPREAD_BUCKETS: list[tuple[str, float, float]] = [
    ("<1c", 0, 0.01),
    ("1-2c", 0.01, 0.02),
    ("2-5c", 0.02, 0.05),
    ("5-10c", 0.05, 0.10),
    (">10c", 0.10, float("inf")),
]


def _brier_score(probabilities: list[float], outcomes: list[int]) -> float:
    if not probabilities:
        return 0.0
    return sum((p - o) ** 2 for p, o in zip(probabilities, outcomes)) / len(probabilities)


def _bucketize(
    snapshots: list[RegimeSnapshot],
    buckets: list[tuple[str, float, float]],
    key_fn,
) -> dict[str, list[RegimeSnapshot]]:
    """Assign snapshots to buckets based on a key function."""
    result: dict[str, list[RegimeSnapshot]] = {name: [] for name, _, _ in buckets}
    for snap in snapshots:
        value = key_fn(snap)
        if value is None:
            continue
        for name, lower, upper in buckets:
            if lower <= value < upper:
                result[name].append(snap)
                break
    return result


def _compute_bucket_metrics(
    snapshots: list[RegimeSnapshot],
    regime: RegimeType,
    lower: float,
    upper: float,
) -> RegimeBucket:
    """Compute metrics for a single bucket."""
    if not snapshots:
        return RegimeBucket(
            regime=regime,
            lower_bound=lower,
            upper_bound=upper,
            count=0,
            model_brier=0.0,
            market_brier=0.0,
            delta_brier=0.0,
            avg_spread=0.0,
            avg_depth=0.0,
            net_edge=0.0,
            realized_pnl=0.0,
            is_efficient=True,
        )

    model_probs = []
    market_probs = []
    outcomes = []
    spreads = []
    depths = []
    edges = []
    pnls = []

    for snap in snapshots:
        if snap.model_probability is not None and snap.final_resolution is not None:
            model_probs.append(snap.model_probability)
            outcomes.append(snap.final_resolution)
        if snap.yes_mid is not None and snap.final_resolution is not None:
            market_probs.append(snap.yes_mid)
        if snap.yes_spread is not None:
            spreads.append(snap.yes_spread)
        depths.append(max(snap.yes_depth_5, snap.yes_depth_10))
        if snap.net_edge is not None:
            edges.append(snap.net_edge)

        # Estimate realized PnL: edge * position_size - spread_cost
        if (
            snap.model_probability is not None
            and snap.final_resolution is not None
            and snap.yes_spread is not None
        ):
            pred = snap.model_probability
            mkt = snap.yes_mid if snap.yes_mid is not None else 0.5
            edge = pred - mkt
            spread_cost = snap.yes_spread / 2  # half-spread per side
            # Simplified: assume unit position, only profitable if edge > spread
            pnl = edge - spread_cost if edge > 0 else 0.0
            pnls.append(pnl)

    model_brier = _brier_score(model_probs, outcomes) if model_probs else 0.0
    market_brier = _brier_score(market_probs, outcomes) if market_probs else 0.0
    avg_spread = sum(spreads) / len(spreads) if spreads else 0.0
    avg_depth = sum(depths) / len(depths) if depths else 0.0
    avg_edge = sum(edges) / len(edges) if edges else 0.0
    avg_pnl = sum(pnls) / len(pnls) if pnls else 0.0

    # Market is "efficient" if delta_brier is small (< 0.01)
    delta = market_brier - model_brier
    is_efficient = abs(delta) < 0.01

    return RegimeBucket(
        regime=regime,
        lower_bound=lower,
        upper_bound=upper,
        count=len(snapshots),
        model_brier=model_brier,
        market_brier=market_brier,
        delta_brier=delta,
        avg_spread=avg_spread,
        avg_depth=avg_depth,
        net_edge=avg_edge,
        realized_pnl=avg_pnl,
        is_efficient=is_efficient,
    )


def analyze_liquidity_regimes(
    snapshots: list[RegimeSnapshot],
) -> RegimeReport:
    """Analyze model performance across liquidity regimes.

    Tests hypothesis: low-liquidity markets appear inefficient (large delta_brier)
    but execution destroys alpha (low realized_pnl due to wide spreads).
    """
    bucketed = _bucketize(snapshots, _LIQUIDITY_BUCKETS, lambda s: s.liquidity)
    buckets: list[RegimeBucket] = []
    for name, lower, upper in _LIQUIDITY_BUCKETS:
        bucket_snaps = bucketed.get(name, [])
        buckets.append(_compute_bucket_metrics(bucket_snaps, name, lower, upper))

    # Test hypothesis: look at lowest vs highest liquidity buckets
    low_liq = next((b for b in buckets if b.regime == "<$1k"), None)
    high_liq = next((b for b in buckets if b.regime == ">$100k"), None)

    hypothesis_supported = False
    evidence = ""

    if low_liq and high_liq and low_liq.count > 10 and high_liq.count > 10:
        low_inefficient = abs(low_liq.delta_brier) > abs(high_liq.delta_brier) * 1.5
        low_pnl_destroyed = low_liq.realized_pnl < high_liq.realized_pnl
        hypothesis_supported = low_inefficient and low_pnl_destroyed
        evidence = (
            f"Low-liquidity delta_brier={low_liq.delta_brier:.4f} vs "
            f"high-liquidity={high_liq.delta_brier:.4f}; "
            f"Low-liquidity realized_pnl={low_liq.realized_pnl:.4f} vs "
            f"high-liquidity={high_liq.realized_pnl:.4f}"
        )
    else:
        evidence = "Insufficient data in extreme liquidity buckets"

    summary = {
        "total_snapshots": sum(b.count for b in buckets),
        "low_liquidity_count": low_liq.count if low_liq else 0,
        "high_liquidity_count": high_liq.count if high_liq else 0,
    }

    return RegimeReport(
        regime_type="liquidity",
        buckets=buckets,
        hypothesis_supported=hypothesis_supported,
        hypothesis_evidence=evidence,
        summary=summary,
    )


def analyze_spread_regimes(
    snapshots: list[RegimeSnapshot],
) -> RegimeReport:
    """Analyze model performance across spread regimes.

    Tests hypothesis: wide-spread markets appear inefficient but the spread
    itself consumes any potential alpha.
    """
    bucketed = _bucketize(snapshots, _SPREAD_BUCKETS, lambda s: s.yes_spread)
    buckets: list[RegimeBucket] = []
    for name, lower, upper in _SPREAD_BUCKETS:
        bucket_snaps = bucketed.get(name, [])
        buckets.append(_compute_bucket_metrics(bucket_snaps, name, lower, upper))

    narrow = next((b for b in buckets if b.regime == "<1c"), None)
    wide = next((b for b in buckets if b.regime == ">10c"), None)

    hypothesis_supported = False
    evidence = ""

    if narrow and wide and narrow.count > 10 and wide.count > 10:
        wide_inefficient = abs(wide.delta_brier) > abs(narrow.delta_brier) * 1.5
        wide_pnl_destroyed = wide.realized_pnl < narrow.realized_pnl
        hypothesis_supported = wide_inefficient and wide_pnl_destroyed
        evidence = (
            f"Wide-spread delta_brier={wide.delta_brier:.4f} vs "
            f"narrow={narrow.delta_brier:.4f}; "
            f"Wide-spread realized_pnl={wide.realized_pnl:.4f} vs "
            f"narrow={narrow.realized_pnl:.4f}"
        )
    else:
        evidence = "Insufficient data in extreme spread buckets"

    summary = {
        "total_snapshots": sum(b.count for b in buckets),
        "narrow_spread_count": narrow.count if narrow else 0,
        "wide_spread_count": wide.count if wide else 0,
    }

    return RegimeReport(
        regime_type="spread",
        buckets=buckets,
        hypothesis_supported=hypothesis_supported,
        hypothesis_evidence=evidence,
        summary=summary,
    )
