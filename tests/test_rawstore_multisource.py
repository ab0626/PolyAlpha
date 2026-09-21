"""Regression tests for multi-source raw-store correctness.

These pin two collector-integrity properties that a single-source raw store
design did not previously guarantee:

1. Distinct sources must land in distinct files (never collapse into the
   first source's file).
2. A fresh store instance must not reuse a finalized index (which would
   overwrite durable `.jsonl.gz` data).
"""

import sys

sys.path.insert(0, "src")

from polyalpha.rawstore import RawStore


def test_multi_source_keeps_separate_files(tmp_path):
    with RawStore(tmp_path, "v1") as store:
        store.append("src_alpha", "c1", {"n": 1}, received_at_ns=1)
        store.append("src_beta", "c2", {"n": 2}, received_at_ns=2)

    names = sorted(p.name for p in tmp_path.rglob("*.jsonl.gz"))
    assert names == ["src_alpha-0000.jsonl.gz", "src_beta-0000.jsonl.gz"]

    by_source = {r.source: r.payload for r in RawStore(tmp_path).replay()}
    assert by_source == {"src_alpha": {"n": 1}, "src_beta": {"n": 2}}


def test_interleaved_sources_stay_isolated(tmp_path):
    with RawStore(tmp_path, "v1") as store:
        store.append("src_alpha", "c", {"n": 1}, received_at_ns=1)
        store.append("src_beta", "c", {"n": 1}, received_at_ns=2)
        store.append("src_alpha", "c", {"n": 2}, received_at_ns=3)

    records = list(RawStore(tmp_path).replay())
    assert len(records) == 3
    alpha = [r.payload["n"] for r in records if r.source == "src_alpha"]
    beta = [r.payload["n"] for r in records if r.source == "src_beta"]
    assert alpha == [1, 2]
    assert beta == [1]


def test_new_instance_does_not_overwrite_finalized_file(tmp_path):
    # Instance 1 finalizes src_alpha-0000.jsonl.gz.
    with RawStore(tmp_path, "v1") as store:
        store.append("src_alpha", "c1", {"n": 1}, received_at_ns=1)
    # Instance 2 must NOT reuse index 0 and overwrite the finalized file.
    with RawStore(tmp_path, "v1") as store:
        store.append("src_alpha", "c2", {"n": 2}, received_at_ns=2)

    records = list(RawStore(tmp_path).replay())
    assert len(records) == 2
    assert [r.payload["n"] for r in records] == [1, 2]

    names = sorted(p.name for p in tmp_path.rglob("*.jsonl.gz"))
    assert names == ["src_alpha-0000.jsonl.gz", "src_alpha-0001.jsonl.gz"]


def test_open_file_coexists_with_finalized_and_advances_index(tmp_path):
    # A finalized -0000 plus an orphaned open -0001 must yield -0002 next.
    with RawStore(tmp_path, "v1") as store:
        store.append("src_alpha", "c", {"n": 1}, received_at_ns=1)
    # Simulate an orphaned open file (crash before finalization).
    with RawStore(tmp_path, "v1") as store:
        store.append("src_alpha", "c", {"n": 2}, received_at_ns=2)
        # Do NOT close: leaves src_alpha-0001.jsonl open.
        store._writers["src_alpha"].flush()

    with RawStore(tmp_path, "v1") as store:
        store.append("src_alpha", "c", {"n": 3}, received_at_ns=3)

    records = list(RawStore(tmp_path).replay())
    assert [r.payload["n"] for r in records] == [1, 2, 3]


def test_replay_skips_transiently_missing_file(tmp_path, monkeypatch):
    # Simulate the finalize/unlink race: _day_files lists a file that is gone
    # by the time replay opens it. Replay must skip it, not crash.
    with RawStore(tmp_path, "v1") as store:
        store.append("src_alpha", "c", {"n": 1}, received_at_ns=1)

    ghost = tmp_path / "src_alpha-9999.jsonl"

    def fake_day_files(self, directory):
        return [ghost]

    monkeypatch.setattr(RawStore, "_day_files", fake_day_files)
    assert list(RawStore(tmp_path).replay()) == []
