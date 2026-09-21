# Order-Flow Data Specification

**Purpose:** the infrastructure that unblocks research families A6 (signed-flow
microstructure), A7 (informed wallet flow), and A8 (execution / adverse
selection) in `docs/RESEARCH_AGENDA_V3.md`.

**Status:** SPEC (not yet implemented). Written before any outcome-conditioned
analysis.

---

## 1. The venue fork (read this first)

"On-chain OrderFilled" and "wallet identity" are properties of **Polymarket
International** (the Polygon CLOB), not of **Polymarket US** (the CFTC
centralized venue that is PolyAlpha's production target). This distinction
decides which families are even *collectable*:

| | International (Polygon) | US (CFTC) |
|---|---|---|
| Fill tape | on-chain `OrderFilled` events (public) | authenticated trade WS (explicit maker/taker) |
| Maker/taker **direction** | from asset swap in the event (public) | explicit `{side, intent}` fields |
| Maker/taker **identity** | public addresses | **not exposed** |
| Reorg risk | yes (Polygon) | no |

**Consequence:** A7 (informed wallet flow, Gomez-Cram) requires persistent
trader identity, which exists only on International's public addresses. It is
**not reproducible on Polymarket US with public data**. A6/A8 are collectable on
both venues (signed flow needs direction, not identity).

This spec therefore specifies **two fill collectors** plus an International-only
wallet join.

---

## 2. Data sources

### C1 — International on-chain `OrderFilled` collector (authoritative)

- **Primary:** the Polymarket orderbook subgraph (GraphQL), querying
  `orderFilledEvents` — the canonical, reorg-safe historical record Dubach
  recommends. Fields: `orderHash`, `maker`, `taker`, `makerAssetId`,
  `takerAssetId`, `makerAmountFilled`, `takerAmountFilled`, `fee`,
  `transactionHash`, block number/timestamp.
- **Fallback / reconciliation:** Polygon JSON-RPC `eth_getLogs` on the CTF
  Exchange orderbook contract, so the subgraph is cross-checked against raw logs
  (the same "feed vs ground truth" discipline as Dubach).
- **Latency model:** subgraph lags the chain by seconds-to-minutes; this is a
  *historical/research* feed, not a live execution feed. For live use you would
  consume the CLOB WS and reconcile to on-chain.

### C2 — US authenticated trade WS collector

- **Source:** `wss://api.polymarket.us/v1/ws/markets`, `SUBSCRIPTION_TYPE_TRADE`.
  Each message carries `price`, `quantity`, `tradeTime`, and `maker`/`taker`
  each `{side, intent}` — **explicit** aggressor flow, so Dubach's
  direction-inference failure does not apply.
- **Auth:** the Retail API key (Ed25519) already scaffolded in
  `src/polyalpha/us/execution.py` (`UsRetailAuth`). Credentials from env only.
- **No wallet identity** — by design (centralized venue).

### C3 — Wallet join (International only)

- Aggregate `FillRecord` rows into a per-address ledger (`maker`/`taker` PnL,
  volume, Brier, execution quality). Input to A7/A8. Never joined to any
  real-world identity; addresses are pseudonymous.

---

## 3. Canonical schema (`FillRecord`)

One venue-agnostic record so downstream research never sees venue specifics:

```
source               : "intl_orderfilled" | "us_trade_ws"
transaction_hash     : str
block_number         : int | None          # International only
block_timestamp      : datetime            # venue/block time
received_at          : datetime            # ingestion time (RawStore 3 clocks)
condition_id         : str                 # market
asset_id             : str                 # outcome token
maker                : str | None          # address (International) / None (US)
taker                : str | None
maker_asset_id       : str                 # what maker gives
taker_asset_id       : str                 # what taker gives
maker_amount         : Decimal
taker_amount         : Decimal
price                : Decimal             # implied from amounts
side                 : "BUY" | "SELL"      # aggressor (taker) direction
intent               : str | None          # US only: *_LONG / *_SHORT
fee                  : Decimal
```

Derived at ingest (never stored as truth): `side` and `price` are *computed*
from `maker_asset_id`/`taker_asset_id` and the amounts, so a parser change never
mutates raw data.

Write-once, wire-exact, append-only via the existing `RawStore` (per-source
files, no overwrite).

---

## 4. Derived signals (what the data unblocks)

Signed flow — from maker/taker, never inferred:

```
OFI_t = sum_j s_j * q_j ,   s_j = +1 if taker buys else -1
```

Kyle's lambda (price impact of signed flow):

```
dP_t = lambda * OFI_t + eps ,   estimate via OLS with HAC/cluster SEs
```

Adverse-selection markout (taker-side):

```
markout_h(trade) = side * (p_{t+h} - p_t) ,   averaged by aggressor side
```

Maker-vs-taker PnL decomposition (A8):

```
PnL_total = PnL_maker + PnL_taker ,   each from the C3 wallet ledger
```

Informed flow (A7) — **look-ahead-safe**:

```
Skill_w(t) = f( PnL_w, Brier_w, exec_w , info < t )        # t^- ONLY
InformedFlow_t = sum_w Skill_w(t^-) * SignedVol_{w,t}
```

`Skill_w` must use strictly pre-`t` information. A future-outcome skill score is
a leak, and H14 (RESEARCH_AGENDA_V3) is the permanent negative control that
tests exactly that leak.

---

## 5. Measurement validation (mandatory before trusting any flow feature)

1. **Reproduce Dubach's negative control:** compute feed-inferred direction on
   the US book feed and compare to `SUBSCRIPTION_TYPE_TRADE` maker/taker ground
   truth. Expect ~59% agreement; if materially better, the feed has changed and
   the assumption must be re-validated.
2. **Subgraph vs. RPC reconciliation:** C1's two sources must agree on
   (maker, taker, asset ids, amounts) to hash-level. Any mismatch → quarantine.
3. **Finality:** International ingest applies a confirmation delay (default 6
   blocks) and treats reorged blocks as corrupt (RawStore scan + quarantine).

---

## 6. Phasing

| Phase | Deliverable | Effort |
|---|---|---|
| P0 | `FillRecord` + parser + `RawStore` integration + reconciliation check | S |
| P1 | C2 (US trade WS collector, reuses `UsRetailAuth`) — unblocks A6/A8 on US | S |
| P2 | C1 (International subgraph + RPC collector) — unblocks A6/A7/A8 on Intl | M |
| P3 | C3 wallet ledger + skill/informed-flow feature builder (t^- guarded) | M |
| P4 | Dubach negative-control harness + Kyle-λ/markout estimators | M |

P1 is first: it is the only collector that produces *live forward* signed flow
on the production venue. C1/C3 are historical/research (International), useful
for reproducing the literature but not for the forward US book.

---

## 7. Compliance / privacy

- International addresses are pseudonymous public on-chain data; no PII.
- US: account/participant identity is **never** collected (centralized venue,
  no public identity; privacy + ToS). A7 is marked NOT_RUN on US for this reason.
- No off-chain deanonymization, no wallet clustering against known identities.

---

## 8. Open decisions

1. **Is reproducing the literature on International worth a separate historical
   collector (C1/C3), or do we stay US-only and drop A7/A9?** This is a venue
   strategy decision, not an engineering one.
2. **Live vs. historical:** US trade WS is live-only (no backfill). Historical
   US fill tape availability must be confirmed with the venue.
3. **Subgraph endpoint** must be pinned as configuration (not code) and verified
   against RPC before C1 is built.
