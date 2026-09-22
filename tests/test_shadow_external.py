"""Tests for shadow-external-v0: revision-safe IDs, typed values, isolation."""

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, "src")

from polyalpha.rawstore import RawStore
from polyalpha.shadow import (
    Coverage,
    ExternalCollector,
    ExternalObservation,
    FetchedObservation,
    Freshness,
    ScalarValue,
    SourceClass,
    TextValue,
    observation_id,
    revision_key,
    value_hash,
)
from polyalpha.shadow.core import SCHEMA_VERSION
from polyalpha.shadow.sources import FredProvider, _parse_rss

T = datetime(2026, 8, 1, tzinfo=UTC)


def _mk(provider="p", ext="e", event=T, rev=None, v=2.8):
    vh = value_hash(ScalarValue(value=v))
    rk = revision_key(provider, ext, event, rev, vh)
    return observation_id(SCHEMA_VERSION, rk)


def test_observation_id_is_content_based_not_local_time():
    a = _mk()
    b = _mk()
    assert a == b
    # Same content, different revision -> different ID (retained, not overwritten).
    assert _mk(rev="v2") != a
    # Different value -> different ID.
    assert _mk(v=2.7) != a


def test_value_hash_distinguishes_union_kinds():
    assert value_hash(ScalarValue(value=1.0)) != value_hash(TextValue(headline="x"))


def test_revision_creates_new_id_but_local_time_does_not():
    obs_id = _mk(provider="fred", ext="CPIAUCSL:2026-08", event=T, rev="2026-09-01", v=2.8)
    assert _mk(provider="fred", ext="CPIAUCSL:2026-08", event=T, rev="2026-09-01", v=2.8) == obs_id
    assert _mk(provider="fred", ext="CPIAUCSL:2026-08", event=T, rev="2026-10-01", v=2.7) != obs_id


class _Fake:
    name = "fake"
    source_class = SourceClass.MACRO
    freshness_class = Freshness.HISTORICAL
    coverage_class = Coverage.FULL

    def __init__(self, items):
        self.items = [FetchedObservation(b"raw-wire", o) for o in items]

    def fetch(self, since):
        return self.items


def _observation(ext="cpi", v=2.8):
    val = ScalarValue(value=v)
    return ExternalObservation(
        observation_id=_mk(provider="fake", ext=ext, event=T, v=v),
        provider="fake", source_class=SourceClass.MACRO, external_id=ext,
        value=val, publisher_or_authority=None, event_time=T,
        provider_published_at=T, provider_updated_at=None, source_claimed_publish_at=T,
        request_started_wall_ns=0, received_wall_ns=0, received_monotonic_ns=0,
        region=None, source_url=None, raw_payload_sha256="", content_sha256="",
        collector_session_id="s", fetch_sequence=0, transport="REST",
        freshness_class=Freshness.HISTORICAL, coverage_class=Coverage.FULL,
        provider_revision=None, value_hash=value_hash(val),
    )


def test_collector_dedups_same_revision(tmp_path):
    raw = RawStore(tmp_path / "raw", "v1")
    o = _observation()
    collector = ExternalCollector([_Fake([o, o])], raw, tmp_path / "wm.json")
    stats = collector.collect()
    assert stats["providers"]["fake"]["new"] == 1  # duplicate revision deduped
    assert stats["providers"]["fake"]["seen"] == 1


def test_collector_does_not_reappend_unchanged(tmp_path):
    raw = RawStore(tmp_path / "raw", "v1")
    o = _observation()
    collector = ExternalCollector([_Fake([o])], raw, tmp_path / "wm.json")
    assert collector.collect()["providers"]["fake"]["new"] == 1
    # Re-poll the same unchanged datum: zero new, and the raw store stays at 1.
    assert collector.collect()["providers"]["fake"]["new"] == 0
    assert len(list(RawStore(tmp_path / "raw").replay())) == 1


def test_collector_seen_persists_across_restart(tmp_path):
    raw = RawStore(tmp_path / "raw", "v1")
    o = _observation()
    ExternalCollector([_Fake([o])], raw, tmp_path / "wm.json").collect()
    # Restart (fresh collector) over the same store + watermark: seen persists.
    c2 = ExternalCollector([_Fake([o])], raw, tmp_path / "wm.json")
    assert c2.collect()["providers"]["fake"]["new"] == 0
    assert len(list(RawStore(tmp_path / "raw").replay())) == 1


def test_collector_preserves_raw_wire_bytes(tmp_path):
    import base64

    raw = RawStore(tmp_path / "raw", "v1")
    o = _observation()
    ExternalCollector([_Fake([o])], raw, tmp_path / "wm.json").collect()
    rec = list(RawStore(tmp_path / "raw").replay())[0]
    assert rec.wire_was_bytes is True  # raw bytes stored, not the normalized dict
    assert base64.b64decode(rec.wire) == b"raw-wire"


def test_obs_computes_raw_payload_sha256():
    import hashlib

    from polyalpha.shadow.sources import _obs

    obs = _obs("p", SourceClass.MACRO, Freshness.HISTORICAL, Coverage.FULL,
               "eid", ScalarValue(value=1.0), T, T, None, None, None, None,
               raw_bytes=b"raw")
    assert obs.raw_payload_sha256 == hashlib.sha256(b"raw").hexdigest()


def test_rss_parse_rss2():
    xml = (
        b'<rss version="2.0"><channel><item><guid>g1</guid><title>t</title>'
        b'<link>http://x</link><pubDate>Tue, 01 Oct 2026 08:30:00 -0400</pubDate>'
        b'</item></channel></rss>'
    )
    items = _parse_rss(xml)
    assert len(items) == 1
    assert items[0][0] == "g1"


def test_fred_missing_credentials(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        FredProvider(["CPIAUCSL"]).fetch(None)


def test_v0_4_does_not_read_shadow_external():
    frozen = [
        "src/polyalpha/event_shock.py",
        "src/polyalpha/propagation.py",
        "src/polyalpha/integration.py",
        "src/polyalpha/information.py",
        "src/polyalpha/gov_releases.py",
        "src/polyalpha/marginal_risk.py",
        "src/polyalpha/us/families.py",
    ]
    for path in frozen:
        src = Path(path).read_text(encoding="utf-8")
        assert "shadow" not in src, f"{path} references the shadow layer"
