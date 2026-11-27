-- 010_procedures_contacts.sql
-- Procedures (agent-scoped multi-step SOPs), their runs and step history,
-- contacts, identity verifiers, and the durable JWT replay ledger.
-- No down path.
--
-- pg_trgm: already created by 001_tenancy.sql:7, so contacts_email_trgm_idx below
-- has its operator class and this file does not re-declare the extension.
--


-- ------------------------------------------------------------- procedures

-- agent_id NOT NULL: a procedure's steps name actions that must be enabled on
-- THAT agent, and its trigger competes with that agent's other triggers.
-- Cross-agent reuse is a copy, not a link.
CREATE TABLE IF NOT EXISTS procedures (
    id                              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent_id                        uuid NOT NULL REFERENCES agents(id) ON DELETE CASCADE,

    name                            text NOT NULL,
    trigger_text                    text NOT NULL,

    -- ProcedureDefinition.steps (Pydantic). Nested: a branch step carries its
    -- branches, each with instruction-only children. Depth capped at 1.
    steps                           jsonb NOT NULL DEFAULT '[]'::jsonb,

    -- Union of action ids resolved from @name tokens at publish time. Lets the
    -- per-turn availability check be one array comparison.
    required_actions                uuid[] NOT NULL DEFAULT ARRAY[]::uuid[],

    -- Sensitive-matter procedures send routing fields only, not the transcript.
    redact_transcript_on_escalation boolean NOT NULL DEFAULT false,

    -- Optional embedding pre-filter (PROCEDURE_SHORTLIST_MODE='embedding').
    -- double precision[] rather than pgvector: <=200 vectors, compared in Python.
    trigger_embedding               double precision[],
    trigger_embedding_model         text,

    enabled                         boolean NOT NULL DEFAULT false,
    version                         integer NOT NULL DEFAULT 1,
    library_key                     text,
    last_run_at                     timestamptz,

    created_by                      uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at                      timestamptz NOT NULL DEFAULT now(),
    updated_at                      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT procedures_name_unique
        UNIQUE (tenant_id, agent_id, name),
    CONSTRAINT procedures_trigger_length
        CHECK (char_length(trigger_text) BETWEEN 10 AND 400),
    CONSTRAINT procedures_steps_is_array
        CHECK (jsonb_typeof(steps) = 'array'),
    -- 15 instruction steps is enforced across the whole tree by the Pydantic
    -- validator; this is the cheap top-level backstop.
    CONSTRAINT procedures_steps_bounded
        CHECK (jsonb_array_length(steps) <= 15)
);

-- Ordering for the trigger catalogue: most recently used first.
CREATE INDEX IF NOT EXISTS procedures_agent_enabled_idx
    ON procedures (tenant_id, agent_id, last_run_at DESC NULLS LAST)
 WHERE enabled;

-- --------------------------------------------------------- procedure_runs

CREATE TABLE IF NOT EXISTS procedure_runs (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent_id            uuid REFERENCES agents(id) ON DELETE SET NULL,
    conversation_id     uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    procedure_id        uuid REFERENCES procedures(id) ON DELETE SET NULL,

    -- Denormalised so a run stays readable after a rename or a delete.
    procedure_name      text NOT NULL,
    procedure_version   integer NOT NULL,

    -- Flattened step list as it was at run start. A few KB. Makes the run
    -- immune to mid-run edits and readable after the procedure is deleted.
    steps_snapshot      jsonb NOT NULL DEFAULT '[]'::jsonb,

    status              text NOT NULL DEFAULT 'active',
    outcome             text,
    ended_reason        text,

    -- step_path is the label a human reads: '3 > otherwise > 3.3'.
    step_index          integer NOT NULL DEFAULT 0,
    step_path           text NOT NULL DEFAULT '1',
    current_sid         text,

    -- {"order_id": {"value": "4471", "source": "customer", "at": "..."}}
    -- Provenance is the point: a lead must see that order_total came from the
    -- action and not from the customer's claim.
    variables           jsonb NOT NULL DEFAULT '{}'::jsonb,

    -- Append-only decision trail, one entry per decision point, recording
    -- whether code or the model decided and why.
    branch_log          jsonb NOT NULL DEFAULT '[]'::jsonb,

    turn_count          integer NOT NULL DEFAULT 0,
    action_calls        integer NOT NULL DEFAULT 0,
    bookkeeping_calls   integer NOT NULL DEFAULT 0,

    -- Deliberately a plain uuid with no FK: escalations lives in 007_escalation.sql
    -- and a handed-off run must stay readable after an escalation is pruned. The
    -- spec block declares it exactly this way.
    escalation_id       uuid,

    started_at          timestamptz NOT NULL DEFAULT now(),
    last_activity_at    timestamptz NOT NULL DEFAULT now(),
    ended_at            timestamptz,
    expires_at          timestamptz NOT NULL,

    CONSTRAINT procedure_runs_status_valid
        CHECK (status IN ('active', 'awaiting_user', 'resolved', 'handed_off',
                          'abandoned', 'failed', 'superseded')),
    CONSTRAINT procedure_runs_outcome_valid
        CHECK (outcome IS NULL
               OR outcome IN ('resolved', 'handed_off', 'abandoned_by_user',
                              'not_applicable')),
    CONSTRAINT procedure_runs_ended_reason_valid
        CHECK (ended_reason IS NULL
               OR ended_reason IN ('completed', 'escalated', 'user_abandoned',
                                   'ttl_expired', 'turn_budget_exhausted',
                                   'action_budget_exhausted',
                                   'required_action_unavailable',
                                   'definition_deleted', 'injection_flagged',
                                   'force_closed'))
);

