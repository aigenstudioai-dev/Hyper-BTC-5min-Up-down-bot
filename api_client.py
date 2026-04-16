"""
api_client.py – Polymarket CLOB + Pyth Network wrappers.

Covers:
  • L1 wallet-signature auth  (derive API credentials from private key)
  • L2 HMAC-SHA256 auth       (sign every CLOB request)
  • EIP-712 order signing      (CTF Exchange on Polygon)
  • Market discovery           (BTC 5-min windows via Gamma API)
  • Order placement / cancel   (limit orders at mid-point)
  • Pyth BTC/USD price feed    (Hermes REST, ~400 ms latency)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests
from eth_account import Account
from utils import http_retry
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount

logger = logging.getLogger(__name__)

# ─── Network constants ─────────────────────────────────────────────────────────

CLOB_BASE_URL: str = os.getenv("CLOB_BASE_URL", "https://clob.polymarket.com")
GAMMA_BASE_URL: str = os.getenv("GAMMA_BASE_URL", "https://gamma-api.polymarket.com")
PYTH_HERMES_URL: str = os.getenv("PYTH_HERMES_URL", "https://hermes.pyth.network")

# Polymarket CTF Exchange on Polygon mainnet
CLOB_EXCHANGE: str = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
CHAIN_ID: int = 137  # Polygon

# Pyth BTC/USD feed ID (verified via https://pyth.network/price-feeds)
BTC_USD_FEED_ID: str = (
    "0xe62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43"
)

WINDOW_SECONDS: int = 300  # 5-minute markets

# ─── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class PriceData:
    price: float          # USD
    confidence: float     # ± uncertainty in USD
    timestamp: int        # Unix seconds (publish time)
    feed_id: str


@dataclass
class TokenPair:
    yes_token_id: str   # "UP" outcome
    no_token_id: str    # "DOWN" outcome


@dataclass
class MarketInfo:
    condition_id: str
    question: str
    end_time: int           # Unix timestamp when the window closes
    tokens: TokenPair
    yes_bid: float          # best BID price for YES (USDC per share, 0–1)
    yes_ask: float          # best ASK price for YES
    no_bid: float
    no_ask: float
    is_active: bool

    @property
    def yes_mid(self) -> float:
        return (self.yes_bid + self.yes_ask) / 2 if self.yes_bid and self.yes_ask else self.yes_ask

    @property
    def no_mid(self) -> float:
        return (self.no_bid + self.no_ask) / 2 if self.no_bid and self.no_ask else self.no_ask


@dataclass
class OrderResult:
    order_id: str
    status: str                    # "live" | "matched" | "cancelled" | "error"
    filled_usdc: float = 0.0
    remaining_usdc: float = 0.0
    error: Optional[str] = None


# ─── Authentication ────────────────────────────────────────────────────────────


class PolymarketAuth:
    """
    Manages both authentication layers required by the Polymarket CLOB.

    L1 – wallet signature: used once to derive/refresh API credentials.
    L2 – HMAC-SHA256:      added to every request header.
    """

    def __init__(
        self,
        private_key: str,
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
    ) -> None:
        pk = private_key if private_key.startswith("0x") else f"0x{private_key}"
        self.account: LocalAccount = Account.from_key(pk)
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase

    # ── L1 ────────────────────────────────────────────────────────────────────

    def l1_headers(self) -> Dict[str, str]:
        """Sign a timestamp with the wallet private key (L1)."""
        ts = str(int(time.time()))
        message = f"polymarket{ts}"
        msg = encode_defunct(text=message)
        sig = self.account.sign_message(msg).signature.hex()
        return {
            "POLY_ADDRESS": self.account.address,
            "POLY_SIGNATURE": f"0x{sig}" if not sig.startswith("0x") else sig,
            "POLY_TIMESTAMP": ts,
            "POLY_NONCE": "0",
        }

    # ── L2 ────────────────────────────────────────────────────────────────────

    def l2_headers(
        self, method: str, path: str, body: str = ""
    ) -> Dict[str, str]:
        """HMAC-SHA256 sign each CLOB request (L2)."""
        ts = str(int(time.time() * 1000))
        payload = ts + method.upper() + path + body
        sig = hmac.new(
            self.api_secret.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        return {
            "POLY-API-KEY": self.api_key,
            "POLY-TIMESTAMP": ts,
            "POLY-SIGNATURE": sig,
            "POLY-PASSPHRASE": self.api_passphrase,
            "Content-Type": "application/json",
        }

    # ── EIP-712 order signing ─────────────────────────────────────────────────

    def sign_order(self, order: dict) -> str:
        """
        Sign a Polymarket CLOB order using EIP-712 typed data.

        The CTF Exchange verifies this on-chain; the signature must match
        the maker address embedded in the order struct.
        """
        domain = {
            "name": "Polymarket CTF Exchange",
            "version": "1",
            "chainId": CHAIN_ID,
            "verifyingContract": CLOB_EXCHANGE,
        }
        types = {
            "Order": [
                {"name": "salt", "type": "uint256"},
                {"name": "maker", "type": "address"},
                {"name": "signer", "type": "address"},
                {"name": "taker", "type": "address"},
                {"name": "tokenId", "type": "uint256"},
                {"name": "makerAmount", "type": "uint256"},
                {"name": "takerAmount", "type": "uint256"},
                {"name": "expiration", "type": "uint256"},
                {"name": "nonce", "type": "uint256"},
                {"name": "feeRateBps", "type": "uint256"},
                {"name": "side", "type": "uint8"},
                {"name": "signatureType", "type": "uint8"},
            ]
        }
        structured = {
            "domain": domain,
            "types": types,
            "primaryType": "Order",
            "message": order,
        }
        signed = self.account.sign_typed_data(
            domain_data=domain,
            message_types={"Order": types["Order"]},
            message_data=order,
        )
        return signed.signature.hex()

    @property
    def address(self) -> str:
        return self.account.address


# ─── Polymarket CLOB client ────────────────────────────────────────────────────


class PolymarketClient:
    """
    REST wrapper for the Polymarket Gamma + CLOB APIs.

    All mutating calls (order placement, cancellation) are gated by the
    `simulation` flag so the bot can run in paper-trade mode without any
    on-chain interaction.
    """

    def __init__(self, auth: PolymarketAuth, simulation: bool = True) -> None:
        self.auth = auth
        self.simulation = simulation
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get(self, base: str, path: str, params: dict = None, auth_level: int = 0) -> dict:
        url = base + path
        headers = {}
        if auth_level == 1:
            headers = self.auth.l1_headers()
        elif auth_level == 2:
            headers = self.auth.l2_headers("GET", path)
        resp = http_retry(self._session, "GET", url, params=params, headers=headers, timeout=10)
        return resp.json()

    def _post(self, path: str, body: dict, auth_level: int = 2) -> dict:
        body_str = json.dumps(body, separators=(",", ":"))
        headers = self.auth.l2_headers("POST", path, body_str)
        resp = http_retry(
            self._session, "POST", CLOB_BASE_URL + path,
            data=body_str, headers=headers, timeout=10,
        )
        return resp.json()

    def _delete(self, path: str, auth_level: int = 2) -> dict:
        headers = self.auth.l2_headers("DELETE", path)
        resp = http_retry(
            self._session, "DELETE", CLOB_BASE_URL + path,
            headers=headers, timeout=10,
        )
        return resp.json()

    # ── API key derivation ────────────────────────────────────────────────────

    def derive_api_credentials(self) -> dict:
        """
        Derive a fresh set of CLOB API credentials using the L1 wallet signature.
        Call this once and persist the result to your .env file.
        """
        headers = self.auth.l1_headers()
        resp = http_retry(
            self._session, "POST", CLOB_BASE_URL + "/auth/api-key",
            headers=headers, timeout=15,
        )
        creds = resp.json()
        logger.info("API credentials derived for address %s", self.auth.address)
        return creds  # {"apiKey": ..., "secret": ..., "passphrase": ...}

    # ── Market discovery ──────────────────────────────────────────────────────

    def find_btc_5min_markets(self, lookahead_windows: int = 2) -> List[MarketInfo]:
        """
        Discover active BTC 5-minute UP/DOWN markets from the Gamma API.

        Strategy:
          1. Query /markets with keyword filters for BTC and the next few
             window end-times (Unix timestamps divisible by 300).
          2. Parse the YES/NO token pair from the market's clob_token_ids field.
          3. Enrich with live order-book prices from the CLOB.

        Returns markets sorted by end_time ascending.
        """
        now = int(time.time())
        target_windows = [
            _next_window_close(now, offset=i) for i in range(lookahead_windows)
        ]

        markets: List[MarketInfo] = []

        # Gamma API returns markets with question text and metadata
        try:
            data = self._get(
                GAMMA_BASE_URL,
                "/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "tag_slug": "crypto",
                    "limit": 200,
                },
            )
        except Exception as exc:
            logger.error("Gamma API error: %s", exc)
            return markets

        raw_markets = data if isinstance(data, list) else data.get("markets", [])

        for m in raw_markets:
            if not _is_btc_5min_market(m):
                continue

            end_ts = _parse_end_time(m)
            if end_ts is None or end_ts not in target_windows:
                continue

            tokens = _extract_tokens(m)
            if tokens is None:
                continue

            book = self._fetch_orderbook(tokens.yes_token_id)
            no_book = self._fetch_orderbook(tokens.no_token_id)

            markets.append(
                MarketInfo(
                    condition_id=m.get("conditionId", m.get("condition_id", "")),
                    question=m.get("question", ""),
                    end_time=end_ts,
                    tokens=tokens,
                    yes_bid=book.get("bid", 0.0),
                    yes_ask=book.get("ask", 1.0),
                    no_bid=no_book.get("bid", 0.0),
                    no_ask=no_book.get("ask", 1.0),
                    is_active=True,
                )
            )

        markets.sort(key=lambda m: m.end_time)
        logger.debug("Found %d BTC 5-min markets", len(markets))
        return markets

    def _fetch_orderbook(self, token_id: str) -> Dict[str, float]:
        """Return best bid/ask for a single outcome token."""
        try:
            book = self._get(CLOB_BASE_URL, f"/orderbook/{token_id}", auth_level=0)
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            best_bid = float(bids[0]["price"]) if bids else 0.0
            best_ask = float(asks[0]["price"]) if asks else 1.0
            return {"bid": best_bid, "ask": best_ask}
        except Exception as exc:
            logger.warning("Orderbook fetch failed for %s: %s", token_id[:8], exc)
            return {"bid": 0.0, "ask": 1.0}

    # ── Order management ──────────────────────────────────────────────────────

    def place_limit_order(
        self,
        token_id: str,
        side: str,           # "BUY" | "SELL"
        price: float,        # USDC per share (0–1)
        size_usdc: float,    # collateral to spend
        expiry_seconds: int = 30,
    ) -> OrderResult:
        """
        Place a limit order on the Polymarket CLOB.

        In simulation mode, returns a fake filled result without any API call.
        In live mode, builds, signs, and submits the EIP-712 order.
        """
        if self.simulation:
            fake_id = f"SIM-{int(time.time())}-{random.randint(1000, 9999)}"
            logger.info(
                "[SIM] LIMIT %s %s @ %.4f USDC (size=%.2f USDC) → %s",
                side, token_id[:8], price, size_usdc, fake_id,
            )
            return OrderResult(
                order_id=fake_id,
                status="live",
                filled_usdc=0.0,
                remaining_usdc=size_usdc,
            )

        # Live order
        salt = random.randint(0, 2**256 - 1)
        expiration = int(time.time()) + expiry_seconds
        maker_amount = int(size_usdc * 1e6)          # USDC has 6 decimals
        taker_amount = int(size_usdc / price * 1e6)  # shares at this price

        order_struct = {
            "salt": salt,
            "maker": self.auth.address,
            "signer": self.auth.address,
            "taker": "0x0000000000000000000000000000000000000000",
            "tokenId": int(token_id),
            "makerAmount": maker_amount,
            "takerAmount": taker_amount,
            "expiration": expiration,
            "nonce": 0,
            "feeRateBps": 0,
            "side": 0 if side == "BUY" else 1,
            "signatureType": 0,  # EOA
        }

        signature = self.auth.sign_order(order_struct)

        payload = {
            "order": order_struct,
            "signature": signature,
            "orderType": "LIMIT",
        }

        try:
            resp = self._post("/order", payload)
            order_id = resp.get("orderID", resp.get("order_id", "unknown"))
            logger.info(
                "[LIVE] LIMIT %s @ %.4f → order_id=%s", side, price, order_id
            )
            return OrderResult(
                order_id=order_id,
                status="live",
                filled_usdc=0.0,
                remaining_usdc=size_usdc,
            )
        except Exception as exc:
            logger.error("Order placement failed: %s", exc)
            return OrderResult(
                order_id="",
                status="error",
                error=str(exc),
            )

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns True on success."""
        if self.simulation:
            logger.info("[SIM] CANCEL order %s", order_id)
            return True
        try:
            self._delete(f"/orders/{order_id}")
            return True
        except Exception as exc:
            logger.error("Cancel failed for %s: %s", order_id, exc)
            return False

    def get_order_status(self, order_id: str) -> OrderResult:
        """Fetch live order status from the CLOB."""
        if self.simulation or order_id.startswith("SIM-"):
            # Simulate a 70% fill rate for paper-trading
            return OrderResult(
                order_id=order_id,
                status="matched" if random.random() < 0.70 else "live",
                filled_usdc=0.0,
            )
        try:
            data = self._get(CLOB_BASE_URL, f"/orders/{order_id}", auth_level=2)
            return OrderResult(
                order_id=order_id,
                status=data.get("status", "unknown"),
                filled_usdc=float(data.get("sizeFilled", 0)) / 1e6,
                remaining_usdc=float(data.get("sizeRemaining", 0)) / 1e6,
            )
        except Exception as exc:
            logger.error("get_order_status error: %s", exc)
            return OrderResult(order_id=order_id, status="error", error=str(exc))

    def get_usdc_balance(self) -> float:
        """Return the CLOB account's available USDC balance."""
        if self.simulation:
            return float(os.getenv("BANKROLL_USDC", "300"))
        try:
            data = self._get(
                CLOB_BASE_URL, "/balance", auth_level=2
            )
            return float(data.get("balance", 0))
        except Exception as exc:
            logger.error("Balance fetch error: %s", exc)
            return 0.0


