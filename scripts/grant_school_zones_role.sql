-- scripts/grant_school_zones_role.sql
-- docs/AI/SCHOOL_ZONES_DB_BUILD_SPEC_2026-07-21.md §8 -- "Dedicated DB role
-- granted write on ONLY the two school tables; ingest runs as that role."
-- First sanctioned dev-box -> prod write path in this repo (no existing
-- CREATE ROLE/GRANT precedent anywhere in scripts/ or docs/) -- it earns
-- this restriction: this role can physically write nothing but the two
-- school tables, even if the ingest script itself had a bug.
--
-- ⚠️ This file is committed to a PUBLIC repo -- it must never contain a
-- real password. CREATE ROLE below has NO PASSWORD; set one afterward,
-- out-of-band, with a role-password command that is never committed and
-- never run by any script in this repo (operator's psql session only).
--
-- Rehearsed on the throwaway DB first (§9.8), never hand-run against the
-- shared prod instance without that rehearsal having passed.
--
-- Usage (run scripts/migrate_school_zones_schema.py FIRST -- the GRANTs
-- below target tables that must already exist):
--   psql "$DATABASE_URL" -f scripts/grant_school_zones_role.sql

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'school_zones_ingest') THEN
        CREATE ROLE school_zones_ingest LOGIN;
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO school_zones_ingest', current_database());
END
$$;

GRANT USAGE ON SCHEMA public TO school_zones_ingest;

-- Write access to ONLY the two school tables. No schema-mutating grant, no other table.
GRANT SELECT, INSERT, UPDATE, DELETE ON school_attendance_zones TO school_zones_ingest;
GRANT SELECT, INSERT, UPDATE, DELETE ON school_campus_ratings TO school_zones_ingest;
GRANT USAGE, SELECT ON school_attendance_zones_id_seq TO school_zones_ingest;
