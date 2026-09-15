"""Disagreement analysis — buckets forecasts by model-market divergence.

Section 10 of the v0.3 spec: when model and market disagree strongly,
does the model add value? Disagreement buckets reveal where edge lives.
"""

from dataclasses import dataclass

from .research_dataset import ResearchDataset

# Fixed percentage buckets of |model_probability - market_mid|
_FIXED_BUCKETS: list[tuple[float, float, str]] = [
    (0.00, 0.01, "0-1%"),
    (0.01, 0.02, "1-2%"),
    (0.02, 0.05, "2-5%"),
    (0.05, 0.10, "5-10%"),
    (0.10, 0.15, "10-15%"),
    (0.15, 0.20, "15-20%"),
    (0.20, float("inf"), ">20%"),
]


@dataclass(frozen=True)
class DisagreementBucket:
    bucket_label: str
    disagreement_range: tuple[float, float]
    model_brier: float
    market_brier: float
    delta_brier: float
    model_correct_pct: float
    market_correct_pct: float
    count: int
    mean_outcome: float
    mean_model_prob: float
    mean_market_mid: float
    win_rate: float = 0.0
    avg_holding_period_hours: float = 0.0


@dataclass(frozen=True)
class DisagreementAnalysis:
    buckets: list[DisagreementBucket]
    total_disagreement: float
    correlation: float

    def summary(self) -> dict:
        return {
            "total_disagreement": self.total_disagreement,
            "correlation": self.correlation,
            "bucket_count": len(self.buckets),
        }


def _pearson_r(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (n - 1)
    sx = (sum((x - mx) ** 2 for x in xs) / (n - 1)) ** 0.5
    sy = (sum((y - my) ** 2 for y in ys) / (n - 1)) ** 0.5
    if sx == 0 or sy == 0:
        return 0.0
    return cov / (sx * sy)


def analyze_disagreement(dataset: ResearchDataset) -> DisagreementAnalysis:
    """Analyze model-market disagreement using fixed percentage buckets."""
    records = [
        r
        for r in dataset.snapshots
        if r.final_resolution is not None
        and r.yes_mid is not None
        and r.model_probability is not None
    ]
    if not records:
        return DisagreementAnalysis(buckets=[], total_disagreement=0.0, correlation=0.0)

    abs_disagree = [abs(float(r.model_probability) - float(r.yes_mid)) for r in records]
    correct_flags = [
        1.0 if (float(r.model_probability) >= 0.5) == (r.final_resolution == 1) else 0.0
        for r in records
    ]
    holding_hours = [r.hours_to_resolution if r.hours_to_resolution is not None else 0.0 for r in records]
    total_disagreement = sum(abs_disagree) / len(abs_disagree)

    # Assign each record to a fixed bucket by index
    bucketed: list[list] = [[] for _ in _FIXED_BUCKETS]
    bucketed_hold: list[list] = [[] for _ in _FIXED_BUCKETS]
    bucketed_correct: list[list] = [[] for _ in _FIXED_BUCKETS]
    for i, rec in enumerate(records):
        for idx, (lo, hi, _) in enumerate(_FIXED_BUCKETS):
            if lo <= abs_disagree[i] < hi:
                bucketed[idx].append((rec, abs_disagree[i]))
                bucketed_hold[idx].append(holding_hours[i])
                bucketed_correct[idx].append(correct_flags[i])
                break

    buckets = []
    for idx, (lo, hi, label) in enumerate(_FIXED_BUCKETS):
        items = bucketed[idx]
        if not items:
            continue
        n = len(items)
        mps = [float(it[0].model_probability) for it in items]
        mks = [float(it[0].yes_mid) for it in items]
        outs = [float(it[0].final_resolution) for it in items]
        mb = sum((p - o) ** 2 for p, o in zip(mps, outs)) / n
        kb = sum((p - o) ** 2 for p, o in zip(mks, outs)) / n
        mc = sum(1 for p, o in zip(mps, outs) if (p >= 0.5) == (o == 1.0))
        kc = sum(1 for p, o in zip(mks, outs) if (p >= 0.5) == (o == 1.0))
        correct_in_bucket = bucketed_correct[idx]
        hold_in_bucket = bucketed_hold[idx]
        wr = sum(correct_in_bucket) / n if n > 0 else 0.0
        avg_hold = sum(hold_in_bucket) / n if n > 0 else 0.0

        buckets.append(
            DisagreementBucket(
                bucket_label=label,
                disagreement_range=(lo, hi) if hi != float("inf") else (lo, 1.0),
                model_brier=round(mb, 6),
                market_brier=round(kb, 6),
                delta_brier=round(kb - mb, 6),
                model_correct_pct=round(mc / n, 4),
                market_correct_pct=round(kc / n, 4),
                count=n,
                mean_outcome=round(sum(outs) / n, 4),
                mean_model_prob=round(sum(mps) / n, 4),
                mean_market_mid=round(sum(mks) / n, 4),
                win_rate=round(wr, 4),
                avg_holding_period_hours=round(avg_hold, 2),
            )
        )

    return DisagreementAnalysis(
        buckets=buckets,
        total_disagreement=round(total_disagreement, 6),
        correlation=round(_pearson_r(abs_disagree, correct_flags), 4),
    )
