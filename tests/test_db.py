"""
tests/test_db.py – Unit tests for db.OrderStore.

All tests use an in-memory SQLite database so they leave no files on disk
and run in sub-millisecond time.
"""

from __future__ import annotations

import time

import pytest

from db import OrderStore


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def store() -> OrderStore:
    """Fresh in-memory OrderStore for each test."""
    s = OrderStore(":memory:")
    yield s
    s.close()


# Minimal valid order kwargs for insert_order
ORDER_DEFAULTS = dict(
    order_id    = "SIM-1713189900-001",
    token_id    = "71321045679252212594626385532706912750332728571942532289631379312455583992563",
    side        = "BUY",
    entry_price = 0.60,
    size_usdc   = 25.0,
    window_end  = 1_713_189_900,   # 2024-04-15 14:05:00 UTC
)


def insert_default(store: OrderStore, **overrides) -> str:
    """Insert the default order (with optional field overrides) and return its ID."""
    kwargs = {**ORDER_DEFAULTS, **overrides}
    store.insert_order(**kwargs)
    return kwargs["order_id"]


# ─── insert_order ─────────────────────────────────────────────────────────────

class TestInsertOrder:

    def test_inserted_row_is_retrievable(self, store):
        insert_default(store)
        row = store.get_order(ORDER_DEFAULTS["order_id"])
        assert row is not None

    def test_initial_status_is_open(self, store):
        insert_default(store)
        row = store.get_order(ORDER_DEFAULTS["order_id"])
        assert row["status"] == "open"

    def test_fields_stored_correctly(self, store):
        insert_default(store)
        row = store.get_order(ORDER_DEFAULTS["order_id"])
        assert row["order_id"]    == ORDER_DEFAULTS["order_id"]
        assert row["token_id"]    == ORDER_DEFAULTS["token_id"]
        assert row["side"]        == ORDER_DEFAULTS["side"]
        assert row["entry_price"] == pytest.approx(ORDER_DEFAULTS["entry_price"])
        assert row["size_usdc"]   == pytest.approx(ORDER_DEFAULTS["size_usdc"])
        assert row["window_end"]  == ORDER_DEFAULTS["window_end"]

    def test_pnl_and_settled_at_are_null_initially(self, store):
        insert_default(store)
        row = store.get_order(ORDER_DEFAULTS["order_id"])
        assert row["pnl_usdc"]  is None
        assert row["settled_at"] is None

    def test_created_at_is_set_to_current_time(self, store):
        before = int(time.time())
        insert_default(store)
        after = int(time.time()) + 1
        row = store.get_order(ORDER_DEFAULTS["order_id"])
        assert before <= row["created_at"] <= after

    def test_duplicate_insert_is_ignored(self, store):
        """INSERT OR IGNORE means a second call with the same order_id is a no-op."""
        insert_default(store)
        insert_default(store)   # must not raise or duplicate
        orders = store.get_open_orders()
        assert len(orders) == 1

    def test_multiple_orders_stored_independently(self, store):
        insert_default(store, order_id="SIM-001")
        insert_default(store, order_id="SIM-002")
        assert store.get_order("SIM-001") is not None
        assert store.get_order("SIM-002") is not None


# ─── update_status ────────────────────────────────────────────────────────────

