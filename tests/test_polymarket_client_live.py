"""
tests/test_polymarket_client_live.py – Unit tests for PolymarketClient's
LIVE (simulation=False) branches: place_limit_order, cancel_order,
get_order_status, get_usdc_balance, derive_api_credentials.

Issue #4. All HTTP is mocked by patching api_client.http_retry, the single
seam every _get/_post/_delete call funnels through — nothing here touches
the network.

place_limit_order's tests mock `client.auth.sign_order` directly: the
signer itself is separately covered in
tests/test_polymarket_auth.py::TestSignOrder, so these tests isolate
order-construction/dispatch correctness from signing.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from api_client import PolymarketAuth, PolymarketClient

TEST_PRIVATE_KEY = "0x" + "1" * 64
TOKEN_ID = "71321045679252212594626385532706912750332728571942532289631379312455583992563"


def make_response(json_body: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = json_body
    return resp


@pytest.fixture
def client() -> PolymarketClient:
    auth = PolymarketAuth(
        private_key=TEST_PRIVATE_KEY,
        api_key="k", api_secret="s", api_passphrase="p",
    )
    return PolymarketClient(auth, simulation=False)


class TestPlaceLimitOrderLive:

    def test_order_struct_fields(self, client):
        client.auth.sign_order = MagicMock(return_value="0xdeadbeef")
        before = int(time.time())

        with patch("api_client.http_retry", return_value=make_response({"orderID": "OID-1"})) as mock_retry:
            client.place_limit_order(
                token_id=TOKEN_ID, side="BUY", price=0.60, size_usdc=25.0, expiry_seconds=10,
            )

        order = client.auth.sign_order.call_args[0][0]
        assert 0 <= order["salt"] < 2 ** 256
        assert order["maker"] == client.auth.address
        assert order["signer"] == client.auth.address
        assert order["taker"] == "0x0000000000000000000000000000000000000000"
        assert order["tokenId"] == int(TOKEN_ID)
        assert order["makerAmount"] == 25_000_000            # 25 USDC * 1e6
        assert order["takerAmount"] == int(25.0 / 0.60 * 1e6)
        assert before + 10 <= order["expiration"] <= before + 11
        assert order["nonce"] == 0
        assert order["feeRateBps"] == 0
        assert order["side"] == 0                             # BUY
        assert order["signatureType"] == 0

        # http_retry was called with a POST to /order carrying the signed payload
        _, kwargs = mock_retry.call_args
        assert mock_retry.call_args[0][1] == "POST"
        assert mock_retry.call_args[0][2].endswith("/order")

    def test_sell_side_maps_to_1(self, client):
        client.auth.sign_order = MagicMock(return_value="0xdeadbeef")
        with patch("api_client.http_retry", return_value=make_response({"orderID": "OID-1"})):
            client.place_limit_order(token_id=TOKEN_ID, side="SELL", price=0.40, size_usdc=10.0)
        order = client.auth.sign_order.call_args[0][0]
        assert order["side"] == 1

    def test_post_payload_shape(self, client):
        client.auth.sign_order = MagicMock(return_value="0xSIGNATURE")
        with patch("api_client.http_retry", return_value=make_response({"orderID": "OID-1"})) as mock_retry:
            client.place_limit_order(token_id=TOKEN_ID, side="BUY", price=0.60, size_usdc=25.0)
        import json
        body = json.loads(mock_retry.call_args.kwargs["data"])
        assert body["signature"] == "0xSIGNATURE"
        assert body["orderType"] == "LIMIT"
        assert "order" in body

    def test_success_returns_live_order_result(self, client):
        client.auth.sign_order = MagicMock(return_value="0xdeadbeef")
        with patch("api_client.http_retry", return_value=make_response({"orderID": "OID-42"})):
            result = client.place_limit_order(token_id=TOKEN_ID, side="BUY", price=0.60, size_usdc=25.0)
        assert result.status == "live"
        assert result.order_id == "OID-42"
        assert result.remaining_usdc == pytest.approx(25.0)

    def test_falls_back_to_order_id_key(self, client):
        client.auth.sign_order = MagicMock(return_value="0xdeadbeef")
        with patch("api_client.http_retry", return_value=make_response({"order_id": "OID-legacy"})):
            result = client.place_limit_order(token_id=TOKEN_ID, side="BUY", price=0.60, size_usdc=25.0)
        assert result.order_id == "OID-legacy"

    def test_http_failure_returns_error_result(self, client):
        client.auth.sign_order = MagicMock(return_value="0xdeadbeef")
        with patch("api_client.http_retry", side_effect=RuntimeError("network down")):
            result = client.place_limit_order(token_id=TOKEN_ID, side="BUY", price=0.60, size_usdc=25.0)
        assert result.status == "error"
        assert result.order_id == ""
        assert "network down" in result.error


class TestCancelOrderLive:

    def test_success_returns_true(self, client):
        with patch("api_client.http_retry", return_value=make_response({})):
            assert client.cancel_order("OID-1") is True

    def test_failure_returns_false_not_raised(self, client):
        with patch("api_client.http_retry", side_effect=RuntimeError("boom")):
            assert client.cancel_order("OID-1") is False


class TestGetOrderStatusLive:

    def test_parses_fields_and_scales_amounts(self, client):
        body = {"status": "matched", "sizeFilled": "25000000", "sizeRemaining": "0"}
        with patch("api_client.http_retry", return_value=make_response(body)):
            result = client.get_order_status("0xREAL_ORDER_ID")
        assert result.status == "matched"
        assert result.filled_usdc == pytest.approx(25.0)
        assert result.remaining_usdc == pytest.approx(0.0)

    def test_failure_returns_error_result(self, client):
        with patch("api_client.http_retry", side_effect=RuntimeError("boom")):
            result = client.get_order_status("0xREAL_ORDER_ID")
        assert result.status == "error"
        assert "boom" in result.error

    def test_sim_prefixed_id_never_hits_network_even_in_live_client(self, client):
        with patch("api_client.http_retry") as mock_retry:
            client.get_order_status("SIM-123-456")
        mock_retry.assert_not_called()


class TestGetUsdcBalanceLive:

    def test_parses_balance(self, client):
        with patch("api_client.http_retry", return_value=make_response({"balance": "123.45"})):
            assert client.get_usdc_balance() == pytest.approx(123.45)

    def test_failure_returns_zero(self, client):
        with patch("api_client.http_retry", side_effect=RuntimeError("boom")):
            assert client.get_usdc_balance() == 0.0


class TestDeriveApiCredentials:

    def test_uses_l1_headers_and_returns_parsed_dict(self, client):
        creds = {"apiKey": "k1", "secret": "s1", "passphrase": "p1"}
        with patch("api_client.http_retry", return_value=make_response(creds)) as mock_retry:
            result = client.derive_api_credentials()
        assert result == creds
        headers = mock_retry.call_args.kwargs["headers"]
        assert "POLY_SIGNATURE" in headers  # L1, not L2
