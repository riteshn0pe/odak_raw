-- Website crawl sources, the crawl frontier, text snippets and Q&A pairs.
--
-- A "source" is one logical thing the tenant added: a crawled site, a sitemap, a
-- set of individual links, a text snippet, or a Q&A set. `documents` remains the
-- unit of ingestion, chunking, indexing, retrieval and citation — a crawled page
-- is an ordinary documents row whose source_id points at its parent. Nothing in
-- DocumentPipeline, Indexer or RetrievalService changes.
--
-- Everything here is additive. Existing documents rows get source_id = NULL and
-- keep byte-level uniqueness under exactly the semantics they have today (see the
-- split of documents_tenant_sha_idx below).

-- ---------------------------------------------------------------- sources

CREATE TABLE IF NOT EXISTS sources (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent_id           UUID REFERENCES agents(id) ON DELETE SET NULL,
    kind               TEXT NOT NULL,
    title              TEXT NOT NULL,
    -- crawl seed or sitemap url. NULL for 'links', 'snippet' and 'qa'
    seed_url           TEXT,
    -- canonical scheme://host the crawl is pinned to, resolved once by following
    -- the seed's redirects. every discovered link is checked against this, so an
    -- open redirect on the seed host cannot widen the crawl's scope later
    origin             TEXT,
    -- include_paths, exclude_paths, max_pages, max_depth, include_subdomains,
    -- include_pdf, delay_ms. validated by CrawlConfig in app/domain/crawl.py
    config             JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- inherited by every child document at creation and re-applied on edit, so a
    -- retrieval facet set on the parent covers the whole site
    tags               TEXT[] NOT NULL DEFAULT '{}',
    collection         TEXT,
    crawl_state        TEXT NOT NULL DEFAULT 'idle',
    error              TEXT,
    -- denormalised aggregates, RECOMPUTED (never incremented) at the end of each
    -- wave from source_links. this is what lets the list API return parents with
    -- child counts in one query instead of N+1, and recomputation is idempotent
    -- so a redelivered wave cannot double-count
    link_total         INTEGER NOT NULL DEFAULT 0,
    link_trained       INTEGER NOT NULL DEFAULT 0,
    link_failed        INTEGER NOT NULL DEFAULT 0,
    link_excluded      INTEGER NOT NULL DEFAULT 0,
    link_pending       INTEGER NOT NULL DEFAULT 0,
    size_bytes         BIGINT  NOT NULL DEFAULT 0,
    chunk_count        INTEGER NOT NULL DEFAULT 0,
    -- fingerprints of blocks that appear on most of this site's pages, i.e. the
    -- boilerplate. computed once from the first sample of fetched pages, frozen
    -- for the rest of the crawl and reused on every recrawl, so extraction stays
    -- single-pass and no page is ever re-indexed to strip a nav bar
    boilerplate        JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- 0 disables auto-retrain. mirrors metadata->>'recrawl_hours' semantics
    auto_retrain_hours INTEGER NOT NULL DEFAULT 0,
    -- set by the cheap discovery probe when a page changed or a new url appeared
    -- but no retrain has run yet. drives the "Retrain available" badge
    pending_changes    BOOLEAN NOT NULL DEFAULT false,
    crawl_started_at   TIMESTAMPTZ,
    -- when extraction last completed with at least one page trained: "Last trained"
    last_trained_at    TIMESTAMPTZ,
    -- when the cheap probe or a retrain last looked, whether or not anything changed
    last_checked_at    TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sources_kind_valid
        CHECK (kind IN ('crawl', 'sitemap', 'links', 'snippet', 'qa')),
    CONSTRAINT sources_crawl_state_valid
        CHECK (crawl_state IN ('idle', 'discovering', 'fetching', 'extracting',
                               'ready', 'failed', 'cancelled')),
    -- a crawl or a sitemap without a seed has nothing to crawl; the other kinds
    -- legitimately have none
    CONSTRAINT sources_seed_present
        CHECK (kind NOT IN ('crawl', 'sitemap') OR seed_url IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS sources_tenant_kind_idx
    ON sources (tenant_id, kind, created_at DESC);
-- the retrain selector's driving index: due sources across all tenants
CREATE INDEX IF NOT EXISTS sources_retrain_due_idx
    ON sources (auto_retrain_hours, last_checked_at)
    WHERE auto_retrain_hours > 0 AND crawl_state IN ('ready', 'failed');
-- the stall sweeper's index: a wave that crashed without re-enqueueing itself
CREATE INDEX IF NOT EXISTS sources_in_flight_idx
    ON sources (updated_at)
    WHERE crawl_state IN ('discovering', 'fetching', 'extracting');

-- ---------------------------------------------------------------- source_links

-- One row per URL this source knows about. This single table is three things at
-- once, deliberately:
--   the crawl frontier   state='pending' rows are the work queue; a wave claims a
--                        slice with FOR UPDATE SKIP LOCKED, which is what makes a
--                        redelivered job resume instead of re-fetching
--   the review list      the Trained / Not found / Excluded groups the tenant sees,
--                        each carrying the reason it landed there
--   the change ledger    etag, last_modified, content_hash and simhash, so a
--                        recrawl costs a 304 rather than a re-parse
CREATE TABLE IF NOT EXISTS source_links (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source_id      UUID NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    -- canonical form, produced by app/domain/crawl.py:canonicalise
    url            TEXT NOT NULL,
    -- sha256 of `url`. the identity of a crawled page is its canonical URL within
    -- its source, NOT its bytes — which is what stops two pages with identical
    -- content from colliding on documents_tenant_sha_idx
    url_hash       TEXT NOT NULL,
    depth          INTEGER NOT NULL DEFAULT 0,
    discovered_via TEXT NOT NULL DEFAULT 'seed',
    state          TEXT NOT NULL DEFAULT 'pending',
    -- machine-readable reason for excluded/failed/gone. this is the string the UI
    -- renders as "why was this skipped", so it is a closed vocabulary, not prose
    reason         TEXT,
    reason_detail  TEXT,
    http_status    INTEGER,
    content_type   TEXT,
    etag           TEXT,
    last_modified  TEXT,
    -- sha256 of the EXTRACTED markdown, not of the raw html: a page whose ads
    -- rotate has stable content and must not look changed
    content_hash   TEXT,
    -- 64-bit simhash of the extracted markdown for near-duplicate detection,
    -- stored signed because postgres has no uint64
    simhash        BIGINT,
    content_bytes  INTEGER NOT NULL DEFAULT 0,
    title          TEXT,
    -- the page's documents row. SET NULL rather than CASCADE so deleting one page
    -- leaves an auditable link row the tenant can see and restore
    document_id    UUID REFERENCES documents(id) ON DELETE SET NULL,
    -- <lastmod> from the sitemap, used to skip unchanged pages before fetching
    lastmod        TIMESTAMPTZ,
    -- raw html between the fetch phase and the extract phase, cleared after
    raw_path       TEXT,
    attempt        INTEGER NOT NULL DEFAULT 0,
    -- lease holder for the pending -> fetching claim, so a crashed wave's rows
    -- can be reclaimed by ttl instead of being stuck forever
    claimed_at     TIMESTAMPTZ,
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    fetched_at     TIMESTAMPTZ,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_id, url_hash),
    CONSTRAINT source_links_state_valid
        CHECK (state IN ('pending', 'fetching', 'fetched', 'trained', 'failed',
                         'excluded', 'unchanged', 'gone'))
);
-- the frontier claim. partial so the index stays the size of the outstanding work
CREATE INDEX IF NOT EXISTS source_links_frontier_idx
    ON source_links (source_id, depth, first_seen_at)
    WHERE state = 'pending';
-- the extract-phase claim
CREATE INDEX IF NOT EXISTS source_links_fetched_idx
    ON source_links (source_id, fetched_at)
    WHERE state = 'fetched';
-- lease reclamation for a wave that died mid-fetch
CREATE INDEX IF NOT EXISTS source_links_claimed_idx
    ON source_links (claimed_at)
    WHERE state IN ('fetching', 'fetched');
-- the review UI's grouping, and the counter recomputation
CREATE INDEX IF NOT EXISTS source_links_state_idx ON source_links (source_id, state);
-- exact content dedupe within a source, before insert
CREATE INDEX IF NOT EXISTS source_links_content_idx
    ON source_links (source_id, content_hash) WHERE content_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS source_links_document_idx
    ON source_links (document_id) WHERE document_id IS NOT NULL;

-- ---------------------------------------------------------------- qa_pairs

-- A Q&A pair is already a chunk, so it does not go through parse/OCR. It still
-- gets a documents row (source='qa') so delete, quota, retrieval facets and
-- citations need no special case; this table holds the authored content, which is
-- what the editor round-trips and what /reindex rebuilds chunks from.
CREATE TABLE IF NOT EXISTS qa_pairs (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source_id    UUID NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    -- one documents row per pair. the citation's filename is the pair's title, so
    -- an answer says "Refund policy" rather than "qa-set-3"
    document_id  UUID REFERENCES documents(id) ON DELETE SET NULL,
    title        TEXT NOT NULL,
    -- every phrasing of the same question. each becomes its own embedded chunk,
    -- all sharing one parent_id whose parent_text is the answer
    questions    TEXT[] NOT NULL,
    answer       TEXT NOT NULL,
    ordinal      INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT qa_pairs_questions_present CHECK (cardinality(questions) > 0)
);
CREATE INDEX IF NOT EXISTS qa_pairs_source_idx ON qa_pairs (source_id, ordinal);
CREATE INDEX IF NOT EXISTS qa_pairs_document_idx
    ON qa_pairs (document_id) WHERE document_id IS NOT NULL;

-- ---------------------------------------------------------------- documents

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS source_id UUID REFERENCES sources(id) ON DELETE SET NULL,
    -- the page's own URL, so a citation can open the live page rather than a
    -- signed blob link. NULL for uploads
    ADD COLUMN IF NOT EXISTS source_url TEXT;

CREATE INDEX IF NOT EXISTS documents_source_idx
    ON documents (source_id) WHERE source_id IS NOT NULL;

-- Split the content-hash unique index.
--
-- Two crawled pages that render identical text — a paginated archive, a print
-- view, an /about vs /about-us alias — have the same sha256, and the second
-- INSERT would fail against the old index. That is not a rare edge case; it
-- happens on most real sites.
--
-- The fix is to narrow the domain rather than relax the guarantee: bytes still
-- identify an upload or a single-URL document (source_id IS NULL), while a
-- crawled page is identified by its canonical URL within its source
-- (source_links' UNIQUE (source_id, url_hash)). Content-level duplicates among
-- crawled pages are caught in the crawler before insert and land in the
-- "Excluded" group with reason='duplicate_content'.
--
-- Every pre-existing row has source_id = NULL, so the rebuilt index covers
-- exactly the same rows it does today. Upload dedupe behaviour is unchanged.
DROP INDEX IF EXISTS documents_tenant_sha_idx;
CREATE UNIQUE INDEX IF NOT EXISTS documents_tenant_sha_idx
    ON documents (tenant_id, sha256)
    WHERE status <> 'deleted' AND source_id IS NULL;

-- ---------------------------------------------------------------- messages

-- Per-Q&A and per-document "times asked" is derived from which documents an
-- answer actually cited. Without this index that read is a full scan of the
-- tenant's message history; jsonb_path_ops supports the @> containment the query
-- uses and is roughly half the size of the default opclass.
CREATE INDEX IF NOT EXISTS messages_citations_gin
    ON messages USING GIN (citations jsonb_path_ops);

-- ---------------------------------------------------------------- rls

ALTER TABLE sources      ENABLE ROW LEVEL SECURITY;
ALTER TABLE sources      FORCE  ROW LEVEL SECURITY;
ALTER TABLE source_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_links FORCE  ROW LEVEL SECURITY;
ALTER TABLE qa_pairs     ENABLE ROW LEVEL SECURITY;
ALTER TABLE qa_pairs     FORCE  ROW LEVEL SECURITY;

DO $$
DECLARE
    target TEXT;
BEGIN
    FOREACH target IN ARRAY ARRAY['sources', 'source_links', 'qa_pairs']
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', target);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_tenant_id()) '
            'WITH CHECK (tenant_id = current_tenant_id())', target);
    END LOOP;
END $$;
