"""partner_registry.py — Supabase-backed partner registry.

Stores and retrieves trading partner config: which connector to use,
workflow IDs, webhook URLs, spec paths, and onboarding status.

Supabase table (run migration below to create it):

    CREATE TABLE IF NOT EXISTS edi_partners (
        id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        name            TEXT NOT NULL,
        isa_qualifier   TEXT,            -- ISA sender/receiver qualifier (e.g. "WALMART")
        platform        TEXT NOT NULL,   -- "orderful" | "tray" | "rest_api"
        connector_config JSONB,          -- platform-specific config (webhook_url, auth, field_map, ...)
        spec_path       TEXT,            -- path to parsed spec JSON on disk
        spec_raw        JSONB,           -- embedded spec (for small specs)
        workflow_ids    JSONB,           -- {doc_type: workflow_id}, e.g. {"850": "wf-abc123"}
        webhook_urls    JSONB,           -- {doc_type: url} for Tray webhook triggers
        status          TEXT DEFAULT 'pending',   -- pending | active | error | suspended
        onboarding_log  JSONB,           -- array of log entries
        created_at      TIMESTAMPTZ DEFAULT NOW(),
        updated_at      TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_edi_partners_isa ON edi_partners(isa_qualifier);
    CREATE INDEX IF NOT EXISTS idx_edi_partners_status ON edi_partners(status);
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


def _get_supabase_client():
    """Lazy-init Supabase client."""
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_KEY", "")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set")
    try:
        from supabase import create_client
        return create_client(url, key)
    except ImportError:
        raise RuntimeError("supabase-py not installed: pip install supabase")


TABLE = "edi_partners"


class PartnerRegistry:
    """CRUD interface for the edi_partners Supabase table."""

    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = _get_supabase_client()
        return self._client

    # ------------------------------------------------------------------
    # Create / upsert
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        platform: str,
        *,
        isa_qualifier: str | None = None,
        connector_config: dict | None = None,
        spec_path: str | None = None,
        spec_raw: dict | None = None,
        workflow_ids: dict | None = None,
        webhook_urls: dict | None = None,
        status: str = "pending",
    ) -> dict:
        """Insert a new partner record. Returns the created row."""
        row = {
            "name": name,
            "platform": platform,
            "status": status,
        }
        if isa_qualifier:
            row["isa_qualifier"] = isa_qualifier
        if connector_config:
            row["connector_config"] = connector_config
        if spec_path:
            row["spec_path"] = spec_path
        if spec_raw:
            row["spec_raw"] = spec_raw
        if workflow_ids:
            row["workflow_ids"] = workflow_ids
        if webhook_urls:
            row["webhook_urls"] = webhook_urls

        resp = self.client.table(TABLE).insert(row).execute()
        if resp.data:
            log.info(f"Registered partner [{name}] id={resp.data[0]['id']}")
            return resp.data[0]
        raise RuntimeError(f"Failed to register partner: {resp}")

    def update(self, partner_id: str, **fields) -> dict:
        """Partial update — only supplied fields are changed."""
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        resp = self.client.table(TABLE).update(fields).eq("id", partner_id).execute()
        if resp.data:
            return resp.data[0]
        raise RuntimeError(f"Failed to update partner {partner_id}: {resp}")

    def append_log(self, partner_id: str, entry: dict) -> None:
        """Append a timestamped log entry to onboarding_log."""
        entry.setdefault("ts", datetime.now(timezone.utc).isoformat())
        # Supabase doesn't support array-append directly via REST — fetch + update
        existing = self.get(partner_id)
        log_entries = existing.get("onboarding_log") or []
        if isinstance(log_entries, str):
            log_entries = json.loads(log_entries)
        log_entries.append(entry)
        self.update(partner_id, onboarding_log=log_entries)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, partner_id: str) -> dict:
        resp = self.client.table(TABLE).select("*").eq("id", partner_id).execute()
        if resp.data:
            return resp.data[0]
        raise KeyError(f"Partner {partner_id} not found")

    def find_by_isa(self, isa_qualifier: str) -> dict | None:
        resp = (
            self.client.table(TABLE)
            .select("*")
            .eq("isa_qualifier", isa_qualifier.upper())
            .eq("status", "active")
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None

    def find_by_name(self, name: str) -> dict | None:
        resp = (
            self.client.table(TABLE)
            .select("*")
            .ilike("name", name)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None

    def list_all(self, status: str | None = None) -> list[dict]:
        q = self.client.table(TABLE).select("*").order("created_at", desc=True)
        if status:
            q = q.eq("status", status)
        return q.execute().data or []

    # ------------------------------------------------------------------
    # Connector factory
    # ------------------------------------------------------------------

    def get_connector(self, partner_id: str):
        """Return an initialized ConnectorBase instance for the partner."""
        partner = self.get(partner_id)
        return connector_from_record(partner)


def connector_from_record(partner: dict):
    """Instantiate the correct connector from a partner registry record."""
    platform = partner.get("platform", "orderful")
    cfg = partner.get("connector_config") or {}
    webhook_urls = partner.get("webhook_urls") or {}

    if platform == "orderful":
        from .connectors.orderful import OrderfulClient
        return OrderfulClient()

    elif platform == "tray":
        from .connectors.tray import TrayConnector
        # For runtime submission we only need the webhook_url for the 850 inbound trigger
        # (outbound 997/855/856/810 each get their own webhook URL stored in webhook_urls)
        webhook_url = webhook_urls.get("inbound") or cfg.get("webhook_url", "")
        return TrayConnector(webhook_url=webhook_url)

    elif platform == "rest_api":
        from .connectors.rest_api import RestApiConnector
        return RestApiConnector(connector_config=cfg)

    else:
        raise ValueError(f"Unknown platform: {platform}")


# Module-level singleton
_registry: PartnerRegistry | None = None


def get_registry() -> PartnerRegistry:
    global _registry
    if _registry is None:
        _registry = PartnerRegistry()
    return _registry
