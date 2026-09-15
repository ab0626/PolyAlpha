# Polymarket US API Reference (Corrected)

**Source:** https://docs.polymarket.us (fetched 2026-09-15, deep pass)
**Status:** ADAPTER BUILT — USRetailAdapter, identifier registry, state model, raw collector, US phase machine, Direct Exchange refdata layer, AND the gRPC market-data stream consumer (`UsGrpcMarketStream`: subscription lifecycle, snapshot, aggregated/unaggregated, heartbeat, reconnect invalidation, out-of-order freshness, instrument-lookup-before-normalization, priceScale-required decoding, liveness staleness, deterministic replay) are implemented and offline-verified. US reconciliation (gRPC ↔ Exchange REST ↔ retail book/BBO) is the documented next phase.
**Relevance:** Polymarket US is a CFTC-regulated US exchange with a **fundamentally different API** from Polymarket International (`docs.polymarket.com` / CLOB). The current PolyAlpha collector targets International. US is a **separate venue adapter** and a **separate empirical data lineage** — never a retrofit of International parsing.

> Corrected from first draft: Polymarket US is **three** distinct surfaces, not two, and `gateway.polymarket.us` (public data) is logically separate from `api.polymarket.us` (authenticated trading).

---

## 1. The three US surfaces

| Surface | Base | Main use | Auth |
|---------|------|----------|------|
| **US Public/Retail DATA** | `https://gateway.polymarket.us` | markets, events, books, BBO, settlement, price history | **None** (public) |
| **US Authenticated Retail** | `https://api.polymarket.us` | orders, portfolio, balances, authenticated WebSockets | Retail API key |
| **Direct Polymarket Exchange** | `api.*.polymarketexchange.com` + gRPC + FIX | institutional/direct market data + trading | Private-key JWT / FIX session |

- `gateway.polymarket.us` = unauthenticated public data API.
- `api.polymarket.us` = authenticated trading API.
- These are **logically separate surfaces** — do not conflate.

### Environments (Direct Exchange)

| Env | REST | gRPC | Auth domain |
|-----|------|------|-------------|
| **Preprod** | `https://api.preprod.polymarketexchange.com` | `grpc-preprod.polymarketexchange.com:443` (see §8 inconsistency) | `pmx-preprod.us.auth0.com` |
| **Prod** | `https://api.prod.polymarketexchange.com` | `grpc-prod.polymarketexchange.com:443` | `pmx-prod.us.auth0.com` |

- Health: `GET /v1/health` → `{"status": "ok"}`.
- Direct access tokens: `expires_in: 180` seconds; **refresh every 3 minutes**.

---

## 2. Rate limits — 20 req/sec global

| Scope | Limit |
|-------|-------|
| Authenticated (all endpoints) | 20 req/sec per API key |
| Public (unauthenticated) | 20 req/sec per IP |

- `429 Too Many Requests` → stop, wait ≥1s, exponential backoff.
- **Latency stopgap (execution edge case):** a new/modified order unprocessed for 5s is rejected with text `Global Rate Limit Exceeded` — **this is NOT a rate-limit event**. Do not throttle in response. Pure cancels are never affected.

> **Reason-code the two differently** (for your reconciliation system):
> - HTTP `429` → `RATE_LIMIT`
> - order reject `Global Rate Limit Exceeded` after processing delay → `LATENCY_STOPGAP`
>
> Do not collapse them.

### Best practice
- Use WebSocket instead of polling; cache reference data (TTL ~300s).

---

## 3. APIs that matter most for PolyAlpha

### Public market discovery — `GET https://gateway.polymarket.us/v1/markets`
Pagination `limit`/`offset`; filters `id[]`, `slug[]`, `active`, `closed`, `archived`, `volumeNumMin/Max`, `startDateMin/Max`, `endDateMin/Max`, `categories[]`, `marketTypes[]`, `tagIds[]`, `sportsMarketTypes[]`, `includeHidden`.
- Rich response: **marketSides[]** (side id, ERC1155|INSTRUMENT, price, long flag, quote, tradable), `orderPriceMinTickSize`, `minimumTradeQty`, `volume/volume24hr/1wk/1mo/1yr`, `bestBidQuote`/`bestAskQuote`, `feeCoefficient`, `comboEnabled`, `tags[]`, `subject`.
- **This is the US equivalent of your universe-discovery/metadata source.**

