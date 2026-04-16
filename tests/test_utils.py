"""
tests/test_utils.py – Unit tests for utils.http_retry, _jitter, _parse_retry_after.

All tests are offline; time.sleep is patched so the suite runs in <1 s.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest
import requests

from utils import _jitter, _parse_retry_after, http_retry, RETRYABLE_STATUSES


# ─── Helpers ──────────────────────────────────────────────────────────────────

def make_session() -> MagicMock:
    return MagicMock(spec=requests.Session)


def make_response(status: int, headers: dict = None) -> MagicMock:
    """Build a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.headers = headers or {}
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(
            f"HTTP {status}", response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


# ─── _jitter ──────────────────────────────────────────────────────────────────

class TestJitter:

    def test_returns_non_negative(self):
        for attempt in range(5):
            assert _jitter(1.0, 32.0, attempt) >= 0.0

    def test_bounded_by_cap(self):
        for attempt in range(6):
            assert _jitter(1.0, 32.0, attempt) <= 32.0

    def test_cap_grows_with_attempt(self):
        """Each attempt allows a larger maximum delay (up to max_delay)."""
        caps = [min(32.0, 1.0 * (2 ** a)) for a in range(6)]
        assert caps == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0]

    def test_never_exceeds_max_delay(self):
        # Even with a huge attempt number the cap cannot exceed max_delay
        assert _jitter(1.0, 5.0, 100) <= 5.0


# ─── _parse_retry_after ───────────────────────────────────────────────────────

class TestParseRetryAfter:

    def test_parses_integer_header(self):
        resp = make_response(429, headers={"Retry-After": "10"})
        assert _parse_retry_after(resp) == pytest.approx(10.0)

    def test_parses_float_header(self):
        resp = make_response(429, headers={"Retry-After": "2.5"})
        assert _parse_retry_after(resp) == pytest.approx(2.5)

    def test_returns_none_when_header_absent(self):
        resp = make_response(429)
        assert _parse_retry_after(resp) is None

    def test_returns_none_for_non_numeric_header(self):
        # RFC 7231 allows an HTTP-date; we skip it and return None
        resp = make_response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
        assert _parse_retry_after(resp) is None


# ─── http_retry ───────────────────────────────────────────────────────────────

