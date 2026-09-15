"""Anomaly detection for market microstructure and external data.

Anomalies should trigger increased uncertainty, not automatic trade signals.
"""

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .domain import Book

D = Decimal


@dataclass(frozen=True)
class Anomaly:
    """Detected anomaly in market data."""

    kind: str
    severity: Decimal  # 0-1
    description: str
    features: dict[str, Any]


class AnomalyDetector:
    """Detect anomalies in order-book and market data.

    Maintains rolling statistics and flags observations that deviate
    significantly from recent history.
    """

    def __init__(
        self,
        spread_window: int = 50,
        volume_window: int = 50,
        price_window: int = 50,
        imbalance_threshold: Decimal = D("0.8"),
        spread_z_threshold: Decimal = D("3.0"),
        volume_z_threshold: Decimal = D("3.0"),
        price_gap_threshold: Decimal = D("0.03"),
    ):
        self.spread_history: deque[Decimal] = deque(maxlen=spread_window)
        self.volume_history: deque[Decimal] = deque(maxlen=volume_window)
        self.price_history: deque[Decimal] = deque(maxlen=price_window)
        self.imbalance_threshold = imbalance_threshold
        self.spread_z_threshold = spread_z_threshold
        self.volume_z_threshold = volume_z_threshold
        self.price_gap_threshold = price_gap_threshold

    def _z_score(self, value: Decimal, history: deque) -> Decimal | None:
        """Compute z-score against rolling history."""
        if len(history) < 10:
            return None
        values = list(history)
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        std = variance.sqrt()
        if std == 0:
            return D(0)
        return (value - mean) / std

    def detect(self, book: Book, features: dict) -> list[Anomaly]:
        """Detect anomalies in current book state and features."""
        anomalies = []

        # Spread anomaly
        if book.spread is not None:
            z = self._z_score(book.spread, self.spread_history)
            self.spread_history.append(book.spread)
            if z is not None and abs(z) > self.spread_z_threshold:
                anomalies.append(
                    Anomaly(
                        kind="spread_widening",
                        severity=min(D(1), abs(z) / D("5")),
                        description=(
                            f"Spread {book.spread} is {abs(z):.1f}"
                            " std devs from recent mean"
                        ),
                        features={"spread": str(book.spread), "z_score": str(z)},
                    )
                )

        # Liquidity disappearance
        total_bid = sum((x.size for x in book.bids[:5]), D(0))
        total_ask = sum((x.size for x in book.asks[:5]), D(0))
        if total_bid + total_ask > 0:
            ratio = abs(total_bid - total_ask) / (total_bid + total_ask)
            if ratio > self.imbalance_threshold:
                anomalies.append(
                    Anomaly(
                        kind="liquidity_imbalance",
                        severity=ratio,
                        description=f"Severe order-book imbalance: {ratio:.2f}",
                        features={"imbalance": str(ratio)},
                    )
                )

        # Zero depth on one side
        if not book.bids:
            anomalies.append(
                Anomaly(
                    kind="no_bids",
                    severity=D("0.9"),
                    description="No bids in order book",
                    features={},
                )
            )
        if not book.asks:
            anomalies.append(
                Anomaly(
                    kind="no_asks",
                    severity=D("0.9"),
                    description="No asks in order book",
                    features={},
                )
            )

        # Price jump detection (compare microprice to mid)
        microprice = features.get("microprice")
        midpoint = features.get("midpoint")
        if microprice is not None and midpoint is not None and midpoint > 0:
            deviation = abs(microprice - midpoint) / midpoint
            if deviation > D("0.02"):  # >2% deviation
                anomalies.append(
                    Anomaly(
                        kind="price_imbalance",
                        severity=min(D(1), deviation * D(10)),
                        description=f"Microprice deviates from midpoint by {deviation:.3%}",
                        features={"deviation": str(deviation)},
                    )
                )

        # Price gap detection (inter-snapshot jump)
        if book.best_ask is not None:
            prev_price = self.price_history[-1] if self.price_history else None
            self.price_history.append(book.best_ask)
            if prev_price is not None and prev_price > 0:
                gap = abs(book.best_ask - prev_price) / prev_price
                if gap > self.price_gap_threshold:
                    anomalies.append(
                        Anomaly(
                            kind="price_gap",
                            severity=min(D(1), gap * D(5)),
                            description=f"Price gap of {gap:.3%} between snapshots",
                            features={
                                "previous_price": str(prev_price),
                                "current_price": str(book.best_ask),
                                "gap": str(gap),
                            },
                        )
                    )

        # Volume spike detection
        total_depth = total_bid + total_ask
        if total_depth > 0:
            z = self._z_score(total_depth, self.volume_history)
            self.volume_history.append(total_depth)
            if z is not None and z > self.volume_z_threshold:
                anomalies.append(
                    Anomaly(
                        kind="volume_spike",
                        severity=min(D(1), z / D("5")),
                        description=f"Unusual volume depth: {total_depth} ({z:.1f} std devs)",
                        features={"total_depth": str(total_depth), "z_score": str(z)},
                    )
                )
        else:
            self.volume_history.append(D(0))

        return anomalies

    def uncertainty_multiplier(self, anomalies: list[Anomaly]) -> Decimal:
        """Compute uncertainty multiplier based on detected anomalies.

        Returns a multiplier >= 1.0 that widens uncertainty bounds.
        """
        if not anomalies:
            return D(1)
        max_severity = max(a.severity for a in anomalies)
        # Linear scaling: severity 0 -> 1x, severity 1 -> 2x
        return D(1) + max_severity
