-- Staff accounts, audit log, and usage rollups.
--
-- These tables are platform-scoped, not tenant-scoped. They carry no RLS policy
-- and are reached only through admin_scope, because staff read and act across
-- every tenant by design. usage_rollups carries tenant_id as a grouping column
-- the console filters on, not as an isolation boundary.

-- Our own team. Separate from users on purpose: a platform operator is not a
-- tenant member, and conflating the two would let a support login inherit a
-- customer's data access.
CREATE TABLE IF NOT EXISTS staff_accounts (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    name          TEXT,
    role          TEXT NOT NULL DEFAULT 'support',
    status        TEXT NOT NULL DEFAULT 'active',
    last_login_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT staff_role_valid CHECK (role IN ('support', 'admin', 'owner')),
    CONSTRAINT staff_status_valid CHECK (status IN ('active', 'suspended'))
);

-- Append-only record of privileged actions. Written in the same transaction as
-- the action it describes, so an action can never exist without its audit row.
-- No UPDATE or DELETE path is provided anywhere in the code.
CREATE TABLE IF NOT EXISTS audit_log (
    id             BIGSERIAL PRIMARY KEY,
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_type     TEXT NOT NULL,
    actor_id       UUID,
    actor_email    TEXT,
    action         TEXT NOT NULL,
    target_tenant  UUID,
    target_type    TEXT,
    target_id      TEXT,
    detail         JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT audit_actor_type_valid CHECK (actor_type IN ('staff', 'user', 'system'))
);
CREATE INDEX IF NOT EXISTS audit_log_ts_idx ON audit_log (ts DESC);
CREATE INDEX IF NOT EXISTS audit_log_tenant_ts_idx ON audit_log (target_tenant, ts DESC);
CREATE INDEX IF NOT EXISTS audit_log_actor_ts_idx ON audit_log (actor_id, ts DESC);
CREATE INDEX IF NOT EXISTS audit_log_action_ts_idx ON audit_log (action, ts DESC);

-- Daily per-tenant aggregation of usage_events. The dashboard and the staff cost
-- views read this; scanning the raw append-only ledger on every request does not
-- scale. The rollup job is idempotent, so a re-run overwrites a day rather than
-- doubling it — hence the unique key and ON CONFLICT upsert.
CREATE TABLE IF NOT EXISTS usage_rollups (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    day            DATE NOT NULL,
    kind           TEXT NOT NULL,
    quantity       BIGINT NOT NULL DEFAULT 0,
    cost_micros    BIGINT NOT NULL DEFAULT 0,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT usage_rollups_unique UNIQUE (tenant_id, day, kind)
);
CREATE INDEX IF NOT EXISTS usage_rollups_tenant_day_idx
    ON usage_rollups (tenant_id, day DESC);
CREATE INDEX IF NOT EXISTS usage_rollups_day_idx ON usage_rollups (day DESC);

-- A cursor so the rollup job knows how far it has already aggregated and never
-- re-scans the whole ledger. Single row, advanced after each successful run.
CREATE TABLE IF NOT EXISTS rollup_state (
    id            INTEGER PRIMARY KEY DEFAULT 1,
    last_event_id BIGINT NOT NULL DEFAULT 0,
    last_run_at   TIMESTAMPTZ,
    CONSTRAINT rollup_state_singleton CHECK (id = 1)
);
INSERT INTO rollup_state (id, last_event_id) VALUES (1, 0)
    ON CONFLICT (id) DO NOTHING;
