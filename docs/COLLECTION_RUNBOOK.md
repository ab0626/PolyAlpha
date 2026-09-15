# PolyAlpha v0.3.0 — Real-Data Collection Runbook

**Status:** PREPARED — no real data collected yet
**Frozen baseline:** `v0.3.0-research-baseline`

This runbook operationalizes the empirical-validation phase. It is the
operating procedure, not a design document. Follow it in order.

---

## 0. Version hierarchy — what can change and what cannot

| Layer                  | May change? | Rule                                                              |
|------------------------|-------------|-------------------------------------------------------------------|
| `collector_version`    | Yes         | May change for correctness fixes; recorded per raw record         |
| `research_baseline_version` | No     | Must not change during the collection window                      |
| `raw_data`             | Never       | Wire-exact bytes, write-once, hash-chained manifests              |
| `normalized_data`      | Yes         | May be regenerated from raw with a fixed parser                   |
| `research_dataset`     | Yes         | Versioned + fingerprinted                                         |
| `model/config`         | Frozen      | `config/frozen/v0.3.0-baseline.yaml` + `model_source_sha256`      |

Freeze discipline: fix a collector bug → record it in the manifest's
`collector_commit` → regenerate any affected normalized snapshots from the raw
layer. The raw layer is never edited. `scripts/freeze_baseline.py verify`
asserts the model/schema/config hashes are unchanged; collection-module fixes
are excluded from `model_source_sha256` by design.

### Data fidelity invariants (hard requirements)

1. **Wire-exact raw storage.** `rawstore.py` preserves the exact upstream text
   (or base64 of bytes) in the `wire` field and hashes the wire bytes, never a
   re-serialized JSON object. JSON key ordering, number formatting, and parser
   changes can never make "raw" data less raw.
2. **Three clocks, not two.** Each record keeps the exchange timestamp
   (`exchange_timestamp_ms`), local wall receive (`received_at_ns`), and local
   monotonic receive/processed (`received_monotonic_ns`, `processed_monotonic_ns`).
   Monotonic clocks give reliable internal queueing/processing durations even
   if NTP steps wall time backward.
3. **Crash-safe finalization.** On close/day-roll the `.jsonl` is gzip'd to a
   `.tmp`, fsynced, atomically renamed to `.jsonl.gz`, then the source is
   removed. Replay promotes a lone `.jsonl.gz.tmp`, reads an orphaned `.jsonl`,
   and treats an existing `.jsonl.gz` as authoritative — no double-counting.
4. **Partial-record quarantine.** A truncated trailing line (crash mid-write) is
   detected and skipped by replay and reported by `rawstore.scan()`; every
   successfully acknowledged record survives.
5. **Canonical reconciliation.** REST vs WS books are compared only after
   canonicalization (bids desc / asks asc, Decimal-normalized, zero-size levels
   dropped) — never on raw float serialization.
6. **Hash-chained manifests.** Each day's manifest records
   `previous_manifest_sha256`, so mutation/deletion/reordering of historical
   manifests is detectable, not just mutation of each day's raw files.

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
`raw_sha256_root`; the sha256 root must be identical across replays. Also run
`rawstore.scan()` — it must report `partial_lines` only for intentionally
injected faults and `hash_mismatches == 0`.

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

---

## 2. Declare the official start boundary

Everything before this timestamp is `collection_burn_in`; everything at/after
is `research_eligible`. Write an immutable marker — do NOT just note it:

    python -m polyalpha.cli collect-start-marker \
      --marker-path data/REAL_DATA_START.json \
      --baseline-tag v0.3.0-research-baseline \
      --baseline-commit <git_commit> \
      --config-sha256 <config_sha256> \
      --model-source-sha256 <model_source_sha256> \
      --feature-schema-sha256 <feature_schema_sha256> \
      --collector-commit <collector_commit> \
      --burnin-report-sha256 <burnin_report_sha256>

The marker is self-hashed and refuses to overwrite; `verify_real_data_start_marker`
detects any post-hoc modification. The marker records:

    {
      "phase": "REAL_DATA_START",
      "timestamp_utc": "...",
      "baseline_tag": "v0.3.0-research-baseline",
      "baseline_commit": "...",
      "config_sha256": "...",
      "model_source_sha256": "...",
      "feature_schema_sha256": "...",
      "collector_commit": "...",
      "burnin_report_sha256": "...",
      "marker_sha256": "..."
    }

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