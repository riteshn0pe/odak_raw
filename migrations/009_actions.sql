-- 009_actions.sql
-- Actions engine: tenant-authored action definitions, agent links, named secrets,
-- the invocation ledger, and durable suspensions for client actions and confirmations.
-- No down path.

-- ---------------------------------------------------------------- actions

CREATE TABLE IF NOT EXISTS actions (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name                        text NOT NULL,
    display_name                text NOT NULL,
    description                 text NOT NULL,

    -- Pydantic ActionDefinition (productisation-next.md §4 §2.2) is authoritative
    -- for the full shape.
    definition                  jsonb NOT NULL DEFAULT '{}'::jsonb,

    -- Promoted columns. ALWAYS written from `definition` in the same statement,
    -- never edited independently. These are exactly what ToolSet.resolve filters
    -- on once per turn.
    kind                        text NOT NULL,
    effect                      text NOT NULL DEFAULT 'read',
    enabled                     boolean NOT NULL DEFAULT false,
    only_in_procedures          boolean NOT NULL DEFAULT false,
    requires_confirmation       boolean NOT NULL DEFAULT false,
    requires_verified_identity  boolean NOT NULL DEFAULT false,
    channels                    text[] NOT NULL DEFAULT ARRAY['widget','whatsapp','email','api'],
    system_handler              text,
    target_host                 text,

    -- Library provenance and setup state.
    library_key                 text,
    setup_required              boolean NOT NULL DEFAULT false,

    -- Auth credential: a ref into tenant_secrets. NULL when the action needs none.
    -- Composite FK; NULL secret_ref means the constraint is not enforced, which is
    -- what we want. ON DELETE RESTRICT so a BYOK delete cannot orphan an action;
    -- app/api/v1/integrations.py must surface the resulting ConflictError.
    secret_ref                  text,

    -- Test state. Only the (rare) test endpoint writes these, and last_tested_at
    -- gates the Save button in the UI, so they live on the row. Call counters do
    -- NOT live here: a counter UPDATE per dispatch locks a hot row, and the list
    -- needs windowed metrics, which action_calls gives and a lifetime counter
    -- cannot.
    last_tested_at              timestamptz,
    last_test_status            text,

    version                     integer NOT NULL DEFAULT 1,
    created_by                  uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT actions_name_format
        CHECK (name ~ '^[a-z][a-z0-9_]{1,47}$'),
    CONSTRAINT actions_name_unique
        UNIQUE (tenant_id, name),
    CONSTRAINT actions_kind_valid
        CHECK (kind IN ('server', 'client', 'widget', 'system')),
    CONSTRAINT actions_effect_valid
        CHECK (effect IN ('read', 'write', 'local')),
    CONSTRAINT actions_test_status_valid
        CHECK (last_test_status IS NULL
               OR last_test_status IN ('ok', 'failed', 'timeout', 'blocked')),
    CONSTRAINT actions_system_handler_requires_system
        CHECK (system_handler IS NULL OR kind = 'system'),
    CONSTRAINT actions_secret_fk
        FOREIGN KEY (tenant_id, secret_ref)
        REFERENCES tenant_secrets (tenant_id, ref) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS actions_tenant_enabled_idx
    ON actions (tenant_id, enabled) WHERE enabled;
CREATE INDEX IF NOT EXISTS actions_tenant_library_idx
    ON actions (tenant_id, library_key) WHERE library_key IS NOT NULL;

-- ------------------------------------------------------ action_secrets

-- Named secrets for {{secret.NAME}} interpolation into headers and bodies.
-- Distinct from actions.secret_ref, which is the single auth credential. Both
-- exist because a multi-secret action is real (an API key PLUS a webhook signing
-- secret) and is not expressible in one column, while ActionAuth carries exactly
-- one ref.
CREATE TABLE IF NOT EXISTS action_secrets (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    action_id   uuid NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
    name        text NOT NULL,
    secret_ref  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT action_secrets_name_format
        CHECK (name ~ '^[A-Z][A-Z0-9_]{0,39}$'),
    CONSTRAINT action_secrets_unique
        UNIQUE (action_id, name),
    CONSTRAINT action_secrets_ref_fk
        FOREIGN KEY (tenant_id, secret_ref)
        REFERENCES tenant_secrets (tenant_id, ref) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS action_secrets_action_idx ON action_secrets (action_id);

-- -------------------------------------------------------- agent_actions

CREATE TABLE IF NOT EXISTS agent_actions (
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent_id    uuid NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    action_id   uuid NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
    created_at  timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (agent_id, action_id)
);

CREATE INDEX IF NOT EXISTS agent_actions_tenant_action_idx
    ON agent_actions (tenant_id, action_id);

-- --------------------------------------------------------- action_calls

-- Append-only invocation ledger. Source of truth for every per-action metric the
-- UI shows: "used 84 times this week", last_called_at, last_error, error rate,
-- latency percentiles. Pruned after 90 days by checkpoint_expiry.
CREATE TABLE IF NOT EXISTS action_calls (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    action_id           uuid REFERENCES actions(id) ON DELETE SET NULL,
    action_name         text NOT NULL,
    agent_id            uuid REFERENCES agents(id) ON DELETE SET NULL,
    conversation_id     uuid REFERENCES conversations(id) ON DELETE CASCADE,
    message_id          uuid,

    -- Plain uuid, no FK: procedure_runs does not exist until 010, which adds the
    -- constraint. A high-volume ledger also should not block run pruning.
    procedure_run_id    uuid,

    status              text NOT NULL,
    http_status         integer,
    duration_ms         integer,
    request_bytes       integer,
    response_bytes      integer,
    error_code          text,
    error_message       text,
    ts                  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT action_calls_status_valid
        CHECK (status IN ('ok', 'error', 'timeout', 'blocked', 'refused',
                          'quota_exhausted', 'identity_required',
                          'identity_incomplete', 'awaiting_client',
                          'awaiting_confirmation', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS action_calls_action_ts_idx
    ON action_calls (tenant_id, action_id, ts DESC);
CREATE INDEX IF NOT EXISTS action_calls_conversation_idx
    ON action_calls (tenant_id, conversation_id, ts DESC);
CREATE INDEX IF NOT EXISTS action_calls_ts_idx ON action_calls (ts);
CREATE INDEX IF NOT EXISTS action_calls_procedure_run_idx
    ON action_calls (procedure_run_id) WHERE procedure_run_id IS NOT NULL;

-- --------------------------------------------------- action_suspensions

-- Durable await state. Holds BOTH kinds of pause: a client action waiting for the
-- browser to answer, and a write action waiting for the visitor to confirm.
-- resume_state exists because the turn resumes on a different machine after
-- scale-to-zero. Expired by checkpoint_expiry.
CREATE TABLE IF NOT EXISTS action_suspensions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    agent_id        uuid REFERENCES agents(id) ON DELETE SET NULL,
    action_id       uuid REFERENCES actions(id) ON DELETE SET NULL,
    action_name     text NOT NULL,
    kind            text NOT NULL,
    call_id         text NOT NULL,
    arguments       jsonb NOT NULL DEFAULT '{}'::jsonb,
    resume_state    jsonb NOT NULL DEFAULT '{}'::jsonb,
    status          text NOT NULL DEFAULT 'pending',
    result          jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    resolved_at     timestamptz,
    expires_at      timestamptz NOT NULL,

    CONSTRAINT action_suspensions_kind_valid
        CHECK (kind IN ('client_action', 'confirmation')),
    CONSTRAINT action_suspensions_status_valid
        CHECK (status IN ('pending', 'resolved', 'declined', 'expired', 'cancelled')),
    CONSTRAINT action_suspensions_call_unique
        UNIQUE (conversation_id, call_id)
);

-- At most one pending suspension per conversation: the turn is blocked on it.
CREATE UNIQUE INDEX IF NOT EXISTS action_suspensions_one_pending_per_conversation
    ON action_suspensions (tenant_id, conversation_id)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS action_suspensions_expiry_idx
    ON action_suspensions (expires_at) WHERE status = 'pending';

-- ----------------------------------------------- messages.actions column

-- Transcript annotation: "called lookup_order", "showed a button". Additive.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS actions jsonb;

-- ------------------------------------------- messages.role admits 'tool'

-- §0.3 V4 / "Could not verify" flag 3, now verified: 002_documents.sql:105 declares
--     CONSTRAINT messages_role_valid CHECK (role IN ('user', 'assistant', 'system'))
-- which forbids a tool-result turn outright, so the messages.actions column alone
-- is not enough. Widened here to admit 'tool'. Drop-then-add rather than a second
-- constraint, so there is exactly one role vocabulary. Existing rows all carry one
-- of the three original values and therefore satisfy the wider constraint, so the
-- re-add validates without a rewrite.
ALTER TABLE messages DROP CONSTRAINT IF EXISTS messages_role_valid;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'messages'::regclass
           AND conname = 'messages_role_valid'
    ) THEN
        ALTER TABLE messages
            ADD CONSTRAINT messages_role_valid
            CHECK (role IN ('user', 'assistant', 'system', 'tool'));
    END IF;
END $$;

-- ------------------------------------------------------------------ RLS

-- Same shape as 001-006: ENABLE + FORCE, and one tenant_isolation policy bound to
-- current_tenant_id(). FORCE applies the policy to the table owner too, which is
-- what stops a superuser style app role from silently bypassing isolation.
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['actions', 'action_secrets', 'agent_actions',
                             'action_calls', 'action_suspensions']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I '
            'USING (tenant_id = current_tenant_id()) '
            'WITH CHECK (tenant_id = current_tenant_id())', t);
    END LOOP;
END $$;
