#!/usr/bin/env python3
"""Build training dataset from collected data.

Usage:
    python scripts/build_dataset.py --database data/polyalpha.sqlite --output data/dataset.csv
"""

import argparse
import csv
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from polyalpha.features.orderbook import extract_all_features
from polyalpha.parsing import parse_book, parse_market
from polyalpha.storage import Store


def main():
    parser = argparse.ArgumentParser(description="Build training dataset")
    parser.add_argument("--database", default="data/polyalpha.sqlite")
    parser.add_argument("--output", default="data/dataset.csv")
    parser.add_argument("--market-id", help="Filter to specific market")
    args = parser.parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with Store(args.database) as store:
        # Load all markets
        markets = {}
        for receipt in store.replay(datetime.now(UTC), "market"):
            market = parse_market(receipt.payload, receipt.received_at)
            if args.market_id and market.market_id != args.market_id:
                continue
            markets[market.market_id] = market

        # Load all books and extract features
        rows = []
        for receipt in store.replay(datetime.now(UTC), "book"):
            try:
                book = parse_book(receipt.payload, receipt.received_at, receipt.entity_id)
            except ValueError:
                continue

            # Find the market for this book
            market = None
            for m in markets.values():
                if m.yes_token_id == book.token_id or m.no_token_id == book.token_id:
                    market = m
                    break

            if market is None:
                continue

            # Extract features
            try:
                feats = extract_all_features(
                    book,
                    market.received_at,
                    [],
                    [],
                )
            except ValueError:
                continue

            # Determine side
            is_yes = book.token_id == market.yes_token_id
            side = "YES" if is_yes else "NO"

            row = {
                "market_id": market.market_id,
                "token_id": book.token_id,
                "side": side,
                "timestamp": receipt.received_at.isoformat(),
                "condition_id": book.condition_id,
                "category": market.category,
            }
            row.update({k: str(v) for k, v in feats.items()})
            rows.append(row)

        # Write CSV
        if rows:
            fieldnames = list(rows[0].keys())
            with open(output_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            print(f"Wrote {len(rows)} rows to {output_path}")
        else:
            print("No data to export")


if __name__ == "__main__":
    main()
