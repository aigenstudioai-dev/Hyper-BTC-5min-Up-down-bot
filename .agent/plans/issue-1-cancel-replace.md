# Plan — Issue #1: Fix broken cancel-replace order lifecycle

Issue: https://github.com/aigenstudioai-dev/Hyper-BTC-5min-Up-down-bot/issues/1
Branch: claude/hyper-btc-ados-delivery-0dmo9k

## Risk classification

Touches order placement/cancellation logic in `bot.py::_manage_open_orders`.
Does NOT touch wallet signing (`sign_order`) or position sizing (`KellySizer`)
directly, but does call `client.place_limit_order` / `client.cancel_order`.
=> Risky logic per charter. TDD mandatory. validate-adversarial required.
Independent review required before close. No live-money test in this change.

## Spec — what "done" means

1. **Timestamp source fix**: replace the `order_id.split("-")` age hack with a
   lookup of `OrderStore.get_order(order_id)["created_at"]`. This column is
   already populated (Unix seconds) at `insert_order` time for every order,
   SIM or live, so this removes the SIM-only assumption entirely.
   - If the row is missing from the store for some reason (defensive case),
     fall back to treating the order as *not yet stale* (age=0) rather than
     *immediately stale* — the previous behavior's failure mode (instant
     cancellation) is the worse of the two failure modes for live money.

2. **Actual replacement**: when `_manage_open_orders` cancels a stale order:
   - Compute `time_left = seconds_until_close(market.end_time)`.
   - Only replace if `time_left > self.cfg.order_timeout_seconds` (i.e.
     enough time remains for a replacement to plausibly fill before window
     close). Otherwise, let the window end with no position (safer than
     forcing a fill into the close).
   - Refresh the orderbook mid (already done via `_refresh_orderbook`).
   - Re-derive side/token from the original order's stored row
     (`token_id`, `side`, `size_usdc`) rather than re-running signal
     evaluation, since the original signal already fired for this window.
   - Place exactly one replacement per stale-cancel event (no unbounded
     replace loop): the new order becomes the new tracked open order and
     will itself be subject to the same staleness check on a later tick,
     naturally bounding total replacements by
     `time_left // order_timeout_seconds`.
   - Persist the new order via `order_store.insert_order` and add it to
     `self._open_orders`.
   - Do NOT reset `_signal_fired_this_window` — a replacement is not a new
     signal-driven entry, it's a continuation of the same position attempt.

3. **Dry-run verification**: this PR is validated in simulation mode only
   (existing `SIMULATION_MODE=true` default, `PolymarketClient.simulation`
   gate already routes `place_limit_order`/`cancel_order` to fake fills).
   No live order will be placed or tested as part of this change.

## TDD plan (RED → GREEN → REFACTOR)

New file `tests/test_bot.py`. Construct `HyperBTCBot` with a `BotConfig`
in simulation mode and monkeypatch/mock `self.client` and `self.order_store`
where needed to avoid real network/DB coupling; use `OrderStore(":memory:")`
for real DB behavior on the created_at lookup (matches existing test style
in test_db.py).

Tests (written first, expected to fail against current code):
1. `test_live_order_not_cancelled_before_timeout` — insert an order with
   `created_at = now`, call `_manage_open_orders` immediately; assert the
   order is NOT cancelled (current code cancels immediately for non-SIM IDs).
2. `test_order_cancelled_after_timeout_and_replaced` — insert an order with
   `created_at = now - (order_timeout_seconds + 1)`, plenty of `time_left`
   on the market; assert original is cancelled AND exactly one new order
   appears in `_open_orders` / `order_store` at the refreshed mid price.
3. `test_no_replace_when_insufficient_time_left` — same staleness, but
   market `time_left <= order_timeout_seconds`; assert cancel happens but
   no replacement order is created.
4. `test_replacement_is_singular_per_stale_event` — after one replace,
   verify `_open_orders` contains exactly one order for that lifecycle
   step (no duplicate/loop).
5. `test_missing_db_row_defensive_fallback` — order_id present in
   `_open_orders` but absent from `OrderStore` (edge case); assert it is
   NOT treated as immediately stale.

Run RED (all new tests fail against unmodified `bot.py`), then implement
the fix in `bot.py`, then GREEN, then refactor for clarity if needed.

## Verification

- `pytest -p no:pytest_ethereum -q` — full suite green.
- validate-adversarial pass: reason about concurrent/edge conditions
  (order fills exactly at timeout boundary, window closes exactly during
  a replace, DB row missing, client.place_limit_order returns error status
  on replacement attempt).

## Review gate

Independent review subagent pass on the diff before Close, per
"never self-review for money-moving code paths."

## Explicitly out of scope

- Wallet signing (`sign_order`) test coverage — tracked separately.
- Live-money / `--network` testing.
- The `nonce=0` hardcoding question raised in the prior retro — separate
  issue, needs human decision.
