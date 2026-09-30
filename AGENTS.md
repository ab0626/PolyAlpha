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

## Forward-evidence gates (the "conclusion")

Pre-registered in `docs/RESEARCH_AGENDA.md`. All three must pass before any family is
evaluated: >=30 days since REAL_DATA_START_US (2026-09-16), >=200 settled markets,
>=200 independent clusters. Cluster count is honest AFTER macro-release clustering
(grouped by `release_id`). Progress monitor:

    .venv/Scripts/python.exe scripts/forward_evidence_monitor.py [--refresh]

## Commands

- Tests: `.venv/Scripts/python.exe -m pytest -q`   (use the venv, NOT system python 3.9)
- Compile check: `.venv/Scripts/python.exe -m py_compile <file>`
- Raw store integrity: `RawStore(data/us/retail/raw).scan()` (valid/partial/mismatch)
- Restart the US collector: `.venv/Scripts/python.exe scripts/us_collection_supervisor.py`

## Gotchas

- Disk has filled twice from unbounded collector writes. Two root causes fixed:
  (1) shadow `wire` duplicated per observation -> store once per `raw_payload_sha256`;
  (2) rawstore `_finalize` left truncated `.gz` on disk-full, which then shadowed the
  intact `.jsonl` and crashed replay. `_day_files`/`replay`/`scan` now tolerate it.
- System `python` (3.9) has a broken `web3`/`eth_typing` install — always use `.venv`.
