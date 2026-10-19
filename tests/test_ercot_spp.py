"""Correctness tests for the E4 NP6-905-CD SPP collector (no live credentials).

Covers the four required checks via fixtures:
  1. HB_WEST filtering + field normalization (parse correctness).
  2. duplicate unchanged fetch does not create a second is_first_seen.
  3. a changed/revised value appends a new row linked to the same natural key.
  4. restart/replay preserves first-seen classification deterministically.
"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "scripts")

import collect_ercot_spp as coll  # noqa: E402

from polyalpha.energy.ercot_spp import (  # noqa: E402
    HUB,
    natural_interval_key,
    parse_spp,
)


def _dict_payload() -> bytes:
    return json.dumps({
        "data": [
            {
                "deliveryDate": "2026-10-19", "deliveryHour": 16, "deliveryInterval": 15,
                "DSTFlag": False, "settlementPoint": "HB_WEST", "settlementPointPrice": 25.43,
            },
            {
                "deliveryDate": "2026-10-19", "deliveryHour": 16, "deliveryInterval": 15,
                "DSTFlag": False, "settlementPoint": "HB_NORTH", "settlementPointPrice": 26.00,
            },
            {
                "deliveryDate": "2026-10-19", "deliveryHour": 16, "deliveryInterval": 15,
                "DSTFlag": False, "settlementPoint": "LZ_HOUSTON", "settlementPointPrice": 24.00,
            },
        ]
    }).encode()


def _rows_payload() -> bytes:
    return json.dumps({
        "fields": [
            {"name": "deliveryDate"}, {"name": "deliveryHour"}, {"name": "deliveryInterval"},
            {"name": "DSTFlag"}, {"name": "settlementPoint"}, {"name": "settlementPointPrice"},
        ],
        "data": [
            ["2026-10-19", 16, 15, False, "HB_WEST", 25.43],
            ["2026-10-19", 16, 15, False, "HB_NORTH", 26.00],
        ],
    }).encode()


def _obs(price="25.43", flag="N"):
    return {
        "operating_day": "2026-10-19",
        "delivery_hour": "16",
        "delivery_interval": "15",
        "repeated_hour_flag": flag,
        "hub": HUB,
        "price": price,
    }


# 1. parse + filter correctness
def test_parse_filters_to_hb_west_dict_payload():
    obs = parse_spp(_dict_payload())
    assert len(obs) == 1
    assert obs[0]["hub"] == HUB
    assert obs[0]["price"] == "25.43"


def test_parse_filters_to_hb_west_rows_payload():
    obs = parse_spp(_rows_payload())
    assert len(obs) == 1
    assert obs[0]["operating_day"] == "2026-10-19"
    assert obs[0]["repeated_hour_flag"] == "N"


def test_natural_key_includes_repeated_hour_flag():
    a = natural_interval_key(_obs(flag="N"))
    b = natural_interval_key(_obs(flag="Y"))
    assert a != b  # DST duplicate must NOT collapse to the same key


# 2/3/4. first-seen vs revision classification
def _append(obs_list, seen, raw_sha="abc", received_at="2026-10-19T15:00:00Z"):
    return coll._append_observations(obs_list, raw_sha, received_at, seen)


def _read_rows(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_duplicate_unchanged_no_second_first_seen(tmp_path, monkeypatch):
    monkeypatch.setattr(coll, "OBS_PATH", tmp_path / "obs.jsonl")
    monkeypatch.setattr(coll, "RAW_DIR", tmp_path / "raw")
    seen = {}
    assert _append([_obs()], seen) == 1  # first-seen
    assert _append([_obs()], seen) == 0  # unchanged duplicate -> skip
    rows = _read_rows(tmp_path / "obs.jsonl")
    assert len(rows) == 1
    assert rows[0]["is_first_seen"] is True


def test_revision_appends_new_row_same_key(tmp_path, monkeypatch):
    monkeypatch.setattr(coll, "OBS_PATH", tmp_path / "obs.jsonl")
    monkeypatch.setattr(coll, "RAW_DIR", tmp_path / "raw")
    seen = {}
    _append([_obs(price="25.43")], seen)
    _append([_obs(price="26.00")], seen)  # changed price -> revision
    rows = _read_rows(tmp_path / "obs.jsonl")
    assert len(rows) == 2
    assert rows[0]["is_first_seen"] is True
    assert rows[1]["is_first_seen"] is False
    assert rows[0]["natural_key"] == rows[1]["natural_key"]
    assert rows[1]["price"] == "26.00"


def test_restart_preserves_first_seen_deterministically(tmp_path, monkeypatch):
    monkeypatch.setattr(coll, "OBS_PATH", tmp_path / "obs.jsonl")
    monkeypatch.setattr(coll, "RAW_DIR", tmp_path / "raw")
    seen = {}
    _append([_obs()], seen)
    # "restart": reload seen from the persisted store, then re-observe unchanged.
    seen2 = coll._load_seen()
    assert _append([_obs()], seen2) == 0  # no second first-seen after replay
    rows = _read_rows(tmp_path / "obs.jsonl")
    assert len(rows) == 1
    assert rows[0]["is_first_seen"] is True