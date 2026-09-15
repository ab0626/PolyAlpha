# Research contracts and extension points

## Reviewed exposure map

`config/clusters.json` maps Gamma market IDs to explicitly reviewed exposure and resolution definitions. The distributed file is empty. The following is a schema example, not a valid real-market approval:

```json
{
  "<market-id>": {
    "event": "<shared-event-id>",
    "cluster": "<broader-correlated-event-family>",
    "category": "<category>",
    "resolution_review": {
      "definition_sha256": "<64-character digest from resolution.definition_hash(market)>",
      "reviewed_at": "2026-09-12T00:00:00+00:00",
      "reference": "<review document or official resolution source>",
      "clarity": "0.9",
      "ambiguity": "0.1",
      "dispute_risk": "0.1"
    }
  }
}
```

Assess these values rather than copying the examples. The definition hash covers question, description, deadline and resolution source. A changed definition requires a new review. Standard CLI engines require the review to have been available by the simulated decision timestamp. For synthetic unit tests, direct construction of `Engine` can omit this requirement; those results must remain labelled synthetic.

Correlated state, national, party and candidate contracts should share a broader cluster when appropriate. A Gamma event identifier alone is not a proof of independence. Reviewed implication/partition constraints are separate from risk clusters: a valid implication is not established by correlation.

## External facts

`CSVSource` accepts these columns:

```text
event_id,source_url,published_at,retrieved_at,feature_<name>,...
```

Dates must include timezones; feature values must be finite. An external fact is available only after both publication and retrieval. Each revision must be a separate row with its own receipt time. Do not overwrite historical forecasts with revised economic series or final election results.

Implement `ExternalSource.available(event_id, at)` for a real public-data provider and `ProbabilityModel.predict(market_id, book, at)` for a model that consumes available facts. A model must return probability, lower/upper conservative bounds, decision time and model version. Fit category models on actual appropriately licensed data; the repository does not invent a polling, sports, macro, or news API.

The supplied `FixtureSource` is an injected-data mock, and `CSVSource` is a file adapter. Neither is presented as a predictive model.

## Calibration observations

```text
market_id,cluster,predicted_at,label_known_at,probability,outcome
```

Outcomes are binary 0/1. `label_known_at` is when the resolution label became available to this research pipeline, not a retrospectively convenient event date. Predictions made after a known outcome are rejected. A 0.5 void/split payout is valid accounting input but is not automatically a binary calibration label.

Use one predeclared forecast horizon per market for meaningful calibration research. Current fitting/scoring uses the earliest included prediction per market to avoid tick-frequency overweighting. Market-level metrics and cluster counts do not establish independent-event confidence bounds. Walk-forward fitting purges clusters appearing in the evaluation fold; folds without adequate mature labels are explicitly skipped.

## Settlement receipts

The v2 adapter automatically creates settlement receipts only for terminal reported BINARY resolutions with explicit valid payouts and transaction provenance. It converts micro-USDC payouts using original token order and uses retrieval time as `known_at`.

For a separately reviewed source, `Store.append('settlement', token_id, received_at, payload, source_at)` expects:

```json
{
  "token_id": "<outcome-token>",
  "payout": "0 or 1, or an explicitly verified split payout",
  "known_at": "<timezone-aware ISO receipt time>",
  "source_url": "<authoritative source>",
  "verified": true
}
```

Supply both outcome-token payouts when appropriate. A quoted market percentage or a price reaching 0.99 is not a settlement. Disputed/proposed and unsupported resolution types remain raw observations until validated.

## Storage contract

Collectors consume a store with `append(kind, entity_id, received_at, payload, source_at=None)`. Replay yields `Record` objects in receipt-time order, bounded by both receipt and source timestamps. `latest` prefers the most recent available source state, preventing a delayed old snapshot from replacing newer state.

SQLite is the default implementation. For the optional PostgreSQL archive:

```python
import os
from polyalpha.postgres import PostgresStore

with PostgresStore(os.environ['DATABASE_URL']) as store:
    store.initialize('sql/schema.sql')
    # Inject store into Collector, Engine replay, or archive.export_jsonl.
```

Install the `postgres` optional dependency first. This backend and schema passed a disposable PostgreSQL 17 integration test. Repeat it with `python scripts/verify_postgres.py` after installing the `postgres` extra and starting Docker. Use a dedicated research database and a restricted database role; database administrators can bypass application-level append-only protections.

`archive.export_jsonl` creates a write-once portable export. `archive.read_jsonl` validates ordering. Reports include the input-stream hash, source-code hash, configuration, cluster mapping, fill journal, positions and breaker state. Git commit is null when the repository has no commit; source hash still records the working code.

## Interpretation boundaries

- Baseline forecasts and uncertainty bands are diagnostics; their numerical parameters are not established alpha or confidence coverage.
- The standalone maker queue consumes aggressive traded volume after queue-ahead. It does not infer fills from price touches or model hidden queues and cancellation priority.
- Trade receipts can repeat across collection pages. Transaction hashes alone are not unique execution IDs.
- Added slippage and resolution penalties are entry uncertainty buffers. Recorded VWAP already reflects observed depth and latency-arrival prices.
- Paper reports may show `net_pnl: null` when liquidation is incomplete, accompanied by an explicit lower bound. Do not interpret unpriced holdings as a confirmed realized loss.
- `performance.performance` refuses annualization without a declared regular observation grid. The default event-driven report does not invent a Sharpe ratio.
- Geographic status is informational; all shipped execution remains paper-only. The relayer, bridge and order-management APIs are intentionally unused because no funds or real orders are involved.
