"""Tests for the scheduled-release source and the reaction measurement (A10)."""

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

sys.path.insert(0, "src")

from polyalpha.gov_releases import GovRelease, GovReleaseSource, load_gov_releases
from polyalpha.us.fills import FillRecord
from polyalpha.us.quote_race import Quote
from polyalpha.us.reaction import measure_reaction

T0 = datetime(2026, 10, 14, 12, 30, tzinfo=UTC)  # CPI


def _release():
    return GovRelease(
        event_id="cpi-2026-10-14",
        name="CPI",
        scheduled_at=T0,
        category="macro",
        source_url="https://www.bls.gov/schedule/2026/",
    )


def _fill(slug, price, side, t):
    return FillRecord(
        market_slug=slug, price=D(str(price)), size=D("10"),
        trade_time=t, received_at=t,
        maker_side="SELL" if side == "BUY" else "BUY", maker_intent=None,
        taker_side=side,
        taker_intent="ORDER_INTENT_BUY_LONG" if side == "BUY" else "ORDER_INTENT_SELL_LONG",
    )


def test_source_available_only_after_release():
    source = GovReleaseSource([_release()])
    assert source.available("cpi-2026-10-14", T0 - timedelta(seconds=1)) == []
    facts = source.available("cpi-2026-10-14", T0 + timedelta(seconds=1))
    assert len(facts) == 1
    assert facts[0].primary_source is True
    assert facts[0].source_authority == 1.0
    assert facts[0].published_at == T0


def test_source_upcoming_and_between():
    source = GovReleaseSource([_release()])
    assert [r.event_id for r in source.upcoming(T0 - timedelta(days=1))] == ["cpi-2026-10-14"]
    assert source.upcoming(T0 + timedelta(days=1)) == []
    assert len(source.releases_between(T0 - timedelta(minutes=1), T0 + timedelta(minutes=1))) == 1


def test_load_gov_releases_sorted():
    source = load_gov_releases("config/gov_releases.json")
    assert len(source.upcoming(datetime(2026, 9, 21, tzinfo=UTC))) == 7
    # Sorted by scheduled_at ascending.
    times = [r.scheduled_at for r in source._releases]
    assert times == sorted(times)


def test_measure_reaction_computes_r_and_race():
    release = _release()
    # Pre-event mid 0.50; post-event stable 0.58 (moves up after CPI).
    quotes = [
        Quote("m1", T0 - timedelta(seconds=60), D("0.48"), D("0.50")),
        Quote("m1", T0 + timedelta(seconds=10), D("0.53"), D("0.55")),
        Quote("m1", T0 + timedelta(seconds=300), D("0.56"), D("0.58")),
    ]
    fills = [_fill("m1", "0.50", "BUY", T0)]  # bought at the stale 0.50 ask
    result = measure_reaction(release, quotes, fills, horizons=(10, 300), stable_seconds=300)
    assert result["R"][10] is not None
    assert result["R"][300] == 1.0  # full incorporation at the stable horizon
    assert result["race"]["race_fills"] == 1
