"""Correlation analysis and event clustering for risk management.

Computes pairwise price correlations between markets and provides
clustering algorithms for grouping correlated positions.
Supports both static and rolling (dynamic) correlation matrices.
"""

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

D = Decimal


@dataclass
class CorrelationMatrix:
    """Pairwise correlation matrix between market tokens.

    Stores correlation coefficients and provides queries for
    risk-aware position management.
    """

    tokens: list[str]
    matrix: dict[tuple[str, str], Decimal] = field(default_factory=dict)

    def __post_init__(self):
        # Ensure diagonal is 1.0
        for t in self.tokens:
            self.matrix[(t, t)] = D(1)

    def get(self, token_a: str, token_b: str) -> Decimal:
        """Get correlation between two tokens. Returns 0 if unknown."""
        return self.matrix.get((token_a, token_b), D(0))

    def set(self, token_a: str, token_b: str, correlation: Decimal):
        """Set symmetric correlation."""
        if not (-1 <= correlation <= 1):
            raise ValueError("correlation must be in [-1, 1]")
        self.matrix[(token_a, token_b)] = correlation
        self.matrix[(token_b, token_a)] = correlation

    def highly_correlated(self, threshold: Decimal = D("0.7")) -> list[tuple[str, str, Decimal]]:
        """Return pairs with correlation above threshold."""
        pairs = []
        seen = set()
        for (a, b), corr in self.matrix.items():
            if a != b and corr >= threshold and (a, b) not in seen and (b, a) not in seen:
                pairs.append((a, b, corr))
                seen.add((a, b))
        return sorted(pairs, key=lambda x: -abs(x[2]))


def compute_correlation(
    prices_a: list[Decimal],
    prices_b: list[Decimal],
) -> Decimal:
    """Compute Pearson correlation between two price series.

    Returns correlation coefficient in [-1, 1].
    Requires at least 3 observations.
    """
    n = min(len(prices_a), len(prices_b))
    if n < 3:
        return D(0)

    a = prices_a[:n]
    b = prices_b[:n]

    # Compute returns
    returns_a = [(a[i] - a[i - 1]) / a[i - 1] if a[i - 1] != 0 else D(0) for i in range(1, n)]
    returns_b = [(b[i] - b[i - 1]) / b[i - 1] if b[i - 1] != 0 else D(0) for i in range(1, n)]

    m = len(returns_a)
    if m < 2:
        return D(0)

    mean_ra = sum(returns_a) / m
    mean_rb = sum(returns_b) / m

    cov = sum((ra - mean_ra) * (rb - mean_rb) for ra, rb in zip(returns_a, returns_b)) / m
    std_a = math.sqrt(sum((ra - mean_ra) ** 2 for ra in returns_a) / m)
    std_b = math.sqrt(sum((rb - mean_rb) ** 2 for rb in returns_b) / m)

    if std_a == 0 or std_b == 0:
        return D(0)

    corr = cov / (D(str(std_a)) * D(str(std_b)))
    # Clamp to [-1, 1]
    return max(D(-1), min(D(1), corr))


def rolling_correlation(
    prices_a: list[Decimal],
    prices_b: list[Decimal],
    window: int = 20,
) -> list[Decimal]:
    """Compute rolling pairwise correlation."""
    result = []
    n = min(len(prices_a), len(prices_b))
    for i in range(window, n + 1):
        corr = compute_correlation(prices_a[i - window : i], prices_b[i - window : i])
        result.append(corr)
    return result


@dataclass
class EventCluster:
    """A cluster of correlated markets belonging to the same event."""

    cluster_id: str
    event_id: str
    category: str
    market_ids: list[str]
    token_ids: list[str]
    correlation_matrix: CorrelationMatrix | None = None

    @property
    def size(self) -> int:
        return len(self.market_ids)

    @property
    def avg_internal_correlation(self) -> Decimal:
        """Average pairwise correlation within the cluster."""
        if self.correlation_matrix is None or len(self.token_ids) < 2:
            return D(0)
        total = D(0)
        count = 0
        for i, a in enumerate(self.token_ids):
            for b in self.token_ids[i + 1 :]:
                total += abs(self.correlation_matrix.get(a, b))
                count += 1
        return total / count if count > 0 else D(0)


