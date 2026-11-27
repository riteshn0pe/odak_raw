-- Growth platform core spine: the per-tenant log DB every growth agent reads
-- from and writes to, the settings that configure them, the human-in-the-loop
-- approval ledger, and the staff-owned per-agent model routing table.
--
-- Names carry a growth_ prefix on purpose. The excalidraw called these tables
-- leads / messages / agent_actions / suppression / tenant_integrations, and all
-- five of those names are already taken by shipped, unrelated tables: contacts
-- and messages (002) are conversation turns, agent_actions (009) is the
-- agent-to-action enablement join, email_suppressions (008) is the email
-- channel's own list, whatsapp_integrations (002) is the Meta connection.
-- Nothing existing is renamed; the new concepts get distinct names.
--
-- No down path, as with every migration here. Forward only.

-- ---------------------------------------------------------------------------
-- growth_settings: one row per tenant, the configuration every agent reads.
--
-- Every knob is JSONB rather than a column per setting. These are read as a
-- whole by the agents and edited as a whole by the settings screen, they gain
-- fields every stage, and none of them is ever a query predicate — so a column
-- per knob would be nine migrations of churn buying nothing. The shapes are
-- validated in app/services/growth/profile.py, which is where a bad value is
-- rejected with a message a human can read.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS growth_settings (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- sells, audience, price_range, icp, website, competitors[], keywords[],
    -- tone, offer_min, offer_max, tenant_type (A | B | both)
    profile       JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- per-feature approval level, 0 automatic / 1 per batch / 2 per message
    hitl_levels   JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- daily email cap, warmup day, working hours, timezone
    send_caps     JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- follow-up day offsets and the maximum number of them
    follow_up     JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- the discount/offer band an agent may propose inside, owner set
    offer_range   JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- allowed reply languages; the resolver never leaves this list
    languages     JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- per-agent groundedness toggle and mode (advisory | block)
    grounding     JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- weekly / monthly / on-demand report switches
    report_prefs  JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- customer-facing channel preference order, e.g. ["whatsapp", "email"]
    channel_order JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT growth_settings_one_per_tenant UNIQUE (tenant_id)
);

-- ---------------------------------------------------------------------------
-- growth_integrations: the generic provider-connection registry.
--
-- One row per (tenant, provider, external_ref). The secret itself never lands
-- here: secret_ref points into tenant_secrets, whose ciphertext is the only
-- place a credential exists at rest. external_ref is the non-secret handle a
-- human recognises — a store id, a phone_number_id, a sending domain.
--
-- external_ref is NOT NULL DEFAULT '' rather than nullable because it is part
-- of the uniqueness key, and in Postgres two NULLs are distinct: a nullable
-- column would let the same provider be connected twice with no ref.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS growth_integrations (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    provider     TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'not_connected',
    external_ref TEXT NOT NULL DEFAULT '',
    secret_ref   TEXT,
    -- last check, last error, provider quality rating
    health       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT growth_integrations_unique UNIQUE (tenant_id, provider, external_ref),
    CONSTRAINT growth_integrations_status_valid CHECK (
        status IN ('not_connected', 'pending', 'verifying', 'active', 'error', 'planned')
    )
);
CREATE INDEX IF NOT EXISTS growth_integrations_tenant_provider_idx
    ON growth_integrations (tenant_id, provider);

-- ---------------------------------------------------------------------------
-- growth_leads: one row per company/person the tenant may sell to, whether
-- found by the research agent or arriving inbound.
--
-- uid is the dedup key: a sha256 over the normalised (phone, email, domain)
-- triple computed by app/domain/growth.py lead_uid(). The same company found
-- twice, spelled "+91 98…" once and "098…" the next time, produces one uid and
-- therefore one row that gets merged rather than duplicated.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS growth_leads (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    uid              TEXT NOT NULL,
    company          TEXT NOT NULL DEFAULT '',
    person           TEXT NOT NULL DEFAULT '',
    role             TEXT NOT NULL DEFAULT '',
    email            TEXT NOT NULL DEFAULT '',
    phone            TEXT NOT NULL DEFAULT '',
    domain           TEXT NOT NULL DEFAULT '',
    source           TEXT NOT NULL DEFAULT '',
    research_summary TEXT NOT NULL DEFAULT '',
    research_links   JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- resolved reply language, NULL until the lead has written to us
    language         TEXT,
    status           TEXT NOT NULL DEFAULT 'new',
    owner_notes      TEXT NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT growth_leads_uid_unique UNIQUE (tenant_id, uid),
    CONSTRAINT growth_leads_status_valid CHECK (
        status IN ('new', 'researched', 'queued', 'mailed', 'replied', 'positive',
                   'meeting', 'won', 'lost', 'later', 'suppressed')
    )
);
CREATE INDEX IF NOT EXISTS growth_leads_tenant_status_idx
    ON growth_leads (tenant_id, status, updated_at DESC);
