# Polyalpha

A production-oriented quantitative prediction-market trading system under empirical validation. The implementation starts with point-in-time research, execution-aware backtesting, and immutable forward market-data capture, then adds shadow/paper execution, rigorous risk controls, and a staged path toward operator-approved live deployment.

**Execution mode:** current execution is `RESEARCH` / `PAPER` / `SHADOW` only. The target architecture is *live-trading-ready* — an execution abstraction (`OrderIntent` → `ExecutionGateway`), a pre-trade safety gate, kill switch, order-state reconciliation, and idempotency are in place — but **no autonomous real-money wagering is implemented**. Live deployment requires empirical validation, compliance checks, and explicit operator approval, and remains behind a deliberate human approval boundary.

**Alpha status: UNKNOWN** — not positive, not negative — until prospective evidence establishes otherwise.

**Venues.** Production target is **Polymarket US** (CFTC-regulated, US-eligible); **Kalshi** is a sibling US-accessible venue. **Polymarket International** (Gamma/CLOB, `docs.polymarket.com`) is a *research-data* lineage only — its collector is never an executable path. The split is hard-coded in `src/polyalpha/venue.py` (`is_executable` / `is_research_only`), never left to convention.

## Audit manifest (snapshot — regenerate with `scripts/repo_manifest.py` or update on release)

| Field | Value |
|---|---|
| Collection phase | `US_COLLECTION_RUNNING` (forward evidence) |
| Venue | Polymarket US (executable target); Kalshi (sibling); International (research-data only) |
| Alpha status | UNKNOWN |
| Executable modes | RESEARCH / PAPER / SHADOW / LIVE_READY (no live adapter wired) |
| Tests | 1,891 passed, 1 skipped, 5 numerical subtests |
| Live data streams | books (2s snapshots) + fills (explicit maker/taker) |
| Evidence gate | ~5/30 days, 0/200 settled, 4/200 independent clusters |
| Known gaps | no validated edge; A6/A8 need fill accumulation; A7/A9 dropped (US-only); sub-second reaction horizons pending finer timestamp precision |

## Quick start

