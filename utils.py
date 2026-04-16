"""
utils.py – HTTP retry utility with exponential back-off and full jitter.

Used by api_client.py (Polymarket CLOB) and backtest.py (Pyth Benchmarks)
so that transient network errors and rate-limit responses are handled
consistently across the entire codebase.

Retry policy
------------
Retried:
  • Network errors   – ConnectionError, Timeout, ChunkedEncodingError
  • HTTP 429         – rate-limited; Retry-After header is respected
  • HTTP 5xx         – 500, 502, 503, 504 (transient server faults)

NOT retried:
  • Other 4xx errors – 400, 401, 403, 404 … are permanent client errors;
    retrying wastes time and may repeat a harmful request.

Back-off formula  (full jitter – avoids thundering-herd on shared APIs):
    cap   = min(max_delay, base_delay × 2^attempt)
    sleep = random.uniform(0, cap)

Example delays with base=1.0, max=32.0 (without jitter, for illustration):
    attempt 0 → up to  1s
    attempt 1 → up to  2s
    attempt 2 → up to  4s
    attempt 3 → up to  8s
    attempt 4 → up to 16s
"""

from __future__ import annotations

import logging
import random
import time
from typing import FrozenSet, Optional

import requests

logger = logging.getLogger(__name__)

# Default set of HTTP status codes worth retrying
RETRYABLE_STATUSES: FrozenSet[int] = frozenset({429, 500, 502, 503, 504})


def http_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    max_retries: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 32.0,
    retryable_statuses: FrozenSet[int] = RETRYABLE_STATUSES,
    **kwargs,
) -> requests.Response:
    """
    Execute an HTTP request with exponential back-off + full jitter.

    Args:
        session:            requests.Session to use.
        method:             HTTP verb e.g. "GET", "POST", "DELETE".
        url:                Full request URL.
        max_retries:        Retry attempts after the first failure (default 4).
        base_delay:         Base back-off delay in seconds (default 1.0).
        max_delay:          Maximum back-off cap in seconds (default 32.0).
        retryable_statuses: HTTP codes to retry on (default: 429 + 5xx).
        **kwargs:           Forwarded to session.request().

    Returns:
        The successful requests.Response.

    Raises:
        requests.HTTPError:       Non-retryable 4xx, or retryable code after
                                  all retries are exhausted.
        requests.RequestException: Network error after all retries exhausted.
    """
    last_exc: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        # ── Execute ───────────────────────────────────────────────────────────
        try:
            resp = session.request(method, url, **kwargs)
        except (
            requests.ConnectionError,
            requests.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ) as exc:
            last_exc = exc
            if attempt == max_retries:
                logger.error(
                    "%s %s – network error after %d attempt(s): %s",
                    method, url, attempt + 1, exc,
                )
                raise
            delay = _jitter(base_delay, max_delay, attempt)
            logger.warning(
                "%s %s – network error (attempt %d/%d), retry in %.1fs: %s",
                method, url, attempt + 1, max_retries + 1, delay, exc,
            )
            time.sleep(delay)
            continue

        # ── Non-retryable response ────────────────────────────────────────────
        if resp.status_code not in retryable_statuses:
            resp.raise_for_status()   # raises HTTPError for 4xx / unexpected 5xx
            return resp

        # ── Retryable response – last attempt ────────────────────────────────
        if attempt == max_retries:
            logger.error(
                "%s %s – HTTP %d after %d attempt(s)",
                method, url, resp.status_code, attempt + 1,
            )
            resp.raise_for_status()

        # ── Retryable response – back off and try again ───────────────────────
        delay = _parse_retry_after(resp) or _jitter(base_delay, max_delay, attempt)
        logger.warning(
            "%s %s – HTTP %d (attempt %d/%d), retry in %.1fs",
            method, url, resp.status_code, attempt + 1, max_retries + 1, delay,
        )
        time.sleep(delay)

    # Unreachable; satisfies type checkers
    raise RuntimeError("http_retry: retry loop exited without returning")  # pragma: no cover


# ─── Internal helpers ──────────────────────────────────────────────────────────

def _jitter(base: float, cap: float, attempt: int) -> float:
    """Full-jitter exponential back-off: uniform in [0, min(cap, base × 2^attempt)]."""
    return random.uniform(0.0, min(cap, base * (2 ** attempt)))


def _parse_retry_after(resp: requests.Response) -> Optional[float]:
    """
    Parse the Retry-After response header.

    Returns the value in seconds as a float, or None if the header is
    absent or cannot be parsed as a number.
    (RFC 7231 also allows an HTTP-date value; we skip that case and fall
    back to jitter so the bot isn't blocked on date parsing.)
    """
    header = resp.headers.get("Retry-After")
    if header is None:
        return None
    try:
        return float(header)
    except ValueError:
        return None
