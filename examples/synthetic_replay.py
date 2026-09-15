"""Synthetic mechanics demonstration. Output is NOT empirical strategy performance."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from polyalpha.backtest import Engine
from polyalpha.forecasting import Forecast
from polyalpha.storage import Store


class FixtureModel:
    def predict(self, market_id, book, at):
        return Forecast(market_id, at, D(".9"), D(".85"), D(".95"), "SYNTHETIC-fixture")


def main():
    at = datetime(2026, 1, 1, tzinfo=UTC)
    market = dict(
        id="fixture",
        conditionId="condition",
        question="Synthetic event",
        outcomes=["Yes", "No"],
        clobTokenIds=["yes", "no"],
        events=[{"id": "event"}],
        active=True,
        closed=False,
        acceptingOrders=True,
        enableOrderBook=True,
        liquidity="6000",
        volume="30000",
        endDate="2027-01-01T00:00:00Z",
        feesEnabled=True,
        feeSchedule={"rate": 0.04, "exponent": 1, "takerOnly": True},
    )
    with Store(":memory:") as store:
        store.append("market", "fixture", at, market)
        for delay in (0, 2):
            now = at + timedelta(seconds=delay)
            book = dict(
                asset_id="yes",
                market="condition",
                timestamp=str(int(now.timestamp() * 1000)),
                bids=[{"price": ".49", "size": "1000"}],
                asks=[{"price": ".51", "size": "1000"}],
                tick_size=".01",
                min_order_size="1",
                hash="SYNTHETIC",
            )
            store.append("book", "yes", now, book, now)
        engine = Engine(
            {"fixture": dict(event="event", cluster="synthetic-shared", category="fixture")},
            model=FixtureModel(),
        )
        report = engine.run(store.replay(at + timedelta(seconds=3)))
    report["synthetic_data"] = True
    path = Path("data/synthetic-demo.json")
    path.parent.mkdir(exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        json.dump(report, output, indent=2, default=str)
    print(json.dumps({"synthetic_demo": str(path), "mechanics_verified": report["fills"] == 1}))


if __name__ == "__main__":
    main()
