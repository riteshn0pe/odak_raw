-- The date-keyed run context store (plan P1).
--
-- Per-day working memory for the swarm: every growth job that runs for a
-- tenant on a calendar day appends findings, drafts and intermediate work
-- under (tenant_id, local_date), and the report agent reads the whole day
-- before composing instead of re-querying.
--
-- Why a date and not a run_id (audit B1): there is no "one daily run" to key
-- on. The swarm is 7+ independent cron tasks (growth_dispatch, report_due,
-- insights, store sync, IndiaMART poll, CRM retry), each *enqueuing* queue
-- jobs whose work happens later in a worker. A run_id minted in dispatch
-- never reaches the report job (different cron), and an end-of-run DELETE
-- erases rows before the enqueued jobs have written them. The tenant's
-- calendar day is the one key every job can compute independently, whatever
-- cron started it — exactly what a fan-out architecture requires.
--
-- The persistent RAG stays the long-term shared record; this is the
-- scratchpad next to it, not a replacement. Postgres, not Redis, because the
-- report agent may read the day minutes after the last agent finished.
--
-- Append-only: no updated_at, no edits. An agent revising its own finding
-- appends a superseding entry; consumers read latest-per-kind when they
-- care. Cleanup is an age sweep off the rollup cron (rows older than 7
-- days), never an end-of-run delete.
--
-- `run_id` is optional and unused by the daily story: a nullable column so a
-- future on-demand run can scope its own rows without a migration. The key
-- is the local date.
--
-- No down path, as with every migration here. Forward only.

CREATE TABLE IF NOT EXISTS run_context (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- the key: the tenant's calendar day, computed from the profile timezone
    local_date  DATE NOT NULL,
    -- AGENT_KEYS key, e.g. growth_research
    agent_key   TEXT NOT NULL,
    -- finding | draft | decision | metric | note
    kind        TEXT NOT NULL,
    payload     JSONB NOT NULL,
    -- optional scope for a future on-demand run; empty for daily work
    run_id      TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT run_context_kind_valid CHECK (
        kind IN ('finding', 'draft', 'decision', 'metric', 'note')
    )
);

-- the day read scans one (tenant, date) slice; the sweep scans created_at
CREATE INDEX IF NOT EXISTS run_context_tenant_day_idx
    ON run_context (tenant_id, local_date, created_at);

-- ---------------------------------------------------------------------------
-- Row level security. FORCE applies the policy to the table owner too, which
-- is what stops the app role — which owns these tables — from bypassing
-- isolation. The policy loop alone is NOT enough (audit R5): a table with
-- policies but RLS not enabled is silently unisolated. Table + ENABLE +
-- FORCE + policy + index, all four, every time.
-- ---------------------------------------------------------------------------
ALTER TABLE run_context ENABLE ROW LEVEL SECURITY;
ALTER TABLE run_context FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    DROP POLICY IF EXISTS tenant_isolation ON run_context;
    CREATE POLICY tenant_isolation ON run_context
        USING (tenant_id = current_tenant_id())
        WITH CHECK (tenant_id = current_tenant_id());
END $$;
