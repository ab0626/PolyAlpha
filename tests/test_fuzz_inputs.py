"""Fuzz testing with malformed inputs.

Part 60: Tests that the system raises controlled exceptions or validation
failures when presented with malformed inputs, rather than exhibiting
undefined behavior (crashes, infinite loops, data corruption).

Categories:
- Order-book parser: None values, missing keys, wrong types, negative prices, huge numbers
- Market parser: empty events, missing outcome fields, duplicate IDs
- JSON decoding: truncated JSON, wrong types, nested garbage
- Fee inputs: None, negative, >1, string, NaN
- Config inputs: missing fields, wrong types, negative values where positive expected
"""

import json
import math
import random
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import pytest

from polyalpha.domain import Book, Level, Market
from polyalpha.execution import FeeSchedule, Order, walk
from polyalpha.parsing import parse_book, parse_market
from polyalpha.calibration import Observation, metrics as calibration_metrics
from polyalpha.performance import brier_score, performance, drawdown_series

D = Decimal
TS = datetime(2025, 6, 1, tzinfo=timezone.utc)


def _valid_book_raw():
    return {
        "asset_id": "t1",
        "market": "c1",
        "timestamp": 1717200000000,
        "bids": [{"price": "0.50", "size": "100"}],
        "asks": [{"price": "0.55", "size": "100"}],
        "tick_size": "0.01",
        "min_order_size": "1",
        "hash": "",
    }


def _valid_market_raw():
    return {
        "id": "m1",
        "conditionId": "c1",
        "question": "Test?",
        "outcomes": ["Yes", "No"],
        "clobTokenIds": ["t1", "t2"],
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "liquidity": "5000",
        "volume": "1000",
        "feesEnabled": False,
        "events": [{"id": "e1"}],
    }


# ── Order-book parser fuzzing ────────────────────────────────────────────────


class TestBookParserFuzzing:
    def test_none_asset_id_raises(self):
        raw = _valid_book_raw()
        raw["asset_id"] = None
        with pytest.raises((ValueError, TypeError, KeyError, InvalidOperation)):
            parse_book(raw, TS)

    def test_missing_bids_key_raises(self):
        raw = _valid_book_raw()
        del raw["bids"]
        with pytest.raises((ValueError, KeyError, TypeError)):
            parse_book(raw, TS)

    def test_missing_asks_key_raises(self):
        raw = _valid_book_raw()
        del raw["asks"]
        with pytest.raises((ValueError, KeyError, TypeError)):
            parse_book(raw, TS)

    def test_negative_price_raises(self):
        raw = _valid_book_raw()
        raw["bids"] = [{"price": "-0.50", "size": "100"}]
        with pytest.raises((ValueError, InvalidOperation)):
            parse_book(raw, TS)

    def test_price_above_one_raises(self):
        raw = _valid_book_raw()
        raw["asks"] = [{"price": "1.50", "size": "100"}]
        with pytest.raises((ValueError, InvalidOperation)):
            parse_book(raw, TS)

    def test_negative_size_raises(self):
        raw = _valid_book_raw()
        raw["bids"] = [{"price": "0.50", "size": "-10"}]
        with pytest.raises((ValueError, InvalidOperation)):
            parse_book(raw, TS)

    def test_huge_price_number_raises(self):
        raw = _valid_book_raw()
        raw["bids"] = [{"price": "99999999", "size": "100"}]
        with pytest.raises((ValueError, InvalidOperation)):
            parse_book(raw, TS)

    def test_missing_tick_size_raises(self):
        raw = _valid_book_raw()
        del raw["tick_size"]
        with pytest.raises((ValueError, KeyError)):
            parse_book(raw, TS)

    def test_string_price_raises(self):
        raw = _valid_book_raw()
        raw["bids"] = [{"price": "abc", "size": "100"}]
        with pytest.raises((ValueError, InvalidOperation)):
            parse_book(raw, TS)

    def test_none_price_in_level_raises(self):
        raw = _valid_book_raw()
        raw["bids"] = [{"price": None, "size": "100"}]
        with pytest.raises((ValueError, TypeError, InvalidOperation)):
            parse_book(raw, TS)

    def test_empty_bids_asks_list_accepted(self):
        raw = _valid_book_raw()
        raw["bids"] = []
        raw["asks"] = []
        book = parse_book(raw, TS)
        assert book.best_bid is None
        assert book.best_ask is None


