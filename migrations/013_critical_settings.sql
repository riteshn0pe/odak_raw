-- Runtime overrides for operational settings (admin console). Only explicit
-- overrides live here; absence of a row means "fall through to env/default".
CREATE TABLE IF NOT EXISTS platform_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT,                            -- plaintext for normal knobs; envelope JSON for encrypted ones (§6)
    is_secret   BOOLEAN NOT NULL DEFAULT FALSE,
    updated_by  UUID NOT NULL REFERENCES staff_accounts(id),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Append-only history for rollback + forensics. value is REDACTED for secrets.
CREATE TABLE IF NOT EXISTS platform_settings_history (
    id          BIGSERIAL PRIMARY KEY,
    key         TEXT NOT NULL,
    old_value   TEXT,                            -- NULL = was not overridden
    new_value   TEXT,                            -- NULL = override removed (reset)
    is_secret   BOOLEAN NOT NULL DEFAULT FALSE,
    action      TEXT NOT NULL,                   -- 'update' | 'reset'
    actor_id    UUID NOT NULL REFERENCES staff_accounts(id),
    actor_email TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS psh_key_ts_idx ON platform_settings_history (key, created_at DESC);
