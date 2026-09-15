"""Forecast research independent of trade eligibility, inventory, and entry decisions."""

import argparse
import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .cli import settings
from .dataset import export_forecasts
from .forecasting import Baseline
from .monitoring import code_provenance
from .parsing import parse_book, parse_market
from .storage import Store


class ForecastRecorder:
    def __init__(self, quality, clusters=None, model=None, cadence_seconds=3600):
        if cadence_seconds < 0:
            raise ValueError("negative forecast cadence")
        self.quality = quality
        self.clusters = clusters or {}
        self.model = model or Baseline()
        self.cadence = cadence_seconds
        self.markets = {}
        self.tokens = {}
        self.last_prediction = {}
        self.forecasts = []
        self.labels = {}
        self.resolved_tokens = set()
        self.resolved_markets = set()
        self.book_times = {}
        self.rejections = {}

    def reject(self, reason):
        self.rejections[reason] = self.rejections.get(reason, 0) + 1

    def run(self, records):
        prior = None
        digest = hashlib.sha256()
        count = 0
        for record in records:
            encoded = json.dumps(
                [
                    record.kind,
                    record.entity_id,
                    record.received_at.isoformat(),
                    record.source_at.isoformat() if record.source_at else None,
                    record.payload,
                ],
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            count += 1
            at = record.received_at
            if prior is not None and at < prior:
                raise ValueError("unordered forecast input")
            prior = at
            if record.kind == "market":
                market = parse_market(record.payload, at)
                self.markets[market.market_id] = market
                self.tokens[market.yes_token_id] = (market.market_id, True)
                self.tokens[market.no_token_id] = (market.market_id, False)
            elif record.kind == "settlement":
                p = record.payload
                if p.get("verified") is not True or not p.get("source_url"):
                    raise ValueError("unverified forecast label")
                if datetime.fromisoformat(p["known_at"]) > at:
                    raise ValueError("future forecast label")
                token = p["token_id"]
                self.resolved_tokens.add(token)
                if token not in self.tokens:
                    continue
                market_id, yes = self.tokens[token]
                self.resolved_markets.add(market_id)
                payout = Decimal(str(p["payout"]))
                if not payout.is_finite() or not 0 <= payout <= 1:
                    raise ValueError("invalid forecast label payout")
                if payout not in (0, 1):
                    continue
                outcome = int(payout) if yes else 1 - int(payout)
                if market_id in self.labels and self.labels[market_id]["outcome"] != outcome:
                    raise ValueError("conflicting forecast labels")
                self.labels.setdefault(market_id, dict(outcome=outcome, known_at=at.isoformat()))
            elif record.kind == "book":
                if record.entity_id not in self.tokens:
                    self.reject("missing_metadata")
                    continue
                market_id, yes = self.tokens[record.entity_id]
                if not yes:
                    continue
                if market_id in self.resolved_markets or record.entity_id in self.resolved_tokens:
                    self.reject("already_resolved")
                    continue
                market = self.markets[market_id]
                book = parse_book(record.payload, at, record.entity_id)
                if book.source_at < self.book_times.get(record.entity_id, book.source_at):
                    self.reject("out_of_order_book")
                    continue
                self.book_times[record.entity_id] = book.source_at
                reasons = self.quality.market_reasons(market, at) + self.quality.book_reasons(
                    book, at
                )
                if book.condition_id != market.condition_id:
                    reasons.append("condition_mismatch")
                if (at - market.received_at).total_seconds() > 300:
                    reasons.append("stale_metadata")
                if reasons:
                    for reason in reasons:
                        self.reject(reason)
                    continue
                previous = self.last_prediction.get(market_id)
                if previous is not None and (
                    at == previous or (at - previous).total_seconds() < self.cadence
                ):
                    continue
                forecast = self.model.predict(market_id, book, at)
                if forecast.timestamp > at or forecast.market_id != market_id:
                    raise ValueError("model returned unavailable/mismatched forecast")
                assignment = self.clusters.get(market_id, {})
                # All unreviewed markets share ONE conservative evaluation group.
                cluster = assignment.get("cluster") or "unreviewed-all"
                self.forecasts.append(
                    dict(
                        market_id=market_id,
                        cluster=cluster,
                        at=at.isoformat(),
                        p=str(forecast.probability),
                        model_version=forecast.version,
                    )
                )
                self.last_prediction[market_id] = at
        return dict(
            status="forecast_research_only",
            input_sha256=digest.hexdigest(),
            input_records=count,
            forecasts=self.forecasts,
            outcome_labels=self.labels,
            rejections=self.rejections,
            cadence_seconds=self.cadence,
            unreviewed_cluster_policy="all unassigned markets share a single group",
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/base.toml")
    parser.add_argument("--database", required=True)
    parser.add_argument("--clusters", default="config/clusters.json")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--cadence-seconds", type=int, default=3600)
    parser.add_argument("--output", required=True, help="new output directory for report and CSV")
    args = parser.parse_args()
    config, quality = settings(args.config)
    with open(args.clusters, encoding="utf-8-sig") as source:
        clusters = json.load(source)
    cutoff = datetime.fromisoformat(args.as_of)
    target = Path(args.output)
    target.mkdir(parents=True, exist_ok=False)
    with Store(args.database) as store:
        model = Baseline(Decimal(str(config.get("model", {}).get("uncertainty", ".05"))))
        report = ForecastRecorder(
            quality, clusters, model=model, cadence_seconds=args.cadence_seconds
        ).run(store.replay(cutoff))
    report.update(
        provenance=code_provenance(),
        cutoff=cutoff.isoformat(),
        configuration=config,
        cluster_assignments=clusters,
    )
    (target / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    export_forecasts(report, target / "forecasts.csv")
    print(json.dumps(dict(report=str(target / "report.json"), forecasts=len(report["forecasts"]))))


if __name__ == "__main__":
    main()
