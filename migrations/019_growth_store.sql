-- Growth platform, Stage 4: store connections + Part C minimal.
--
-- Three tables:
--
--   growth_customers — one row per person the tenant has sold to (or imported),
--                      keyed by the same normalised-identity uid the leads use.
--                      The store (Shopify/WooCommerce) and the CSV import are
--                      three doors into the same table; dedup is by uid, so the
--                      same person arriving from two doors merges, never doubles.
--   growth_orders    — one row per store order, keyed by
--                      (tenant, store_provider, external_id). Attribution reads
--                      this table: a campaign's discount code (and UTM) is
--                      matched against the order's items payload.
--   growth_campaigns — one row per campaign draft through send + results.
--                      trigger is exactly owner | insight | event; status walks
--                      draft -> pending_approval -> approved -> sending -> done,
--                      with rejected as the owner's no.
--
-- growth_wa_templates already exists (migration 017) and is reused unchanged:
-- the campaign's WhatsApp text is submitted as a template row there and the
-- send waits for Meta's approval, which is a status flip on that row.
--
-- No down path, as with every migration here. Forward only.

-- ---------------------------------------------------------------------------
-- growth_customers: the tenant's buyers.
--
-- `uid` is the same sha256-over-normalised-triple the leads use
-- (app/domain/growth.py lead_uid), so a buyer who was once a lead resolves to
-- the same identity logic. `consent` is per channel ({"whatsapp": true, ...}),
-- recorded at collection: the store checkout tick-box, the CSV's opt-in
-- column. The senders re-check growth_consents/growth_suppressions at send
-- time; this column is what the segment builder and the channel picker read.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS growth_customers (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    uid         TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    phone       TEXT NOT NULL DEFAULT '',
    email       TEXT NOT NULL DEFAULT '',
    tags        JSONB NOT NULL DEFAULT '[]'::jsonb,
    consent     JSONB NOT NULL DEFAULT '{}'::jsonb,
    language    TEXT,
    first_order_at TIMESTAMPTZ,
    last_order_at  TIMESTAMPTZ,
    total_spend_micros BIGINT NOT NULL DEFAULT 0,
    order_count INT NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT growth_customers_uid_unique UNIQUE (tenant_id, uid)
);
CREATE INDEX IF NOT EXISTS growth_customers_tenant_orders_idx
    ON growth_customers (tenant_id, last_order_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS growth_customers_tenant_email_idx
    ON growth_customers (tenant_id, email)
 WHERE email <> '';
CREATE INDEX IF NOT EXISTS growth_customers_tenant_phone_idx
    ON growth_customers (tenant_id, phone)
 WHERE phone <> '';

-- ---------------------------------------------------------------------------
-- growth_orders: the money. One row per store order.
--
-- `items` carries the line items verbatim plus the attribution handles the
-- store gave us (discount_codes array, utm/source tags) — kept as JSONB
-- because each provider names them differently and a column per provider's
-- vocabulary would be a migration per vendor. `total_micros` is micros:
-- millionths of a *major* currency unit (1 rupee = 1_000_000), the same unit
-- cost_micros carries everywhere else — not paise/cents. An earlier version of
-- this comment said minor units, and the store adapters were written to it and
-- stored every order ten times too small.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS growth_orders (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    customer_id    UUID REFERENCES growth_customers(id) ON DELETE SET NULL,
    external_id    TEXT NOT NULL DEFAULT '',
    store_provider TEXT NOT NULL DEFAULT '',
    total_micros   BIGINT NOT NULL DEFAULT 0,
    payment        TEXT NOT NULL DEFAULT 'prepaid',
    status         TEXT NOT NULL DEFAULT '',
    items          JSONB NOT NULL DEFAULT '{}'::jsonb,
    ordered_at     TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT growth_orders_external_unique UNIQUE (tenant_id, store_provider, external_id),
    CONSTRAINT growth_orders_payment_valid CHECK (payment IN ('prepaid', 'cod'))
);
CREATE INDEX IF NOT EXISTS growth_orders_tenant_customer_idx
    ON growth_orders (tenant_id, customer_id, ordered_at DESC);
CREATE INDEX IF NOT EXISTS growth_orders_tenant_ordered_idx
    ON growth_orders (tenant_id, ordered_at DESC);

-- ---------------------------------------------------------------------------
-- growth_campaigns: the Part C minimal ledger.
--
-- `segment` is the frozen audience definition at draft time
-- ({"key": "at_risk", "params": {...}, "member_ids": [...], "count": N}):
-- frozen, because a segment computed at send time would silently change size
-- between the owner's approval and the send. `offer` is inside the owner-set
-- range by construction ({"discount_pct": N, "code": "..."}). `channels` is a
-- TEXT[] of customer-facing channels only (email | whatsapp).
-- `discount_code` is the unique per-campaign code created on the store;
-- `messages` is the frozen per-language copy the planner wrote
-- ({"en": "...", "hi": "..."}): frozen, because re-rendering at send time
-- could produce different words than the owner approved. The send reads these;
-- `results` is filled after the send ({"sent": N, ...}).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS growth_campaigns (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    trigger     TEXT NOT NULL DEFAULT 'owner',
    name        TEXT NOT NULL DEFAULT '',
    segment     JSONB NOT NULL DEFAULT '{}'::jsonb,
    offer       JSONB NOT NULL DEFAULT '{}'::jsonb,
    channels    TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    status      TEXT NOT NULL DEFAULT 'draft',
    approval_id UUID REFERENCES growth_approvals(id) ON DELETE SET NULL,
    discount_code TEXT,
    messages    JSONB NOT NULL DEFAULT '{}'::jsonb,
    results     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT growth_campaigns_trigger_valid CHECK (
        trigger IN ('owner', 'insight', 'event')
    ),
    CONSTRAINT growth_campaigns_status_valid CHECK (
        status IN ('draft', 'pending_approval', 'approved', 'sending', 'done', 'rejected')
    )
);
CREATE INDEX IF NOT EXISTS growth_campaigns_tenant_status_idx
    ON growth_campaigns (tenant_id, status, created_at DESC);

-- ---------------------------------------------------------------------------
-- growth_messages gains the campaign recipient link.
--
-- Campaigns write to *customers*, not leads: the B2B lead ladder
-- (new -> ... -> won) must never see a D2C buyer, and stuffing a customer id
-- into lead_id would violate the FK to growth_leads. So one nullable column;
-- a row carries exactly one of lead_id / customer_id, and the senders resolve
-- whichever is set. Last in the file so the ALTER's lock is held shortest
-- (same reasoning as 017's growth_leads ALTER).
-- ---------------------------------------------------------------------------
ALTER TABLE growth_messages
    ADD COLUMN IF NOT EXISTS customer_id UUID REFERENCES growth_customers(id) ON DELETE CASCADE;
CREATE INDEX IF NOT EXISTS growth_messages_tenant_customer_idx
    ON growth_messages (tenant_id, customer_id, created_at DESC)
 WHERE customer_id IS NOT NULL;

-- Exactly one recipient per row. Every writer already obeys it — the cold mail,
-- follow-up, inbound-ack and reply paths set lead_id, the campaign queuer sets
-- customer_id and passes lead_id=None — and the senders' `_lead` resolver
-- refuses a row carrying neither. Without the constraint that refusal happens at
-- send time, on a row already queued and already counted against the daily cap;
-- with it, the bug is a failed insert next to the code that caused it. Both
-- columns stay nullable: which one is set is the B2B/D2C distinction, and
-- num_nonnulls is what says "one of them, not both".
ALTER TABLE growth_messages DROP CONSTRAINT IF EXISTS growth_messages_recipient_valid;
ALTER TABLE growth_messages
    ADD CONSTRAINT growth_messages_recipient_valid
    CHECK (num_nonnulls(lead_id, customer_id) = 1);

-- growth_messages.campaign_id has been an unconstrained UUID since 016, because
-- growth_campaigns did not exist yet. It does now, and this is the migration
-- that owns it: without the reference a deleted campaign leaves its queued rows
-- pointing at nothing, and the release job's resume set
-- (`campaign_customer_ids`) would re-queue an audience it already wrote.
-- SET NULL rather than CASCADE — the message was really sent, and deleting the
-- campaign must not delete the record of what went to a customer.
ALTER TABLE growth_messages DROP CONSTRAINT IF EXISTS growth_messages_campaign_fk;
ALTER TABLE growth_messages
    ADD CONSTRAINT growth_messages_campaign_fk
    FOREIGN KEY (campaign_id) REFERENCES growth_campaigns(id) ON DELETE SET NULL;

-- and the index that FK now needs, plus the three Stage 4 queries that read by
-- campaign: the release job's resume set, `campaign_stats` for the results
-- panel, and `cancel_queued_for_campaign` behind a rejection. None of the 016
-- indexes leads with campaign_id, so all three were scanning the tenant's whole
-- message log.
CREATE INDEX IF NOT EXISTS growth_messages_tenant_campaign_idx
    ON growth_messages (tenant_id, campaign_id)
 WHERE campaign_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Row level security. FORCE applies the policy to the table owner too, which is
-- what stops the app role — which owns these tables — from bypassing isolation.
-- Identical to 016/017/018.
-- ---------------------------------------------------------------------------
ALTER TABLE growth_customers ENABLE ROW LEVEL SECURITY;
ALTER TABLE growth_customers FORCE ROW LEVEL SECURITY;
ALTER TABLE growth_orders    ENABLE ROW LEVEL SECURITY;
ALTER TABLE growth_orders    FORCE ROW LEVEL SECURITY;
ALTER TABLE growth_campaigns ENABLE ROW LEVEL SECURITY;
ALTER TABLE growth_campaigns FORCE ROW LEVEL SECURITY;

DO $$
DECLARE
    target TEXT;
BEGIN
    FOREACH target IN ARRAY ARRAY['growth_customers', 'growth_orders',
                                  'growth_campaigns']
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', target);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_tenant_id()) '
            'WITH CHECK (tenant_id = current_tenant_id())', target);
    END LOOP;
END $$;
