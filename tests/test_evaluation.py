import csv
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from test_foundation import NOW
from test_research import rows

from polyalpha.calibration import Isotonic, Observation, canonical_rows, walk_forward
from polyalpha.comparison import summarize_folds
from polyalpha.dataset import export_forecasts


class EvaluationTests(unittest.TestCase):
    def test_market_is_not_recounted_in_later_fold(self):
        samples = rows() + [
            Observation("test", "test", NOW + timedelta(days=1), NOW + timedelta(days=50), 0.7, 1),
            Observation("test", "test", NOW + timedelta(days=31), NOW + timedelta(days=50), 0.8, 1),
        ]
        folds = walk_forward(samples, NOW, NOW + timedelta(days=60))
        self.assertEqual(sum(f["candidate_markets"] for f in folds), 1)
        self.assertEqual(summarize_folds(folds)["scored_markets"], 1)

    def test_unresolved_cluster_is_purged_and_reported(self):
        samples = rows() + [Observation("pending", "0", NOW + timedelta(hours=1), None, 0.5, None)]
        fold = walk_forward(samples, NOW, NOW + timedelta(days=1))[0]
        self.assertEqual(fold["status"], "awaiting_labels")
        self.assertEqual(fold["training_markets"], 5)
        self.assertEqual(fold["unresolved_markets"], 1)
        self.assertIsNone(fold["prediction_ledger"][0]["outcome"])

    def test_later_label_enriches_earliest_forecast_without_backdating(self):
        samples = [
            Observation("m", "c", NOW, None, 0.3, None),
            Observation("m", "c", NOW + timedelta(days=1), NOW + timedelta(days=2), 0.4, 1),
        ]
        canonical = canonical_rows(samples)[0]
        self.assertEqual(canonical.probability, 0.3)
        self.assertEqual(canonical.label_known_at, NOW + timedelta(days=2))

    def test_conflicting_labels_are_not_silently_reduced(self):
        samples = [
            Observation("m", "c", NOW, NOW + timedelta(days=2), 0.3, 0),
            Observation("m", "c", NOW + timedelta(days=1), NOW + timedelta(days=2), 0.4, 1),
        ]
        with self.assertRaisesRegex(ValueError, "conflicting market outcomes"):
            canonical_rows(samples)

    def test_calibrator_error_is_not_mislabeled_as_insufficient_data(self):
        samples = rows() + [
            Observation("test", "test", NOW + timedelta(hours=1), NOW + timedelta(hours=2), 0.7, 1)
        ]
        with patch.object(Isotonic, "fit", side_effect=ValueError("unexpected solver failure")):
            with self.assertRaisesRegex(ValueError, "unexpected solver"):
                walk_forward(samples, NOW, NOW + timedelta(days=1))

    def test_cluster_resampling_is_reproducible(self):
        ledger = [
            dict(
                market_id=str(i),
                cluster=str(i),
                raw_probability=0.4,
                calibrated_probability=0.6,
                outcome=1,
            )
            for i in range(20)
        ]
        folds = [
            dict(
                candidate_markets=20,
                unresolved_markets=0,
                status="evaluated",
                prediction_ledger=ledger,
            )
        ]
        first = summarize_folds(folds, bootstrap_samples=100)
        self.assertEqual(first, summarize_folds(folds, bootstrap_samples=100))
        self.assertLess(first["cluster_bootstrap"]["upper"], 0)

    def test_forecast_export_preserves_unresolved_rows(self):
        report = dict(
            forecasts=[dict(market_id="m", cluster="c", at=NOW.isoformat(), p=".5")],
            outcome_labels={},
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "forecasts.csv"
            export_forecasts(report, path)
            with path.open(newline="") as source:
                row = next(csv.DictReader(source))
            self.assertEqual(row["outcome"], "")
            self.assertEqual(row["label_known_at"], "")
            with self.assertRaises(FileExistsError):
                export_forecasts(report, path)

    def test_forecasts_exist_without_trading_reviews_or_fees(self):
        from test_foundation import book, market

        from polyalpha.forecast_runner import ForecastRecorder
        from polyalpha.quality import Filter
        from polyalpha.storage import Store

        with Store(":memory:") as store:
            store.append("market", "m", NOW, market())
            store.append("book", "y", NOW, book(), NOW)
            report = ForecastRecorder(Filter()).run(store.replay(NOW))
            repeated = ForecastRecorder(Filter()).run(store.replay(NOW))
            self.assertEqual(report["input_sha256"], repeated["input_sha256"])
            store.append("audit", "m", NOW, {"reason": "additional receipt"})
            changed = ForecastRecorder(Filter()).run(store.replay(NOW))
            self.assertNotEqual(report["input_sha256"], changed["input_sha256"])
            self.assertEqual(report["input_records"], 2)
        self.assertEqual(len(report["forecasts"]), 1)
        self.assertEqual(report["forecasts"][0]["cluster"], "unreviewed-all")
        self.assertEqual(report["outcome_labels"], {})

    def test_forecast_recorder_preserves_resolution_receipt_time(self):
        from test_foundation import book, market

        from polyalpha.forecast_runner import ForecastRecorder
        from polyalpha.quality import Filter
        from polyalpha.storage import Store

        later = NOW + timedelta(days=1)
        with Store(":memory:") as store:
            store.append("market", "m", NOW, market())
            store.append("book", "y", NOW, book(), NOW)
            store.append(
                "settlement",
                "y",
                later,
                dict(
                    token_id="y",
                    payout="1",
                    known_at=later.isoformat(),
                    verified=True,
                    source_url="fixture",
                ),
            )
            report = ForecastRecorder(Filter()).run(store.replay(later))
        self.assertEqual(report["outcome_labels"]["m"]["known_at"], later.isoformat())
        self.assertEqual(report["outcome_labels"]["m"]["outcome"], 1)
