"""
approve_usdc.py – One-time USDC approval for the Polymarket CTF Exchange.

The Polymarket CLOB exchange pulls USDC from your wallet via the standard
ERC-20 allowance mechanism.  Run this script once before going live; the
bot will then be able to place orders without further manual steps.

If the allowance is already >= 2^128 the script exits immediately without
broadcasting any transaction.

Usage:
    python approve_usdc.py               # send the approval on-chain
    python approve_usdc.py --dry-run     # check allowance, build tx, but do NOT send

Required env vars (.env):
    PRIVATE_KEY       – 0x-prefixed hex private key of your Polygon wallet
    POLYGON_RPC_URL   – Polygon mainnet JSON-RPC (e.g. https://polygon-rpc.com
                        or a private Alchemy/Infura endpoint)

Optional env vars:
    USDC_ADDRESS      – Override the USDC contract address.
                        Default: 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
                        (USDC.e – Bridged USDC on Polygon, the one Polymarket uses)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Optional

from api_client import CHAIN_ID, CLOB_EXCHANGE, USDC_ADDRESS

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

MAX_UINT256: int = 2 ** 256 - 1

# If the existing allowance is already this large we skip the transaction.
# Using 2^128 as the threshold means we re-approve only when allowance
# drops below a practically inexhaustible amount.
ALREADY_APPROVED_THRESHOLD: int = 2 ** 128

# Minimal ERC-20 ABI (allowance + approve only)
_ERC20_ABI = [
    {
        "inputs": [
            {"name": "owner",   "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount",  "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


# ─── Pure helpers (easy to unit-test) ────────────────────────────────────────


def _is_already_approved(allowance: int, threshold: int = ALREADY_APPROVED_THRESHOLD) -> bool:
    """Return True if the existing allowance is sufficient (>= threshold)."""
    return allowance >= threshold


def _get_allowance(contract, owner: str, spender: str) -> int:
    """Query the ERC-20 allowance.  Thin wrapper so tests can mock it."""
    return contract.functions.allowance(owner, spender).call()


def _send_approval(contract, account, spender: str, w3) -> str:
    """
    Build, sign, and broadcast an approve(spender, MAX_UINT256) transaction.
    Returns the hex transaction hash.
    Raises RuntimeError if the on-chain receipt reports failure.
    """
    nonce = w3.eth.get_transaction_count(account.address)
    tx = contract.functions.approve(spender, MAX_UINT256).build_transaction(
        {
            "from":     account.address,
            "nonce":    nonce,
            "gas":      100_000,
            "gasPrice": w3.eth.gas_price,
            "chainId":  CHAIN_ID,
        }
    )
    signed  = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt.status != 1:
        raise RuntimeError(
            f"Approval transaction reverted: {tx_hash.hex()}"
        )

    logger.info(
        "Approved!  tx=%s  gas_used=%d",
        tx_hash.hex(), receipt.gasUsed,
    )
    return tx_hash.hex()


# ─── Main entry point ─────────────────────────────────────────────────────────


def _create_w3_and_account(rpc_url: str, private_key: str, usdc_address: str, spender: str):
    """
    Construct the Web3 instance, account, and USDC contract.

    Isolated into its own function so tests can patch
    ``approve_usdc._create_w3_and_account`` without needing web3 installed.

    Returns:
        (w3, account, usdc_contract, checksum_spender)
    """
    from web3 import Web3
    from eth_account import Account

    w3       = Web3(Web3.HTTPProvider(rpc_url))
    account  = Account.from_key(private_key)
    checksum = Web3.to_checksum_address(usdc_address)
    usdc     = w3.eth.contract(address=checksum, abi=_ERC20_ABI)
    spender  = Web3.to_checksum_address(spender)
    return w3, account, usdc, spender


def approve(
    private_key: str,
    rpc_url: str,
    *,
    usdc_address: str = USDC_ADDRESS,
    spender: str = CLOB_EXCHANGE,
    dry_run: bool = False,
) -> Optional[str]:
    """
    Ensure the CLOB Exchange has max USDC allowance from the given wallet.

    Args:
        private_key:   Hex private key (with or without 0x prefix).
        rpc_url:       Polygon mainnet JSON-RPC URL.
        usdc_address:  USDC contract address on Polygon.
        spender:       Contract to approve (default: Polymarket CLOB Exchange).
        dry_run:       If True, log what would happen but send no transaction.

    Returns:
        Hex transaction hash if a new approval was broadcast, None otherwise.

    Raises:
        ConnectionError: If the RPC endpoint is unreachable.
        RuntimeError:    If the approval transaction reverts on-chain.
    """
    w3, account, usdc, spender = _create_w3_and_account(
        rpc_url, private_key, usdc_address, spender
    )
    if not w3.is_connected():
        raise ConnectionError(f"Cannot connect to Polygon RPC: {rpc_url}")

    return _check_and_approve(usdc, account, spender, w3, dry_run=dry_run)


def _check_and_approve(usdc_contract, account, spender: str, w3, *, dry_run: bool) -> Optional[str]:
    """
    Core approval logic — separated so tests can inject mocks directly
    without constructing a real Web3 instance.
    """
    allowance = _get_allowance(usdc_contract, account.address, spender)

    if _is_already_approved(allowance):
        logger.info(
            "Wallet %s already has sufficient USDC allowance (%d ≥ threshold). "
            "No action needed.",
            account.address, allowance,
        )
        return None

    logger.info(
        "Current allowance for %s: %d (below threshold %d).",
        account.address, allowance, ALREADY_APPROVED_THRESHOLD,
    )

    if dry_run:
        logger.info("[DRY RUN] Would approve %s to spend MAX_UINT256 USDC. "
                    "No transaction sent.", spender)
        return None

    logger.info("Sending approve(spender=%s, amount=MAX_UINT256)…", spender)
    return _send_approval(usdc_contract, account, spender, w3)


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    from dotenv import load_dotenv  # only needed for the CLI; not imported at module level

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    p = argparse.ArgumentParser(
        description="Approve the Polymarket CLOB Exchange to spend USDC from your wallet."
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Check allowance and build the tx without broadcasting it.",
    )
    args = p.parse_args()

    load_dotenv()

    private_key = os.getenv("PRIVATE_KEY", "")
    rpc_url     = os.getenv("POLYGON_RPC_URL", "")
    usdc_addr   = os.getenv("USDC_ADDRESS", USDC_ADDRESS)

    if not private_key:
        logger.error("PRIVATE_KEY is not set in .env")
        sys.exit(1)
    if not rpc_url:
        logger.error("POLYGON_RPC_URL is not set in .env")
        sys.exit(1)

    try:
        tx_hash = approve(
            private_key=private_key,
            rpc_url=rpc_url,
            usdc_address=usdc_addr,
            dry_run=args.dry_run,
        )
    except (ConnectionError, RuntimeError) as exc:
        logger.error("%s", exc)
        sys.exit(1)

    if tx_hash:
        print(f"\nApproval sent: {tx_hash}")
        print("You can now run the bot with: python bot.py\n")
    elif not args.dry_run:
        print("\nAllowance already sufficient – nothing to do.\n")


if __name__ == "__main__":
    main()
