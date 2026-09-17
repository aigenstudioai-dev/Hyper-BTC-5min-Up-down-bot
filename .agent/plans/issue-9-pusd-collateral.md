# Plan — Issue #9: migrate collateral setup from USDC.e to pUSD

Issue: https://github.com/aigenstudioai-dev/Hyper-BTC-5min-Up-down-bot/issues/9
Branch: claude/hyper-btc-ados-delivery-0dmo9k

## Source verification

`docs.polymarket.com` and `polygonscan.com` are both blocked from this
sandbox (403, organization egress policy) — could not reach the primary
docs' `/concepts/pusd` or `/resources/contracts` pages, or the verified
contract source directly. Confirmed instead via independently-converging
GitHub-reachable sources:

- pUSD token address `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` and
  Collateral Onramp address `0x93070a847efEf7F70739046A929D47a521F5B8ee` —
  confirmed directly from Polymarket's own `ctf-exchange-v2` GitHub repo
  README "Deployed Contracts" table (primary/official source, same one
  used to confirm the V2 exchange address in #7).
- `wrap()` function signature `wrap(address asset, address to, uint256 amount)
  external` — confirmed by THREE independent sources agreeing exactly:
  (1) a generated Go ABI binding on pkg.go.dev (mechanically derived from
  a real ABI, not paraphrased), (2) a WebSearch-surfaced ABI JSON snippet,
  (3) **Polymarket's own official `py-sdk` GitHub repo, issue #261**,
  which states the literal function selector `wrap(address,address,uint256)`
  and confirms the caller must first approve the Onramp (not the pUSD
  token) to spend USDC.e. A fourth, less reliable community example repo
  suggested a different single-argument `wrap(amount)` signature, but that
  repo explicitly flagged its own Onramp address as "not public as of this
  writing" — i.e. it was written with placeholder/unconfirmed information
  and is discounted in favor of the three agreeing sources.
- Full flow (3 transactions), corroborated by the same community example
  repo (whose overall architecture description agrees even though its
  specific wrap() signature doesn't): (1) approve Collateral Onramp to
  spend USDC.e, (2) call `Onramp.wrap(asset=USDC_ADDRESS, to=wallet,
  amount)`, (3) approve CTF Exchange V2 to spend pUSD.

Confidence: high on addresses and overall flow (official primary source
for addresses, 3/4 independent sources for the wrap ABI). This is StILL
not the same as fetching the primary docs directly — flagging this
clearly in the PR/issue rather than overclaiming certainty.

## What's being fixed vs. added

**Existing bug, not just a gap**: `approve_usdc.py`'s current default
behavior — approving `CLOB_EXCHANGE` to spend `USDC_ADDRESS` (USDC.e)
directly — is not merely incomplete under V2, it's now **pointless**: the
V2 Exchange settles trades in pUSD, not USDC.e, so an EOA's USDC.e
allowance to the Exchange does nothing for a V2 trade. This needs fixing,
not just extending.

**New capability**: the wrap() call itself (moving USDC.e into pUSD) is
new logic with no prior equivalent in this codebase — real fund movement,
mandatory TDD + dry-run + review per the risky-logic charter.

## Design

`approve_usdc.py`'s existing `approve()`/`_check_and_approve()` are
already fully generic over (token address, spender address) — reused
as-is for both approval legs, no changes needed there:
- `approve(pk, rpc, usdc_address=USDC_ADDRESS, spender=COLLATERAL_ONRAMP_ADDRESS, ...)`
  — approve Onramp to pull USDC.e for wrapping.
- `approve(pk, rpc, usdc_address=PUSD_ADDRESS, spender=CLOB_EXCHANGE, ...)`
  — approve Exchange to pull pUSD for settlement. **This replaces the old
  default** (USDC_ADDRESS/CLOB_EXCHANGE), which is now wrong.

New function `wrap_usdc_to_pusd(private_key, rpc_url, amount_usdc, *,
usdc_address=USDC_ADDRESS, onramp_address=COLLATERAL_ONRAMP_ADDRESS,
dry_run=False)`: builds/signs/sends a `wrap(asset, to, amount)` call on
the Onramp, mirroring `_send_approval`'s tx-building pattern exactly.
`amount_usdc` (human units, e.g. `300.0`) is scaled by `1e6` (USDC.e/pUSD
both use 6 decimals, consistent with every other USDC scaling already in
this codebase — `bot.py`, `api_client.py`).

**Safety**: wrapping moves real funds and is irreversible without a
separate unwrap transaction, so it is never automatic. The CLI only wraps
when an explicit `--wrap AMOUNT` flag with a real amount is given; running
`python approve_usdc.py` with no flags only handles the two *approval*
legs (reversible, standard ERC-20 approve calls, same risk profile as the
script's existing behavior), never moves collateral.

`api_client.py`: add `PUSD_ADDRESS` and `COLLATERAL_ONRAMP_ADDRESS`
constants (env-overridable, matching the existing `USDC_ADDRESS` pattern).

`go_live.py`: `step_usdc_allowance` currently checks USDC.e allowance to
the Exchange — the wrong, now-meaningless check under V2. Update it to
check **pUSD** allowance to the Exchange (the check that actually matters
for whether the bot can trade), and add a pUSD balance check that warns
the user to run `--wrap` if it's zero.

**Explicitly left alone**: `PolymarketClient.get_usdc_balance()` calls the
CLOB backend's own `/balance` API endpoint, not a raw on-chain call — no
evidence from what's been confirmed that this endpoint's semantics changed
in V2 (the migration guide's own FAQ says "most message payloads are
unchanged"). Not touching it; flagging the assumption rather than silently
asserting it's fine.

## TDD plan

`tests/test_approve_usdc.py`: existing tests for `approve()` already cover
the generic (token, spender) reuse implicitly since nothing token-specific
changes — add tests confirming `approve()` works correctly when called
with `usdc_address=PUSD_ADDRESS`/`spender=CLOB_EXCHANGE`. New test class
`TestWrapUsdcToPusd` mirroring `TestSendApproval`/`TestApprove`'s existing
mock patterns: correct call args to `wrap()` (asset/to/amount), correct
amount scaling (1e6), dry-run never sends a tx, reverted receipt raises,
returns tx hash on success.

`tests/test_go_live.py`: update/add tests for the new pUSD-based
`step_usdc_allowance` (or renamed equivalent) — checks pUSD allowance not
USDC.e allowance now.

## Review

Independent review pass (fresh agent) before commit — this touches actual
fund movement (wrap), the highest-risk category. No live wrap/approve
will be executed as part of verification; everything is web3-mocked.
