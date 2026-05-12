# Pre-Campaign Data Audit

> **Purpose:** Before launching a 150-300 seed marathon scraper campaign,
> verify we're capturing every field we'll want and have a clean migration
> path to (a) future schema changes and (b) the client's eventual cloud.
> Better to fix gaps now than re-scrape 5,000+ comps later.

## TL;DR — Findings

✅ **Re-parseability is solid** — `raw_payload` JSONB preserves the full
Propelio response, so we can always re-derive `parsed_payload` from raw if
the schema changes. **Adding a typed column later is cheap (single
migration, no re-scrape).**

⚠️ **Photo ownership not yet implemented** — we store Propelio URLs only.
If Propelio rotates or revokes them, photos go dark. Mike wants to own
this; not implemented yet. **Address before campaign or after?**

⚠️ **A few useful fields aren't extracted** — schools, pool, stories,
listing timestamps. Easy to add via migration; low priority unless team
needs them.

✅ **Portability story is clean** — pg_dump/restore for DB,
gsutil cp -r for any Cloud Storage assets we add. No exotic data types.

## Current schema: `propelio_comps`

Typed columns we extract (45 total):

**Internal:** comp_id, comp_address_key (unique), parsed_payload (JSONB),
raw_payload (JSONB), first_seen_at, last_seen_at, first_seen_source.

**Address/geo:** address, neighborhood, lat, lng, geom (PostGIS Point).

**Status/dates:** status, last_status, sold_date, close_date, dom.

**Pricing:** price, last_price, list_price.

**Characteristics:** beds, baths, baths_full, baths_half, garage, sqft,
lot_size, year_built.

**Identifiers:** mls, property_type, property_category.

**Remarks:** remarks.

**Agent/office contacts:** listing_agent_name/phone/email,
listing_office_name/phone, buyer_agent_name/phone/email,
buyer_office_name/phone.

**Photos:** photo_count, photos (JSONB array of URL+desc+timestamp).

**Parcel match:** parcel_account_num, parcel_county, parcel_geom (JSONB).

## What Propelio actually sends (raw_payload keys)

90+ keys per comp. Categorized:

### Fields we ARE extracting ✅

Mapped to typed columns above.

### Fields we're NOT extracting but ARE in raw_payload

| Category | Field | Recommendation |
|---|---|---|
| **Schools** | `school_district`, `elementary_school`, `middle_school`, `high_school`, `junior_high_school`, `intermediate_school`, `primary_school`, `senior_high_school` | **Extract.** Useful for buyer-side filtering. Mike's team may filter by district. |
| **House features** | `stories`, `pool`, `unit_count` | **Extract.** Pool is a meaningful price modifier. Stories affects comparability. |
| **Address parts** | `address_city`, `address_county`, `address_state`, `address_zip`, `address_subdivision`, `address_line1`, `address_unit`, `address_number`, `address_street` | **Extract `address_city`, `address_zip`, `address_subdivision` at minimum.** Useful for grouping, filtering, future enrichment. Others can stay in raw. |
| **Timestamps** | `listing_timestamp`, `modified_timestamp`, `photo_timestamp`, `status_timestamp`, `created_at` (in Propelio), `updated_at` (in Propelio) | **Extract `listing_timestamp` and `status_timestamp`.** Useful for understanding listing age vs DOM. Others can stay in raw. |
| **Pricing detail** | `seller_paid` | **Skip for now.** Rare field, easy to grab from raw later if needed. |
| **Agent IDs** | `buyer_agent_id`, `listing_agent_id`, `buyer_office_id`, `listing_office_id`, `organization_id` | **Skip.** We have names+phones, IDs not actionable for our use case. |
| **Propelio internals** | `id`, `cma_id`, `key`, `source`, `source_id`, `photo_code`, `geocode_attempted`, `geocode_precision`, `permit_avm`, `permit_internet`, `calculated_dom`, `original_status` | **Skip.** Propelio-internal. Stay in raw. |
| **Extensibility fields** | `bool_1-3`, `date_1-3`, `number_1-4`, `string_1-4`, `text_1` | **Skip.** Propelio's vendor-specific extension slots. Mostly null. Stay in raw. |

### Proposed migration BEFORE campaign

Single ALTER TABLE migration to add 13 columns + populate from existing
raw_payload via UPDATE:

```sql
ALTER TABLE propelio_comps
  ADD COLUMN address_city TEXT,
  ADD COLUMN address_zip TEXT,
  ADD COLUMN address_subdivision TEXT,
  ADD COLUMN school_district TEXT,
  ADD COLUMN elementary_school TEXT,
  ADD COLUMN middle_school TEXT,
  ADD COLUMN high_school TEXT,
  ADD COLUMN stories INTEGER,
  ADD COLUMN pool BOOLEAN,
  ADD COLUMN unit_count INTEGER,
  ADD COLUMN listing_timestamp TIMESTAMPTZ,
  ADD COLUMN status_timestamp TIMESTAMPTZ,
  ADD COLUMN photo_timestamp TIMESTAMPTZ;

-- Backfill from existing raw_payload (one-time, then trigger on insert)
UPDATE propelio_comps SET
  address_city = raw_payload->>'address_city',
  address_zip = raw_payload->>'address_zip',
  address_subdivision = raw_payload->>'address_subdivision',
  school_district = raw_payload->>'school_district',
  elementary_school = raw_payload->>'elementary_school',
  middle_school = raw_payload->>'middle_school',
  high_school = raw_payload->>'high_school',
  stories = (raw_payload->>'stories')::INTEGER,
  pool = CASE
    WHEN raw_payload->>'pool' IN ('true', 't', '1', 'yes') THEN TRUE
    WHEN raw_payload->>'pool' IN ('false', 'f', '0', 'no') THEN FALSE
    ELSE NULL
  END,
  unit_count = (raw_payload->>'unit_count')::INTEGER,
  listing_timestamp = (raw_payload->>'listing_timestamp')::TIMESTAMPTZ,
  status_timestamp = (raw_payload->>'status_timestamp')::TIMESTAMPTZ,
  photo_timestamp = (raw_payload->>'photo_timestamp')::TIMESTAMPTZ
WHERE raw_payload IS NOT NULL;
```

