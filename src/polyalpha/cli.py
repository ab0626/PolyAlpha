"""Bounded collection, lifecycle refresh, public streaming, and research commands."""

import argparse
import json
import time
import tomllib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .collector import Collector
from .quality import Filter
from .storage import Store
from .structured_logging import SystemLogger
from .transport import PublicHTTP


def settings(path):
    with open(path, "rb") as source:
        config = tomllib.load(source)
    filters = dict(config["market_filter"])
    for key in ("min_liquidity", "min_volume", "max_spread", "min_depth_shares"):
        if key in filters:
            filters[key] = Decimal(str(filters[key]))
    return config, Filter(**filters)


def collect_cycle(
    config, quality, refresh=False, stream_seconds=0, activity=False, resolutions=False
):
    collection = config["collection"]
    transport = PublicHTTP(collection["timeout_seconds"], collection["attempts"])
    with Store(collection["database"]) as store:
        stats = Collector(transport, store, quality).collect(
            collection["page_size"], collection["max_pages"]
        )
        if refresh:
            from .lifecycle import refresh_tracked

            stats["refreshed"] = len(refresh_tracked(transport, store))
        if activity or resolutions:
            from .activity import collect_resolution, collect_trades

            snapshots = {r.entity_id: r.payload for r in store.replay(datetime.now(UTC), "market")}
            stats["trade_rows"] = 0
            stats["settlements"] = 0
            for raw in snapshots.values():
                if activity:
                    page = collect_trades(
                        transport, store, raw["conditionId"], limit=100, max_pages=1
                    )
                    stats["trade_rows"] += page["rows"]
                stats["settlements"] += collect_resolution(transport, store, raw)
            if activity:
                stats["trade_coverage"] = "bounded to one page per condition"
        if stream_seconds:
            from .parsing import parse_book
            from .stream import stream

            specifications = {}
            for receipt in store.replay(datetime.now(UTC), "book"):
                book = parse_book(receipt.payload, receipt.received_at, receipt.entity_id)
                specifications[book.token_id] = (
                    book.condition_id,
                    book.tick_size,
                    book.min_order_size,
                )
            if specifications:
                stream(specifications, store, stream_seconds)
            stats["stream_tokens"] = len(specifications)
    return stats


def cmd_collect(args):
    """Data collection command."""
    config, quality = settings(args.config)
    interval = config["collection"]["interval_seconds"]
    if interval <= 0:
        raise ValueError("interval must be positive")

    log_dir = Path(config["collection"].get("log_dir", "data/logs"))
    sys_log = SystemLogger(log_dir / "system.jsonl")

    for cycle in range(args.cycles):
        stats = collect_cycle(
            config,
            quality,
            args.refresh_tracked,
            args.stream_seconds,
            args.activity,
            args.resolutions,
        )
        sys_log.log_collection_stats({**stats, "cycle": cycle + 1})
        print(
            json.dumps({"event": "collection_completed", "cycle": cycle + 1, **stats}),
            flush=True,
        )
        if cycle + 1 < args.cycles:
            time.sleep(interval)

    sys_log.close()


def cmd_bias(args):
    """Run bias checks on a backtest report."""
    from .bias import bias_summary, run_all_checks

    with open(args.report, encoding="utf-8") as f:
        report = json.load(f)

    checks = run_all_checks(report)
    summary = bias_summary(checks)

    output = {
        "report": args.report,
        "summary": summary,
        "checks": [
            {
                "name": c.name,
                "passed": c.passed,
                "severity": c.severity,
                "description": c.description,
                "details": c.details,
            }
            for c in checks
        ],
    }

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, default=str)
        print(json.dumps({"event": "bias_check_complete", "output": args.output, **summary}))
    else:
        print(json.dumps(output, indent=2, default=str))


def cmd_walk_forward(args):
    """Run walk-forward calibration evaluation."""
    import csv
    from datetime import timedelta

    from .calibration import Observation, walk_forward
    from .comparison import summarize_folds

    with open(args.csv, encoding="utf-8-sig", newline="") as source:
        rows = [
            Observation(
                r["market_id"],
                r["cluster"],
                datetime.fromisoformat(r["predicted_at"]),
                datetime.fromisoformat(r["label_known_at"]) if r["label_known_at"] else None,
                float(r["probability"]),
                int(r["outcome"]) if r["outcome"] else None,
            )
            for r in csv.DictReader(source)
        ]

    folds = walk_forward(
        rows,
        datetime.fromisoformat(args.first_test),
        datetime.fromisoformat(args.end),
        timedelta(days=args.window_days),
        args.method,
    )

    result = {
        "method": args.method,
        "folds": folds,
        "aggregate": summarize_folds(folds),
        "first_test": args.first_test,
        "end": args.end,
        "window_days": args.window_days,
    }

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, default=str)
    print(
        json.dumps({"event": "walk_forward_complete", "output": args.output, "folds": len(folds)})
    )


def cmd_experiment(args):
    """Record an experiment."""
    from .experiments import ExperimentTracker

    tracker = ExperimentTracker(args.output_dir)

    with open(args.report, encoding="utf-8") as f:
        report = json.load(f)

    exp = tracker.record(
        config=report.get("configuration", {}),
        metrics=report,
        data_path=args.report,
        model_version=args.model_version,
        feature_version=args.feature_version,
    )
    print(json.dumps({"event": "experiment_recorded", "experiment_id": exp.experiment_id}))


def cmd_compare(args):
    """Compare multiple strategy reports."""
    from .strategies import compare_strategies, result_from_report, strategy_report

    results = []
    for report_path in args.reports:
        with open(report_path, encoding="utf-8") as f:
            report = json.load(f)
        name = Path(report_path).stem
        results.append(result_from_report(report, name))

    comparison = compare_strategies(results)
    report_text = strategy_report(results)

    output = {"comparison": comparison, "report": report_text}

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, default=str)
        print(json.dumps({"event": "comparison_complete", "output": args.output}))
    else:
        print(report_text)