-- every period-bounded read of this table filters on created_at and nothing
-- else: the report's "leads found this week" and its by-source split
-- (app/services/growth/reports/queries.py _funnel), the six-month monthly
-- buckets, and the Stage 5 own-numbers watcher. The status index above cannot
-- serve them — status is its leading column and none of those queries names a
-- status — so without this they are a full scan of the tenant's whole history
-- once an hour, per tenant, forever.
CREATE INDEX IF NOT EXISTS growth_leads_tenant_created_idx
    ON growth_leads (tenant_id, created_at);
-- the reply router matches an inbound sender against these two, so both are
-- partial: a lead with no email is not a candidate for an email reply
CREATE INDEX IF NOT EXISTS growth_leads_tenant_email_idx
    ON growth_leads (tenant_id, email)
 WHERE email <> '';
CREATE INDEX IF NOT EXISTS growth_leads_tenant_phone_idx
    ON growth_leads (tenant_id, phone)
 WHERE phone <> '';

-- ---------------------------------------------------------------------------
-- growth_messages: the outreach log. Customer-facing channels only.
--
-- cost_micros is a denormalised mirror for per-thread display. usage_events
-- stays the authoritative billing source: reports read this table, billing
-- reads the rollups, and the two are never mixed.
--
-- Idempotency is the partial unique index on provider_id: a webhook redelivery
-- carries the same provider id and collides instead of writing a second row.
-- Rows we have not sent yet have no provider id, hence the partial predicate.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS growth_messages (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    lead_id     UUID REFERENCES growth_leads(id) ON DELETE CASCADE,
    channel     TEXT NOT NULL,
    direction   TEXT NOT NULL,
    content     TEXT NOT NULL DEFAULT '',
    -- the language this one message was written in; reports break reply rate
    -- down by it, so it is per message and not only per lead
    language    TEXT,
    template_id TEXT,
    provider_id TEXT,
    status      TEXT NOT NULL DEFAULT 'queued',
    campaign_id UUID,
    cost_micros BIGINT NOT NULL DEFAULT 0,
    -- when the scheduler may send this row; NULL means "as soon as due"
    send_after  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT growth_messages_channel_valid CHECK (channel IN ('email', 'whatsapp')),
    CONSTRAINT growth_messages_direction_valid CHECK (direction IN ('out', 'in')),
    CONSTRAINT growth_messages_status_valid CHECK (
        status IN ('queued', 'sent', 'delivered', 'opened', 'replied',
                   'bounced', 'complained', 'failed')
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS growth_messages_provider_unique
    ON growth_messages (tenant_id, channel, provider_id)
 WHERE provider_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS growth_messages_tenant_lead_idx
    ON growth_messages (tenant_id, lead_id, created_at DESC);
-- the dispatcher's hot query: this tenant's due outbound work, oldest first
CREATE INDEX IF NOT EXISTS growth_messages_tenant_queued_idx
    ON growth_messages (tenant_id, created_at)
 WHERE status = 'queued' AND direction = 'out';
-- the analytics predicate, which is a *window* and not a queue: every report
-- section reads this table as "tenant, created_at between start and end" and
-- then groups by direction, channel, status or language
-- (app/services/growth/reports/queries.py _funnel / _daily_trend /
-- _deliverability / _by_language / _reply_time), and Stage 5's own-numbers
-- watcher reads the same shape daily. The three indexes above are all partial
-- or lead with lead_id, so none of them can serve it; this is the only one that
-- keeps a weekly report proportional to the week rather than to the tenant's
-- entire message history.
CREATE INDEX IF NOT EXISTS growth_messages_tenant_created_idx
    ON growth_messages (tenant_id, created_at);

-- ---------------------------------------------------------------------------
-- growth_approvals: the HITL ledger.
--
-- Deliberately not an extension of escalations (007). An escalation row is
-- notification-only — pending -> notified | failed | suppressed, no decision,
-- no levels, no expiry. An approval is a decision with a held batch behind it:
-- pending -> approved | rejected | edited | expired. Different state machine,
-- different table, beside escalation rather than inside it.
--
-- level is snapshotted at creation, not read back from growth_settings at
-- decision time: an owner who lowers the level mid-flight must not retroactively
-- auto-approve a batch a human was already asked about.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS growth_approvals (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    batch_id   UUID NOT NULL,
    feature    TEXT NOT NULL,
    level      INTEGER NOT NULL DEFAULT 1,
    payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
    status     TEXT NOT NULL DEFAULT 'pending',
    decided_by TEXT,
    decided_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT growth_approvals_status_valid CHECK (
        status IN ('pending', 'approved', 'rejected', 'expired', 'edited')
    ),
    CONSTRAINT growth_approvals_level_valid CHECK (level BETWEEN 0 AND 2)
);
-- the panel lists pending newest first; the expiry sweep scans pending by
-- deadline. One partial index on the live rows serves both, and stays small
-- however many decided rows accumulate behind it.
CREATE INDEX IF NOT EXISTS growth_approvals_pending_idx
    ON growth_approvals (tenant_id, expires_at)
 WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS growth_approvals_tenant_created_idx
    ON growth_approvals (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS growth_approvals_batch_idx
    ON growth_approvals (tenant_id, batch_id);

-- ---------------------------------------------------------------------------
-- growth_agent_actions: the audit trail. One row per agent decision.
--
-- BIGSERIAL, like usage_events and audit_log: append-only, high volume, never
-- referenced by another row. Logs are for operations; this table is the audit,
-- and neither replaces the other.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS growth_agent_actions (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent       TEXT NOT NULL,
    action      TEXT NOT NULL,
    input       JSONB NOT NULL DEFAULT '{}'::jsonb,
    output      JSONB NOT NULL DEFAULT '{}'::jsonb,
    reason      TEXT NOT NULL DEFAULT '',
    cost_micros BIGINT NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS growth_agent_actions_tenant_ts_idx
    ON growth_agent_actions (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS growth_agent_actions_tenant_agent_ts_idx
    ON growth_agent_actions (tenant_id, agent, created_at DESC);

-- ---------------------------------------------------------------------------
-- growth_suppressions: cross-channel do-not-contact.
--
-- email_suppressions (008) stays exactly as it is — it is the email channel's
-- own provider-driven list. This table is the cross-channel one, and the consent
-- gate checks both. address is the normalised form from app/domain/growth.py, so
-- a STOP from "+91 98765 43210" also blocks "09876543210".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS growth_suppressions (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    channel    TEXT NOT NULL,
    address    TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT growth_suppressions_unique UNIQUE (tenant_id, channel, address)
);

-- ---------------------------------------------------------------------------
-- growth_consents: the positive side of the same ledger.
--
-- revoked_at is what makes an opt-in expire without deleting the proof that it
-- once existed — which is the whole point of holding proof at all.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS growth_consents (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    channel     TEXT NOT NULL,
    address     TEXT NOT NULL,
    opted_in_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source      TEXT NOT NULL DEFAULT '',
    proof       JSONB,
    revoked_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT growth_consents_unique UNIQUE (tenant_id, channel, address)
);
CREATE INDEX IF NOT EXISTS growth_consents_live_idx
    ON growth_consents (tenant_id, channel, address)
 WHERE revoked_at IS NULL;

-- ---------------------------------------------------------------------------
-- agent_model_config: which LLM backend and model each agent runs on.
--
-- tenant_id NULL is the platform default row; a non-null tenant_id is that
-- tenant's override. This is the one table here whose policy is not the plain
-- tenant_isolation form, and the exception is deliberate and load-bearing:
--
--   USING (tenant_id = current_tenant_id() OR tenant_id IS NULL)
--
-- Under admin_scope() no app.tenant_id is set, so current_tenant_id() is NULL
-- and only the platform rows are visible and writable — exactly the staff
-- surface for setting a default. Under tenant_scope(T) the tenant's own rows
-- are visible plus the platform rows, which is precisely what llm_for() needs
-- to resolve override -> default -> container fallback in one query. A tenant
-- can never see another tenant's override, and the WITH CHECK repeats the
-- predicate so a tenant-scoped session cannot write a row for anyone else.
--
-- Staff writes reach a tenant override by opening tenant_scope(tenant_id) for
-- that one write; the admin auth gate in front of the route is what makes it
-- staff-only. RLS is the isolation boundary, not the authorisation one.
--
-- role maps onto the existing two-tier split: 'primary' is container.llm,
-- 'utility' is container.utility_llm.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_model_config (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID REFERENCES tenants(id) ON DELETE CASCADE,
    agent_key  TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT 'primary',
    backend    TEXT NOT NULL,
    model      TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT agent_model_config_role_valid CHECK (role IN ('primary', 'utility')),
    CONSTRAINT agent_model_config_backend_valid CHECK (
        backend IN ('gemini', 'openai', 'anthropic', 'openrouter', 'nvidia')
    )
);
-- two unique indexes, not one constraint: UNIQUE (tenant_id, agent_key, role)
-- would not constrain the platform rows at all, because NULL is distinct from
-- NULL in a unique index and every platform row would compare unequal to every
-- other. The pair below gives both halves a real uniqueness guarantee.
CREATE UNIQUE INDEX IF NOT EXISTS agent_model_config_tenant_unique
    ON agent_model_config (tenant_id, agent_key, role)
 WHERE tenant_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS agent_model_config_platform_unique
    ON agent_model_config (agent_key, role)
 WHERE tenant_id IS NULL;

-- ---------------------------------------------------------------------------
-- Row level security. FORCE applies the policy to the table owner too, which is
-- what stops the app role — which owns these tables — from bypassing isolation.
-- ---------------------------------------------------------------------------
ALTER TABLE growth_settings      ENABLE ROW LEVEL SECURITY;
ALTER TABLE growth_settings      FORCE ROW LEVEL SECURITY;
ALTER TABLE growth_integrations  ENABLE ROW LEVEL SECURITY;
ALTER TABLE growth_integrations  FORCE ROW LEVEL SECURITY;
ALTER TABLE growth_leads         ENABLE ROW LEVEL SECURITY;
ALTER TABLE growth_leads         FORCE ROW LEVEL SECURITY;
ALTER TABLE growth_messages      ENABLE ROW LEVEL SECURITY;
ALTER TABLE growth_messages      FORCE ROW LEVEL SECURITY;
ALTER TABLE growth_approvals     ENABLE ROW LEVEL SECURITY;
ALTER TABLE growth_approvals     FORCE ROW LEVEL SECURITY;
ALTER TABLE growth_agent_actions ENABLE ROW LEVEL SECURITY;
ALTER TABLE growth_agent_actions FORCE ROW LEVEL SECURITY;
ALTER TABLE growth_suppressions  ENABLE ROW LEVEL SECURITY;
ALTER TABLE growth_suppressions  FORCE ROW LEVEL SECURITY;
ALTER TABLE growth_consents      ENABLE ROW LEVEL SECURITY;
ALTER TABLE growth_consents      FORCE ROW LEVEL SECURITY;
ALTER TABLE agent_model_config   ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_model_config   FORCE ROW LEVEL SECURITY;

DO $$
DECLARE
    target TEXT;
BEGIN
    FOREACH target IN ARRAY ARRAY['growth_settings', 'growth_integrations',
                                  'growth_leads', 'growth_messages',
                                  'growth_approvals', 'growth_agent_actions',
                                  'growth_suppressions', 'growth_consents']
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', target);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_tenant_id()) '
            'WITH CHECK (tenant_id = current_tenant_id())', target);
    END LOOP;
END $$;

-- the sanctioned exception, reasoned about above the table
DROP POLICY IF EXISTS tenant_isolation ON agent_model_config;
CREATE POLICY tenant_isolation ON agent_model_config
    USING (tenant_id = current_tenant_id() OR tenant_id IS NULL)
    WITH CHECK (tenant_id = current_tenant_id() OR tenant_id IS NULL);

-- ---------------------------------------------------------------------------
-- Per-feature cost visibility.
--
-- The recording never stops: every growth usage_events row stamps meta.feature
-- whatever this flag says. The flag only decides whether the tenant's own
-- Billing screen breaks the total down, which is a support and pricing decision
-- staff make per tenant, not a tenant preference.
-- ---------------------------------------------------------------------------
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS feature_cost_visible BOOLEAN NOT NULL DEFAULT false;

-- ---------------------------------------------------------------------------
-- usage_rollups gains the feature dimension.
--
-- NOT NULL DEFAULT '' rather than nullable, and the unique key widened to carry
-- it. A nullable column would break the fold outright: run_rollup upserts on
-- ON CONFLICT (tenant_id, day, kind), and in a unique index NULL is distinct
-- from NULL — every pre-growth row would stop deduplicating and the daily total
-- would grow a new row per rollup tick. '' is the pre-growth / untagged bucket
-- and existing rows land in it without a backfill pass.
-- ---------------------------------------------------------------------------
ALTER TABLE usage_rollups
    ADD COLUMN IF NOT EXISTS feature TEXT NOT NULL DEFAULT '';

ALTER TABLE usage_rollups DROP CONSTRAINT IF EXISTS usage_rollups_unique;
ALTER TABLE usage_rollups
    ADD CONSTRAINT usage_rollups_unique UNIQUE (tenant_id, day, kind, feature);

CREATE INDEX IF NOT EXISTS usage_rollups_tenant_feature_day_idx
    ON usage_rollups (tenant_id, feature, day DESC)
 WHERE feature <> '';
