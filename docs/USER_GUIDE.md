# LotLedger User Guide

A practical guide to **using the app** and **querying the data** behind it.

This guide has two parts:

- **[Part 1 — Using the App](#part-1--using-the-app)** — for everyone. How the map, filters, saved areas,
  comps, and exports work.
- **[Part 2 — Querying the Data](#part-2--querying-the-data)** — for technical readers comfortable with SQL.
  How to connect read-only and a library of ready-to-run queries.

> You only need Part 2 if you want to pull data directly from the database. Everything in Part 1 is done
> in the app's normal interface.

---

## Part 1 — Using the App

### Roles at a glance

LotLedger has several access levels. Access is **capability-based**, not strictly tiered — a few features
are limited to specific roles, noted where they apply.

| Role | Roughly what it can do |
|---|---|
| **developer** | Everything, including internal/admin tooling |
| **owner** | Full app, plus user management |
| **power_user** | Full app features, including **CSV export** and **outreach tracking** |
| **user** | Standard use — search, save, and analyze areas (CSV export and outreach tracking are not available at this level) |
| **member** | Legacy role; existing members are treated as `user` |

If a feature described below isn't visible to you, it's gated to a role you don't have.

### The map and property types

The map color-codes parcels by **property type** so you can spot opportunities at a glance. The current
legend is shown in the app; the main categories are:

- **Off Market** — not currently listed for sale
- **Vacant** — vacant land / lots
- **Duplexes** — 2–4 unit properties
- **Exempt** — tax-exempt parcels (government, church, etc.)
- Listing-status categories (Sold / Active / Pending) for on-market context

### Filters

Use the **Property Type Filters** in the sidebar to control what's drawn on the map.

- **On by default:** Off Market, Vacant, and Duplexes.
- **Off by default:** Exempt and the other types — toggle them on when you want them.
- Each filter shows a live **count** of how many parcels currently match.

Filters are part of your saved workspace — when you save an area, the filter state is saved with it.

### Searching and selecting parcels

1. **Search an address** to jump the map to a location.
2. **Draw an area** (the polygon tool) to select every parcel inside the shape.
3. The selected parcels are what you then analyze, save, and export.

### Saving areas: Save vs Update

There are two distinct save actions:

- **Copy Area As** — creates a **new** saved area from your current selection (a copy). Use this to capture
  a fresh workspace.
- **Update** — saves changes to the area you currently have loaded (such as its filter state) without
  making a copy.

A saved area stores its polygon(s), filter state, and the **subject property** (see below).

### The subject property

Within a saved area you can mark a **subject property** — the parcel the area is built around (its
"originator"). It's marked with a **gold star** and a gold parcel outline so it stands out from the rest
of the selection. Saving a parcel inside a loaded area stages it as the new subject; updating commits it.

### Comps

When comps are available for a parcel, you can mark which ones are good. A **green check** badge marks a
parcel you've flagged as a good comp, so your best comparables stay visible as you work.

### Stored Values

For a saved area you can record your own working numbers (ARV, NBV, TDPP, Rehab Needed, MAO). These
autosave as you type and are stored with the **saved area** (workspace), not the individual parcel. For
example, **NBV** (New Build Value) drives **TDPP** automatically (NBV × 0.2).

### Flood zones

A **flood-zone overlay** can be switched on from the **LYRS** (layers) menu in the map toolbar. It shades
FEMA flood zones over the map (including the lighter "X (unshaded)" reduced-risk areas), so you can factor
flood risk into a parcel's appeal.

### CSV export

You can export your selected parcels to CSV for offline work or to feed another tool. The export includes
the parcel data plus appended columns at the right edge (owner detail, outreach status, comp rating, and
similar), depending on your role.

### Importing outreach contacts

If you track outreach in a CRM, you can **import** a CSV back into LotLedger to mark which parcels have had
their **contact info retrieved** and record a **mailer date**. Imports are matched to parcels and logged so
you can see what landed.

---

## Part 2 — Querying the Data

For when you want to pull numbers directly from the database instead of through the app.

### Connecting (read-only)

The data lives in a single **PostgreSQL** instance that hosts **two databases**:

| Database | Contains | When to use it |
|---|---|---|
| **`lotledger`** | Real-estate data — parcels, appraisal detail, outreach notes, ZIP polygons, comp cache | "How many parcels…", "show outreach for this parcel" |
| **`lotledger_sessions`** | App state — users, saved areas, sessions, stored values, ratings | "Who has accounts", "what did I save", "when did I last analyze X" |

To connect, use **Cloud SQL Studio** (in the Google Cloud Console) or any PostgreSQL client (`psql`,
DBeaver, TablePlus) with **read-only credentials provided by your administrator**. Pick the database you
need; switch databases if you get a `relation "..." does not exist` error (you're likely in the other one).

> **Read-only, always.** Run `SELECT` queries only. Do **not** run `UPDATE`, `DELETE`, `DROP`, `TRUNCATE`,
> or `ALTER` against this database — it is production data. If you think you need to change something, stop
> and ask your administrator first. All queries are audit-logged.

### Table map

**`lotledger` (data DB)**

| Table | What it is |
|---|---|
| `parcels` | Dallas (DCAD) parcels |
| `tad_parcels` | Tarrant (TAD) parcels |
| `collin_parcels` | Collin parcels |
| `denton_parcels` | Denton parcels |
| `appraisal`, `res_detail`, `land_detail` | DCAD detail tables (joined on `account_num`) |
| `parcel_outreach_notes` | Contact-info-retrieved + mailer-date per parcel |
| `outreach_import_log` | Audit log of CRM CSV imports |
| `csv_export_log` | Audit log of CSV downloads that include outreach data |
| `zcta_polygons` | USPS ZIP polygons (for property-ZIP resolution) |
| `propelio_cache` | Cached comp pulls per address |
| `ownership_snapshots` | Historical owner names by parcel + year |

**`lotledger_sessions` (session DB)**

| Table | What it is |
|---|---|
| `users` | Accounts + roles |
| `auth_audit_log` | Login attempts, password changes, admin actions |
| `saved_areas` | Saved workspaces (polygons + filter state + subject parcel) |
| `saved_area_members` | Who has access to which shared area |
| `saved_area_filter_fields` | Per-field filter state for multi-user sync |
| `analysis_sessions` | Named search snapshots you can revisit |
| `cached_jobs` | Cached parcel results (powers fast re-opens + CSV export) |
| `parcel_ratings` | Good/bad parcel marks |
| `comp_ratings` | Good/bad comp marks |
| `stored_value_entries` | ARV / NBV / TDPP / Rehab / MAO numbers per saved area (keyed by `area_id` + `field_key`) |
| `session_tags` | Verified-Vacant / Potential-Target tags |

### Query library

All queries below are **read-only**. Run them against the database noted in each section.

#### On `lotledger` (data)

**Parcel counts per county**
```sql
SELECT 'dcad'   AS county, COUNT(*) FROM parcels
UNION ALL SELECT 'tad',    COUNT(*) FROM tad_parcels
UNION ALL SELECT 'collin', COUNT(*) FROM collin_parcels
UNION ALL SELECT 'denton', COUNT(*) FROM denton_parcels;
```

**Look up one DCAD parcel by account number**
```sql
SELECT account_num, property_address, property_city, property_zip, owner_name
FROM parcels
WHERE account_num = '<account_num>';
```

**Recent outreach notes (contact-info-retrieved + mailer date)**
```sql
SELECT county, parcel_id, contact_info_retrieved, mailer_date, last_updated_at
FROM parcel_outreach_notes
ORDER BY last_updated_at DESC
LIMIT 50;
```

**Outreach notes set this month (count)**
```sql
SELECT COUNT(*) FROM parcel_outreach_notes
WHERE last_updated_at > date_trunc('month', now());
```

**CRM import history**
```sql
SELECT started_at, file_name, mode, rows_total, rows_matched, rows_unmatched, rows_updated
FROM outreach_import_log
ORDER BY started_at DESC
LIMIT 20;
```

#### On `lotledger_sessions` (app state)

**Who has accounts**
```sql
SELECT username, email, role, is_active, created_at
FROM users
ORDER BY created_at;
```

**All saved areas (most recent first)**
```sql
SELECT area_id, name, type, user_id, created_at, updated_at
FROM saved_areas
ORDER BY updated_at DESC
LIMIT 50;
```

**Recent analysis sessions (last 7 days)**
```sql
SELECT name, parcel_count, county_coverage, created_at
FROM analysis_sessions
WHERE created_at > now() - interval '7 days'
ORDER BY created_at DESC;
```

**Stored values for one user** (joined through their saved areas — `stored_value_entries` is keyed by area, not user)
```sql
SELECT sa.name AS area_name, sve.field_key, sve.numeric_value, sve.comment_text, sve.updated_at
FROM stored_value_entries sve
JOIN saved_areas sa ON sa.area_id = sve.area_id
WHERE sa.user_id = (SELECT id FROM users WHERE username = '<username>')
ORDER BY sve.updated_at DESC
LIMIT 50;
```

### If something looks off

- **Empty result on a parcel query?** Try the other county tables — each county is a separate table.
- **`relation "..." does not exist`?** You're querying the wrong database. Switch to the other one.
- **`permission denied`?** Your credentials don't have access yet — ask your administrator.
- **Looks like data is missing?** Don't panic — the database is backed up. Note what you were looking at
  and when, and contact your administrator.

---

*For development setup, architecture, and deployment, see the project `README.md`.*
