"""
tests/test_market_discovery.py – Unit tests for market-discovery helpers.

All tests are offline.  Fake market dicts mirror the real Gamma API
shape: camelCase fields, clobTokenIds as a JSON-encoded string.
"""

from __future__ import annotations

import json
import pytest

from api_client import (
    TokenPair,
    _extract_tokens,
    _is_btc_5min_market,
    _parse_end_time,
    _parse_json_list,
)

# ─── Realistic fake market fixtures ───────────────────────────────────────────

# Token IDs as they actually appear in the Gamma API (long uint256 strings)
YES_TOKEN = "71321045679252212594626385532706912750332728571942532289631379312455583992563"
NO_TOKEN  = "52114319501245915516055106046884209969926127482827954674443846427813813222426"

# clobTokenIds encoded as a JSON string (the real API format)
CLOB_IDS_STR  = json.dumps([YES_TOKEN, NO_TOKEN])
# clobTokenIds as a native Python list (some API versions)
CLOB_IDS_LIST = [YES_TOKEN, NO_TOKEN]


def btc_5min_market(**overrides) -> dict:
    """Return a minimal valid BTC 5-min market dict, with optional overrides."""
    base = {
        "conditionId": "0x" + "a" * 64,
        "question": "Will BTC be above $65,000 at 2:05 PM on April 15?",
        "slug": "btc-above-65000-2-05-pm-april-15",
        "endDateIso": "2024-04-15T14:05:00Z",   # 14:05 UTC → divisible by 300
        "active": True,
        "closed": False,
        "clobTokenIds": CLOB_IDS_STR,
        "tokens": [
            {"token_id": YES_TOKEN, "outcome": "Yes", "price": 0.60, "winner": False},
            {"token_id": NO_TOKEN,  "outcome": "No",  "price": 0.40, "winner": False},
        ],
    }
    base.update(overrides)
    return base


# ─── _parse_json_list ─────────────────────────────────────────────────────────

class TestParseJsonList:

    def test_native_list_returned_unchanged(self):
        lst = ["a", "b", "c"]
        assert _parse_json_list(lst) is lst

    def test_json_encoded_string_parsed(self):
        assert _parse_json_list('["x", "y"]') == ["x", "y"]

    def test_json_encoded_numbers(self):
        assert _parse_json_list("[1, 2, 3]") == [1, 2, 3]

    def test_invalid_json_returns_none(self):
        assert _parse_json_list("not-json") is None

    def test_json_object_not_a_list_returns_none(self):
        assert _parse_json_list('{"a": 1}') is None

    def test_none_returns_none(self):
        assert _parse_json_list(None) is None

    def test_integer_returns_none(self):
        assert _parse_json_list(42) is None

    def test_empty_list_string(self):
        assert _parse_json_list("[]") == []


# ─── _extract_tokens ──────────────────────────────────────────────────────────

class TestExtractTokens:

    # ── Primary: clobTokenIds as JSON string (the real API format) ────────────

    def test_clob_token_ids_json_string(self):
        m = btc_5min_market(clobTokenIds=CLOB_IDS_STR)
        result = _extract_tokens(m)
        assert result == TokenPair(yes_token_id=YES_TOKEN, no_token_id=NO_TOKEN)

    def test_clob_token_ids_native_list(self):
        m = btc_5min_market(clobTokenIds=CLOB_IDS_LIST)
        result = _extract_tokens(m)
        assert result == TokenPair(yes_token_id=YES_TOKEN, no_token_id=NO_TOKEN)

    # ── Snake-case alias ──────────────────────────────────────────────────────

    def test_clob_token_ids_snake_case_string(self):
        m = btc_5min_market()
        del m["clobTokenIds"]
        m["clob_token_ids"] = CLOB_IDS_STR
        result = _extract_tokens(m)
        assert result == TokenPair(yes_token_id=YES_TOKEN, no_token_id=NO_TOKEN)

    def test_clob_token_ids_snake_case_list(self):
        m = btc_5min_market()
        del m["clobTokenIds"]
        m["clob_token_ids"] = CLOB_IDS_LIST
        result = _extract_tokens(m)
        assert result == TokenPair(yes_token_id=YES_TOKEN, no_token_id=NO_TOKEN)

    # ── Silent bug guard: old code would read chars from the string ───────────

    def test_does_not_return_bracket_as_token_id(self):
        """If clobTokenIds is a JSON string, token IDs must NOT be '[' or '"'."""
        m = btc_5min_market(clobTokenIds=CLOB_IDS_STR)
        result = _extract_tokens(m)
        assert result is not None
        assert result.yes_token_id != "["
        assert result.no_token_id != '"'
        assert len(result.yes_token_id) > 10

    # ── Fallback: tokens array ────────────────────────────────────────────────

    def test_falls_back_to_tokens_array_when_clob_ids_absent(self):
        m = btc_5min_market()
        del m["clobTokenIds"]
        result = _extract_tokens(m)
        assert result == TokenPair(yes_token_id=YES_TOKEN, no_token_id=NO_TOKEN)

    def test_tokens_array_with_dict_elements(self):
        m = {"tokens": [
            {"token_id": YES_TOKEN, "outcome": "Yes"},
            {"token_id": NO_TOKEN,  "outcome": "No"},
        ]}
        result = _extract_tokens(m)
        assert result == TokenPair(yes_token_id=YES_TOKEN, no_token_id=NO_TOKEN)

    def test_tokens_array_as_json_string(self):
        tokens_str = json.dumps([
            {"token_id": YES_TOKEN, "outcome": "Yes"},
            {"token_id": NO_TOKEN,  "outcome": "No"},
        ])
        m = {"tokens": tokens_str}
        result = _extract_tokens(m)
        assert result == TokenPair(yes_token_id=YES_TOKEN, no_token_id=NO_TOKEN)

    # ── Returns None when nothing usable is present ───────────────────────────

    def test_returns_none_when_no_token_fields(self):
        assert _extract_tokens({"question": "Will BTC go up?"}) is None

    def test_returns_none_for_empty_clob_ids_list(self):
        assert _extract_tokens({"clobTokenIds": "[]"}) is None

    def test_returns_none_for_single_token(self):
        assert _extract_tokens({"clobTokenIds": json.dumps([YES_TOKEN])}) is None

    def test_returns_none_for_malformed_clob_ids_string(self):
        assert _extract_tokens({"clobTokenIds": "not-json"}) is None

    def test_returns_none_for_empty_token_ids(self):
        """Empty string token IDs should be treated as absent."""
        m = {"clobTokenIds": json.dumps(["", ""])}
        assert _extract_tokens(m) is None


