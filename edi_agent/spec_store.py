"""Partner EDI spec / companion-guide library.

Onboarding a new trading partner = uploading their EDI companion guides (PDFs),
one per outbound doc type (855/856/810/...). Those specs become the rulebook the
agent uses when generating that partner's documents (see ``spec_generator.py``).

A spec is keyed by ``(trading_partner, doc_type)``. The set of specs for one
partner *is* the integration: which docs we can build, and how.

Files are stored under ``SPECS_DIR``; metadata in the SQLite state DB.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from .config import config

_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS doc_specs (
    id               TEXT PRIMARY KEY,
    trading_partner  TEXT,
    doc_type         TEXT,
    filename         TEXT,
    file_path        TEXT,
    excerpt          TEXT,
    uploaded_at      TEXT
);
CREATE TABLE IF NOT EXISTS integration_activation (
    trading_partner  TEXT PRIMARY KEY,
    activated_at     TEXT,
    doc_types        TEXT,
    workflow         TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_excerpt(pdf_bytes: bytes, limit: int = 4000) -> str:
    """Best-effort text excerpt for listing/preview (optional dependency)."""
    try:
        import io
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:10])
        return text[:limit].strip()
    except BaseException:  # noqa: BLE001 - pypdf optional; native backends can panic
        return ""


class SpecStore:
    def __init__(self, db_path: Optional[str] = None, specs_dir: Optional[str] = None):
        self.db_path = db_path or config.STATE_DB_PATH
        self.specs_dir = specs_dir or config.SPECS_DIR
        os.makedirs(self.specs_dir, exist_ok=True)
        with _LOCK, sqlite3.connect(self.db_path) as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, trading_partner: str, doc_type: str, filename: str,
             content: bytes) -> dict:
        """Store a spec PDF + metadata. Returns the spec record."""
        spec_id = uuid.uuid4().hex[:12]
        safe_partner = "".join(c for c in (trading_partner or "any") if c.isalnum() or c in "-_")
        disk_name = f"{safe_partner}_{doc_type}_{spec_id}_{os.path.basename(filename)}"
        file_path = os.path.join(self.specs_dir, disk_name)
        with open(file_path, "wb") as fh:
            fh.write(content)
        excerpt = _extract_excerpt(content)
        with _LOCK, self._connect() as conn:
            conn.execute(
                "INSERT INTO doc_specs (id, trading_partner, doc_type, filename, "
                "file_path, excerpt, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (spec_id, trading_partner or "", doc_type, filename, file_path,
                 excerpt, _now()),
            )
        return self.get(spec_id)

    def get(self, spec_id: str) -> Optional[dict]:
        with _LOCK, self._connect() as conn:
            row = conn.execute("SELECT * FROM doc_specs WHERE id = ?", (spec_id,)).fetchone()
        return _to_dict(row) if row else None

    def find(self, doc_type: str, trading_partner: str = "") -> Optional[dict]:
        """Best match for a doc type: exact partner first, then a generic spec."""
        with _LOCK, self._connect() as conn:
            if trading_partner:
                row = conn.execute(
                    "SELECT * FROM doc_specs WHERE doc_type = ? AND trading_partner = ? "
                    "ORDER BY uploaded_at DESC LIMIT 1",
                    (doc_type, trading_partner),
                ).fetchone()
                if row:
                    return _to_dict(row)
            row = conn.execute(
                "SELECT * FROM doc_specs WHERE doc_type = ? AND "
                "(trading_partner = '' OR trading_partner IS NULL) "
                "ORDER BY uploaded_at DESC LIMIT 1",
                (doc_type,),
            ).fetchone()
        return _to_dict(row) if row else None

    def list(self, trading_partner: Optional[str] = None) -> List[dict]:
        with _LOCK, self._connect() as conn:
            if trading_partner is not None:
                rows = conn.execute(
                    "SELECT * FROM doc_specs WHERE trading_partner = ? ORDER BY uploaded_at DESC",
                    (trading_partner,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM doc_specs ORDER BY uploaded_at DESC").fetchall()
        return [_to_dict(r) for r in rows]

    def partners(self) -> List[dict]:
        """Each known integration: partner + the doc types its specs cover."""
        out: dict = {}
        for spec in self.list():
            p = spec["trading_partner"] or "(generic)"
            out.setdefault(p, set()).add(spec["doc_type"])
        return [{"trading_partner": p, "doc_types": sorted(d)} for p, d in sorted(out.items())]

    # --- Workflow activation -------------------------------------------------
    def activate(self, trading_partner: str, doc_types: List[str], workflow: dict) -> dict:
        """Mark a partner's workflow as wired up (doc types + phase plan)."""
        import json
        with _LOCK, self._connect() as conn:
            conn.execute(
                "INSERT INTO integration_activation "
                "(trading_partner, activated_at, doc_types, workflow) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(trading_partner) DO UPDATE SET "
                "activated_at=excluded.activated_at, doc_types=excluded.doc_types, "
                "workflow=excluded.workflow",
                (trading_partner, _now(), json.dumps(doc_types), json.dumps(workflow)),
            )
        return self.activation(trading_partner)

    def activation(self, trading_partner: str) -> Optional[dict]:
        import json
        with _LOCK, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM integration_activation WHERE trading_partner = ?",
                (trading_partner,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["doc_types"] = json.loads(d.get("doc_types") or "[]")
        d["workflow"] = json.loads(d.get("workflow") or "{}")
        return d

    def list_activations(self) -> List[dict]:
        with _LOCK, self._connect() as conn:
            rows = conn.execute("SELECT trading_partner FROM integration_activation").fetchall()
        return [dict(r) for r in rows]

    def delete(self, spec_id: str) -> bool:
        spec = self.get(spec_id)
        if not spec:
            return False
        try:
            if spec["file_path"] and os.path.exists(spec["file_path"]):
                os.remove(spec["file_path"])
        except OSError:
            pass
        with _LOCK, self._connect() as conn:
            conn.execute("DELETE FROM doc_specs WHERE id = ?", (spec_id,))
        return True


def _to_dict(row: sqlite3.Row) -> dict:
    """Full internal record (includes file_path/excerpt for the generator)."""
    return dict(row)


def public(spec: Optional[dict]) -> Optional[dict]:
    """Sanitized view for API responses (no disk path, short excerpt preview)."""
    if not spec:
        return None
    d = dict(spec)
    d["excerpt_preview"] = (d.pop("excerpt", "") or "")[:200]
    d.pop("file_path", None)
    return d
