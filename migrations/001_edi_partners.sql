-- Migration: 001_edi_partners
-- Creates the edi_partners table used by partner_registry.py
-- Run once against your Supabase project via the SQL editor or psql.

CREATE TABLE IF NOT EXISTS edi_partners (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name             TEXT NOT NULL,
    isa_qualifier    TEXT,                          -- ISA sender/receiver ID (e.g. "WALMART")
    platform         TEXT NOT NULL,                 -- "orderful" | "tray" | "rest_api" | "pending"
    connector_config JSONB,                         -- platform-specific config blob
    spec_path        TEXT,                          -- local path to parsed spec JSON
    spec_raw         JSONB,                         -- embedded parsed spec (field_map, doc_types, ...)
    workflow_ids     JSONB,                         -- {doc_type: workflow_id}
    webhook_urls     JSONB,                         -- {doc_type: trigger_url} for Tray webhooks
    status           TEXT DEFAULT 'pending',        -- pending | active | pending_manual_step | error | suspended
    onboarding_log   JSONB DEFAULT '[]'::jsonb,     -- [{ts, event, message}, ...]
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_edi_partners_isa      ON edi_partners(isa_qualifier);
CREATE INDEX IF NOT EXISTS idx_edi_partners_status   ON edi_partners(status);
CREATE INDEX IF NOT EXISTS idx_edi_partners_platform ON edi_partners(platform);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_edi_partners_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_edi_partners_updated_at ON edi_partners;
CREATE TRIGGER trg_edi_partners_updated_at
    BEFORE UPDATE ON edi_partners
    FOR EACH ROW EXECUTE FUNCTION update_edi_partners_updated_at();

-- Enable Row Level Security (optional but recommended)
ALTER TABLE edi_partners ENABLE ROW LEVEL SECURITY;

-- Grant full access to the service role (used by supabase-py with SUPABASE_KEY)
CREATE POLICY IF NOT EXISTS "service_role_full_access"
    ON edi_partners
    USING (true)
    WITH CHECK (true);
