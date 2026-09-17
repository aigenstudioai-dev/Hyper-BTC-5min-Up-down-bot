"""
approve_usdc.py – Collateral setup for the Polymarket CTF Exchange (CLOB V2).

CLOB V2 settles trades in pUSD, not USDC.e directly (see issue #7/#9).
Getting a wallet ready to trade is three steps:

  1. Approve the Collateral Onramp to pull USDC.e (needed before wrapping).
  2. Wrap USDC.e into pUSD via the Onramp's wrap() function — moves real
     funds, so this only happens when you explicitly pass --wrap AMOUNT.
  3. Approve the CTF Exchange to pull pUSD (the allowance that actually
     matters for V2 order settlement).

Running with no flags handles the two *approval* legs only (steps 1 and 3)
— ordinary, reversible ERC-20 approvals. Nothing ever gets wrapped without
an explicit --wrap AMOUNT.

If an allowance is already >= 2^128 that leg is skipped without
broadcasting a transaction.

Usage:
    python approve_usdc.py                    # approve both legs
    python approve_usdc.py --dry-run          # check allowances, build txs, send nothing
    python approve_usdc.py --wrap 300         # also wrap 300 USDC.e into pUSD
    python approve_usdc.py --wrap 300 --dry-run

Required env vars (.env):
    PRIVATE_KEY       – 0x-prefixed hex private key of your Polygon wallet
    POLYGON_RPC_URL   – Polygon mainnet JSON-RPC (e.g. https://polygon-rpc.com
                        or a private Alchemy/Infura endpoint)

Optional env vars:
    USDC_ADDRESS      – Override the USDC.e contract address.
                        Default: 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
    PUSD_ADDRESS      – Override the pUSD contract address.
    COLLATERAL_ONRAMP_ADDRESS – Override the Collateral Onramp address.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Optional

from api_client import (
    CHAIN_ID,
    CLOB_EXCHANGE,
    COLLATERAL_ONRAMP_ADDRESS,
    PUSD_ADDRESS,
    USDC_ADDRESS,
)

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

MAX_UINT256: int = 2 ** 256 - 1

# If the existing allowance is already this large we skip the transaction.
# Using 2^128 as the threshold means we re-approve only when allowance
# drops below a practically inexhaustible amount.
ALREADY_APPROVED_THRESHOLD: int = 2 ** 128

# Minimal ERC-20 ABI (allowance + approve + balanceOf)
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
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# Collateral Onramp ABI (wrap() only). Signature confirmed via Polymarket's
# own py-sdk GitHub repo (issue #261: "wrap(address,address,uint256)"),
# corroborated by an independently-generated ABI binding — see issue #9.
_ONRAMP_ABI = [
    {
        "inputs": [
            {"name": "asset",  "type": "address"},
            {"name": "to",     "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "wrap",
        "outputs": [],
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


def _send_wrap(onramp_contract, account, asset: str, to: str, amount: int, w3) -> str:
    """
    Build, sign, and broadcast a wrap(asset, to, amount) transaction on the
    Collateral Onramp — moves real USDC.e into pUSD.
    Returns the hex transaction hash.
    Raises RuntimeError if the on-chain receipt reports failure.
    """
    nonce = w3.eth.get_transaction_count(account.address)
    tx = onramp_contract.functions.wrap(asset, to, amount).build_transaction(
        {
            "from":     account.address,
            "nonce":    nonce,
            "gas":      200_000,
            "gasPrice": w3.eth.gas_price,
            "chainId":  CHAIN_ID,
        }
    )
    signed  = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt.status != 1:
        raise RuntimeError(
            f"Wrap transaction reverted: {tx_hash.hex()}"
        )

    logger.info(
        "Wrapped!  tx=%s  gas_used=%d",
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


def _create_w3_account_and_contract(rpc_url: str, private_key: str, address: str, abi: list):
    """
    Like ``_create_w3_and_account`` but for an arbitrary (address, ABI) pair
    rather than the hardcoded ERC-20 ABI — used for the Collateral Onramp.

    Isolated so tests can patch ``approve_usdc._create_w3_account_and_contract``
    without needing web3 installed.

    Returns:
        (w3, account, contract)
    """
    from web3 import Web3
    from eth_account import Account

    w3       = Web3(Web3.HTTPProvider(rpc_url))
    account  = Account.from_key(private_key)
    checksum = Web3.to_checksum_address(address)
    contract = w3.eth.contract(address=checksum, abi=abi)
    return w3, account, contract


def approve(
    private_key: str,
    rpc_url: str,
    *,
    usdc_address: str = USDC_ADDRESS,
    spender: str = CLOB_EXCHANGE,
    dry_run: bool = False,
) -> Optional[str]:
    """
    Ensure ``spender`` has max allowance to spend ``usdc_address`` from the
    given wallet. Despite the parameter names (kept for backwards
    compatibility), this works for any ERC-20 token — issue #9 reuses it
    for both collateral-setup legs under CLOB V2:
      - usdc_address=USDC_ADDRESS, spender=COLLATERAL_ONRAMP_ADDRESS
        (needed before wrap_usdc_to_pusd can pull funds)
      - usdc_address=PUSD_ADDRESS, spender=CLOB_EXCHANGE
        (the allowance that actually matters for V2 order settlement)

    Args:
        private_key:   Hex private key (with or without 0x prefix).
        rpc_url:       Polygon mainnet JSON-RPC URL.
        usdc_address:  ERC-20 token contract address on Polygon.
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


