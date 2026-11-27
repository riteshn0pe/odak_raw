-- Inbound email as a channel: one watched address per tenant agent, replay
-- receipts, thread continuity, and delivery/bounce state.
--
-- Shaped after 003_whatsapp.sql on purpose. email_channels.address is the
-- inbound routing key and is UNIQUE platform-wide, the same role and the same
-- constraint as whatsapp_integrations.phone_number_id — routing has to resolve
-- before any tenant context exists, so the lookup runs under admin_scope and
-- the uniqueness is what makes it unambiguous.

-- The conversations CHECK from 002 has to be widened before any email
-- conversation can be written. Drop-and-recreate because Postgres has no
-- ADD CONSTRAINT IF NOT EXISTS. This validates every existing row under an
-- ACCESS EXCLUSIVE lock; at this table's size that is milliseconds. On a large
-- table use ADD ... NOT VALID followed by VALIDATE CONSTRAINT instead.
ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_channel_valid;
ALTER TABLE conversations
    ADD CONSTRAINT conversations_channel_valid
    CHECK (channel IN ('widget', 'whatsapp', 'api', 'email'));

CREATE TABLE IF NOT EXISTS email_channels (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent_id           UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,

    -- The address we watch: <slug>-<8 hex>@<EMAIL_INBOUND_DOMAIN>. The random
    -- suffix is not decoration: a guessable <slug>@ address lets anyone mail a
    -- tenant's agent directly, bypassing the tenant's own inbox and filters.
    address            TEXT NOT NULL UNIQUE,
    -- The tenant's real support address. Used as Reply-To at stage 0 so the
    -- customer's reply returns through the tenant's own inbox, and as the
    -- From address once their domain is verified.
    forward_from       TEXT,
    -- Stage 1: verified sending domain. NULL means send from our domain with
    -- Reply-To set, because From: their-domain without their DKIM fails DMARC.
    sending_domain     TEXT,
    sending_domain_verified_at TIMESTAMPTZ,
    display_name       TEXT,

    -- Optional pin on who may forward to us, once the tenant knows their
    -- forwarder. Empty array = accept any sender.
    allowed_forwarders TEXT[] NOT NULL DEFAULT '{}',

    ingest_attachments BOOLEAN NOT NULL DEFAULT false,
    link_sources       BOOLEAN NOT NULL DEFAULT false,
    -- Notify the tenant's escalation destination on this many auto-sent
    -- replies, then stop. Bounded audit-in-the-loop for a newly trusted
    -- mailbox; nobody should learn what the bot has been saying a month later.
    audit_sends_remaining INTEGER NOT NULL DEFAULT 20,

    status             TEXT NOT NULL DEFAULT 'active',
    error_reason       TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    disconnected_at    TIMESTAMPTZ,

    CONSTRAINT email_channels_status_valid
        CHECK (status IN ('pending', 'active', 'error', 'disconnected')),
    CONSTRAINT email_channels_audit_sends_nonneg
        CHECK (audit_sends_remaining >= 0)
);
-- Composite unique so child tables can carry a (tenant_id, channel_id) FK, the
-- same trick 003 uses for whatsapp_integrations: the FK then cannot point at
-- another tenant's channel even if application code gets a parameter wrong.
CREATE UNIQUE INDEX IF NOT EXISTS email_channels_tenant_id_idx
    ON email_channels (tenant_id, id);
CREATE INDEX IF NOT EXISTS email_channels_tenant_status_idx
    ON email_channels (tenant_id, status);