# ─── Pyth price feed ───────────────────────────────────────────────────────────


class PythPriceFeed:
    """
    Fetches BTC/USD spot price from Pyth Network's Hermes REST endpoint.

    Hermes provides the latest attested price for each Pyth feed.
    Confidence intervals are surfaced so the strategy can gate on spread.
    """

    def __init__(self) -> None:
        self._session = requests.Session()
        self._last: Optional[PriceData] = None

    def fetch_btc_price(self) -> PriceData:
        """
        Query Hermes for the latest BTC/USD price.
        Returns cached value on transient errors (with a staleness warning).
        """
        url = f"{PYTH_HERMES_URL}/v2/updates/price/latest"
        params = {"ids[]": BTC_USD_FEED_ID, "parsed": "true"}
        try:
            resp = self._session.get(url, params=params, timeout=5)
            resp.raise_for_status()
            parsed = resp.json().get("parsed", [])
            if not parsed:
                raise ValueError("Empty parsed price list from Hermes")

            entry = parsed[0]
            price_obj = entry["price"]
            raw_price = float(price_obj["price"])
            exponent = int(price_obj["expo"])
            price_usd = raw_price * (10 ** exponent)

            raw_conf = float(price_obj["conf"])
            conf_usd = raw_conf * (10 ** exponent)

            pub_time = int(entry["metadata"]["slot"])   # slot ≈ publish time
            # Prefer the explicit publish_time if available
            if "publish_time" in entry:
                pub_time = int(entry["publish_time"])

            self._last = PriceData(
                price=price_usd,
                confidence=conf_usd,
                timestamp=pub_time,
                feed_id=BTC_USD_FEED_ID,
            )
            return self._last

        except Exception as exc:
            if self._last is not None:
                age = int(time.time()) - self._last.timestamp
                logger.warning(
                    "Pyth error (using cached price %.2f, age %ds): %s",
                    self._last.price, age, exc,
                )
                return self._last
            raise RuntimeError(f"Pyth price unavailable: {exc}") from exc

    @property
    def last_price(self) -> Optional[float]:
        return self._last.price if self._last else None


