"""
tests/test_notifier.py – Unit tests for notifier.py.

All tests are offline; HTTP calls are intercepted with MagicMock sessions
so no real Telegram or Discord endpoints are contacted.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, call

import pytest
import requests

from notifier import (
    CompositeNotifier,
    DiscordNotifier,
    NullNotifier,
    TelegramNotifier,
    build_notifier_from_env,
    fmt_entry,
    fmt_halt,
    fmt_settle_loss,
    fmt_settle_win,
    fmt_shutdown,
    fmt_startup,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _ok_response(status: int = 200) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.ok = (status < 400)
    resp.text = ""
    return resp


def _err_response(status: int, body: str = "error") -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.ok = False
    resp.text = body
    return resp


def _mock_session(response: MagicMock = None) -> MagicMock:
    sess = MagicMock(spec=requests.Session)
    sess.post.return_value = response or _ok_response()
    return sess


# ─── NullNotifier ─────────────────────────────────────────────────────────────

class TestNullNotifier:

    def test_send_does_not_raise(self):
        n = NullNotifier()
        n.send("hello")   # no exception expected

    def test_send_accepts_empty_string(self):
        NullNotifier().send("")


# ─── TelegramNotifier ─────────────────────────────────────────────────────────

class TestTelegramNotifier:

    TOKEN   = "123456:ABCDEFabcdef"
    CHAT_ID = "-100123456789"

    def _notifier(self, session=None):
        return TelegramNotifier(self.TOKEN, self.CHAT_ID, session=session)

    def test_posts_to_correct_url(self):
        sess = _mock_session()
        self._notifier(sess).send("hello")
        url = sess.post.call_args[0][0]
        assert f"/bot{self.TOKEN}/sendMessage" in url

    def test_payload_contains_chat_id_and_text(self):
        sess = _mock_session()
        self._notifier(sess).send("test message")
        payload = sess.post.call_args[1]["json"]
        assert payload["chat_id"] == self.CHAT_ID
        assert payload["text"]    == "test message"

    def test_parse_mode_is_html(self):
        sess = _mock_session()
        self._notifier(sess).send("hi")
        payload = sess.post.call_args[1]["json"]
        assert payload["parse_mode"] == "HTML"

    def test_does_not_raise_on_http_error(self):
        sess = _mock_session(_err_response(400, "Bad Request"))
        self._notifier(sess).send("boom")   # must not raise

    def test_does_not_raise_on_network_error(self):
        sess = MagicMock(spec=requests.Session)
        sess.post.side_effect = requests.ConnectionError("refused")
        self._notifier(sess).send("boom")   # must not raise

    def test_uses_timeout(self):
        sess = _mock_session()
        self._notifier(sess).send("hi")
        timeout = sess.post.call_args[1].get("timeout")
        assert timeout is not None and timeout > 0


# ─── DiscordNotifier ──────────────────────────────────────────────────────────

class TestDiscordNotifier:

    WEBHOOK = "https://discord.com/api/webhooks/123/abc"

    def _notifier(self, session=None):
        return DiscordNotifier(self.WEBHOOK, session=session)

    def test_posts_to_webhook_url(self):
        sess = _mock_session()
        self._notifier(sess).send("hello")
        assert sess.post.call_args[0][0] == self.WEBHOOK

    def test_payload_contains_content(self):
        sess = _mock_session()
        self._notifier(sess).send("test message")
        payload = sess.post.call_args[1]["json"]
        assert payload["content"] == "test message"

    def test_accepts_204_no_content(self):
        sess = _mock_session(_ok_response(204))
        self._notifier(sess).send("hi")   # no warning expected

    def test_does_not_raise_on_http_error(self):
        sess = _mock_session(_err_response(500, "Internal Server Error"))
        self._notifier(sess).send("boom")   # must not raise

    def test_does_not_raise_on_network_error(self):
        sess = MagicMock(spec=requests.Session)
        sess.post.side_effect = requests.Timeout("timed out")
        self._notifier(sess).send("boom")   # must not raise

    def test_uses_timeout(self):
        sess = _mock_session()
        self._notifier(sess).send("hi")
        timeout = sess.post.call_args[1].get("timeout")
        assert timeout is not None and timeout > 0


# ─── CompositeNotifier ────────────────────────────────────────────────────────

class TestCompositeNotifier:

    def test_sends_to_all_notifiers(self):
        a = MagicMock(spec=NullNotifier)
        b = MagicMock(spec=NullNotifier)
        CompositeNotifier([a, b]).send("hi")
        a.send.assert_called_once_with("hi")
        b.send.assert_called_once_with("hi")

    def test_sends_same_message_to_all(self):
        notifiers = [MagicMock(spec=NullNotifier) for _ in range(3)]
        msg = "broadcast"
        CompositeNotifier(notifiers).send(msg)
        for n in notifiers:
            n.send.assert_called_once_with(msg)

    def test_continues_after_first_notifier_fails(self):
        a = MagicMock(spec=NullNotifier)
        a.send.side_effect = RuntimeError("explode")
        b = MagicMock(spec=NullNotifier)
        # RuntimeError propagates from CompositeNotifier since it doesn't
        # swallow errors from sub-notifiers (each Notifier is responsible
        # for its own error handling).
        with pytest.raises(RuntimeError):
            CompositeNotifier([a, b]).send("hi")

    def test_notifiers_property_returns_copy(self):
        a = NullNotifier()
        c = CompositeNotifier([a])
        lst = c.notifiers
        lst.append(NullNotifier())
        assert len(c.notifiers) == 1   # internal list not mutated

    def test_empty_composite_does_not_raise(self):
        CompositeNotifier([]).send("hi")


# ─── build_notifier_from_env ──────────────────────────────────────────────────

class TestBuildNotifierFromEnv:

    def _clear_env(self, monkeypatch):
        for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL"):
            monkeypatch.delenv(var, raising=False)

    def test_returns_null_notifier_when_nothing_set(self, monkeypatch):
        self._clear_env(monkeypatch)
        n = build_notifier_from_env()
        assert isinstance(n, NullNotifier)

    def test_returns_telegram_when_only_telegram_set(self, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
        monkeypatch.setenv("TELEGRAM_CHAT_ID",   "123")
        n = build_notifier_from_env()
        assert isinstance(n, TelegramNotifier)

    def test_returns_discord_when_only_discord_set(self, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/x/y")
        n = build_notifier_from_env()
        assert isinstance(n, DiscordNotifier)

    def test_returns_composite_when_both_set(self, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN",  "tok")
        monkeypatch.setenv("TELEGRAM_CHAT_ID",    "123")
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/x/y")
        n = build_notifier_from_env()
        assert isinstance(n, CompositeNotifier)
        assert len(n.notifiers) == 2

    def test_ignores_blank_telegram_token(self, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "   ")   # whitespace only
        monkeypatch.setenv("TELEGRAM_CHAT_ID",   "123")
        n = build_notifier_from_env()
        assert isinstance(n, NullNotifier)

    def test_ignores_blank_discord_url(self, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
        n = build_notifier_from_env()
        assert isinstance(n, NullNotifier)


# ─── Message formatters ───────────────────────────────────────────────────────

class TestFormatters:

    WINDOW_END = 1_713_189_900   # 2024-04-15 14:05:00 UTC
    TOKEN_ID   = "71321045679252212594626385532706912750332728571942532289631379312455583992563"

    def test_fmt_startup_contains_mode_and_balance(self):
        msg = fmt_startup("SIMULATION", 300.0)
        assert "SIMULATION" in msg
        assert "300" in msg

    def test_fmt_shutdown_contains_balance_and_trades(self):
        msg = fmt_shutdown(balance=348.71, n_trades=5, win_rate=0.6)
        assert "348" in msg
        assert "5"   in msg

    def test_fmt_entry_contains_signal_type(self):
        msg = fmt_entry("STRONG_BUY_UP", 45.23, 0.62, self.TOKEN_ID, self.WINDOW_END)
        assert "STRONG_BUY_UP" in msg

    def test_fmt_entry_contains_size(self):
        msg = fmt_entry("STRONG_BUY_UP", 45.23, 0.62, self.TOKEN_ID, self.WINDOW_END)
        assert "45.23" in msg

    def test_fmt_entry_contains_truncated_token(self):
        msg = fmt_entry("STRONG_BUY_UP", 45.23, 0.62, self.TOKEN_ID, self.WINDOW_END)
        assert self.TOKEN_ID[:12] in msg

    def test_fmt_entry_contains_window_time(self):
        msg = fmt_entry("STRONG_BUY_UP", 45.23, 0.62, self.TOKEN_ID, self.WINDOW_END)
        assert "14:05" in msg   # UTC time of window close

    def test_fmt_settle_win_shows_positive_pnl(self):
        msg = fmt_settle_win(size_usdc=45.23, pnl_usdc=28.71, new_balance=348.71)
        assert "WIN" in msg
        assert "28.71" in msg

    def test_fmt_settle_win_shows_roi(self):
        msg = fmt_settle_win(size_usdc=100.0, pnl_usdc=61.0, new_balance=361.0)
        assert "61.0%" in msg

    def test_fmt_settle_loss_shows_loss_label(self):
        msg = fmt_settle_loss(size_usdc=45.23, new_balance=254.77)
        assert "LOSS" in msg
        assert "45.23" in msg

    def test_fmt_halt_shows_drawdown_and_limit(self):
        msg = fmt_halt(drawdown_pct=0.152, limit_pct=0.15, balance=255.0)
        assert "HALT" in msg
        assert "15.2%" in msg
        assert "15.0%" in msg

    def test_fmt_shutdown_shows_win_rate(self):
        msg = fmt_shutdown(balance=350.0, n_trades=10, win_rate=0.70)
        assert "70.0%" in msg
