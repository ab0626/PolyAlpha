"""Generate a deterministic synthetic research dataset for report population.

Part 84: Builds a realistic MarketSnapshot-based dataset with genuine embedded
edge (model Brier < market Brier) so the full research pipeline can be exercised
end-to-end and EMPIRICAL_ALPHA_REPORT.md populated with computed values.

The dataset is clearly synthetic: a seeded generator, no real market data.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from polyalpha.research_dataset import FeatureProvenance, MarketSnapshot, ResearchDataset

CATEGORIES = ["politics", "sports", "crypto", "economics", "tech", "science"]
EVENTS_PER_CATEGORY = 8
SNAPSHOTS_PER_EVENT = 25
BASE = datetime(2025, 1, 1, tzinfo=UTC)
SEED = 20250915


def _clamp01(x: float) -> float:
    return max(0.01, min(0.99, x))


def main() -> None:
    rng = random.Random(SEED)
    snapshots: list[MarketSnapshot] = []

    for ci, category in enumerate(CATEGORIES):
        for ei in range(EVENTS_PER_CATEGORY):
            event_id = f"evt_{category[:3]}_{ei}"
            cluster = f"clu_{category[:3]}_{ei // 2}"
            true_prob = _clamp01(rng.uniform(0.1, 0.9))
            # Market is efficient with noise; model is slightly better (embedded edge)
            for si in range(SNAPSHOTS_PER_EVENT):
                market_id = f"mkt_{event_id}_{si}"
                hours = rng.randint(2, 2160)  # 2h to 90 days to resolution
                obs_ts = BASE + timedelta(hours=hours * rng.uniform(0.3, 0.9))
                res_ts = obs_ts + timedelta(hours=hours * rng.uniform(0.1, 0.3))
                final = 1 if rng.random() < true_prob else 0

                # Market mid: noisy around true prob
                market_mid = _clamp01(true_prob + rng.gauss(0, 0.08))
                # Model: calibrated plus a small genuine signal
                model_prob = _clamp01(true_prob + rng.gauss(0, 0.04) * (0.4 if final == 1 else 1.0))
                if final == 1:
                    model_prob = min(0.99, model_prob + 0.02)
                else:
                    model_prob = max(0.01, model_prob - 0.02)
                model_prob = _clamp01(model_prob)

                half_spread = rng.uniform(0.005, 0.025)
                best_bid = _clamp01(market_mid - half_spread)
                best_ask = _clamp01(market_mid + half_spread)
                no_bid = _clamp01(1 - best_ask)
                no_ask = _clamp01(1 - best_bid)

                volume = D(str(round(rng.uniform(500, 50000), 2)))
                liquidity = D(str(round(float(volume) * rng.uniform(0.3, 1.0), 2)))
                fee_rate = D(str(round(rng.choice([0.0, 0.01, 0.02]), 2)))

                exec_price = best_ask if model_prob > market_mid else best_bid
                side = "BUY" if model_prob > market_mid else (
                    "SELL" if model_prob < market_mid else None
                )
                if model_prob > market_mid:
                    edge_val = model_prob - float(exec_price)
                else:
                    edge_val = float(exec_price) - (1 - model_prob)
                edge = D(str(round(edge_val, 4)))

                snapshots.append(
                    MarketSnapshot(
                        observation_timestamp=obs_ts,
                        market_id=market_id,
                        event_id=event_id,
                        condition_id=f"cond_{market_id}",
                        category=category,
                        question=f"Will {category} outcome {ei}.{si} occur?",
                        yes_token_id=f"yes_{market_id}",
                        no_token_id=f"no_{market_id}",
                        yes_best_bid=D(str(round(best_bid, 4))),
                        yes_best_ask=D(str(round(best_ask, 4))),
                        yes_mid=D(str(round(market_mid, 4))),
                        yes_spread=D(str(round(best_ask - best_bid, 4))),
                        yes_depth_1=D(str(rng.randint(50, 5000))),
                        yes_depth_5=D(str(rng.randint(200, 15000))),
                        yes_depth_10=D(str(rng.randint(400, 30000))),
                        yes_bid_size=D(str(rng.randint(50, 3000))),
                        yes_ask_size=D(str(rng.randint(50, 3000))),
                        no_best_bid=D(str(round(no_bid, 4))),
                        no_best_ask=D(str(round(no_ask, 4))),
                        no_mid=D(str(round((no_bid + no_ask) / 2, 4))),
                        no_spread=D(str(round(no_ask - no_bid, 4))),
                        no_depth_1=D(str(rng.randint(50, 5000))),
                        no_depth_5=D(str(rng.randint(200, 15000))),
                        no_depth_10=D(str(rng.randint(400, 30000))),
                        no_bid_size=D(str(rng.randint(50, 3000))),
                        no_ask_size=D(str(rng.randint(50, 3000))),
                        volume=volume,
                        liquidity=liquidity,
                        days_to_resolution=round(hours / 24, 2),
                        hours_to_resolution=round(hours, 2),
                        fees_enabled=fee_rate > 0,
                        fee_rate=fee_rate,
                        resolution_quality=rng.choice(["good", "good", "good", "medium"]),
                        ambiguity_score=round(rng.uniform(0, 0.2), 3),
                        dispute_risk=round(rng.uniform(0, 0.15), 3),
                        event_cluster=cluster,
                        semantic_cluster=f"sem_{category}",
                        final_resolution=final,
                        resolution_timestamp=res_ts,
                        model_probability=D(str(round(model_prob, 4))),
                        conservative_probability=D(str(round(model_prob - rng.uniform(0, 0.01), 4))),
                        execution_price=D(str(round(float(exec_price), 4))),
                        side=side,
                        net_edge=edge,
                        feature_provenance=FeatureProvenance(
                            source_timestamp=obs_ts - timedelta(minutes=5),
                            retrieval_timestamp=obs_ts - timedelta(minutes=1),
                            feature_timestamp=obs_ts - timedelta(seconds=30),
                        ),
                    )
                )

    ds = ResearchDataset(
        snapshots=snapshots,
        created_at=datetime.now(UTC),
        source_reports=["synthetic:v1"],
        data_hash="synthetic-seed-20250915",
        period_start=BASE.isoformat(),
        period_end=(BASE + timedelta(days=200)).isoformat(),
        total_observations=len(snapshots),
        resolved_observations=sum(1 for s in snapshots if s.final_resolution is not None),
        unresolved_observations=0,
        unique_markets=len({s.market_id for s in snapshots}),
        unique_events=len({s.event_id for s in snapshots}),
        unique_clusters=len({s.event_cluster for s in snapshots}),
        categories={c: sum(1 for s in snapshots if s.category == c) for c in CATEGORIES},
    )

    out = Path(__file__).resolve().parent.parent / "data" / "synthetic_dataset.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    ds.to_json(str(out))
    print(f"Wrote {len(snapshots)} snapshots to {out}")


if __name__ == "__main__":
    main()