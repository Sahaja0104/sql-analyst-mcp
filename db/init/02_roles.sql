-- 02_roles.sql
-- Creates a restricted login ("role") that the MCP server will use.
-- Runs automatically after 01_chinook.sql, as the admin user, inside the chinook database.

-- 1. Create the login. (Dev-only password; we'll move it to .env later.)
CREATE ROLE mcp_reader WITH LOGIN PASSWORD 'reader_pass';

-- 2. Let it connect to the chinook database.
GRANT CONNECT ON DATABASE chinook TO mcp_reader;

-- 3. Let it see inside the "public" schema (the folder that holds our tables).
GRANT USAGE ON SCHEMA public TO mcp_reader;

-- 4. Let it READ every existing table. Nothing else: no INSERT, UPDATE, DELETE, DROP.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mcp_reader;

-- 5. Same read-only access for any tables created later.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO mcp_reader;

-- 6. Make sure it cannot create new tables.
REVOKE CREATE ON SCHEMA public FROM mcp_reader;

-- 7. Extra safety defaults for every session this role opens:
--    - transactions are read-only unless someone explicitly changes it
--    - any query running longer than 5 seconds is cancelled by Postgres itself
ALTER ROLE mcp_reader SET default_transaction_read_only = on;
ALTER ROLE mcp_reader SET statement_timeout = '5s';