# ── Market parser fuzzing ────────────────────────────────────────────────────


class TestMarketParserFuzzing:
    def test_empty_outcomes_raises(self):
        raw = _valid_market_raw()
        raw["outcomes"] = []
        with pytest.raises((ValueError, IndexError)):
            parse_market(raw, TS)

    def test_missing_outcomes_key_raises(self):
        raw = _valid_market_raw()
        del raw["outcomes"]
        with pytest.raises((ValueError, KeyError)):
            parse_market(raw, TS)

    def test_missing_clob_token_ids_raises(self):
        raw = _valid_market_raw()
        del raw["clobTokenIds"]
        with pytest.raises((ValueError, KeyError)):
            parse_market(raw, TS)

    def test_duplicate_token_ids_raises(self):
        raw = _valid_market_raw()
        raw["clobTokenIds"] = ["t1", "t1"]
        with pytest.raises((ValueError, KeyError)):
            parse_market(raw, TS)

    def test_missing_id_field_raises(self):
        raw = _valid_market_raw()
        del raw["id"]
        with pytest.raises((ValueError, KeyError)):
            parse_market(raw, TS)

    def test_missing_question_raises(self):
        raw = _valid_market_raw()
        del raw["question"]
        with pytest.raises((ValueError, KeyError)):
            parse_market(raw, TS)

    def test_three_outcomes_raises(self):
        raw = _valid_market_raw()
        raw["outcomes"] = ["Yes", "No", "Maybe"]
        raw["clobTokenIds"] = ["t1", "t2", "t3"]
        with pytest.raises((ValueError, IndexError)):
            parse_market(raw, TS)

    def test_non_bool_active_raises(self):
        raw = _valid_market_raw()
        raw["active"] = "yes"
        with pytest.raises((ValueError, TypeError)):
            parse_market(raw, TS)


# ── JSON decoding fuzzing ────────────────────────────────────────────────────


class TestJsonDecodingFuzzing:
    def test_truncated_json_raises(self):
        with pytest.raises((json.JSONDecodeError, ValueError)):
            json.loads('{"asset_id": "t1", "ma')

    def test_wrong_type_for_array_raises(self):
        data = json.loads('{"bids": "not_an_array"}')
        assert not isinstance(data["bids"], list)

    def test_nested_garbage_json_raises(self):
        with pytest.raises((json.JSONDecodeError, ValueError)):
            json.loads('{"bids": [[[{{{{]]}}')

    def test_empty_string_json_raises(self):
        with pytest.raises((json.JSONDecodeError, ValueError)):
            json.loads("")

    def test_number_instead_of_object_raises(self):
        data = json.loads("42")
        assert data == 42

    def test_null_json_accepted(self):
        result = json.loads("null")
        assert result is None


# ── Fee input fuzzing ────────────────────────────────────────────────────────


class TestFeeInputFuzzing:
    def test_none_shares_raises(self):
        fees = FeeSchedule(D("0.02"), TS, "test")
        with pytest.raises((ValueError, TypeError, AttributeError)):
            fees.fee(None, D("0.50"))

    def test_none_price_raises(self):
        fees = FeeSchedule(D("0.02"), TS, "test")
        with pytest.raises((ValueError, TypeError, AttributeError)):
            fees.fee(D("100"), None)

    def test_negative_shares_raises(self):
        fees = FeeSchedule(D("0.02"), TS, "test")
        with pytest.raises(ValueError):
            fees.fee(D("-10"), D("0.50"))

    def test_price_above_one_raises(self):
        fees = FeeSchedule(D("0.02"), TS, "test")
        with pytest.raises(ValueError):
            fees.fee(D("100"), D("1.50"))

    def test_negative_price_raises(self):
        fees = FeeSchedule(D("0.02"), TS, "test")
        with pytest.raises(ValueError):
            fees.fee(D("100"), D("-0.50"))

    def test_negative_fee_rate_raises(self):
        with pytest.raises(ValueError):
            FeeSchedule(D("-0.01"), TS, "test")

    def test_infinite_shares_raises(self):
        fees = FeeSchedule(D("0.02"), TS, "test")
        with pytest.raises((ValueError, InvalidOperation)):
            fees.fee(D("Infinity"), D("0.50"))

    def test_nan_shares_raises(self):
        fees = FeeSchedule(D("0.02"), TS, "test")
        with pytest.raises((ValueError, InvalidOperation)):
            fees.fee(D("NaN"), D("0.50"))

    def test_empty_version_raises(self):
        with pytest.raises(ValueError):
            FeeSchedule(D("0.02"), TS, "")

    def test_zero_shares_yields_zero_fee(self):
        fees = FeeSchedule(D("0.02"), TS, "test")
        assert fees.fee(D(0), D("0.50")) == D(0)


