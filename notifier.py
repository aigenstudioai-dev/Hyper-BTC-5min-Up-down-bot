"""
notifier.py – Trade alert notifications via Telegram and/or Discord.

Notifications are best-effort: failures are logged but never raise to
the caller so a broken webhook never interrupts live trading.

Supported channels
------------------
Telegram   – set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env
Discord    – set DISCORD_WEBHOOK_URL in .env
Both       – set all three; CompositeNotifier fans out to each
Neither    – NullNotifier silently drops every message (default)

Usage
-----
    notifier = build_notifier_from_env()
    notifier.send(fmt_entry(signal, size_usdc, price, window_end))
    notifier.send(fmt_settle(record))
"""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

# ─── Base ─────────────────────────────────────────────────────────────────────


class Notifier(ABC):
    """Abstract base for all notification channels."""

    @abstractmethod
    def send(self, message: str) -> None:
        """Send *message*.  Must never raise."""


# ─── Null (no-op) ─────────────────────────────────────────────────────────────


class NullNotifier(Notifier):
    """Used when no notification channel is configured."""

    def send(self, message: str) -> None:
        pass


# ─── Telegram ────────────────────────────────────────────────────────────────


class TelegramNotifier(Notifier):
    """
    Send messages via the Telegram Bot API.

    Set up:
      1. Create a bot with @BotFather → copy the token.
      2. Add the bot to a group or start a private chat.
      3. Get your chat ID:  https://api.telegram.org/bot<TOKEN>/getUpdates
    """

    _API_BASE = "https://api.telegram.org"

    def __init__(
        self,
        token: str,
        chat_id: str,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._token   = token
        self._chat_id = chat_id
        self._session = session or requests.Session()

    def send(self, message: str) -> None:
        url = f"{self._API_BASE}/bot{self._token}/sendMessage"
        try:
            resp = self._session.post(
                url,
                json={
                    "chat_id":    self._chat_id,
                    "text":       message,
                    "parse_mode": "HTML",
                },
                timeout=8,
            )
            if not resp.ok:
                logger.warning(
                    "Telegram notification failed: HTTP %d – %s",
                    resp.status_code, resp.text[:120],
                )
        except Exception as exc:
            logger.warning("Telegram notification error: %s", exc)


# ─── Discord ──────────────────────────────────────────────────────────────────


class DiscordNotifier(Notifier):
    """
    Send messages to a Discord channel via an Incoming Webhook.

    Set up:
      Channel settings → Integrations → Webhooks → New Webhook → Copy URL
    """

    def __init__(
        self,
        webhook_url: str,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._url     = webhook_url
        self._session = session or requests.Session()

    def send(self, message: str) -> None:
        try:
            resp = self._session.post(
                self._url,
                json={"content": message},
                timeout=8,
            )
            # Discord returns 204 No Content on success
            if resp.status_code not in (200, 204):
                logger.warning(
                    "Discord notification failed: HTTP %d – %s",
                    resp.status_code, resp.text[:120],
                )
        except Exception as exc:
            logger.warning("Discord notification error: %s", exc)


# ─── Composite ────────────────────────────────────────────────────────────────


class CompositeNotifier(Notifier):
    """Fan-out notifier that delivers to every registered channel in order."""

    def __init__(self, notifiers: List[Notifier]) -> None:
        self._notifiers = notifiers

    def send(self, message: str) -> None:
        for n in self._notifiers:
            n.send(message)

    @property
    def notifiers(self) -> List[Notifier]:
        return list(self._notifiers)


# ─── Factory ──────────────────────────────────────────────────────────────────


def build_notifier_from_env() -> Notifier:
    """
    Read notification credentials from environment variables and return
    a ready-to-use Notifier.

    Returns NullNotifier when no credentials are configured.
    Returns TelegramNotifier or DiscordNotifier when one is configured.
    Returns CompositeNotifier when both are configured.
    """
    channels: List[Notifier] = []

    tg_token   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat_id = os.getenv("TELEGRAM_CHAT_ID",   "").strip()
    if tg_token and tg_chat_id:
        channels.append(TelegramNotifier(tg_token, tg_chat_id))
        logger.info("Telegram notifications enabled (chat_id=%s)", tg_chat_id)

    dc_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if dc_url:
        channels.append(DiscordNotifier(dc_url))
        logger.info("Discord notifications enabled")

    if not channels:
        logger.debug("No notification channels configured – using NullNotifier")
        return NullNotifier()

    if len(channels) == 1:
        return channels[0]

    return CompositeNotifier(channels)


# ─── Message formatters (pure functions) ─────────────────────────────────────


def _ts_to_utc(unix_ts: int) -> str:
    """Format a Unix timestamp as HH:MM UTC."""
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%H:%M UTC")


def fmt_startup(mode: str, balance: float) -> str:
    return (
        f"[START] Hyper-BTC Bot\n"
        f"  Mode:    {mode}\n"
        f"  Balance: ${balance:,.2f} USDC"
    )


def fmt_shutdown(balance: float, n_trades: int, win_rate: float) -> str:
    return (
        f"[STOP] Hyper-BTC Bot\n"
        f"  Final balance: ${balance:,.2f} USDC\n"
        f"  Trades:        {n_trades}\n"
        f"  Win rate:      {win_rate:.1%}"
    )


def fmt_entry(
    signal_type: str,
    size_usdc: float,
    price: float,
    token_id: str,
    window_end: int,
) -> str:
    return (
        f"[ENTRY] {signal_type}\n"
        f"  Size:   ${size_usdc:.2f} USDC\n"
        f"  Price:  {price:.4f}\n"
        f"  Token:  {token_id[:12]}...\n"
        f"  Window: {_ts_to_utc(window_end)}"
    )


def fmt_settle_win(
    size_usdc: float,
    pnl_usdc: float,
    new_balance: float,
) -> str:
    roi_pct = pnl_usdc / size_usdc * 100 if size_usdc else 0.0
    return (
        f"[WIN] +${pnl_usdc:.2f} USDC (+{roi_pct:.1f}%)\n"
        f"  Stake:   ${size_usdc:.2f}\n"
        f"  Balance: ${new_balance:,.2f}"
    )


def fmt_settle_loss(
    size_usdc: float,
    new_balance: float,
) -> str:
    return (
        f"[LOSS] -${size_usdc:.2f} USDC\n"
        f"  Balance: ${new_balance:,.2f}"
    )


def fmt_halt(drawdown_pct: float, limit_pct: float, balance: float) -> str:
    return (
        f"[HALT] Circuit breaker triggered\n"
        f"  Drawdown: {drawdown_pct:.1%}  (limit {limit_pct:.1%})\n"
        f"  Balance:  ${balance:,.2f} USDC"
    )
