-- Local dev only. Creates the unprivileged role the API connects as, so that
-- FORCE ROW LEVEL SECURITY actually applies. `app` is NOSUPERUSER — that is the
-- property that matters: a superuser bypasses RLS entirely. Because migrations
-- are run through this same role, `app` owns the tables, and FORCE RLS makes the
-- policies apply even to the owner, so tenant isolation holds.
--
-- Runs once, on first initialisation of the postgres data volume. The schema
-- itself is applied separately by scripts/migrate.py against this database.

CREATE ROLE app WITH LOGIN PASSWORD 'password' NOSUPERUSER NOCREATEDB NOCREATEROLE;

GRANT CONNECT ON DATABASE context_agent TO app;
-- CREATE lets migrate.py build the schema as `app`; USAGE lets it read it back.
GRANT USAGE, CREATE ON SCHEMA public TO app;
