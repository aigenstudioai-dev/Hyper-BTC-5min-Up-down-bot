"""
tests/test_price_feed.py – Unit tests for PythWebSocketFeed.

All tests are fully offline:
  • _parse_message() is a pure function – tested directly.
  • get_latest() / is_stale() operate on in-memory state.
  • Async methods (start, stop, _run_forever) are exercised via
    asyncio.run() so no third-party async plugin is required.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api_client import BTC_USD_FEED_ID, PriceData, PythWebSocketFeed


# ─── Shared test data ─────────────────────────────────────────────────────────

# BTC at $65,000:  6500000000000 × 10⁻⁸ = 65_000.0
RAW_PRICE    = "6500000000000"
RAW_CONF     = "500000000"       # ±$5.00
EXPO         = -8
PUBLISH_TS   = 1_713_189_900     # 2024-04-15 14:05:00 UTC
EXPECTED_USD  = 65_000.0
EXPECTED_CONF =      5.0

FEED_ID = BTC_USD_FEED_ID.lstrip("0x")


def _price_update_msg(
    *,
    price: str = RAW_PRICE,
    conf: str = RAW_CONF,
    expo: int = EXPO,
    publish_time: int = PUBLISH_TS,
    feed_id: str = FEED_ID,
) -> dict:
    """Minimal valid Hermes price_update message."""
    return {
        "type": "price_update",
        "price_feed": {
            "id": feed_id,
            "price": {
                "price":        price,
                "conf":         conf,
                "expo":         expo,
                "publish_time": publish_time,
            },
        },
    }


def _fresh_price_data(age_seconds: float = 0.0) -> PriceData:
    """Return a PriceData with a timestamp offset by age_seconds in the past."""
    return PriceData(
        price=EXPECTED_USD,
        confidence=EXPECTED_CONF,
        timestamp=time.time() - age_seconds,
        feed_id=BTC_USD_FEED_ID,
    )


# ─── _parse_message ───────────────────────────────────────────────────────────

class TestParseMessage:

    @pytest.fixture
    def feed(self) -> PythWebSocketFeed:
        return PythWebSocketFeed()

    # ── Happy path ────────────────────────────────────────────────────────────

    def test_returns_price_data_for_valid_message(self, feed):
        result = feed._parse_message(_price_update_msg())
        assert isinstance(result, PriceData)

    def test_correct_usd_price(self, feed):
        result = feed._parse_message(_price_update_msg())
        assert result.price == pytest.approx(EXPECTED_USD)

    def test_correct_confidence(self, feed):
        result = feed._parse_message(_price_update_msg())
        assert result.confidence == pytest.approx(EXPECTED_CONF)

    def test_correct_publish_time(self, feed):
        result = feed._parse_message(_price_update_msg())
        assert result.timestamp == PUBLISH_TS

    def test_feed_id_stored(self, feed):
        result = feed._parse_message(_price_update_msg())
        assert result.feed_id == BTC_USD_FEED_ID

    # ── Expo edge cases ───────────────────────────────────────────────────────

    def test_positive_expo(self, feed):
        result = feed._parse_message(_price_update_msg(price="650", expo=2))
        assert result.price == pytest.approx(65_000.0)

    def test_zero_expo(self, feed):
        result = feed._parse_message(_price_update_msg(price="65000", expo=0))
        assert result.price == pytest.approx(65_000.0)

    def test_large_negative_expo(self, feed):
        result = feed._parse_message(
            _price_update_msg(price="65000000000000000", expo=-12)
        )
        assert result.price == pytest.approx(65_000.0)

    # ── Non-price messages → None ─────────────────────────────────────────────

    def test_wrong_type_returns_none(self, feed):
        assert feed._parse_message({"type": "subscribed"}) is None

    def test_missing_type_returns_none(self, feed):
        assert feed._parse_message({}) is None

    def test_heartbeat_message_returns_none(self, feed):
        assert feed._parse_message({"type": "heartbeat"}) is None

    # ── Malformed payloads → None ─────────────────────────────────────────────

    def test_missing_price_feed_returns_none(self, feed):
        assert feed._parse_message({"type": "price_update"}) is None

    def test_empty_price_feed_returns_none(self, feed):
        assert feed._parse_message({"type": "price_update", "price_feed": {}}) is None

    def test_missing_price_key_returns_none(self, feed):
        msg = {"type": "price_update", "price_feed": {"id": FEED_ID}}
        assert feed._parse_message(msg) is None

    def test_non_numeric_expo_returns_none(self, feed):
        msg = _price_update_msg()
        msg["price_feed"]["price"]["expo"] = "bad"
        assert feed._parse_message(msg) is None

    # ── Price guard: zero / negative / NaN ───────────────────────────────────

    def test_zero_raw_price_returns_none(self, feed):
        assert feed._parse_message(_price_update_msg(price="0")) is None

    def test_negative_raw_price_returns_none(self, feed):
        assert feed._parse_message(_price_update_msg(price="-6500000000000")) is None

    def test_nan_price_returns_none(self, feed):
        """float('NaN') must be rejected — not (NaN > 0) is True."""
        assert feed._parse_message(_price_update_msg(price="NaN")) is None

    # ── Optional fields ───────────────────────────────────────────────────────

    def test_missing_conf_defaults_to_zero(self, feed):
        msg = _price_update_msg()
        del msg["price_feed"]["price"]["conf"]
        result = feed._parse_message(msg)
        assert result is not None
        assert result.confidence == pytest.approx(0.0)

    def test_missing_publish_time_defaults_to_current(self, feed):
        msg = _price_update_msg()
        del msg["price_feed"]["price"]["publish_time"]
        before = int(time.time())
        result = feed._parse_message(msg)
        after = int(time.time()) + 1
        assert result is not None
        assert before <= result.timestamp <= after


# ─── get_latest / is_stale ────────────────────────────────────────────────────

class TestGetLatestAndIsStale:

    @pytest.fixture
    def feed(self) -> PythWebSocketFeed:
        return PythWebSocketFeed()

    def test_get_latest_is_none_before_first_message(self, feed):
        assert feed.get_latest() is None

    def test_is_stale_when_no_price_cached(self, feed):
        assert feed.is_stale() is True

    def test_get_latest_returns_cached_price(self, feed):
        pd = _fresh_price_data()
        feed._last = pd
        assert feed.get_latest() is pd

    def test_is_stale_false_for_fresh_price(self, feed):
        feed._last = _fresh_price_data(age_seconds=1.0)
        assert feed.is_stale(max_age_seconds=10.0) is False

    def test_is_stale_true_for_old_price(self, feed):
        feed._last = _fresh_price_data(age_seconds=30.0)
        assert feed.is_stale(max_age_seconds=10.0) is True

    def test_is_stale_boundary_uses_patched_clock(self, feed):
        """Exactly at the age boundary: (now - ts) > max → boundary is not stale."""
        fixed_now = 1_000_000.0
        feed._last = PriceData(
            price=EXPECTED_USD, confidence=EXPECTED_CONF,
            timestamp=fixed_now - 10.0,   # exactly 10 s old
            feed_id=BTC_USD_FEED_ID,
        )
        with patch("api_client.time.time", return_value=fixed_now):
            # 10.0 > 10.0 is False → not stale
            assert feed.is_stale(max_age_seconds=10.0) is False

    def test_is_stale_just_past_boundary(self, feed):
        fixed_now = 1_000_000.0
        feed._last = PriceData(
            price=EXPECTED_USD, confidence=EXPECTED_CONF,
            timestamp=fixed_now - 10.001,
            feed_id=BTC_USD_FEED_ID,
        )
        with patch("api_client.time.time", return_value=fixed_now):
            assert feed.is_stale(max_age_seconds=10.0) is True

    def test_is_stale_default_max_age_is_10s(self, feed):
        feed._last = _fresh_price_data(age_seconds=5.0)
        assert feed.is_stale() is False   # default is 10 s


# ─── last_price property & is_connected ──────────────────────────────────────

class TestProperties:

    @pytest.fixture
    def feed(self) -> PythWebSocketFeed:
        return PythWebSocketFeed()

    def test_last_price_none_initially(self, feed):
        assert feed.last_price is None

    def test_last_price_returns_float_when_cached(self, feed):
        feed._last = _fresh_price_data()
        assert feed.last_price == pytest.approx(EXPECTED_USD)

    def test_is_connected_false_initially(self, feed):
        assert feed.is_connected is False


# ─── start / stop lifecycle ───────────────────────────────────────────────────

class TestStartStop:
    """
    Async lifecycle methods tested via asyncio.run() — no pytest-asyncio needed.
    asyncio.create_task is patched so no real coroutine is scheduled.
    """

    def test_start_creates_task(self):
        feed = PythWebSocketFeed()
        mock_task = MagicMock(spec=asyncio.Task)
        mock_task.done.return_value = False

        async def _test():
            with patch("asyncio.create_task", return_value=mock_task) as mock_create:
                await feed.start()
                mock_create.assert_called_once()
            assert feed._task is mock_task

        asyncio.run(_test())

    def test_start_is_idempotent(self):
        """Calling start() twice must not create a second task."""
        feed = PythWebSocketFeed()
        mock_task = MagicMock(spec=asyncio.Task)
        mock_task.done.return_value = False

        async def _test():
            with patch("asyncio.create_task", return_value=mock_task) as mock_create:
                await feed.start()
                await feed.start()
                mock_create.assert_called_once()

        asyncio.run(_test())

    def test_stop_when_not_started_does_not_raise(self):
        feed = PythWebSocketFeed()
        asyncio.run(feed.stop())   # must not raise

    def test_stop_sets_connected_false(self):
        feed = PythWebSocketFeed()
        feed._connected = True

        async def _test():
            await feed.stop()
            assert feed._connected is False

        asyncio.run(_test())

    def test_stop_cancels_running_task(self):
        """stop() cancels the task and clears _connected."""
        feed = PythWebSocketFeed()

        async def _long_running():
            await asyncio.sleep(9999)

        async def _test():
            await feed.start()
            feed._connected = True
            assert feed._task is not None
            await feed.stop()
            assert feed._connected is False
            assert feed._task.done()

        asyncio.run(_test())


# ─── _run_forever reconnect backoff ──────────────────────────────────────────

class TestReconnectBackoff:

    def test_reconnects_after_error(self):
        """_run_forever retries after OSError failures."""
        feed = PythWebSocketFeed()
        call_count = 0

        async def flaky_connect():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise OSError("connection refused")
            raise asyncio.CancelledError

        async def _test():
            with patch.object(feed, "_connect_and_stream", side_effect=flaky_connect):
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    with pytest.raises(asyncio.CancelledError):
                        await feed._run_forever()

        asyncio.run(_test())
        assert call_count == 3   # two failures + one CancelledError

    def test_sleep_called_between_retries(self):
        """asyncio.sleep must be awaited once per error before reconnect."""
        feed = PythWebSocketFeed()
        call_count = 0
        sleep_calls: list[float] = []

        async def flaky_connect():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise OSError("boom")
            raise asyncio.CancelledError

        async def record_sleep(delay: float):
            sleep_calls.append(delay)

        async def _test():
            with patch.object(feed, "_connect_and_stream", side_effect=flaky_connect):
                with patch("asyncio.sleep", side_effect=record_sleep):
                    with pytest.raises(asyncio.CancelledError):
                        await feed._run_forever()

        asyncio.run(_test())
        assert len(sleep_calls) == 2   # one sleep per failure

    def test_backoff_delay_bounded_by_max(self):
        """Every sleep value must be ≤ MAX_RECONNECT_DELAY."""
        feed = PythWebSocketFeed()
        sleep_calls: list[float] = []
        call_count = 0

        async def always_fails():
            nonlocal call_count
            call_count += 1
            if call_count >= 6:
                raise asyncio.CancelledError
            raise OSError("boom")

        async def record_sleep(delay: float):
            sleep_calls.append(delay)

        async def _test():
            with patch.object(feed, "_connect_and_stream", side_effect=always_fails):
                with patch("asyncio.sleep", side_effect=record_sleep):
                    with pytest.raises(asyncio.CancelledError):
                        await feed._run_forever()

        asyncio.run(_test())
        for s in sleep_calls:
            assert 0.0 <= s <= feed.MAX_RECONNECT_DELAY

    def test_delay_resets_after_clean_disconnect(self):
        """Clean return from _connect_and_stream resets the backoff to base."""
        feed = PythWebSocketFeed()
        sleep_calls: list[float] = []
        call_count = 0

        async def alternating():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return                   # clean disconnect → delay resets to 1.0
            if call_count == 2:
                raise OSError("error after clean reset")
            raise asyncio.CancelledError

        async def record_sleep(delay: float):
            sleep_calls.append(delay)

        async def _test():
            with patch.object(feed, "_connect_and_stream", side_effect=alternating):
                with patch("asyncio.sleep", side_effect=record_sleep):
                    with pytest.raises(asyncio.CancelledError):
                        await feed._run_forever()

        asyncio.run(_test())
        # After a clean disconnect delay resets to 1.0, so the one sleep
        # recorded (for the error on attempt 2) must be ≤ 1.0
        assert len(sleep_calls) == 1
        assert sleep_calls[0] <= 1.0
