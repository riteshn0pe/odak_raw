-- Telegram as a customer support channel, not only the owner's remote control.
--
-- Three things this migration does, none of which adds a new customer-facing
-- table beyond the receipts:
--
-- 1. The conversations CHECK from 002 (last widened by 008 for email) grows
--    'telegram'. A customer's chat with the default agent is a conversation
--    like a WhatsApp one: same runner, same transcript, same retrieval — only
--    the transport differs.
--
-- 2. The escalations CHECK from 007 (widened by 008 for email) grows
--    'telegram' too. A telegram conversation can hit the same escalate-to-human
--    triggers as any other channel (low confidence, ungrounded, "I want a
--    person"), and a row the service cannot write would be a silently
--    unescalatable conversation — the worst failure mode the escalation
--    design has.
--
-- 3. `telegram_inbound_receipts`, the replay guard, shaped after
--    whatsapp_inbound_receipts (003). The webhook reserves a message id and
--    returns 200; a worker runs the agent and marks the receipt processed or
--    failed. Telegram redelivers an unacknowledged update, so the guard is
--    what keeps one customer message from becoming two answers.
--
-- No down path, as with every migration here. Forward only.

-- ---------------------------------------------------------------------------
-- conversations + escalations: widen the channel CHECKs
-- ---------------------------------------------------------------------------
--
-- Drop-and-recreate because Postgres has no ADD CONSTRAINT IF NOT EXISTS. This
-- validates every existing row under an ACCESS EXCLUSIVE lock; at this table's
-- size that is milliseconds (the same reasoning 008 records). On a large table
-- use ADD ... NOT VALID followed by VALIDATE CONSTRAINT instead.

ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_channel_valid;
ALTER TABLE conversations
    ADD CONSTRAINT conversations_channel_valid
    CHECK (channel IN ('widget', 'whatsapp', 'api', 'email', 'telegram'));

ALTER TABLE escalations DROP CONSTRAINT IF EXISTS escalations_channel_valid;
ALTER TABLE escalations
    ADD CONSTRAINT escalations_channel_valid
    CHECK (channel IN ('widget', 'whatsapp', 'api', 'email', 'telegram'));

-- ---------------------------------------------------------------------------
-- telegram_inbound_receipts
-- ---------------------------------------------------------------------------
--
-- One successful insert reserves a message for processing; a retry conflicts
-- with the tenant-local unique key and therefore cannot run twice.
--
-- `message_id` is stored as "<chat_id>:<message_id>" because Telegram's
-- message ids are unique per chat, not per bot: customer A's message 7 and
-- customer B's message 7 are different messages, and a bare id would make the
-- second of them look like a replay of the first. The composite is built by
-- the repository, never by a caller, so the two halves can never disagree on
-- the spelling.

CREATE TABLE IF NOT EXISTS telegram_inbound_receipts (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    message_id   TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'reserved',
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    error        TEXT,
    CONSTRAINT telegram_inbound_receipts_message_unique UNIQUE (tenant_id, message_id),
    CONSTRAINT telegram_inbound_receipts_state_valid
        CHECK (state IN ('reserved', 'processed', 'failed'))
);
CREATE INDEX IF NOT EXISTS telegram_inbound_receipts_tenant_state_idx
    ON telegram_inbound_receipts (tenant_id, state, received_at);

-- ---------------------------------------------------------------------------
-- Row level security. Table + ENABLE + FORCE + policy, all four, every time
-- (the pattern every tenant table in this schema follows — see 024 and 026).
-- ---------------------------------------------------------------------------

ALTER TABLE telegram_inbound_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE telegram_inbound_receipts FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    DROP POLICY IF EXISTS tenant_isolation ON telegram_inbound_receipts;
    CREATE POLICY tenant_isolation ON telegram_inbound_receipts
        USING (tenant_id = current_tenant_id())
        WITH CHECK (tenant_id = current_tenant_id());
END $$;
