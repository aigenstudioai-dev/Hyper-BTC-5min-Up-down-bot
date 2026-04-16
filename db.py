"""
db.py – SQLite order persistence for crash recovery.

Every order placed by the bot is written here before it reaches the
exchange.  On restart the bot reads all ``open`` rows and reconciles
them against the live CLOB API so no position is silently forgotten.

Schema
------
orders
  order_id    TEXT  PRIMARY KEY   – Polymarket order ID (or SIM-<ts>)
  token_id    TEXT  NOT NULL      – YES or NO token being bought
  side        TEXT  NOT NULL      – always 'BUY' (we buy YES or NO)
  entry_price REAL  NOT NULL      – limit price (0–1 USDC per share)
  size_usdc   REAL  NOT NULL      – USDC committed
  window_end  INT   NOT NULL      – Unix timestamp of the 5-min window close
  status      TEXT  NOT NULL      – open | filled | cancelled | settled
  pnl_usdc    REAL                – NULL until settled
  settled_at  INT                 – Unix timestamp of settlement (NULL until then)
  created_at  INT   NOT NULL      – Unix timestamp of insertion
"""

from __future__ import annotations

import sqlite3
import time
from typing import List, Optional

_DDL = """
CREATE TABLE IF NOT EXISTS orders (
    order_id    TEXT    PRIMARY KEY,
    token_id    TEXT    NOT NULL,
    side        TEXT    NOT NULL,
    entry_price REAL    NOT NULL,
    size_usdc   REAL    NOT NULL,
    window_end  INTEGER NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'open',
    pnl_usdc    REAL,
    settled_at  INTEGER,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);
CREATE INDEX IF NOT EXISTS idx_orders_window ON orders (window_end);
"""

# Valid status transitions (not enforced in SQL; enforced here)
_VALID_STATUSES = frozenset({"open", "filled", "cancelled", "settled"})


class OrderStore:
    """
    Thin SQLite wrapper for order lifecycle tracking.

    Usage
    -----
    store = OrderStore()                   # opens/creates orders.db
    store = OrderStore(":memory:")         # in-memory (tests)

    store.insert_order(order_id, token_id, side, entry_price, size_usdc, window_end)
    store.update_status(order_id, "filled")
    store.update_status(order_id, "settled", pnl_usdc=12.5)
    rows = store.get_open_orders()         # list[dict] with status='open'
    store.close()
    """

    def __init__(self, db_path: str = "orders.db") -> None:
        self._path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        self._conn.commit()

    # ── Write operations ──────────────────────────────────────────────────────

    def insert_order(
        self,
        order_id: str,
        token_id: str,
        side: str,
        entry_price: float,
        size_usdc: float,
        window_end: int,
    ) -> None:
        """
        Persist a new order.  Silently ignored if ``order_id`` already exists
        (idempotent – safe to call again after a restart).
        """
        self._conn.execute(
            """
            INSERT OR IGNORE INTO orders
              (order_id, token_id, side, entry_price, size_usdc,
               window_end, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            (order_id, token_id, side, entry_price, size_usdc, window_end,
             int(time.time())),
        )
        self._conn.commit()

    def update_status(
        self,
        order_id: str,
        status: str,
        *,
        pnl_usdc: Optional[float] = None,
        settled_at: Optional[int] = None,
    ) -> bool:
        """
        Update the status of an existing order.

        Returns True if a row was updated, False if the order_id was not found.
        Raises ValueError for unknown status values.
        """
        if status not in _VALID_STATUSES:
            raise ValueError(f"Unknown status {status!r}; expected one of {_VALID_STATUSES}")

        ts = settled_at if settled_at is not None else (
            int(time.time()) if status == "settled" else None
        )
        cur = self._conn.execute(
            """
            UPDATE orders
               SET status     = ?,
                   pnl_usdc   = COALESCE(?, pnl_usdc),
                   settled_at = COALESCE(?, settled_at)
             WHERE order_id   = ?
            """,
            (status, pnl_usdc, ts, order_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    # ── Read operations ───────────────────────────────────────────────────────

    def get_open_orders(self) -> List[dict]:
        """Return all orders with status='open' as plain dicts."""
        cur = self._conn.execute(
            "SELECT * FROM orders WHERE status = 'open' ORDER BY created_at"
        )
        return [dict(row) for row in cur.fetchall()]

    def get_order(self, order_id: str) -> Optional[dict]:
        """Return a single order by ID, or None if not found."""
        cur = self._conn.execute(
            "SELECT * FROM orders WHERE order_id = ?", (order_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_orders_for_window(self, window_end: int) -> List[dict]:
        """Return all orders (any status) whose window_end matches."""
        cur = self._conn.execute(
            "SELECT * FROM orders WHERE window_end = ? ORDER BY created_at",
            (window_end,),
        )
        return [dict(row) for row in cur.fetchall()]

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()