# ─── Helper utilities ──────────────────────────────────────────────────────────


def _next_window_close(now: int, offset: int = 0) -> int:
    """
    Calculate the Unix timestamp of the (offset+1)-th upcoming 5-minute window close.

    Every Polymarket BTC 5-min market closes at a time divisible by 300.
    E.g. 17:00:00, 17:05:00, 17:10:00 ...
    """
    base = ((now // WINDOW_SECONDS) + 1 + offset) * WINDOW_SECONDS
    return base


def _is_btc_5min_market(market: dict) -> bool:
    """
    Heuristic check that a Gamma market is a BTC 5-minute up/down market.
    Adjust the keyword set as Polymarket's naming conventions evolve.
    """
    q: str = market.get("question", "").lower()
    slug: str = market.get("slug", "").lower()
    keywords = ("btc", "bitcoin")
    time_kws = ("5-min", "5 min", "5min", "300s", "5-minute")
    has_btc = any(k in q or k in slug for k in keywords)
    has_time = any(k in q or k in slug for k in time_kws)
    is_up_down = any(k in q for k in ("above", "below", "higher", "lower", "up", "down"))
    return has_btc and (has_time or is_up_down)


def _parse_end_time(market: dict) -> Optional[int]:
    """Extract and return the market end time as a Unix integer."""
    for key in ("endDateIso", "end_date_iso", "endDate", "end_date", "end_time"):
        val = market.get(key)
        if val is None:
            continue
        if isinstance(val, (int, float)):
            ts = int(val)
        else:
            import datetime
            try:
                dt = datetime.datetime.fromisoformat(str(val).replace("Z", "+00:00"))
                ts = int(dt.timestamp())
            except Exception:
                continue
        # Snap to nearest 300-second boundary (guard against minor drift)
        snapped = round(ts / WINDOW_SECONDS) * WINDOW_SECONDS
        return snapped
    return None


def _extract_tokens(market: dict) -> Optional[TokenPair]:
    """Parse YES/NO token IDs from the market dict."""
    # Polymarket encodes outcome tokens in different fields depending on API version
    tokens = market.get("clobTokenIds") or market.get("clob_token_ids")
    if tokens and len(tokens) >= 2:
        return TokenPair(yes_token_id=str(tokens[0]), no_token_id=str(tokens[1]))

    outcomes = market.get("outcomes", [])
    token_ids = market.get("outcomePrices") or market.get("outcome_prices") or []
    if len(outcomes) >= 2 and len(token_ids) >= 2:
        return TokenPair(yes_token_id=str(token_ids[0]), no_token_id=str(token_ids[1]))

    return None


def seconds_until_close(end_time: int) -> float:
    """Seconds remaining until a market window closes."""
    return end_time - time.time()
