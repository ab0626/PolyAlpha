"""Experiment metadata and diagnostics; no fabricated annualized ratios.

Includes drift detection, feature distribution monitoring, concept drift
detectors (ADWIN, Page-Hinkley), and provenance tracking.
"""

import hashlib
import json
import math
import subprocess
from collections import deque
from pathlib import Path
from uuid import uuid4


def save_experiment(
    directory, config, report, data_path, training_window=None, validation_window=None
):
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except subprocess.CalledProcessError:
        commit = None
    experiment_id = str(uuid4())
    path = Path(directory) / f"{experiment_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Stream hash to avoid loading an entire historical database into memory.
    digest = hashlib.sha256()
    with open(data_path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    payload = dict(
        experiment_id=experiment_id,
        git_commit=commit,
        model_version="diagnostic-baseline-v1",
        feature_version="book-v1",
        training_window=training_window,
        validation_window=validation_window,
        configuration=config,
        data_sha256=digest.hexdigest(),
        metrics=report,
    )
    with path.open("x", encoding="utf-8") as destination:
        json.dump(payload, destination, indent=2, default=str)
    return path


def drift(previous_brier, recent_brier, minimum_count, recent_count, tolerance=0.05):
    return dict(
        actionable=recent_count >= minimum_count and recent_brier > previous_brier + tolerance,
        recent_count=recent_count,
        change=recent_brier - previous_brier,
    )


def code_provenance():
    package = Path(__file__).resolve().parent
    root = package.parents[1] if package.parent.name == "src" else Path.cwd()
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        digest.update(path.relative_to(package).as_posix().encode())
        digest.update(path.read_bytes())
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        commit = None
    return dict(
        git_commit=commit,
        source_sha256=digest.hexdigest(),
        feature_version="book-v1",
        model_version="diagnostic-baseline-v1",
        training_window=None,
        validation_window=None,
    )


class CalibrationDriftDetector:
    """Monitor calibration quality over time for drift detection.

    Maintains rolling windows of Brier scores and calibration errors
    to detect model deterioration.
    """

    def __init__(
        self,
        window_size: int = 100,
        alert_threshold: float = 0.05,
        min_samples: int = 20,
    ):
        self.window_size = window_size
        self.alert_threshold = alert_threshold
        self.min_samples = min_samples
        self.recent_brier: deque[float] = deque(maxlen=window_size)
        self.baseline_brier: float | None = None
        self.calibration_errors: deque[float] = deque(maxlen=window_size)

    def update(self, brier: float, calibration_error: float | None = None):
        """Add new observation."""
        self.recent_brier.append(brier)
        if calibration_error is not None:
            self.calibration_errors.append(calibration_error)

    def set_baseline(self, brier: float):
        """Set baseline Brier score from training period."""
        self.baseline_brier = brier

    def check_drift(self) -> dict:
        """Check for calibration drift.

        Returns dict with drift status and metrics.
        """
        if len(self.recent_brier) < self.min_samples:
            return {
                "drift_detected": False,
                "reason": "insufficient_samples",
                "recent_count": len(self.recent_brier),
            }

        recent_avg = sum(self.recent_brier) / len(self.recent_brier)

        if self.baseline_brier is not None:
            change = recent_avg - self.baseline_brier
            drift_detected = change > self.alert_threshold
            return {
                "drift_detected": drift_detected,
                "baseline_brier": self.baseline_brier,
                "recent_avg_brier": recent_avg,
                "change": change,
                "threshold": self.alert_threshold,
                "recent_count": len(self.recent_brier),
            }

        # No baseline: check variance
        if len(self.recent_brier) >= self.min_samples:
            variance = sum((b - recent_avg) ** 2 for b in self.recent_brier) / len(
                self.recent_brier
            )
            std = math.sqrt(variance)
            # High variance in recent predictions may indicate instability
            return {
                "drift_detected": std > 0.1,
                "recent_avg_brier": recent_avg,
                "recent_std": std,
                "recent_count": len(self.recent_brier),
            }

        return {
            "drift_detected": False,
            "reason": "no_baseline",
            "recent_avg_brier": recent_avg,
        }


class FeatureDriftDetector:
    """Monitor feature distribution drift using population stability index."""

    def __init__(self, reference_distributions: dict[str, list[float]] | None = None):
        self.reference = reference_distributions or {}
        self.current: dict[str, list[float]] = {}

    def set_reference(self, feature_name: str, values: list[float]):
        """Set reference distribution for a feature."""
        self.reference[feature_name] = values

    def update(self, feature_name: str, value: float):
        """Add new feature observation."""
        self.current.setdefault(feature_name, []).append(value)

    def psi(self, feature_name: str, buckets: int = 10) -> float | None:
        """Population Stability Index for a feature.

        PSI < 0.1: no significant change
        PSI 0.1-0.2: moderate change
        PSI > 0.2: significant drift
        """
        ref = self.reference.get(feature_name)
        cur = self.current.get(feature_name)
        if not ref or not cur or len(ref) < buckets or len(cur) < buckets:
            return None

        # Create bins from reference
        lo = min(ref)
        hi = max(ref)
        if lo == hi:
            # All reference values are the same
            if all(v == lo for v in cur):
                return 0.0  # distributions are identical
            # Reference is constant but current varies - compute simple divergence
            return 1.0  # maximum drift
        bin_width = (hi - lo) / buckets

        ref_counts = [0] * buckets
        cur_counts = [0] * buckets

        for v in ref:
            idx = min(buckets - 1, int((v - lo) / bin_width))
            ref_counts[idx] += 1
        for v in cur:
            idx = min(buckets - 1, int((v - lo) / bin_width))
            cur_counts[idx] += 1

        psi = 0.0
        for r, c in zip(ref_counts, cur_counts):
            r_pct = r / len(ref) if len(ref) > 0 else 0
            c_pct = c / len(cur) if len(cur) > 0 else 0
            if r_pct > 0 and c_pct > 0:
                psi += (c_pct - r_pct) * math.log(c_pct / r_pct)

        return psi

    def check_all(self, threshold: float = 0.2) -> dict:
        """Check all features for drift."""
        results = {}
        any_drift = False
        for name in self.reference:
            psi = self.psi(name)
            if psi is not None:
                drifted = psi > threshold
                if drifted:
                    any_drift = True
                results[name] = {"psi": psi, "drifted": drifted}
        return {"any_drift": any_drift, "features": results}


class ADWINDriftDetector:
    """ADaptive WINdowing drift detector.

    Detects concept drift by maintaining a variable-length window and testing
    whether the mean of recent observations differs significantly from older
    observations. Uses Hoeffding-like bounds.

    Reference: Bifet & Gavalda, "Learning from Time-Changing Data with Adaptive Windowing"
    """

    def __init__(self, delta: float = 0.002, min_window: int = 5, max_window: int = 1000):
        if not 0.0001 <= delta <= 0.5:
            raise ValueError("delta must be in [0.0001, 0.5]")
        self.delta = delta
        self.min_window = min_window
        self.max_window = max_window
        self._window: deque[float] = deque(maxlen=max_window)
        self._total: float = 0
        self._variance_total: float = 0
        self._drift_points: list[int] = []

    def update(self, value: float) -> dict:
        """Add observation and check for drift.

        Returns dict with drift_detected flag and metrics.
        """
        self._window.append(value)
        self._total += value
        n = len(self._window)

        if n < self.min_window * 2:
            return {"drift_detected": False, "reason": "insufficient_data", "window_size": n}

        # Test all possible split points
        for cut in range(self.min_window, n - self.min_window):
            left = list(self._window)[:cut]
            right = list(self._window)[cut:]
            mean_left = sum(left) / len(left)
            mean_right = sum(right) / len(right)
            n_left, n_right = len(left), len(right)

            # Hoeffding-like bound for means
            m = 1.0 / (1.0 / n_left + 1.0 / n_right)
            epsilon = math.sqrt((1.0 / (2.0 * m)) * math.log(4.0 / self.delta))

            if abs(mean_left - mean_right) >= epsilon:
                # Drift detected: shrink window to right half
                old_len = len(self._window)
                self._drift_points.append(old_len)
                # Keep only the right portion
                keep = list(self._window)[cut:]
                self._window.clear()
                self._window.extend(keep)
                self._total = sum(self._window)
                return {
                    "drift_detected": True,
                    "drift_magnitude": abs(mean_left - mean_right),
                    "cut_point": cut,
                    "window_before": old_len,
                    "window_after": len(self._window),
                    "left_mean": mean_left,
                    "right_mean": mean_right,
                }

        return {"drift_detected": False, "window_size": n}

    @property
    def mean(self) -> float | None:
        if not self._window:
            return None
        return self._total / len(self._window)

    @property
    def drift_count(self) -> int:
        return len(self._drift_points)

    @property
    def drift_points(self) -> list[int]:
        return list(self._drift_points)


class PageHinkleyDetector:
    """Page-Hinkley test for detecting changes in the mean of a Gaussian process.

    Accumulates deviations from the running mean and flags drift when the
    accumulated deviation exceeds a threshold.

    Reference: Page, "Continuous Inspection Schemes" (1954)
    """

    def __init__(
        self,
        threshold: float = 50.0,
        min_instances: int = 30,
        alpha: float = 0.005,
    ):
        self.threshold = threshold
        self.min_instances = min_instances
        self.alpha = alpha  # tolerance parameter
        self._n: int = 0
        self._sum: float = 0
        self._mean: float = 0
        self._min_cumulative: float = 0
        self._cumulative: float = 0
        self._drift_points: list[int] = []

    def update(self, value: float) -> dict:
        """Add observation and check for drift.

        Returns dict with drift_detected flag and metrics.
        """
        self._n += 1
        self._sum += value

        if self._n < self.min_instances:
            return {"drift_detected": False, "reason": "insufficient_data", "count": self._n}

        # Update running mean
        old_mean = self._mean
        self._mean = self._sum / self._n

        # Accumulate deviation from running mean
        self._cumulative += value - self._mean - self.alpha

        # Track minimum
        if self._cumulative < self._min_cumulative:
            self._min_cumulative = self._cumulative

        # Test statistic
        statistic = self._cumulative - self._min_cumulative

        if statistic > self.threshold:
            # Drift detected: reset
            self._drift_points.append(self._n)
            self._cumulative = 0
            self._min_cumulative = 0
            self._n = 0
            self._sum = 0
            self._mean = 0
            return {
                "drift_detected": True,
                "statistic": statistic,
                "threshold": self.threshold,
                "mean_before": old_mean,
                "count": self._n,
            }

        return {"drift_detected": False, "statistic": statistic, "count": self._n}

    @property
    def mean(self) -> float | None:
        return self._mean if self._n > 0 else None

    @property
    def drift_count(self) -> int:
        return len(self._drift_points)

    @property
    def drift_points(self) -> list[int]:
        return list(self._drift_points)
