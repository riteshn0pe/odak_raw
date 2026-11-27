-- Growth platform, Stage 5: Part B insights.
--
-- Three tables:
--
--   growth_insights       — one row per delivered insight. Deduped on
--                           (tenant_id, uid) where uid is sha256 over
--                           source + subject, so a re-run on the same day adds
--                           nothing. confidence and urgency are SMALLINT 1-3
--                           buckets with a CHECK, never REAL: an LLM-produced
--                           0.73 is fake precision nobody can tune or trust,
--                           and a float column invites exactly that write.
--   growth_events         — the event calendar as a table, not an agent. The
--                           fixed annual festival/sale/statutory calendar is
--                           seeded below so no agent ever burns a web search
--                           learning when Diwali is; only the variable
--                           categories (trade fairs, industry events,
--                           regulation and budget dates, tenant-specific
--                           dates) are searched at run time.
--   growth_page_snapshots — the competitor site watcher's own store, keyed
--                           (tenant_id, url). Snapshots deliberately do NOT
--                           live in growth_integrations.health: there is no
--                           integration row per competitor URL, health is a
--                           whole-column replace at the SQL layer, and it
--                           already carries store-sync cursors that a daily
--                           page snapshot would silently bury.
--
-- No down path, as with every migration here. Forward only.

-- ---------------------------------------------------------------------------
-- growth_insights: what the agent found, why it matters here, what to do.
--
-- Every hard delivery rule the agent enforces in Python is also a constraint
-- here, so a future caller that skips the service cannot write a rule-breaking
-- row: `source_url <> ''` is the "nothing without a source" bar, and
-- `suggested_action <> ''` is the "nothing without a concrete action" bar. The
-- "action must cite this tenant's own rows" half of that bar is a judgement the
-- database cannot make; it is enforced in the service before the insert.
--
-- `action_payload` is the machine-readable half of `suggested_action`.
-- suggested_action is prose for the owner to read; the composer needs a segment
-- selector it can hand to app/domain/segments.py, and re-parsing English at
-- click time would be a second, worse LLM call. It carries
-- {"segment": {"key": ..., "params": {...}}, "offer_pct": int, "channel": ...}
-- — the same shape draft_from_job already accepts.
--
-- `type` is closed: there is no competitor_social. Instagram/Facebook/X give no
-- third-party read API for accounts we do not own, and scraping them is
-- ToS-breaking, so the watcher does not exist and the CHECK makes that
-- permanent rather than a convention someone re-litigates.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS growth_insights (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    uid         TEXT NOT NULL,
    date        DATE NOT NULL DEFAULT CURRENT_DATE,
    type        TEXT NOT NULL,
    summary     TEXT NOT NULL,
    why         TEXT NOT NULL,
    suggested_action TEXT NOT NULL,
    action_payload   JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_url  TEXT NOT NULL,
    confidence  SMALLINT NOT NULL,
    urgency     SMALLINT NOT NULL,
    acted_on    TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT growth_insights_uid_unique UNIQUE (tenant_id, uid),
    CONSTRAINT growth_insights_type_check CHECK (
        type IN ('competitor_site', 'competitor_news', 'industry_news',
                 'event', 'own_numbers')),
    CONSTRAINT growth_insights_confidence_check CHECK (confidence BETWEEN 1 AND 3),
    CONSTRAINT growth_insights_urgency_check    CHECK (urgency    BETWEEN 1 AND 3),
    CONSTRAINT growth_insights_source_check     CHECK (source_url <> ''),
    CONSTRAINT growth_insights_action_check     CHECK (suggested_action <> ''),
    CONSTRAINT growth_insights_acted_on_check CHECK (
        acted_on IS NULL OR acted_on IN ('campaign', 'snooze', 'ignore'))
);

-- The panel's only ordering, and the digest's: urgency, then confidence, then
-- recency. Leading with tenant_id keeps it usable under RLS.
CREATE INDEX IF NOT EXISTS growth_insights_tenant_rank_idx
    ON growth_insights (tenant_id, urgency DESC, confidence DESC, created_at DESC);
-- The report's month/year window scan (app/services/growth/reports/queries.py).
CREATE INDEX IF NOT EXISTS growth_insights_tenant_date_idx
    ON growth_insights (tenant_id, date DESC);
-- "what is still open" — the panel's default filter and the act/snooze counts.
CREATE INDEX IF NOT EXISTS growth_insights_tenant_open_idx
    ON growth_insights (tenant_id, created_at DESC)
 WHERE acted_on IS NULL;

