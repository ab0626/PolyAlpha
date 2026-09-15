"""Scan explicitly reviewed partitions against a point-in-time public archive."""

import argparse
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .graph import ConstraintGraph, ReviewedNode
from .monitoring import code_provenance
from .parsing import parse_book, parse_market
from .relative_value import Constraint
from .resolution import ResolutionReview
from .storage import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--reviews", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--shares", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    at = datetime.fromisoformat(args.as_of)
    raw = json.loads(Path(args.reviews).read_text(encoding="utf-8-sig"))
    nodes, books = [], {}
    with Store(args.database) as store:
        for member, review in raw["nodes"].items():
            record = store.latest("market", member, at)
            if record is None:
                raise ValueError(f"missing historical metadata: {member}")
            market = parse_market(record.payload, record.received_at)
            nodes.append(ReviewedNode(market, ResolutionReview.from_dict(review)))
            record = store.latest("book", market.yes_token_id, at)
            if record:
                books[market.yes_token_id] = parse_book(
                    record.payload, record.received_at, market.yes_token_id
                )
    rules = [
        Constraint(
            r["kind"],
            tuple(r["markets"]),
            r["review_reference"],
            datetime.fromisoformat(r["reviewed_at"]),
        )
        for r in raw["rules"]
    ]
    graph = ConstraintGraph(nodes, rules)
    reports = [
        graph.quote_partition(rule, books, Decimal(args.shares), at)
        for rule in rules
        if rule.kind == "partition"
    ]
    with Path(args.output).open("x", encoding="utf-8") as output:
        json.dump(
            dict(
                status="research_only",
                cutoff=at.isoformat(),
                reviews=raw,
                provenance=code_provenance(),
                baskets=reports,
            ),
            output,
            indent=2,
        )


if __name__ == "__main__":
    main()
