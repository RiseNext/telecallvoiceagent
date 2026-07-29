-- Local database bootstrap. Runs once, on first container start.
--
-- Extensions only. Schema belongs in Alembic migrations, never here — otherwise
-- local and deployed databases drift and the difference is invisible until it
-- breaks in staging.

-- Vector similarity search for knowledge retrieval.
CREATE EXTENSION IF NOT EXISTS vector;

-- gen_random_uuid() for primary keys.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Trigram indexes for contact/lead name and phone search in the dashboard.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- A second database used by the integration test suite, so a test run can drop
-- and recreate schema without destroying local development data.
SELECT 'CREATE DATABASE risenext_test OWNER risenext'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'risenext_test')\gexec

\connect risenext_test
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