-- ---------------------------------------------------------------------------
-- growth_events: the calendar.
--
-- tenant_id NULL means shared across tenants — Diwali is Diwali for everyone in
-- India, and seeding it per tenant would be one row per tenant per festival for
-- no gain. This is the one deliberate deviation from "every growth table is
-- tenant_id NOT NULL", stated here, and it follows the precedent already
-- shipped on agent_model_config in 016. The read side of the policy is widened
-- to `tenant_id = current_tenant_id() OR tenant_id IS NULL`; the write side is
-- narrowed so a shared row can only be inserted where there is no tenant in
-- scope — i.e. by the insights job under admin_scope(). A tenant session can
-- write its own rows and nothing else.
--
-- `category` is closed on purpose. The seeded categories (festival, sale,
-- regulation) are the fixed calendar; the searched ones (trade_fair, industry,
-- tenant) are what actually varies year to year. The CHECK is what makes
-- "the agent searches only the variable categories" checkable instead of
-- aspirational.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS growth_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID REFERENCES tenants(id) ON DELETE CASCADE,
    date        DATE NOT NULL,
    region      TEXT NOT NULL DEFAULT 'IN',
    category    TEXT NOT NULL,
    name        TEXT NOT NULL,
    source_url  TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT growth_events_category_check CHECK (
        category IN ('festival', 'sale', 'regulation',
                     'trade_fair', 'industry', 'tenant'))
);

-- Two partial uniques, not one constraint: NULL never equals NULL in a UNIQUE,
-- so a plain UNIQUE (tenant_id, date, region, name) would let the seed insert
-- Diwali again on every re-run. Partial indexes split the shared and the
-- tenant-owned halves so both are idempotent, and ON CONFLICT can infer either.
CREATE UNIQUE INDEX IF NOT EXISTS growth_events_shared_key
    ON growth_events (date, region, name)
 WHERE tenant_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS growth_events_tenant_key
    ON growth_events (tenant_id, date, region, name)
 WHERE tenant_id IS NOT NULL;

-- The lookahead scan: "what is coming in the next N days for me or everyone".
CREATE INDEX IF NOT EXISTS growth_events_date_idx ON growth_events (date);

-- ---------------------------------------------------------------------------
-- growth_page_snapshots: last seen state of each competitor URL.
--
-- `extract` is the normalised text the hash was taken over, not the raw HTML —
-- the diff is computed against it, and storing raw HTML would mean every banner
-- swap and cart counter shows up as a change. One row per URL, replaced in
-- place: the watcher needs "what did this page look like last time", not a
-- history, and an append-only table would grow one row per URL per day forever.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS growth_page_snapshots (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    url          TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    extract      TEXT NOT NULL DEFAULT '',
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT growth_page_snapshots_url_unique UNIQUE (tenant_id, url)
);

