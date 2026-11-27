-- Growth platform, Stage 6: one-way CRM sync.
--
-- One table:
--
--   growth_crm_links — the mapping between one of our leads and the record the
--                      tenant's CRM created for it, plus everything the push
--                      loop needs to decide whether it has anything to do.
--
-- The whole stage is a push. Nothing in the product reads a CRM to decide
-- anything: no lead is created from a CRM record, no status is taken from one,
-- and a CRM that is down or disconnected changes no behaviour anywhere except
-- this table's `status` column. That is a product rule, not a technical one,
-- but it is the reason this table exists at all — if the CRM were a source of
-- truth we would be caching it, and a cache needs invalidation, conflict rules
-- and a two-way merge. A push destination needs a receipt. This is the receipt.
--
-- No down path, as with every migration here. Forward only.

-- ---------------------------------------------------------------------------
-- growth_crm_links: one row per (tenant, provider, lead).
--
-- `external_id` is the CRM's own id for the contact — a Zoho record id, a
-- HubSpot contact id. It is the entire point of the table: without a stored id,
-- every push would have to search the CRM by email to find out whether it had
-- already created this person, which is a read dependency on a system we have
-- promised not to depend on, and which silently creates a duplicate the first
-- time the search is rate limited. With it, the second push is an update to a
-- known id. The row is created *before* the first push (status 'pending') so a
-- push that half-succeeds — record created, our transaction lost — is still
-- recoverable: the link row is the thing we reconcile against.
--
-- `synced_status` is the lead status as it stood at the last successful push.
-- The push job compares it against the lead's current status and does nothing
-- when they match, which is what makes a burst of writes to one lead collapse
-- into one API call rather than one per write. The Redis debounce lock narrows
-- the window; this column closes it, because a lock that expires or a Redis
-- that is down must not turn into duplicate CRM traffic.
--
-- `notes_through` is the high-water mark for activity notes. Every
-- growth_messages row for the lead created at or before this instant has
-- already been logged to the CRM as a note. Storing a timestamp rather than a
-- message id keeps the follow-up query a plain range scan on
-- (tenant_id, lead_id, created_at) and means a message deleted for compliance
-- does not strand the cursor on a row that no longer exists.
--
-- `status` is what the integration card shows for this lead, and it is
-- deliberately per-lead rather than per-integration: a CRM connection can be
-- perfectly healthy while one particular contact is rejected for a field the
-- tenant made mandatory in their own CRM, and rolling that up into the
-- integration's health would report the whole connection as broken.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS growth_crm_links (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    provider       TEXT NOT NULL,
    lead_id        UUID NOT NULL REFERENCES growth_leads(id) ON DELETE CASCADE,
    -- blank until the first successful upsert returns an id
    external_id    TEXT NOT NULL DEFAULT '',
    -- the lead status we last successfully pushed; '' before the first push
    synced_status  TEXT NOT NULL DEFAULT '',
    -- messages created at or before this instant are already logged as notes
    notes_through  TIMESTAMPTZ,
    last_synced_at TIMESTAMPTZ,
    status         TEXT NOT NULL DEFAULT 'pending',
    -- truncated in the service; the column is unbounded so a provider that
    -- returns a long structured error is not silently cut mid-JSON here as well
    last_error     TEXT NOT NULL DEFAULT '',
    -- consecutive failures, cleared on success. Drives the same backoff shape
    -- the store adapters use, so a CRM that is down for a day costs one call
    -- an hour rather than one per lead status change.
    attempts       INTEGER NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT growth_crm_links_unique UNIQUE (tenant_id, provider, lead_id),
    CONSTRAINT growth_crm_links_status_valid CHECK (
        status IN ('pending', 'synced', 'error', 'skipped')
    ),
    CONSTRAINT growth_crm_links_attempts_sane CHECK (attempts >= 0)
);

-- The duplicate guard, and the reason the checklist item "a duplicate lead is
-- never created twice" is a property of the schema rather than of the code
-- that happens to be running today. Two leads in one tenant cannot point at the
-- same CRM record: if a later dedupe merges two of our leads that were already
-- pushed separately, the second link insert fails loudly instead of leaving two
-- rows that both claim the same contact and both keep updating it. Partial,
-- because '' is the pre-first-push state and every pending row carries it.
CREATE UNIQUE INDEX IF NOT EXISTS growth_crm_links_external_unique
    ON growth_crm_links (tenant_id, provider, external_id)
 WHERE external_id <> '';

-- The leads screen asks for the link state of a page of leads at a time, with
-- no provider in the predicate — the unique constraint above leads on
-- (tenant_id, provider) and cannot serve that.
CREATE INDEX IF NOT EXISTS growth_crm_links_tenant_lead_idx
    ON growth_crm_links (tenant_id, lead_id);

-- The lead-deletion cascade (ON DELETE CASCADE from growth_leads) fires as
-- DELETE ... WHERE lead_id = $1 with no tenant predicate, which the
-- (tenant_id, lead_id) index above cannot serve. Today's lead deletions
-- pre-clear the links explicitly, but a future dedupe that merges leads will
-- delete one, and each such delete would be a seq scan without this.
CREATE INDEX IF NOT EXISTS growth_crm_links_lead_idx
    ON growth_crm_links (lead_id);

-- The CRM links screen lists failures first and the retry sweep asks for
-- exactly the errored rows, so both read this rather than scanning the tenant's
-- whole link table. Partial: 'synced' is the overwhelming majority of rows and
-- no query ever asks for them by status.
CREATE INDEX IF NOT EXISTS growth_crm_links_retry_idx
    ON growth_crm_links (tenant_id, provider, updated_at)
 WHERE status IN ('pending', 'error');

-- The links screen's ORDER BY (status = 'synced'), updated_at DESC. The retry
-- index above only holds pending/error rows, so without this the screen's
-- default view — which includes the synced majority — sorts the tenant's whole
-- link table on every page. No `provider` column: it is a filter, not an
-- ordering term, and putting it before the expression would break the ordering
-- exactly when no provider filter is set — the screen's default view.
CREATE INDEX IF NOT EXISTS growth_crm_links_list_idx
    ON growth_crm_links (tenant_id, (status = 'synced'), updated_at DESC);

-- ---------------------------------------------------------------------------
-- Row level security. FORCE applies the policy to the table owner too, which is
-- what stops the app role — which owns these tables — from bypassing isolation.
-- Identical to 016/017/018/019/020.
-- ---------------------------------------------------------------------------
ALTER TABLE growth_crm_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE growth_crm_links FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON growth_crm_links;
CREATE POLICY tenant_isolation ON growth_crm_links
    USING (tenant_id = current_tenant_id())
    WITH CHECK (tenant_id = current_tenant_id());
