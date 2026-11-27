-- Growth platform, Stage 7: the remaining inbound lead sources.
--
-- No new tables. This migration does exactly two things:
--
--   1. A CHECK on growth_leads.source, which until now has been free text.
--      Every value the built code can already write is admitted, plus the two
--      values this stage introduces. The list is the union of the writers
--      verified in code (0.7b): '' (the column default, and a lead the owner
--      creates by hand from the panel PATCH), 'research' and 'web_search'
--      (app/services/growth/research.py — search candidates carry source
--      'web_search', the fallback is 'research'), 'apollo' and 'hunter'
--      (app/adapters/leads/{apollo,hunter}.py — enrichment fills a candidate's
--      empty source with the vendor), 'website_form' (the Stage 2.6 ingress),
--      'justdial' and 'indiamart' (this stage), and 'csv' and 'manual',
--      admitted for the panel's own vocabulary even though no writer asserts
--      them today: the store import writes growth_customers and consent, not
--      leads, and today's PATCH carries the stored value forward rather than
--      asserting one — but the plan's own list names both, and a CHECK that
--      would reject the honest label for a hand-entered or imported lead is a
--      bug waiting to fire.
--
--   2. Source-filter and default-board indexes. Stage 7 adds the source
--      filter itself: the Leads screen now narrows by source, and the repo's
--      list() gains a source clause. (tenant_id, source, updated_at DESC)
--      serves that page exactly the way growth_leads_tenant_status_idx serves
--      the status tabs. The second index closes a gap this migration's own
--      query shape made visible: the default board (no status tab, no source
--      chip) sorts on updated_at alone, and until now no index led with only
--      tenant_id — the most common Leads view was a sort of the tenant's whole
--      history on every page load.
--
-- growth_lead_raw deliberately gains nothing: 017's comment still holds —
-- it is written on every inbound enquiry and read only by primary key, and
-- the parse-failure rows this stage makes visible are surfaced through the
-- integration row's health and the resolve() call, not through an index a
-- query still does not make.
--
-- No down path, as with every migration here. Forward only.

-- ---------------------------------------------------------------------------
-- The source CHECK. NOT VALID first: CHECKs and indexes take locks, and the
-- growth tables are empty in every deployment that has not run a stage, but
-- the pattern is correct regardless — validate after adding, so a full-table
-- scan happens without holding the write lock on inserts that follow.
--
-- The constraint must admit '' — it is the column's DEFAULT, and every lead
-- written before Stage 7 by a caller that did not set a source carries it.
-- A CHECK that excluded the default would make the next default insert fail
-- on a table the migration was supposed to leave working.
-- ---------------------------------------------------------------------------
ALTER TABLE growth_leads
    ADD CONSTRAINT growth_leads_source_valid CHECK (
        source IN ('', 'research', 'web_search', 'apollo', 'hunter',
                   'website_form', 'csv', 'manual', 'justdial', 'indiamart')
    ) NOT VALID;

ALTER TABLE growth_leads VALIDATE CONSTRAINT growth_leads_source_valid;

-- The source filter's index. The screen's page is one source, one page,
-- newest-updated first, so the leading (tenant_id, source) pair makes it an
-- index-range read rather than a filtered full scan of the tenant's history.
CREATE INDEX IF NOT EXISTS growth_leads_tenant_source_idx
    ON growth_leads (tenant_id, source, updated_at DESC);

-- The default board: no status tab, no source chip, newest first. Without this
-- index that page sorts the tenant's whole lead history on every load; with it
-- the read is a straight index-range scan.
CREATE INDEX IF NOT EXISTS growth_leads_tenant_updated_idx
    ON growth_leads (tenant_id, updated_at DESC);