-- ---------------------------------------------------------------------------
-- The fixed calendar.
--
-- Seeded before RLS is enabled below, so the insert is not subject to a policy
-- that does not exist yet; ON CONFLICT keeps a re-run of this migration a
-- no-op. Two kinds of row:
--
--   * Gregorian-fixed (Republic Day, Independence Day, quarter ends, Black
--     Friday) — these are arithmetic, seeded for 2026 and 2027.
--   * Lunar (Diwali, Holi, Onam, Dussehra) — these move every year and are
--     seeded for 2026 only, from the almanac cited in source_url. 2027's lunar
--     dates are deliberately absent rather than guessed: a wrong Diwali is
--     worse than no Diwali, and the row can be added by a later migration or by
--     staff once the almanac is published.
--
-- Statutory dates (GST quarter ends, the ITR due date, the financial year end)
-- are seeded as category 'regulation' because they are fixed. The *variable*
-- regulation dates — a budget, a threshold change, a new compliance deadline —
-- are what the agent searches for.
-- ---------------------------------------------------------------------------
INSERT INTO growth_events (tenant_id, date, region, category, name, source_url)
VALUES
    -- 2026, Gregorian-fixed
    (NULL, DATE '2026-01-01', 'IN', 'festival',   'New Year''s Day',            'https://www.india.gov.in/calendar'),
    (NULL, DATE '2026-01-14', 'IN', 'festival',   'Makar Sankranti / Pongal',   'https://www.india.gov.in/calendar'),
    (NULL, DATE '2026-01-26', 'IN', 'festival',   'Republic Day',               'https://www.india.gov.in/calendar'),
    (NULL, DATE '2026-02-14', 'IN', 'sale',       'Valentine''s Day',           'https://www.india.gov.in/calendar'),
    (NULL, DATE '2026-03-31', 'IN', 'regulation', 'Financial year end / Q4 GST return period ends', 'https://www.gst.gov.in/'),
    (NULL, DATE '2026-06-30', 'IN', 'regulation', 'Q1 GST return period ends',  'https://www.gst.gov.in/'),
    (NULL, DATE '2026-07-31', 'IN', 'regulation', 'Income tax return due date (individuals)', 'https://www.incometax.gov.in/'),
    (NULL, DATE '2026-08-15', 'IN', 'festival',   'Independence Day',           'https://www.india.gov.in/calendar'),
    (NULL, DATE '2026-09-30', 'IN', 'regulation', 'Q2 GST return period ends',  'https://www.gst.gov.in/'),
    (NULL, DATE '2026-10-02', 'IN', 'festival',   'Gandhi Jayanti',             'https://www.india.gov.in/calendar'),
    (NULL, DATE '2026-11-27', 'IN', 'sale',       'Black Friday',               'https://www.india.gov.in/calendar'),
    (NULL, DATE '2026-11-30', 'IN', 'sale',       'Cyber Monday',               'https://www.india.gov.in/calendar'),
    (NULL, DATE '2026-12-25', 'IN', 'festival',   'Christmas',                  'https://www.india.gov.in/calendar'),
    (NULL, DATE '2026-12-31', 'IN', 'regulation', 'Q3 GST return period ends',  'https://www.gst.gov.in/'),
    -- 2026, lunar / almanac
    (NULL, DATE '2026-03-03', 'IN', 'festival', 'Holika Dahan',          'https://www.drikpanchang.com/calendars/indian/indiancalendar.html?year=2026'),
    (NULL, DATE '2026-03-04', 'IN', 'festival', 'Holi',                  'https://www.drikpanchang.com/calendars/indian/indiancalendar.html?year=2026'),
    (NULL, DATE '2026-03-19', 'IN', 'festival', 'Ugadi / Gudi Padwa',    'https://www.drikpanchang.com/calendars/indian/indiancalendar.html?year=2026'),
    (NULL, DATE '2026-03-20', 'IN', 'festival', 'Eid al-Fitr',           'https://www.drikpanchang.com/calendars/indian/indiancalendar.html?year=2026'),
    (NULL, DATE '2026-03-26', 'IN', 'festival', 'Ram Navami',            'https://www.drikpanchang.com/calendars/indian/indiancalendar.html?year=2026'),
    (NULL, DATE '2026-04-14', 'IN', 'festival', 'Baisakhi',              'https://www.drikpanchang.com/calendars/indian/indiancalendar.html?year=2026'),
    (NULL, DATE '2026-08-26', 'IN', 'festival', 'Onam (Thiruvonam)',     'https://www.drikpanchang.com/calendars/indian/indiancalendar.html?year=2026'),
    (NULL, DATE '2026-08-28', 'IN', 'festival', 'Raksha Bandhan',        'https://www.drikpanchang.com/calendars/indian/indiancalendar.html?year=2026'),
    (NULL, DATE '2026-09-04', 'IN', 'festival', 'Krishna Janmashtami',   'https://www.drikpanchang.com/calendars/indian/indiancalendar.html?year=2026'),
    (NULL, DATE '2026-09-14', 'IN', 'festival', 'Ganesh Chaturthi',      'https://www.drikpanchang.com/calendars/indian/indiancalendar.html?year=2026'),
    (NULL, DATE '2026-10-11', 'IN', 'festival', 'Sharad Navratri begins','https://www.drikpanchang.com/calendars/indian/indiancalendar.html?year=2026'),
    (NULL, DATE '2026-10-20', 'IN', 'festival', 'Dussehra (Vijayadashami)', 'https://www.drikpanchang.com/calendars/indian/indiancalendar.html?year=2026'),
    (NULL, DATE '2026-10-29', 'IN', 'festival', 'Karwa Chauth',          'https://www.drikpanchang.com/calendars/indian/indiancalendar.html?year=2026'),
    (NULL, DATE '2026-11-08', 'IN', 'festival', 'Diwali (Lakshmi Puja)', 'https://www.drikpanchang.com/calendars/indian/indiancalendar.html?year=2026'),
    (NULL, DATE '2026-11-10', 'IN', 'festival', 'Govardhan Puja',        'https://www.drikpanchang.com/calendars/indian/indiancalendar.html?year=2026'),
    (NULL, DATE '2026-11-11', 'IN', 'festival', 'Bhai Dooj',             'https://www.drikpanchang.com/calendars/indian/indiancalendar.html?year=2026'),
    (NULL, DATE '2026-11-15', 'IN', 'festival', 'Chhath Puja',           'https://www.drikpanchang.com/calendars/indian/indiancalendar.html?year=2026'),
    (NULL, DATE '2026-11-24', 'IN', 'festival', 'Guru Nanak Jayanti',    'https://www.drikpanchang.com/calendars/indian/indiancalendar.html?year=2026'),
    -- 2027, Gregorian-fixed only
    (NULL, DATE '2027-01-01', 'IN', 'festival',   'New Year''s Day',            'https://www.india.gov.in/calendar'),
    (NULL, DATE '2027-01-14', 'IN', 'festival',   'Makar Sankranti / Pongal',   'https://www.india.gov.in/calendar'),
    (NULL, DATE '2027-01-26', 'IN', 'festival',   'Republic Day',               'https://www.india.gov.in/calendar'),
    (NULL, DATE '2027-02-14', 'IN', 'sale',       'Valentine''s Day',           'https://www.india.gov.in/calendar'),
    (NULL, DATE '2027-03-31', 'IN', 'regulation', 'Financial year end / Q4 GST return period ends', 'https://www.gst.gov.in/'),
    (NULL, DATE '2027-06-30', 'IN', 'regulation', 'Q1 GST return period ends',  'https://www.gst.gov.in/'),
    (NULL, DATE '2027-07-31', 'IN', 'regulation', 'Income tax return due date (individuals)', 'https://www.incometax.gov.in/'),
    (NULL, DATE '2027-08-15', 'IN', 'festival',   'Independence Day',           'https://www.india.gov.in/calendar'),
    (NULL, DATE '2027-09-30', 'IN', 'regulation', 'Q2 GST return period ends',  'https://www.gst.gov.in/'),
    (NULL, DATE '2027-10-02', 'IN', 'festival',   'Gandhi Jayanti',             'https://www.india.gov.in/calendar'),
    (NULL, DATE '2027-11-26', 'IN', 'sale',       'Black Friday',               'https://www.india.gov.in/calendar'),
    (NULL, DATE '2027-11-29', 'IN', 'sale',       'Cyber Monday',               'https://www.india.gov.in/calendar'),
    (NULL, DATE '2027-12-25', 'IN', 'festival',   'Christmas',                  'https://www.india.gov.in/calendar'),
    (NULL, DATE '2027-12-31', 'IN', 'regulation', 'Q3 GST return period ends',  'https://www.gst.gov.in/')