-- One procedure per conversation at a time.
CREATE UNIQUE INDEX IF NOT EXISTS procedure_runs_one_active_per_conversation
    ON procedure_runs (tenant_id, conversation_id)
 WHERE status IN ('active', 'awaiting_user');

-- Every Performance-tab number comes off this index. No denormalised counters.
CREATE INDEX IF NOT EXISTS procedure_runs_procedure_started_idx
    ON procedure_runs (tenant_id, procedure_id, started_at DESC);
CREATE INDEX IF NOT EXISTS procedure_runs_expiry_idx
    ON procedure_runs (expires_at) WHERE status IN ('active', 'awaiting_user');
CREATE INDEX IF NOT EXISTS procedure_runs_conversation_idx
    ON procedure_runs (tenant_id, conversation_id, started_at DESC);

-- 009 declared action_calls.procedure_run_id without a FK because this table
-- did not exist yet. Add it now; SET NULL so run pruning never blocks the ledger.
-- ADD CONSTRAINT has no IF NOT EXISTS in postgres, so it is guarded the same way
-- 003_whatsapp.sql:12-33 guards its added constraints.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'action_calls'::regclass
           AND conname = 'action_calls_procedure_run_fk'
    ) THEN
        ALTER TABLE action_calls
            ADD CONSTRAINT action_calls_procedure_run_fk
            FOREIGN KEY (procedure_run_id)
            REFERENCES procedure_runs(id) ON DELETE SET NULL;
    END IF;
END $$;

-- --------------------------------------------------- procedure_run_steps

-- The run timeline the dashboard renders. A table rather than a bounded array on
-- the run row: this is what support reads most, it grows on a long run, and a
-- bounded array would truncate the middle of exactly the run being investigated.
CREATE TABLE IF NOT EXISTS procedure_run_steps (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    run_id       uuid NOT NULL REFERENCES procedure_runs(id) ON DELETE CASCADE,
    ordinal      integer NOT NULL,
    step_path    text NOT NULL,
    -- The rendered text the model was actually shown, variables interpolated.
    step_text    text NOT NULL,
    action_name  text,
    outcome      text NOT NULL,
    note         text,
    at           timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT procedure_run_steps_outcome_valid
        CHECK (outcome IN ('done', 'skipped_unavailable', 'asked_instead',
                           'action_ok', 'action_failed', 'branch_taken',
                           'escalated', 'aborted')),
    CONSTRAINT procedure_run_steps_unique UNIQUE (run_id, ordinal)
);

CREATE INDEX IF NOT EXISTS procedure_run_steps_run_idx
    ON procedure_run_steps (run_id, ordinal);

-- ---------------------------------------------------------------- contacts

CREATE TABLE IF NOT EXISTS contacts (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- The tenant's own user id, in plaintext. The ONLY place the raw identifier
    -- lives. Tenant-scoped and RLS-protected; support must be able to search it.
    external_id         text,

    -- Plaintext: must be searchable in the dashboard and interpolatable into
    -- action URLs. RLS is the boundary, not encryption. The tenant already
    -- receives these unmasked in escalations.
    email               text,
    phone               text,
    name                text,

    -- Flat, string values only. Validator: <=25 keys, key ^[a-z][a-z0-9_]{0,30}$,
    -- value <=200 chars. These are interpolated into URL paths; nested objects
    -- and long values produce malformed requests.
    attributes          jsonb NOT NULL DEFAULT '{}'::jsonb,

    source              text NOT NULL DEFAULT 'api',
    verified_at         timestamptz,

    -- Hashed pseudonyms this contact has also browsed as, capped at 20 by the
    -- service. Lets support trace "also appeared as X" without raw ids.
    external_user_refs  text[] NOT NULL DEFAULT ARRAY[]::text[],

    last_seen_at        timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT contacts_external_id_unique UNIQUE (tenant_id, external_id),
    CONSTRAINT contacts_identified
        CHECK (external_id IS NOT NULL OR email IS NOT NULL OR phone IS NOT NULL),
    CONSTRAINT contacts_source_valid
        CHECK (source IN ('identity_jwt', 'collect_lead', 'collect_data',
                          'whatsapp', 'email', 'dashboard', 'api')),
    CONSTRAINT contacts_attributes_is_object
        CHECK (jsonb_typeof(attributes) = 'object')
);