### Full L2 book — `GET https://gateway.polymarket.us/v1/markets/{slug}/book`
```json
{"marketData": {
  "marketSlug": "...",
  "bids":  [{"px": {"value": "0.555", "currency": "USD"}, "qty": "0.50"}],
  "offers":[{"px": {"value": "0.560", "currency": "USD"}, "qty": "0.80"}],
  "state": "MARKET_STATE_OPEN",
  "stats": {"lastTradePx": {...}, "sharesTraded": "150000", "openInterest": "500000",
            "highPx": {...}, "lowPx": {...}, "currentPx": {...}, "settlementPx": {...}},
  "transactTime": "2024-01-15T10:30:00Z"}}
```
- **Terminology differs from International:** `marketSlug` (not `asset_id`/`token_id`), `px.value`/`qty` (not `price`/`size`), `offers` (not `asks`).
- **Your International parser must not parse this directly.**

### BBO — `GET https://gateway.polymarket.us/v1/markets/{slug}/bbo`
`currentPx`, `lastTradePx`, `settlementPx`, `bestBid`, `bestAsk`, `bidDepth`, `askDepth`, `longQuote`, `shortQuote`, `sharesTraded`, `openInterest`, recent price samples.
- **Recommended usage:** full `/book` for periodic authoritative L2 reconciliation; `/bbo` for frequent lightweight sanity checks.

### Settlement — `GET https://gateway.polymarket.us/v1/markets/{slug}/settlement`
Useful for the resolution pipeline. Also see richer settlement fields from the Exchange gRPC (§6).

### Events — `GET https://gateway.polymarket.us/v1/events`
Nested `markets[]`, and where applicable **sports live state** (score, period, elapsed, live/ended flags, finishedTimestamp, sport-specific state objects). Could be a genuinely useful external/fundamental feature source for sports markets.

### Price history — `GET https://gateway.polymarket.us/v1/price-history`
- `symbol=<market slug>`; `fixedInterval`: `INTERVAL_1H/6H/1D/1W/1M/ALL/LIVE` with documented `fidelity`; or custom Unix range with `fidelity=1` (≤24h).
- **CRITICAL SEMANTIC GOTCHA:** `longPrice` = YES display (normally best ask), `shortPrice` = NO display (normally 1 − best bid). **They can sum > 1 because the series preserves the spread. These are NOT historical trade prices.**
- **Do not use this endpoint as your historical-trades dataset.**
- Cacheable 30s; one market per request.

---

## 4. US Retail WebSocket — `wss://api.polymarket.us/v1/ws/markets`