ON CONFLICT (date, region, name) WHERE tenant_id IS NULL DO NOTHING;

-- ---------------------------------------------------------------------------
-- Row level security. FORCE applies the policy to the table owner too, which is
-- what stops the app role — which owns these tables — from bypassing isolation.
-- Identical to 016/017/018/019 for two of the three tables.
-- ---------------------------------------------------------------------------
ALTER TABLE growth_insights       ENABLE ROW LEVEL SECURITY;
ALTER TABLE growth_insights       FORCE  ROW LEVEL SECURITY;
ALTER TABLE growth_events         ENABLE ROW LEVEL SECURITY;
ALTER TABLE growth_events         FORCE  ROW LEVEL SECURITY;
ALTER TABLE growth_page_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE growth_page_snapshots FORCE  ROW LEVEL SECURITY;

DO $$
DECLARE
    target TEXT;
BEGIN
    FOREACH target IN ARRAY ARRAY['growth_insights', 'growth_page_snapshots']
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', target);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_tenant_id()) '
            'WITH CHECK (tenant_id = current_tenant_id())', target);
    END LOOP;
END $$;

-- the sanctioned exception, reasoned about above the table. Read: your rows
-- plus the shared calendar. Write: your rows, or a shared row only when there
-- is no tenant in scope at all — which is admin_scope(), which is where the
-- insights job writes the events it finds.
DROP POLICY IF EXISTS tenant_isolation ON growth_events;
CREATE POLICY tenant_isolation ON growth_events
    USING (tenant_id = current_tenant_id() OR tenant_id IS NULL)
    WITH CHECK (
        tenant_id = current_tenant_id()
        OR (tenant_id IS NULL AND current_tenant_id() IS NULL));
