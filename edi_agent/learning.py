"""Failure-feedback learning store.

Every time the agent outputs a document that fails — a partner/VAN rejection or
our own validation failure — we record a *lesson* keyed by
``(trading_partner, doc_type)``. Those lessons are fed back into future
generation and correction prompts so the agent proactively avoids the same
mistake next time. This is in-context reinforcement (a growing, persistent
memory of what went wrong), not model-weight training.

Backed by the SQLite state DB so lessons persist across restarts.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import List, Optional

from .config import config

_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS failure_lessons (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_partner  TEXT,
    doc_type         TEXT,
    source           TEXT,          -- 'rejection' | 'validation'
    failure_message  TEXT,
    lesson           TEXT,
    created_at       TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LessonStore:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or config.STATE_DB_PATH
        with _LOCK, self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def record(self, trading_partner: str, doc_type: str, failure_message: str,
               lesson: Optional[str] = None, source: str = "rejection") -> None:
        """Record a failure lesson, de-duplicating identical ones per partner/doc."""
        partner = trading_partner or ""
        lesson = (lesson or failure_message or "").strip()
        if not lesson:
            return
        with _LOCK, self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM failure_lessons WHERE trading_partner=? AND doc_type=? "
                "AND lesson=? LIMIT 1", (partner, doc_type, lesson)).fetchone()
            if exists:
                return
            conn.execute(
                "INSERT INTO failure_lessons (trading_partner, doc_type, source, "
                "failure_message, lesson, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (partner, doc_type, source, failure_message, lesson, _now()),
            )

    def lessons_for(self, trading_partner: str, doc_type: str, limit: int = 15) -> List[str]:
        """Lesson texts for a partner/doc (newest first) to inject into prompts.

        Includes generic ('') lessons for the doc type as well.
        """
        with _LOCK, self._connect() as conn:
            rows = conn.execute(
                "SELECT lesson FROM failure_lessons WHERE doc_type=? AND "
                "(trading_partner=? OR trading_partner='') "
                "ORDER BY id DESC LIMIT ?",
                (doc_type, trading_partner or "", limit)).fetchall()
        return [r["lesson"] for r in rows]

    def all(self) -> List[dict]:
        with _LOCK, self._connect() as conn:
            rows = conn.execute(
                "SELECT trading_partner, doc_type, source, failure_message, lesson, "
                "created_at FROM failure_lessons ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        with _LOCK, self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM failure_lessons").fetchone()[0]
