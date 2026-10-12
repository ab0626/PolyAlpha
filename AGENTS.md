# PolyAlpha — operational notes (agent-facing)

## Data lineage (what lives where, what feeds what)

- `data/us/retail/raw/` — the forward-evidence raw store. **This is the source of
  truth for the research dataset** (`src/polyalpha/us/research.py`). Wire-exact,
  hash-chained. Do not edit; replay-only.
- `data/us/market_data/raw/` — WebSocket/gRPC market data (`collect_market_data.py`),
  large volume, separate from the retail research lineage.
- `data/us/trade/` — trade-stream collection (`collect_us_trades.py`).
- `data/shadow/raw/` — external observations (FRED/EIA/weather/SEC/GDELT) via
  `shadow_supervisor.py`. "Observation only — nothing feeds v0.4."
- `data/us/{tracked,settled,stratified}_membership.jsonl` — persisted collection
  universe state. `release_mapping.jsonl` maps macro buckets -> release_id (parent
  event for clustering).
- `data/energy/kalshi/ladders/` — append-only Kalshi energy ladder snapshots
  (`energy_capture_supervisor.py` -> `energy_capture.py`). Isolated from the frozen
  `data/us/...` lineage. `data/energy/kalshi/status.json` is the freshness heartbeat;
  `restart-log.jsonl` / `supervisor-log.jsonl` / `alerts.jsonl` are the lifecycle logs.
- `data/energy/kalshi/ws/` — authenticated Kalshi WebSocket raw lineage
  (`energy_ws_supervisor.py` -> `energy_ws_collector.py`). orderbook_delta/trade/
  ticker/market_lifecycle_v2 for the energy series. PARALLEL to (never mixed with)
  the frozen 30s REST snapshots. `status.json` (heartbeat) is the freshness signal.
- `data/energy/ercot/` — append-only ERCOT capture (forecast vintages + actual
  settlement source) via `energy_forecast_supervisor.py` -> `energy_forecast_collector.py`.
  Full hourly intraday path, keyed by `publish_timestamp` (never collapses revisions):
  `forecast_vintages.jsonl` + `actuals.jsonl` (normalized rows), `receipts.jsonl` +
  `actual_receipts.jsonl` (raw-report provenance), `status.json` (freshness heartbeat).
  Isolated from `data/us/...`. Readiness: `scripts/energy_e3_readiness.py`.
- `data/energy/shadow_observations.jsonl` / `shadow_resolutions.jsonl` — E3 shadow
  observation ledger (frozen lead-0-only rule, ENERGY_E3_SPEC v3). Full denominator
  (every strike, `is_candidate` derived), deterministic id, append-only. Written by
  `scripts/energy_e3_runner.py` (measurement only; never trades, never scores).

## Collector launcher architecture (not in the repo, on the machine)

