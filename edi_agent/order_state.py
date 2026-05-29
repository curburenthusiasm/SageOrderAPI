"""Order state machine — per-PO document status, persisted in SQLite.

Tracks the lifecycle of every PO through the pipeline so status survives a
process restart and the ``/orders`` / ``/order/{po}/status`` endpoints can
report which documents have been sent and their Orderful transaction ids.

    RECEIVED -> 997_SENT -> 855_SENT -> SHIPPED (856_SENT) -> INVOICED (810_SENT)
             \\-> ERROR (logged)

Uses the stdlib ``sqlite3`` (synchronous) — no extra dependency, fine for the
current single-process FastAPI app. Connections are opened per operation.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import List, Optional

from .config import config

_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS order_state (
    po_number       TEXT PRIMARY KEY,
    received_at     TEXT,
    parsed_ok       INTEGER,
    doc_997_sent    INTEGER DEFAULT 0,
    doc_997_id      TEXT,
    doc_855_sent    INTEGER DEFAULT 0,
    doc_855_id      TEXT,
    doc_856_sent    INTEGER DEFAULT 0,
    doc_856_id      TEXT,
    doc_810_sent    INTEGER DEFAULT 0,
    doc_810_id      TEXT,
    ship_date       TEXT,
    invoice_number  TEXT,
    error_log       TEXT,
    updated_at      TEXT
);
"""

_DOC_COLUMNS = {"997", "855", "856", "810"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class OrderStateStore:
    """SQLite-backed store for per-PO document state."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or config.STATE_DB_PATH
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with _LOCK, self._connect() as conn:
            conn.executescript(_SCHEMA)

    def record_received(self, po_number: str, parsed_ok: bool = True) -> None:
        with _LOCK, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO order_state (po_number, received_at, parsed_ok, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(po_number) DO UPDATE SET
                    parsed_ok = excluded.parsed_ok,
                    updated_at = excluded.updated_at
                """,
                (po_number, _now(), 1 if parsed_ok else 0, _now()),
            )

    def mark_doc_sent(self, po_number: str, doc_type: str, tx_id: Optional[str]) -> None:
        if doc_type not in _DOC_COLUMNS:
            return
        sent_col = f"doc_{doc_type}_sent"
        id_col = f"doc_{doc_type}_id"
        with _LOCK, self._connect() as conn:
            # Ensure the row exists, then update the doc columns.
            conn.execute(
                "INSERT OR IGNORE INTO order_state (po_number, received_at, updated_at) "
                "VALUES (?, ?, ?)",
                (po_number, _now(), _now()),
            )
            conn.execute(
                f"UPDATE order_state SET {sent_col} = 1, {id_col} = ?, updated_at = ? "
                "WHERE po_number = ?",
                (tx_id, _now(), po_number),
            )

    def set_fields(self, po_number: str, **fields) -> None:
        """Set arbitrary columns (e.g. ship_date, invoice_number)."""
        allowed = {"ship_date", "invoice_number", "parsed_ok"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        assignments = ", ".join(f"{k} = ?" for k in sets)
        values = list(sets.values()) + [_now(), po_number]
        with _LOCK, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO order_state (po_number, received_at, updated_at) "
                "VALUES (?, ?, ?)",
                (po_number, _now(), _now()),
            )
            conn.execute(
                f"UPDATE order_state SET {assignments}, updated_at = ? WHERE po_number = ?",
                values,
            )

    def log_error(self, po_number: str, message: str) -> None:
        with _LOCK, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO order_state (po_number, received_at, updated_at) "
                "VALUES (?, ?, ?)",
                (po_number, _now(), _now()),
            )
            conn.execute(
                "UPDATE order_state SET error_log = ?, updated_at = ? WHERE po_number = ?",
                (message, _now(), po_number),
            )

    def get(self, po_number: str) -> Optional[dict]:
        with _LOCK, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM order_state WHERE po_number = ?", (po_number,)
            ).fetchone()
        return _row_to_state(row) if row else None

    def list_orders(self) -> List[dict]:
        with _LOCK, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM order_state ORDER BY updated_at DESC"
            ).fetchall()
        return [_row_to_state(r) for r in rows]


def _row_to_state(row: sqlite3.Row) -> dict:
    """Shape a DB row into a friendly state dict with a derived phase."""
    d = dict(row)
    for col in ("parsed_ok", "doc_997_sent", "doc_855_sent", "doc_856_sent", "doc_810_sent"):
        d[col] = bool(d.get(col))
    if d["doc_810_sent"]:
        phase = "INVOICED"
    elif d["doc_856_sent"]:
        phase = "SHIPPED"
    elif d["doc_855_sent"]:
        phase = "855_SENT"
    elif d["doc_997_sent"]:
        phase = "997_SENT"
    elif d.get("error_log"):
        phase = "ERROR"
    else:
        phase = "RECEIVED"
    d["phase"] = phase
    return d
