"""Point-in-time forecasters and explicit conservative probability intervals."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from .domain import Book

D = Decimal


@dataclass(frozen=True)
class Forecast:
    market_id: str
    timestamp: datetime
    probability: Decimal
    lower: Decimal
    upper: Decimal
    version: str

    def __post_init__(self):
        from .domain import utc

        utc(self.timestamp)
        if (
            any(not p.is_finite() for p in (self.lower, self.probability, self.upper))
            or not 0 <= self.lower <= self.probability <= self.upper <= 1
        ):
            raise ValueError("invalid probability bounds")

    def conservative(self, yes=True):
        return self.lower if yes else 1 - self.upper


class ProbabilityModel(Protocol):
    def predict(self, market_id: str, book: Book, at: datetime) -> Forecast: ...


def features(book: Book, levels=5):
    if levels < 1 or book.mid is None or book.spread <= 0:
        raise ValueError("unusable book")
    bids = sum((x.size for x in book.bids[:levels]), D(0))
    asks = sum((x.size for x in book.asks[:levels]), D(0))
    bid, ask = book.bids[0].size, book.asks[0].size
    return dict(
        midpoint=book.mid,
        spread=book.spread,
        relative_spread=book.spread / book.mid,
        imbalance=(bids - asks) / (bids + asks),
        microprice=(book.best_ask * bid + book.best_bid * ask) / (bid + ask),
    )


class Baseline:
    """Infrastructure diagnostic only. No claim of independent information."""

    def __init__(self, uncertainty=D(".05")):
        if not uncertainty.is_finite() or not D(".03") <= uncertainty <= 1:
            raise ValueError("baseline requires uncertainty >= 0.03")
        self.uncertainty = uncertainty
        self.history = {}

    def predict(self, market_id, book, at):
        if (
            book.received_at > at
            or book.source_at > at
            or (at - book.source_at).total_seconds() > 30
        ):
            raise ValueError("unavailable or stale features")
        f = features(book)
        from collections import deque

        history = self.history.setdefault(book.token_id, deque(maxlen=20))
        if history and at < history[-1][0]:
            raise ValueError("baseline observations must be chronological")
        momentum = f["midpoint"] - history[0][1] if history else D(0)
        if history and at == history[-1][0]:
            history[-1] = (at, f["midpoint"])
        else:
            history.append((at, f["midpoint"]))
        q = min(
            D(".99"),
            max(
                D(".01"),
                f["midpoint"] + D(".20") * momentum + D(".25") * f["spread"] * f["imbalance"],
            ),
        )
        return Forecast(
            market_id,
            at,
            q,
            max(D(0), q - self.uncertainty),
            min(D(1), q + self.uncertainty),
            "diagnostic-baseline-v1",
        )


def ensemble(forecasts, weights, at):
    if (
        not forecasts
        or len(forecasts) != len(weights)
        or sum(weights) != 1
        or any(w < 0 for w in weights)
    ):
        raise ValueError("invalid ensemble weights")
    if len({f.market_id for f in forecasts}) != 1 or any(f.timestamp > at for f in forecasts):
        raise ValueError("mismatched or future forecasts")
    q = sum((f.probability * w for f, w in zip(forecasts, weights)), D(0))
    # Enclose every component interval; disagreement widens the conservative bound.
    return Forecast(
        forecasts[0].market_id,
        at,
        q,
        min(f.lower for f in forecasts),
        max(f.upper for f in forecasts),
        "ensemble-v1",
    )


def net_edge(forecast, fill, yes=True, extra_slippage=D(".002"), resolution_penalty=D(".01")):
    if not fill.shares or fill.side != "BUY":
        raise ValueError("entry evaluation requires a buy fill")
    if extra_slippage < 0 or resolution_penalty < 0:
        raise ValueError("negative cost buffer")
    # fill.vwap already incorporates depth slippage. Do not subtract it twice.
    return (
        forecast.conservative(yes)
        - fill.vwap
        - fill.fees / fill.shares
        - extra_slippage
        - resolution_penalty
    )


class CalibratedModel:
    """Wrap a fitted calibrator; intervals remain conservative bands, not coverage claims."""

    def __init__(self, base, calibrator, buffer=D(".03")):
        if not buffer.is_finite() or buffer < 0:
            raise ValueError("invalid calibration buffer")
        self.base, self.calibrator, self.buffer = base, calibrator, buffer

    def predict(self, market_id, book, at):
        if self.calibrator.cutoff >= at:
            raise ValueError("calibration must be fitted before prediction")
        raw = self.base.predict(market_id, book, at)
        q = D(str(self.calibrator.predict(float(raw.probability))))
        lo = min(raw.lower, q - self.buffer)
        hi = max(raw.upper, q + self.buffer)
        return Forecast(market_id, at, q, max(D(0), lo), min(D(1), hi), "calibrated-" + raw.version)
