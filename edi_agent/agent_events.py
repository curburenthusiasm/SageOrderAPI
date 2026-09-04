"""Unified agent-activity event log (the dashboard's data backbone).

A single append-only event stream that every bot writes to — this project's EDI
bots directly, and the autonomous-department agents (InboxBot, EDI Monitor,
sage_bot, leadtime_bot, ceo_scheduler, open_claw) via the ``POST /events``
ingest endpoint. The shape mirrors the department spec's ``work_events`` table
so the two systems converge on one schema ahead of the full merge.

Backed by the SQLite state DB so it persists across restarts.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import List, Optional

from .config import config

_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT,
    source      TEXT,          -- which bot (inboxbot, edi_agent, open_claw, ...)
    event_type  TEXT,          -- heartbeat | email | edi | erp_query | ... | resolution
    subject     TEXT,
    outcome     TEXT,          -- pending | resolved | escalated | failed | ignored
    decision    TEXT,          -- self_heal | escalate | watch | (null)
    metadata    TEXT           -- JSON
);
CREATE INDEX IF NOT EXISTS idx_agent_events_source ON agent_events(source);
CREATE INDEX IF NOT EXISTS idx_agent_events_created ON agent_events(created_at);
"""

# The full roster shown on the dashboard, so every expected bot appears even
# before it has reported in (status = offline until its first event).
ROSTER = [
    {"source": "edi_agent",     "label": "EDI Agent",       "group": "OrderAPI"},
    {"source": "orderful_sync", "label": "Orderful Sync",   "group": "OrderAPI"},
    {"source": "inboxbot",      "label": "InboxBot",        "group": "Department"},
    {"source": "edi_monitor",   "label": "EDI Monitor",     "group": "Department"},
    {"source": "sage_bot",      "label": "sage_bot",        "group": "Department"},
    {"source": "leadtime_bot",  "label": "leadtime_bot",    "group": "Department"},
    {"source": "ceo_scheduler", "label": "CEO Scheduler",   "group": "Department"},
    {"source": "open_claw",     "label": "Open Claw",       "group": "Department"},
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventStore:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or config.STATE_DB_PATH
        with _LOCK, self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def record(self, source: str, event_type: str, subject: str = "",
               outcome: str = "pending", decision: Optional[str] = None,
               metadata: Optional[dict] = None, created_at: Optional[str] = None) -> int:
        with _LOCK, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO agent_events (created_at, source, event_type, subject, "
                "outcome, decision, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (created_at or _now(), source or "unknown", event_type, subject,
                 outcome, decision, json.dumps(metadata or {}, default=str)),
            )
            return cur.lastrowid

    def recent(self, limit: int = 100, source: Optional[str] = None) -> List[dict]:
        with _LOCK, self._connect() as conn:
            if source:
                rows = conn.execute(
                    "SELECT * FROM agent_events WHERE source=? ORDER BY id DESC LIMIT ?",
                    (source, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM agent_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_row(r) for r in rows]

    def all_in_window(self, days: int = 7) -> List[dict]:
        """Every event newer than ``days`` (for judging)."""
        cutoff = _iso_days_ago(days)
        with _LOCK, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_events WHERE created_at >= ? ORDER BY id DESC",
                (cutoff,)).fetchall()
        return [_row(r) for r in rows]

    def distinct_sources(self) -> List[str]:
        with _LOCK, self._connect() as conn:
            rows = conn.execute("SELECT DISTINCT source FROM agent_events").fetchall()
        return [r["source"] for r in rows]

    def count(self) -> int:
        with _LOCK, self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM agent_events").fetchone()[0]


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    try:
        d["metadata"] = json.loads(d.get("metadata") or "{}")
    except (TypeError, ValueError):
        d["metadata"] = {}
    return d


def _iso_days_ago(days: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
