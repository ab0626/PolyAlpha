"""Optional PostgreSQL receipt archive implementing the Store read/write contract."""

import hashlib
import json
from pathlib import Path

from .domain import utc
from .storage import Record


class PostgresStore:
    def __init__(self, dsn):
        import psycopg

        self.connection = psycopg.connect(dsn, autocommit=True)

    def initialize(self, schema_path="sql/schema.sql"):
        with self.connection.transaction():
            self.connection.execute(Path(schema_path).read_text(encoding="utf-8-sig"))

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def append(self, kind, entity_id, received_at, payload, source_at=None):
        from psycopg.types.json import Jsonb

        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self.connection.transaction():
            row = self.connection.execute(
                "INSERT INTO receipts(kind,entity_id,received_at,"
                "source_at,payload,sha256) VALUES(%s,%s,%s,%s,%s,%s)"
                " RETURNING id",
                (
                    kind,
                    entity_id,
                    utc(received_at),
                    utc(source_at) if source_at else None,
                    Jsonb(payload),
                    hashlib.sha256(encoded.encode()).hexdigest(),
                ),
            ).fetchone()
        return row[0]

    def replay(self, as_of, kind=None):
        cutoff = utc(as_of)
        rows = self.connection.execute(
            "SELECT id,kind,entity_id,received_at,source_at,payload FROM receipts "
            "WHERE received_at<=%s AND (source_at IS NULL OR source_at<=%s) "
            "AND (%s::text IS NULL OR kind=%s) ORDER BY received_at,id",
            (cutoff, cutoff, kind, kind),
        )
        for row in rows:
            yield Record(*row)

    def latest(self, kind, entity_id, as_of):
        rows = (r for r in self.replay(as_of, kind) if r.entity_id == entity_id)
        return max(
            rows,
            key=lambda r: (r.source_at or r.received_at, r.received_at, r.id),
            default=None,
        )
