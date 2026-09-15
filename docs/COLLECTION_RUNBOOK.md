# PolyAlpha v0.3.0 — Real-Data Collection Runbook

**Status:** PREPARED — no real data collected yet (International)
**Frozen baseline:** `v0.3.0-research-baseline` + immutable tag `v0.3.0-research-baseline-334b911`

This runbook operationalizes the empirical-validation phase. It is the
operating procedure, not a design document. Follow it in order.

---

## 0.6 Venue decision — International (frozen) vs Polymarket US (pivot)

As of this state, **International** is preserved as a completed engineering /
reference lineage (baseline `334b911`, frozen, burn-in NOT started,
`REAL_DATA_START` absent). It is NOT deleted or rewritten, but it is also NOT
the intended production venue if US is the target.

**Polymarket US** is the primary production venue direction. The US adapter
layer is implemented and offline-verified (`src/polyalpha/us/`): USRetailAdapter
(market discovery, L2 books, BBO, events, settlement, price history),
canonical 3-way identifier mapping, the full US market-state model (not
`active: bool`), a US raw collector into `data/us/...`, and a separate US
phase machine.

US has its **own raw lineage** (`data/us/{retail,exchange}/raw`, manifests,
burnin, `REAL_DATA_START_US.json`) and its **own phase sequence**
(`US_BASELINE_FROZEN → US_BURNIN_RUNNING → US_BURNIN_PASSED →
REAL_DATA_START_US → US_COLLECTION_RUNNING`). An International collector
burn-in would prove nothing about US marketSlug parsing, scaled priceScale
conversion, gRPC behavior, US settlement finality, or US market-state
transitions.

The US engineering order (from the US API reference):
1. **USRetailAdapter** ✅ implemented (parser, discovery, book, bbo, events, settlement)
2. **Canonical ID mapping** ✅ implemented (internal_market_id ↔ slug ↔ symbol)
3. **US state model** ✅ implemented (full lifecycle + predicates)
4. **US raw collector** ✅ implemented (venue-specific lineage)
5. **USExchangeAdapter (refdata)** ✅ implemented — `UsInstrument`, `UsInstrumentRegistry`,
   `parse_exchange_book` (scaled-int px/priceScale), `ExchangeRefDataClient`
   (JWT boundary documented). Authoritative per-instrument `tickSize`,
   `minimumTradeQty`, and `priceScale` now drive validation; `0.001` is a
   documented fallback only, never permanent truth.
6. **gRPC real-time feed** — NEXT (snapshot semantics, aggregated/unaggregated
   books, dynamic subscribe/unsubscribe, heartbeat/reconnect, sequence/
   freshness, priceScale normalization)

---

## 0. Version hierarchy — what can change and what cannot

| Layer                  | May change? | Rule                                                              |
|------------------------|-------------|-------------------------------------------------------------------|
| `collector_version`    | Yes         | May change for correctness fixes; recorded per raw record         |
| `research_baseline_version` | No     | Must not change during the collection window                      |
| `raw_data`             | Never       | Wire-exact bytes, write-once, hash-chained manifests              |
| `normalized_data`      | Yes         | May be regenerated from raw with a fixed parser                   |
| `research_dataset`     | Yes         | Versioned + fingerprinted                                         |
| `model/config`         | Frozen      | `config/frozen/v0.3.0-baseline.yaml` + `research_logic_sha256`    |

Freeze discipline: fix a collector bug → record it in the manifest's
`collector_commit` → regenerate any affected normalized snapshots from the raw
layer. The raw layer is never edited. `scripts/freeze_baseline.py verify`
asserts the research-logic/config hashes are unchanged; collection-module and
interface fixes are excluded from `research_logic_sha256` by design.

### Hash domains (explicit, disjoint)

