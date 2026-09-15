"""Chronological calibration with explicitly available labels and cluster purging."""

import math
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Observation:
    market_id: str
    cluster: str
    predicted_at: datetime
    label_known_at: datetime | None
    probability: float
    outcome: int | None

    def __post_init__(self):
        from .domain import utc

        utc(self.predicted_at)
        if self.label_known_at is not None:
            utc(self.label_known_at)
        if (
            not self.market_id
            or not self.cluster
            or not math.isfinite(self.probability)
            or not 0 <= self.probability <= 1
            or (
                self.outcome is not None
                and (type(self.outcome) is not int or self.outcome not in (0, 1))
            )
        ):
            raise ValueError("invalid calibration observation")
        if (self.outcome is None) != (self.label_known_at is None):
            raise ValueError("outcome and availability timestamp must both be present or absent")
        if self.label_known_at is not None and self.label_known_at < self.predicted_at:
            raise ValueError("forecast follows known outcome")


class InsufficientData(ValueError):
    """A valid dataset does not yet contain enough mature training labels."""


def canonical_rows(rows):
    """One predeclared earliest forecast per market across the entire evaluation."""
    selected = {}
    labels = {}
    availability = {}
    for row in sorted(rows, key=lambda r: (r.predicted_at, r.market_id)):
        prior = selected.get(row.market_id)
        if prior is not None and prior.cluster != row.cluster:
            raise ValueError("market has inconsistent evaluation cluster assignments")
        if (
            prior is not None
            and prior.predicted_at == row.predicted_at
            and prior.probability != row.probability
        ):
            raise ValueError("conflicting forecasts at the same market/timestamp")
        if row.outcome is not None:
            if row.market_id in labels and labels[row.market_id] != row.outcome:
                raise ValueError("conflicting market outcomes")
            labels[row.market_id] = row.outcome
            availability[row.market_id] = min(
                availability.get(row.market_id, row.label_known_at), row.label_known_at
            )
        selected.setdefault(row.market_id, row)
    from dataclasses import replace

    return [
        replace(row, outcome=labels[row.market_id], label_known_at=availability[row.market_id])
        if row.market_id in labels
        else row
        for row in selected.values()
    ]


def metrics(probabilities, outcomes, bins=10):
    if len(probabilities) != len(outcomes) or not probabilities or bins < 1:
        raise ValueError("nonempty aligned observations required")
    if any(not math.isfinite(p) or not 0 <= p <= 1 for p in probabilities) or any(
        y not in (0, 1) for y in outcomes
    ):
        raise ValueError("invalid probabilities/outcomes")
    buckets = [[] for _ in range(bins)]
    for p, y in zip(probabilities, outcomes):
        buckets[min(bins - 1, int(p * bins))].append((p, y))
    reliability = []
    ece = 0
    for i, values in enumerate(buckets):
        avg = sum(p for p, y in values) / len(values) if values else None
        realized = sum(y for p, y in values) / len(values) if values else None
        if values:
            ece += len(values) / len(probabilities) * abs(avg - realized)
        reliability.append(
            dict(
                lower=i / bins,
                upper=(i + 1) / bins,
                count=len(values),
                predicted=avg,
                observed=realized,
            )
        )
    clipped = [min(1 - 1e-12, max(1e-12, p)) for p in probabilities]
    return dict(
        sample_size=len(probabilities),
        brier=sum((p - y) ** 2 for p, y in zip(probabilities, outcomes)) / len(outcomes),
        log_loss=-sum(y * math.log(p) + (1 - y) * math.log1p(-p) for p, y in zip(clipped, outcomes))
        / len(outcomes),
        ece=ece,
        reliability=reliability,
    )


def training_rows(rows, cutoff, excluded_clusters=()):
    selected = {}
    for row in canonical_rows(rows):
        if (
            row.predicted_at < cutoff
            and row.label_known_at is not None
            and row.label_known_at < cutoff
            and row.cluster not in excluded_clusters
        ):
            # One forecast per market: repeated ticks cannot multiply its training weight.
            selected.setdefault(row.market_id, row)
    result = list(selected.values())
    if len(result) < 4 or len({r.outcome for r in result}) < 2:
        raise InsufficientData("insufficient resolved training events")
    return result


class Isotonic:
    def fit(self, rows, cutoff, excluded_clusters=()):
        rows = training_rows(rows, cutoff, excluded_clusters)
        grouped = {}
        for row in rows:
            grouped.setdefault(row.probability, []).append(row.outcome)
        blocks = []
        for p, ys in sorted(grouped.items()):
            blocks.append([p, p, sum(ys), len(ys)])
            while len(blocks) > 1 and blocks[-2][2] / blocks[-2][3] > blocks[-1][2] / blocks[-1][3]:
                right, left = blocks.pop(), blocks.pop()
                blocks.append([left[0], right[1], left[2] + right[2], left[3] + right[3]])
        self.starts = [b[0] for b in blocks]
        self.values = [b[2] / b[3] for b in blocks]
        self.cutoff = cutoff
        return self

    def predict(self, p):
        if not math.isfinite(p) or not 0 <= p <= 1:
            raise ValueError("invalid probability")
        return self.values[max(0, bisect_right(self.starts, p) - 1)]


