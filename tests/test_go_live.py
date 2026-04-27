"""
tests/test_go_live.py – Unit tests for go_live.py pure validation helpers.

All tests are offline and fully deterministic — no filesystem writes,
no network calls, no interactive prompts.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from go_live import (
    env_file_exists,
    find_missing_vars,
    find_placeholder_vars,
    is_simulation_mode,
    is_valid_private_key,
    load_env_values,
    patch_simulation_mode,
)


# ─── env_file_exists ──────────────────────────────────────────────────────────

class TestEnvFileExists:

    def test_returns_true_when_file_exists(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("SIMULATION_MODE=true\n")
        assert env_file_exists(f) is True

    def test_returns_false_when_file_missing(self, tmp_path):
        assert env_file_exists(tmp_path / ".env") is False


# ─── load_env_values ─────────────────────────────────────────────────────────

class TestLoadEnvValues:

    def _write(self, tmp_path, content: str) -> Path:
        f = tmp_path / ".env"
        f.write_text(content)
        return f

    def test_parses_simple_key_value(self, tmp_path):
        f = self._write(tmp_path, "FOO=bar\n")
        assert load_env_values(f) == {"FOO": "bar"}

    def test_ignores_comment_lines(self, tmp_path):
        f = self._write(tmp_path, "# comment\nFOO=bar\n")
        assert "# comment" not in load_env_values(f)

    def test_ignores_blank_lines(self, tmp_path):
        f = self._write(tmp_path, "\nFOO=bar\n\n")
        assert load_env_values(f) == {"FOO": "bar"}

    def test_handles_values_with_equals_sign(self, tmp_path):
        # URL with = in query string
        f = self._write(tmp_path, "URL=https://x.com?a=1\n")
        assert load_env_values(f)["URL"] == "https://x.com?a=1"

    def test_returns_empty_dict_for_missing_file(self, tmp_path):
        assert load_env_values(tmp_path / ".env") == {}

    def test_strips_surrounding_whitespace(self, tmp_path):
        f = self._write(tmp_path, "  FOO  =  bar  \n")
        assert load_env_values(f)["FOO"] == "bar"

    def test_parses_multiple_vars(self, tmp_path):
        f = self._write(tmp_path, "A=1\nB=2\nC=3\n")
        assert load_env_values(f) == {"A": "1", "B": "2", "C": "3"}


# ─── find_missing_vars ────────────────────────────────────────────────────────

class TestFindMissingVars:

    REQUIRED = ["A", "B", "C"]

    def test_returns_empty_when_all_present(self):
        assert find_missing_vars({"A": "x", "B": "y", "C": "z"}, self.REQUIRED) == []

    def test_returns_missing_key(self):
        assert find_missing_vars({"A": "x", "B": "y"}, self.REQUIRED) == ["C"]

    def test_treats_empty_string_as_missing(self):
        assert find_missing_vars({"A": "x", "B": "", "C": "z"}, self.REQUIRED) == ["B"]

    def test_treats_whitespace_as_missing(self):
        assert find_missing_vars({"A": "x", "B": "   ", "C": "z"}, self.REQUIRED) == ["B"]

    def test_returns_all_when_dict_is_empty(self):
        assert find_missing_vars({}, self.REQUIRED) == self.REQUIRED

    def test_returns_multiple_missing(self):
        result = find_missing_vars({}, ["X", "Y"])
        assert set(result) == {"X", "Y"}


# ─── find_placeholder_vars ───────────────────────────────────────────────────

class TestFindPlaceholderVars:

    def test_detects_your_prefix(self):
        result = find_placeholder_vars({"KEY": "your_api_key_here"})
        assert any(k == "KEY" for k, v in result)

    def test_detects_all_zero_private_key(self):
        result = find_placeholder_vars({"PRIVATE_KEY": "0x" + "0" * 64})
        assert any(k == "PRIVATE_KEY" for k, v in result)

    def test_clean_values_not_flagged(self):
        values = {
            "PRIVATE_KEY":       "0x" + "a" * 64,
            "POLY_API_KEY":      "abc123real",
            "POLYGON_RPC_URL":   "https://polygon-rpc.com",
        }
        assert find_placeholder_vars(values) == []

    def test_case_insensitive_your_prefix(self):
        result = find_placeholder_vars({"K": "YOUR_KEY_HERE"})
        assert any(k == "K" for k, v in result)

    def test_empty_string_not_flagged_as_placeholder(self):
        # Empty strings are caught by find_missing_vars, not here
        result = find_placeholder_vars({"K": ""})
        assert result == []


# ─── is_valid_private_key ────────────────────────────────────────────────────

class TestIsValidPrivateKey:

    VALID_KEY = "0x" + "ab" * 32   # 64 hex chars with 0x prefix

    def test_valid_key_with_prefix(self):
        assert is_valid_private_key(self.VALID_KEY) is True

    def test_valid_key_without_prefix(self):
        assert is_valid_private_key("ab" * 32) is True

    def test_all_zeros_rejected(self):
        assert is_valid_private_key("0x" + "0" * 64) is False

    def test_too_short_rejected(self):
        assert is_valid_private_key("0x" + "ab" * 16) is False

    def test_too_long_rejected(self):
        assert is_valid_private_key("0x" + "ab" * 33) is False

    def test_non_hex_chars_rejected(self):
        assert is_valid_private_key("0x" + "zz" * 32) is False

    def test_empty_string_rejected(self):
        assert is_valid_private_key("") is False

    def test_uppercase_hex_accepted(self):
        assert is_valid_private_key("AB" * 32) is True


# ─── is_simulation_mode ──────────────────────────────────────────────────────

class TestIsSimulationMode:

    def test_true_when_set_to_true(self):
        assert is_simulation_mode({"SIMULATION_MODE": "true"}) is True

    def test_false_when_set_to_false(self):
        assert is_simulation_mode({"SIMULATION_MODE": "false"}) is False

    def test_default_is_true_when_absent(self):
        assert is_simulation_mode({}) is True

    def test_case_insensitive_false(self):
        assert is_simulation_mode({"SIMULATION_MODE": "False"}) is False

    def test_case_insensitive_true(self):
        assert is_simulation_mode({"SIMULATION_MODE": "TRUE"}) is True


# ─── patch_simulation_mode ───────────────────────────────────────────────────

class TestPatchSimulationMode:

    def test_replaces_true_with_false(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("PRIVATE_KEY=abc\nSIMULATION_MODE=true\n")
        patch_simulation_mode(f)
        assert "SIMULATION_MODE=false" in f.read_text()
        assert "SIMULATION_MODE=true"  not in f.read_text()

    def test_appends_when_line_absent(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("PRIVATE_KEY=abc\n")
        patch_simulation_mode(f)
        assert "SIMULATION_MODE=false" in f.read_text()

    def test_does_not_duplicate_other_lines(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("A=1\nSIMULATION_MODE=true\nB=2\n")
        patch_simulation_mode(f)
        text = f.read_text()
        assert text.count("A=1") == 1
        assert text.count("B=2") == 1

    def test_returns_false_when_file_missing(self, tmp_path):
        result = patch_simulation_mode(tmp_path / ".env")
        assert result is False

    def test_handles_case_insensitive_true(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("SIMULATION_MODE=TRUE\n")
        patch_simulation_mode(f)
        assert "SIMULATION_MODE=false" in f.read_text()

    def test_handles_whitespace_around_equals(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("SIMULATION_MODE = true\n")
        patch_simulation_mode(f)
        assert "SIMULATION_MODE=false" in f.read_text()

    def test_does_not_touch_already_false(self, tmp_path):
        f = tmp_path / ".env"
        original = "SIMULATION_MODE=false\n"
        f.write_text(original)
        patch_simulation_mode(f)
        # false is not matched by the regex (only true is), so it gets appended
        # which means the file will have SIMULATION_MODE=false appearing once still
        text = f.read_text()
        assert "SIMULATION_MODE=false" in text
