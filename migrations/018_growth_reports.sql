-- Growth platform, Stage 3: the report agent's own ledger.
--
-- One table. Everything the report reads already exists: growth_leads,
-- growth_messages, research_runs (016/017) and usage_rollups carry the numbers,
-- and growth_campaigns / growth_customers / growth_orders / growth_insights /
-- growth_events do not exist yet — they land in Stages 4-5. The report queries
-- probe for those tables at runtime and treat an absent one as an empty
-- section, so this migration needs no stub and the agent renders "only sections
-- with data" from day one.
--
-- `numbers` keeps the full computed set, so a report can be compared against
-- the previous period or regenerated without re-running the queries. `pdf_path`
-- is the object-storage key under tenants/{tenant_id}/reports/, never bytes in
-- the row. One row per (tenant, period, period_start): an on-demand re-run of
-- the same period regenerates the row rather than duplicating it.
--
-- No down path, as with every migration here. Forward only.

CREATE TABLE IF NOT EXISTS growth_reports (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    period       TEXT NOT NULL,
    period_start DATE NOT NULL,
    -- the full computed number set, JSON so the panel can render a fallback
    -- while the PDF is still rendering and month-end review can diff periods
    numbers      JSONB NOT NULL DEFAULT '{}'::jsonb,
    pdf_path     TEXT NOT NULL DEFAULT '',
    -- the two-line chat summary that went to WhatsApp/Telegram with this report
    summary      TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'queued',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT growth_reports_period_valid CHECK (
        period IN ('weekly', 'monthly', 'daily', 'on_demand')
    ),
    CONSTRAINT growth_reports_status_valid CHECK (
        status IN ('queued', 'ready', 'failed')
    ),
    CONSTRAINT growth_reports_one_per_period UNIQUE (tenant_id, period, period_start)
);

-- the panel lists newest first; the due-check asks "is there a row for this
-- tenant covering this period start", and the unique constraint above serves it
CREATE INDEX IF NOT EXISTS growth_reports_tenant_created_idx
    ON growth_reports (tenant_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- Row level security. FORCE applies the policy to the table owner too, which is
-- what stops the app role — which owns these tables — from bypassing isolation.
-- ---------------------------------------------------------------------------
ALTER TABLE growth_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE growth_reports FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    DROP POLICY IF EXISTS tenant_isolation ON growth_reports;
    CREATE POLICY tenant_isolation ON growth_reports
        USING (tenant_id = current_tenant_id())
        WITH CHECK (tenant_id = current_tenant_id());
END $$;