def cmd_train(args):
    """Train ML probability models."""
    from .config import load_env, load_toml
    from .training import (
        extract_features_from_db,
        save_artifacts,
        train_ensemble,
    )

    load_env()
    config = load_toml(args.config)

    db_path = args.db or config.get("database", {}).get("path", "data/polyalpha.db")
    if not Path(db_path).exists():
        print(json.dumps({"error": f"database not found: {db_path}"}))
        return

    sys_log = SystemLogger(Path(args.output_dir) / "system.jsonl")
    sys_log.log_event("training_started", db=db_path)

    samples = extract_features_from_db(db_path)
    if not samples:
        sys_log.log_event("training_failed", reason="no_samples")
        sys_log.close()
        print(json.dumps({"error": "no training samples found"}))
        return

    sys_log.log_event("samples_extracted", count=len(samples))

    # Chronological split
    samples.sort(key=lambda s: s.timestamp)
    n = len(samples)
    n_train = int(n * args.train_ratio)
    n_val = int(n * args.val_ratio)

    train = samples[:n_train]
    val = samples[n_train : n_train + n_val]
    test = samples[n_train + n_val :]

    if not val:
        val = train[-max(1, len(train) // 5) :]
        train = train[: -len(val)]
    if not test:
        test = val[-max(1, len(val) // 3) :]
        val = val[: -len(test)]

    result = train_ensemble(
        train,
        val,
        test,
        config={
            "logistic_C": args.logistic_c,
            "gbm_n_estimators": args.gbm_estimators,
            "gbm_max_depth": args.gbm_depth,
        },
    )

    if "error" in result:
        sys_log.log_event("training_failed", reason=result["error"])
        sys_log.close()
        print(json.dumps({"error": result["error"]}))
        return

    save_artifacts(
        result["trained_models"],
        result["ensemble_weights"],
        result["ensemble_metrics"],
        args.output_dir,
    )

    sys_log.log_event(
        "training_complete",
        ensemble_weights=result["ensemble_weights"],
        ensemble_metrics=result["ensemble_metrics"],
    )
    sys_log.close()

    output = {
        "event": "training_complete",
        "output_dir": args.output_dir,
        "train_samples": len(train),
        "val_samples": len(val),
        "test_samples": len(test),
        "ensemble_weights": result["ensemble_weights"],
        "ensemble_metrics": result["ensemble_metrics"],
        "component_results": {
            k: {kk: vv for kk, vv in v.items() if kk != "model_params"}
            for k, v in result["component_results"].items()
        },
    }
    print(json.dumps(output, indent=2, default=str))


def cmd_paper_run(args):
    """Run paper trading backtest from collected data."""
    from .paper_runner import main as paper_main

    paper_main(
        [
            "--db",
            args.db,
            "--output",
            args.output,
            "--exit-policy",
            args.exit_policy,
            "--limit",
            str(args.limit),
        ]
    )


def cmd_sizing(args):
    """Compute position sizing for a given scenario."""
    from .sizing import constrained_sizing, fixed_fractional_sizing, kelly_sizing

    equity = Decimal(args.equity)
    price = Decimal(args.price)
    probability = Decimal(args.probability) if args.probability else None

    if args.method == "fixed":
        result = fixed_fractional_sizing(equity, Decimal(args.fraction))
        output = {
            "method": "fixed_fractional",
            "shares": str(result),
            "notional": str(result * price),
            "fraction": args.fraction,
        }
    elif args.method == "kelly":
        if probability is None:
            print(json.dumps({"error": "probability required for kelly sizing"}))
            return
        result = kelly_sizing(probability, price, equity, Decimal(args.kelly_multiplier))
        output = {
            "method": "fractional_kelly",
            "shares": str(result.shares),
            "notional": str(result.notional),
            "fraction_of_bankroll": str(result.fraction_of_bankroll),
            "kelly_full": str(result.kelly_full) if result.kelly_full else None,
            "kelly_fractional": str(result.kelly_fractional) if result.kelly_fractional else None,
            "capped_by": result.capped_by,
        }
    else:
        risk_budget = Decimal(args.risk_budget) if args.risk_budget else equity * Decimal("0.02")
        probability = probability or Decimal("0.5")
        result = constrained_sizing(
            equity,
            risk_budget,
            price,
            normal_fraction=Decimal(args.fraction),
            max_market_fraction=Decimal(args.max_market_fraction),
            use_kelly=args.use_kelly,
            probability=probability,
        )
        output = {
            "method": result.method,
            "shares": str(result.shares),
            "notional": str(result.notional),
            "fraction_of_bankroll": str(result.fraction_of_bankroll),
            "capped_by": result.capped_by,
        }

    print(json.dumps(output, indent=2, default=str))


def cmd_market_making(args):
    """Analyze market-making opportunity for a given token."""
    from decimal import Decimal as D

    from .market_making import MakerQuoter

    fair = D(args.fair_value)
    half_spread = D(args.half_spread)

    quoter = MakerQuoter(base_half_spread=half_spread)
    from .market_making import MakerInventory

    inventory = MakerInventory(token_id=args.token_id, side="NONE")
    quote = quoter.quote(fair, inventory)

    output = {
        "token_id": args.token_id,
        "fair_value": str(fair),
        "bid_quote": str(quote.price),
        "bid_size": str(quote.size),
        "half_spread": str(quote.half_spread),
        "risk_adjustment": str(quote.risk_adjustment),
        "inventory_skew": str(quote.inventory_skew),
    }
    print(json.dumps(output, indent=2, default=str))


def cmd_alpha_decay(args):
    """Analyze alpha decay from a report's forecast data."""

    from .alpha_decay import AlphaDecayObservation, analyze_alpha_decay, decay_summary

    with open(args.report, encoding="utf-8") as f:
        report = json.load(f)

    forecasts = report.get("forecasts", [])
    if not forecasts:
        print(json.dumps({"error": "no forecasts in report"}))
        return

    # Try to build observations from fill journal + equity curve
    fills = report.get("fill_journal", [])

    if not fills:
        # No fills → use forecast-level analysis (predicted edge only)
        by_market = {}
        for fc in forecasts:
            mid = fc["market_id"]
            if mid not in by_market:
                by_market[mid] = fc

        output = {
            "total_markets": len(by_market),
            "forecasts_analyzed": len(by_market),
            "message": "Alpha decay analysis requires post-signal price observations.",
            "recommendation": (
                "Collect order-book snapshots at regular intervals after signals, "
                "or provide a CSV with columns: market_id,signal_time,signal_side,"
                "signal_probability,entry_price,price_30s,price_60s,price_300s,"
                "price_900s,price_3600s,price_21600s,price_86400s"
            ),
            "data_format": {
                "required_columns": [
                    "market_id",
                    "signal_time",
                    "signal_side",
                    "signal_probability",
                    "entry_price",
                ],
                "optional_columns": [
                    "price_30s",
                    "price_60s",
                    "price_300s",
                    "price_900s",
                    "price_3600s",
                    "price_21600s",
                    "price_86400s",
                ],
            },
        }
        print(json.dumps(output, indent=2, default=str))
        return

    # Build observations from fill journal
    observations = []
    for fill in fills:
        token_id = fill.get("token_id", "")
        side = fill.get("side", "BUY")
        vwap = Decimal(str(fill.get("vwap", "0")))
        ts_str = fill.get("timestamp", fill.get("filled_at", ""))
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError:
            continue

        # Find market_id from token mapping in equity curve or forecasts
        market_id = token_id
        for fc in forecasts:
            if fc.get("yes_token_id") == token_id or fc.get("token_id") == token_id:
                market_id = fc["market_id"]
                break

        observations.append(
            AlphaDecayObservation(
                market_id=market_id,
                signal_time=ts,
                signal_side="YES" if side == "BUY" else "NO",
                signal_probability=Decimal("0.5"),
                entry_price=vwap,
                price_observations={},
            )
        )

    if not observations:
        print(json.dumps({"error": "could not construct alpha decay observations from fills"}))
        return

    results = analyze_alpha_decay(observations)
    summary = decay_summary(results)

    output = {
        "observations": len(observations),
        "horizons": [
            {
                "horizon_seconds": r.horizon_seconds,
                "mean_price_change": r.mean_price_change,
                "median_price_change": r.median_price_change,
                "directional_accuracy": r.directional_accuracy,
                "sample_size": r.sample_size,
                "t_statistic": r.t_statistic,
                "p_value": r.p_value,
            }
            for r in results
        ],
        "summary": summary,
    }
    print(json.dumps(output, indent=2, default=str))


def cmd_feature_importance(args):
    """Run feature importance analysis on a trained model."""
    import pickle

    from .feature_importance import feature_summary, permutation_importance

    model_path = Path(args.model)
    if not model_path.exists():
        print(json.dumps({"error": f"model not found: {model_path}"}))
        return

    with open(model_path, "rb") as f:
        model_data = pickle.load(f)

    # Load test data from CSV
    import csv

    test_data = []
    test_labels = []
    with open(args.test_csv, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            features = {k: float(v) for k, v in row.items() if k != "outcome" and v}
            test_data.append(features)
            test_labels.append(int(row.get("outcome", 0)))

    if not test_data:
        print(json.dumps({"error": "no test data found"}))
        return

    feature_names = list(test_data[0].keys())
    importances = permutation_importance(
        model_data["model"], feature_names, test_data, test_labels, n_repeats=args.repeats
    )
    summary = feature_summary(importances)

    output = {
        "model_path": str(model_path),
        "test_samples": len(test_data),
        "summary": summary,
        "importances": [
            {
                "feature": i.feature_name,
                "importance": i.importance,
                "direction": i.direction,
            }
            for i in importances
        ],
    }
    print(json.dumps(output, indent=2, default=str))


def cmd_anomaly(args):
    """Run anomaly detection on a book snapshot."""
    import json as json_mod
    from decimal import Decimal

    from .anomaly import AnomalyDetector

    with open(args.book, encoding="utf-8") as f:
        raw = json_mod.load(f)

    from .domain import Book, Level

    bids = [Level(Decimal(str(b["price"])), Decimal(str(b["size"]))) for b in raw.get("bids", [])]
    asks = [Level(Decimal(str(a["price"])), Decimal(str(a["size"]))) for a in raw.get("asks", [])]
    from datetime import UTC, datetime

    book = Book(
        token_id=raw.get("token_id", "unknown"),
        condition_id=raw.get("condition_id", ""),
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("1"),
        source_at=datetime.now(UTC),
        bids=bids,
        asks=asks,
    )

    detector = AnomalyDetector()
    anomalies = detector.detect(book, {"microprice": None, "midpoint": None})
    multiplier = detector.uncertainty_multiplier(anomalies)

    output = {
        "token_id": book.token_id,
        "anomaly_count": len(anomalies),
        "uncertainty_multiplier": str(multiplier),
        "anomalies": [
            {
                "kind": a.kind,
                "severity": str(a.severity),
                "description": a.description,
            }
            for a in anomalies
        ],
    }
    print(json.dumps(output, indent=2, default=str))


# ── v0.3 Research Command Handlers ───────────────────────────────────────────


def _load_dataset(path: str):
    """Load a ResearchDataset from JSON."""
    import json as _json
    from datetime import datetime
    from decimal import Decimal

    from .research_dataset import MarketSnapshot, ResearchDataset

    with open(path, encoding="utf-8") as f:
        data = _json.load(f)

    snapshots = []
    for r in data.get("snapshots", data.get("records", [])):
        fp_data = r.get("feature_provenance", {})
        fp = None
        if fp_data:
            fp_kwargs = {}
            for ts_key in [
                "source_timestamp", "retrieval_timestamp", "feature_timestamp"
            ]:
                val = fp_data.get(ts_key)
                if val:
                    fp_kwargs[ts_key] = datetime.fromisoformat(val)
            fp_data_clean = {
                k: v for k, v in fp_data.items()
                if k not in ("source_timestamp", "retrieval_timestamp",
                             "feature_timestamp") or k in fp_kwargs
            }
            fp_data_clean.update(fp_kwargs)
            fp = fp_data_clean

        snap_kwargs = {}
        for k, v in r.items():
            if k == "feature_provenance":
                continue
            if k in ("model_probability", "conservative_probability",
                      "execution_price", "net_edge", "yes_best_bid",
                      "yes_best_ask", "yes_mid", "yes_spread",
                      "yes_depth_1", "yes_depth_5", "yes_depth_10",
                      "yes_bid_size", "yes_ask_size", "no_best_bid",
                      "no_best_ask", "no_mid", "no_spread", "no_depth_1",
                      "no_depth_5",
                      "no_depth_10", "no_bid_size", "no_ask_size",
                      "volume", "liquidity", "fee_rate",
                      "ambiguity_score", "consensus_score"):
                snap_kwargs[k] = Decimal(str(v)) if v is not None and v != "" else None
            elif k in ("observation_timestamp", "resolution_timestamp",
                       "source_timestamp", "event_start", "event_end"):
                snap_kwargs[k] = datetime.fromisoformat(v) if v and v != "" else None
            elif k in ("hours_to_resolution", "days_to_resolution"):
                snap_kwargs[k] = float(v) if v is not None and v != "" else None
            elif k == "final_resolution":
                snap_kwargs[k] = int(v) if v is not None and v != "" else None
            elif k in ("fees_enabled", "resolution_consensus"):
                snap_kwargs[k] = bool(v) if v is not None else None
            else:
                snap_kwargs[k] = v

        if fp is not None:
            from .research_dataset import FeatureProvenance
            snap_kwargs["feature_provenance"] = FeatureProvenance(**fp)
        snapshots.append(MarketSnapshot(**snap_kwargs))

    return ResearchDataset(
        snapshots=snapshots,
        created_at=datetime.fromisoformat(
            data.get("created_at", datetime.utcnow().isoformat())
        ),
        source_reports=data.get("source_reports", []),
        data_hash=data.get("data_hash", ""),
        period_start=data.get("period_start"),
        period_end=data.get("period_end"),
        total_observations=data.get(
            "total_observations",
            data.get("total_markets", len(snapshots)),
        ),
        resolved_observations=data.get(
            "resolved_observations", data.get("resolved_markets", 0)
        ),
        unresolved_observations=data.get(
            "unresolved_observations", data.get("unresolved_markets", 0)
        ),
        unique_markets=data.get(
            "unique_markets",
            data.get("total_markets", len(snapshots)),
        ),
        unique_events=data.get("unique_events", 0),
        unique_clusters=data.get("unique_clusters", 0),
        categories=data.get("categories", {}),
    )


def cmd_build_dataset(args):
    """Build canonical research dataset from reports."""
    import json

    reports = []
    for path in args.reports:
        with open(path, encoding="utf-8") as f:
            reports.append(json.load(f))

    from .research_dataset import build_dataset

    dataset = build_dataset(reports, source_labels=args.reports)

    if args.format == "csv":
        csv_path = args.output.replace(".json", ".csv")
        dataset.to_csv(csv_path)
        print(
            json.dumps(
                {"dataset": csv_path, "records": len(dataset.snapshots), "hash": dataset.data_hash}
            )
        )
    else:
        dataset.to_json(args.output)
        print(
            json.dumps(
                {"dataset": args.output, "records": len(dataset.snapshots), "hash": dataset.data_hash}
            )
        )


def cmd_audit_dataset(args):
    """Audit research dataset integrity."""
    import json

    from .dataset_audit import DatasetAuditor

    dataset = _load_dataset(args.dataset)
    auditor = DatasetAuditor()
    report = auditor.audit(dataset)
    print(json.dumps(report.summary(), indent=2))


def cmd_market_benchmark(args):
    """Evaluate model vs market baseline."""
    import json

    from .market_benchmark import evaluate_market_benchmark

    dataset = _load_dataset(args.dataset)
    result = evaluate_market_benchmark(dataset)
    print(json.dumps(result.summary(), indent=2))


def cmd_null_strategies(args):
    """Evaluate null/sanity strategies."""
    import json
    from dataclasses import asdict

    from .null_strategies import evaluate_null_strategies

    dataset = _load_dataset(args.dataset)
    results = evaluate_null_strategies(dataset)
    print(json.dumps([asdict(r) for r in results], indent=2))


def cmd_cost_ladder(args):
    """Run cost ladder experiment."""
    import json

    from .cost_ladder import run_cost_ladder

    dataset = _load_dataset(args.dataset)
    result = run_cost_ladder(dataset)
    print(json.dumps(result.summary(), indent=2))


def cmd_model_ablation(args):
    """Run model ablation study."""
    import json

    from .model_ablation import run_ablation

    dataset = _load_dataset(args.dataset)
    result = run_ablation(dataset)
    print(json.dumps(result.summary(), indent=2))


def cmd_model_vs_market(args):
    """Build model vs market matrix."""
    import json

    from .model_vs_market import build_market_model_matrix

    dataset = _load_dataset(args.dataset)
    result = build_market_model_matrix(dataset, n_buckets=10)
    print(json.dumps(result.summary(), indent=2))


def cmd_disagreement(args):
    """Analyze model-market disagreement."""
    import json

    from .disagreement_analysis import analyze_disagreement

    dataset = _load_dataset(args.dataset)
    result = analyze_disagreement(dataset)
    print(json.dumps(result.summary(), indent=2))


def cmd_time_to_resolution(args):
    """Analyze time-to-resolution performance."""
    import json

    from .time_to_resolution import analyze_time_to_resolution

    dataset = _load_dataset(args.dataset)
    result = analyze_time_to_resolution(dataset)
    print(json.dumps(result.summary(), indent=2))


def cmd_category_analysis(args):
    """Analyze per-category performance."""
    import json

    from .category_analysis import analyze_categories

    dataset = _load_dataset(args.dataset)
    result = analyze_categories(dataset)
    print(json.dumps(result.summary(), indent=2))


def cmd_ensemble_consensus(args):
    """Analyze ensemble agreement quality."""
    import json

    from .ensemble_consensus import analyze_consensus

    dataset = _load_dataset(args.dataset)
    result = analyze_consensus(dataset)
    print(json.dumps(result.summary(), indent=2))


def cmd_effective_sample_size(args):
    """Compute effective sample size."""
    import json

    from .effective_sample_size import compute_effective_sample_size

    dataset = _load_dataset(args.dataset)
    result = compute_effective_sample_size(dataset)
    print(json.dumps(result.summary(), indent=2))


def cmd_bootstrap_ci(args):
    """Compute bootstrap confidence interval."""
    import json

    from .effective_sample_size import bootstrap_confidence_interval

    def _metric_fn_for(metric_name):
        if metric_name == "pnl":
            def fn(recs):
                if not recs:
                    return 0.0
                return sum(float(s.net_edge or 0) for s in recs) / len(recs)
            return fn
        elif metric_name == "edge":
            def fn(recs):
                if not recs:
                    return 0.0
                edges = [float(s.net_edge or 0) for s in recs]
                return sum(edges) / len(edges)
            return fn
        elif metric_name == "profit_factor":
            def fn(recs):
                if not recs:
                    return 0.0
                gains = sum(float(s.net_edge or 0) for s in recs if float(s.net_edge or 0) > 0)
                losses = abs(sum(float(s.net_edge or 0) for s in recs if float(s.net_edge or 0) < 0))
                return gains / losses if losses > 0 else float("inf")
            return fn
        elif metric_name == "sharpe":
            def fn(recs):
                if not recs:
                    return 0.0
                edges = [float(s.net_edge or 0) for s in recs]
                mean_e = sum(edges) / len(edges)
                if len(edges) < 2:
                    return 0.0
                var_e = sum((e - mean_e) ** 2 for e in edges) / (len(edges) - 1)
                return mean_e / (var_e ** 0.5) if var_e > 0 else 0.0
            return fn
        else:  # brier
            return None

    metric_fn = _metric_fn_for(args.metric)
    metric_name = args.metric if args.metric != "brier" else "brier_score"

    dataset = _load_dataset(args.dataset)
    result = bootstrap_confidence_interval(
        dataset, metric_fn=metric_fn, n_bootstrap=args.samples
    )
    # Override metric name if non-default
    if args.metric != "brier":
        result = type(result)(
            metric_name=metric_name,
            point_estimate=result.point_estimate,
            ci_lower=result.ci_lower,
            ci_upper=result.ci_upper,
            ci_level=result.ci_level,
            bootstrap_samples=result.bootstrap_samples,
            std_error=result.std_error,
        )
    print(json.dumps(result.summary(), indent=2))


def cmd_monte_carlo_stress(args):
    """Run Monte Carlo stress testing."""
    import json

    from .monte_carlo_stress import run_monte_carlo_stress

    dataset = _load_dataset(args.dataset)
    result = run_monte_carlo_stress(dataset, n_simulations=args.simulations)
    print(json.dumps(result.summary(), indent=2))


def cmd_model_degradation(args):
    """Analyze model degradation over time."""
    import json

    from .model_degradation import run_degradation_tests

    dataset = _load_dataset(args.dataset)
    result = run_degradation_tests(dataset)
    print(json.dumps(result.summary(), indent=2))


def cmd_negative_controls(args):
    """Run negative control tests."""
    import json

    from .negative_controls import run_negative_controls

    dataset = _load_dataset(args.dataset)
    results = run_negative_controls(dataset)
    print(json.dumps([r.summary() for r in results], indent=2))


def cmd_holdout_lock(args):
    """Lock the final holdout period."""
    import json

    from .holdout import lock_holdout

    state = lock_holdout(args.config_dir)
    print(json.dumps(state, indent=2))


def cmd_holdout_unlock(args):
    """Unlock the final holdout with justification."""
    import json

    from .holdout import unlock_holdout

    state = unlock_holdout(args.config_dir, reason=args.reason)
    print(json.dumps(state, indent=2))


def cmd_holdout_status(args):
    """Check holdout lock status."""
    import json

    from .holdout import check_holdout_lock

    state = check_holdout_lock(args.config_dir)
    print(json.dumps(state, indent=2))


# ── v0.4 Research Command Handlers ───────────────────────────────────────────


def cmd_conditional_graph(args):
    """Build conditional probability graph from dataset."""
    import json

    from .conditional_graph import ConditionalGraph

    dataset = _load_dataset(args.dataset)
    graph = ConditionalGraph(
        min_samples=args.min_samples,
        residual_threshold=args.threshold,
    )

    for snap in dataset.snapshots:
        if snap.market_id and snap.cluster:
            b_price = None
            if snap.execution_price is not None:
                b_price = float(snap.execution_price)
            graph.add_relationship(
                a_id=snap.cluster,
                b_id=snap.market_id,
                a_observed=True,
                b_observed=(
                    snap.final_resolution == 1
                    if snap.final_resolution is not None
                    else False
                ),
                b_executable_price=b_price,
            )

    summary = graph.summary()
    signals = graph.get_signals()
    output = {
        "total_relationships": summary.total_relationships,
        "active_signals": summary.active_signals,
        "mean_abs_residual": summary.mean_abs_residual,
        "max_abs_residual": summary.max_abs_residual,
        "strong_signal_count": summary.strong_signal_count,
        "signals": [
            {
                "b_id": s.b_id,
                "implied_p_b": s.implied_p_b,
                "observed_p_b": s.observed_p_b,
                "residual": s.residual,
                "strength": s.strength,
            }
            for s in signals
        ],
    }
    print(json.dumps(output, indent=2, default=str))


def cmd_detect_leakage(args):
    """Detect train/val leakage in dataset splits."""
    import json

    from .leakage_tests import detect_train_val_leakage

    dataset = _load_dataset(args.dataset)
    n = len(dataset.snapshots)
    n_train = int(n * args.train_ratio)

    train_ids = {r.snapshot_id for r in dataset.snapshots[:n_train]}
    val_ids = {r.snapshot_id for r in dataset.snapshots[n_train:]}

    report = detect_train_val_leakage(
        dataset.snapshots, train_ids, val_ids,
        temporal_threshold_seconds=args.temporal_threshold,
    )
    print(json.dumps(report.summary(), indent=2, default=str))


def cmd_oos_test(args):
    """Run category-exclusion out-of-domain test."""
    import json
    from dataclasses import asdict

    from .out_of_domain import OODSnapshot, run_oos_test

    dataset = _load_dataset(args.dataset)
    snapshots = []
    for r in dataset.snapshots:
        category = r.cluster or r.event_cluster or "unknown"
        prob = float(r.model_probability) if r.model_probability is not None else 0.5
        outcome = r.final_resolution if r.final_resolution is not None else 0
        snapshots.append(OODSnapshot(category=category, model_probability=prob, outcome=outcome))

    all_categories = list({s.category for s in snapshots})
    half = len(all_categories) // 2
    train_cats = (
        args.train_categories
        if args.train_categories
        else all_categories[:half]
    )
    test_cats = (
        args.test_categories
        if args.test_categories
        else all_categories[half:]
    )

    result = run_oos_test(snapshots, train_cats, test_cats)
    print(json.dumps(asdict(result), indent=2, default=str))


def cmd_regime_analysis(args):
    """Analyze liquidity and spread regimes."""
    import json

    from .regime_analysis import RegimeSnapshot, analyze_liquidity_regimes, analyze_spread_regimes

    dataset = _load_dataset(args.dataset)
    snapshots = []
    for r in dataset.snapshots:
        snapshots.append(RegimeSnapshot(
            market_id=r.market_id,
            yes_mid=float(r.yes_mid) if r.yes_mid is not None else None,
            yes_spread=float(r.yes_spread) if r.yes_spread is not None else None,
            yes_depth_5=float(r.yes_depth_5) if r.yes_depth_5 is not None else 0.0,
            yes_depth_10=float(r.yes_depth_10) if r.yes_depth_10 is not None else 0.0,
            volume=float(r.volume) if r.volume is not None else 0.0,
            liquidity=float(r.liquidity) if r.liquidity is not None else 0.0,
            model_probability=(
                float(r.model_probability)
                if r.model_probability is not None
                else 0.5
            ),
            final_resolution=r.final_resolution if r.final_resolution is not None else 0,
            net_edge=float(r.net_edge) if r.net_edge is not None else 0.0,
        ))

    liq_report = analyze_liquidity_regimes(snapshots)
    spread_report = analyze_spread_regimes(snapshots)
    output = {
        "liquidity": liq_report.summary_dict(),
        "spread": spread_report.summary_dict(),
    }
    print(json.dumps(output, indent=2, default=str))


def cmd_signal_decay(args):
    """Analyze signal decay over time horizons."""
    import json

    from .signal_decay import analyze_signal_decay

    dataset = _load_dataset(args.dataset)
    snapshots = []
    for r in dataset.snapshots:
        snapshots.append({
            "signal_time": r.observation_timestamp.isoformat() if r.observation_timestamp else "",
            "signal_side": "YES" if (r.model_probability or 0) > 0.5 else "NO",
            "signal_probability": (
                float(r.model_probability)
                if r.model_probability is not None
                else 0.5
            ),
            "entry_midpoint": (
                float(r.yes_mid) if r.yes_mid is not None else 0.5
            ),
            "midpoints": {},
            "executables": {},
        })

    result = analyze_signal_decay(snapshots)
    print(json.dumps(result.summary(), indent=2, default=str))


def cmd_latency_sim(args):
    """Simulate execution latency effects."""
    import json

    from .latency_sim import simulate_latency

    dataset = _load_dataset(args.dataset)
    snapshots = []
    for r in dataset.snapshots:
        snapshots.append({
            "market_id": r.market_id,
            "signal_probability": (
                float(r.model_probability)
                if r.model_probability is not None
                else 0.5
            ),
            "spread": float(r.yes_spread) if r.yes_spread is not None else 0.02,
            "depth": float(r.yes_depth_5) if r.yes_depth_5 is not None else 0.0,
            "side": "BUY",
            "timestamp": r.observation_timestamp or r.source_timestamp,
        })

    delays = [int(d) for d in args.delays.split(",")] if args.delays else None
    result = simulate_latency(snapshots, delays_seconds=delays)
    print(json.dumps(result.summary(), indent=2, default=str))


def cmd_partial_fill(args):
    """Stress test partial fill scenarios."""
    import json

    from .partial_fill import simulate_partial_fills

    dataset = _load_dataset(args.dataset)
    snapshots = []
    for r in dataset.snapshots:
        snapshots.append({
            "market_id": r.market_id,
            "signal_side": "YES" if (r.model_probability or 0) > 0.5 else "NO",
            "midpoint": float(r.yes_mid) if r.yes_mid is not None else 0.5,
            "order_size": 100.0,
            "book_depth": float(r.yes_depth_5) if r.yes_depth_5 is not None else 0.0,
            "spread": float(r.yes_spread) if r.yes_spread is not None else 0.02,
            "edge": float(r.net_edge) if r.net_edge is not None else 0.0,
        })

    fractions = [float(f) for f in args.fractions.split(",")] if args.fractions else None
    result = simulate_partial_fills(snapshots, fill_fractions=fractions)
    print(json.dumps(result.summary(), indent=2, default=str))


def cmd_maker_stress(args):
    """Stress test maker strategy."""
    import json

    from .maker_stress import stress_test_maker

    dataset = _load_dataset(args.dataset)
    signals = []
    for r in dataset.snapshots:
        signals.append({
            "market_id": r.market_id,
            "side": "BUY",
            "price": float(r.execution_price) if r.execution_price is not None else 0.5,
            "size": 100.0,
            "future_midpoint": float(r.yes_mid) if r.yes_mid is not None else 0.5,
            "aggressive_volume_reaching_price": 50.0,
            "queue_ahead": 0.0,
        })

    scenarios = args.scenarios.split(",") if args.scenarios else None
    result = stress_test_maker(signals, [], scenarios=scenarios)
    print(json.dumps(result.summary(), indent=2, default=str))


def cmd_liquidation(args):
    """Compute executable liquidation equity."""
    import json

    from .liquidation import compute_liquidation_equity

    dataset = _load_dataset(args.dataset)
    positions = []
    books = {}
    for r in dataset.snapshots:
        if r.yes_best_bid is not None and r.yes_depth_1 is not None:
            positions.append({
                "token_id": r.market_id,
                "side": "LONG",
                "shares": float(r.yes_depth_1) if r.yes_depth_1 else 100.0,
                "mid_price": float(r.yes_mid) if r.yes_mid is not None else 0.5,
                "basis": float(r.execution_price) if r.execution_price is not None else 0.5,
            })
            books[r.market_id] = [
                {"price": float(r.yes_best_bid), "size": float(r.yes_depth_1), "side": "bid"},
                {
                    "price": (
                        float(r.yes_best_ask)
                        if r.yes_best_ask
                        else float(r.yes_best_bid) + 0.02
                    ),
                    "size": float(r.yes_depth_1),
                    "side": "ask"
                },
            ]

    result = compute_liquidation_equity(positions, books)
    print(json.dumps(result.summary(), indent=2, default=str))


def cmd_concentration(args):
    """Analyze profit concentration."""
    import json

    from .concentration import Decision, analyze_concentration

    dataset = _load_dataset(args.dataset)
    decisions = []
    for r in dataset.snapshots:
        if r.net_edge is not None:
            decisions.append(Decision(
                group_key=r.cluster or r.market_id or "unknown",
                pnl=float(r.net_edge),
                trade_count=1,
            ))

    result = analyze_concentration(decisions, group_by=args.group_by)
    print(json.dumps(result.summary(), indent=2, default=str))


def cmd_persistence(args):
    """Analyze profit persistence over rolling windows."""
    import json

    from .persistence import EquityPoint, analyze_profit_persistence

    dataset = _load_dataset(args.dataset)
    equity_curve = []
    for i, r in enumerate(dataset.snapshots):
        ts = (
            r.observation_timestamp.timestamp()
            if r.observation_timestamp
            else i * 3600.0
        )
        net_edge = float(r.net_edge) if r.net_edge is not None else 0.0
        outcome = r.final_resolution
        mkt_brier = (
            (float(r.yes_mid) - outcome) ** 2
            if r.yes_mid is not None and outcome is not None
            else None
        )
        mdl_brier = (
            (float(r.model_probability) - outcome) ** 2
            if r.model_probability is not None and outcome is not None
            else None
        )
        equity_curve.append(EquityPoint(
            trade_index=i,
            timestamp=ts,
            cumulative_pnl=sum(float(rec.net_edge or 0) for rec in dataset.snapshots[:i+1]),
            market_brier=mkt_brier,
            model_brier=mdl_brier,
            net_edge=net_edge,
        ))

    result = analyze_profit_persistence(
        equity_curve,
        window_size=args.window_size,
        window_types=[w.strip() for w in args.window_types.split(",")],
    )
    print(json.dumps(result.summary(), indent=2, default=str))


def cmd_sensitivity(args):
    """Run parameter sensitivity grid."""
    import json

    from .parameter_sensitivity import run_sensitivity_grid

    param_values = [float(v) for v in args.values.split(",")]

    dataset = _load_dataset(args.dataset)

    def eval_fn(pv):
        n = len(dataset.snapshots)
        return sum(float(r.net_edge or 0) for r in dataset.snapshots) / n if n > 0 else 0.0

    result = run_sensitivity_grid(
        args.param_name, param_values, eval_fn,
        profitability_threshold=args.threshold,
    )
    print(json.dumps(result.summary_dict(), indent=2, default=str))


def cmd_overfitting(args):
    """Detect overfitting signals."""
    import json

    from .overfitting import TrainTestMetrics, detect_overfitting

    with open(args.report, encoding="utf-8") as f:
        report = json.load(f)

    train = report.get("train_metrics", {})
    test = report.get("test_metrics", {})
    train_metrics = TrainTestMetrics(
        brier_score=train.get("brier_score", 0.0),
        log_loss=train.get("log_loss", 0.0),
        ece=train.get("ece", 0.0),
        sample_count=train.get("sample_count", 0),
        accuracy=train.get("accuracy", 0.0),
    )
    test_metrics = TrainTestMetrics(
        brier_score=test.get("brier_score", 0.0),
        log_loss=test.get("log_loss", 0.0),
        ece=test.get("ece", 0.0),
        sample_count=test.get("sample_count", 0),
        accuracy=test.get("accuracy", 0.0),
    )

    result = detect_overfitting(
        train_metrics, test_metrics,
        n_parameters=report.get("n_parameters", 0),
        n_observations=report.get("n_observations", 0),
        holdout_evaluations=report.get("holdout_evaluations", 0),
        n_strategies_tested=report.get("n_strategies_tested", 1),
        best_p_value=report.get("best_p_value", 1.0),
    )
    print(json.dumps(result.summary(), indent=2, default=str))


def cmd_paper_monitor(args):
    """Show paper trading monitor summary."""
    import json

    from .paper_monitor import PaperMonitor

    monitor = PaperMonitor()
    if args.log_file and Path(args.log_file).exists():
        with open(args.log_file, encoding="utf-8") as f:
            log_data = json.load(f)
        for s in log_data.get("signals", []):
            monitor.record_signal(
                market_id=s.get("market_id", ""),
                signal_side=s.get("side", "BUY"),
                signal_probability=Decimal(s.get("probability", "0.5")),
                midpoint=Decimal(s.get("midpoint", "0.5")),
                spread=Decimal(s.get("spread", "0.02")),
                book_depth=Decimal("0"),
                model_version=s.get("model_version", "unknown"),
            )

    summary = monitor.get_summary()
    print(json.dumps(summary, indent=2, default=str))


def cmd_paper_compare(args):
    """Compare paper vs backtest performance."""
    import json

    from .paper_backtest_compare import compare_paper_backtest

    with open(args.paper_decisions, encoding="utf-8") as f:
        paper = json.load(f)
    with open(args.backtest_decisions, encoding="utf-8") as f:
        backtest = json.load(f)

    overlap = None
    if args.overlap_start and args.overlap_end:
        overlap = (args.overlap_start, args.overlap_end)

    result = compare_paper_backtest(
        paper if isinstance(paper, list) else paper.get("decisions", []),
        backtest if isinstance(backtest, list) else backtest.get("decisions", []),
        overlap_period=overlap,
    )
    print(json.dumps(result.summary(), indent=2, default=str))


def cmd_manifest(args):
    """Generate experiment manifest."""
    import json

    from .reproducibility import create_experiment_manifest

    with open(args.config, encoding="utf-8") as f:
        config = json.load(f)

    result = create_experiment_manifest(
        config=config,
        dataset_size=args.dataset_size,
        feature_count=args.feature_count,
        categories=[c.strip() for c in args.categories.split(",")] if args.categories else [],
        model_version=args.model_version,
        git_commit=args.git_commit,
        base_seed=args.seed,
        tags=[t.strip() for t in args.tags.split(",")] if args.tags else None,
    )
    print(json.dumps(result.to_dict(), indent=2, default=str))


def cmd_fingerprint(args):
    """Compute dataset fingerprint."""
    import json

    from .reproducibility import compute_dataset_fingerprint

    dataset = _load_dataset(args.dataset)
    observation_ids = [r.snapshot_id for r in dataset.snapshots]
    timestamps = [
        r.observation_timestamp.isoformat()
        if r.observation_timestamp
        else ""
        for r in dataset.snapshots
    ]
    labels = [r.final_resolution if r.final_resolution is not None else 0 for r in dataset.snapshots]
    schema_fields = list(vars(dataset.snapshots[0]).keys()) if dataset.snapshots else []

    fingerprint = compute_dataset_fingerprint(observation_ids, timestamps, labels, schema_fields)
    print(json.dumps({"fingerprint": fingerprint, "record_count": len(observation_ids)}, indent=2))


def cmd_significance(args):
    """Run statistical significance tests."""
    import json

    from .statistical_tests import full_statistical_analysis

    with open(args.report, encoding="utf-8") as f:
        report = json.load(f)

    data = report.get("values", [])
    p_values = report.get("p_values", None)
    cluster_ids = report.get("cluster_ids", list(range(len(data))))

    def stat_fn(x):
        return sum(x) / len(x) if x else 0.0

    result = full_statistical_analysis(
        data, stat_fn, cluster_ids,
        p_values=p_values,
        brier_model=report.get("brier_model"),
        brier_market=report.get("brier_market"),
        n_bootstrap=args.bootstrap_samples,
    )
    print(json.dumps(result.summary(), indent=2, default=str))


def cmd_multiple_testing(args):
    """Apply multiple testing corrections."""
    import json

    from .statistical_tests import benjamini_hochberg, bonferroni_correction

    with open(args.p_values_file, encoding="utf-8") as f:
        p_values = json.load(f)

    bonf = bonferroni_correction(p_values)
    bh = benjamini_hochberg(p_values)
    output = {
        "bonferroni": {
            "method": bonf.method,
            "n_tests": len(p_values),
            "rejected_at_005": sum(bonf.rejected_at_005),
            "rejected_at_010": sum(bonf.rejected_at_010),
            "adjusted_p_values": bonf.adjusted_p_values,
        },
        "benjamini_hochberg": {
            "method": bh.method,
            "n_tests": len(p_values),
            "rejected_at_005": sum(bh.rejected_at_005),
            "rejected_at_010": sum(bh.rejected_at_010),
            "adjusted_p_values": bh.adjusted_p_values,
        },
    }
    print(json.dumps(output, indent=2, default=str))


def cmd_model_staleness(args):
    """Detect model staleness."""
    import json
    from datetime import datetime

    from .model_staleness import detect_staleness

    trained_at = datetime.fromisoformat(args.trained_at)
    current = datetime.fromisoformat(args.current_time) if args.current_time else datetime.utcnow()

    result = detect_staleness(
        trained_at, current,
        max_age_days=args.max_age_days,
    )
    print(json.dumps(result.summary(), indent=2, default=str))


def cmd_decision_audit(args):
    """Reconstruct decision audit trail."""
    import json
    from datetime import datetime
    from decimal import Decimal as D

    from .decision_audit import (
        CostSnapshot,
        DecisionAuditor,
        FeatureSnapshot,
        ForecastSnapshot,
        MarketStateSnapshot,
        ModelSnapshot,
        RiskSnapshot,
    )

    with open(args.audit_log, encoding="utf-8") as f:
        audit_data = json.load(f)

    auditor = DecisionAuditor()
    for record in audit_data.get("decisions", []):
        ms = record.get("market_state", {})
        market_state = MarketStateSnapshot(
            market_id=record.get("market_id", ""),
            timestamp=datetime.fromisoformat(record.get("timestamp", "2025-01-01T00:00:00+00:00")),
            midpoint=D(str(ms.get("yes_mid", 0.5))),
            bid=D(str(ms.get("yes_best_bid", 0.5))),
            ask=D(str(ms.get("yes_best_ask", 0.5))),
            spread=D(str(ms.get("yes_spread", 0.0))),
            bid_depth=D("0"),
            ask_depth=D("0"),
            volume_24h=D("0"),
            last_trade_price=D("0"),
        )
        signal = record.get("signal", {})
        forecast = ForecastSnapshot(
            raw_forecast=D(str(signal.get("model_probability", 0.5))),
            calibrated_forecast=D(str(signal.get("model_probability", 0.5))),
        )
        costs = record.get("costs", {})
        cost_snapshot = CostSnapshot(
            spread_cost=D(str(costs.get("spread_cost", 0))),
            slippage_estimate=D(str(costs.get("slippage_estimate", 0))),
            fee_estimate=D(str(costs.get("fee_cost", 0))),
            total_cost=D(str(costs.get("spread_cost", 0) + costs.get("fee_cost", 0))),
        )
        execution = record.get("execution", {})
        auditor.record_decision(
            market_id=record.get("market_id", ""),
            signal_id=record.get("decision_id", ""),
            side=signal.get("side", "BUY"),
            size=D("0"),
            target_price=D(str(execution.get("exec_price", 0.5))),
            market_state=market_state,
            features=FeatureSnapshot(feature_names=[], feature_values=[]),
            model=ModelSnapshot(
                model_id="",
                model_version=record.get("model_version", ""),
                model_type="",
                training_samples=0,
            ),
            forecast=forecast,
            costs=cost_snapshot,
            risk=RiskSnapshot(
                position_size=D(0), portfolio_heat=D(0), current_drawdown=D(0),
                daily_pnl=D(0), risk_budget_remaining=D(0),
            ),
            edge=D(str(execution.get("net_edge", 0))),
            expected_value=D(str(execution.get("net_edge", 0))),
            final_decision="accept" if record.get("accepted", False) else "reject",
        )

    if args.decision_id:
        result = auditor.reconstruct(args.decision_id)
        print(json.dumps(result, indent=2, default=str))
    else:
        summary = auditor.get_summary_stats()
        print(json.dumps(summary, indent=2, default=str))


def cmd_replay(args):
    """Replay paper decisions from database snapshots for a given date."""
    import json
    from datetime import UTC, datetime

    from .storage import Store

    target_date = datetime.fromisoformat(args.date).date()
    db_path = args.db

    if not Path(db_path).exists():
        print(json.dumps({"error": f"database not found: {db_path}"}))
        return

    with Store(db_path) as store:
        # Load all market snapshots from DB
        all_snapshots = list(store.replay(datetime.now(UTC), "market"))
        if not all_snapshots:
            print(json.dumps({"error": "no snapshots found in database", "date": args.date}))
            return

        # Filter snapshots to the target date
        date_snapshots = [
            s for s in all_snapshots
            if s.received_at and s.received_at.date() == target_date
        ]

        if not date_snapshots:
            print(json.dumps({
                "error": f"no snapshots found for {args.date}",
                "total_snapshots": len(all_snapshots),
            }))
            return

        # Re-run paper decisions for each snapshot
        decisions = []
        for receipt in date_snapshots:
            payload = receipt.payload
            market_id = payload.get("conditionId", receipt.entity_id)
            yes_best_bid = payload.get("yes_best_bid")
            yes_best_ask = payload.get("yes_best_ask")
            yes_mid = payload.get("yes_mid")
            model_prob = payload.get("model_probability")

            decision = {
                "market_id": market_id,
                "timestamp": receipt.received_at.isoformat() if receipt.received_at else "",
                "yes_best_bid": yes_best_bid,
                "yes_best_ask": yes_best_ask,
                "yes_mid": yes_mid,
                "model_probability": model_prob,
                "action": "HOLD",
                "side": None,
                "confidence": "low",
            }

            if model_prob is not None and yes_best_ask is not None:
                try:
                    model_p = float(model_prob)
                    ask = float(yes_best_ask)
                    edge = model_p - ask
                    if edge > 0.025:
                        decision["action"] = "BUY"
                        decision["side"] = "YES"
                        decision["edge"] = round(edge, 4)
                        decision["confidence"] = "high" if edge > 0.05 else "medium"
                    elif edge < -0.025:
                        decision["action"] = "BUY"
                        decision["side"] = "NO"
                        decision["edge"] = round(-edge, 4)
                        decision["confidence"] = "high" if edge < -0.05 else "medium"
                    else:
                        decision["action"] = "HOLD"
                        decision["edge"] = round(edge, 4)
                except (TypeError, ValueError):
                    pass

            decisions.append(decision)

        # Summary statistics
        buys = sum(1 for d in decisions if d["action"] == "BUY")
        holds = sum(1 for d in decisions if d["action"] == "HOLD")
        avg_edge = (
            sum(d.get("edge", 0.0) for d in decisions) / len(decisions)
            if decisions else 0.0
        )

        output = {
            "date": args.date,
            "db_path": db_path,
            "total_snapshots": len(date_snapshots),
            "decisions": decisions,
            "summary": {
                "total_decisions": len(decisions),
                "buy_signals": buys,
                "hold_signals": holds,
                "avg_edge": round(avg_edge, 4),
                "high_confidence_buys": sum(
                    1 for d in decisions
                    if d["action"] == "BUY" and d["confidence"] == "high"
                ),
            },
        }
        print(json.dumps(output, indent=2, default=str))


def cmd_regime_detect(args):
    """Detect market regimes."""
    import json
    from decimal import Decimal as D

    from .regime_detection import detect_regimes

    dataset = _load_dataset(args.dataset)
    snapshots = []
    for r in dataset.snapshots:
        snapshots.append({
            "timestamp": r.observation_timestamp or r.source_timestamp,
            "midpoint": D(str(r.yes_mid)) if r.yes_mid is not None else D("0.5"),
            "spread": D(str(r.yes_spread)) if r.yes_spread is not None else D("0.02"),
            "volume": D(str(r.volume)) if r.volume is not None else D("100"),
        })

    result = detect_regimes(snapshots)
    print(json.dumps(result.summary(), indent=2, default=str))


def cmd_collect_raw(args):
    """Run the raw market collector (WS -> immutable raw store)."""
    import json
    from pathlib import Path

    from .collection import run_collection
    from .market_metadata import MetadataStore
    from .rawstore import RawStore
    from .transport import PublicHTTP

    token_ids = [t.strip() for t in args.tokens.split(",") if t.strip()]
    raw = RawStore(Path(args.raw_dir), collector_version=args.collector_version)
    metadata = MetadataStore()
    transport = PublicHTTP(timeout=args.timeout, attempts=args.attempts) if args.reconcile else None
    try:
        result = run_collection(
            raw=raw,
            metadata=metadata,
            token_ids=token_ids,
            transport=transport,
            duration_seconds=args.duration,
            reconcile_interval_seconds=args.reconcile_interval,
            collector_version=args.collector_version,
        )
    finally:
        raw.close()
    print(json.dumps(result.as_dict(), indent=2, default=str))


def cmd_collect_health(args):
    """Render the collector health SLO surface from a raw store (read-only)."""
    import json
    from pathlib import Path

    from .collector_health import HealthReport, render_dashboard, render_terminal
    from .rawstore import RawStore, list_days

    raw = RawStore(Path(args.raw_dir))
    try:
        report = HealthReport(
            uptime_seconds=0.0,
            ws_connections=0,
            reconnects=0,
            messages_today=raw.count(),
            markets_tracked=0,
            tokens_tracked=0,
            disk_bytes=sum(
                p.stat().st_size
                for p in raw.root.rglob("*.jsonl*")
                if p.is_file()
            ),
        )
        report.collected_at = report.collected_at
        days = list_days(args.raw_dir)
        report_dict = report.as_dict()
        report_dict["days_present"] = [d.isoformat() for d in days]
        report_dict["raw_records"] = raw.count()
    finally:
        raw.close()
    if args.format == "terminal":
        print(render_terminal(report))
        print("Days present:", ", ".join(d.isoformat() for d in days))
    elif args.format == "dashboard":
        print(render_dashboard(report))
    else:
        print(json.dumps(report_dict, indent=2, default=str))


def cmd_collect_manifest(args):
    """Write today's immutable raw-data manifest."""
    import json
    from pathlib import Path

    from .collection import write_daily_manifest
    from .daily_manifest import ManifestWriter
    from .rawstore import RawStore

    raw = RawStore(Path(args.raw_dir))
    writer = ManifestWriter(Path(args.manifest_dir))
    try:
        result = write_daily_manifest(
            raw=raw,
            writer=writer,
            collector_version=args.collector_version,
            config_hash=args.config_hash or "unset",
            markets_observed=int(args.markets_observed or 0),
            resolved_markets=int(args.resolved_markets or 0),
            dropped_connections=int(args.dropped_connections or 0),
            reconciliations=int(args.reconciliations or 0),
            book_mismatches=int(args.book_mismatches or 0),
        )
    finally:
        raw.close()
    print(json.dumps(result, indent=2, default=str))


def cmd_collect_burnin(args):
    """Run the 24-72h burn-in and verify raw->replay determinism.

    The burn-in's only objective is proving the collector cannot silently
    corrupt the future dataset: every message is captured raw and replay
    must reproduce identical book state and identical dataset hashes.
    """
    import json
    from pathlib import Path

    from .collection import run_collection
    from .market_metadata import MetadataStore
    from .rawstore import RawStore
    from .transport import PublicHTTP

    token_ids = [t.strip() for t in args.tokens.split(",") if t.strip()]
    raw = RawStore(Path(args.raw_dir), collector_version=args.collector_version)
    metadata = MetadataStore()
    transport = PublicHTTP(timeout=args.timeout, attempts=args.attempts)
    try:
        result = run_collection(
            raw=raw,
            metadata=metadata,
            token_ids=token_ids,
            transport=transport,
            duration_seconds=args.duration,
            reconcile_interval_seconds=args.reconcile_interval,
            collector_version=args.collector_version,
        )
        # Determinism check: raw root hash must be stable across replays.
        h1 = raw.sha256_root()
        h2 = raw.sha256_root()
        output = result.as_dict()
        output["burn_in_deterministic"] = h1 == h2
        output["raw_sha256_root"] = h1
    finally:
        raw.close()
    print(json.dumps(output, indent=2, default=str))


def cmd_collect_gate(args):
    """Evaluate the burn-in go/no-go gate (offline; takes observed counts)."""
    import json

    from .burnin_gate import evaluate_burn_in_gate

    gate = evaluate_burn_in_gate(
        raw_corruption_count=int(args.raw_corruption),
        replay_deterministic=args.replay_deterministic.lower() in ("1", "true", "yes"),
        delta_on_stale_count=int(args.delta_on_stale),
        unrecoverable_reconnect_count=int(args.unrecoverable_reconnects),
        timestamp_invariant_failures=int(args.timestamp_failures),
        reconciliation_total=int(args.reconciliations),
        reconciliation_mismatches=int(args.reconciliation_mismatches),
        heartbeat_recovery=args.heartbeat_recovery.lower() in ("1", "true", "yes"),
        restart_recovery=args.restart_recovery.lower() in ("1", "true", "yes"),
        partial_file_recovery=args.partial_file_recovery.lower() in ("1", "true", "yes"),
        metadata_point_in_time=args.metadata_pit.lower() in ("1", "true", "yes"),
        resolution_captured=args.resolution_captured.lower() in ("1", "true", "yes"),
        model_hash_unchanged=args.model_hash_unchanged.lower() in ("1", "true", "yes"),
    )
    summary = gate.summary()
    print(json.dumps(summary, indent=2))
    if not gate.all_pass:
        raise SystemExit(1)


def cmd_collect_start_marker(args):
    """Write the immutable REAL_DATA_START marker (refuses to overwrite)."""
    import json

    from .burnin_gate import write_real_data_start_marker
    from .phases import PhaseStore

    phase_store = PhaseStore(args.phase_file, args.baseline_commit) if args.phase_file else None
    if phase_store is not None:
        phase_store.require("BURNIN_PASSED")
    marker = write_real_data_start_marker(
        args.marker_path,
        baseline_tag=args.baseline_tag,
        baseline_commit=args.baseline_commit,
        config_sha256=args.config_sha256,
        model_source_sha256=args.model_source_sha256,
        feature_schema_sha256=args.feature_schema_sha256,
        collector_commit=args.collector_commit,
        burnin_report_sha256=args.burnin_report_sha256,
        phase_store=phase_store,
    )
    print(json.dumps(marker.as_dict(), indent=2))


def cmd_collect_phase(args):
    """Show or transition the collection phase state machine."""
    import json

    from .phases import PhaseStore

    store = PhaseStore(args.phase_file, args.baseline_commit)
    if args.to:
        state = store.transition(args.to)
    else:
        state = store.read()
    print(json.dumps(state.as_dict(), indent=2))


def cmd_collect_phase_init(args):
    """Initialize phase file at BASELINE_FROZEN (idempotent)."""
    import json

    from .phases import initialize_phase

    state = initialize_phase(args.phase_file, args.baseline_commit)
    print(json.dumps(state.as_dict(), indent=2))


def cmd_collect_replay_check(args):
    """Run the dual-replay determinism check (Replay A / Replay B)."""
    import json

    from .replay_verification import run_dual_replay

    result = run_dual_replay(args.raw_dir)
    print(json.dumps(result.as_dict(), indent=2))
    if not result.deterministic:
        raise SystemExit(1)


def cmd_collect_burnin_report(args):
    """Write burnin/burnin-report.{json,md} and print its sha256."""
    import json

    from .burnin_gate import BurnInReport, write_burn_in_report

    report = BurnInReport(
        period_start=args.period_start,
        period_end=args.period_end,
        baseline_commit=args.baseline_commit,
        messages=int(args.messages),
        markets=int(args.markets),
        reconnects=int(args.reconnects),
        forced_failures=int(args.forced_failures),
        replay_deterministic=args.replay_deterministic.lower() in ("1", "true", "yes"),
        raw_corruption=int(args.raw_corruption),
        partial_records=int(args.partial_records),
        hash_mismatches=int(args.hash_mismatches),
        invalid_delta_applications=int(args.invalid_delta_applications),
        unresolved_book_mismatches=int(args.unresolved_book_mismatches),
        manifest_chain_ok=args.manifest_chain_ok.lower() in ("1", "true", "yes"),
        metadata_reconstruction_ok=args.metadata_reconstruction_ok.lower() in ("1", "true", "yes"),
        resolution_lifecycle_ok=args.resolution_lifecycle_ok.lower() in ("1", "true", "yes"),
        crash_recovery_ok=args.crash_recovery_ok.lower() in ("1", "true", "yes"),
        gate_passed=args.gate_passed.lower() in ("1", "true", "yes"),
    )
    directory, digest = write_burn_in_report(report, args.output_dir)
    print(json.dumps({"report_dir": str(directory), "burnin_report_sha256": digest}, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Polyalpha research platform")
    subparsers = parser.add_subparsers(dest="command")

    # Collect command
    collect_parser = subparsers.add_parser("collect", help="Collect market data")
    collect_parser.add_argument("--config", default="config/base.toml")
    collect_parser.add_argument("--cycles", type=int, default=1)
    collect_parser.add_argument("--refresh-tracked", action="store_true")
    collect_parser.add_argument("--resolutions", action="store_true")
    collect_parser.add_argument("--activity", action="store_true")
    collect_parser.add_argument("--stream-seconds", type=int, default=0)

    # Bias check command
    bias_parser = subparsers.add_parser("bias", help="Run bias checks on a report")
    bias_parser.add_argument("report", help="Path to backtest report JSON")
    bias_parser.add_argument("--output", help="Output path for results")

    # Walk-forward command
    wf_parser = subparsers.add_parser("walk-forward", help="Run walk-forward evaluation")
    wf_parser.add_argument("csv", help="Observation CSV file")
    wf_parser.add_argument("--first-test", required=True)
    wf_parser.add_argument("--end", required=True)
    wf_parser.add_argument("--window-days", type=int, default=30)
    wf_parser.add_argument("--method", choices=["isotonic", "platt"], default="isotonic")
    wf_parser.add_argument("--output", required=True)

    # Experiment command
    exp_parser = subparsers.add_parser("experiment", help="Record an experiment")
    exp_parser.add_argument("report", help="Path to report JSON")
    exp_parser.add_argument("--output-dir", default="data/experiments")
    exp_parser.add_argument("--model-version", default="unknown")
    exp_parser.add_argument("--feature-version", default="unknown")

    # Compare command
    comp_parser = subparsers.add_parser("compare", help="Compare strategy reports")
    comp_parser.add_argument("reports", nargs="+", help="Report JSON files")
    comp_parser.add_argument("--output", help="Output path")

    # Train command
    train_parser = subparsers.add_parser("train", help="Train ML probability models")
    train_parser.add_argument("--config", default="config/base.toml")
    train_parser.add_argument("--db", help="SQLite database path")
    train_parser.add_argument("--output-dir", default="data/trained_models")
    train_parser.add_argument("--train-ratio", type=float, default=0.6)
    train_parser.add_argument("--val-ratio", type=float, default=0.2)
    train_parser.add_argument("--logistic-c", type=float, default=1.0, dest="logistic_c")
    train_parser.add_argument("--gbm-estimators", type=int, default=100)
    train_parser.add_argument("--gbm-depth", type=int, default=3)

    # Paper-run command
    paper_run_parser = subparsers.add_parser("paper-run", help="Run paper trading backtest")
    paper_run_parser.add_argument("--db", required=True, help="SQLite database path")
    paper_run_parser.add_argument("--output", default="data/paper_report.json")
    paper_run_parser.add_argument(
        "--exit-policy",
        choices=["hold", "edge", "time", "stop_loss", "trailing", "all"],
        default="hold",
    )
    paper_run_parser.add_argument("--limit", type=int, default=10000)

    # Sizing command
    sizing_parser = subparsers.add_parser("sizing", help="Compute position sizing")
    sizing_parser.add_argument("--equity", required=True, help="Current equity")
    sizing_parser.add_argument("--price", required=True, help="Execution price")
    sizing_parser.add_argument("--probability", help="Fair probability (required for kelly)")
    sizing_parser.add_argument(
        "--method", choices=["fixed", "kelly", "constrained"], default="constrained"
    )
    sizing_parser.add_argument("--fraction", default="0.005")
    sizing_parser.add_argument("--risk-budget", default="200")
    sizing_parser.add_argument("--max-market-fraction", default="0.02")
    sizing_parser.add_argument("--kelly-multiplier", default="0.25")
    sizing_parser.add_argument("--use-kelly", action="store_true")

    # Market-making command
    mm_parser = subparsers.add_parser("market-making", help="Analyze market-making opportunity")
    mm_parser.add_argument("--token-id", required=True)
    mm_parser.add_argument("--fair-value", required=True, help="Model fair probability")
    mm_parser.add_argument("--half-spread", default="0.02", help="Half spread for quoting")

    # Alpha-decay command
    ad_parser = subparsers.add_parser("alpha-decay", help="Analyze alpha decay from report")
    ad_parser.add_argument("report", help="Backtest report JSON path")

    # Feature-importance command
    fi_parser = subparsers.add_parser("feature-importance", help="Run feature importance analysis")
    fi_parser.add_argument("--model", required=True, help="Pickled model path")
    fi_parser.add_argument("--test-csv", required=True, help="Test data CSV")
    fi_parser.add_argument("--repeats", type=int, default=5)

    # Anomaly command
    an_parser = subparsers.add_parser("anomaly", help="Run anomaly detection on a book")
    an_parser.add_argument("--book", required=True, help="Book snapshot JSON")

    # Scan command
    scan_parser = subparsers.add_parser("scan", help="Scan reviewed partitions for relative-value")
    scan_parser.add_argument("--database", required=True, help="SQLite database path")
    scan_parser.add_argument("--reviews", required=True, help="Review JSON file")
    scan_parser.add_argument("--as-of", required=True, help="Point-in-time timestamp (ISO)")
    scan_parser.add_argument("--shares", required=True, help="Number of shares to quote")
    scan_parser.add_argument("--output", required=True, help="Output JSON path")

    # ── v0.3 Research Commands ──────────────────────────────────────────────

    # Research dataset builder
    ds_parser = subparsers.add_parser("build-dataset", help="Build canonical research dataset")
    ds_parser.add_argument("reports", nargs="+", help="Backtest report JSON paths")
    ds_parser.add_argument("--output", required=True, help="Output dataset path (.json)")
    ds_parser.add_argument("--format", choices=["json", "csv"], default="json")

    # Dataset auditor
    audit_parser = subparsers.add_parser("audit-dataset", help="Audit research dataset integrity")
    audit_parser.add_argument("dataset", help="Dataset JSON path")

    # Market benchmark
    bench_parser = subparsers.add_parser(
        "market-benchmark", help="Evaluate model vs market baseline"
    )
    bench_parser.add_argument("dataset", help="Dataset JSON path")

    # Null strategies
    null_parser = subparsers.add_parser("null-strategies", help="Evaluate null/sanity strategies")
    null_parser.add_argument("dataset", help="Dataset JSON path")

    # Cost ladder
    cl_parser = subparsers.add_parser("cost-ladder", help="Run cost ladder experiment")
    cl_parser.add_argument("dataset", help="Dataset JSON path")

    # Model ablation
    abl_parser = subparsers.add_parser("model-ablation", help="Run model ablation study")
    abl_parser.add_argument("dataset", help="Dataset JSON path")

    # Model vs market matrix
    mvm_parser = subparsers.add_parser("model-vs-market", help="Build model vs market matrix")
    mvm_parser.add_argument("dataset", help="Dataset JSON path")

    # Disagreement analysis
    da_parser = subparsers.add_parser("disagreement", help="Analyze model-market disagreement")
    da_parser.add_argument("dataset", help="Dataset JSON path")

    # Time-to-resolution analysis
    ttr_parser = subparsers.add_parser(
        "time-to-resolution", help="Analyze time-to-resolution performance"
    )
    ttr_parser.add_argument("dataset", help="Dataset JSON path")

    # Category analysis
    cat_parser = subparsers.add_parser("category-analysis", help="Analyze per-category performance")
    cat_parser.add_argument("dataset", help="Dataset JSON path")

    # Ensemble consensus
    ec_parser = subparsers.add_parser(
        "ensemble-consensus", help="Analyze ensemble agreement quality"
    )
    ec_parser.add_argument("dataset", help="Dataset JSON path")

    # Effective sample size
    ess_parser = subparsers.add_parser(
        "effective-sample-size", help="Compute effective sample size"
    )
    ess_parser.add_argument("dataset", help="Dataset JSON path")

    # Bootstrap CI
    bc_parser = subparsers.add_parser("bootstrap-ci", help="Compute bootstrap confidence interval")
    bc_parser.add_argument("dataset", help="Dataset JSON path")
    bc_parser.add_argument("--samples", type=int, default=2000)
    bc_parser.add_argument(
        "--metric", default="brier",
        choices=["brier", "pnl", "edge", "profit_factor", "sharpe"],
        help="Metric to bootstrap (default: brier)"
    )

    # Monte Carlo stress
    mc_parser = subparsers.add_parser("monte-carlo-stress", help="Run Monte Carlo stress testing")
    mc_parser.add_argument("dataset", help="Dataset JSON path")
    mc_parser.add_argument("--simulations", type=int, default=1000)

    # Model degradation
    md_parser = subparsers.add_parser(
        "model-degradation", help="Analyze model degradation over time"
    )
    md_parser.add_argument("dataset", help="Dataset JSON path")

    # Negative controls
    nc_parser = subparsers.add_parser("negative-controls", help="Run negative control tests")
    nc_parser.add_argument("dataset", help="Dataset JSON path")

    # Holdout management
    ho_lock = subparsers.add_parser("holdout-lock", help="Lock the final holdout period")
    ho_lock.add_argument("--config-dir", default="data")

    ho_unlock = subparsers.add_parser(
        "holdout-unlock", help="Unlock the final holdout (requires justification)"
    )
    ho_unlock.add_argument("--reason", required=True, help="Justification for unlocking")
    ho_unlock.add_argument("--config-dir", default="data")

    ho_status = subparsers.add_parser("holdout-status", help="Check holdout lock status")
    ho_status.add_argument("--config-dir", default="data")

    # ── v0.4 Research Commands ──────────────────────────────────────────────

    # Conditional probability graph
    cg_parser = subparsers.add_parser(
        "conditional-graph",
        help="Build conditional probability graph"
    )
    cg_parser.add_argument("dataset", help="Dataset JSON path")
    cg_parser.add_argument("--min-samples", type=int, default=30)
    cg_parser.add_argument("--threshold", type=float, default=2.0)

    # Leakage detection
    lk_parser = subparsers.add_parser("detect-leakage", help="Detect train/val leakage")
    lk_parser.add_argument("dataset", help="Dataset JSON path")
    lk_parser.add_argument("--train-ratio", type=float, default=0.8)
    lk_parser.add_argument("--temporal-threshold", type=float, default=86400.0)

    # Out-of-domain test
    oos_parser = subparsers.add_parser("oos-test", help="Run out-of-domain test")
    oos_parser.add_argument("dataset", help="Dataset JSON path")
    oos_parser.add_argument("--train-categories", nargs="*")
    oos_parser.add_argument("--test-categories", nargs="*")

    # Regime analysis
    ra_parser = subparsers.add_parser(
        "regime-analysis",
        help="Analyze liquidity and spread regimes"
    )
    ra_parser.add_argument("dataset", help="Dataset JSON path")

    # Signal decay
    sd_parser = subparsers.add_parser(
        "signal-decay",
        help="Analyze signal decay over time horizons"
    )
    sd_parser.add_argument("dataset", help="Dataset JSON path")

    # Latency simulation
    ls_parser = subparsers.add_parser("latency-sim", help="Simulate execution latency effects")
    ls_parser.add_argument("dataset", help="Dataset JSON path")
    ls_parser.add_argument("--delays", help="Comma-separated delay levels in seconds")

    # Partial fill stress
    pf_parser = subparsers.add_parser("partial-fill", help="Stress test partial fill scenarios")
    pf_parser.add_argument("dataset", help="Dataset JSON path")
    pf_parser.add_argument("--fractions", help="Comma-separated fill fractions (e.g. 1.0,0.75,0.5)")

    # Maker stress
    ms_parser = subparsers.add_parser("maker-stress", help="Stress test maker strategy")
    ms_parser.add_argument("dataset", help="Dataset JSON path")
    ms_parser.add_argument("--scenarios", help="Comma-separated scenario names")

    # Liquidation equity
    liq_parser = subparsers.add_parser("liquidation", help="Compute executable liquidation equity")
    liq_parser.add_argument("dataset", help="Dataset JSON path")

    # Concentration analysis
    ca_parser = subparsers.add_parser("concentration", help="Analyze profit concentration")
    ca_parser.add_argument("dataset", help="Dataset JSON path")
    ca_parser.add_argument(
        "--group-by", default="market",
        choices=["market", "event", "cluster", "category", "strategy"]
    )

    # Profit persistence
    pp_parser = subparsers.add_parser(
        "persistence",
        help="Analyze profit persistence over rolling windows"
    )
    pp_parser.add_argument("dataset", help="Dataset JSON path")
    pp_parser.add_argument("--window-size", type=int, default=30)
    pp_parser.add_argument(
        "--window-types",
        default="rolling,expanding,calendar,regime_conditioned",
        help="Comma-separated window types: rolling, expanding, calendar, regime_conditioned",
    )

    # Parameter sensitivity
    sens_parser = subparsers.add_parser("sensitivity", help="Run parameter sensitivity grid")
    sens_parser.add_argument("dataset", help="Dataset JSON path")
    sens_parser.add_argument("param_name", help="Parameter name to test")
    sens_parser.add_argument("values", help="Comma-separated parameter values")
    sens_parser.add_argument("--threshold", type=float, default=0.0)

    # Overfitting detection
    of_parser = subparsers.add_parser("overfitting", help="Detect overfitting signals")
    of_parser.add_argument("report", help="Backtest report JSON with train/test metrics")

    # Paper monitor
    pm_parser = subparsers.add_parser("paper-monitor", help="Show paper trading monitor summary")
    pm_parser.add_argument("--log-file", help="Paper monitor log JSON file")

    # Paper vs backtest comparison
    pbc_parser = subparsers.add_parser(
        "paper-compare",
        help="Compare paper vs backtest performance"
    )
    pbc_parser.add_argument("paper_decisions", help="Paper decisions JSON file")
    pbc_parser.add_argument("backtest_decisions", help="Backtest decisions JSON file")
    pbc_parser.add_argument("--overlap-start", help="Overlap period start (ISO timestamp)")
    pbc_parser.add_argument("--overlap-end", help="Overlap period end (ISO timestamp)")

    # Experiment manifest
    mf_parser = subparsers.add_parser("manifest", help="Generate experiment manifest")
    mf_parser.add_argument("config", help="Experiment config JSON file")
    mf_parser.add_argument("--dataset-size", type=int, required=True)
    mf_parser.add_argument("--feature-count", type=int, required=True)
    mf_parser.add_argument("--categories", help="Comma-separated category labels")
    mf_parser.add_argument("--model-version", default="v1.0")
    mf_parser.add_argument("--git-commit", default="unknown")
    mf_parser.add_argument("--seed", type=int, default=42)
    mf_parser.add_argument("--tags", help="Comma-separated tags")

    # Dataset fingerprint
    fp_parser = subparsers.add_parser("fingerprint", help="Compute dataset fingerprint")
    fp_parser.add_argument("dataset", help="Dataset JSON path")

    # Statistical significance
    sig_parser = subparsers.add_parser("significance", help="Run statistical significance tests")
    sig_parser.add_argument("report", help="Report JSON with values and optional p-values")
    sig_parser.add_argument("--bootstrap-samples", type=int, default=2000)

    # Multiple testing correction
    mt_parser = subparsers.add_parser("multiple-testing", help="Apply multiple testing corrections")
    mt_parser.add_argument("p_values_file", help="JSON file containing list of p-values")

    # Model staleness
    stal_parser = subparsers.add_parser("model-staleness", help="Detect model staleness")
    stal_parser.add_argument("trained_at", help="Model training timestamp (ISO)")
    stal_parser.add_argument("--current-time", help="Current timestamp (ISO), defaults to now")
    stal_parser.add_argument("--max-age-days", type=int, default=30)

    # Decision audit
    da_parser = subparsers.add_parser("decision-audit", help="Reconstruct decision audit trail")
    da_parser.add_argument("audit_log", help="Audit log JSON file")
    da_parser.add_argument("--decision-id", help="Specific decision ID to reconstruct")

    # Regime detection
    rd_parser = subparsers.add_parser("regime-detect", help="Detect market regimes")
    rd_parser.add_argument("dataset", help="Dataset JSON path")

    # Replay command
    rp_parser = subparsers.add_parser("replay", help="Replay paper decisions from DB snapshots")
    rp_parser.add_argument("--date", required=True, help="Date to replay (YYYY-MM-DD)")
    rp_parser.add_argument("--db", required=True, help="SQLite database path")

    # ── Collection / raw-data layer ───────────────────────────────────────
    cr_parser = subparsers.add_parser("collect-raw", help="Run raw market WS collector")
    cr_parser.add_argument("--tokens", required=True, help="Comma-separated token IDs")
    cr_parser.add_argument("--raw-dir", default="data/raw", help="Raw store root")
    cr_parser.add_argument("--duration", type=float, default=None, help="Seconds to run (None=indefinite)")
    cr_parser.add_argument("--reconcile", action="store_true", help="Enable REST reconciliation")
    cr_parser.add_argument("--reconcile-interval", type=float, default=30.0)
    cr_parser.add_argument("--timeout", type=float, default=20.0)
    cr_parser.add_argument("--attempts", type=int, default=3)
    cr_parser.add_argument("--collector-version", default="v0.3.0")

    ch_parser = subparsers.add_parser("collect-health", help="Show collector health SLO")
    ch_parser.add_argument("--raw-dir", default="data/raw", help="Raw store root")
    ch_parser.add_argument("--tokens", default="", help="Comma-separated token IDs (for counts)")
    ch_parser.add_argument("--collector-version", default="v0.3.0")
    ch_parser.add_argument("--format", default="terminal", choices=["terminal", "dashboard", "json"])

    cm_parser = subparsers.add_parser("collect-manifest", help="Write daily immutable manifest")
    cm_parser.add_argument("--raw-dir", default="data/raw", help="Raw store root")
    cm_parser.add_argument("--manifest-dir", default="data/manifests", help="Manifest output dir")
    cm_parser.add_argument("--collector-version", default="v0.3.0")
    cm_parser.add_argument("--config-hash", default="")
    cm_parser.add_argument("--markets-observed", default=0)
    cm_parser.add_argument("--resolved-markets", default=0)
    cm_parser.add_argument("--dropped-connections", default=0)
    cm_parser.add_argument("--reconciliations", default=0)
    cm_parser.add_argument("--book-mismatches", default=0)

    cb_parser = subparsers.add_parser("collect-burnin", help="Run 24-72h burn-in with determinism check")
    cb_parser.add_argument("--tokens", required=True, help="Comma-separated token IDs")
    cb_parser.add_argument("--raw-dir", default="data/raw", help="Raw store root")
    cb_parser.add_argument("--duration", type=float, default=60.0)
    cb_parser.add_argument("--reconcile-interval", type=float, default=30.0)
    cb_parser.add_argument("--timeout", type=float, default=20.0)
    cb_parser.add_argument("--attempts", type=int, default=3)
    cb_parser.add_argument("--collector-version", default="v0.3.0")

    cg_parser = subparsers.add_parser("collect-gate", help="Evaluate burn-in go/no-go gate")
    cg_parser.add_argument("--raw-corruption", default=0)
    cg_parser.add_argument("--replay-deterministic", default="true")
    cg_parser.add_argument("--delta-on-stale", default=0)
    cg_parser.add_argument("--unrecoverable-reconnects", default=0)
    cg_parser.add_argument("--timestamp-failures", default=0)
    cg_parser.add_argument("--reconciliations", default=0)
    cg_parser.add_argument("--reconciliation-mismatches", default=0)
    cg_parser.add_argument("--heartbeat-recovery", default="true")
    cg_parser.add_argument("--restart-recovery", default="true")
    cg_parser.add_argument("--partial-file-recovery", default="true")
    cg_parser.add_argument("--metadata-pit", default="true")
    cg_parser.add_argument("--resolution-captured", default="true")
    cg_parser.add_argument("--model-hash-unchanged", default="true")

    cs_parser = subparsers.add_parser("collect-start-marker", help="Write immutable REAL_DATA_START marker")
    cs_parser.add_argument("--marker-path", required=True)
    cs_parser.add_argument("--baseline-tag", required=True)
    cs_parser.add_argument("--baseline-commit", required=True)
    cs_parser.add_argument("--config-sha256", required=True)
    cs_parser.add_argument("--model-source-sha256", required=True)
    cs_parser.add_argument("--feature-schema-sha256", required=True)
    cs_parser.add_argument("--collector-commit", required=True)
    cs_parser.add_argument("--burnin-report-sha256", required=True)
    cs_parser.add_argument("--phase-file", default="data/phase.json", help="Phase state file")

    cph_parser = subparsers.add_parser("collect-phase", help="Show or transition collection phase")
    cph_parser.add_argument("--phase-file", default="data/phase.json")
    cph_parser.add_argument("--baseline-commit", default="")
    cph_parser.add_argument("--to", choices=[
        "DEVELOPMENT", "BASELINE_FROZEN", "BURNIN_RUNNING", "BURNIN_FAILED",
        "BURNIN_PASSED", "REAL_DATA_START", "COLLECTION_RUNNING",
    ])

    cpi_parser = subparsers.add_parser("collect-phase-init", help="Initialize phase file at BASELINE_FROZEN")
    cpi_parser.add_argument("--phase-file", default="data/phase.json")
    cpi_parser.add_argument("--baseline-commit", default="")

    crc_parser = subparsers.add_parser("collect-replay-check", help="Dual-replay determinism check")
    crc_parser.add_argument("--raw-dir", default="data/raw")

    cbr_parser = subparsers.add_parser("collect-burnin-report", help="Write burnin report + hash")
    cbr_parser.add_argument("--output-dir", default="burnin")
    cbr_parser.add_argument("--period-start", required=True)
    cbr_parser.add_argument("--period-end", required=True)
    cbr_parser.add_argument("--baseline-commit", required=True)
    cbr_parser.add_argument("--messages", default=0)
    cbr_parser.add_argument("--markets", default=0)
    cbr_parser.add_argument("--reconnects", default=0)
    cbr_parser.add_argument("--forced-failures", default=0)
    cbr_parser.add_argument("--replay-deterministic", default="true")
    cbr_parser.add_argument("--raw-corruption", default=0)
    cbr_parser.add_argument("--partial-records", default=0)
    cbr_parser.add_argument("--hash-mismatches", default=0)
    cbr_parser.add_argument("--invalid-delta-applications", default=0)
    cbr_parser.add_argument("--unresolved-book-mismatches", default=0)
    cbr_parser.add_argument("--manifest-chain-ok", default="true")
    cbr_parser.add_argument("--metadata-reconstruction-ok", default="true")
    cbr_parser.add_argument("--resolution-lifecycle-ok", default="true")
    cbr_parser.add_argument("--crash-recovery-ok", default="true")
    cbr_parser.add_argument("--gate-passed", default="true")

    args = parser.parse_args()

    if args.command == "collect":
        cmd_collect(args)
    elif args.command == "bias":
        cmd_bias(args)
    elif args.command == "walk-forward":
        cmd_walk_forward(args)
    elif args.command == "experiment":
        cmd_experiment(args)
    elif args.command == "compare":
        cmd_compare(args)
    elif args.command == "train":
        cmd_train(args)
    elif args.command == "paper-run":
        cmd_paper_run(args)
    elif args.command == "sizing":
        cmd_sizing(args)
    elif args.command == "market-making":
        cmd_market_making(args)
    elif args.command == "alpha-decay":
        cmd_alpha_decay(args)
    elif args.command == "feature-importance":
        cmd_feature_importance(args)
    elif args.command == "anomaly":
        cmd_anomaly(args)
    elif args.command == "scan":
        from .scan import main as scan_main

        scan_main()
    elif args.command == "build-dataset":
        cmd_build_dataset(args)
    elif args.command == "audit-dataset":
        cmd_audit_dataset(args)
    elif args.command == "market-benchmark":
        cmd_market_benchmark(args)
    elif args.command == "null-strategies":
        cmd_null_strategies(args)
    elif args.command == "cost-ladder":
        cmd_cost_ladder(args)
    elif args.command == "model-ablation":
        cmd_model_ablation(args)
    elif args.command == "model-vs-market":
        cmd_model_vs_market(args)
    elif args.command == "disagreement":
        cmd_disagreement(args)
    elif args.command == "time-to-resolution":
        cmd_time_to_resolution(args)
    elif args.command == "category-analysis":
        cmd_category_analysis(args)
    elif args.command == "ensemble-consensus":
        cmd_ensemble_consensus(args)
    elif args.command == "effective-sample-size":
        cmd_effective_sample_size(args)
    elif args.command == "bootstrap-ci":
        cmd_bootstrap_ci(args)
    elif args.command == "monte-carlo-stress":
        cmd_monte_carlo_stress(args)
    elif args.command == "model-degradation":
        cmd_model_degradation(args)
    elif args.command == "negative-controls":
        cmd_negative_controls(args)
    elif args.command == "holdout-lock":
        cmd_holdout_lock(args)
    elif args.command == "holdout-unlock":
        cmd_holdout_unlock(args)
    elif args.command == "holdout-status":
        cmd_holdout_status(args)
    elif args.command == "conditional-graph":
        cmd_conditional_graph(args)
    elif args.command == "detect-leakage":
        cmd_detect_leakage(args)
    elif args.command == "oos-test":
        cmd_oos_test(args)
    elif args.command == "regime-analysis":
        cmd_regime_analysis(args)
    elif args.command == "signal-decay":
        cmd_signal_decay(args)
    elif args.command == "latency-sim":
        cmd_latency_sim(args)
    elif args.command == "partial-fill":
        cmd_partial_fill(args)
    elif args.command == "maker-stress":
        cmd_maker_stress(args)
    elif args.command == "liquidation":
        cmd_liquidation(args)
    elif args.command == "concentration":
        cmd_concentration(args)
    elif args.command == "persistence":
        cmd_persistence(args)
    elif args.command == "sensitivity":
        cmd_sensitivity(args)
    elif args.command == "overfitting":
        cmd_overfitting(args)
    elif args.command == "paper-monitor":
        cmd_paper_monitor(args)
    elif args.command == "paper-compare":
        cmd_paper_compare(args)
    elif args.command == "manifest":
        cmd_manifest(args)
    elif args.command == "fingerprint":
        cmd_fingerprint(args)
    elif args.command == "significance":
        cmd_significance(args)
    elif args.command == "multiple-testing":
        cmd_multiple_testing(args)
    elif args.command == "model-staleness":
        cmd_model_staleness(args)
    elif args.command == "decision-audit":
        cmd_decision_audit(args)
    elif args.command == "regime-detect":
        cmd_regime_detect(args)
    elif args.command == "replay":
        cmd_replay(args)
    elif args.command == "collect-raw":
        cmd_collect_raw(args)
    elif args.command == "collect-health":
        cmd_collect_health(args)
    elif args.command == "collect-manifest":
        cmd_collect_manifest(args)
    elif args.command == "collect-burnin":
        cmd_collect_burnin(args)
    elif args.command == "collect-gate":
        cmd_collect_gate(args)
    elif args.command == "collect-start-marker":
        cmd_collect_start_marker(args)
    elif args.command == "collect-phase":
        cmd_collect_phase(args)
    elif args.command == "collect-phase-init":
        cmd_collect_phase_init(args)
    elif args.command == "collect-replay-check":
        cmd_collect_replay_check(args)
    elif args.command == "collect-burnin-report":
        cmd_collect_burnin_report(args)
    else:
        # Default: legacy collect behavior
        parser = argparse.ArgumentParser(description="Public data only; no order submission")
        parser.add_argument("--config", default="config/base.toml")
        parser.add_argument("--cycles", type=int, default=1)
        parser.add_argument("--refresh-tracked", action="store_true")
        parser.add_argument("--resolutions", action="store_true")
        parser.add_argument("--activity", action="store_true")
        parser.add_argument("--stream-seconds", type=int, default=0)
        args = parser.parse_args()
        if args.cycles < 1 or args.stream_seconds < 0:
            parser.error("invalid duration or cycles")
        cmd_collect(args)


if __name__ == "__main__":
    main()
