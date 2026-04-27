"""
go_live.py – Interactive go-live checklist wizard.

Walks through every requirement before switching to live trading and,
at the end, offers to patch SIMULATION_MODE=false in your .env.

Usage:
    python go_live.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# ─── Constants ────────────────────────────────────────────────────────────────

ENV_FILE = Path(".env")

# Values that mean the user hasn't filled in .env yet
_PLACEHOLDER_PATTERNS = [
    r"your_.*",
    r"^0x0{64}$",           # all-zero private key
    r"^$",                   # empty (checked separately per field)
]

_REQUIRED_VARS = [
    "PRIVATE_KEY",
    "POLYGON_RPC_URL",
    "POLY_API_KEY",
    "POLY_API_SECRET",
    "POLY_API_PASSPHRASE",
]

_WIDTH = 40   # label column width

# ─── Pure validation helpers (fully testable without I/O) ────────────────────


def env_file_exists(path: Path = ENV_FILE) -> bool:
    return path.is_file()


def load_env_values(path: Path = ENV_FILE) -> dict:
    """
    Parse a .env file into a dict of key→value pairs.
    Ignores comment lines and blank lines.
    """
    values: dict = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip()
    except OSError:
        pass
    return values


def find_missing_vars(values: dict, required: List[str] = _REQUIRED_VARS) -> List[str]:
    """Return the names of required variables that are absent or empty."""
    return [v for v in required if not values.get(v, "").strip()]


def find_placeholder_vars(values: dict) -> List[Tuple[str, str]]:
    """
    Return (key, value) pairs where the value still looks like a placeholder.
    E.g. POLY_API_KEY=your_polymarket_api_key
    """
    suspects = []
    for key, val in values.items():
        if re.fullmatch(r"your_.*", val, re.IGNORECASE):
            suspects.append((key, val))
        elif re.fullmatch(r"0x0{64}", val):
            suspects.append((key, val))
    return suspects


def is_valid_private_key(key: str) -> bool:
    """
    Return True if key looks like a valid 32-byte hex private key.
    Accepts with or without leading 0x.
    """
    stripped = key.lower().lstrip("0x")
    return bool(re.fullmatch(r"[0-9a-f]{64}", stripped)) and int(stripped, 16) != 0


def is_simulation_mode(values: dict) -> bool:
    return values.get("SIMULATION_MODE", "true").lower() != "false"


def patch_simulation_mode(path: Path = ENV_FILE) -> bool:
    """
    Replace SIMULATION_MODE=true with SIMULATION_MODE=false in *path*.
    Returns True if the file was modified, False if the line wasn't found
    (caller should append it in that case).
    """
    if not path.is_file():
        return False

    text = path.read_text()
    new_text, count = re.subn(
        r"(?m)^SIMULATION_MODE\s*=\s*true\s*$",
        "SIMULATION_MODE=false",
        text,
        flags=re.IGNORECASE,
    )
    if count == 0:
        # Line not present – append it
        new_text = text.rstrip("\n") + "\nSIMULATION_MODE=false\n"

    path.write_text(new_text)
    return True


# ─── Output helpers ───────────────────────────────────────────────────────────

def _header(text: str) -> None:
    print(f"\n  {text}")
    print("  " + "─" * 56)


def _step(n: int, total: int, label: str) -> None:
    print(f"\n  Step {n}/{total}  {label}")


def _ok(label: str, detail: str = "") -> None:
    print(f"    [PASS]  {label:<{_WIDTH}}  {detail}")


def _fail(label: str, detail: str = "") -> None:
    print(f"    [FAIL]  {label:<{_WIDTH}}  {detail}")


def _info(text: str) -> None:
    print(f"    {text}")


def _ask(prompt: str) -> str:
    try:
        return input(f"\n  {prompt} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


# ─── Individual check steps ───────────────────────────────────────────────────


def step_env_file() -> bool:
    if not env_file_exists():
        _fail(".env file", "not found — run: cp .env.example .env")
        return False
    _ok(".env file", "found")
    return True


def step_placeholders(values: dict) -> bool:
    suspects = find_placeholder_vars(values)
    if suspects:
        _fail("Placeholder values", f"{len(suspects)} var(s) still have placeholder text")
        for k, v in suspects:
            _info(f"  {k} = {v}")
        return False
    _ok("Placeholder values", "none detected")
    return True


def step_required_vars(values: dict) -> bool:
    missing = find_missing_vars(values)
    if missing:
        _fail("Required variables", f"missing: {', '.join(missing)}")
        return False
    _ok("Required variables", "all set")
    return True


def step_private_key(values: dict) -> bool:
    pk = values.get("PRIVATE_KEY", "")
    if not is_valid_private_key(pk):
        _fail("PRIVATE_KEY format", "must be a 64-hex-char Ethereum private key")
        return False
    _ok("PRIVATE_KEY format", "valid 32-byte key")
    return True


def step_health_check(skip_ws: bool = False) -> bool:
    """Run the health_check module inline and return True if all pass."""
    try:
        from health_check import run_checks
        return run_checks(skip_ws=skip_ws)
    except Exception as exc:
        _fail("Health check", f"unexpected error: {exc}")
        return False


def step_usdc_allowance(values: dict) -> bool:
    """
    Check USDC allowance using approve_usdc dry-run logic.
    Skips gracefully if web3 is not installed.
    """
    pk      = values.get("PRIVATE_KEY", "")
    rpc_url = values.get("POLYGON_RPC_URL", "")

    if not pk or not rpc_url:
        _fail("USDC allowance", "PRIVATE_KEY or POLYGON_RPC_URL missing — skipped")
        return False

    try:
        from approve_usdc import ALREADY_APPROVED_THRESHOLD, _create_w3_and_account, _get_allowance
        from api_client import CLOB_EXCHANGE, USDC_ADDRESS

        w3, account, usdc, spender = _create_w3_and_account(
            rpc_url, pk, USDC_ADDRESS, CLOB_EXCHANGE
        )
        if not w3.is_connected():
            _fail("USDC allowance", f"cannot connect to {rpc_url}")
            return False

        allowance = _get_allowance(usdc, account.address, spender)
        if allowance >= ALREADY_APPROVED_THRESHOLD:
            _ok("USDC allowance", f"approved (allowance ≥ 2^128)")
            return True

        _fail(
            "USDC allowance",
            f"only {allowance} — run: python approve_usdc.py",
        )
        return False

    except ImportError:
        _info("web3 not installed in this environment — skipping allowance check")
        _info("Run manually:  python approve_usdc.py --dry-run")
        return True   # non-fatal; user must do this themselves


# ─── Main wizard ─────────────────────────────────────────────────────────────


def run_wizard(skip_ws: bool = False) -> None:
    TOTAL = 6

    _header("Hyper-BTC Go-Live Checklist")

    # ── Step 1: .env exists ──────────────────────────────────────────────────
    _step(1, TOTAL, "Checking .env file…")
    if not step_env_file():
        _abort()

    values = load_env_values()

    # ── Step 2: no placeholder values ────────────────────────────────────────
    _step(2, TOTAL, "Checking for placeholder values…")
    placeholder_ok = step_placeholders(values)

    # ── Step 3: required vars + key format ───────────────────────────────────
    _step(3, TOTAL, "Validating credentials…")
    vars_ok = step_required_vars(values)
    key_ok  = step_private_key(values) if vars_ok else False

    if not (placeholder_ok and vars_ok and key_ok):
        _abort()

    # ── Step 4: health check ─────────────────────────────────────────────────
    _step(4, TOTAL, "Running health check…")
    health_ok = step_health_check(skip_ws=skip_ws)
    if not health_ok:
        _abort()

    # ── Step 5: USDC allowance ───────────────────────────────────────────────
    _step(5, TOTAL, "Checking USDC allowance on Polygon…")
    allowance_ok = step_usdc_allowance(values)
    if not allowance_ok:
        _abort()

    # ── Step 6: human confirmation ───────────────────────────────────────────
    _step(6, TOTAL, "Confirmation")

    currently_sim = is_simulation_mode(values)
    if currently_sim:
        _info("SIMULATION_MODE is currently: true (paper trading)")
    else:
        _info("SIMULATION_MODE is already: false — .env is already set for live trading.")

    print()
    print("  " + "─" * 56)
    print("  All checks passed.")
    print()
    print("  IMPORTANT: live trading will spend REAL USDC from your wallet.")
    print("  Make sure you have tested in simulation mode first.")
    print()

    answer = _ask("Have you paper-traded and reviewed the results? [yes/no]:")
    if answer != "yes":
        print("\n  Aborted. Run in simulation mode first: python bot.py")
        sys.exit(0)

    if currently_sim:
        answer2 = _ask("Flip SIMULATION_MODE=false in .env now? [yes/no]:")
        if answer2 == "yes":
            patch_simulation_mode()
            print("\n  Done — .env updated. SIMULATION_MODE=false")
        else:
            print("\n  .env not changed. Edit it manually when you're ready.")

    print()
    print("  To start live trading, run:")
    print("      python bot.py --live")
    print()


def _abort() -> None:
    print("\n  Fix the issues above, then re-run:  python go_live.py")
    sys.exit(1)


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(
        description="Interactive go-live checklist for the Hyper-BTC bot."
    )
    p.add_argument(
        "--no-ws",
        action="store_true",
        help="Skip Pyth WebSocket check in the health check step.",
    )
    args = p.parse_args()
    run_wizard(skip_ws=args.no_ws)


if __name__ == "__main__":
    main()
