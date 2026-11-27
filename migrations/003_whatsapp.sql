-- WhatsApp replay receipts, delivery lifecycle, encrypted integration references, and tenant RLS.

-- whatsapp_integrations originates in 002_documents. Keep the WABA association a
-- mutable column: phone numbers can migrate between WABAs without changing their
-- integration identity or inbound routing key.
ALTER TABLE whatsapp_integrations
    ADD COLUMN IF NOT EXISTS service_window_hours SMALLINT NOT NULL DEFAULT 24,
    ADD COLUMN IF NOT EXISTS template_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS disconnected_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'whatsapp_integrations'::regclass
           AND conname = 'whatsapp_integrations_status_valid'
    ) THEN
        ALTER TABLE whatsapp_integrations
            ADD CONSTRAINT whatsapp_integrations_status_valid
            CHECK (status IN ('pending', 'active', 'error', 'disconnected'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'whatsapp_integrations'::regclass
           AND conname = 'whatsapp_integrations_service_window_valid'
    ) THEN
        ALTER TABLE whatsapp_integrations
            ADD CONSTRAINT whatsapp_integrations_service_window_valid
            CHECK (service_window_hours BETWEEN 1 AND 168);
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS whatsapp_integrations_tenant_id_idx
    ON whatsapp_integrations (tenant_id, id);
CREATE INDEX IF NOT EXISTS whatsapp_integrations_tenant_status_idx
    ON whatsapp_integrations (tenant_id, status);
CREATE INDEX IF NOT EXISTS whatsapp_integrations_tenant_waba_idx
    ON whatsapp_integrations (tenant_id, waba_id);

-- One successful insert reserves a provider message id for processing. A retry
-- conflicts with the tenant-local unique key and therefore cannot run twice.
CREATE TABLE IF NOT EXISTS whatsapp_inbound_receipts (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    integration_id UUID NOT NULL,
    message_id     TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'reserved',
    received_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at   TIMESTAMPTZ,
    error          TEXT,
    CONSTRAINT whatsapp_inbound_receipts_message_unique UNIQUE (tenant_id, message_id),
    CONSTRAINT whatsapp_inbound_receipts_integration_fk
        FOREIGN KEY (tenant_id, integration_id)
        REFERENCES whatsapp_integrations (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT whatsapp_inbound_receipts_state_valid
        CHECK (state IN ('reserved', 'processed', 'failed'))
);
CREATE INDEX IF NOT EXISTS whatsapp_inbound_receipts_tenant_integration_idx
    ON whatsapp_inbound_receipts (tenant_id, integration_id, received_at DESC);
CREATE INDEX IF NOT EXISTS whatsapp_inbound_receipts_tenant_state_idx
    ON whatsapp_inbound_receipts (tenant_id, state, received_at);

-- Delivery callbacks can be duplicated or arrive out of order. The repository
-- upserts this row and only advances successful lifecycle states.
CREATE TABLE IF NOT EXISTS whatsapp_delivery_receipts (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    integration_id     UUID NOT NULL,
    message_id         TEXT NOT NULL,
    recipient_id       TEXT,
    status             TEXT NOT NULL DEFAULT 'sent',
    error_code         TEXT,
    error              TEXT,
    provider_timestamp TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT whatsapp_delivery_receipts_message_unique UNIQUE (tenant_id, message_id),
    CONSTRAINT whatsapp_delivery_receipts_integration_fk
        FOREIGN KEY (tenant_id, integration_id)
        REFERENCES whatsapp_integrations (tenant_id, id) ON DELETE CASCADE,
    CONSTRAINT whatsapp_delivery_receipts_status_valid
        CHECK (status IN ('sent', 'delivered', 'read', 'failed', 'deleted'))
);
CREATE INDEX IF NOT EXISTS whatsapp_delivery_receipts_tenant_integration_idx
    ON whatsapp_delivery_receipts (tenant_id, integration_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS whatsapp_delivery_receipts_tenant_status_idx
    ON whatsapp_delivery_receipts (tenant_id, status, updated_at DESC);

ALTER TABLE whatsapp_inbound_receipts  ENABLE ROW LEVEL SECURITY;
ALTER TABLE whatsapp_inbound_receipts  FORCE ROW LEVEL SECURITY;
ALTER TABLE whatsapp_delivery_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE whatsapp_delivery_receipts FORCE ROW LEVEL SECURITY;

DO $$
DECLARE
    target TEXT;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'whatsapp_inbound_receipts', 'whatsapp_delivery_receipts'
    ]
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', target);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_tenant_id()) '
            'WITH CHECK (tenant_id = current_tenant_id())', target);
    END LOOP;
END $$;
