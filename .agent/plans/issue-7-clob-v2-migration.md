# Plan — Issue #7: migrate order signing from CLOB V1 to V2

Issue: https://github.com/aigenstudioai-dev/Hyper-BTC-5min-Up-down-bot/issues/7
Branch: claude/hyper-btc-ados-delivery-0dmo9k

## Source of truth

The user fetched and pasted the full content of docs.polymarket.com/v2-migration
(this sandbox's network egress blocks that domain directly — 403,
organization policy — so this is the authoritative source for this change,
not a secondary/community source like the rest of issue #7's initial
findings). Cross-checked: the V2 exchange address it gives
(`0xE111180000d2663C0091e4f400237545B87B996B`) matches what was
independently confirmed earlier against Polymarket's own `ctf-exchange-v2`
GitHub repo README.

## Scope of this pass

Fix ONLY what's needed for `sign_order`/`place_limit_order` to produce a
V2-valid signed order and wire payload. Explicitly OUT of scope (filed
separately as #9): pUSD collateral wrapping — that's a new capability
(calling `wrap()` on the Collateral Onramp), not a fix to existing broken
signing logic, and this bot doesn't touch NegRisk markets (BTC 5-min
Up/Down is a plain two-outcome market) so the NegRisk exchange address is
irrelevant here.

## Confirmed changes (from the pasted docs, verbatim)

1. **EIP-712 domain**: `version` "1" → "2"; `verifyingContract` old V1
   address → `0xE111180000d2663C0091e4f400237545B87B996B`.
2. **EIP-712 Order type** (signed struct) — drops `taker`, `expiration`,
   `nonce`, `feeRateBps`; adds `timestamp` (uint256, ms), `metadata`
   (bytes32), `builder` (bytes32). Final field order per the docs:
   `salt, maker, signer, tokenId, makerAmount, takerAmount, side,
   signatureType, timestamp, metadata, builder` (11 fields).
3. **`nonce` is gone entirely** — supersedes #6, which is being closed as
   moot once this lands. `timestamp` (ms) is the new uniqueness source,
   not something this bot needs to manage — always "now".
4. **`side` in the signed struct stays a uint8** (0=BUY/1=SELL) — "No
   change from V1." Only the wire body's `order.side` is the string
   `"BUY"`/`"SELL"`.
5. **POST /order wire body**: drops `taker`/`nonce`/`feeRateBps` (same as
   the signed struct), but **keeps `expiration`** (for GTD wire-level
   handling — NOT part of the EIP-712 signed struct), adds
   `timestamp`/`metadata`/`builder`, and the payload gains two new
   top-level fields not previously sent by this repo at all: `owner`
   (the API key) and `postOnly` (bool). `orderType` in the docs' example
   is `"GTC"`, not this repo's existing `"LIMIT"` literal.
6. **ClobAuthDomain (L1 API-key derivation) is unchanged** — stays version
   "1", "L1/L2 auth is identical in V2". `l1_headers`/`l2_headers`/
   `derive_api_credentials` need no change.
7. Large numeric wire fields (`salt`, `tokenId`, `makerAmount`,
   `takerAmount`, `timestamp`) are shown as **JSON strings** in the docs'
   wire example, not numbers — sensible for uint256-range values that
   would lose precision in many JSON number parsers. Adopting this for
   the wire payload only; the values passed into `sign_order`/EIP-712
   hashing stay native Python ints (that's a Solidity-type concern, not a
   JSON-transport concern, and eth_account expects real ints there).

## What's still uncertain (flagged, not blocking this narrower fix)

- Whether `"GTC"` is actually the right `orderType` for a bot-managed
  limit order with a real expiration (vs. `"GTD"`), since the docs only
  show one representative example rather than specifying every enum's
  semantics. Using the docs' literal example value (`"GTC"`) rather than
  guessing an alternative; flagging for follow-up verification against
  Polymarket's API reference before any live order.
- Whether large numeric wire fields *must* be stringified or whether
  that's just documentation styling. Adopting stringification since it's
  what the primary source shows and is the safer choice for uint256-range
  values over JSON.

## Implementation

- `api_client.py`: `CLOB_EXCHANGE` → V2 address (with a comment on the
  NegRisk address existing but being unused). `sign_order`'s domain
  version and `types["Order"]` list updated to the 11-field V2 schema.
  `place_limit_order`'s live branch: build a `signed_order` dict with
  exactly the 11 EIP-712 fields (correct Solidity-typed Python values) for
  signing, then a separate `wire_order` dict for the POST body (side as
  string, expiration added back as a string, large numerics stringified,
  signature attached), wrapped in a payload carrying `owner`, `orderType`,
  `postOnly`.

## TDD plan

- `tests/test_polymarket_auth.py::TestSignOrder`: update the fixture
  `ORDER` dict to the new 11-field shape; add an assertion that the domain
  version used is `"2"`.
- `tests/test_polymarket_client_live.py::TestPlaceLimitOrderLive`: update
  `test_order_struct_fields` to assert the *signed* struct has exactly the
  11 V2 fields (no `taker`/`expiration`/`nonce`/`feeRateBps`) with correct
  values; update `test_post_payload_shape` to assert the wire body's
  `owner`, `orderType`, `postOnly`, stringified numerics, and that
  `expiration` is present in the wire body despite being absent from the
  signed struct.
- Write these first against the current (V1-shaped) code — expect RED —
  then implement, expect GREEN.

## Review

Independent review pass (fresh agent) before commit, given this is the
core wallet-signing/order-placement change — no self-review, per policy.
No live order will be placed as part of verifying this.
