-- Comprehensive PostgreSQL schema for polymarket-alpha research infrastructure.
-- SQLite remains the executable default backend; this is for optional archival.

-- ============================================================
-- CORE RECEIPT TABLE (append-only)
-- ============================================================
CREATE TABLE IF NOT EXISTS receipts (
  id BIGSERIAL PRIMARY KEY,
  kind TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  received_at TIMESTAMPTZ NOT NULL,
  source_at TIMESTAMPTZ,
  payload JSONB NOT NULL,
  sha256 TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS receipt_lookup ON receipts(kind,entity_id,received_at,id);
CREATE INDEX IF NOT EXISTS receipt_kind_time ON receipts(kind,received_at);

-- Append-only trigger
CREATE OR REPLACE FUNCTION forbid_receipt_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'append-only archive'; END; $$;
DROP TRIGGER IF EXISTS immutable_receipt ON receipts;
CREATE TRIGGER immutable_receipt BEFORE UPDATE OR DELETE ON receipts
FOR EACH ROW EXECUTE FUNCTION forbid_receipt_mutation();

-- ============================================================
-- CONVENIENCE VIEWS
-- ============================================================
CREATE OR REPLACE VIEW markets AS
  SELECT id, entity_id AS market_id, received_at, source_at, payload
  FROM receipts WHERE kind='market';

CREATE OR REPLACE VIEW market_snapshots AS
  SELECT id, received_at, entity_id, payload
  FROM receipts WHERE kind='market_quality';

CREATE OR REPLACE VIEW orderbook_snapshots AS
  SELECT id, received_at, entity_id AS token_id, source_at, payload
  FROM receipts WHERE kind='book';

CREATE OR REPLACE VIEW raw_books AS
  SELECT id, received_at, entity_id AS token_id, payload
  FROM receipts WHERE kind='raw_book';

CREATE OR REPLACE VIEW forecasts AS
  SELECT id, received_at, entity_id, payload
  FROM receipts WHERE kind='forecast';

CREATE OR REPLACE VIEW signals AS
  SELECT id, received_at, entity_id, payload
  FROM receipts WHERE kind='signal';

CREATE OR REPLACE VIEW paper_orders AS
  SELECT id, received_at, entity_id, payload
  FROM receipts WHERE kind='paper_order';

CREATE OR REPLACE VIEW paper_fills AS
  SELECT id, received_at, entity_id, payload
  FROM receipts WHERE kind='paper_fill';

CREATE OR REPLACE VIEW portfolio_snapshots AS
  SELECT id, received_at, entity_id, payload
  FROM receipts WHERE kind='portfolio';

CREATE OR REPLACE VIEW external_information AS
  SELECT id, received_at, entity_id, payload
  FROM receipts WHERE kind='external';

CREATE OR REPLACE VIEW settlements AS
  SELECT id, received_at, entity_id, payload
  FROM receipts WHERE kind='settlement';

CREATE OR REPLACE VIEW trade_activity AS
  SELECT id, received_at, entity_id, payload
  FROM receipts WHERE kind='trade';

-- ============================================================
-- AGGREGATE ANALYTICS VIEWS
-- ============================================================

-- Market summary with latest snapshot
CREATE OR REPLACE VIEW market_summary AS
WITH latest_market AS (
  SELECT DISTINCT ON (entity_id)
    entity_id AS market_id, received_at, payload
  FROM receipts WHERE kind='market'
  ORDER BY entity_id, received_at DESC
),
latest_book AS (
  SELECT DISTINCT ON (entity_id)
    entity_id AS token_id, received_at, source_at, payload
  FROM receipts WHERE kind='book'
  ORDER BY entity_id, received_at DESC
)
SELECT
  m.market_id,
  m.received_at AS last_metadata_at,
  m.payload->>'question' AS question,
  m.payload->>'category' AS category,
  (m.payload->>'liquidity')::numeric AS liquidity,
  (m.payload->>'volume')::numeric AS volume,
  b.source_at AS last_book_at,
  b.payload->>'best_bid' AS best_bid,
  b.payload->>'best_ask' AS best_ask
FROM latest_market m
LEFT JOIN latest_book b ON b.token_id = (m.payload->>'yes_token_id');

-- Fill summary
CREATE OR REPLACE VIEW fill_summary AS
SELECT
  entity_id AS token_id,
  COUNT(*) AS total_fills,
  SUM((payload->>'shares')::numeric) AS total_shares,
  SUM((payload->>'notional')::numeric) AS total_notional,
  SUM((payload->>'fees')::numeric) AS total_fees,
  AVG((payload->>'vwap')::numeric) AS avg_vwap
FROM receipts WHERE kind='paper_fill'
GROUP BY entity_id;

-- Daily portfolio snapshots
CREATE OR REPLACE VIEW daily_portfolio AS
SELECT
  DATE(received_at) AS day,
  MIN(received_at) AS first_snapshot,
  MAX(received_at) AS last_snapshot,
  COUNT(*) AS snapshots
FROM receipts WHERE kind='portfolio'
GROUP BY DATE(received_at)
ORDER BY day DESC;

-- ============================================================
-- INDEXES FOR ANALYTICS
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_receipts_entity ON receipts(entity_id);
CREATE INDEX IF NOT EXISTS idx_receipts_kind_time ON receipts(kind, received_at);
CREATE INDEX IF NOT EXISTS idx_receipts_source_at ON receipts(source_at) WHERE source_at IS NOT NULL;