@dataclass
class ClusterRegistry:
    """Registry of all event clusters with correlation tracking."""

    clusters: dict[str, EventCluster] = field(default_factory=dict)
    _market_to_cluster: dict[str, str] = field(default_factory=dict)

    def register(self, cluster: EventCluster):
        """Register a cluster and update lookup indices."""
        self.clusters[cluster.cluster_id] = cluster
        for mid in cluster.market_ids:
            self._market_to_cluster[mid] = cluster.cluster_id

    def get_cluster(self, market_id: str) -> EventCluster | None:
        """Get cluster for a market."""
        cid = self._market_to_cluster.get(market_id)
        return self.clusters.get(cid) if cid else None

    def cluster_exposure(
        self,
        positions: dict[str, Decimal],  # token_id -> notional
    ) -> dict[str, Decimal]:
        """Compute total notional exposure per cluster."""
        exposure: dict[str, Decimal] = defaultdict(lambda: D(0))
        for token_id, notional in positions.items():
            # Find which cluster this token belongs to
            for cluster in self.clusters.values():
                if token_id in cluster.token_ids:
                    exposure[cluster.cluster_id] += notional
                    break
        return dict(exposure)

    def correlation_adjusted_exposure(
        self,
        positions: dict[str, Decimal],
    ) -> dict[str, Decimal]:
        """Compute correlation-adjusted exposure for each cluster.

        For positively correlated positions, the effective risk is higher
        than the sum of individual positions.
        """
        raw = self.cluster_exposure(positions)
        adjusted = {}
        for cid, total_exposure in raw.items():
            cluster = self.clusters.get(cid)
            if cluster is None or cluster.correlation_matrix is None:
                adjusted[cid] = total_exposure
                continue

            # Simple approximation: scale by average correlation
            avg_corr = cluster.avg_internal_correlation
            # If avg correlation is 0.5, effective exposure is 1.25x raw
            scale = D(1) + avg_corr / D(2)
            adjusted[cid] = (total_exposure * scale).quantize(D("0.01"))
        return adjusted


class RollingCorrelationTracker:
    """Dynamic correlation tracker that maintains rolling windows of price returns.

    As new price observations arrive, updates the correlation matrix using
    a configurable lookback window. This captures time-varying correlations
    that static matrices miss — e.g., correlations increase during market stress.

    Section 19: cluster creation from historical return correlation.
    """

    def __init__(
        self,
        tokens: list[str],
        window: int = 50,
        min_observations: int = 10,
        correlation_threshold: Decimal = D("0.7"),
    ):
        if window < 5:
            raise ValueError("window must be >= 5")
        if min_observations < 3:
            raise ValueError("min_observations must be >= 3")
        self.tokens = list(tokens)
        self.window = window
        self.min_observations = min_observations
        self.correlation_threshold = correlation_threshold
        self._price_history: dict[str, deque[Decimal]] = {
            t: deque(maxlen=window + 1) for t in tokens
        }
        self._timestamps: deque[datetime] = deque(maxlen=window + 1)
        self._matrix = CorrelationMatrix(tokens=tokens)
        self._last_update: datetime | None = None

    def update(self, prices: dict[str, Decimal], at: datetime):
        """Add a new price observation and recompute correlations.

        Args:
            prices: token_id -> current price (midpoint or last trade)
            at: timestamp of the observation
        """
        self._timestamps.append(at)
        for token, price in prices.items():
            if token in self._price_history:
                self._price_history[token].append(price)

        self._last_update = at

        # Recompute correlations if we have enough data
        n = min(len(ts) for ts in self._price_history.values() if ts) if self._price_history else 0
        if n >= self.min_observations:
            self._recompute()

    def _recompute(self):
        """Recompute the full correlation matrix from rolling windows."""
        for i, a in enumerate(self.tokens):
            for b in self.tokens[i + 1 :]:
                prices_a = list(self._price_history[a])
                prices_b = list(self._price_history[b])
                if (
                    len(prices_a) >= self.min_observations
                    and len(prices_b) >= self.min_observations
                ):
                    corr = compute_correlation(prices_a, prices_b)
                    self._matrix.set(a, b, corr)

    @property
    def matrix(self) -> CorrelationMatrix:
        return self._matrix

    def get_correlation(self, token_a: str, token_b: str) -> Decimal:
        """Get current rolling correlation between two tokens."""
        return self._matrix.get(token_a, token_b)

    def highly_correlated_pairs(self) -> list[tuple[str, str, Decimal]]:
        """Return pairs currently above the correlation threshold."""
        return self._matrix.highly_correlated(self.correlation_threshold)

    def get_dynamic_clusters(self) -> list[list[str]]:
        """Find clusters of highly correlated tokens using Union-Find.

        Returns lists of token IDs that are mutually correlated above threshold.
        """
        pairs = self.highly_correlated_pairs()
        if not pairs:
            return []

        # Union-Find
        parent = {t: t for t in self.tokens}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x, y):
            px, py = find(x), find(y)
            if px != py:
                parent[px] = py

        for a, b, _ in pairs:
            union(a, b)

        # Group by root
        groups: dict[str, list[str]] = defaultdict(list)
        for t in self.tokens:
            groups[find(t)].append(t)

        return [g for g in groups.values() if len(g) >= 2]

    def summary(self) -> dict:
        """Return a summary of current correlation state."""
        pairs = self.highly_correlated_pairs()
        all_corrs = []
        for i, a in enumerate(self.tokens):
            for b in self.tokens[i + 1 :]:
                c = self._matrix.get(a, b)
                if c != 0:
                    all_corrs.append(float(c))

        return {
            "tokens": len(self.tokens),
            "observations": min(len(ts) for ts in self._price_history.values() if ts)
            if self._price_history
            else 0,
            "highly_correlated_pairs": len(pairs),
            "mean_abs_correlation": sum(abs(c) for c in all_corrs) / len(all_corrs)
            if all_corrs
            else 0,
            "max_correlation": max(all_corrs) if all_corrs else 0,
            "dynamic_clusters": len(self.get_dynamic_clusters()),
            "last_update": self._last_update.isoformat() if self._last_update else None,
        }
