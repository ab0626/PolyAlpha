"""Information-incorporation primitives: log-odds space and the reaction curve.

The Information Incorporation Function measures how quickly a market absorbs a
timestamped information event:

    R_{e,m}(h) = (P(t_e + h) - P(t_e^-)) / (P(stable) - P(t_e^-))

in log-odds (logit) space, so a move from 0.02->0.07 is comparable to a move
from 0.50->0.55. Pure functions; offline-testable.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from decimal import Decimal

D = Decimal


def logit(p: float | Decimal) -> float:
    """Log-odds transform: logit(p) = log(p / (1 - p)), clamped away from 0/1."""
    x = float(p)
    if not 0.0 < x < 1.0:
        raise ValueError(f"logit undefined at p={p!r}")
    return math.log(x / (1.0 - x))


def logit_delta(p0: float | Decimal, p1: float | Decimal) -> float:
    """Change in log-odds: logit(p1) - logit(p0)."""
    return logit(p1) - logit(p0)


def incorporation_curve(
    series: list[tuple[datetime, Decimal]],
    event_time: datetime,
    horizons_seconds: list[int],
    stable_seconds: int,
) -> dict[int, float | None]:
    """Normalized information-incorporation curve R(h) in log-odds space.

    ``series`` is a time-ordered list of (timestamp, price). ``event_time`` is
    the public information time t_e; ``stable_seconds`` is the horizon at which
    the eventual move is measured. Returns R(h) per horizon, or None when the
    pre-event or stable price is missing/unchanged.
    """
    ordered = sorted(series, key=lambda ts_p: ts_p[0])

    def price_at(t: datetime) -> Decimal | None:
        last = None
        for ts, p in ordered:
            if ts <= t:
                last = p
            else:
                break
        return last

    p_pre = price_at(event_time)
    p_stable = price_at(event_time + timedelta(seconds=stable_seconds))
    if p_pre is None or p_stable is None:
        return {h: None for h in horizons_seconds}

    l_pre = logit(p_pre)
    l_stable = logit(p_stable)
    denom = l_stable - l_pre
    if abs(denom) < 1e-12:
        return {h: 0.0 for h in horizons_seconds}

    out: dict[int, float | None] = {}
    for h in horizons_seconds:
        p_h = price_at(event_time + timedelta(seconds=h))
        if p_h is None:
            out[h] = None
        else:
            out[h] = (logit(p_h) - l_pre) / denom
    return out