class Platt:
    def fit(self, rows, cutoff, excluded_clusters=()):
        rows = training_rows(rows, cutoff, excluded_clusters)
        # Library solver; learned only from the filtered resolved training set.
        from sklearn.linear_model import LogisticRegression

        self.model = LogisticRegression(C=1.0, solver="lbfgs", random_state=0)
        self.model.fit([[self.logit(r.probability)] for r in rows], [r.outcome for r in rows])
        self.cutoff = cutoff
        return self

    @staticmethod
    def logit(p):
        if not math.isfinite(p) or not 0 <= p <= 1:
            raise ValueError("invalid probability")
        p = min(1 - 1e-9, max(1e-9, p))
        return math.log(p / (1 - p))

    def predict(self, p):
        return float(self.model.predict_proba([[self.logit(p)]])[0, 1])


class TemperatureScaling:
    """Temperature scaling for probability calibration.

    Learns a single temperature parameter T that scales the logits
    before sigmoid. When T > 1, probabilities are pushed toward 0.5
    (more uncertain). When T < 1, probabilities are pushed toward
    extremes (more confident).
    """

    def __init__(self):
        self._temperature = 1.0
        self._fitted = False

    def _sigmoid(self, z):
        if z >= 0:
            return 1 / (1 + math.exp(-z))
        else:
            exp_z = math.exp(z)
            return exp_z / (1 + exp_z)

    def _to_logit(self, p):
        eps = 1e-6
        p = max(eps, min(1 - eps, p))
        return math.log(p / (1 - p))

    def fit(self, probabilities, outcomes):
        """Fit temperature using grid search on NLL."""
        n = len(probabilities)
        if n < 2 or n != len(outcomes):
            raise ValueError("at least two aligned observations required")

        best_T = 1.0
        best_nll = float("inf")

        for T_int in range(10, 500):  # T from 0.10 to 5.00
            T = T_int / 100.0
            nll = 0.0
            for p, y in zip(probabilities, outcomes):
                logit = self._to_logit(float(p)) / T
                q = self._sigmoid(logit)
                q = max(1e-12, min(1 - 1e-12, q))
                nll -= float(y) * math.log(q) + (1 - float(y)) * math.log(1 - q)
            nll /= n
            if nll < best_nll:
                best_nll = nll
                best_T = T

        self._temperature = best_T
        self._fitted = True

    def predict(self, probability):
        """Apply temperature scaling to a single probability."""
        if not self._fitted:
            raise RuntimeError("calibrator not fitted")
        logit = self._to_logit(float(probability)) / self._temperature
        return self._sigmoid(logit)

    @property
    def temperature(self):
        return self._temperature


def walk_forward(rows, first_test, end, window=timedelta(days=30), method="isotonic"):
    from .domain import utc

    utc(first_test)
    utc(end)
    if window.total_seconds() <= 0 or first_test >= end or method not in ("isotonic", "platt"):
        raise ValueError("invalid walk-forward specification")
    rows = canonical_rows(rows)
    results = []
    start = first_test
    while start < end:
        stop = min(end, start + window)
        candidates = [r for r in rows if start <= r.predicted_at < stop]
        clusters = {r.cluster for r in candidates}
        testing = [
            r for r in candidates if r.label_known_at is not None and r.label_known_at <= end
        ]
        fold = dict(
            start=start.isoformat(),
            end=stop.isoformat(),
            training_cutoff_exclusive=start.isoformat(),
            label_evaluation_cutoff=end.isoformat(),
            candidate_markets=len(candidates),
            unresolved_markets=len(candidates) - len(testing),
            cluster_count=len(clusters),
            prediction_ledger=[],
        )
        try:
            if not candidates:
                raise InsufficientData("no forecasts in evaluation fold")
            training = training_rows(rows, start, clusters)
            model = (Isotonic() if method == "isotonic" else Platt()).fit(training, start)
            fold["training_markets"] = len(training)
            fold["training_prediction_start"] = min(r.predicted_at for r in training).isoformat()
            fold["training_prediction_end"] = max(r.predicted_at for r in training).isoformat()
            fold["training_latest_label"] = max(r.label_known_at for r in training).isoformat()
            fold["excluded_clusters"] = sorted(clusters)
            for row in candidates:
                mature = row.label_known_at is not None and row.label_known_at <= end
                fold["prediction_ledger"].append(
                    dict(
                        market_id=row.market_id,
                        cluster=row.cluster,
                        predicted_at=row.predicted_at.isoformat(),
                        label_known_at=row.label_known_at.isoformat() if mature else None,
                        raw_probability=row.probability,
                        calibrated_probability=model.predict(row.probability),
                        outcome=row.outcome if mature else None,
                    )
                )
            if testing:
                observed = [p for p in fold["prediction_ledger"] if p["outcome"] is not None]
                fold["metrics"] = metrics(
                    [p["calibrated_probability"] for p in observed],
                    [p["outcome"] for p in observed],
                )
                fold["raw_metrics"] = metrics(
                    [p["raw_probability"] for p in observed], [p["outcome"] for p in observed]
                )
                fold["status"] = "evaluated"
            else:
                fold["status"] = "awaiting_labels"
        except InsufficientData as error:
            fold.update(status="insufficient_data", reason=str(error))
        results.append(fold)
        start = stop
    return results