Collectors are auto-started at Windows login via hidden `.vbs` files in the Startup
folder (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`), which invoke
`.bat` files in `C:\Users\adith\`:

- `PolyAlphaShadowSupervisor.vbs` -> `polyalpha_shadow_supervisor.bat` (crash-loop)
- `PolyAlphaUSCollector.vbs`     -> `polyalpha_us_collector.bat`
- `PolyAlphaUSHealth.vbs`        -> `polyalpha_us_health.bat`
- `PolyAlphaUSMarketData.vbs`    -> `polyalpha_us_market_data.bat` (crash-loop)
- `PolyAlphaUSTrades.vbs`        -> `polyalpha_us_trades.bat` (crash-loop)
- `PolyAlphaUSReport.vbs`        -> `polyalpha_us_report.bat`
- `PolyAlphaPreNfpWatchdog.vbs`  -> `polyalpha_pre_nfp_watchdog.bat`
- `PolyAlphaEnergyCapture.vbs`   -> `polyalpha_energy_capture.bat` (crash-loop)
- `PolyAlphaEnergyHealth.vbs`    -> `polyalpha_energy_health.bat`
- `PolyAlphaEnergyForecast.vbs`  -> `polyalpha_energy_forecast.bat` (crash-loop)
- `PolyAlphaEnergyForecastHealth.vbs` -> `polyalpha_energy_forecast_health.bat`
- `PolyAlphaEnergyWs.vbs`       -> `polyalpha_energy_ws.bat` (crash-loop)
- `PolyAlphaEnergyWsHealth.vbs` -> `polyalpha_energy_ws_health.bat`

The E3 evaluator is NOT a service: it is a deterministic batch job scheduled via
Windows Task Scheduler (`PolyAlphaEnergyE3`, daily 01:30 local/Central Time,
DST-safe) -> `polyalpha_energy_e3.bat` -> `scripts/energy_e3_runner.py`.

The crash-loop `.bat` files use `:loop ... timeout /t 5 ... goto loop`, which is why
killing a collector PID makes it respawn ~5s later. To stop one permanently, kill the
`cmd.exe` running its `.bat` (or disable the `.vbs` Startup item). Inside the Codex
agent runtime, each `.venv` process also gets a `codex-runtimes` wrapper child — the
same logical script, so treat them as one.

## Freeze discipline (do NOT skip)

- `scripts/run_us_collection.py` is frozen against `COLLECTION_IMPL_COMMIT` in
  `scripts/us_collection_supervisor.py`. After ANY change to that runner: commit it,
  then update `COLLECTION_IMPL_COMMIT` to the new commit hash (the supervisor
  `_collector_matches_impl` check now LF-normalizes, so CRLF is not a false fail).
- Frozen research logic: `scripts/freeze_baseline.py verify` covers the v0.3.0
  International baseline (models/features/research_dataset/etc.). The US `us/research.py`,
  `us/collector.py`, and scripts are NOT in that frozen domain — collector/infra fixes
  are Class A (allowed), strategy/research changes are Class C (forbidden).
- Frozen execution mechanics: `scripts/freeze_kalshi_exec.py verify` pins the
  Kalshi demo venue adapter (`kalshi-exec-demo-v0.2`, a separate integrity domain):
  `src/polyalpha/kalshi_exec.py`, `src/polyalpha/kalshi_auth.py`,
  `src/polyalpha/kill_switch.py`. Any runtime drift fails verification and requires
  explicit review + a new execution version (re-freeze). Tests, the smoke script, and
  docs can evolve without re-freezing.
- Frozen execution control plane: `scripts/freeze_kalshi_oms.py verify` pins the
  demo automated execution harness (`kalshi-oms-demo-v0.3`): `src/polyalpha/kalshi_oms.py`
  and `scripts/kalshi_oms_demo.py` (both are runtime control-plane code). Same drift
  rule; the venue adapter stays independently frozen at v0.2.

## Forward-evidence gates (the "conclusion")

Pre-registered in `docs/RESEARCH_AGENDA.md`. All four must pass before any family is
evaluated: >=30 days since REAL_DATA_START_US (2026-09-16), >=200 settled markets,
>=200 independent clusters, and >=0.8 observed coverage (1 minus known-gap fraction
of the elapsed window). Cluster count is honest AFTER macro-release clustering
(grouped by `release_id`). Progress monitor:

    .venv/Scripts/python.exe scripts/forward_evidence_monitor.py [--refresh]

## Commands

- Tests: `.venv/Scripts/python.exe -m pytest -q`   (use the venv, NOT system python 3.9)
- Compile check: `.venv/Scripts/python.exe -m py_compile <file>`
- Raw store integrity: `RawStore(data/us/retail/raw).scan()` (valid/partial/mismatch)
- Restart the US collector: `.venv/Scripts/python.exe scripts/us_collection_supervisor.py`
- Raw-store replay diagnostic: `.venv/Scripts/python.exe scripts/replay_diagnostic.py [--max-seconds N]`
- E3 readiness: `.venv/Scripts/python.exe scripts/energy_e3_readiness.py`
- E3 shadow runner (scheduled daily 01:30 CT): `.venv/Scripts/python.exe scripts/energy_e3_runner.py`

## Gotchas

- The forward-evidence `--refresh` replay is NOT fast: ~7.7 GB compressed raw store
  read twice (~45-65 min, JSON-parse-bound at ~5 MB/s; each record is double-parsed:
  outer envelope + inner `canonical_json`). It used to look hung because it printed
  nothing until done; `daily_forward_evidence.py` now streams progress to stdout and
  has a `--watchdog-minutes` no-progress abort. `scripts/replay_diagnostic.py` measures
  per-file timing when diagnosing this.
- Disk has filled twice from unbounded collector writes. Two root causes fixed:
  (1) shadow `wire` duplicated per observation -> store once per `raw_payload_sha256`;
  (2) rawstore `_finalize` left truncated `.gz` on disk-full, which then shadowed the
  intact `.jsonl` and crashed replay. `_day_files`/`replay`/`scan` now tolerate it.
- System `python` (3.9) has a broken `web3`/`eth_typing` install — always use `.venv`.
- The host is a laptop (on AC). Sleep/hibernate/lid-close are set to never/do-nothing
  so idle does not halt collection (the ~35h data gaps were the laptop sleeping after
  1h idle). Keep it plugged in: DC "never sleep" drains the battery to a hard
  shutdown. A reboot still needs a login to restart the launchers (no auto-login).
- The Kalshi WS `market_lifecycle_v2` channel is delivered exchange-wide (the
  `market_tickers` filter in the subscribe message is ignored for it), so the
  energy WS lineage briefly captured non-energy lifecycle events (BTC/NFL/SOL/ETH/
  etc.). Fixed 2026-10-12 by a client-side scope guard in `energy_ws_collector.py`
  (drops any message whose `market_ticker` is outside the subscribed energy set;
  liveness counters still track raw flow). Pre-fix files
  `data/energy/kalshi/ws/2026-10-11.jsonl` and `2026-10-12.jsonl` (through ~02:54 UTC)
  still contain those rows and are left as-is (append-only lineage).
  `orderbook_delta`/`trade`/`ticker` were always correctly ticker-scoped.
