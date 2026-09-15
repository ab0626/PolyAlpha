# PolyAlpha v0.3.0 — Real-Data Collection Runbook

**Status:** PREPARED — no real data collected yet
**Frozen baseline:** `v0.3.0-research-baseline` (git `16d239b`)

This runbook operationalizes the empirical-validation phase. It is the
operating procedure, not a design document. Follow it in order.

---

## 0. What is frozen, and what is not

| Frozen (do NOT touch)                    | Not frozen (fix freely)                        |
|-------------------------------------------|-----------------------------------------------|
| Model logic (`src/polyalpha/models/**`)  | Collector (`rawstore`, `market_collector`)    |
| Feature schema (`features/**`, `domain`) | Reconciler, metadata, health, manifests      |
| Signal/risk/execution thresholds         | Storage/parser bugs                           |
| `config/frozen/v0.3.0-baseline.yaml`     | Anything that does not change a decision      |

Freeze discipline: fix a collector bug, then re-freeze the **model** hash (the
collection modules are excluded from `model_source_sha256`), record the fix in
the manifest's `collector_commit`, and regenerate any affected normalized
snapshots from the raw layer. The raw layer is never edited.

---

## 1. Burn-in (24–72 hours) — the only objective is proving the collector
   cannot silently corrupt the dataset

Before the official window, run:

    python -m polyalpha.cli collect-burnin --tokens <token_ids> --raw-dir data/raw --duration 3600 --collector-version v0.3.0

Then deliberately break it and verify recovery:

1. Kill internet → collector reconnects → `reconnect_count` increments, books invalidated.
2. Restart the machine mid-run → raw files must be replayable (they are plain JSONL while open).
3. Kill the collector → restart → new `connection_id`, sequence resets, books resynced from full snapshot.
4. Drop the WebSocket → reconnect → no deltas applied onto stale books.
5. Force a REST timeout → reconcile records a failure, collection continues.
6. Send a malformed frame → parse error recorded, raw still captured, loop does not die.
7. Restart the database/store → files are gzip-compressed on close, re-open is clean.

Then verify determinism — replay the burn-in raw files and confirm:

    python -m polyalpha.cli collect-manifest --raw-dir data/raw --manifest-dir data/manifests ...
    python -m polyalpha.cli collect-health --raw-dir data/raw --format terminal

The `collect-burnin` command already emits `burn_in_deterministic` and
`raw_sha256_root`; the sha256 root must be identical across replays.

**Gate:** only after the burn-in replays deterministically and all fault
injections are recovered may `REAL_DATA_START` be declared.

---

## 2. Declare the official start boundary

Everything before this timestamp is `collection_burn_in`; everything at/after
is `research_eligible`. Write it down:

    REAL_DATA_START = 2026-XX-XXT00:00:00Z

Record it in the daily manifest of the first official day. Do not mix the two
periods in any downstream analysis without an explicit flag.

---

## 3. Run the collector continuously

    python -m polyalpha.cli collect-raw \
      --tokens <all_token_ids> \
      --raw-dir data/raw \
      --reconcile \
      --collector-version v0.3.0 \
      --reconcile-interval 30

Run under a supervisor (systemd / task scheduler / docker) so a crash auto-restarts.

**Collection universe ≠ research universe.** Collect every reasonable active
order-book market (the `universes.collection` config is deliberately broad);
apply `research`/`signal`/`executable` filters only downstream. Otherwise you
can never answer "what happened to the markets my filter rejected?".

---

## 4. Daily operations

Each UTC day, run:

    python -m polyalpha.cli collect-manifest --raw-dir data/raw --manifest-dir data/manifests \
      --collector-version v0.3.0 --config-hash <config_sha256> \
      --markets-observed N --resolved-markets M \
      --dropped-connections K --reconciliations R --book-mismatches B

The manifest is append-only (refuses to overwrite an existing day). Verify the
previous day's integrity:

    python scripts/freeze_baseline.py verify          # model/feature/config unchanged

Nightly validation (NOT strategy optimization):
- raw integrity (sha256 root matches manifest)
- schema / timestamp / book-consistency checks
- reconciliation match rate (should be ~100%; investigate persistent mismatches)
- metadata + resolution completeness
- disk integrity / compression ratio

**Do not** stare at strategy PnL or tune thresholds daily — that is how a
"future holdout" quietly becomes training data.

---

## 5. Stopping criterion is resolved independent events, not wall-clock days

| Metric              | Approx. milestone          |
|---------------------|----------------------------|
| 100 resolved events | pipeline sanity             |
| 250–500             | first meaningful exploratory results |
| 500–1,000+          | substantially more interesting |
| multiple categories + regimes + forward coverage | much stronger |

Track `effective N` (design-effect / ICC adjusted), not raw snapshot count.
Ten thousand snapshots from one presidential market are still one event.

---

## 6. Then — exactly one frozen baseline experiment

Freeze the model (already done). Build the research dataset from raw →
normalized → snapshots with the fixed parser. Split by **event group + time**
(not by row):

- First 60% → train
- Next 20% → validation
- Last 20% → FINAL HOLDOUT (lock via `polyalpha.cli holdout-lock`)

Run the full pipeline and produce the v0.3 result report (ΔBrier, EdgeRealization,
OOS Net PnL, P(NetPnL>0), effective N). Unlock the holdout once.

---

## 7. Only after the baseline: build the five missing tools

Order matters — each depends on the previous:

1. **Execution calibration** — VWAP_predicted vs VWAP_forward-paper, conditional on
   market/liquidity/spread/size/volatility/time-to-resolution; feed the bias back.
2. **Capacity curves** — replay $100→$100k through identical book states.
3. **Information-value curves** — incremental ΔBrier per data family.
4. **Prediction revision attribution** — what drove q_t=0.52 → q_t+1=0.67.
5. **PBO / deflated Sharpe** — only meaningful after many real strategies are tried.

---

## 8. Health SLO targets

| Metric                    | Target    |
|---------------------------|-----------|
| Uptime                    | ≥ 99.9%   |
| Reconciliation match rate | ≥ 99%     |
| Median receive lag        | < 100 ms  |
| P99 receive lag           | < 1 s     |
| Raw manifest integrity    | 100% pass |

If the reconciliation match rate drops persistently, treat the reconstructed
books as suspect and rely on REST snapshots until the collector bug is fixed
and the affected raw→normalized segment is regenerated.