From the repository root, using Python 3.12+:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,research,stream]"
.\.venv\Scripts\python.exe -m pytest -q
```

A local `.venv` is already installed in this workspace. `requirements-dev.lock` records the tested dependency versions; core REST collection and numerical accounting use only the standard library. Platt calibration uses scikit-learn; public streaming uses websockets.

Collect one bounded page of market metadata and both outcome books:

```powershell
.\.venv\Scripts\python.exe -m polyalpha.cli --config config/base.toml --cycles 1
```

Collect trades, terminal resolution status, lifecycle updates and a short public stream:

```powershell
.\.venv\Scripts\python.exe -m polyalpha.cli --config config/smoke.toml --refresh-tracked --activity --stream-seconds 10
```

Run two bounded paper cycles:

```powershell
.\.venv\Scripts\python.exe -m polyalpha.paper --config config/smoke.toml --clusters config/clusters.json --cycles 2
```

The supplied cluster map is empty, so new paper entries are rejected. Populate it only after reviewing related-event exposure and resolution wording. The paper runner refreshes terminal resolution status each cycle. No strategy optimization or empirical profitability is implied by the diagnostic baseline.

Replay a fixed historical cutoff:

```powershell
.\.venv\Scripts\python.exe -m polyalpha.research --database data/research.sqlite3 --clusters config/clusters.json --as-of "2026-09-12T06:00:00+00:00" --output data/replay.json
```

All report outputs are write-once. Choose a new filename for each replay. The paper runner generates a fresh UUID filename per cycle. Installed entry points are `polyalpha`, `polyalpha-paper`, `polyalpha-replay`, and `polyalpha-evaluate`.

## Architecture and repository

The network layer performs public GET requests with bounded retries. The collector stores raw decoded JSON before normalization. Immutable domain records carry separate source and receipt times. Append-only storage provides the historical information boundary. The execution layer walks depth and applies known market-specific fees. Accounting conserves cash and cost basis. Risk aggregates exposure before a forecast can cause a simulated order. Replay queues orders until a later observed book arrives after latency, then checks eligibility and economics again.

```text
polybot/
  pyproject.toml, requirements-dev.lock
  README.md, .gitignore, .env.example
  Dockerfile, docker-compose.yml, docker-compose.postgres.yml, .dockerignore
  config/
    base.toml, smoke.toml, clusters.json
    base.yaml, research.yaml, paper.yaml
  sql/schema.sql
  docs/
    ROADMAP.md
    RESEARCH_CONTRACTS.md
  scripts/verify_postgres.py
  examples/
    synthetic_replay.py
    synthetic_calibration.csv
  src/polyalpha/
    domain.py             Immutable market/book/level records and decimal validation
    parsing.py            Strict Gamma and CLOB normalization
    transport.py          Public GET transport, timeouts and retries
    collector.py          Discovery, both-token books, raw receipts and quality logs
    lifecycle.py          Refresh tracked markets, including closure; price-history adapter
    activity.py           Data API v2 trades and terminal binary payout ingestion
    stream.py             WebSocket snapshots, level replacement/removal and invalidation
    quality.py            Eligibility, timing, spread, depth rejection and quality scoring
    storage.py            SQLite append-only archive and point-in-time retrieval
    postgres.py           Optional PostgreSQL implementation of the receipt-store interface
    archive.py            Write-once JSONL export and chronological import
    execution.py          Fee schedules, depth walking, partial fills and consumed liquidity
    portfolio.py          Cash, fee-inclusive basis, realized PnL, settlement and valuation
    risk.py               Market/event/cluster/category/total caps and loss breakers
    resolution.py         Reviewed contract-definition hashes and resolution penalties
    forecasting.py        Model interface, features, diagnostic baseline, ensemble and calibration wrapper
    calibration.py        Isotonic/Platt fitting, reliability metrics and walk-forward evaluation
    relative_value.py     Reviewed partition, exclusivity and implication diagnostics
    external.py           External fact/source interface with availability timestamps
    csv_source.py         CSV facts with explicit public-source provenance
    backtest.py           Receipt-time replay, delayed entries/exits and result journals
    maker.py              Separate queue-ahead research approximation
    performance.py        Drawdown, ratios, Brier, log loss, trade/edge/holding metrics
    monitoring.py         Experiment provenance, calibration-drift and feature-drift detection
    compliance.py         Read-only geographic availability query
    config.py             Configuration-to-engine construction
    cli.py                Bounded public-data collection
    paper.py              Repeatable paper sessions from recorded history
    research.py           Fixed-cutoff replay command
    evaluate.py           Calibration CSV and walk-forward command
    uncertainty.py        Bootstrap, disagreement, spread and ensemble uncertainty estimation
    expected_value.py     Full expected-value decomposition and Kelly criterion
    sizing.py             Fixed fractional and Kelly-constrained position sizing
    anomaly.py            Order-book anomaly detection (spread, imbalance, liquidity)
    edge_metrics.py       Edge realization ratio, realized edge stats, bucket analysis
    feature_importance.py Permutation importance and leakage detection
    alpha_decay.py        Signal decay analysis across time horizons
    pipeline.py           News/information pipeline with deduplication
    market_making.py      Inventory management, adverse selection modeling
    walk_forward.py       Chronological walk-forward with cluster purging
    strategies.py         Multi-strategy comparison framework
    bias.py               Systematic bias checks for backtest integrity
    experiments.py        Experiment tracking with git commit, source/data hashes
    compliance.py         Geographic compliance, jurisdiction checks, restricted regions
    train.py              Model training pipeline for calibrators
    models/
      __init__.py         Model package exports
      market_prior.py     Component A: market price as Bayesian prior
      microstructure.py   Component B: order-book dynamics model
      fundamental.py      Component C: Bayesian external-data model (category priors + likelihood-ratio updates + source weighting)
    features/
      __init__.py
      orderbook.py        20+ microstructure features (imbalance, microprice, slope, HHI, momentum)
      temporal.py         Time-until-resolution, urgency, hour-of-day features
      market.py           Liquidity/volume scoring, fee/resolution presence flags
      cross_market.py     Cross-market dispersion and partition deviation features
      external.py         External-fact feature aggregation stub
  notebooks/
    __init__.py
    market_analysis.py    Market universe analysis
    calibration_analysis.py  Calibration curve and Brier analysis
    relative_value_analysis.py  Cross-market relationship analysis
    strategy_evaluation.py  Strategy performance comparison
  scripts/
    collect_markets.py    Collect market metadata from Polymarket API
    collect_books.py      Collect order books for tracked markets
    build_dataset.py      Build training dataset from collected data
    backtest.py           Run backtest engine with performance metrics
    run_paper.py          Run paper trading simulation
    train.py              Train probability calibrators
    verify.py             Repeatable verification (pytest, lint, format)
    verify_postgres.py    PostgreSQL integration verification
  tests/
    test_foundation.py
    test_execution.py
    test_research.py
    test_integration.py
    test_activity.py
    test_regressions.py
    test_features.py
    test_uncertainty.py
    test_expected_value.py
    test_enhanced.py
    test_comprehensive.py
    test_domain_extensions.py
    test_numerical_properties.py
    test_new_modules.py
    test_models_and_analysis.py
