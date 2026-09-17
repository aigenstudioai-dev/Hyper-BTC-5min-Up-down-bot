# Plan — Issue #4: unit test coverage for wallet signing / live order execution

Issue: https://github.com/aigenstudioai-dev/Hyper-BTC-5min-Up-down-bot/issues/4
Branch: claude/hyper-btc-ados-delivery-0dmo9k

## Risk classification

Wallet signing (`PolymarketAuth`) and order placement/cancellation
(`PolymarketClient` live branches). Highest risk category per charter.
This pass is coverage-only — no intentional behavior change.

## Critical finding during this work (see issue #5)

`PolymarketAuth.sign_order` calls `self.account.sign_typed_data(...)` where
`self.account` is a `LocalAccount`. In the pinned `eth-account==0.10.0`,
`LocalAccount` has no `sign_typed_data` method — it exists only as an
`Account` classmethod taking the raw private key. Every live order
placement crashes with `AttributeError`, caught by `place_limit_order`'s
`except Exception`, silently turning every live order into
`OrderResult(status="error")`. **Live trading cannot place a single order
as currently written.**

Per the charter's "never fix-forward on wallet signing without a human
checkpoint" rule, this is NOT fixed in this pass. It is filed as #5 with a
verified (but unapplied) candidate fix, and the dependent test is marked
`xfail(raises=AttributeError)` referencing #5 so the suite stays green
while still documenting the exact break and flipping to an unexpected-pass
signal the moment someone fixes it.

## Test plan

New file `tests/test_polymarket_auth.py` — auth layer, using a real
throwaway private key (`"0x" + "1"*64`) so signing logic runs for real;
no network involved at this layer.

- `l1_headers`: header keys present; `POLY_SIGNATURE` recovers (via
  `eth_account.messages.encode_defunct` + `Account.recover_message`) to
  `self.account.address` for the exact message signed
  (`f"polymarket{ts}"` using the timestamp echoed in `POLY_TIMESTAMP`).
- `l2_headers`: recompute the expected HMAC-SHA256 independently
  (`ts + method.upper() + path + body`, keyed by `api_secret`) and assert
  it matches `POLY-SIGNATURE` exactly, for GET/POST/DELETE and with/without
  a body.
- `sign_order`: `xfail(raises=AttributeError, reason="issue #5")`. Body
  still attempts the real call and the EIP-712 recovery round-trip, so it
  will auto-flip to a visible XPASS the moment #5 is fixed.

New file `tests/test_polymarket_client_live.py` — `PolymarketClient` live
branches (`simulation=False`), HTTP mocked via patching
`api_client.http_retry` (matches the existing `tests/test_utils.py` style
of mocking at the `requests.Session`/response boundary — here we go one
level up and patch `http_retry` itself since that's the single seam all of
`_get`/`_post`/`_delete`/`derive_api_credentials` funnel through).

- `place_limit_order` live branch: mock `client.auth.sign_order` (isolates
  order-construction/dispatch correctness from the known-broken signer;
  the signer itself is covered, and known-broken, in
  `test_polymarket_auth.py`). Assert: `salt` in `[0, 2**256)`, `maker`/
  `signer` == auth address, `taker` == zero address, `tokenId` ==
  `int(token_id)`, `makerAmount` == `int(size_usdc * 1e6)`, `takerAmount`
  == `int(size_usdc / price * 1e6)`, `expiration` ≈ `now + expiry_seconds`,
  `nonce == 0`, `feeRateBps == 0`, `side` 0 for BUY / 1 for SELL,
  `signatureType == 0`. POST payload contains `order`, `signature`,
  `orderType: "LIMIT"`. Success → `OrderResult(status="live", order_id=...)`
  parsed from `orderID`/`order_id`. `http_retry` raising → caught,
  `OrderResult(status="error", error=...)`.
- `cancel_order` live branch: `http_retry` success → `True`. Raising →
  caught, returns `False` (never propagates).
- `get_order_status` live branch: response parsed
  (`sizeFilled`/`sizeRemaining` ÷ 1e6). Raising → `OrderResult(status="error")`.
- `get_usdc_balance` live branch: parses `balance`. Raising → `0.0`.
- `derive_api_credentials`: uses L1 headers (auth_level 1, not 2), returns
  parsed dict.

## Explicitly out of scope

- Fixing #5 (needs human decision).
- The pre-existing `nonce: 0` question (separate, still open).
- Live/testnet execution of any kind.