| Domain                | Files                                                                 | Invariant                          |
|-----------------------|-----------------------------------------------------------------------|------------------------------------|
| `research_logic_sha256` | `models/`, `features/`, forecasting, calibration, uncertainty, signals, sizing, expected_value, risk, relative_value, graph, execution, backtest, walk_forward, domain, research_dataset, parsing, frozen config | MUST equal the REAL_DATA_START value |
| `collector_sha256`    | rawstore, market_collector, reconciler, market_metadata, collector_health, daily_manifest, collection | may evolve for correctness          |
| `interface_sha256`    | cli.py, dashboard, reporting glue                                    | may evolve                          |

### Data fidelity invariants (hard requirements)

1. **Wire-exact raw storage.** `rawstore.py` preserves the exact upstream text
   (or base64 of bytes) in the `wire` field and hashes the wire bytes, never a
   re-serialized JSON object.
2. **Three clocks, not two.** `exchange_timestamp_ms`, `received_at_ns`,
   `received_monotonic_ns`, `processed_at_ns`, `processed_monotonic_ns`.
3. **Crash-safe finalization.** gzip to `.tmp`, fsync, atomic rename, unlink
   source; replay promotes a lone `.tmp`, reads an orphaned `.jsonl`, prefers
   an existing `.gz`.
4. **Partial-record quarantine.** Truncated trailing lines are detected and
   skipped by replay, reported by `rawstore.scan()`.
5. **Canonical reconciliation.** bids desc / asks asc / Decimal / zero-size
   dropped before any hash or comparison.
6. **Hash-chained manifests.** Each day records `previous_manifest_sha256`.

---

## 0.4 Code-change classification (after REAL_DATA_START)

Once REAL_DATA_START is written, every code change is classified:

| Class | What it is | Allowed? | Requirements |
|-------|-----------|----------|--------------|
| **A — collector correctness fix** | WS parser bug, manifest bug, REST reconciliation bug, raw-file recovery bug | Yes | issue documented; regression test added; `collector_sha256` changes; `research_logic_sha256` unchanged; historical raw preserved; derived data replayed if necessary |
| **B — execution-safety fix** | idempotency bug, approval-state bug, kill-switch bug, reconciliation bug | Yes | research logic unchanged |
| **C — strategy/research logic** | model weights, edge threshold, new feature, uncertainty formula, position sizing, calibration, category exclusion | **NO** | becomes future-version work (`v0.3.1` candidate), never a silent modification to v0.3 |

The permanent rule: **research decides whether an opportunity exists; risk
decides whether exposure is allowed; execution decides only whether the
intended action can still be performed safely.** Keep that boundary intact
and PolyAlpha can evolve research → shadow → paper → operator-approved live
readiness without components becoming entangled.

---

## 0.5 Phase state machine

Collection phases are explicit and recorded in `data/phase.json`. Transitions
are validated; the REAL_DATA_START marker may ONLY be written from
`BURNIN_PASSED`.

    DEVELOPMENT
        ↓
    BASELINE_FROZEN        ← python -m polyalpha.cli collect-phase-init
        ↓
    BURNIN_RUNNING         ← python -m polyalpha.cli collect-phase --to BURNIN_RUNNING
        ↓
    BURNIN_FAILED ──→ fix collector ──→ rerun
        ↓
    BURNIN_PASSED
        ↓
    REAL_DATA_START        ← collect-start-marker (enforced from BURNIN_PASSED)
        ↓
    COLLECTION_RUNNING

Show current phase: `python -m polyalpha.cli collect-phase --phase-file data/phase.json`

---

## 1. Burn-in (24–72 hours) — the only objective is proving the collector
   cannot silently corrupt the dataset

Before the official window, run:

    python -m polyalpha.cli collect-phase --phase-file data/phase.json --to BURNIN_RUNNING
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
`raw_sha256_root`; the sha256 root must be identical across replays. Also run
`rawstore.scan()` — it must report `partial_lines` only for intentionally
injected faults and `hash_mismatches == 0`.