```

## What has been verified

- 1,891 tests pass (1 skipped), including five numerical subtests. They cover parser failures, outcome mapping, timestamps, append-only enforcement, future-data exclusion, cursor handling, fees, both requested VWAP examples, partial fills, liquidity depletion, duplicate-fill rejection, cash/basis conservation, settlement, cluster limits, loss breakers, delayed exits, stream invalidation, calibration leakage controls, payout units, resolution changes, configuration, order-book features, uncertainty estimation, expected value decomposition, Kelly sizing, anomaly detection, quality scoring, performance metrics, information pipeline, market making, walk-forward evaluation, strategy comparison, bias detection, experiment tracking, calibration/feature drift detection, market snapshots, trading signals, model ensembles, correlation analysis, event clustering, tail risk metrics, temperature scaling, correlation-adjusted risk, property-based numerical tests (fee symmetry, Kelly bounds, drawdown invariants), microstructure model, market prior model, fundamental model (Bayesian external-data), feature importance (permutation), alpha decay analysis, resolution quality scoring with temporal decay, edge realization metrics, geographic compliance, and comprehensive integration tests.
- Live Gamma/CLOB collection stored 100 markets and 200 REST books with zero REST errors. The one-page universe was explicitly marked truncated.
- A bounded public WebSocket session persisted 292 raw stream events and 384 additional reconstructed snapshots without a reported stream gap. This does not establish absence of undetectable transport loss.
- A separate two-cycle live paper smoke test collected four books and refreshed two markets per cycle; it produced zero fills with the empty review map.
- A Data API v2 smoke test stored 200 trade rows from two bounded pages. Neither selected market supplied a terminal settlement.
- The synthetic replay and calibration command examples run. Their outcomes are invented fixtures for mechanics tests, never evidence of alpha.
- Docker image build and a bounded live collector run passed. A disposable PostgreSQL 17 instance verified idempotent schema initialization, point-in-time queries, durable writes after reads, and append-only enforcement; the test container was removed.

## Accounting and execution assumptions

Actual asks and bids determine fills. Decimal VWAP includes walking depth; depth impact is reported separately and is not subtracted twice from edge. Fee schedules must explicitly be fee-free or provide the supported market-specific rate, exponent 1 and taker-only flag. Unknown or unsupported fees reject entry.

The implemented fee is `shares * rate * price * (1-price)`, rounded to five decimal places per consumed price level. Fees are represented as a cash-equivalent charge on both sides. This is a paper accounting approximation, not wallet-level token/settlement replication. Per-counterparty rounding may differ because aggregate depth does not identify every matched order.

An order fills only on a subsequent recorded book after configured latency. No later book means no fill. The simulator permanently subtracts its consumed quantity at each token/side/price during a replay; it never guesses replenishment. This is intentionally pessimistic and can understate long-run capacity. Maker research is separate and requires observed aggressive trade volume to consume queue-ahead before any simulated fill.

Positions are long-only. Buy fees enter cost basis. Sells remove proportional basis. Explicit payouts settle remaining holdings. Midpoint valuation is a diagnostic; executable liquidation uses bid depth and exit fees. If some holdings cannot be priced, reports mark valuation incomplete, set ordinary net PnL to null, and show a separately named lower bound. Incomplete valuations block new entries and do not independently trip a drawdown breaker. Observed drawdown uses complete valuation observations only; the ordinary drawdown field is null when valuation gaps exist. Liquidation also subtracts previously consumed sell depth.

`hold` is the default exit policy. `edge` additionally queues delayed exits when model value falls to the executable sale threshold. A latched loss breaker can queue risk exits when required market data and metadata remain valid. Ordinary minimum-size and data rules still apply; immediate liquidation is never guaranteed.

Risk limits use gross fee-inclusive cost basis without offsetting related YES/NO positions. This caps invested capital, not every possible factor loading. The supplied values are conservative starting assumptions, not optimized parameters. Semantic clusters and reviewed resolution assessments are required in standard CLI paper runs; no automatic independence assumption is made.

## Expected value and position sizing

The central quantity is estimated fair probability q = P(event resolves YES | currently available information), not predicted future market price. For each binary contract, the system compares q against the executable market price.

```
net_edge = fair_probability
           - execution_price (VWAP from depth)
           - estimated_fees_per_share
           - expected_slippage
           - model_uncertainty_buffer
           - liquidity_penalty
           - stale_data_penalty
           - resolution_risk_penalty