Then update `merge_comps_into_global()` in `archive.py` to populate these
on insert/update. **Estimate: 30-45 min Copilot work, low risk
(additive).**

## Photo ownership — bigger decision

Currently `photos` is JSONB array like:
```json
[{"url":"https://api.propelio.com/mls-media/v1/...","description":null,"lastModified":"2025-06-03..."}]
```

If Propelio rotates URLs or revokes our access, all photos disappear from
LotLedger.

**Mike's stated preference** (per memory): own the photos.

**Options:**

1. **Download on first sight** — when a comp is inserted, kick off a
   background task to fetch + store each photo URL into our Cloud Storage
   bucket. Replace the URL in our `photos` JSONB with our own URL.
   - Pros: full ownership
   - Cons: ~17 photos/comp × 4,000 comps so far = ~68k photos. ~5-50KB
     each = 0.3-3 GB. Cheap to store but slow to backfill.
   - Cons: each photo download is another HTTP call to Propelio. Doubles
     our outbound traffic. Could look suspicious.

2. **Download on demand** — only fetch when user clicks to view photo.
   Cache in our bucket. Future requests use our cache.
   - Pros: only pay download cost for photos analysts actually view
   - Cons: first-view UX has a delay; if Propelio revokes, photos already
     unloaded stay broken

3. **Defer** — accept the risk for now. Document. Revisit when client
   migration happens.

**Recommendation: Option 3 (defer)** until after the seeding campaign
ships. We won't have lost anything because raw_payload preserves URLs
even if image hosts go dark — at worst we lose the binary photos but
keep all metadata. Then implement Option 1 as a background job that runs
slowly over weeks (also has plausible "regular product use" cover for
the traffic).

**If client wants it sooner**, we can build Option 1 with throttled
downloads (1 photo per 10s = ~30 days for backfill) so it doesn't burn
Propelio rate limit.

## Re-parseability — confirmed solid

`raw_payload` JSONB preserves the full Propelio response unchanged.
`parsed_payload` is derived via `_parse_property()` in
`api/propelio/scraper.py:1546`.

If we change `_parse_property()` or add a column:
```sql
UPDATE propelio_comps SET
  parsed_payload = ...derived from raw_payload...,
  new_column = raw_payload->>'new_key'
WHERE raw_payload IS NOT NULL;
```

No need to re-scrape ever, as long as raw_payload is preserved. **This
is the strongest design choice in the schema.** Keep it.

## Migration discipline — rules for the campaign

1. **Additive only.** Never DROP COLUMN on data we might want.
2. **Add via `_run_schema_steps`** in `api/main.py:462` — centralized,
   step-named, idempotent.
3. **Backfill in same migration** when adding a column derived from
   raw_payload (so the new column is consistent across all rows).
4. **Never delete raw_payload.** Period.
5. **Schema version comment** at top of `propelio_comps` table —
   document major schema epochs.

## Portability — moving to client's cloud

When we eventually migrate to client's GCP (or AWS):

1. `pg_dump --format=custom propelio_comps comp_ratings saved_areas ...`
2. `pg_restore` on target Cloud SQL
3. Update `.env` `DB_HOST` / `DB_USER` / `DB_PASSWORD`
4. If we add photo storage: `gsutil -m rsync gs://our-bucket
   gs://their-bucket`

**No exotic types.** Everything is text, numeric, jsonb, timestamp,
geometry. PostGIS extension required on target — standard for Cloud SQL.

## Data lineage — currently captured

- `first_seen_at`: when we first inserted this row
- `last_seen_at`: when we last touched this row (UPDATE refreshes this)
- `first_seen_source`: enum-like text — values seen: `'backfill'`,
  `'deep_pull'`. Need to add `'campaign_seed'` for the marathon scraper.

**Recommendation:** before campaign, add `'campaign_seed'` as a possible
value. Maybe also add `last_seen_source` so we know which subsystem
most recently confirmed each comp.

## Audit findings summary

| Item | Action | When |
|---|---|---|
| 13 missing typed columns | Add via migration | Before campaign |
| Photo ownership | Defer to post-campaign | Phase 2.6 |
| Re-parseability | ✅ Already solid | — |
| Migration discipline | Document, follow rules | Ongoing |
| Portability | ✅ Standard PG + PostGIS | — |
| Data lineage | Add `'campaign_seed'` source value | Before campaign |

## What this means for the marathon scraper plan

The campaign script can proceed if we:
1. Run the migration (~30-45min)
2. Update `merge_comps_into_global` to populate new columns (~30 min)
3. Add `'campaign_seed'` as accepted `first_seen_source` value
4. Document the photo-ownership deferral in the campaign spec

Total pre-work: ~1.5 hours. Then we can spec the marathon scraper itself.
