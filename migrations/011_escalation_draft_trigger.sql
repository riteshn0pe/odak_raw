-- Draft-mode email escalation gets its own trigger value.
--
-- Capability B's `draft` reply mode never writes to the customer: it produces a
-- complete, cited reply and hands it to a human to send. That is the recommended
-- default for a newly connected mailbox, so it is the most common escalation the
-- product creates — and it was recorded as `model_requested`, which says "the
-- agent asked for a human" and is a lie about why the row exists. A tenant
-- reading their escalation list could not tell a healthy draft apart from an
-- answer the model gave up on, and neither could the analytics that group by
-- trigger.
--
-- `escalations_trigger_valid` shipped in 007 with nine values and no `draft`, so
-- it is dropped and recreated with the tenth. 007 is applied and is never edited;
-- this is the forward-only replacement, character-exact against the original
-- apart from the one added value.
--
-- Drop-and-recreate because Postgres has no ADD CONSTRAINT IF NOT EXISTS — the
-- same shape as the conversations CHECK widened in 008. Recreating validates
-- every existing row under an ACCESS EXCLUSIVE lock; at this table's size that is
-- milliseconds, and every existing value is already in the new list, so no row
-- can fail it. On a large table use ADD ... NOT VALID followed by VALIDATE
-- CONSTRAINT instead.
--
-- `draft` is recorded by the system when the reply mode hands a finished answer
-- to a human. It is not a trigger a tenant enables, so it is deliberately absent
-- from DEFAULT_ESCALATION_TRIGGERS in app/domain/agent_config.py.
--
-- No down-path, by project convention. Nothing else in the table changes: the
-- column, the indexes and the RLS policy from 007 all stand.

ALTER TABLE escalations DROP CONSTRAINT IF EXISTS escalations_trigger_valid;
ALTER TABLE escalations
    ADD CONSTRAINT escalations_trigger_valid CHECK (trigger IN (
        'low_confidence', 'ungrounded', 'user_requested', 'negative_feedback',
        'crisis', 'blocklist', 'model_requested', 'email_undeliverable',
        'email_thread_loop', 'draft'
    ));
