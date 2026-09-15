"""Part 48 — Risk hidden tests.

Market cap at boundaries, event/cluster/category/total caps, daily loss limit,
max drawdown, multiple markets same event, cluster updates, equity falling,
negative cash, zero equity, near-zero equity, kill switch.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from polyalpha.portfolio import Portfolio, Position
from polyalpha.risk import Limits, Risk

D = Decimal
TZ = timezone.utc
TS = datetime(2025, 6, 1, tzinfo=TZ)


def _risk(equity=10000, limits=None):
    r = Risk(D(str(equity)), limits or Limits())
    return r


def _portfolio(cash=10000):
    return Portfolio(D(str(cash)))


def _pos(token="t1", shares=100, basis=50, market_id="m1",
         event_id="e1", cluster="cl1", category="cat1"):
    return Position(token, market_id, event_id, cluster, category,
                    D(str(shares)), D(str(basis)))


# ── Market cap at boundary ───────────────────────────────────────────────


class TestMarketCapBoundary:
    def test_budget_within_market_cap(self):
        r = _risk(equity=10000)
        p = _portfolio()
        m = _pos(shares=0, basis=0)
        p.positions["t1"] = m
        budget = r.budget(p, D(10000), "m1", "e1", "cl1", "cat1")
        assert budget > 0
        # Market cap = 10000 * 0.02 = 200
        assert budget <= D(200)

    def test_budget_at_market_cap(self):
        r = _risk(equity=10000)
        p = _portfolio()
        # Position already at market cap
        p.positions["t1"] = _pos(shares=200, basis=200, market_id="m1")
        budget = r.budget(p, D(10000), "m1", "e1", "cl1", "cat1")
        assert budget == D(0)

    def test_budget_exceeds_market_cap(self):
        r = _risk(equity=10000)
        p = _portfolio()
        p.positions["t1"] = _pos(shares=300, basis=300, market_id="m1")
        budget = r.budget(p, D(10000), "m1", "e1", "cl1", "cat1")
        assert budget == D(0)


# ── Event cap ────────────────────────────────────────────────────────────


class TestEventCap:
    def test_event_cap_limits_budget(self):
        r = _risk(equity=100000)
        p = _portfolio()
        # Two markets in same event, event cap = 100000 * 0.05 = 5000
        p.positions["t1"] = _pos(token="t1", shares=2500, basis=2500, event_id="e1", market_id="m1")
        p.positions["t2"] = _pos(token="t2", shares=2500, basis=2500, event_id="e1", market_id="m2")
        budget = r.budget(p, D(100000), "m3", "e1", "cl1", "cat1")
        assert budget == D(0)

    def test_different_event_not_limited(self):
        r = _risk(equity=100000)
        p = _portfolio()
        p.positions["t1"] = _pos(token="t1", shares=200, basis=200, event_id="e1")
        budget = r.budget(p, D(100000), "m2", "e2", "cl1", "cat1")
        assert budget > 0


# ── Cluster cap ──────────────────────────────────────────────────────────


class TestClusterCap:
    def test_cluster_cap_limits_budget(self):
        r = _risk(equity=10000)
        p = _portfolio()
        p.positions["t1"] = _pos(token="t1", shares=250, basis=250, cluster="cl1", market_id="m1")
        p.positions["t2"] = _pos(token="t2", shares=250, basis=250, cluster="cl1", market_id="m2")
        # Cluster cap = 10000 * 0.05 = 500
        budget = r.budget(p, D(10000), "m3", "e3", "cl1", "cat1")
        assert budget == D(0)

    def test_different_cluster_not_limited(self):
        r = _risk(equity=10000)
        p = _portfolio()
        p.positions["t1"] = _pos(token="t1", shares=250, basis=250, cluster="cl1")
        budget = r.budget(p, D(10000), "m2", "e2", "cl2", "cat1")
        assert budget > 0


# ── Category cap ─────────────────────────────────────────────────────────


class TestCategoryCap:
    def test_category_cap_limits_budget(self):
        r = _risk(equity=10000)
        p = _portfolio()
        # Category cap = 10000 * 0.10 = 1000
        p.positions["t1"] = _pos(token="t1", shares=500, basis=500, category="cat1", market_id="m1")
        p.positions["t2"] = _pos(token="t2", shares=500, basis=500, category="cat1", market_id="m2")
        budget = r.budget(p, D(10000), "m3", "e3", "cl3", "cat1")
        assert budget == D(0)


# ── Total cap ────────────────────────────────────────────────────────────


class TestTotalCap:
    def test_total_cap_limits_budget(self):
        r = _risk(equity=10000)
        p = _portfolio()
        # Total cap = 10000 * 0.20 = 2000
        p.positions["t1"] = _pos(token="t1", shares=1000, basis=1000, market_id="m1", event_id="e1", cluster="cl1", category="cat1")
        p.positions["t2"] = _pos(token="t2", shares=1000, basis=1000, market_id="m2", event_id="e2", cluster="cl2", category="cat2")
        budget = r.budget(p, D(10000), "m3", "e3", "cl3", "cat3")
        assert budget == D(0)


# ── Daily loss limit ─────────────────────────────────────────────────────


class TestDailyLossLimit:
    def test_daily_loss_halts(self):
        r = _risk(equity=10000)
        # Simulate equity drop > 2% daily loss limit
        r.observe(D(10000), TS)
        r.observe(D(9700), TS + timedelta(hours=1))
        assert r.halted
        assert r.reason == "daily_loss"

    def test_daily_loss_boundary_not_halted(self):
        r = _risk(equity=10000)
        r.observe(D(10000), TS)
        r.observe(D(9801), TS + timedelta(hours=1))
        assert not r.halted

    def test_daily_loss_exact_boundary_halted(self):
        r = _risk(equity=10000)
        r.observe(D(10000), TS)
        r.observe(D(9800), TS + timedelta(hours=1))
        assert r.halted


# ── Max drawdown ─────────────────────────────────────────────────────────


class TestMaxDrawdown:
    def test_drawdown_halts(self):
        r = _risk(equity=10000)
        r.observe(D(10000), TS)
        r.observe(D(9100), TS + timedelta(hours=1))
        # Drawdown = 9% > 8% limit
        assert r.halted
        assert r.reason == "drawdown"

    def test_drawdown_boundary_not_halted(self):
        r = _risk(equity=10000)
        r.observe(D(10000), TS)
        # New day: reset day_start so daily_loss doesn't trigger
        r.observe(D(9300), TS + timedelta(days=1))
        # Now drawdown from peak(10000) to 9205 is 7.95% < 8%
        # Daily loss from day_start(9300) to 9205 is 1.02% < 2%
        r.observe(D(9205), TS + timedelta(days=1, hours=1))
        assert not r.halted

    def test_drawdown_exact_boundary_halted(self):
        r = _risk(equity=10000)
        r.observe(D(10000), TS)
        r.observe(D(9200), TS + timedelta(hours=1))
        assert r.halted


# ── Multiple markets same event ──────────────────────────────────────────


class TestMultipleMarketsSameEvent:
    def test_budget_shared_across_event(self):
        r = _risk(equity=100000)
        p = _portfolio()
        # Event cap = 100000 * 0.05 = 5000. Position cost = 5000 fills the cap.
        p.positions["t1"] = _pos(token="t1", shares=5000, basis=5000, event_id="e1", market_id="m1")
        budget = r.budget(p, D(100000), "m2", "e1", "cl1", "cat1")
        assert budget == D(0)

    def test_different_events_independent(self):
        r = _risk(equity=100000)
        p = _portfolio()
        p.positions["t1"] = _pos(token="t1", shares=200, basis=200, event_id="e1", market_id="m1")
        budget = r.budget(p, D(100000), "m2", "e2", "cl1", "cat1")
        assert budget > 0


# ── Equity falling causing caps to shrink ────────────────────────────────


class TestEquityFallingCapsShrink:
    def test_budget_decreases_with_equity(self):
        r = _risk(equity=10000)
        p = _portfolio()
        budget_high = r.budget(p, D(10000), "m1", "e1", "cl1", "cat1")
        budget_low = r.budget(p, D(5000), "m1", "e1", "cl1", "cat1")
        assert budget_low < budget_high

    def test_budget_ratio_proportional(self):
        r = _risk(equity=10000)
        p = _portfolio()
        b1 = r.budget(p, D(10000), "m1", "e1", "cl1", "cat1")
        b2 = r.budget(p, D(5000), "m1", "e1", "cl1", "cat1")
        assert b2 == b1 / 2


# ── Negative cash / zero equity ──────────────────────────────────────────


class TestZeroNegativeEquity:
    def test_zero_equity_halted(self):
        r = _risk(equity=10000)
        r.observe(D(0), TS)
        assert r.halted
        assert r.reason == "drawdown"

    def test_negative_equity_halted(self):
        r = _risk(equity=10000)
        r.observe(D(-100), TS)
        assert r.halted

    def test_zero_equity_budget_zero(self):
        r = _risk(equity=10000)
        p = _portfolio()
        budget = r.budget(p, D(0), "m1", "e1", "cl1", "cat1")
        assert budget == D(0)


# ── Kill switch behavior ─────────────────────────────────────────────────


class TestKillSwitch:
    def test_halted_returns_zero_budget(self):
        r = _risk(equity=10000)
        r.halted = True
        r.reason = "manual"
        p = _portfolio()
        budget = r.budget(p, D(10000), "m1", "e1", "cl1", "cat1")
        assert budget == D(0)

    def test_halted_stays_halted(self):
        r = _risk(equity=10000)
        r.observe(D(0), TS)
        assert r.halted
        r.observe(D(10000), TS + timedelta(hours=1))
        assert r.halted


# ── Risk limits validation ───────────────────────────────────────────────


class TestLimitsValidation:
    def test_invalid_limit_rejected(self):
        with pytest.raises(ValueError, match="invalid risk fraction"):
            Limits(normal=D(0))

    def test_limit_above_one_rejected(self):
        with pytest.raises(ValueError, match="invalid risk fraction"):
            Limits(normal=D(1.5))

    def test_negative_limit_rejected(self):
        with pytest.raises(ValueError, match="invalid risk fraction"):
            Limits(daily_loss=D(-0.01))


# ── Budget never negative ────────────────────────────────────────────────


class TestBudgetNonNegative:
    def test_budget_floor_zero(self):
        r = _risk(equity=10000)
        p = _portfolio()
        p.positions["t1"] = _pos(token="t1", shares=5000, basis=5000, market_id="m1")
        budget = r.budget(p, D(10000), "m1", "e1", "cl1", "cat1")
        assert budget >= D(0)


# ── Normal position within limits ────────────────────────────────────────


class TestNormalPosition:
    def test_normal_cap(self):
        r = _risk(equity=10000)
        p = _portfolio()
        budget = r.budget(p, D(10000), "m1", "e1", "cl1", "cat1")
        # Normal cap = 10000 * 0.005 = 50
        assert budget == D(50)

    def test_normal_cap_with_existing_position(self):
        r = _risk(equity=10000)
        p = _portfolio()
        p.positions["t1"] = _pos(token="t1", shares=30, basis=30, market_id="m1")
        # Normal cap = equity * 0.005 = 50 (not reduced by existing positions)
        # Other caps are reduced by exposures: market=170, event=470, cluster=470, category=970, total=1970
        # min(10000, 50, 1970, 170, 470, 470, 970) = 50
        budget = r.budget(p, D(10000), "m1", "e1", "cl1", "cat1")
        assert budget == D(50)


# ── Peak tracking ────────────────────────────────────────────────────────


class TestPeakTracking:
    def test_peak_updates_on_new_high(self):
        r = _risk(equity=10000)
        r.observe(D(12000), TS)
        assert r.peak == D(12000)

    def test_peak_does_not_decrease(self):
        r = _risk(equity=10000)
        r.observe(D(12000), TS)
        r.observe(D(11000), TS + timedelta(hours=1))
        assert r.peak == D(12000)
