-- scripts/rollback_school_zones.sql
-- docs/AI/SCHOOL_ZONES_DB_BUILD_SPEC_2026-07-21.md §7 -- "written before
-- anything runs." One-command undo for the DB-backed school-zones feature.
-- Touches nothing else: only the two additive tables this feature created
-- and the one restricted role scripts/grant_school_zones_role.sql created.
--
-- Usage:
--   psql "$DATABASE_URL" -f scripts/rollback_school_zones.sql
--
-- After running this: SCHOOL_SOURCE must be "static" (or unset) everywhere
-- it's set, or the runtime path (api/school_pilot/zones_db.py) will query
-- tables that no longer exist -- it degrades safely either way (a query
-- against a missing table raises, assign_db() catches it, returns
-- district_status=None, frontend renders nothing -- see api/school_pilot/
-- zones_db.py's own docstring) but there is no reason to run dark after a
-- rollback. Flip SCHOOL_SOURCE back to static (or unset it) as part of the
-- same rollback action, not as an afterthought.

DROP TABLE IF EXISTS school_attendance_zones, school_campus_ratings;

-- Role + grants. DROP TABLE above already removes the table-level grants
-- (SELECT/INSERT/UPDATE/DELETE) along with the tables themselves, but the
-- DATABASE- and SCHEMA-level grants from scripts/grant_school_zones_role.sql
-- (GRANT CONNECT ON DATABASE, GRANT USAGE ON SCHEMA public) are separate
-- objects that survive a table drop -- confirmed live in the throwaway-DB
-- rehearsal: DROP ROLE failed with "some objects depend on it" until these
-- were explicitly revoked first. If the role should be kept for a future
-- re-ingest, skip this whole block -- an unused role with no grantable
-- objects left is inert, not a risk.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'school_zones_ingest') THEN
        EXECUTE format('REVOKE ALL ON DATABASE %I FROM school_zones_ingest', current_database());
        REVOKE ALL ON SCHEMA public FROM school_zones_ingest;
    END IF;
END
$$;
DROP ROLE IF EXISTS school_zones_ingest;
