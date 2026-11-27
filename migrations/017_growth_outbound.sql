-- Growth platform, Part A: the outbound loop's own state and the inbound
-- loop's raw archive.
--
-- Three tables, and each one exists because a growth_* table from 016 is the
-- wrong place for it:
--
--   research_runs    — one row per search, holding the *freshness* clock. Leads
--                      are deduped by uid in growth_leads; what growth_leads
--                      cannot answer is "did we already pay a provider to look
--                      this company up recently", and that question is what
--                      stops a re-run from re-billing Apollo.
--   growth_wa_templates — Meta-approved WhatsApp template names per tenant. The
--                      24-hour window means an outbound WhatsApp message
--                      outside a live conversation must name an approved
--                      template; whatsapp_integrations (002) holds the
--                      connection, not the tenant's template catalogue.
--   growth_lead_raw  — every inbound payload, verbatim, written before parsing.
--                      A parse failure then has evidence attached instead of
--                      being a log line about a body nobody kept.
--
-- No down path, as with every migration here. Forward only.

-- ---------------------------------------------------------------------------
-- research_runs: what the research agent has already looked at, and when.
--
-- `results` keeps the raw provider/search payload the run was built from, so a
-- summary that reads oddly can be traced to what it was written from without
-- re-paying for the search. `lead_ids` is the run's output, as an array rather
-- than a join table: a run is read whole or not at all, it is never queried
-- "which runs mention this lead", and a five-row join table per search would be
-- the most-written table in the schema for no query anyone makes.
--
-- `subject` is the freshness key — the normalised domain (or, with no site, the
-- normalised company name). The partial index below is the agent's one hot
-- query: "is there a row for this subject younger than 30 days".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_runs (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    query      TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT '',
    -- the company this run was about, normalised. '' for a broad search that
    -- was not about one company, which is why it is not part of a unique key
    subject    TEXT NOT NULL DEFAULT '',
    results    JSONB NOT NULL DEFAULT '{}'::jsonb,
    lead_ids   UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- the freshness check: one indexed lookup per candidate company, per run
CREATE INDEX IF NOT EXISTS research_runs_tenant_subject_idx
    ON research_runs (tenant_id, subject, created_at DESC)
 WHERE subject <> '';
CREATE INDEX IF NOT EXISTS research_runs_tenant_created_idx
    ON research_runs (tenant_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- growth_wa_templates: the tenant's Meta-approved message templates.
--
-- Meta approves a template by (name, language) and rejects anything else sent
-- outside the 24-hour service window, so the sender has to know the tenant's
-- catalogue before it composes. `status` mirrors Meta's own vocabulary rather
-- than being normalised into ours: when a template is rejected, the owner is
-- going to read Meta's reason in Meta's words, and a translated status makes
-- that conversation harder.
--
-- `category` decides the price of the conversation the template opens, which is
-- why it is stored and stamped onto the usage event rather than guessed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS growth_wa_templates (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    language     TEXT NOT NULL DEFAULT 'en',
    category     TEXT NOT NULL DEFAULT 'utility',
    -- the approved body, with {{1}}-style placeholders, kept so the panel can
    -- show the owner what will actually be sent
    body         TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending',
    -- Meta's own template id, when it has issued one
    external_ref TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT growth_wa_templates_unique UNIQUE (tenant_id, name, language),
    CONSTRAINT growth_wa_templates_category_valid CHECK (
        category IN ('utility', 'marketing', 'authentication')
    ),
    CONSTRAINT growth_wa_templates_status_valid CHECK (
        status IN ('pending', 'approved', 'rejected', 'paused', 'disabled')
    )
);
-- No status index: the only lookup is by (name, language) with status as a
-- filter, and the unique constraint above already serves that. The catalogue is
-- a handful of rows per tenant, so a second index would be write cost with no
-- reader.

-- ---------------------------------------------------------------------------
-- growth_lead_raw: every inbound payload, before anything interprets it.
--
-- Written first, parsed second, and never updated. `parse_error` is filled in
-- by the job when interpretation fails, so a malformed form post is a visible
-- row with the body attached rather than a 500 in a log nobody kept. `lead_id`
-- is set when parsing succeeded, which makes "which raw payload produced this
-- lead" answerable during a support conversation.
--
-- `dedupe_key` is the provider's own idempotency handle where it has one. The
-- partial unique index is what makes a redelivered webhook one lead instead of
-- two — the same posture as growth_messages.provider_id.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS growth_lead_raw (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    dedupe_key  TEXT,
    lead_id     UUID REFERENCES growth_leads(id) ON DELETE SET NULL,
    parse_error TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS growth_lead_raw_dedupe_unique
    ON growth_lead_raw (tenant_id, source, dedupe_key)
 WHERE dedupe_key IS NOT NULL;
-- No further index on this table on purpose. It is written on every inbound
-- enquiry and read only by primary key, so a "recent payloads" and a "what failed
-- to parse today" index would both be write cost with no reader. They land with
-- the ops screen that asks those questions, not before it.

-- ---------------------------------------------------------------------------
-- Row level security. FORCE applies the policy to the table owner too, which is
-- what stops the app role — which owns these tables — from bypassing isolation.
-- Identical to 016; growth_leads keeps the policy it already has.
-- ---------------------------------------------------------------------------
ALTER TABLE research_runs        ENABLE ROW LEVEL SECURITY;
ALTER TABLE research_runs        FORCE ROW LEVEL SECURITY;
ALTER TABLE growth_wa_templates  ENABLE ROW LEVEL SECURITY;
ALTER TABLE growth_wa_templates  FORCE ROW LEVEL SECURITY;
ALTER TABLE growth_lead_raw      ENABLE ROW LEVEL SECURITY;
ALTER TABLE growth_lead_raw      FORCE ROW LEVEL SECURITY;

DO $$
DECLARE
    target TEXT;
BEGIN
    FOREACH target IN ARRAY ARRAY['research_runs', 'growth_wa_templates',
                                  'growth_lead_raw']
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', target);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_tenant_id()) '
            'WITH CHECK (tenant_id = current_tenant_id())', target);
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- growth_leads gains the columns the outbound cadence and the inbox read.
--
-- Last in the file, and that placement is the point. This is the only statement
-- here that touches a table which may already hold rows, and `scripts/migrate.py`
-- runs each migration inside one transaction — so the ACCESS EXCLUSIVE lock an
-- ALTER takes is held until the whole file commits, not until the ALTER
-- finishes. Putting it last is what makes that hold as short as it can be under
-- this runner. (A genuinely non-blocking index build needs CREATE INDEX
-- CONCURRENTLY, which cannot run inside a transaction block at all.)
--
-- Today the point is moot: migration 016 has not been applied anywhere, so
-- growth_leads is empty when this runs. The ordering is for the next time.
--
-- All four columns are a deliberate denormalisation. "Which leads are due a
-- follow-up" and "which conversations changed most recently" would otherwise be
-- correlated aggregates over the message log — on every dispatch tick and every
-- inbox page load respectively. These columns turn both into indexed range
-- scans. growth_messages stays the record of what was actually said.
--
-- `paused_until` is how an out-of-office reply is honoured: the cadence stops
-- until the date they gave, rather than the lead being marked lost.
--
-- `follow_up_count INT NOT NULL DEFAULT 0` is a metadata-only change: the
-- default is a non-volatile constant, so Postgres 11+ rewrites no rows.
-- ---------------------------------------------------------------------------
ALTER TABLE growth_leads
    ADD COLUMN IF NOT EXISTS follow_up_count INT NOT NULL DEFAULT 0;
ALTER TABLE growth_leads
    ADD COLUMN IF NOT EXISTS last_outbound_at TIMESTAMPTZ;
ALTER TABLE growth_leads
    ADD COLUMN IF NOT EXISTS paused_until TIMESTAMPTZ;
-- maintained on every growth_messages insert, in either direction. Without it
-- the inbox has to evaluate a per-lead lateral subquery for every lead the
-- tenant has ever messaged before it can order and take one page
ALTER TABLE growth_leads
    ADD COLUMN IF NOT EXISTS last_message_at TIMESTAMPTZ;

-- the follow-up agent's hot query: mailed leads, oldest contact first
CREATE INDEX IF NOT EXISTS growth_leads_followup_due_idx
    ON growth_leads (tenant_id, last_outbound_at)
 WHERE status = 'mailed';

-- the inbox's hot query: this tenant's conversations, newest activity first.
-- Partial, because a lead nobody has ever messaged has no conversation to list
CREATE INDEX IF NOT EXISTS growth_leads_last_message_idx
    ON growth_leads (tenant_id, last_message_at DESC)
 WHERE last_message_at IS NOT NULL;
