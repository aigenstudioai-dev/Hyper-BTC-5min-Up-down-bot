"""
tests/test_polymarket_auth.py – Unit tests for api_client.PolymarketAuth.

Covers issue #4 (wallet-signing coverage backfill). Uses a real throwaway
private key so the actual signing code runs; nothing here touches the
network.

`sign_order` previously crashed (issue #5: it called
LocalAccount.sign_typed_data, which does not exist on eth-account==0.10.0's
LocalAccount) and now calls Account.sign_typed_data(self.account.key, ...)
instead. TestSignOrder verifies the fix by recovering the signer address
from the returned signature.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data

from api_client import CHAIN_ID, CLOB_EXCHANGE, PolymarketAuth

TEST_PRIVATE_KEY = "0x" + "1" * 64


@pytest.fixture
def auth() -> PolymarketAuth:
    return PolymarketAuth(
        private_key=TEST_PRIVATE_KEY,
        api_key="test-key",
        api_secret="test-secret",
        api_passphrase="test-pass",
    )


class TestL1Headers:

    def test_contains_expected_keys(self, auth):
        headers = auth.l1_headers()
        assert set(headers) == {
            "POLY_ADDRESS", "POLY_SIGNATURE", "POLY_TIMESTAMP", "POLY_NONCE",
        }

    def test_address_matches_account(self, auth):
        headers = auth.l1_headers()
        assert headers["POLY_ADDRESS"] == auth.address

    def test_nonce_is_zero(self, auth):
        assert auth.l1_headers()["POLY_NONCE"] == "0"

    def test_signature_recovers_to_wallet_address(self, auth):
        headers = auth.l1_headers()
        message = f"polymarket{headers['POLY_TIMESTAMP']}"
        signable = encode_defunct(text=message)
        recovered = Account.recover_message(signable, signature=headers["POLY_SIGNATURE"])
        assert recovered == auth.address

    def test_signature_has_0x_prefix(self, auth):
        assert auth.l1_headers()["POLY_SIGNATURE"].startswith("0x")


class TestL2Headers:

    def _expected_signature(self, auth, ts: str, method: str, path: str, body: str) -> str:
        payload = ts + method.upper() + path + body
        return hmac.new(
            auth.api_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()

    def test_contains_expected_keys(self, auth):
        headers = auth.l2_headers("GET", "/orders")
        assert set(headers) == {
            "POLY-API-KEY", "POLY-TIMESTAMP", "POLY-SIGNATURE",
            "POLY-PASSPHRASE", "Content-Type",
        }
        assert headers["POLY-API-KEY"] == "test-key"
        assert headers["POLY-PASSPHRASE"] == "test-pass"

    def test_signature_matches_independent_hmac_get_no_body(self, auth):
        headers = auth.l2_headers("GET", "/orders")
        expected = self._expected_signature(auth, headers["POLY-TIMESTAMP"], "GET", "/orders", "")
        assert headers["POLY-SIGNATURE"] == expected

    def test_signature_matches_independent_hmac_post_with_body(self, auth):
        body = '{"foo":"bar"}'
        headers = auth.l2_headers("post", "/order", body)
        # method.upper() means lowercase input must still hash as "POST"
        expected = self._expected_signature(auth, headers["POLY-TIMESTAMP"], "POST", "/order", body)
        assert headers["POLY-SIGNATURE"] == expected

    def test_different_bodies_produce_different_signatures(self, auth):
        h1 = auth.l2_headers("POST", "/order", '{"a":1}')
        h2 = auth.l2_headers("POST", "/order", '{"a":2}')
        assert h1["POLY-SIGNATURE"] != h2["POLY-SIGNATURE"]

    def test_different_secret_produces_different_signature(self):
        auth_a = PolymarketAuth(TEST_PRIVATE_KEY, api_secret="secret-a")
        auth_b = PolymarketAuth(TEST_PRIVATE_KEY, api_secret="secret-b")
        sig_a = auth_a.l2_headers("GET", "/orders")["POLY-SIGNATURE"]
        sig_b = auth_b.l2_headers("GET", "/orders")["POLY-SIGNATURE"]
        assert sig_a != sig_b


class TestSignOrder:
    """
    Issue #5 (fixed): sign_order used to call the non-existent
    LocalAccount.sign_typed_data instance method on eth-account==0.10.0.
    It now calls Account.sign_typed_data(self.account.key, ...) instead.

    Issue #7: CLOB V2 migration (docs.polymarket.com/v2-migration).
    EIP-712 domain version "1" -> "2", verifyingContract moved to the V2
    exchange, and the signed Order struct drops taker/expiration/nonce/
    feeRateBps in favor of timestamp/metadata/builder.
    """

    ORDER = {
        "salt": 12345,
        "maker": "0x0000000000000000000000000000000000000001",
        "signer": "0x0000000000000000000000000000000000000001",
        "tokenId": 71321045,
        "makerAmount": 25_000_000,
        "takerAmount": 41_666_666,
        "side": 0,
        "signatureType": 0,
        "timestamp": 1_713_398_400_000,
        "metadata": "0x" + "0" * 64,
        "builder": "0x" + "0" * 64,
    }

    def test_signature_recovers_to_wallet_address(self, auth):
        order = {**self.ORDER, "maker": auth.address, "signer": auth.address}
        signature = auth.sign_order(order)

        domain = {
            "name": "Polymarket CTF Exchange",
            "version": "2",
            "chainId": CHAIN_ID,
            "verifyingContract": CLOB_EXCHANGE,
        }
        types = {
            "Order": [
                {"name": "salt", "type": "uint256"},
                {"name": "maker", "type": "address"},
                {"name": "signer", "type": "address"},
                {"name": "tokenId", "type": "uint256"},
                {"name": "makerAmount", "type": "uint256"},
                {"name": "takerAmount", "type": "uint256"},
                {"name": "side", "type": "uint8"},
                {"name": "signatureType", "type": "uint8"},
                {"name": "timestamp", "type": "uint256"},
                {"name": "metadata", "type": "bytes32"},
                {"name": "builder", "type": "bytes32"},
            ]
        }
        signable = encode_typed_data(domain_data=domain, message_types=types, message_data=order)
        recovered = Account.recover_message(signable, signature=signature)
        assert recovered == auth.address

    def test_exchange_address_is_v2(self):
        assert CLOB_EXCHANGE == "0xE111180000d2663C0091e4f400237545B87B996B"

    def test_signature_does_not_recover_under_v1_domain(self, auth):
        """Negative control: proves sign_order actually uses domain
        version "2", not "1" — recovering under the old V1 domain must
        NOT match the signer's address."""
        order = {**self.ORDER, "maker": auth.address, "signer": auth.address}
        signature = auth.sign_order(order)

        v1_domain = {
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
                {"name": "tokenId", "type": "uint256"},
                {"name": "makerAmount", "type": "uint256"},
                {"name": "takerAmount", "type": "uint256"},
                {"name": "side", "type": "uint8"},
                {"name": "signatureType", "type": "uint8"},
                {"name": "timestamp", "type": "uint256"},
                {"name": "metadata", "type": "bytes32"},
                {"name": "builder", "type": "bytes32"},
            ]
        }
        signable = encode_typed_data(domain_data=v1_domain, message_types=types, message_data=order)
        recovered = Account.recover_message(signable, signature=signature)
        assert recovered != auth.address

    def test_different_orders_produce_different_signatures(self, auth):
        order_a = {**self.ORDER, "maker": auth.address, "signer": auth.address, "salt": 1}
        order_b = {**self.ORDER, "maker": auth.address, "signer": auth.address, "salt": 2}
        assert auth.sign_order(order_a) != auth.sign_order(order_b)
