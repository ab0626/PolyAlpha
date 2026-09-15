"""External data features (stub interface for plugging in real sources).

When real external data sources are connected, they should implement
the ExternalSource protocol from polyalpha.external and provide
category-specific features.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

D = Decimal


def extract_external_features(
    facts: list,
    at: datetime,
) -> dict[str, Any]:
    """Extract features from available external facts.

    This is a stub that documents the expected interface.
    Real implementations should:
    1. Parse fact features by category (politics, crypto, sports, etc.)
    2. Compute derived features (polling averages, momentum, etc.)
    3. Return category-specific feature dict.
    """
    if not facts:
        return {
            "external_fact_count": 0,
            "external_recency_hours": None,
            "has_external_data": 0,
        }

    # Sort by publication time
    sorted_facts = sorted(facts, key=lambda f: f.published_at, reverse=True)
    newest = sorted_facts[0]

    recency_hours = (at - newest.published_at).total_seconds() / 3600

    # Aggregate all feature values
    aggregated = {}
    for fact in sorted_facts:
        for key, value in fact.features.items():
            if key not in aggregated:
                aggregated[key] = []
            aggregated[key].append(value)

    # Return mean of each feature
    result = {
        "external_fact_count": len(facts),
        "external_recency_hours": D(str(recency_hours)),
        "has_external_data": 1,
    }
    for key, values in aggregated.items():
        result[f"ext_{key}"] = sum(values) / len(values)

    return result