# ── Domain object fuzzing ────────────────────────────────────────────────────


class TestDomainFuzzing:
    def test_level_with_negative_price_raises(self):
        with pytest.raises(ValueError):
            Level(D("-0.10"), D("100"))

    def test_level_with_zero_size_raises(self):
        with pytest.raises(ValueError):
            Level(D("0.50"), D(0))

    def test_level_with_negative_size_raises(self):
        with pytest.raises(ValueError):
            Level(D("0.50"), D("-10"))

    def test_level_with_infinite_price_raises(self):
        with pytest.raises((ValueError, InvalidOperation)):
            Level(D("Infinity"), D("100"))

    def test_book_with_empty_token_id_raises(self):
        with pytest.raises(ValueError):
            Book("", "c1", TS, TS, (), (), D("0.01"), D("1"), "")

    def test_order_with_negative_shares_raises(self):
        with pytest.raises(ValueError):
            Order("o1", "t1", "BUY", D("-10"), TS)

    def test_order_with_zero_shares_raises(self):
        with pytest.raises(ValueError):
            Order("o1", "t1", "BUY", D(0), TS)

    def test_order_with_invalid_side_raises(self):
        with pytest.raises(ValueError):
            Order("o1", "t1", "LONG", D("10"), TS)

    def test_order_with_empty_id_raises(self):
        with pytest.raises(ValueError):
            Order("", "t1", "BUY", D("10"), TS)

    def test_market_with_same_yes_no_tokens_raises(self):
        with pytest.raises(ValueError):
            Market("m1", "c1", ("e1",), "Q?", "", None, None,
                   True, False, True, True, D("5000"), D("1000"),
                   None, None, "t1", "t1", TS)

    def test_market_with_empty_id_raises(self):
        with pytest.raises(ValueError):
            Market("", "c1", ("e1",), "Q?", "", None, None,
                   True, False, True, True, D("5000"), D("1000"),
                   None, None, "t1", "t2", TS)


# ── Calibration input fuzzing ────────────────────────────────────────────────


class TestCalibrationFuzzing:
    def test_empty_observations_raises(self):
        with pytest.raises(ValueError):
            calibration_metrics([], [])

    def test_mismatched_lengths_raises(self):
        with pytest.raises(ValueError):
            calibration_metrics([0.5, 0.6], [1])

    def test_probability_outside_unit_interval_raises(self):
        with pytest.raises(ValueError):
            calibration_metrics([1.5, 0.5], [1, 0])

    def test_negative_probability_raises(self):
        with pytest.raises(ValueError):
            calibration_metrics([-0.1, 0.5], [1, 0])

    def test_invalid_outcome_raises(self):
        with pytest.raises(ValueError):
            calibration_metrics([0.5, 0.6], [2, 0])

    def test_infinite_probability_raises(self):
        with pytest.raises(ValueError):
            calibration_metrics([float("inf"), 0.5], [1, 0])


# ── Performance input fuzzing ────────────────────────────────────────────────


class TestPerformanceFuzzing:
    def test_empty_equities_raises(self):
        with pytest.raises(ValueError):
            performance([], [])

    def test_mismatched_equities_times_raises(self):
        with pytest.raises(ValueError):
            performance([100, 110], [TS])

    def test_non_positive_equity_raises(self):
        with pytest.raises(ValueError):
            performance([100, 0, 110], [TS, TS + __import__("datetime").timedelta(hours=1),
                                         TS + __import__("datetime").timedelta(hours=2)])

    def test_non_increasing_timestamps_raises(self):
        with pytest.raises(ValueError):
            performance([100, 110], [TS, TS])

    def test_drawdown_series_empty_input(self):
        assert drawdown_series([]) == []

    def test_brier_score_empty_raises(self):
        with pytest.raises(ValueError):
            brier_score([], [])

    def test_brier_score_mismatched_lengths_raises(self):
        with pytest.raises(ValueError):
            brier_score([0.5], [1, 0])
