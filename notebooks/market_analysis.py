"""Market Analysis Notebook.

Analyzes the Polymarket market universe:
- Market discovery and filtering
- Liquidity and volume distributions
- Spread analysis
- Category breakdowns
- Resolution quality assessment

Usage:
    python -m notebooks.market_analysis
"""

# %%
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from datetime import UTC, datetime
from decimal import Decimal

D = Decimal

# %%
# Load configuration
from polyalpha.config import load_toml

config = load_toml("config/base.toml")
filters = config.get("market_filter", {})
print("Market filters:", filters)

# %%
# Collect a snapshot
from polyalpha.collector import Collector
from polyalpha.quality import Filter
from polyalpha.storage import Store
from polyalpha.transport import PublicHTTP

quality = Filter(
    min_liquidity=Decimal(str(filters.get("min_liquidity", 5000))),
    min_volume=Decimal(str(filters.get("min_volume", 25000))),
    max_spread=Decimal(str(filters.get("max_spread", 0.05))),
    min_depth_shares=Decimal(str(filters.get("min_depth_shares", 100))),
)

transport = PublicHTTP(timeout=30, attempts=3)

with Store(":memory:") as store:
    stats = Collector(transport, store, quality).collect(page_size=100, max_pages=1)
    print(f"Collected: {stats}")

    # %%
    # Replay collected markets
    markets = []
    for record in store.replay(datetime.now(UTC), "market"):
        try:
            from polyalpha.parsing import parse_market

            market = parse_market(record.payload, record.received_at)
            markets.append(market)
        except (ValueError, KeyError):
            continue

    print(f"\nMarkets loaded: {len(markets)}")

    if not markets:
        print("No markets collected. Check network connectivity.")
    else:
        # %%
        # Category breakdown
        categories = {}
        for m in markets:
            cat = m.category or "unknown"
            categories.setdefault(cat, []).append(m)

        print("\n--- Category Breakdown ---")
        for cat, ms in sorted(categories.items(), key=lambda x: -len(x[1])):
            avg_liq = sum(float(m.liquidity) for m in ms) / len(ms)
            avg_vol = sum(float(m.volume) for m in ms) / len(ms)
            print(f"  {cat:20s}: {len(ms):3d} markets  avg_liq=${avg_liq:,.0f}  avg_vol=${avg_vol:,.0f}")

        # %%
        # Liquidity distribution
        liquidities = [float(m.liquidity) for m in markets]
        print(f"\n--- Liquidity Distribution ---")
        print(f"  Min:    ${min(liquidities):>12,.0f}")
        print(f"  Median: ${sorted(liquidities)[len(liquidities)//2]:>12,.0f}")
        print(f"  Max:    ${max(liquidities):>12,.0f}")
        print(f"  Mean:   ${sum(liquidities)/len(liquidities):>12,.0f}")

        # %%
        # Volume distribution
        volumes = [float(m.volume) for m in markets]
        print(f"\n--- Volume Distribution ---")
        print(f"  Min:    ${min(volumes):>12,.0f}")
        print(f"  Median: ${sorted(volumes)[len(volumes)//2]:>12,.0f}")
        print(f"  Max:    ${max(volumes):>12,.0f}")

        # %%
        # Active vs inactive
        active = [m for m in markets if m.active and not m.closed]
        print(f"\n--- Status ---")
        print(f"  Active:   {len(active)}")
        print(f"  Inactive: {len(markets) - len(active)}")

        # %%
        # Order-book quality
        with_books = 0
        tight_spread = 0
        for record in store.replay(datetime.now(UTC), "book"):
            try:
                from polyalpha.parsing import parse_book

                book = parse_book(record.payload, record.received_at, record.entity_id)
                with_books += 1
                if book.spread is not None and book.spread <= D("0.05"):
                    tight_spread += 1
            except (ValueError, KeyError):
                continue

        print(f"\n--- Order Book Quality ---")
        print(f"  Books collected:       {with_books}")
        print(f"  Tight spread (<=5c):   {tight_spread}")
        if with_books:
            print(f"  Tight spread ratio:    {tight_spread/with_books:.1%}")
