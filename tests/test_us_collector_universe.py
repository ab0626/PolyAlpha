"""Regression: the US collector must target OPEN markets/events, not the
default sort's already-resolved/expired universe."""

import sys
from datetime import UTC, datetime

sys.path.insert(0, "src")

from polyalpha.us.collector import UsRawCollector
from polyalpha.us.identifiers import UsIdentifierRegistry

NOW = datetime(2026, 9, 21, tzinfo=UTC)


class _RecClient:
    def __init__(self):
        self.market_params = None
        self.event_params = None

    def markets(self, params=None):
        self.market_params = params
        return {"markets": [{"slug": "s1", "question": "Q", "category": "sports"}]}, NOW

    def events(self, params=None):
        self.event_params = params
        return {"events": []}, NOW


def test_discover_markets_filters_closed_false(tmp_path):
    from polyalpha.rawstore import RawStore

    client = _RecClient()
    with RawStore(tmp_path, "t") as raw:
        UsRawCollector(client, raw, UsIdentifierRegistry()).discover_markets()
    assert client.market_params.get("closed") == "false"


def test_collect_events_filters_closed_false(tmp_path):
    from polyalpha.rawstore import RawStore

    client = _RecClient()
    with RawStore(tmp_path, "t") as raw:
        UsRawCollector(client, raw, UsIdentifierRegistry()).collect_events(
            {"limit": 25, "offset": 0}
        )
    assert client.event_params.get("closed") == "false"