-- One successful insert reserves an inbound Message-ID. A retry conflicts on
-- the tenant-local unique key and therefore cannot be processed twice. Read-
-- then-write would race under concurrent webhook redelivery.
CREATE TABLE IF NOT EXISTS email_inbound_receipts (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    channel_id   UUID NOT NULL,
    -- RFC 5322 Message-ID, or provider:<id> when the sender omitted one
    message_id   TEXT NOT NULL,
    provider_id  TEXT,
    state        TEXT NOT NULL DEFAULT 'reserved',
    -- why a message was dropped without an answer: auto_submitted, bulk,
    -- list_mail, bounce_sender, self_addressed, loop_header, spam
    ignored_reason TEXT,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    error        TEXT,
    CONSTRAINT email_inbound_receipts_message_unique UNIQUE (tenant_id, message_id),
    CONSTRAINT email_inbound_receipts_channel_fk
        FOREIGN KEY (tenant_id, channel_id)
        REFERENCES email_channels (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT email_inbound_receipts_state_valid
        CHECK (state IN ('reserved', 'processed', 'ignored', 'failed'))
);
CREATE INDEX IF NOT EXISTS email_inbound_receipts_tenant_channel_idx
    ON email_inbound_receipts (tenant_id, channel_id, received_at DESC);
CREATE INDEX IF NOT EXISTS email_inbound_receipts_tenant_state_idx
    ON email_inbound_receipts (tenant_id, state, received_at);

-- One row per email thread, mapping RFC 5322 threading onto a conversation.
CREATE TABLE IF NOT EXISTS email_threads (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    channel_id        UUID NOT NULL,
    conversation_id   UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,

    -- Salted per-tenant hash of the participant address, identical treatment to
    -- a WhatsApp sender: the same person writing to two tenants must not
    -- collide, because that would leak that they contacted both.
    external_user_ref TEXT NOT NULL,
    -- The address itself, encrypted, because replying needs it and nothing
    -- queries it. participant_masked is what list views render.
    participant_ciphertext TEXT NOT NULL,
    participant_masked     TEXT NOT NULL,

    subject_normalised TEXT NOT NULL DEFAULT '',
    -- every Message-ID seen or sent in this thread. Inbound resolution matches
    -- In-Reply-To and each References entry against this, which is what keeps
    -- threading working when a client drops In-Reply-To but keeps References.
    message_ids        TEXT[] NOT NULL DEFAULT '{}',
    last_message_id    TEXT,
    reply_seq          INTEGER NOT NULL DEFAULT 0,
    agent_reply_count  INTEGER NOT NULL DEFAULT 0,
    -- false after a hard bounce: never send here again
    deliverable        BOOLEAN NOT NULL DEFAULT true,

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_inbound_at   TIMESTAMPTZ,
    last_outbound_at  TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT email_threads_conversation_unique UNIQUE (tenant_id, conversation_id),
    CONSTRAINT email_threads_channel_fk
        FOREIGN KEY (tenant_id, channel_id)
        REFERENCES email_channels (tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS email_threads_tenant_participant_idx
    ON email_threads (tenant_id, channel_id, external_user_ref, updated_at DESC);
-- GIN over the id array turns thread resolution into an index scan rather than
-- a scan of every thread the tenant has ever had.
CREATE INDEX IF NOT EXISTS email_threads_message_ids_idx
    ON email_threads USING GIN (message_ids);
CREATE INDEX IF NOT EXISTS email_threads_tenant_subject_idx
    ON email_threads (tenant_id, channel_id, subject_normalised, updated_at DESC);

-- Delivery, bounce, and complaint callbacks. Duplicated and out-of-order like
-- every provider's, so the repository upserts and only advances state.
CREATE TABLE IF NOT EXISTS email_delivery_receipts (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    channel_id         UUID NOT NULL,
    provider_id        TEXT NOT NULL,
    recipient_masked   TEXT,
    status             TEXT NOT NULL DEFAULT 'sent',
    bounce_type        TEXT,
    error              TEXT,
    provider_timestamp TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT email_delivery_receipts_provider_unique UNIQUE (tenant_id, provider_id),
    CONSTRAINT email_delivery_receipts_channel_fk
        FOREIGN KEY (tenant_id, channel_id)
        REFERENCES email_channels (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT email_delivery_receipts_status_valid
        CHECK (status IN ('queued', 'sent', 'delivered', 'bounced',
                          'complained', 'failed'))
);
CREATE INDEX IF NOT EXISTS email_delivery_receipts_tenant_status_idx
    ON email_delivery_receipts (tenant_id, status, updated_at DESC);

-- Addresses we must never write to again for this tenant: hard bounces and
-- spam complaints. Kept per tenant, not platform-wide, because one tenant's
-- unknown-user is another tenant's valid customer.
CREATE TABLE IF NOT EXISTS email_suppressions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    address_hash  TEXT NOT NULL,
    address_masked TEXT NOT NULL,
    reason        TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT email_suppressions_unique UNIQUE (tenant_id, address_hash),
    CONSTRAINT email_suppressions_reason_valid
        CHECK (reason IN ('hard_bounce', 'complaint', 'manual'))
);

ALTER TABLE email_channels          ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_channels          FORCE  ROW LEVEL SECURITY;
ALTER TABLE email_inbound_receipts  ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_inbound_receipts  FORCE  ROW LEVEL SECURITY;
ALTER TABLE email_threads           ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_threads           FORCE  ROW LEVEL SECURITY;
ALTER TABLE email_delivery_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_delivery_receipts FORCE  ROW LEVEL SECURITY;
ALTER TABLE email_suppressions      ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_suppressions      FORCE  ROW LEVEL SECURITY;

DO $$
DECLARE
    target TEXT;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'email_channels', 'email_inbound_receipts', 'email_threads',
        'email_delivery_receipts', 'email_suppressions'
    ]
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', target);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_tenant_id()) '
            'WITH CHECK (tenant_id = current_tenant_id())', target);
    END LOOP;
END $$;
