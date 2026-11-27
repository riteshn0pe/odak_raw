-- Human escalation: the durable record of every time the agent handed a
-- conversation to a person, and the idempotency ledger that keeps a queue
-- redelivery from notifying twice.
--
-- Tenant-scoped, so RLS ENABLE + FORCE + the tenant_isolation policy, exactly
-- like the tables in 002 and 003. The audit_log row for each escalation lives
-- in the platform-scoped table from 004 and is deliberately not duplicated here:
-- the fact is permanent, this payload expires (see the email_retention cron).

CREATE TABLE IF NOT EXISTS escalations (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent_id          UUID REFERENCES agents(id) ON DELETE SET NULL,
    conversation_id   UUID REFERENCES conversations(id) ON DELETE SET NULL,
    message_id        UUID REFERENCES messages(id) ON DELETE SET NULL,

    trigger           TEXT NOT NULL,
    channel           TEXT NOT NULL,
    state             TEXT NOT NULL DEFAULT 'pending',

    -- The unique key is the double-notify guard. It embeds a time bucket
    -- (floor(now / cooldown_seconds)), so the tenant's cooldown window is a
    -- database guarantee rather than a cache-dependent one: evicting Redis
    -- cannot produce a second email for the same conversation and trigger.
    dedupe_key        TEXT NOT NULL,

    confidence        REAL,
    question          TEXT NOT NULL DEFAULT '',
    -- what the bot actually said, so the tenant can see it without opening
    -- the transcript. NULL when nothing was said (crisis, blocklist).
    answer            TEXT,
    -- retrieved-but-insufficient sources: [{filename, page_from, score,
    -- document_id}]. The single most useful field in the row — it tells the
    -- tenant which document was almost right, i.e. which one to fix.
    considered        JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- Visitor contact. Encrypted because it is the one field in this table
    -- that is unambiguously personal data and is not needed for any query.
    -- contact_masked is the display form (ri***@gmail.com).
    contact_ciphertext TEXT,
    contact_masked     TEXT,
    contact_source     TEXT,
    external_user_ref  TEXT,

    page_url          TEXT,
    note              TEXT,
    -- one entry per notifier: [{channel, provider_message_id, delivered, error}]
    receipts          JSONB NOT NULL DEFAULT '[]'::jsonb,

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    notified_at       TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT escalations_dedupe_unique UNIQUE (tenant_id, dedupe_key),
    CONSTRAINT escalations_trigger_valid CHECK (trigger IN (
        'low_confidence', 'ungrounded', 'user_requested', 'negative_feedback',
        'crisis', 'blocklist', 'model_requested', 'email_undeliverable',
        'email_thread_loop'
    )),
    CONSTRAINT escalations_channel_valid
        CHECK (channel IN ('widget', 'whatsapp', 'api', 'email')),
    CONSTRAINT escalations_state_valid
        CHECK (state IN ('pending', 'notified', 'failed', 'suppressed'))
);

CREATE INDEX IF NOT EXISTS escalations_tenant_created_idx
    ON escalations (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS escalations_tenant_state_idx
    ON escalations (tenant_id, state, created_at)
    WHERE state = 'pending';
CREATE INDEX IF NOT EXISTS escalations_tenant_trigger_idx
    ON escalations (tenant_id, trigger, created_at DESC);
CREATE INDEX IF NOT EXISTS escalations_conversation_idx
    ON escalations (tenant_id, conversation_id)
    WHERE conversation_id IS NOT NULL;

ALTER TABLE escalations ENABLE ROW LEVEL SECURITY;
ALTER TABLE escalations FORCE  ROW LEVEL SECURITY;

DO $$
DECLARE
    target TEXT;
BEGIN
    FOREACH target IN ARRAY ARRAY['escalations']
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', target);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_tenant_id()) '
            'WITH CHECK (tenant_id = current_tenant_id())', target);
    END LOOP;
END $$;
