-- documents, chunks, ingestion jobs, conversations, usage. all tenant scoped.

CREATE TABLE IF NOT EXISTS documents (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent_id      UUID REFERENCES agents(id) ON DELETE SET NULL,
    filename      TEXT NOT NULL,
    mime          TEXT NOT NULL,
    size_bytes    BIGINT NOT NULL,
    sha256        TEXT NOT NULL,
    storage_path  TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'upload',
    status        TEXT NOT NULL DEFAULT 'queued',
    error         TEXT,
    page_count    INTEGER NOT NULL DEFAULT 0,
    chunk_count   INTEGER NOT NULL DEFAULT 0,
    token_count   INTEGER NOT NULL DEFAULT 0,
    tier_counts   JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    indexed_at    TIMESTAMPTZ,
    CONSTRAINT documents_status_valid
        CHECK (status IN ('queued', 'processing', 'ready', 'failed', 'deleted'))
);
-- dedup is scoped to the tenant. two tenants uploading the same file are
-- independent, and a global unique would leak that a document already exists
CREATE UNIQUE INDEX IF NOT EXISTS documents_tenant_sha_idx
    ON documents (tenant_id, sha256) WHERE status <> 'deleted';
CREATE INDEX IF NOT EXISTS documents_tenant_status_idx ON documents (tenant_id, status);
CREATE INDEX IF NOT EXISTS documents_tenant_created_idx ON documents (tenant_id, created_at DESC);

-- canonical chunk text. the vector store holds embeddings plus a pointer here,
-- so changing embedding model is a re-embed and never a re-parse
CREATE TABLE IF NOT EXISTS chunks (
    id           TEXT PRIMARY KEY,
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    document_id  UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal      INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    text         TEXT NOT NULL,
    parent_text  TEXT,
    parent_id    TEXT,
    heading_path TEXT[] NOT NULL DEFAULT '{}',
    page_from    INTEGER NOT NULL DEFAULT 0,
    page_to      INTEGER NOT NULL DEFAULT 0,
    token_count  INTEGER NOT NULL DEFAULT 0,
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chunks_tenant_document_idx ON chunks (tenant_id, document_id);
CREATE INDEX IF NOT EXISTS chunks_tenant_parent_idx ON chunks (tenant_id, parent_id);

-- step wise ingestion state machine. one row per document, advanced by queue
-- callbacks so no single request has to finish a large file
CREATE TABLE IF NOT EXISTS document_jobs (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    document_id  UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    step         TEXT NOT NULL DEFAULT 'classify',
    state        TEXT NOT NULL DEFAULT 'pending',
    attempt      INTEGER NOT NULL DEFAULT 0,
    cursor       JSONB NOT NULL DEFAULT '{}'::jsonb,
    error        TEXT,
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id)
);
CREATE INDEX IF NOT EXISTS document_jobs_pending_idx
    ON document_jobs (state, scheduled_at) WHERE state IN ('pending', 'running');

CREATE TABLE IF NOT EXISTS conversations (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent_id          UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    channel           TEXT NOT NULL,
    external_user_ref TEXT,
    summary           TEXT,
    turn_count        INTEGER NOT NULL DEFAULT 0,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_activity     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT conversations_channel_valid CHECK (channel IN ('widget', 'whatsapp', 'api'))
);
CREATE INDEX IF NOT EXISTS conversations_tenant_activity_idx
    ON conversations (tenant_id, last_activity DESC);
-- a returning whatsapp number or identified web user resumes their own thread
CREATE INDEX IF NOT EXISTS conversations_tenant_external_idx
    ON conversations (tenant_id, channel, external_user_ref)
    WHERE external_user_ref IS NOT NULL;

CREATE TABLE IF NOT EXISTS messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    citations       JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence      REAL,
    latency_ms      INTEGER,
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    model           TEXT,
    feedback        TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT messages_role_valid CHECK (role IN ('user', 'assistant', 'system'))
);
CREATE INDEX IF NOT EXISTS messages_conversation_idx
    ON messages (conversation_id, created_at);

-- append only. aggregated by a rollup job, never scanned raw at request time
CREATE TABLE IF NOT EXISTS usage_events (
    id               BIGSERIAL PRIMARY KEY,
    tenant_id        UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    ts               TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind             TEXT NOT NULL,
    quantity         BIGINT NOT NULL,
    unit_cost_micros BIGINT NOT NULL DEFAULT 0,
    meta             JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS usage_events_tenant_ts_idx ON usage_events (tenant_id, ts DESC);
CREATE INDEX IF NOT EXISTS usage_events_tenant_kind_ts_idx
    ON usage_events (tenant_id, kind, ts DESC);

CREATE TABLE IF NOT EXISTS whatsapp_integrations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent_id        UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    waba_id         TEXT NOT NULL,
    phone_number_id TEXT NOT NULL UNIQUE,
    display_number  TEXT,
    token_ref       TEXT,
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE documents             ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents             FORCE ROW LEVEL SECURITY;
ALTER TABLE chunks                ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks                FORCE ROW LEVEL SECURITY;
ALTER TABLE document_jobs         ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_jobs         FORCE ROW LEVEL SECURITY;
ALTER TABLE conversations         ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations         FORCE ROW LEVEL SECURITY;
ALTER TABLE messages              ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages              FORCE ROW LEVEL SECURITY;
ALTER TABLE usage_events          ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_events          FORCE ROW LEVEL SECURITY;
ALTER TABLE whatsapp_integrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE whatsapp_integrations FORCE ROW LEVEL SECURITY;

DO $$
DECLARE
    target TEXT;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'documents', 'chunks', 'document_jobs', 'conversations', 'messages',
        'usage_events', 'whatsapp_integrations'
    ]
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', target);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_tenant_id()) '
            'WITH CHECK (tenant_id = current_tenant_id())', target);
    END LOOP;
END $$;
