-- Document tags, collections, and per-document metadata — used as retrieval
-- filters on top of the mandatory tenant filter.
--
-- documents originates in 002_documents and already carries RLS (ENABLE + FORCE
-- plus the tenant_isolation policy). ALTER TABLE ... ADD COLUMN leaves that
-- intact, so no policy is re-declared here. Everything is additive with a
-- default, so existing rows and existing ingestion/retrieval behave unchanged.

-- tags: free-form labels, matched by overlap. collection: a single grouping
-- name. metadata already exists from 002 (it also holds parsed_title etc.); it
-- is reused as the per-document bag, so ADD COLUMN IF NOT EXISTS is a no-op there
-- and never clobbers what is already stored.
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS tags       TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS collection TEXT,
    ADD COLUMN IF NOT EXISTS metadata   JSONB NOT NULL DEFAULT '{}'::jsonb;

-- GIN over tags makes an overlap (&&) filter an index scan rather than a table
-- scan; the collection index is tenant-scoped like the other document indexes.
CREATE INDEX IF NOT EXISTS documents_tags_idx ON documents USING GIN (tags);
CREATE INDEX IF NOT EXISTS documents_tenant_collection_idx
    ON documents (tenant_id, collection) WHERE collection IS NOT NULL;
