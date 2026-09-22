# Implementation status and research gates

## Implemented

The repository now contains the ordered core requested in the specification:

1. Public market discovery; live REST books; raw receipts; typed normalization; quality filters; tracked-market lifecycle refresh; price-history adapter; public v2 trade/resolution adapters; WebSocket snapshot/delta reconstruction.
2. Decimal depth walking; per-market known-fee handling; partial fills; price limits; consumed liquidity; delayed paper execution; separate conservative maker-queue research.
3. Cash and fee-inclusive cost basis; partial sells; realized PnL; explicit settlement; liquidation-depth valuation; incomplete-valuation reporting; fill journals.
4. Market, event, semantic-cluster, category and portfolio caps; daily-loss/drawdown latches; resolution-definition review; stale/unknown-data rejection.
5. Forecast interface; microstructure features; explicitly diagnostic rolling baseline; ensemble and conservative bands; external source/CSV contracts.
6. Isotonic and Platt calibration; Brier/log loss/reliability metrics; mature-label filtering; repeated-market reduction; cluster-purged walk-forward folds.
7. Receipt-time historical replay; delayed arrival rechecks; settlement availability; configurable hold/edge exits; paper runner; CLI examples; reproducibility metadata.
8. Reviewed relative-value constraints; performance/alpha-decay/drift helpers; SQLite and optional PostgreSQL archive; schema, Docker files, tests and documentation.

## Verified locally

1,925 tests (1 skipped) plus five numerical subtests pass. Live REST, bounded WebSocket, lifecycle refresh, v2 trade collection, two-cycle paper operation, synthetic replay, and synthetic calibration CLI execution have been exercised. See README for bounded sample counts and assumptions. Docker collection and a disposable PostgreSQL integration test also pass. No live empirical profitability has been assessed.

## Remaining verification and research work

- Gather sufficiently long, representative historical depth and point-in-time market lifecycle coverage. Current live smoke data is far too small for strategy evaluation and cannot recover markets missing before collection began.
- Build and validate category-specific independent probability models using actual public datasets and recorded revisions. The interface and CSV/mock adapters are provided; an election or sports probability model cannot be substantiated without its data.
- Review real semantic exposure clusters and resolution definitions before enabling paper entries. The distributed review map stays empty.
- Validate uncertainty coverage and statistical dependence with enough resolved events. Conservative bands, cluster counts and Brier scores alone do not establish a robust independent sample.
- Investigate real execution semantics, per-match fee rounding, replenishment and queue evidence before relaxing the deliberately pessimistic simulator. Missing depth remains missing.
- Perform final untouched out-of-sample evaluation after model/data decisions are fixed, reporting sample count, periods, costs, calibration, concentration and failure diagnostics.

A dashboard, Parquet-scale storage, advanced election models, market-making optimization and production operations are optional extensions. They must not substitute for the data and evaluation gates above. This implementation remains research/paper-only.
