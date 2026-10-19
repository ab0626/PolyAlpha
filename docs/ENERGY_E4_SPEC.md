# ENERGY_E4_SPEC.md — ERCOT Wholesale Power Price (E4-v0) Preregistration

Append-only preregistration for a NEW, separate energy research arm. This arm
is independent of E3 (ERCOT daily peak-load) and of `LIVE_CALIBRATION_V0`.
Nothing here modifies E3, reuses E3 probabilities/thresholds, or grants
execution authority.

## Status (frozen at this version)

```
MODEL = UNBUILT
ALPHA = UNKNOWN
LIVE  = DISABLED
```

No execution authority. No model exists yet. Evidence collection only.

---

## 1. Scope

- **Series:** `KXERCOTWESTW` — "ERCOT West Hub weekly power price" (Kalshi category: Commodities).
- **Underlying:** ERCOT West Hub real-time Settlement Point Price, hub `HB_WEST`.
- **Horizon:** weekly only for E4-v0. Monthly variants (`KXERCOTWESTM`) are out of scope.
- **Markets:** one Kalshi market per strike K, ticker `KXERCOTWESTW-<WEEK>-T<K>`
  (e.g. `KXERCOTWESTW-16OCT26-T50` is "Above $50/MWh"). The strike is encoded in
  the ticker/`yes_sub_title`, not the numeric `strike_value` field.

## 2. Settlement variable (target)

Let `SPP^{RT}_{HB_WEST,t}` be the official ERCOT 15-minute real-time Settlement
Point Price at ERCOT West Hub for delivery interval `t`.

```
A_w = (1 / N_w) * Σ_{t ∈ w} SPP^{RT}_{HB_WEST,t}     (simple mean over the week)

Y_K = 1[ A_w > K ]
```

- **Equality resolves No** (strict `>`). If the rounded weekly average equals the
  strike, the market resolves No.
- **Rounding:** the weekly average is rounded to two decimals, exact halves away
  from zero.

## 3. Governing rule — recorded semantics (not a verbatim copy)

Retrieved live from the venue on the date below. Per Kalshi's own guidance, the
on-page market summary may not be the complete contract terms; this spec records
the *semantics* of the market-page rule and identifies the full governing rules
separately. Any discrepancy between this summary and the full rules is resolved
in favor of the full Kalshi governing rules.

Provenance of the rule snapshot used to write this spec:

```
series:            KXERCOTWESTW
example market:    KXERCOTWESTW-16OCT26-T50
source:            Kalshi trade-api/v2 market endpoint (market-page rule)
retrieved_at:      2026-10-19T15:00Z (approx; see commit timestamp)
rules_secondary_sha256: 39acba71da4bf56e943e8dd772a8803a5d298de55928532b186032c41b2d26f1
```

Recorded semantics:

- **Settlement source:** ERCOT 15-minute real-time Settlement Point Prices,
  ERCOT product **NP6-905-CD**.
- **Interval convention:** ERCOT Central-time operating days and delivery-interval
  conventions; **repeated daylight-saving intervals count separately** (an
  interval that occurs twice across the DST transition is counted twice, not once).
- **Week period:** Friday 5:00 PM EDT to the following Friday 5:00 PM EDT
  (inclusive of the start, exclusive of the end) — a 168-hour window. The boundary
  is stated in EDT; the underlying intervals are Central-time (CDT/CST), so the
  EDT boundary corresponds to 4:00 PM Central time.
- **Calculation:** simple average over ALL applicable 15-minute intervals in the
  window, **including negative prices**.
- **Trading close vs. final interval:** trading closes one minute before the
  period end; the **final delivery interval still counts** in the average.

## 4. Price versioning, corrections, and missing data (semantics)

- **Price versions:** first-published applicable prices are authoritative. Later
  corrections count only if published before expiration AND accepted by the
  Exchange as correcting a material error. Exclude unverified / estimated /
  indicative / forecast prices.
- **Missing data:** do NOT fill missing prices. At expiration:
  - if ≥ 95% of required intervals are available, average the available prices;
  - below 95%, resolve only if the available data establishes the outcome
    regardless of the missing values; otherwise the Exchange settles at the
    last fair price under the full rules.

## 5. Settlement source vs. research-time observable (separation)

The settlement variable is the **first-published** NP6-905-CD real-time SPP,
subject to Kalshi's versioning/correction rule above.

Our research-time observable must be captured as a **separate point-in-time
stream** with explicit `received_at` and first-seen-vs-revision flags, so we
never silently treat a later revision (which Kalshi may ignore for resolution)
as if it were the first-published value. The model must be evaluated strictly
against PIT information available before the close.

## 6. Data lineage (preregistered before capture begins)

A new append-only lineage, distinct from E3's `data/energy/ercot/`:

- **Raw envelope fields** (one row per interval):
  - `operating_day` (ERCOT Central-time operating day)
  - `interval_ending` (ERCOT delivery-interval convention)
  - `hub` (`HB_WEST` for E4-v0)
  - `price` (SPP, exact as published)
  - `ercot_publish_timestamp` / `last_update` (where available)
  - `received_at` (our local receipt clock)
  - `raw_payload_sha256` (hash of the raw ERCOT payload)
  - `is_first_seen` (first observation vs. later revision of the same interval)
- Append-only; never edit historical rows; revisions are new rows, not overwrites.
- Capture begins only AFTER this spec is committed (future-only).

## 7. Model (future — not built)

The model is not built at this version. The intended structural hierarchy is
recorded for the future model-design pass:

```
weather forecasts
  → load / renewable-generation forecasts
  → net-load / scarcity distribution
  → hub real-time price distribution
  → weekly-average distribution
  → Kalshi threshold probabilities  P(A_w > K | PIT information)
```

Model design/fit happens only AFTER the data lineage has accumulated prospective
evidence, and only under its own freeze (`energy-e4-research-vX`, future).

**No reuse of E3:** E3's forecast-error methodology, thresholds, probabilities,
and data are E3-specific and are not imported into E4.

## 8. Evidence gates + future-only cutover

- **Future-only cutover:** this spec's commit time is the cutover. Only data
  observed after the cutover may inform E4 conclusions.
- **Evidence gates** (to be defined and frozen in the future model pass, before
  any model fit — NOT now): minimum weeks, minimum resolved weekly markets,
  minimum observed interval coverage, and any split date. These are deliberately
  left undefined here to avoid encoding a post-hoc threshold.

## 9. Execution / live status

- `LIVE = DISABLED`. E4-v0 has **no execution authority** and no order path.
- Any future live-calibration for E4 is a separate version with its own
  preregistration, caps, and operator gates — never sharing E3/V0 authority.

## Version history

- **E4-v0 (this spec)** — preregistration only. Scope, settlement variable,
  governing-rule semantics, data lineage, future-only cutover, and
  `MODEL=UNBUILT / ALPHA=UNKNOWN / LIVE=DISABLED`. No model, no execution.