```

Only paper trades with net_edge exceeding a configurable threshold (default 2.5 cents) are considered. Position sizing uses either fixed fractional (default 0.5% bankroll per trade) or fractional Kelly with conservative multipliers (default 0.10-0.25x full Kelly). All dimension caps (market, event, cluster, category, total) are enforced simultaneously.

## Uncertainty estimation

Multiple uncertainty estimators are available:
- **Historical error**: Calibration-error-based buffer scaled by probability position
- **Model disagreement**: Variance across model component forecasts
- **Spread-based**: Market spread as consensus-uncertainty proxy
- **Ensemble**: Maximum conservatism across all estimators

Conservative probability uses the lower bound of the confidence interval, making the strategy less likely to trade marginal edges caused by model noise.

## Order-book features

The feature engine extracts 20+ microstructure features:
- Core: midpoint, spread, relative spread, microprice, weighted mid, spread BPS
- Depth: bid/ask depth, imbalance, depth imbalance at multiple levels
- Slope: price impact per unit size on each side
- Concentration: Herfindahl-like liquidity concentration (HHI)
- Rolling: momentum, realized volatility, spread change, distance from extremes

Temporal features include time-until-resolution, urgency score, hour-of-day, and day-of-week. Market features include log-scaled liquidity/volume scores and fee/resolution presence flags.

## Anomaly detection

The anomaly detector identifies:
- Spread widening (z-score based against rolling history)
- Liquidity imbalance (severe bid/ask size asymmetry)
- Zero depth on one side
- Price imbalance (microprice deviation from midpoint)

Anomalies increase uncertainty bounds rather than generating automatic trade signals.

## Point-in-time and data limitations

Book timestamps are parsed as Unix milliseconds. Data API trade timestamps are epoch seconds. Metadata, fee schedules and outcomes become available at receipt time. Terminal resolution receipts preserve both source resolution time and later retrieval time; they are never retroactively treated as known.

Only explicitly labelled YES/NO markets are normalized. Payouts use the original token ordering and micro-USDC units. Proposed, disputed, incomplete, derived, non-binary or provenance-free resolutions do not become automatic settlement labels. Actual category-specific model datasets are not bundled; external interfaces and a CSV adapter are provided.

Discovery is bounded and currently starts from open markets. Lifecycle refresh preserves state changes for already tracked markets. This does not reconstruct markets that disappeared before collection began. Historical price charts cannot reconstruct order-book depth. REST snapshots of YES and NO are not atomic. WebSocket disconnects invalidate reconstruction; top-of-book inconsistencies also force resynchronization. The feed provides no sequence-number guarantee in this implementation, and reported hashes are retained without claiming cryptographic book-hash validation.

Public trade pages are append-only observations, including repeated retrievals. A transaction hash is not a unique fill identifier. Do not sum repeated pages as unique volume or replay them into maker fills without a defensible deduplication policy.

The paper runner reconstructs accounting, depleted liquidity and breaker state from immutable history each cycle. This is deterministic for unchanged inputs, configuration and code, but scales linearly with stored history and holds replay state/results in memory. It is suitable for research and bounded sessions, not a high-throughput execution service.

## Calibration and research

The baseline combines midpoint, a bounded rolling midpoint change and depth imbalance with a large uncertainty band. Its numerical weights only exercise the pipeline. The bands are conservative assumptions, not empirically validated confidence intervals.

Isotonic and Platt calibrators only fit outcomes known before the training cutoff. Repeated forecasts are reduced to one per market for fitting/scoring. Walk-forward folds exclude validation clusters from training and report skipped folds when data is insufficient. Cluster counts are reported; market-level Brier scores do not imply that those markets are statistically independent.

Run the explicitly synthetic calibration example:

```powershell
.\.venv\Scripts\python.exe -m polyalpha.evaluate examples/synthetic_calibration.csv --first-test "2026-07-01T00:00:00+00:00" --end "2026-08-01T00:00:00+00:00" --window-days 31 --output data/calibration-example.json
```

For real research, supply recorded forecasts and verified outcome-availability timestamps using the schema in `docs/RESEARCH_CONTRACTS.md`. Keep the final test period untouched while choosing models and parameters. No optimized strategy, validated external probability model, or empirical profitability result is provided.

## Drift detection

The monitoring module provides:
- **CalibrationDriftDetector**: Rolling Brier score window with baseline comparison
- **FeatureDriftDetector**: Population Stability Index (PSI) for feature distribution changes

PSI < 0.1 indicates no significant change; PSI 0.1-0.2 indicates moderate change; PSI > 0.2 indicates significant drift requiring investigation.

## Optional deployment

`docker compose build collector` builds a bounded collector image. `docker compose run --rm collector` runs one cycle. The separate `docker-compose.postgres.yml` file supplies an archive service after `POSTGRES_PASSWORD` is set in the environment: run `docker compose -f docker-compose.postgres.yml up -d`. Keeping it separate lets collection run without database credentials. There are no embedded credentials. The default runner uses SQLite; `PostgresStore` can be injected into collectors/replay/export code after applying `sql/schema.sql`.

The PostgreSQL schema has append-only receipts and views for market, book, forecast, signal, paper-order/fill, portfolio and external-information records. Only kinds actually written by a caller appear in those views. SQLite reports and paper journals remain the default persistence path. Database-owner privileges can bypass archive protections; the archive is not tamper-proof storage.

## Official integration sources

**Polymarket US** (executable venue) — `docs.polymarket.us`:
- [US API reference](https://docs.polymarket.us)

**Polymarket International** (research-data lineage only) — `docs.polymarket.com`:
- [Documentation index](https://docs.polymarket.com/llms.txt)
- [Market discovery and cursors](https://docs.polymarket.com/api-reference/markets/list-markets-keyset-pagination)
- [Order books](https://docs.polymarket.com/api-reference/market-data/get-order-book)
- [Fees](https://docs.polymarket.com/trading/fees)
- [Public streaming](https://docs.polymarket.com/market-data/realtime-data)
- [Data API v2 OpenAPI schema](https://data-api.polymarket.com/v2/openapi.json)
- [Rate limits](https://docs.polymarket.com/api-reference/rate-limits)

The APIs were checked during this implementation. Market-specific metadata remains authoritative at runtime; a future incompatible schema or fee schedule must fail closed until adapted.

## Independent forecast datasets and repeatable verification

`polyalpha-forecast --database data/research.sqlite3 --as-of <UTC-ISO-timestamp> --output data/new-forecast-run` writes a new report directory and outcome CSV. Forecasts are collected independently of trade eligibility, fee availability, reviews and inventory, while retaining market/book quality gates. Unreviewed markets share one conservative evaluation cluster. The report fingerprints the exact ordered input receipts and includes the cluster assignments. Unresolved forecasts remain in the CSV with blank outcome fields.

`polyalpha-dataset <report.json> --output <new.csv>` exports an existing compatible report. Evaluation selects the earliest forecast per market across all folds, preserves label availability, purges clusters including unresolved candidates, and reports coverage. Paired raw/calibrated scores use a seeded cluster bootstrap only with sufficient clusters; this does not establish trading profitability or prove cluster independence.

Run `.\.venv\Scripts\python.exe scripts/verify.py` for tests, lint and formatting. Add `--integration --live` to rebuild the container, verify a disposable PostgreSQL archive, and exercise bounded public collection and paper operation. Each invocation writes separate logs and a summary under `data/verification/` and exits unsuccessfully if any check fails.

## Reviewed basket scanner

Run `python -m polyalpha.scan --database data/research.sqlite3 --reviews config/graph.json --as-of <UTC-ISO-timestamp> --shares 100 --output data/new-basket-report.json` after supplying real reviews. No review file is fabricated or enabled by default.

The JSON review schema has `nodes`, mapping market IDs to the ResolutionReview fields documented in the resolution module, and `rules`, an array containing `kind`, `markets`, `review_reference`, and timezone-aware `reviewed_at`. A partition review must explicitly establish mutual exclusivity and exhaustiveness for the pinned definitions. Its timestamp must follow every member's definition review. Similar titles do not establish a partition.

The scanner uses equal shares of each YES leg, actual ask depth and known fees, then subtracts per-leg resolution penalties and a 0.002 additional slippage buffer. It rejects incomplete depth, unknown fees, stale metadata, contract mismatches and source-time skew greater than two seconds. Definition changes invalidate reviews. A positive hypothetical surplus is only a research quote: it does not model atomic execution, funding time value, sequential leg risk or guaranteed resolution. Correlation and conditional relationships do not establish a fixed basket payout.

The specification's synthetic asks 0.39 + 0.32 + 0.17 + 0.08 cost 96 for 100 equal-share baskets before fees. With explicitly zero fees, four 0.01 resolution buffers and four 0.002 additional slippage buffers, the modeled surplus is -0.80. This is a numerical fixture, not an observed opportunity.

## Research questions this system helps answer

1. Do high-confidence model disagreements with the market predict final resolutions?
2. Does performance survive fees and VWAP slippage?
3. Does alpha disappear after realistic latency?
4. Which market categories contain genuine predictive edge?
5. Does edge persist out-of-sample?
6. Are results driven by a small number of correlated events?
7. Are probability estimates calibrated?
8. How much theoretical edge becomes realized PnL?
9. Does edge decay as liquidity increases?
10. Do related markets contain exploitable information about one another?

**Do not assume profitability. Treat all results as empirical hypotheses requiring rigorous validation.**

## Dashboard (optional)

```bash
pip install -e ".[dashboard]"
streamlit run src/polyalpha/dashboard/app.py
```

Six tabs: Market Scanner, Positions, Performance, Forecast Quality, Signal Analysis, Risk.

## CLI Commands

```bash
polyalpha collect --config config/base.toml --cycles 3
polyalpha bias report.json --output bias_results.json
polyalpha walk-forward data/observations.csv --first-test 2026-03-01 --end 2026-09-01 --output wf.json
polyalpha experiment report.json --output-dir data/experiments
polyalpha compare baseline.json ensemble.json --output comparison.json
```