# ─── _is_btc_5min_market ──────────────────────────────────────────────────────

class TestIsBtc5MinMarket:

    # ── Should match ──────────────────────────────────────────────────────────

    def test_explicit_5min_keyword_in_question(self):
        assert _is_btc_5min_market({"question": "BTC 5-min up or down?"})

    def test_explicit_5_minute_keyword(self):
        assert _is_btc_5min_market({"question": "Will Bitcoin move 0.1% in the next 5 minute window?"})

    def test_bitcoin_keyword_with_5min(self):
        assert _is_btc_5min_market({"question": "Bitcoin 5min price above 65k?"})

    def test_5min_in_slug(self):
        assert _is_btc_5min_market({"question": "BTC move", "slug": "btc-5min-apr-15-1405"})

    def test_hh_mm_pattern_above(self):
        assert _is_btc_5min_market({
            "question": "Will BTC be above $65,000 at 2:05 PM on April 15?"
        })

    def test_hh_mm_pattern_below(self):
        assert _is_btc_5min_market({
            "question": "Will BTC be below $65,000 at 2:10 PM?"
        })

    def test_hh_mm_higher(self):
        assert _is_btc_5min_market({
            "question": "Will BTC be higher than $65k at 3:15 PM?"
        })

    def test_hh_mm_lower(self):
        assert _is_btc_5min_market({
            "question": "Will BTC price be lower at 4:30 PM?"
        })

    # ── Should NOT match ──────────────────────────────────────────────────────

    def test_no_btc_keyword_rejected(self):
        assert not _is_btc_5min_market({"question": "Will ETH be above $3000 at 2:05 PM?"})

    def test_btc_hourly_market_no_5min_keyword(self):
        # No 5-min label, no HH:MM pattern → rejected
        assert not _is_btc_5min_market({
            "question": "Will BTC hit $100k by end of year?"
        })

    def test_btc_daily_market_rejected(self):
        assert not _is_btc_5min_market({
            "question": "Will BTC be above $70k by Friday?"
        })

    def test_empty_question_rejected(self):
        assert not _is_btc_5min_market({})

    def test_btc_at_round_hour_accepted_as_candidate(self):
        # ":00" IS a valid 5-min boundary (2:00 PM ends a 5-min window).
        # _is_btc_5min_market is a coarse pre-filter; hourly vs 5-min
        # discrimination happens downstream via end_time divisibility.
        assert _is_btc_5min_market({
            "question": "Will BTC be above $65,000 at 2:00 PM?"
        })


# ─── _parse_end_time ──────────────────────────────────────────────────────────

class TestParseEndTime:

    # Convenience: April 15 2024 14:05:00 UTC = 1713189900 (divisible by 300)
    ISO_14_05 = "2024-04-15T14:05:00Z"
    TS_14_05  = 1713189900

    def test_parses_endDateIso(self):
        result = _parse_end_time({"endDateIso": self.ISO_14_05})
        assert result == self.TS_14_05

    def test_parses_end_date_iso_snake(self):
        result = _parse_end_time({"end_date_iso": self.ISO_14_05})
        assert result == self.TS_14_05

    def test_parses_endDate(self):
        result = _parse_end_time({"endDate": self.ISO_14_05})
        assert result == self.TS_14_05

    def test_parses_end_date_snake(self):
        result = _parse_end_time({"end_date": self.ISO_14_05})
        assert result == self.TS_14_05

    def test_parses_unix_integer(self):
        result = _parse_end_time({"endDateIso": self.TS_14_05})
        assert result == self.TS_14_05

    def test_snaps_minor_drift(self):
        """A timestamp 1 second off a boundary should snap back to the boundary."""
        drifted = self.TS_14_05 + 1
        result = _parse_end_time({"endDateIso": drifted})
        assert result == self.TS_14_05

    def test_result_divisible_by_300(self):
        result = _parse_end_time({"endDateIso": self.ISO_14_05})
        assert result % 300 == 0

    def test_returns_none_for_missing_field(self):
        assert _parse_end_time({}) is None
        assert _parse_end_time({"question": "Will BTC go up?"}) is None

    def test_returns_none_for_unparseable_string(self):
        assert _parse_end_time({"endDateIso": "not-a-date"}) is None

    def test_iso_with_offset_timezone(self):
        # Same moment as 14:05 UTC expressed with +00:00
        result = _parse_end_time({"endDateIso": "2024-04-15T14:05:00+00:00"})
        assert result == self.TS_14_05

    def test_prioritises_endDateIso_over_later_fields(self):
        """endDateIso should win when multiple end-time fields coexist."""
        m = {
            "endDateIso": self.ISO_14_05,
            "endDate": "2024-04-15T15:00:00Z",  # different time
        }
        result = _parse_end_time(m)
        assert result == self.TS_14_05
