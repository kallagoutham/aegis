-- Database bootstrap, run once by the postgres image on first start.
--
-- Only creates extensions. Tables are owned by Alembic (migrations/), so this
-- file never defines schema - two sources of truth for table definitions is
-- how a schema silently drifts from the code that reads it.

-- Vector similarity search. Required before any `vector` column can exist.
CREATE EXTENSION IF NOT EXISTS vector;

-- Trigram matching, used for fuzzy service-name filters so "payments-api" and
-- "payments_api" do not silently return nothing.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- gen_random_uuid(), used as a server-side fallback for rows inserted outside
-- the ORM.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
