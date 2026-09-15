import unittest
from datetime import timedelta
from decimal import Decimal as D

from test_foundation import NOW, book, market

from polyalpha.backtest import Engine
from polyalpha.calibration import (
    Isotonic,
    Observation,
    Platt,
    metrics,
    training_rows,
    walk_forward,
)
from polyalpha.forecasting import Forecast
from polyalpha.storage import Store
from polyalpha.stream import Reconstructor


class StreamTests(unittest.TestCase):
    def test_delta_replace_delete_disconnect(self):
        state = Reconstructor({"y": ("c", D(".01"), D(1))})
        event = book()
        event["event_type"] = "book"
        state.apply(event, NOW)
        delta = dict(
            event_type="price_change",
            market="c",
            timestamp=event["timestamp"],
            price_changes=[dict(asset_id="y", price=".51", size="50", side="SELL")],
        )
        state.apply(delta, NOW)
        self.assertEqual(state.books["y"].asks[0].size, D(50))
        delta["price_changes"][0]["size"] = "0"
        state.apply(delta, NOW)
        self.assertEqual(state.books["y"].best_ask, D(".52"))
        state.invalidate()
        with self.assertRaises(ValueError):
            state.apply(delta, NOW)
        self.assertFalse(state.books)

    def test_mismatch_invalidates(self):
        state = Reconstructor({"y": ("c", D(".01"), D(1))})
        state.apply(dict(book(), event_type="book"), NOW)
        with self.assertRaises(ValueError):
            state.apply(
                dict(
                    event_type="price_change",
                    market="c",
                    timestamp=book()["timestamp"],
                    price_changes=[
                        dict(
                            asset_id="y",
                            price=".51",
                            size="10",
                            side="SELL",
                            best_ask=".6",
                        )
                    ],
                ),
                NOW,
            )
        self.assertFalse(state.books)


def rows():
    return [
        Observation(str(i), str(i), NOW - timedelta(days=10), NOW - timedelta(days=1), p, y)
        for i, (p, y) in enumerate([(0.1, 0), (0.2, 0), (0.3, 1), (0.4, 0), (0.7, 1), (0.9, 1)])
    ]


class CalibrationTests(unittest.TestCase):
    def test_scores_and_edge_buckets(self):
        score = metrics([0, 1], [0, 1])
        self.assertEqual(score["brier"], 0)
        self.assertEqual(score["reliability"][-1]["count"], 1)
        self.assertEqual(score["ece"], 0)

    def test_monotonic_and_platt(self):
        for model in (Isotonic(), Platt()):
            model.fit(rows(), NOW)
            values = [model.predict(i / 10) for i in range(11)]
            self.assertEqual(values, sorted(values))
            self.assertTrue(all(0 <= v <= 1 for v in values))

    def test_future_labels_excluded_and_duplicates(self):
        extra = Observation(
            "future", "future", NOW - timedelta(days=2), NOW + timedelta(days=1), 0.9, 1
        )
        selected = training_rows(rows() + rows() + [extra], NOW)
        self.assertEqual(len(selected), 6)
        self.assertNotIn("future", [r.market_id for r in selected])
        self.assertNotIn("0", [r.market_id for r in training_rows(rows(), NOW, ["0"])])

    def test_walk_forward(self):
        future = Observation(
            "test", "test", NOW + timedelta(hours=1), NOW + timedelta(days=1), 0.8, 1
        )
        folds = walk_forward(rows() + [future], NOW, NOW + timedelta(days=2), timedelta(days=2))
        self.assertEqual(folds[0]["status"], "evaluated")
        self.assertEqual(folds[0]["metrics"]["sample_size"], 1)


class CertainFixtureModel:
    def predict(self, market_id, book, at):
        return Forecast(market_id, at, D(".9"), D(".85"), D(".95"), "synthetic-only")


class ReplayTests(unittest.TestCase):
    def test_latency_entry_then_settlement(self):
        with Store(":memory:") as store:
            m = market()
            m["feesEnabled"] = False
            store.append("market", "m", NOW, m)
            store.append("book", "y", NOW, book(), NOW)
            later = NOW + timedelta(seconds=2)
            store.append("book", "y", later, book(source=later), later)
            settled = later + timedelta(seconds=1)
            store.append(
                "settlement",
                "y",
                settled,
                dict(
                    token_id="y",
                    payout="1",
                    known_at=settled.isoformat(),
                    source_url="fixture://resolution",
                    verified=True,
                ),
            )
            engine = Engine(
                {"m": dict(event="e", cluster="shared", category="test")},
                model=CertainFixtureModel(),
            )
            result = engine.run(store.replay(settled))
            self.assertEqual(result["fills"], 1)
            self.assertEqual(engine.simulator.orders["paper-1"].filled_at, later)
            self.assertEqual(
                engine.portfolio.cash - engine.portfolio.initial_cash,
                engine.portfolio.realized,
            )

    def test_no_arrival_no_fill_and_missing_cluster(self):
        for clusters in ({}, {"m": dict(event="e", cluster="c", category="test")}):
            with Store(":memory:") as store:
                store.append("market", "m", NOW, dict(market(), feesEnabled=False))
                store.append("book", "y", NOW, book(), NOW)
                engine = Engine(clusters, model=CertainFixtureModel())
                report = engine.run(store.replay(NOW))
                self.assertEqual(report["fills"], 0)

    def test_unknown_fees_reject(self):
        with Store(":memory:") as store:
            store.append("market", "m", NOW, market())
            store.append("book", "y", NOW, book(), NOW)
            report = Engine({"m": dict(event="e", cluster="c", category="test")}).run(
                store.replay(NOW)
            )
            self.assertIn("unknown_fees", report["decisions"][0]["rejections"])
