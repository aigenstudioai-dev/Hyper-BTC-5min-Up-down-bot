"""
tests/test_approve_usdc.py – Unit tests for approve_usdc helpers.

All tests are fully offline:
  • Pure functions (_is_already_approved) are tested directly.
  • _check_and_approve is tested by injecting mock web3 contract / account
    objects instead of real Polygon connections.
  • The top-level approve() function is tested by patching Web3 at import time.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch, call

import pytest

from approve_usdc import (
    ALREADY_APPROVED_THRESHOLD,
    MAX_UINT256,
    _check_and_approve,
    _create_w3_and_account,
    _get_allowance,
    _is_already_approved,
    _send_approval,
    approve,
)
from api_client import CLOB_EXCHANGE, USDC_ADDRESS


# ─── Helpers ──────────────────────────────────────────────────────────────────

OWNER_ADDR   = "0xABcDef1234567890AbcdEF1234567890aBCDEF12"
SPENDER_ADDR = CLOB_EXCHANGE


def _mock_account(address: str = OWNER_ADDR) -> MagicMock:
    acct = MagicMock()
    acct.address = address
    return acct


def _mock_contract(allowance: int = 0) -> MagicMock:
    """Return a mock ERC-20 contract whose allowance() returns the given value."""
    fn_allowance = MagicMock()
    fn_allowance.return_value = allowance          # .call() returns int
    fn_allowance_call = MagicMock()
    fn_allowance_call.call = fn_allowance

    fn_approve_build = MagicMock(return_value={"data": "0x1234"})
    fn_approve = MagicMock()
    fn_approve.build_transaction = fn_approve_build

    contract = MagicMock()
    contract.functions.allowance.return_value.call = fn_allowance
    contract.functions.approve.return_value.build_transaction = fn_approve_build
    return contract


def _mock_w3(gas_price: int = 30_000_000_000) -> MagicMock:
    """Return a mock Web3 instance."""
    w3 = MagicMock()
    w3.eth.get_transaction_count.return_value = 42
    w3.eth.gas_price = gas_price

    signed      = MagicMock()
    signed.rawTransaction = b"\xde\xad\xbe\xef"
    w3.eth.send_raw_transaction.return_value = b"\xaa" * 32  # 32-byte tx hash

    receipt = MagicMock()
    receipt.status   = 1
    receipt.gasUsed  = 55_000
    w3.eth.wait_for_transaction_receipt.return_value = receipt

    return w3


# ─── _is_already_approved ────────────────────────────────────────────────────

class TestIsAlreadyApproved:

    def test_exactly_at_threshold_is_approved(self):
        assert _is_already_approved(ALREADY_APPROVED_THRESHOLD) is True

    def test_above_threshold_is_approved(self):
        assert _is_already_approved(MAX_UINT256) is True

    def test_one_below_threshold_is_not_approved(self):
        assert _is_already_approved(ALREADY_APPROVED_THRESHOLD - 1) is False

    def test_zero_is_not_approved(self):
        assert _is_already_approved(0) is False

    def test_custom_threshold_respected(self):
        assert _is_already_approved(100, threshold=100) is True
        assert _is_already_approved(99,  threshold=100) is False


# ─── _get_allowance ──────────────────────────────────────────────────────────

class TestGetAllowance:

    def test_returns_allowance_from_contract(self):
        contract = MagicMock()
        contract.functions.allowance.return_value.call.return_value = 999
        result = _get_allowance(contract, OWNER_ADDR, SPENDER_ADDR)
        assert result == 999

    def test_passes_owner_and_spender_to_contract(self):
        contract = MagicMock()
        contract.functions.allowance.return_value.call.return_value = 0
        _get_allowance(contract, OWNER_ADDR, SPENDER_ADDR)
        contract.functions.allowance.assert_called_once_with(OWNER_ADDR, SPENDER_ADDR)


# ─── _send_approval ──────────────────────────────────────────────────────────

class TestSendApproval:

    def test_builds_tx_with_correct_spender_and_max_uint(self):
        account  = _mock_account()
        w3       = _mock_w3()
        contract = MagicMock()
        build_tx = MagicMock(return_value={"nonce": 42})
        contract.functions.approve.return_value.build_transaction = build_tx

        signed = MagicMock()
        signed.rawTransaction = b"\x00" * 32
        account.sign_transaction.return_value = signed

        receipt = MagicMock()
        receipt.status  = 1
        receipt.gasUsed = 50_000
        w3.eth.send_raw_transaction.return_value = b"\xbb" * 32
        w3.eth.wait_for_transaction_receipt.return_value = receipt

        _send_approval(contract, account, SPENDER_ADDR, w3)

        contract.functions.approve.assert_called_once_with(SPENDER_ADDR, MAX_UINT256)

    def test_returns_hex_tx_hash(self):
        account  = _mock_account()
        w3       = _mock_w3()
        contract = MagicMock()
        contract.functions.approve.return_value.build_transaction.return_value = {}

        signed = MagicMock()
        signed.rawTransaction = b"\x00" * 32
        account.sign_transaction.return_value = signed

        raw_hash = bytes(range(32))
        receipt  = MagicMock()
        receipt.status  = 1
        receipt.gasUsed = 50_000
        w3.eth.send_raw_transaction.return_value = raw_hash
        w3.eth.wait_for_transaction_receipt.return_value = receipt

        result = _send_approval(contract, account, SPENDER_ADDR, w3)
        assert result == raw_hash.hex()

    def test_raises_on_reverted_receipt(self):
        account  = _mock_account()
        w3       = _mock_w3()
        contract = MagicMock()
        contract.functions.approve.return_value.build_transaction.return_value = {}

        signed = MagicMock()
        signed.rawTransaction = b"\x00" * 32
        account.sign_transaction.return_value = signed

        receipt = MagicMock()
        receipt.status  = 0    # revert!
        receipt.gasUsed = 21_000
        w3.eth.send_raw_transaction.return_value = b"\xcc" * 32
        w3.eth.wait_for_transaction_receipt.return_value = receipt

        with pytest.raises(RuntimeError, match="reverted"):
            _send_approval(contract, account, SPENDER_ADDR, w3)


# ─── _check_and_approve ──────────────────────────────────────────────────────

class TestCheckAndApprove:

    def _make_setup(self, allowance: int):
        account  = _mock_account()
        w3       = _mock_w3()
        contract = MagicMock()
        contract.functions.allowance.return_value.call.return_value = allowance

        signed = MagicMock()
        signed.rawTransaction = b"\x00" * 32
        account.sign_transaction.return_value = signed

        build_tx = MagicMock(return_value={})
        contract.functions.approve.return_value.build_transaction = build_tx

        raw_hash = b"\xdd" * 32
        receipt  = MagicMock()
        receipt.status  = 1
        receipt.gasUsed = 50_000
        w3.eth.send_raw_transaction.return_value = raw_hash
        w3.eth.wait_for_transaction_receipt.return_value = receipt

        return contract, account, w3, raw_hash

    # ── Already approved path ─────────────────────────────────────────────────

    def test_returns_none_when_already_approved(self):
        contract, account, w3, _ = self._make_setup(MAX_UINT256)
        result = _check_and_approve(contract, account, SPENDER_ADDR, w3, dry_run=False)
        assert result is None

    def test_does_not_send_tx_when_already_approved(self):
        contract, account, w3, _ = self._make_setup(MAX_UINT256)
        _check_and_approve(contract, account, SPENDER_ADDR, w3, dry_run=False)
        w3.eth.send_raw_transaction.assert_not_called()

    # ── Dry-run path ──────────────────────────────────────────────────────────

    def test_dry_run_returns_none(self):
        contract, account, w3, _ = self._make_setup(0)
        result = _check_and_approve(contract, account, SPENDER_ADDR, w3, dry_run=True)
        assert result is None

    def test_dry_run_does_not_send_tx(self):
        contract, account, w3, _ = self._make_setup(0)
        _check_and_approve(contract, account, SPENDER_ADDR, w3, dry_run=True)
        w3.eth.send_raw_transaction.assert_not_called()

    def test_dry_run_even_when_allowance_is_zero(self):
        """Even zero allowance → no tx in dry-run mode."""
        contract, account, w3, _ = self._make_setup(0)
        _check_and_approve(contract, account, SPENDER_ADDR, w3, dry_run=True)
        account.sign_transaction.assert_not_called()

    # ── Live approval path ────────────────────────────────────────────────────

    def test_sends_tx_when_allowance_is_zero(self):
        contract, account, w3, raw_hash = self._make_setup(0)
        result = _check_and_approve(contract, account, SPENDER_ADDR, w3, dry_run=False)
        w3.eth.send_raw_transaction.assert_called_once()
        assert result == raw_hash.hex()

    def test_returns_tx_hash_on_success(self):
        contract, account, w3, raw_hash = self._make_setup(0)
        result = _check_and_approve(contract, account, SPENDER_ADDR, w3, dry_run=False)
        assert result == raw_hash.hex()

    def test_sends_tx_when_allowance_just_below_threshold(self):
        contract, account, w3, raw_hash = self._make_setup(ALREADY_APPROVED_THRESHOLD - 1)
        result = _check_and_approve(contract, account, SPENDER_ADDR, w3, dry_run=False)
        w3.eth.send_raw_transaction.assert_called_once()
        assert result is not None


# ─── approve() top-level ─────────────────────────────────────────────────────

class TestApprove:
    """
    Tests for the public approve() function.

    We patch ``approve_usdc._create_w3_and_account`` so no real web3
    import is needed (web3 may not be installed in the test environment).
    The patch returns pre-built mock objects that let us control allowance
    values and verify which calls are made.
    """

    DUMMY_KEY = "0x" + "a" * 64
    DUMMY_RPC = "https://polygon-rpc.com"

    def _make_factory(self, allowance: int = 0, connected: bool = True):
        """
        Return (mock_w3, mock_account, mock_contract, patch_target) so
        tests can patch _create_w3_and_account with a factory that injects
        these mocks into approve().
        """
        mock_w3      = _mock_w3()
        mock_w3.is_connected.return_value = connected

        mock_contract = MagicMock()
        mock_contract.functions.allowance.return_value.call.return_value = allowance

        signed = MagicMock()
        signed.rawTransaction = b"\xee" * 32
        mock_account = _mock_account()
        mock_account.sign_transaction.return_value = signed

        build_tx = MagicMock(return_value={})
        mock_contract.functions.approve.return_value.build_transaction = build_tx

        raw_hash = b"\xff" * 32
        receipt  = MagicMock()
        receipt.status  = 1
        receipt.gasUsed = 50_000
        mock_w3.eth.send_raw_transaction.return_value = raw_hash
        mock_w3.eth.wait_for_transaction_receipt.return_value = receipt

        def factory(rpc_url, private_key, usdc_address, spender):
            return mock_w3, mock_account, mock_contract, spender

        return factory, mock_w3, mock_account

    def test_raises_connection_error_when_rpc_unreachable(self):
        factory, mock_w3, _ = self._make_factory(connected=False)
        with patch("approve_usdc._create_w3_and_account", side_effect=factory):
            with pytest.raises(ConnectionError):
                approve(self.DUMMY_KEY, self.DUMMY_RPC)

    def test_returns_none_when_already_approved(self):
        factory, mock_w3, _ = self._make_factory(allowance=MAX_UINT256)
        with patch("approve_usdc._create_w3_and_account", side_effect=factory):
            result = approve(self.DUMMY_KEY, self.DUMMY_RPC)
        assert result is None
        mock_w3.eth.send_raw_transaction.assert_not_called()

    def test_returns_tx_hash_when_approval_needed(self):
        factory, mock_w3, _ = self._make_factory(allowance=0)
        with patch("approve_usdc._create_w3_and_account", side_effect=factory):
            result = approve(self.DUMMY_KEY, self.DUMMY_RPC)
        assert result is not None
        mock_w3.eth.send_raw_transaction.assert_called_once()

    def test_dry_run_never_sends_tx(self):
        factory, mock_w3, _ = self._make_factory(allowance=0)
        with patch("approve_usdc._create_w3_and_account", side_effect=factory):
            result = approve(self.DUMMY_KEY, self.DUMMY_RPC, dry_run=True)
        assert result is None
        mock_w3.eth.send_raw_transaction.assert_not_called()
