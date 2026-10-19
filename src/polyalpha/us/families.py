"""Pre-registered forward-evidence family evaluations (v2.3.1 clarified).

Implements the decision rules of ``docs/RESEARCH_AGENDA.md`` v2.0 + v2.1 + v2.2 +
v2.3 + v2.3.1, for the Polymarket US forward sample. Every inference is
cluster-level and follows the registered sampling hierarchy:

    raw snapshots -> (slug, cell) mean -> (parent cluster, cell) mean
                   -> cross-cluster statistic

Global decision rules (frozen):
  - temporal split: evaluate on markets with ``endDate >= SPLIT_DATE``
  - multiple testing: Benjamini-Hochberg FDR at 10%, family-wide (one BH
    universe per family across every cell/sub-group contributing to its verdict)
  - significance: FDR-adjusted p < 0.10 AND the family's minimum effect size
  - minimum effective N: >= 200 independent-ish event clusters
  - sparse-cell floor (v2.3): any cell must have >= 20 clusters to contribute

Families 5/6 DEFERRED; 4 and 7 NOT_RUN; 3 NOT_RUN / INSUFFICIENT_SPECIFICATION.
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
MIN_CELL_CLUSTERS = 20          # sparse-cell floor (v2.3 amendment)
REAL_DATA_START = date(2026, 9, 16)
N_BOOTSTRAP = 2000
SEED = 42

VERDICT_SUPPORTED = "SUPPORTED"
VERDICT_NOT_SUPPORTED = "NOT_SUPPORTED"
VERDICT_INSUFFICIENT = "INSUFFICIENT_DATA"
VERDICT_DEFERRED = "DEFERRED"
VERDICT_NOT_RUN = "NOT_RUN"

MIN_RESIDUAL = 0.01            # Families 1/2: >= 1pp mean residual
MIN_TTR_RESIDUAL = 0.01        # Family 1 refinement: >= 1pp |residual| spread

FLB_LOW_PRICE = 0.10           # v2.3.1 pooled low-price region


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
    """Cluster-level values under the registered sampling hierarchy.

    Collapses raw snapshots to (slug, cell) means first, then to (parent cluster,
    cell) means, so a market sampled many times contributes one market-level unit
    and an event with many child markets contributes one cluster-level unit.
    """
    by_market: dict[tuple[str, str], list[float]] = {}
    for s in snaps:
        by_market.setdefault((s.event_cluster, s.market_id), []).append(fn(s))
    groups: dict[str, list[float]] = {}
    for (cluster, _mid), vals in by_market.items():
        groups.setdefault(cluster, []).append(sum(vals) / len(vals))
    return groups


def _cluster_bootstrap_p(
    cluster_values: dict[str, list[float]],
    n_bootstrap: int = N_BOOTSTRAP,
    seed: int = SEED,
) -> tuple[float, float, int]:
    """Two-sided null-centered cluster bootstrap: (estimate, p, n_clusters)."""
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
    # [0,.1), [.1,.2), ..., [.9,1.0]; p=1.00 lands in the final bin.
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
    return ">=168h"


def _spread_regime(s: MarketSnapshot) -> str | None:
    if s.yes_spread is None:
        return None  # missing spread: no cell
    if float(s.yes_spread) <= 0.02:
        return "tight"
    return "wide"


def _normalize_category(s: MarketSnapshot) -> str:
    c = (s.category or "").strip().lower()
    return c or "unknown"


def _liquidity_bounds(dataset: ResearchDataset, split_date: date) -> tuple[float, float] | None:
    """Training-only openInterest tercile cutpoints (q1/3, q2/3).

    Computed once from pre-split market-level openInterest means. None when the
    two quantiles collapse (ties/zeros) -> liquidity dimension INSUFFICIENT_DATA.
    """
    by_market: dict[str, list[float]] = {}
    for s in dataset.snapshots:
        end = _end_date(s)
        if end is None or end.date() >= split_date:
            continue  # training only
        if s.liquidity is None:
            continue  # missing is missing, not zero
        by_market.setdefault(s.market_id, []).append(float(s.liquidity))
    vals = sorted(sum(v) / len(v) for v in by_market.values())
    if len(vals) < 3:
        return None
    lo = vals[len(vals) // 3]
    hi = vals[2 * len(vals) // 3]
    if lo == hi:
        return None
    return (lo, hi)


def _liquidity_tercile(s: MarketSnapshot, bounds: tuple[float, float] | None) -> str | None:
    if s.liquidity is None or bounds is None:
        return None
    liq = float(s.liquidity)
    if liq < bounds[0]:
        return "low"
    if liq < bounds[1]:
        return "mid"
    return "high"


def _group_clusters(snaps: list[MarketSnapshot], key_fn) -> dict[str, list[MarketSnapshot]]:
    groups: dict[str, list[MarketSnapshot]] = {}
    for s in snaps:
        groups.setdefault(key_fn(s), []).append(s)
    return groups


def _cell_test(
    name: str,
    snaps: list[MarketSnapshot],
    fn,
    min_clusters: int | None = None,
    min_effect: float = MIN_RESIDUAL,
) -> FamilyTest | None:
    """A bootstrap test over one cell; None if below the sparse-cell floor."""
    if min_clusters is None:
        min_clusters = MIN_CELL_CLUSTERS
    groups = _cluster_means(snaps, fn)
    ncl = len(groups)
    if ncl < min_clusters:
        return None
    est, p, _ = _cluster_bootstrap_p(groups)
    return FamilyTest(
        name=name, p_value=p, p_adjusted=1.0, effect=est,
        min_effect=min_effect, n_clusters=ncl, passed=False,
        details=f"{ncl} clusters",
    )


def _finalize_tests(tests: list[FamilyTest], fdr_level: float = FDR_LEVEL) -> list[FamilyTest]:
    adj = _bh([t.p_value for t in tests])
    for t, pa in zip(tests, adj):
        t.p_adjusted = pa
        t.passed = pa < fdr_level and abs(t.effect) >= t.min_effect
    return tests


def _binned_ece(snaps: list[MarketSnapshot], key_fn) -> float:
    """Cluster-weighted binned calibration error (v2.3.1 F1 metric)."""
    groups = _group_clusters(snaps, key_fn)
    total_clusters = 0
    cell_effects: dict[str, tuple[int, float]] = {}
    for key, cell in groups.items():
        cm = _cluster_means(cell, _residual)
        ncl = len(cm)
        if ncl == 0:
            continue
        cluster_residuals = [sum(v) / len(v) for v in cm.values()]
        cell_effects[key] = (ncl, sum(cluster_residuals) / ncl)
        total_clusters += ncl
    if total_clusters == 0:
        return 0.0
    ece = 0.0
    for ncl, effect in cell_effects.values():
        ece += (ncl / total_clusters) * abs(effect)
    return ece


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

    # Primary descriptive metric: cluster-weighted binned ECE over price deciles.
    ece = _binned_ece(snaps, lambda s: _price_bucket(float(s.yes_mid)))

    liq_bounds = _liquidity_bounds(dataset, split_date)

    cell_defs: list[tuple[str, list[MarketSnapshot]]] = []
    for idx in range(10):
        cell = [s for s in snaps if _price_bucket(float(s.yes_mid)) == idx]
        cell_defs.append((f"price[{idx/10:.1f},{(idx+1)/10:.1f})", cell))
    for cat in sorted({_normalize_category(s) for s in snaps}):
        if cat == "unknown":
            continue  # reported but no inference cell (v2.3.1)
        cell = [s for s in snaps if _normalize_category(s) == cat]
        cell_defs.append((f"category[{cat}]", cell))
    cell_defs.append(("spread[tight]", [s for s in snaps if _spread_regime(s) == "tight"]))
    cell_defs.append(("spread[wide]", [s for s in snaps if _spread_regime(s) == "wide"]))
    if liq_bounds is not None:
        for name in ("low", "mid", "high"):
            cell = [s for s in snaps if _liquidity_tercile(s, liq_bounds) == name]
            cell_defs.append((f"liquidity[{name}]", cell))
    else:
        d.reasons.append("liquidity dimension INSUFFICIENT_DATA (tercile collapse)")
    for ttr in ("<1h", "1h-24h", "1d-7d", ">=168h"):
        cell = [s for s in snaps if _ttr_bucket(s.hours_to_resolution) == ttr]
        cell_defs.append((f"TTR[{ttr}]", cell))

    tests: list[FamilyTest] = []
    for name, cell in cell_defs:
        t = _cell_test(name, cell, _residual)
        if t is not None:
            tests.append(t)

    tests = _finalize_tests(tests)
    significant = [t for t in tests if t.passed]
    if significant:
        d.verdict = VERDICT_SUPPORTED
        d.reasons.append(
            f"non-flat residual surface: {len(significant)} cell(s) FDR-significant "
            f"with |residual| >= 1pp"
        )
    else:
        d.verdict = VERDICT_NOT_SUPPORTED
        d.reasons.append("residual surface indistinguishable from flat at FDR 10%")
    d.tests = tests
    # ECE is descriptive (cluster-bootstrap CI, not a p-value).
    d.primary_metric = f"ECE={ece:.4f} (descriptive; reduction arm NOT_RUN)"
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

    # Pooled low-price test (headline FLB hypothesis) + category x price cells,
    # all in ONE family-wide BH universe (v2.3.1).
    pooled = [s for s in snaps if float(s.yes_mid) < FLB_LOW_PRICE]
    pooled_test = _cell_test("price[0.00,0.10)_pooled", pooled, _residual)

    cells: list[FamilyTest] = []
    categories = sorted({_normalize_category(s) for s in snaps if _normalize_category(s) != "unknown"})
    for idx in range(10):
        lo = idx / 10.0
        for cat in categories:
            cell = [
                s for s in snaps
                if _price_bucket(float(s.yes_mid)) == idx and _normalize_category(s) == cat
            ]
            t = _cell_test(f"price[{lo:.1f},{(idx+1)/10:.1f})x{cat}", cell, _residual)
            if t is not None:
                cells.append(t)

    tests = [t for t in (pooled_test, *cells) if t is not None]
    tests = _finalize_tests(tests)

    pooled_final = next((t for t in tests if t.name == "price[0.00,0.10)_pooled"), None)
    if pooled_final is not None and pooled_final.passed and pooled_final.effect < 0:
        d.verdict = VERDICT_SUPPORTED
        d.reasons.append(
            "pooled longshot underperformance: E[Y-p | p<0.10] < 0, FDR-significant, "
            f"|effect|>=1pp (effect={pooled_final.effect:.4f})"
        )
    else:
        d.verdict = VERDICT_NOT_SUPPORTED
        d.reasons.append(
            "pooled p<0.10 residual not negative/FDR-significant/>=1pp; "
            "category x longshot cells are reported but do not alone support FLB"
        )
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

    order = ["<1h", "1h-24h", "1d-7d", ">=168h"]
    buckets = {k: [s for s in snaps if _ttr_bucket(s.hours_to_resolution) == k] for k in order}

    tests: list[FamilyTest] = []
    mag: dict[str, float] = {}
    for b in order:
        t = _cell_test(
            f"TTR[{b}]", buckets[b],
            lambda s: abs(_residual(s)), min_effect=MIN_TTR_RESIDUAL,
        )
        if t is not None:
            tests.append(t)
            mag[b] = t.effect

    tests = _finalize_tests(tests)

    present = [b for b in order if b in mag]
    if len(present) < 2:
        d.verdict = VERDICT_INSUFFICIENT
        d.reasons.append("insufficient TTR buckets with >= 20 clusters")
        d.tests = tests
        return d

    nearest, farthest = present[0], present[-1]
    diff = mag[farthest] - mag[nearest]
    monotone = all(mag[present[i]] <= mag[present[i + 1]] for i in range(len(present) - 1))

    if diff >= MIN_TTR_RESIDUAL and monotone:
        d.verdict = VERDICT_SUPPORTED
        d.reasons.append(f"|residual| monotone in TTR with far-near increase {diff:.4f} >= 1pp")
    else:
        d.verdict = VERDICT_NOT_SUPPORTED
        d.reasons.append(
            f"no monotone increasing |residual| with TTR (far-near {diff:.4f}, monotone={monotone})"
        )
    d.tests = tests
    return d


# ── Family 3 — microstructure / price-impact proxy (deferred: no horizon) ────


def evaluate_family_3_microstructure(
    dataset: ResearchDataset, split_date: date | None = None
) -> FamilyDecision:
    return FamilyDecision(
        "F3_microstructure", "mean|dMid|", 0, VERDICT_NOT_RUN,
        reasons=["insufficient specification: no frozen horizon (v2.3 deferral)"],
    )


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