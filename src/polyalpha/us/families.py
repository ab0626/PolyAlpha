"""Pre-registered forward-evidence family evaluations.

Implements the decision rules of ``docs/RESEARCH_AGENDA.md`` for the Polymarket
US forward sample. Every inference is cluster-level: the unit of analysis is
the parent-event cluster (``event_cluster``), never raw rows, so correlated
child markets cannot inflate significance.

Global decision rules (frozen ex ante):
  - temporal split: evaluate on markets with ``endDate >= SPLIT_DATE``
  - multiple testing: Benjamini-Hochberg FDR at 10% across a family's tests
  - significance: FDR-adjusted p < 0.10 AND the family's minimum effect size
  - minimum effective N: >= 200 independent-ish event clusters

Families 5 (maker/taker) and 6 (informed-flow) are DEFERRED (require signed
trade / account flow). Families 4 (cross-market) and 7 (shock) are NOT_RUN
here: 4 needs both executable sides or defensible related-market linkage, and
7 needs the frozen v2.1 shock classifier — neither is satisfiable from the
current single-side public book lineage without risk of post-hoc fitting.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ..research_dataset import MarketSnapshot, ResearchDataset
from ..statistical_tests import benjamini_hochberg

FDR_LEVEL = 0.10
MIN_EFFECTIVE_N = 200
MIN_CATEGORY_CLUSTERS = 20
REAL_DATA_START = date(2026, 9, 16)
N_BOOTSTRAP = 2000
SEED = 42

VERDICT_SUPPORTED = "SUPPORTED"
VERDICT_NOT_SUPPORTED = "NOT_SUPPORTED"
VERDICT_INSUFFICIENT = "INSUFFICIENT_DATA"
VERDICT_DEFERRED = "DEFERRED"
VERDICT_NOT_RUN = "NOT_RUN"

# Minimum effects are the pre-registered per-family thresholds (frozen).
MIN_RESIDUAL = 0.01          # Families 1/2: >= 1pp mean residual
MIN_TTR_RESIDUAL = 0.01      # Family 1 refinement: >= 1pp |residual| spread
MIN_MICRO_DP = 0.005         # Family 3: >= 0.5 tick mean |dMid| difference


def default_split_date() -> date:
    """SPLIT_DATE = min(REAL_DATA_START + 45d, 2026-11-01) = 2026-10-31."""
    return min(REAL_DATA_START + timedelta(days=45), date(2026, 11, 1))


@dataclass
class FamilyTest:
    name: str
    p_value: float
    p_adjusted: float
    effect: float
    min_effect: float
    n_clusters: int
    passed: bool
    details: str = ""

    def summary(self) -> dict:
        return {
            "name": self.name,
            "p_value": round(self.p_value, 6),
            "p_adjusted": round(self.p_adjusted, 6),
            "effect": round(self.effect, 6),
            "min_effect": self.min_effect,
            "n_clusters": self.n_clusters,
            "passed": self.passed,
            "details": self.details,
        }


@dataclass
class FamilyDecision:
    family: str
    primary_metric: str
    n_eff: int
    verdict: str
    tests: list[FamilyTest] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "family": self.family,
            "primary_metric": self.primary_metric,
            "n_eff": self.n_eff,
            "verdict": self.verdict,
            "tests": [t.summary() for t in self.tests],
            "reasons": self.reasons,
        }


def _end_date(s: MarketSnapshot) -> datetime | None:
    if s.hours_to_resolution is None:
        return None
    return s.observation_timestamp + timedelta(hours=s.hours_to_resolution)


def _resolved_eval(dataset: ResearchDataset, split_date: date) -> list[MarketSnapshot]:
    """Resolved, eval-period snapshots whose prediction precedes the known outcome."""
    out: list[MarketSnapshot] = []
    for s in dataset.snapshots:
        if s.final_resolution is None or s.yes_mid is None:
            continue
        end = _end_date(s)
        if end is None or end.date() < split_date:
            continue
        if s.resolution_timestamp is not None and s.observation_timestamp >= s.resolution_timestamp:
            continue
        out.append(s)
    return out


def _cluster_means(snaps: list[MarketSnapshot], fn) -> dict[str, list[float]]:
    groups: dict[str, list[float]] = {}
    for s in snaps:
        groups.setdefault(s.event_cluster, []).append(fn(s))
    return groups


def _cluster_bootstrap_p(
    cluster_values: dict[str, list[float]],
    n_bootstrap: int = N_BOOTSTRAP,
    seed: int = SEED,
) -> tuple[float, float, int]:
    """Two-sided null-centered cluster bootstrap: (estimate, p, n_clusters).

    H0 is "cluster-mean == 0". Recentering each cluster mean by the observed
    grand mean makes H0 hold under resampling, yielding a valid p-value that
    respects within-cluster correlation.
    """
    means = [sum(v) / len(v) for v in cluster_values.values() if v]
    if not means:
        return 0.0, 1.0, 0
    observed = sum(means) / len(means)
    rng = random.Random(seed)
    centered = [m - observed for m in means]
    n = len(means)
    extreme = 0
    for _ in range(n_bootstrap):
        boot = sum(rng.choices(centered, k=n)) / n
        if abs(boot) >= abs(observed):
            extreme += 1
    p = (extreme + 1) / (n_bootstrap + 1)
    return observed, p, n


def _bh(p_values: list[float]) -> list[float]:
    if not p_values:
        return []
    return benjamini_hochberg(p_values).adjusted_p_values


def _residual(s: MarketSnapshot) -> float:
    return float(s.final_resolution) - float(s.yes_mid)


def _price_bucket(mid: float, n: int = 10) -> int:
    return min(int(mid * n), n - 1)


def _ttr_bucket(hours: float | None) -> str:
    if hours is None:
        return "unknown"
    if hours < 1:
        return "<1h"
    if hours < 24:
        return "1h-24h"
    if hours < 168:
        return "1d-7d"
    return ">7d"


# ── Family 1 — calibration residual (Manski / Wolfers-Zitzewitz) ────────────


def evaluate_family_1_calibration(
    dataset: ResearchDataset, split_date: date | None = None
) -> FamilyDecision:
    split_date = split_date or default_split_date()
    snaps = _resolved_eval(dataset, split_date)
    n_eff = len({s.event_cluster for s in snaps})
    d = FamilyDecision("F1_calibration_residual", "ECE", n_eff, VERDICT_INSUFFICIENT)
    if n_eff < MIN_EFFECTIVE_N:
        d.reasons.append(f"effective N {n_eff} < {MIN_EFFECTIVE_N}")
        return d

    buckets: dict[int, list[MarketSnapshot]] = {}
    for s in snaps:
        buckets.setdefault(_price_bucket(float(s.yes_mid)), []).append(s)

    tests: list[FamilyTest] = []
    pvals: list[float] = []
    for idx in sorted(buckets):
        snap = buckets[idx]
        est, p, ncl = _cluster_bootstrap_p(_cluster_means(snap, _residual))
        lo, hi = idx / 10.0, (idx + 1) / 10.0
        pvals.append(p)
        tests.append(
            FamilyTest(
                name=f"price[{lo:.1f},{hi:.1f})",
                p_value=p,
                p_adjusted=1.0,
                effect=est,
                min_effect=MIN_RESIDUAL,
                n_clusters=ncl,
                passed=False,
            )
        )

    adj = _bh(pvals)
    for t, pa in zip(tests, adj):
        t.p_adjusted = pa
        t.passed = pa < FDR_LEVEL and abs(t.effect) >= MIN_RESIDUAL

    residuals = [_residual(s) for s in snaps]
    ece = sum(abs(r) for r in residuals) / len(residuals) if residuals else 0.0

    significant = [t for t in tests if t.passed]
    if significant:
        d.verdict = VERDICT_SUPPORTED
        d.reasons.append(
            f"non-flat residual surface: {len(significant)} bucket(s) FDR-significant "
            f"with |residual| >= 1pp"
        )
    else:
        d.verdict = VERDICT_NOT_SUPPORTED
        d.reasons.append("residual surface indistinguishable from flat at FDR 10%")
    d.tests = tests
    d.primary_metric = f"ECE={ece:.4f}"
    return d


# ── Family 2 — favorite–longshot bias (Cardozo et al.) ──────────────────────


def evaluate_family_2_longshot(
    dataset: ResearchDataset, split_date: date | None = None
) -> FamilyDecision:
    split_date = split_date or default_split_date()
    snaps = _resolved_eval(dataset, split_date)
    n_eff = len({s.event_cluster for s in snaps})
    d = FamilyDecision("F2_favorite_longshot", "mean_residual_by_price", n_eff, VERDICT_INSUFFICIENT)
    if n_eff < MIN_EFFECTIVE_N:
        d.reasons.append(f"effective N {n_eff} < {MIN_EFFECTIVE_N}")
        return d

    buckets: dict[int, list[MarketSnapshot]] = {}
    for s in snaps:
        buckets.setdefault(_price_bucket(float(s.yes_mid)), []).append(s)

    tests: list[FamilyTest] = []
    pvals: list[float] = []
    for idx in sorted(buckets):
        est, p, ncl = _cluster_bootstrap_p(_cluster_means(buckets[idx], _residual))
        lo, hi = idx / 10.0, (idx + 1) / 10.0
        pvals.append(p)
        tests.append(
            FamilyTest(
                name=f"price[{lo:.1f},{hi:.1f})",
                p_value=p,
                p_adjusted=1.0,
                effect=est,
                min_effect=MIN_RESIDUAL,
                n_clusters=ncl,
                passed=False,
            )
        )

    adj = _bh(pvals)
    for t, pa in zip(tests, adj):
        t.p_adjusted = pa
        t.passed = pa < FDR_LEVEL and abs(t.effect) >= MIN_RESIDUAL

    # Longshot bias specifically: low-price buckets realize below quoted.
    low_buckets = [t for t in tests if t.effect < 0 and "price[0." in t.name]
    significant = [t for t in tests if t.passed]
    if significant:
        d.verdict = VERDICT_SUPPORTED
    else:
        d.verdict = VERDICT_NOT_SUPPORTED
        d.reasons.append("no price bucket shows FDR-significant realized-vs-quoted residual")
    if low_buckets and not any(t.passed for t in low_buckets):
        d.reasons.append("no significant longshot (low-price) underperformance")
    d.tests = tests
    return d


# ── Family 1 refinement — time-to-resolution miscalibration (Page & Clemen) ─


def evaluate_family_1_ttr(
    dataset: ResearchDataset, split_date: date | None = None
) -> FamilyDecision:
    split_date = split_date or default_split_date()
    snaps = _resolved_eval(dataset, split_date)
    n_eff = len({s.event_cluster for s in snaps})
    d = FamilyDecision("F1_TTR_miscalibration", "|residual|_by_TTR", n_eff, VERDICT_INSUFFICIENT)
    if n_eff < MIN_EFFECTIVE_N:
        d.reasons.append(f"effective N {n_eff} < {MIN_EFFECTIVE_N}")
        return d

    order = ["<1h", "1h-24h", "1d-7d", ">7d"]
    buckets: dict[str, list[MarketSnapshot]] = {k: [] for k in order}
    for s in snaps:
        b = _ttr_bucket(s.hours_to_resolution)
        if b in buckets:
            buckets[b].append(s)

    mag: dict[str, tuple[float, float, int]] = {}
    pvals: list[float] = []
    tests: list[FamilyTest] = []
    for b in order:
        if not buckets[b]:
            continue
        est, p, ncl = _cluster_bootstrap_p(
            _cluster_means(buckets[b], lambda s: abs(_residual(s)))
        )
        mag[b] = (est, p, ncl)
        pvals.append(p)
        tests.append(
            FamilyTest(
                name=f"TTR_{b}",
                p_value=p,
                p_adjusted=1.0,
                effect=est,
                min_effect=MIN_TTR_RESIDUAL,
                n_clusters=ncl,
                passed=False,
            )
        )

    adj = _bh(pvals)
    for t, pa in zip(tests, adj):
        t.p_adjusted = pa
        t.passed = pa < FDR_LEVEL and abs(t.effect) >= MIN_TTR_RESIDUAL

    present = [b for b in order if b in mag]
    nearest, farthest = present[0], present[-1]
    spread = abs(mag[farthest][0] - mag[nearest][0])

    if mag and spread >= MIN_TTR_RESIDUAL and all(t.passed for t in tests if t.name in ("TTR_<1h", "TTR_>7d")):
        d.verdict = VERDICT_SUPPORTED
        d.reasons.append(f"|residual| grows with TTR: spread {spread:.4f} >= 1pp")
    else:
        d.verdict = VERDICT_NOT_SUPPORTED
        d.reasons.append(f"no TTR-monotone miscalibration (spread {spread:.4f})")
    d.tests = tests
    return d


# ── Family 3 — microstructure / price-impact proxy (Glosten-Milgrom, Kyle) ──


def evaluate_family_3_microstructure(
    dataset: ResearchDataset, split_date: date | None = None
) -> FamilyDecision:
    split_date = split_date or default_split_date()
    # Microstructure uses book-state -> subsequent repricing; it does not
    # require resolution, only a market's time series in the eval period.
    series: dict[str, list[MarketSnapshot]] = {}
    for s in dataset.snapshots:
        if s.yes_mid is None or s.yes_spread is None:
            continue
        end = _end_date(s)
        if end is None or end.date() < split_date:
            continue
        series.setdefault(s.market_id, []).append(s)

    # Build (state, dMid) pairs from consecutive snapshots within a market.
    tight: dict[str, list[float]] = {}
    wide: dict[str, list[float]] = {}
    for snap_list in series.values():
        snap_list.sort(key=lambda s: s.observation_timestamp)
        for a, b in zip(snap_list, snap_list[1:]):
            dmid = float(b.yes_mid) - float(a.yes_mid)
            cluster = a.event_cluster
            if float(a.yes_spread) <= 0.02:
                tight.setdefault(cluster, []).append(abs(dmid))
            else:
                wide.setdefault(cluster, []).append(abs(dmid))

    n_eff = len({s.event_cluster for m in series.values() for s in m})
    d = FamilyDecision("F3_microstructure", "mean|dMid|", n_eff, VERDICT_INSUFFICIENT)
    if n_eff < MIN_EFFECTIVE_N or not tight or not wide:
        d.reasons.append(
            f"insufficient microstructure series (n_eff={n_eff}, tight={len(tight)}, wide={len(wide)})"
        )
        return d

    tight_est, tight_p, _ = _cluster_bootstrap_p(tight)
    wide_est, wide_p, _ = _cluster_bootstrap_p(wide)
    diff = wide_est - tight_est
    # One-sided-ish: wide/shallow books should repricing more (diff > 0).
    pvals = [tight_p, wide_p]
    adj = _bh(pvals)
    tests = [
        FamilyTest("tight_books|dMid|", tight_p, adj[0], tight_est, MIN_MICRO_DP, len(tight), False),
        FamilyTest("wide_books|dMid|", wide_p, adj[1], wide_est, MIN_MICRO_DP, len(wide), False),
    ]
    if diff >= MIN_MICRO_DP and wide_p < FDR_LEVEL:
        d.verdict = VERDICT_SUPPORTED
        d.reasons.append(f"wide books repricing more: diff {diff:.5f} >= 0.5 tick")
    else:
        d.verdict = VERDICT_NOT_SUPPORTED
        d.reasons.append(f"no spread-conditional repricing (diff {diff:.5f})")
    d.tests = tests
    return d


# ── All families ─────────────────────────────────────────────────────────────


def evaluate_all_families(
    dataset: ResearchDataset, split_date: date | None = None
) -> list[FamilyDecision]:
    split_date = split_date or default_split_date()
    decisions = [
        evaluate_family_1_calibration(dataset, split_date),
        evaluate_family_2_longshot(dataset, split_date),
        evaluate_family_1_ttr(dataset, split_date),
        evaluate_family_3_microstructure(dataset, split_date),
        FamilyDecision(
            "F4_cross_market", "constraint_violations", 0, VERDICT_NOT_RUN,
            reasons=["requires both executable sides or defensible related-market linkage"],
        ),
        FamilyDecision(
            "F5_maker_taker", "maker_edge", 0, VERDICT_DEFERRED,
            reasons=["requires aggressor-signed trade flow (not in public REST)"],
        ),
        FamilyDecision(
            "F6_informed_flow", "flow_leadership", 0, VERDICT_DEFERRED,
            reasons=["requires participant/order-flow attribution (not available)"],
        ),
        FamilyDecision(
            "F7_shock_reversion", "continuation_vs_reversion", 0, VERDICT_NOT_RUN,
            reasons=["requires the frozen v2.1 shock classifier (not yet implemented)"],
        ),
    ]
    return decisions
