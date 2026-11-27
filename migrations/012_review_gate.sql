-- A crawl can park for human approval before anything is indexed.
--
-- Until now every website source ran to completion on its own: discover, fetch,
-- extract, embed. The tenant saw the URL list only afterwards, from the review
-- screen, by which point every page was already chunked, embedded and retrievable.
-- Removing a junk page there is a deletion, not a decision.
--
-- `awaiting_review` is the state a gated crawl stops in once its frontier is
-- fetched and before a single page is trained. The rows are `fetched`, their raw
-- bytes are in storage, and nothing has cost an embedding yet. The tenant approves
-- the list and the crawl resumes into `extracting`; whatever they removed first is
-- an `excluded` row with reason `review_excluded` and is never trained.
--
-- Deliberately NOT added to `sources_in_flight_idx`. That index feeds the stall
-- sweeper, whose job is restarting waves that crashed without re-enqueueing
-- themselves. A parked crawl has not crashed — it is waiting for a person, and
-- restarting it would be the one thing that defeats the gate. Same reasoning for
-- `sources_retrain_due_idx`: it already matches only ('ready', 'failed'), so
-- auto-retrain cannot pick up a source whose list nobody has approved.
--
-- `sources_crawl_state_valid` shipped in 006 with seven values. 006 is applied and
-- is never edited; this is the forward-only replacement, character-exact against
-- the original apart from the one added value.
--
-- Drop-and-recreate because Postgres has no ADD CONSTRAINT IF NOT EXISTS — the
-- same shape as the escalation trigger CHECK widened in 011. Recreating validates
-- every existing row under an ACCESS EXCLUSIVE lock; at this table's size that is
-- milliseconds, and every existing value is already in the new list, so no row can
-- fail it. On a large table use ADD ... NOT VALID followed by VALIDATE CONSTRAINT.

ALTER TABLE sources DROP CONSTRAINT IF EXISTS sources_crawl_state_valid;
ALTER TABLE sources
    ADD CONSTRAINT sources_crawl_state_valid
        CHECK (crawl_state IN ('idle', 'discovering', 'fetching', 'extracting',
                               'awaiting_review', 'ready', 'failed', 'cancelled'));

-- When a human last approved this source's URL list.
--
-- This, not the config flag, is what the engine tests. The flag says "this source
-- wants a gate"; the timestamp says "the gate has been passed". Keeping them apart
-- is what lets an approved crawl resume without the flag being flipped off — the
-- setting survives, so the next retrain gates again, which is the point of asking
-- for review in the first place.
--
-- NULL means never approved: a fresh source, or one whose retrain cleared it.
ALTER TABLE sources
    ADD COLUMN IF NOT EXISTS review_approved_at TIMESTAMPTZ;

-- The review screen groups the frontier by host and by path prefix, and it counts
-- every group server side rather than paging the whole frontier into the browser.
-- Both the grouping query and the "show me this section" query filter on
-- (source_id, state) and then sort or split on url, which the existing
-- source_links_source_state_idx cannot serve past its first column.
CREATE INDEX IF NOT EXISTS source_links_source_state_url_idx
    ON source_links (source_id, state, url);