CREATE INDEX IF NOT EXISTS contacts_email_idx ON contacts (tenant_id, lower(email))
    WHERE email IS NOT NULL;
CREATE INDEX IF NOT EXISTS contacts_phone_idx ON contacts (tenant_id, phone)
    WHERE phone IS NOT NULL;
-- pg_trgm comes from 001_tenancy.sql:7. Substring search on email needs the
-- trigram opclass; the lower(email) btree above covers exact lookup.
CREATE INDEX IF NOT EXISTS contacts_email_trgm_idx
    ON contacts USING gin (email gin_trgm_ops);
CREATE INDEX IF NOT EXISTS contacts_last_seen_idx
    ON contacts (tenant_id, last_seen_at DESC);

-- ---------------------------------------------------------- contact_events

CREATE TABLE IF NOT EXISTS contact_events (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    contact_id      uuid NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    conversation_id uuid REFERENCES conversations(id) ON DELETE SET NULL,
    kind            text NOT NULL,
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    at              timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT contact_events_kind_valid
        CHECK (kind IN ('identity_verified', 'lead_collected', 'data_collected',
                        'attributes_updated', 'merge_declined', 'exported',
                        'erased'))
);

CREATE INDEX IF NOT EXISTS contact_events_contact_idx
    ON contact_events (contact_id, at DESC);

-- ----------------------------------------------------- identity_verifiers

-- Up to two active rows per (tenant, agent) so a secret can be rotated without
-- a cutover. agent_id NULL means "all agents".
-- HS256 with a secret in tenant_secrets, NOT RS256: RS256 would put a JWKS
-- fetch on the tenant's web server inside the chat hot path.
CREATE TABLE IF NOT EXISTS identity_verifiers (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent_id            uuid REFERENCES agents(id) ON DELETE CASCADE,
    alg                 text NOT NULL DEFAULT 'HS256',
    secret_ref          text NOT NULL,
    issuer              text,
    audience            text,
    max_ttl_seconds     integer NOT NULL DEFAULT 300,
    attribute_allowlist text[],
    last_verified_at    timestamptz,
    last_rejected_at    timestamptz,
    reject_count        integer NOT NULL DEFAULT 0,
    created_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT identity_verifiers_alg_valid CHECK (alg = 'HS256'),
    CONSTRAINT identity_verifiers_ttl_bounded
        CHECK (max_ttl_seconds BETWEEN 30 AND 600),
    CONSTRAINT identity_verifiers_secret_fk
        FOREIGN KEY (tenant_id, secret_ref)
        REFERENCES tenant_secrets (tenant_id, ref) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS identity_verifiers_tenant_idx
    ON identity_verifiers (tenant_id, agent_id);

-- ------------------------------------------------------ identity_jti_seen

-- Durable replay ledger. The cache is the fast path; this is the backstop that
-- makes "fail closed" honest across machines. Swept by checkpoint_expiry.
CREATE TABLE IF NOT EXISTS identity_jti_seen (
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    jti         text NOT NULL,
    expires_at  timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, jti)
);

CREATE INDEX IF NOT EXISTS identity_jti_seen_expiry_idx
    ON identity_jti_seen (expires_at);

-- --------------------------------------------- conversations: identity

-- Existing RLS on conversations is untouched (same approach as 005).
ALTER TABLE conversations
    ADD COLUMN IF NOT EXISTS contact_id uuid REFERENCES contacts(id) ON DELETE SET NULL;
ALTER TABLE conversations
    ADD COLUMN IF NOT EXISTS identity_verified boolean NOT NULL DEFAULT false;
ALTER TABLE conversations
    ADD COLUMN IF NOT EXISTS identity_verified_at timestamptz;

CREATE INDEX IF NOT EXISTS conversations_contact_idx
    ON conversations (tenant_id, contact_id, started_at DESC)
 WHERE contact_id IS NOT NULL;

-- ------------------------------------------------------------------ RLS

-- identity_jti_seen is tenant-keyed and gets RLS like the rest. The procedure
-- and action template galleries are platform data and live in code, not tables,
-- so there is nothing here without RLS.
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['procedures', 'procedure_runs', 'procedure_run_steps',
                             'contacts', 'contact_events', 'identity_verifiers',
                             'identity_jti_seen']
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