**Dual independent replay check (strongest pre-collection test).** Run the
raw → normalized → reconstructed transformation twice from scratch and require
identical hashes:

    python -m polyalpha.cli collect-replay-check --raw-dir data/raw

This deletes no data; it copies the raw tree and replays both copies
independently. Output must show `"deterministic": true` and
`Hash_A == Hash_B`.

**Write the burn-in report artifact** (auditable ancestry):

    python -m polyalpha.cli collect-burnin-report \
      --output-dir burnin \
      --period-start 2026-XX-XX --period-end 2026-XX-XX \
      --baseline-commit <commit> \
      --messages N --markets N --reconnects N --forced-failures N \
      --replay-deterministic true --raw-corruption 0 --partial-records 0 \
      --hash-mismatches 0 --invalid-delta-applications 0 \
      --unresolved-book-mismatches 0 \
      --manifest-chain-ok true --metadata-reconstruction-ok true \
      --resolution-lifecycle-ok true --crash-recovery-ok true \
      --gate-passed true

This writes `burnin/burnin-report.json` + `burnin/burnin-report.md` and prints
`burnin_report_sha256` — that hash goes into the REAL_DATA_START marker.

**GO/NO-GO gate** — evaluate explicitly before declaring REAL_DATA_START:

    python -m polyalpha.cli collect-gate \
      --raw-corruption 0 --replay-deterministic true --delta-on-stale 0 \
      --unrecoverable-reconnects 0 --timestamp-failures 0 \
      --reconciliations N --reconciliation-mismatches M \
      --heartbeat-recovery true --restart-recovery true \
      --partial-file-recovery true --metadata-pit true \
      --resolution-captured true --model-hash-unchanged true

The command exits 1 (and prints a FAIL) if any condition fails. Required
conditions before the gate can pass:

| Burn-in condition                          | Gate                       |
|--------------------------------------------|----------------------------|
| Unexplained raw-record corruption          | 0                          |
| Replay produces different normalized state | 0                          |
| Delta applied to invalid/stale book        | 0                          |
| Unrecoverable reconnect state              | 0                          |
| Future/local timestamp invariant failures  | 0 unexplained              |
| REST reconciliation mismatches             | explained + bounded (≤1%)  |
| Heartbeat recovery after forced disconnect | pass                       |
| Collector restart recovery                 | pass                       |
| Partial raw-file recovery                  | pass                       |
| Metadata point-in-time reconstruction      | pass                       |
| Market resolution captured and replayable  | pass                       |
| Baseline/model hash changed                | no                         |
| Phase-machine violations                   | 0                          |
| Intent-ledger duplicates                   | 0                          |
| Kill-switch bypasses                       | 0                          |
| Approval-boundary bypasses                 | 0                          |

The live-ready state machine is also exercised during burn-in even though it
does not transmit. Fault-inject the state machinery (duplicate intents after
restart, kill-switch timing, expiring/wrong-bound approvals, stale book after
approval, reconciliation-unknown states, ledger corruption) — the invariant
everywhere is:

    uncertainty => stop or reconcile     (never: retry and hope)

The burn-in report (`burnin/burnin-report.md`) records the live-ready
scoreboard: phase-machine violations, intent-ledger duplicates, kill-switch
bypasses, approval-boundary bypasses — all must be 0.

### Fault evidence is append-only

Each injected fault is recorded as its own event in
`burnin/fault-evidence.jsonl` (`fault_id`, `type`, `started_at`,
`expected_behavior`, `observed_behavior`, `result`), so "11/11 PASS" can be
audited as a list of concrete scenarios.

**Never retroactively edit a failed fault record.** If a fault fails, append a
new event for the rerun rather than changing the failed one:

    FI-007-A  hard_process_kill              FAIL
    FI-007-B  hard_process_kill_after_fix    PASS

That preserves the experimental history. Likewise, if burn-in exposes a bug,
do not pretend the old implementation commit was fine — the honest chain is:

    f5f64a1 → burn-in FAIL → bug discovered → regression test → fix
      → new implementation commit → new burn-in → PASS