def wrap_usdc_to_pusd(
    private_key: str,
    rpc_url: str,
    amount_usdc: float,
    *,
    usdc_address: str = USDC_ADDRESS,
    onramp_address: str = COLLATERAL_ONRAMP_ADDRESS,
    dry_run: bool = False,
) -> Optional[str]:
    """
    Wrap ``amount_usdc`` of USDC.e into pUSD via the Collateral Onramp's
    wrap(asset, to, amount) function (issue #9).

    This moves real funds and is never automatic elsewhere in this
    codebase — it only runs when the caller passes an explicit,
    positive amount.

    Args:
        private_key:    Hex private key (with or without 0x prefix).
        rpc_url:        Polygon mainnet JSON-RPC URL.
        amount_usdc:    Human-readable USDC.e amount to wrap (e.g. 300.0).
                         Must be > 0. Scaled by 1e6 (6 decimals, same as
                         USDC.e/pUSD).
        usdc_address:   USDC.e contract address.
        onramp_address: Collateral Onramp contract address.
        dry_run:        If True, log what would happen but send no transaction.

    Returns:
        Hex transaction hash if a wrap was broadcast, None if dry_run.

    Raises:
        ValueError:      If amount_usdc is not positive.
        ConnectionError: If the RPC endpoint is unreachable.
        RuntimeError:    If the wrap transaction reverts on-chain.
    """
    if amount_usdc <= 0:
        raise ValueError(f"amount_usdc must be positive, got {amount_usdc}")

    w3, account, onramp = _create_w3_account_and_contract(
        rpc_url, private_key, onramp_address, _ONRAMP_ABI
    )
    if not w3.is_connected():
        raise ConnectionError(f"Cannot connect to Polygon RPC: {rpc_url}")

    amount = int(amount_usdc * 1e6)

    if dry_run:
        logger.info(
            "[DRY RUN] Would wrap %.2f USDC.e (%d units) into pUSD via %s. "
            "No transaction sent.",
            amount_usdc, amount, onramp_address,
        )
        return None

    logger.info(
        "Sending wrap(asset=%s, to=%s, amount=%d)…",
        usdc_address, account.address, amount,
    )
    return _send_wrap(onramp, account, usdc_address, account.address, amount, w3)


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    from dotenv import load_dotenv  # only needed for the CLI; not imported at module level

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    p = argparse.ArgumentParser(
        description="Set up collateral for the Polymarket CLOB Exchange (CLOB V2 / pUSD)."
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Check allowances/balances and build txs without broadcasting anything.",
    )
    p.add_argument(
        "--wrap",
        type=float,
        default=None,
        metavar="AMOUNT",
        help="Also wrap AMOUNT USDC.e into pUSD via the Collateral Onramp. "
             "Moves real funds — only runs when you pass this explicitly.",
    )
    args = p.parse_args()

    load_dotenv()

    private_key = os.getenv("PRIVATE_KEY", "")
    rpc_url     = os.getenv("POLYGON_RPC_URL", "")
    usdc_addr   = os.getenv("USDC_ADDRESS", USDC_ADDRESS)
    pusd_addr   = os.getenv("PUSD_ADDRESS", PUSD_ADDRESS)
    onramp_addr = os.getenv("COLLATERAL_ONRAMP_ADDRESS", COLLATERAL_ONRAMP_ADDRESS)

    if not private_key:
        logger.error("PRIVATE_KEY is not set in .env")
        sys.exit(1)
    if not rpc_url:
        logger.error("POLYGON_RPC_URL is not set in .env")
        sys.exit(1)

    try:
        # Leg 1: approve the Onramp to pull USDC.e (needed before wrapping).
        onramp_tx = approve(
            private_key=private_key,
            rpc_url=rpc_url,
            usdc_address=usdc_addr,
            spender=onramp_addr,
            dry_run=args.dry_run,
        )
        if onramp_tx:
            print(f"\nApproved Collateral Onramp for USDC.e: {onramp_tx}")
        elif not args.dry_run:
            print("\nCollateral Onramp USDC.e allowance already sufficient.")

        # Leg 2 (optional): wrap USDC.e into pUSD.
        if args.wrap is not None:
            wrap_tx = wrap_usdc_to_pusd(
                private_key=private_key,
                rpc_url=rpc_url,
                amount_usdc=args.wrap,
                usdc_address=usdc_addr,
                onramp_address=onramp_addr,
                dry_run=args.dry_run,
            )
            if wrap_tx:
                print(f"Wrapped {args.wrap} USDC.e into pUSD: {wrap_tx}")

        # Leg 3: approve the Exchange to pull pUSD — the allowance that
        # actually matters for V2 order settlement.
        exchange_tx = approve(
            private_key=private_key,
            rpc_url=rpc_url,
            usdc_address=pusd_addr,
            spender=CLOB_EXCHANGE,
            dry_run=args.dry_run,
        )
        if exchange_tx:
            print(f"Approved CTF Exchange for pUSD: {exchange_tx}")
        elif not args.dry_run:
            print("CTF Exchange pUSD allowance already sufficient.")

    except (ConnectionError, RuntimeError, ValueError) as exc:
        logger.error("%s", exc)
        sys.exit(1)

    if not args.dry_run:
        print("\nYou can now run the bot with: python bot.py\n")


if __name__ == "__main__":
    main()
