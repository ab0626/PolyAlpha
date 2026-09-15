"""CSV external facts with explicit timestamps; no inferred publication date."""

import csv
from datetime import datetime

from .external import ExternalFact, FixtureSource


class CSVSource(FixtureSource):
    def __init__(self, path):
        facts = []
        with open(path, encoding="utf-8-sig", newline="") as source:
            for row in csv.DictReader(source):
                facts.append(
                    ExternalFact(
                        row["event_id"],
                        row["source_url"],
                        datetime.fromisoformat(row["published_at"]),
                        datetime.fromisoformat(row["retrieved_at"]),
                        {k: float(v) for k, v in row.items() if k.startswith("feature_")},
                    )
                )
        super().__init__(facts)