**Requires API-key authentication in the handshake** (unlike International's public market WS).

| Subscription | Payload |
|--------------|---------|
| `SUBSCRIPTION_TYPE_MARKET_DATA` | full book `bids`/`offers`, `state`, `stats`, `transactTime` |
| `SUBSCRIPTION_TYPE_MARKET_DATA_LITE` | `currentPx`, `lastTradePx`, `bestBid`, `bestAsk`, `bidDepth`, `askDepth`, `sharesTraded`, `openInterest` |
| `SUBSCRIPTION_TYPE_TRADE` | `price`, `quantity`, `tradeTime`, `maker`/`taker` each `{side, intent}` |

- Max **100 markets / subscription**.
- `responsesDebounced=true` batches updates. **For research-quality raw collection, do NOT enable debouncing** unless storage is a real problem.
- **Trade streaming is surprisingly useful for microstructure:** maker/taker side + intent (`ORDER_INTENT_BUY_LONG`, `SELL_LONG`, `BUY_SHORT`, `SELL_SHORT`) gives **aggressor-flow features** far better than inferring direction from prices.
- No documented delta/price_change events — full-book snapshots (unlike International's book + deltas).

### Private execution WS — `wss://api.polymarket.us/v1/ws/private`
`SUBSCRIPTION_TYPE_ORDER`, `ORDER_SNAPSHOT`, `POSITION`, `ACCOUNT_BALANCE`, `RFQ`.
- Order updates: partial fills, trade IDs, execution states.
- Positions: before/after net positions and costs.
- Balances: buying-power changes.
- **Maps almost exactly to your intended submission → ack → order stream → partial/full fill → position stream → account reconciliation flow — no polling.**

---

## 5. Direct Exchange REST (data / reference)

### Order book (uses `symbol`, not `marketSlug`)
- `GET /v1/orderbook/{symbol}/bbo`
- `GET /v1/orderbook/{symbol}` (L2)

### Reference data (richer than retail listing)
- `POST /v1/refdata/instruments`
- `POST /v1/refdata/symbols`
- `POST /v1/refdata/metadata`

Documented instrument fields:
```
symbol, tickSize, minimumTradeQty, startDate, expirationDate, terminationDate,
state, priceScale, price limits,
event_category, event_subcategory, event_id, event_series, event_start_time,
instrument_rules, long_participant_id, long_participant_name,
short_participant_id, short_participant_name,
outcome_type, question, payoutValue, createTime, updateTime
```
- **Potentially the superior canonical instrument definition** for point-in-time metadata and resolution semantics.

### Formal instrument state lifecycle
```
PENDING → OPEN → CLOSED → EXPIRED → TERMINATED
plus SUSPENDED, HALTED, PREOPEN, MATCH_AND_CLOSE_AUCTION
```
- **`Market.active: bool` is too weak** for faithful US semantics. Needs a real state field.

### Identifier mapping (critical)
You eventually need a **three-way mapping**, not one universal identifier:
```
market_slug (retail) ↔ exchange_symbol (direct) ↔ internal_market_id
```

---

## 6. gRPC market data — the most interesting long-term collector path

Two streaming APIs:
- **`CreateMarketDataSubscription`** (one-shot subscribe)
- **`BiDirectionalStreamMarketData`** — dynamically add/remove instruments without reconnecting

Request fields: `symbols`, `unaggregated`, `depth`, `snapshot_only`.
- **Up to 1,000 symbols per stream** (vs retail WS 100).
- **Aggregated-by-price** or **unaggregated/raw** book — highly interesting for microstructure research.
- Stream messages: `heartbeat`, `update`, `subscription_ack`, `subscription_error`.
- `MarketDataUpdate`: book levels, state (where present), stats, `transact_time`, `book_hidden`.

### Settlement metadata (richer than retail)
gRPC market statistics can expose:
```
settlement_px, settlement_set_time, settlement_preliminary,
settlement_price_calculation_method, settlement_price_calculation_text
```
> **Label policy:** do NOT freeze a training label merely because `settlement_px` exists if `settlement_preliminary == true`. Record preliminary, then freeze the label only when `settlement_preliminary == false` (final). This strengthens historical-label machinery.

---

## 7. Price representation — three forms, one canonical Decimal

| Source | Representation | Canonical |
|--------|----------------|-----------|
| US Retail | `{"px": {"value": "0.555", "currency": "USD"}, "qty": "10"}` | Decimal(0.555) |
| US Exchange (REST/gRPC) | scaled integer: `px = 650`, `price_scale = 1000` (int64) | 650/1000 = 0.650 |
| International | token price string | Decimal |

- Exchange prices are **int64 scaled by `price_scale`, which can vary per instrument**.
- **The core research domain must only see the final canonical `Decimal` probability.** Normalization happens at the adapter boundary.

---

## 8. Authentication — two completely different models

### Retail (API key)
Headers:
| Header | Value |
|--------|-------|
| `X-PM-Access-Key` | Key ID |
| `X-PM-Timestamp` | current time in milliseconds |
| `X-PM-Signature` | `Ed25519(timestamp + method + path)` with secret key (base64) |

- Timestamp within **30 seconds** of server time. Secret shown once; never commit.

### Direct Exchange / institutional (RSA-signed Private Key JWT)
- RSA-signed private-key JWT → Auth0 access token (`expires_in: 180`, refresh every 3 min).
- Account-scoped endpoints additionally use **`x-participant-id`**.
- Scopes include: `read:marketdata`, `read:l2marketdata`, `read:instruments`, `read:orders`, `write:orders`, `read:reports`, `read:positions`, `read:dropcopy`, `read:accounts`, ...

> **Scope system fits your privilege-separation architecture:**
> - A data collector could hold only `read:marketdata`, `read:l2marketdata`, `read:instruments`.
> - Execution credentials hold `write:orders` in a separate process.

---

## 9. Live execution (Retail) — maps onto your live-ready shell

Endpoints: `POST /v1/orders`, `POST /v1/order/preview`, `GET /v1/orders/open`, `GET /v1/orders/{id}`, cancel, cancel-all, modify (cancel-replace), close-position, batch create/cancel/modify (≤20 each).

Order parameters:
- Types: `LIMIT`, `MARKET`
- TIF: `DAY`, `GTC`, `GTD`, `IOC`, `FOK`
- `participateDontInitiate`, `goodTillTime`, `synchronousExecution`, `slippageTolerance`
- **`participateDontInitiate=true` = maker-only primitive** (must rest; rejected if it would immediately match).

**Preview endpoint maps cleanly:**
```
OrderIntent
   ↓
LivePreTradeGate
   ↓
Polymarket /v1/order/preview
   ↓
compare expected execution
   ↓
human approval
   ↓
future venue adapter
```

---

## 10. FIX (institutional end-state only)

- Full surface: order management, execution reports, order modification/cancellation, market-data subscription, incremental market data, drop-copy.
- AWS **PrivateLink**, FIX session identity, `MsgSeqNum` sequencing, `ResendRequest`/`GapFill`, persistent sessions.
- **REST/gRPC vs FIX:** public internet + JWT vs PrivateLink + session sequencing.
- For PolyAlpha today: **overkill**. Worth knowing the route exists for a serious institutional/high-throughput future deployment.

---

## 11. Documented gRPC host inconsistency — guard against it

The US docs show **three** gRPC host naming conventions:
- `grpc-preprod.polymarketexchange.com:443` / `grpc-prod.polymarketexchange.com:443` (environments page)
- `grpc-api.preprod.polymarketexchange.com:443` / `grpc-api.prod.polymarketexchange.com:443` (dedicated gRPC docs / auth scope)
- `grpc.preprod.polymarketexchange.com:443` (a Data Guide example)

> **Do not hardcode from one docs page.** When onboarding the Exchange data API, use the host actually supplied/validated for the credentials/environment, and make it **configuration, not code**.

---

## 12. Corrected adapter architecture

Not one `PolymarketAdapter` — model as a venue family:

```
Venue
├── PolymarketInternationalAdapter
│       token_id / asset_id
│       International CLOB WS
│
└── PolymarketUS
    ├── USRetailAdapter
    │       marketSlug
    │       gateway REST (public data)
    │       authenticated market WS
    │
    └── USExchangeAdapter
            symbol
            refdata
            Exchange REST
            gRPC
            eventually FIX
```

Normalize all three into the existing domain objects: `Market`, `Book`, `Level`, `Trade`, `MarketSnapshot`.

---

## 13. Recommended US research hierarchy

| Layer | Source |
|-------|--------|
| **REFERENCE DATA** | Direct Exchange `POST /v1/refdata/instruments` (canonical symbol/rules/price_scale/lifecycle) |
| **REAL-TIME BOOK** | Direct Exchange gRPC (high-fidelity L2) |
| **CROSS-CHECK** | Retail `/v1/markets/{slug}/book` + `/bbo` |
| **MARKET/EVENT DISCOVERY** | Retail `/v1/markets`, `/v1/events` |
| **HISTORY** | `/v1/price-history` — display-price history ONLY, NOT trade history |
| **SETTLEMENT** | Exchange settlement fields + retail settlement endpoint |

The Direct Exchange **Data Guide is explicitly intended for research teams / data vendors analyzing prediction-market data** — not abusing an institutional trading API.

---

## 14. Bottom line

- US is **not compatible** with the International collector — confirmed.
- There are **two worthwhile US collector targets**: the Retail market API/WS and the Direct Exchange REST/gRPC data surface.
- The **Direct Exchange surface may be the better PolyAlpha feed**: richer reference data, configurable L2 streaming, up to 1,000 symbols/stream, scaled canonical exchange prices, richer settlement metadata, and a purpose-built read-only research onboarding path.
- **Do not mutate the already-frozen International burn-in because of this discovery.** US is a separate venue adapter and separate empirical data lineage.
- If Polymarket US is the intended operating venue, the next implementation is a **clean `USRetailAdapter` / `USExchangeAdapter` branch with its own burn-in** — not a retrofit of International parsing.