class TestUpdateStatus:

    def test_open_to_filled(self, store):
        insert_default(store)
        store.update_status(ORDER_DEFAULTS["order_id"], "filled")
        row = store.get_order(ORDER_DEFAULTS["order_id"])
        assert row["status"] == "filled"

    def test_open_to_cancelled(self, store):
        insert_default(store)
        store.update_status(ORDER_DEFAULTS["order_id"], "cancelled")
        row = store.get_order(ORDER_DEFAULTS["order_id"])
        assert row["status"] == "cancelled"

    def test_settled_stores_pnl(self, store):
        insert_default(store)
        store.update_status(ORDER_DEFAULTS["order_id"], "settled", pnl_usdc=12.5)
        row = store.get_order(ORDER_DEFAULTS["order_id"])
        assert row["status"]   == "settled"
        assert row["pnl_usdc"] == pytest.approx(12.5)

    def test_settled_stores_negative_pnl(self, store):
        insert_default(store)
        store.update_status(ORDER_DEFAULTS["order_id"], "settled", pnl_usdc=-25.0)
        row = store.get_order(ORDER_DEFAULTS["order_id"])
        assert row["pnl_usdc"] == pytest.approx(-25.0)

    def test_settled_records_settled_at_timestamp(self, store):
        insert_default(store)
        before = int(time.time())
        store.update_status(ORDER_DEFAULTS["order_id"], "settled", pnl_usdc=5.0)
        after = int(time.time()) + 1
        row = store.get_order(ORDER_DEFAULTS["order_id"])
        assert before <= row["settled_at"] <= after

    def test_explicit_settled_at_is_honoured(self, store):
        insert_default(store)
        ts = 1_713_190_000
        store.update_status(ORDER_DEFAULTS["order_id"], "settled",
                            pnl_usdc=0.0, settled_at=ts)
        row = store.get_order(ORDER_DEFAULTS["order_id"])
        assert row["settled_at"] == ts

    def test_returns_true_when_row_updated(self, store):
        insert_default(store)
        result = store.update_status(ORDER_DEFAULTS["order_id"], "filled")
        assert result is True

    def test_returns_false_for_unknown_order(self, store):
        result = store.update_status("NONEXISTENT-999", "filled")
        assert result is False

    def test_raises_for_invalid_status(self, store):
        insert_default(store)
        with pytest.raises(ValueError, match="Unknown status"):
            store.update_status(ORDER_DEFAULTS["order_id"], "bogus")

    def test_pnl_not_overwritten_by_none(self, store):
        """Passing pnl_usdc=None must not clear an already-set pnl value."""
        insert_default(store)
        store.update_status(ORDER_DEFAULTS["order_id"], "settled", pnl_usdc=8.0)
        store.update_status(ORDER_DEFAULTS["order_id"], "settled", pnl_usdc=None)
        row = store.get_order(ORDER_DEFAULTS["order_id"])
        assert row["pnl_usdc"] == pytest.approx(8.0)


# ─── get_open_orders ──────────────────────────────────────────────────────────

class TestGetOpenOrders:

    def test_empty_when_no_orders(self, store):
        assert store.get_open_orders() == []

    def test_returns_inserted_open_order(self, store):
        insert_default(store)
        rows = store.get_open_orders()
        assert len(rows) == 1
        assert rows[0]["order_id"] == ORDER_DEFAULTS["order_id"]

    def test_excludes_filled_orders(self, store):
        insert_default(store, order_id="A")
        insert_default(store, order_id="B")
        store.update_status("A", "filled")
        rows = store.get_open_orders()
        assert len(rows) == 1
        assert rows[0]["order_id"] == "B"

    def test_excludes_cancelled_orders(self, store):
        insert_default(store, order_id="A")
        store.update_status("A", "cancelled")
        assert store.get_open_orders() == []

    def test_excludes_settled_orders(self, store):
        insert_default(store, order_id="A")
        store.update_status("A", "settled", pnl_usdc=1.0)
        assert store.get_open_orders() == []

    def test_returns_multiple_open_orders(self, store):
        insert_default(store, order_id="A")
        insert_default(store, order_id="B")
        insert_default(store, order_id="C")
        rows = store.get_open_orders()
        ids = {r["order_id"] for r in rows}
        assert ids == {"A", "B", "C"}

    def test_ordered_by_created_at(self, store):
        """Rows must come back oldest-first."""
        insert_default(store, order_id="FIRST")
        insert_default(store, order_id="SECOND")
        rows = store.get_open_orders()
        assert rows[0]["order_id"] == "FIRST"
        assert rows[1]["order_id"] == "SECOND"


# ─── get_order ────────────────────────────────────────────────────────────────

class TestGetOrder:

    def test_returns_none_for_missing_order(self, store):
        assert store.get_order("does-not-exist") is None

    def test_returns_dict_not_sqlite_row(self, store):
        insert_default(store)
        row = store.get_order(ORDER_DEFAULTS["order_id"])
        assert isinstance(row, dict)


# ─── get_orders_for_window ────────────────────────────────────────────────────

class TestGetOrdersForWindow:

    def test_returns_orders_matching_window(self, store):
        insert_default(store, order_id="A", window_end=1_000)
        insert_default(store, order_id="B", window_end=1_300)
        rows = store.get_orders_for_window(1_000)
        assert len(rows) == 1
        assert rows[0]["order_id"] == "A"

    def test_includes_all_statuses(self, store):
        insert_default(store, order_id="OPEN",   window_end=5_000)
        insert_default(store, order_id="FILLED", window_end=5_000)
        store.update_status("FILLED", "filled")
        rows = store.get_orders_for_window(5_000)
        ids = {r["order_id"] for r in rows}
        assert ids == {"OPEN", "FILLED"}

    def test_returns_empty_for_unknown_window(self, store):
        assert store.get_orders_for_window(999_999) == []
