"""Conditional probability graph for co-occurrence signal extraction.

Part 14: Models P(B|A) and P(B|not A) from historical co-occurrence to
produce implied probability signals. Compares implied vs. observed
executable P(B) and tracks residual distributions for signal generation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TypeAlias

D = Decimal

SignalId: TypeAlias = str


@dataclass(frozen=True)
class Relationship:
    """A single conditional relationship A -> B."""

    a_id: SignalId
    b_id: SignalId
    p_b_given_a: float = 0.0
    p_b_given_not_a: float = 0.0
    p_a: float = 0.0
    co_occurrences: int = 0
    a_true_count: int = 0
    b_true_count: int = 0
    implied_p_b: float = 0.0
    residual: float = 0.0
    normalized_residual: float = 0.0


@dataclass(frozen=True)
class Signal:
    """Output signal from the conditional graph."""

    b_id: SignalId
    implied_p_b: float
    observed_p_b: float
    residual: float
    normalized_residual: float
    relationship_count: int
    strength: float  # abs(normalized_residual) * sign(residual)


@dataclass(frozen=True)
class GraphSummary:
    """Aggregate summary of the conditional graph."""

    total_relationships: int
    active_signals: int
    mean_abs_residual: float
    max_abs_residual: float
    mean_normalized_residual: float
    residual_std: float
    strong_signal_count: int  # |normalized_residual| > 2.0


class ConditionalGraph:
    """Tracks conditional probabilities P(B|A) and P(B|not A) from history.

    Produces implied probability signals and compares them against observed
    executable prices. This is a candidate signal source, NOT an auto-trader.
    """

    def __init__(self, min_samples: int = 30, residual_threshold: float = 2.0) -> None:
        self._min_samples = min_samples
        self._residual_threshold = residual_threshold
        self._relationships: dict[tuple[SignalId, SignalId], _Counts] = {}
        self._observed_p_b: dict[SignalId, _RunningStats] = {}

    # ── Public API ──────────────────────────────────────────────────────────

    def add_relationship(
        self,
        a_id: SignalId,
        b_id: SignalId,
        a_observed: bool,
        b_observed: bool,
        b_executable_price: float | None = None,
    ) -> None:
        """Record a co-occurrence observation for A and B.

        Args:
            a_id: Signal identifier for the antecedent.
            b_id: Signal identifier for the consequent.
            a_observed: Whether A is true in this observation.
            b_observed: Whether B is true in this observation.
            b_executable_price: Optional executable price for B (observed P(B)).
        """
        key = (a_id, b_id)
        if key not in self._relationships:
            self._relationships[key] = _Counts()
        counts = self._relationships[key]
        counts.total += 1
        if a_observed:
            counts.a_true += 1
        if b_observed:
            counts.b_true += 1
        if a_observed and b_observed:
            counts.ab_true += 1

        # Track observed P(B) globally
        if b_executable_price is not None:
            if b_id not in self._observed_p_b:
                self._observed_p_b[b_id] = _RunningStats()
            self._observed_p_b[b_id].add(b_executable_price)

    def compute_implied(self) -> dict[tuple[SignalId, SignalId], Relationship]:
        """Compute implied P(B) for all relationships with sufficient data."""
        results: dict[tuple[SignalId, SignalId], Relationship] = {}
        for key, counts in self._relationships.items():
            if counts.total < self._min_samples:
                continue
            a_id, b_id = key
            p_a = counts.a_true / counts.total
            p_b = counts.b_true / counts.total

            if counts.a_true > 0:
                p_b_given_a = counts.ab_true / counts.a_true
            else:
                p_b_given_a = 0.0

            not_a_count = counts.total - counts.a_true
            if not_a_count > 0:
                b_given_not_a = (counts.b_true - counts.ab_true) / not_a_count
            else:
                b_given_not_a = 0.0

            implied_p_b = p_b_given_a * p_a + b_given_not_a * (1.0 - p_a)
            residual = implied_p_b - p_b

            # Compute residual std from all relationships for this b_id
            residual_std = self._residual_std_for_b(b_id)
            normalized_residual = residual / residual_std if residual_std > 0 else 0.0

            results[key] = Relationship(
                a_id=a_id,
                b_id=b_id,
                p_b_given_a=p_b_given_a,
                p_b_given_not_a=b_given_not_a,
                p_a=p_a,
                co_occurrences=counts.total,
                a_true_count=counts.a_true,
                b_true_count=counts.b_true,
                implied_p_b=implied_p_b,
                residual=residual,
                normalized_residual=normalized_residual,
            )
        return results

    def get_signals(
        self,
        observed_prices: dict[SignalId, float] | None = None,
    ) -> list[Signal]:
        """Generate signal candidates from large residuals.

        Args:
            observed_prices: Optional map of b_id -> observed executable price.
                             Falls back to internally tracked observed P(B).
        """
        relationships = self.compute_implied()
        signals: list[Signal] = []
        seen_b: set[SignalId] = set()

        for (a_id, b_id), rel in relationships.items():
            if b_id in seen_b:
                continue
            obs_p_b = 0.5
            if observed_prices and b_id in observed_prices:
                obs_p_b = observed_prices[b_id]
            elif b_id in self._observed_p_b:
                stats = self._observed_p_b[b_id]
                obs_p_b = stats.mean if stats.count > 0 else 0.5

            residual_std = self._residual_std_for_b(b_id)
            norm_res = rel.residual / residual_std if residual_std > 0 else 0.0

            signals.append(
                Signal(
                    b_id=b_id,
                    implied_p_b=rel.implied_p_b,
                    observed_p_b=obs_p_b,
                    residual=rel.residual,
                    normalized_residual=norm_res,
                    relationship_count=1,
                    strength=abs(norm_res),
                )
            )
            seen_b.add(b_id)

        # Aggregate signals for same b_id from multiple relationships
        signal_map: dict[SignalId, list[Signal]] = {}
        for sig in signals:
            signal_map.setdefault(sig.b_id, []).append(sig)

        aggregated: list[Signal] = []
        for b_id, sigs in signal_map.items():
            total_strength = sum(s.strength for s in sigs)
            avg_implied = sum(s.implied_p_b for s in sigs) / len(sigs)
            avg_obs = sum(s.observed_p_b for s in sigs) / len(sigs)
            avg_residual = sum(s.residual for s in sigs) / len(sigs)
            avg_norm = sum(s.normalized_residual for s in sigs) / len(sigs)
            aggregated.append(
                Signal(
                    b_id=b_id,
                    implied_p_b=avg_implied,
                    observed_p_b=avg_obs,
                    residual=avg_residual,
                    normalized_residual=avg_norm,
                    relationship_count=len(sigs),
                    strength=total_strength,
                )
            )

        return sorted(aggregated, key=lambda s: -s.strength)

    def summary(self) -> GraphSummary:
        """Compute aggregate summary statistics."""
        relationships = self.compute_implied()
        if not relationships:
            return GraphSummary(
                total_relationships=0,
                active_signals=0,
                mean_abs_residual=0.0,
                max_abs_residual=0.0,
                mean_normalized_residual=0.0,
                residual_std=0.0,
                strong_signal_count=0,
            )

        residuals = [r.residual for r in relationships.values()]
        norm_residuals = [r.normalized_residual for r in relationships.values()]
        abs_residuals = [abs(r) for r in residuals]

        mean_abs = sum(abs_residuals) / len(abs_residuals)
        max_abs = max(abs_residuals)
        mean_norm = sum(norm_residuals) / len(norm_residuals)
        std_norm = (
            sum((n - mean_norm) ** 2 for n in norm_residuals) / len(norm_residuals)
        ) ** 0.5
        strong = sum(1 for n in norm_residuals if abs(n) > self._residual_threshold)

        signals = self.get_signals()
        return GraphSummary(
            total_relationships=len(relationships),
            active_signals=len(signals),
            mean_abs_residual=mean_abs,
            max_abs_residual=max_abs,
            mean_normalized_residual=mean_norm,
            residual_std=std_norm,
            strong_signal_count=strong,
        )

    # ── Internal ────────────────────────────────────────────────────────────

    def _residual_std_for_b(self, b_id: SignalId) -> float:
        """Compute std of residuals across all relationships targeting b_id."""
        residuals = []
        for (a, b), counts in self._relationships.items():
            if b != b_id or counts.total < self._min_samples:
                continue
            p_a = counts.a_true / counts.total
            p_b = counts.b_true / counts.total
            p_ba = counts.ab_true / counts.a_true if counts.a_true > 0 else 0.0
            not_a = counts.total - counts.a_true
            p_b_not_a = (counts.b_true - counts.ab_true) / not_a if not_a > 0 else 0.0
            implied = p_ba * p_a + p_b_not_a * (1.0 - p_a)
            residuals.append(implied - p_b)
        if len(residuals) < 2:
            return 1.0
        mean = sum(residuals) / len(residuals)
        var = sum((r - mean) ** 2 for r in residuals) / (len(residuals) - 1)
        return var ** 0.5


@dataclass
class _Counts:
    """Internal co-occurrence counters."""

    total: int = 0
    a_true: int = 0
    b_true: int = 0
    ab_true: int = 0


@dataclass
class _RunningStats:
    """Running mean and variance using Welford's algorithm."""

    count: int = 0
    mean: float = 0.0
    m2: float = 0.0

    def add(self, value: float) -> None:
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        delta2 = value - self.mean
        self.m2 += delta * delta2

    @property
    def variance(self) -> float:
        return self.m2 / self.count if self.count > 1 else 0.0

    @property
    def std(self) -> float:
        return self.variance ** 0.5