The frozen research baseline (`334b911` / `v0.3.0-research-baseline-334b911`)
remains unchanged throughout as long as `research_logic_sha256` is unchanged.
The implementation commit is auto-detected from git at burn-in time; it is
*by definition* the code that is running.

### Clean-worktree launch invariant

The burn-in launch must run against the exact code identified by HEAD. If the
worktree is dirty, `git rev-parse HEAD` does not describe all the code being
executed. `scripts/run_burnin.py` therefore **fails closed** unless the
worktree is clean (or `--allow-dirty` is passed for a documented edge case):

    git status --porcelain   # must be empty
    git rev-parse HEAD       # the implementation commit

The launch record establishes:

    baseline_commit       = 334b911
    baseline_tag          = v0.3.0-research-baseline-334b911
    implementation_commit = <HEAD at process start>
    working_tree_dirty    = false

The implementation commit is captured **once** at process start — a later
branch checkout does not change what the historical run identifies.

---

## 2. Declare the official start boundary

The phase machine must be at `BURNIN_PASSED` first (enforced — the marker
write fails otherwise). Everything before this timestamp is
`collection_burn_in`; everything at/after is `research_eligible`. Write an
immutable marker — do NOT just note it:

    python -m polyalpha.cli collect-phase --phase-file data/phase.json --to BURNIN_PASSED
    python -m polyalpha.cli collect-start-marker \
      --marker-path data/REAL_DATA_START.json \
      --baseline-tag v0.3.0-research-baseline-334b911 \
      --baseline-commit <git_commit> \
      --config-sha256 <config_sha256> \
      --model-source-sha256 <research_logic_sha256> \
      --feature-schema-sha256 <feature_schema_sha256> \
      --collector-commit <collector_commit> \
      --burnin-report-sha256 <burnin_report_sha256> \
      --phase-file data/phase.json

The marker is self-hashed and refuses to overwrite;
`verify_real_data_start_marker` detects any post-hoc modification. The marker
records:

    {
      "phase": "REAL_DATA_START",
      "timestamp_utc": "...",
      "baseline_tag": "v0.3.0-research-baseline-334b911",
      "baseline_commit": "...",
      "config_sha256": "...",
      "model_source_sha256": "...",
      "feature_schema_sha256": "...",
      "collector_commit": "...",
      "burnin_report_sha256": "...",
      "marker_sha256": "..."
    }

Then advance to COLLECTION_RUNNING:

    python -m polyalpha.cli collect-phase --phase-file data/phase.json --to COLLECTION_RUNNING

Do not mix the two periods in any downstream analysis without an explicit flag.

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

**Reconciliation strategy.** Stay dramatically below the published CLOB limits
(1500 `/book`, 500 `/books` per 10s). Use a rotating sample reconciled
frequently, reconcile suspicious/reconnected books immediately, and do broader
sweeps less often. Batch `/books` where practical. There is no statistical
benefit to living near the ceiling.

**Become boring.** Once collection is running, watch instrument health, not
strategy PnL:

    python -m polyalpha.cli collect-health --raw-dir data/raw --format dashboard

The dashboard surfaces elapsed time, raw events, market/resolution counts,
fidelity, latency, and shows MODEL PERFORMANCE as **LOCKED** — do not evaluate
which model/threshold/category is making money during the collection window.

---

## 4. Daily operations

Each UTC day, run:

    python -m polyalpha.cli collect-manifest --raw-dir data/raw --manifest-dir data/manifests \
      --collector-version v0.3.0 --config-hash <config_sha256> \
      --markets-observed N --resolved-markets M \
      --dropped-connections K --reconciliations R --book-mismatches B

The manifest is append-only (refuses to overwrite an existing day) and
automatically hash-chains to the previous day via `previous_manifest_sha256`,
so mutation/deletion/reordering of historical manifests is detectable. Verify
the previous day's integrity:

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