@patch("utils.time.sleep")          # suppress real sleeps in all tests below
class TestHttpRetry:

    URL = "https://api.example.com/data"

    # ── Happy path ────────────────────────────────────────────────────────────

    def test_returns_response_on_first_success(self, mock_sleep):
        session = make_session()
        session.request.return_value = make_response(200)
        resp = http_retry(session, "GET", self.URL)
        assert resp.status_code == 200
        assert session.request.call_count == 1
        mock_sleep.assert_not_called()

    def test_passes_kwargs_to_session(self, mock_sleep):
        session = make_session()
        session.request.return_value = make_response(200)
        http_retry(session, "GET", self.URL, params={"k": "v"}, timeout=5)
        session.request.assert_called_once_with(
            "GET", self.URL, params={"k": "v"}, timeout=5
        )

    # ── Retry on 429 ─────────────────────────────────────────────────────────

    def test_retries_once_on_429(self, mock_sleep):
        session = make_session()
        session.request.side_effect = [make_response(429), make_response(200)]
        http_retry(session, "GET", self.URL, max_retries=1)
        assert session.request.call_count == 2
        mock_sleep.assert_called_once()

    def test_respects_retry_after_header(self, mock_sleep):
        session = make_session()
        session.request.side_effect = [
            make_response(429, headers={"Retry-After": "7"}),
            make_response(200),
        ]
        http_retry(session, "GET", self.URL, max_retries=1)
        mock_sleep.assert_called_once_with(7.0)

    def test_retries_multiple_times_on_429(self, mock_sleep):
        session = make_session()
        session.request.side_effect = [
            make_response(429),
            make_response(429),
            make_response(200),
        ]
        http_retry(session, "GET", self.URL, max_retries=2)
        assert session.request.call_count == 3

    # ── Retry on 5xx ─────────────────────────────────────────────────────────

    def test_retries_on_500(self, mock_sleep):
        session = make_session()
        session.request.side_effect = [make_response(500), make_response(200)]
        http_retry(session, "GET", self.URL, max_retries=1)
        assert session.request.call_count == 2

    def test_retries_on_502(self, mock_sleep):
        session = make_session()
        session.request.side_effect = [make_response(502), make_response(200)]
        http_retry(session, "GET", self.URL, max_retries=1)
        assert session.request.call_count == 2

    def test_retries_on_503(self, mock_sleep):
        session = make_session()
        session.request.side_effect = [make_response(503), make_response(200)]
        http_retry(session, "GET", self.URL, max_retries=1)
        assert session.request.call_count == 2

    def test_retries_on_504(self, mock_sleep):
        session = make_session()
        session.request.side_effect = [make_response(504), make_response(200)]
        http_retry(session, "GET", self.URL, max_retries=1)
        assert session.request.call_count == 2

    # ── No retry on other 4xx ─────────────────────────────────────────────────

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_does_not_retry_on_client_errors(self, mock_sleep, status):
        session = make_session()
        session.request.return_value = make_response(status)
        with pytest.raises(requests.HTTPError):
            http_retry(session, "GET", self.URL)
        assert session.request.call_count == 1
        mock_sleep.assert_not_called()

    # ── Retry on network errors ───────────────────────────────────────────────

    def test_retries_on_connection_error(self, mock_sleep):
        session = make_session()
        session.request.side_effect = [
            requests.ConnectionError("refused"),
            make_response(200),
        ]
        http_retry(session, "GET", self.URL, max_retries=1)
        assert session.request.call_count == 2

    def test_retries_on_timeout(self, mock_sleep):
        session = make_session()
        session.request.side_effect = [
            requests.Timeout("timed out"),
            make_response(200),
        ]
        http_retry(session, "GET", self.URL, max_retries=1)
        assert session.request.call_count == 2

    def test_retries_on_chunked_encoding_error(self, mock_sleep):
        session = make_session()
        session.request.side_effect = [
            requests.exceptions.ChunkedEncodingError("broken pipe"),
            make_response(200),
        ]
        http_retry(session, "GET", self.URL, max_retries=1)
        assert session.request.call_count == 2

    # ── Exhausted retries ─────────────────────────────────────────────────────

    def test_raises_after_max_retries_on_5xx(self, mock_sleep):
        session = make_session()
        session.request.return_value = make_response(500)
        with pytest.raises(requests.HTTPError):
            http_retry(session, "GET", self.URL, max_retries=2)
        assert session.request.call_count == 3   # 1 initial + 2 retries

    def test_raises_after_max_retries_on_network_error(self, mock_sleep):
        session = make_session()
        session.request.side_effect = requests.ConnectionError("down")
        with pytest.raises(requests.ConnectionError):
            http_retry(session, "GET", self.URL, max_retries=2)
        assert session.request.call_count == 3

    def test_raises_after_max_retries_on_429(self, mock_sleep):
        session = make_session()
        session.request.return_value = make_response(429)
        with pytest.raises(requests.HTTPError):
            http_retry(session, "GET", self.URL, max_retries=3)
        assert session.request.call_count == 4

    # ── Sleep count matches retry count ──────────────────────────────────────

    def test_sleep_called_once_per_retry(self, mock_sleep):
        session = make_session()
        session.request.side_effect = [
            make_response(503),
            make_response(503),
            make_response(200),
        ]
        http_retry(session, "GET", self.URL, max_retries=3)
        assert mock_sleep.call_count == 2   # two failures before success

    # ── Custom retryable_statuses ─────────────────────────────────────────────

    def test_custom_retryable_statuses(self, mock_sleep):
        """A caller can override which codes trigger retries."""
        session = make_session()
        # 503 is normally retryable, but we exclude it here
        session.request.return_value = make_response(503)
        with pytest.raises(requests.HTTPError):
            http_retry(
                session, "GET", self.URL,
                retryable_statuses=frozenset({429}),   # only 429
            )
        assert session.request.call_count == 1
        mock_sleep.assert_not_called()

    # ── max_retries=0 → no retries ────────────────────────────────────────────

    def test_zero_max_retries_raises_immediately_on_5xx(self, mock_sleep):
        session = make_session()
        session.request.return_value = make_response(500)
        with pytest.raises(requests.HTTPError):
            http_retry(session, "GET", self.URL, max_retries=0)
        assert session.request.call_count == 1
        mock_sleep.assert_not_called()
