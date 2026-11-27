-- Agent definitions and the tenant fact base (plan agent.md §11).
--
-- Two things this migration creates, one thing it extends:
--
-- 1. The `agents` table grows registry columns. Until now every row was a
--    chat agent authored from the dashboard; the swarm's fifteen definitions
--    lived only in code. `agent_key` links a row to its registry entry
--    (app/services/agents/registry.py), `kind` says chat vs swarm, and
--    `enabled` is the owner's off switch. Rows are materialized lazily per
--    tenant by AgentPromptService.ensure_rows on first read — no backfill, so
--    the registry stays the one source of prompt text and a changed default
--    reaches existing tenants through the reset path, not a migration.
--
-- 2. `tenant_fact_revisions`: append-only history of every change to the
--    profile block. The whoami agent fills facts after a crawl; the owner
--    edits them by hand; this table is the difference between "the agent
--    overwrote what I typed" and a page that shows exactly that. Written by
--    ProfileService.put, never updated, never deleted (compliance purge
--    cascades with the tenant).
--
-- No down path, as with every migration here. Forward only.

-- ---------------------------------------------------------------------------
-- agents: registry columns
-- ---------------------------------------------------------------------------

ALTER TABLE agents ADD COLUMN IF NOT EXISTS agent_key TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'chat';
ALTER TABLE agents ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS activated_at TIMESTAMPTZ;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS blocked_reason TEXT NOT NULL DEFAULT '';

DO $$
BEGIN
    ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_kind_valid;
    ALTER TABLE agents ADD CONSTRAINT agents_kind_valid CHECK (kind IN ('chat', 'swarm'));
END $$;

-- one row per registry agent per tenant. Existing chat rows have NULL
-- agent_key and never collide; the partial index keeps it that way even if a
-- future template is named after a registry key.
CREATE UNIQUE INDEX IF NOT EXISTS agents_agent_key_per_tenant
    ON agents (tenant_id, agent_key) WHERE agent_key IS NOT NULL;

-- ensure_rows and the dashboard list both look up the swarm by key
CREATE INDEX IF NOT EXISTS agents_agent_key_idx ON agents (agent_key);

-- ---------------------------------------------------------------------------
-- tenant_fact_revisions
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tenant_fact_revisions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- monotonically increasing per tenant; 1 on the first write
    revision     INTEGER NOT NULL,
    -- owner | whoami_agent — who made this change, for the audit trail's
    -- "never overwrite what the owner typed" proof
    actor        TEXT NOT NULL,
    -- the whole profile block as it stood after this change
    profile      JSONB NOT NULL,
    -- which fields changed and from what: {"sells": {"from": "", "to": "..."}}
    changed      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tenant_fact_revisions_actor_valid CHECK (
        actor IN ('owner', 'whoami_agent')
    ),
    CONSTRAINT tenant_fact_revisions_one_rev_per_tenant UNIQUE (tenant_id, revision)
);

CREATE INDEX IF NOT EXISTS tenant_fact_revisions_tenant_idx
    ON tenant_fact_revisions (tenant_id, revision DESC);

-- ---------------------------------------------------------------------------
-- Row level security. Table + ENABLE + FORCE + policy, all four, every time
-- (the pattern every tenant table in this schema follows — see 024).
-- ---------------------------------------------------------------------------

ALTER TABLE tenant_fact_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_fact_revisions FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    DROP POLICY IF EXISTS tenant_isolation ON tenant_fact_revisions;
    CREATE POLICY tenant_isolation ON tenant_fact_revisions
        USING (tenant_id = current_tenant_id())
        WITH CHECK (tenant_id = current_tenant_id());
END $$;
