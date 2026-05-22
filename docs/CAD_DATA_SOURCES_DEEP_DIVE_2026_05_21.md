---
title: CAD residential detail data sources — deep dive (DCAD baseline + 3 other counties)
date: 2026-05-21
trigger: After shipping DCAD residential detail (Phase 1+2 today), KK asked "do the others have it?" — investigated all four counties' free public data + their CAD websites + adjacent portals to determine what additional residential improvement detail is recoverable without paid contracts or email-based PIA requests.
status: research notes — implementation tracked in separate per-county PRs
constraint: KK explicitly out for now — paid custom data exports + email-based Open Records requests. Free-online-download only.
---

# CAD Residential Detail Data Sources — Deep Dive

## TL;DR ranking (free-data-payoff first)

1. **Denton** — `dentoncad.net/data/_uploaded/files/datafiles/<year>/CertifiedDataAllProperty/` has free `APPRAISAL_IMPROVEMENT_DETAIL.TXT` (877MB) + `APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT` (216MB) + `APPRAISAL_IMPROVEMENT_INFO.TXT` (44MB) + others. Likely the DCAD `RES_DETAIL.CSV` equivalent. **Biggest free win.**
2. **TAD** — `tad.org/data-download/` publishes "Residential Comp Attribute Data" for tax years 2019-2024. Browser-accessible; bot 403s on direct curl. Likely contains foundation/roof/HVAC/exterior wall descriptors.
3. **Collin** — wall. Free public download is `LiteDatabaseCurrent.zip` (.mdb file matching the 91-field parcel-summary we already have via the shapefile). Rich per-improvement attributes locked behind paid custom export OR PIA OR scrape of `propaccess.trueautomation.com`.

## DCAD baseline (already shipped 2026-05-21)

`data/RES_DETAIL.CSV` has 39 columns. We ingest 27 of the residential-relevant ones (skipping mobile-home-specific MBL_HOME_* fields). Coverage 100% across ~683k DCAD residential/commercial parcels.

Canonical residential keys produced from DCAD via SELECT aliases:
- beds, full_baths, half_baths, fireplaces, kitchens, wet_bars, units
- eff_yr_built, act_age, pct_complete
- pool_flag, spa_flag, sauna_flag, sprinkler_flag, deck_flag (canonical T/F/NULL)
- stories_desc, bldg_class, cdu_rating
- construction_frame_type, foundation_type, heating_type, ac_type
- fence_type, ext_wall, basement, roof_type, roof_material

See `docs/CAD_RESIDENTIAL_DETAIL_EXPANSION_SPEC.md` for the canonical-field contract.

---

## Denton — FREE rich data exists, just not in the bundle we have

### Current state of our Denton ingest

KK's local Denton bundle at `ingest/counties/denton/cad/current/unzipped/` has:
- `Parcels_FC.csv` (71 columns — parcel-summary)
- `Parcels_FC.geojson` (same)
- 14 sibling shapefiles: 911 addresses, city boundaries, county lines, development permits, ETJ, historical districts, lakes, schools, subdivisions, utility permits, zipcodes

**Residential-detail fields in current bundle:** NONE worth speaking of. Only `imprvActualYearBuilt`, `imprvMainArea`, `imprvTotalArea`, `imprvEffYearBuilt`, `imprvClasses` (5 generic improvement fields). No beds, baths, pool, foundation, HVAC, roof.

### The "free rich data" — `CertifiedDataAllProperty/` extract

**Location:** `https://dentoncad.net/data/_uploaded/files/datafiles/<year>/CertifiedDataAllProperty/` (year currently 2024; will refresh in July with 2025 certified)

**Directory listing observed (via WebFetch 2026-05-21):**

| File | Size | Likely content |
|---|---|---|
| `APPRAISAL_IMPROVEMENT_DETAIL.TXT` | 877 MB | Likely the DCAD `RES_DETAIL.CSV` equivalent (per-improvement detail rows) |
| `APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT` | 216 MB | Likely attribute table: foundation, roof, HVAC, exterior wall codes per improvement |
| `APPRAISAL_IMPROVEMENT_INFO.TXT` | 44 MB | Likely improvement-level summary (one row per improvement) |
| `APPRAISAL_INFO.TXT` | 3.9 GB | Comprehensive property info — full denormalized export |
| `APPRAISAL_LAND_DETAIL.TXT` | 70 MB | Land segments |
| `CertifiedDataAll.zip` | 257 MB | Compressed bundle of all 21 .TXT files |
| Plus 16 other .TXT files | various | Entity, subdivision, mobile home, tax deferral, etc. |

**Total: ~5.5 GB of data, plain text format (pipe-delimited typical for Texas CAMA exports).**

This is what we want. We just don't have it yet.

### How to grab it

KK opens `https://dentoncad.net/data/_uploaded/files/datafiles/2024/CertifiedDataAllProperty/` in a browser. The 257MB `CertifiedDataAll.zip` is the easiest single download (then we unzip locally and audit fields). Alternatively, grab individual .TXT files we need.

Drop in `ingest/counties/denton/cad/certified_2024/` (new subfolder to keep separate from the AGOL/portal bundle).

### Once on disk, implementation plan

Mirrors the DCAD Phase 1 expansion pattern:
1. Audit the IMPROVEMENT_DETAIL_ATTR.TXT columns to confirm DCAD-equivalent fields exist
2. Schema-add new columns to `denton_parcels` (or a new `denton_improvement_detail` join table)
3. Build an ingest script (`scripts/build_denton_improvement_detail.py`) — read the .TXT, normalize flags, populate
4. Update Denton SELECT in `api/counties/denton.py` to project canonical residential keys
5. Verify on preview before promote

Implementation will get its own spec file when we audit the actual columns.

---

## TAD — FREE Residential Comp Attribute Data exists

### Current state of our TAD ingest

KK's TAD bundle at `ingest/counties/tarrant/tad/2026-05-01/` has:
- `ParcelView.shp/.dbf` (~60 fields including residential basics)
- `PropertyData(Delimited)/PropertyData_2026.txt` (785 MB, same columns as DBF)
- Cities/County/Creeks/Lakes/MUDS/Neighborhoods/PIDS/Schools/Subdivisions/TIFS supporting layers
- `StandardData/{Commercial,Residential,Personal,LegacyCertified}PropertyData/` — only contains template `.txt` files (`PropertyData(Delimited)_R.txt` etc., 645 bytes each = header only)

**Residential-detail fields in current bundle:** ~15 useful (Num_Bedrooms, Num_Bathrooms total, Garage_Capacity, Year_Built, Living_Area, Swimming_Pool_Ind, Central_Heat_Ind, Central_Air_Ind, Structure_Count, Deed_*, MAPSCO, ARB_Indicator). No foundation, roof, exterior wall, fireplace count, kitchen count, basement, CDU rating.

### The "free rich data" — Residential Comp Attribute Data

**Page:** `https://www.tad.org/data-download/` (or `https://www.tad.org/resources/data-downloads`)

Per web search hits (page itself blocks bot fetches):
- "Res Comp Attribute Data provides comparative attribute data for residential properties"
- Tax years 2019, 2020, 2021, 2022, 2023, 2024 all published
- TAD's "Residential Appraisal Manual" PDFs (also blocked by bot 403 but referenced) document the foundation/roof/HVAC/exterior-wall attributes their appraisers capture — same fields are presumably exposed in the Res Comp Attribute file

### How to grab it

KK opens `https://www.tad.org/data-download/` (or `tad.org/resources/data-downloads`) in browser. Look for "Residential Comp Attribute Data" download for 2024 (or latest year). Drop in `ingest/counties/tarrant/tad/2026-05-01/unzipped/ResCompAttribute/` (new subfolder).

### Once on disk

Audit columns vs DCAD canonical keys. Likely we can populate beds/full_baths/half_baths/foundation/roof/etc. canonical keys from this single file. May need to join against PropertyData by Account_Num.

Implementation will get its own spec file when we audit the actual columns.

---

## Collin — wall (free data caps at what we have)

### Current state of our Collin ingest

KK's local Collin bundle at `ingest/counties/collin/cad/current/unzipped/` has:
- `parcels_with_appraisal_data_R5.shp` (100 fields) — our primary source. Documented in `parcels_with_appraisal_data_column_definitions.xls` as 91 "Field Descriptions"
- `Collin_CAD_Appraisal_Data_-_2025_20260502.csv` (109 cols) — parcel-summary CSV mirror
- `Collin_CAD_Code_File_-_Improvement_Type_20260503.csv` + `CodeFileLists.xls` — code lookups
- `Collin_CAD_Building_Permits_*.csv` — building permits
- `Collin_CAD_Protest_Data_*.csv` — protests
- `Collin_CAD_Agent_Prop_List_*.csv` — agent assignments
- Various other support files

**Residential-detail fields in current bundle:** beds, baths (as text strings, not split), stories (numeric), units, pool (T/F), eff_yr_blt, class_cd, percent_co. No foundation, roof, exterior wall, HVAC, fireplace count, basement, CDU rating, deck/spa/sauna/sprinkler.

### What's missing + why

CCAD uses **TrueAutomation** as their CAMA system. TrueAutomation's internal data model has many tables (PROPERTY, IMPROVEMENT, IMPROVEMENT_DETAIL, etc.) with rich attributes per improvement. But CCAD's public download is **deliberately limited**:

| Download | Format | Fields |
|---|---|---|
| `CodeFileLists.xls` | xls | Code lookups only |
| `LiteDatabaseCurrent.zip` (Microsoft Access .mdb) | mdb | Per the 2013 file-layout PDF: same 91 fields as the shapefile. "Lite" really means lite. |
| `LiteDatabase_FileLayout.pdf` | PDF | Documents 91 fields of ONE table |
| Texas open data portal (data.texas.gov) | JSON/RDF/XML/CSV | Same 91-field summary |
| TrueAutomation property search (`propaccess.trueautomation.com/ClientDB/Property.aspx?cid=111&prop_id=...`) | HTML | Shows richer per-improvement detail PER PARCEL but blocked by 504/403 to bots |

The PDF doc says "Field Descriptions" not "Table Descriptions" — there's a small chance the .mdb file contains additional tables beyond what the PDF documents. **Can't confirm without downloading the .mdb file in a browser and opening it in Access (or via mdb-tools in Python).** Worth doing once we have a sec.

### Paths to richer Collin data (all out-of-scope per KK's "no extra cost / no emails" rule)

1. **Custom data export** — CCAD explicitly says "Custom data exports available for a fee" at `collincad.org/open-data-portal`. Paid. Out for now.
2. **Open Records / PIA request** — Free legal mechanism via `collincadtx.justfoia.com`. Takes weeks. Email-driven. Out for now.
3. **Per-parcel scrape of `propaccess.trueautomation.com/ClientDB/Property.aspx?cid=111&prop_id=...`** — Technically works but slow (sequential per-parcel), brittle (ToS-risk), maintenance burden if their HTML changes. Defer.

### Verification opportunity

If KK downloads `LiteDatabaseCurrent.zip` via browser, I can inspect actual .mdb tables via:
```bash
sudo apt install mdbtools  # or use python pyodbc
mdb-tables LiteDatabaseCurrent.mdb
```
Even a 2-minute test would tell us if there are hidden tables the 2013 PDF doesn't document.

---

## Things tried but blocked

| Attempt | Result | Conclusion |
|---|---|---|
| WebFetch on `tad.org/data-download/` | 403 | bot-blocked. Browser only. |
| WebFetch on `tad.org/open-records` | 403 | bot-blocked. |
| WebFetch on `tad.org/content/forms/Residential Appraisal Manual 2022.pdf` | 403 | bot-blocked. |
| WebFetch on `tad.org/content/forms/PropertyData&PropertyLocationLayouts.pdf` | 403 | bot-blocked. |
| WebFetch on `collincad.org/open-data-portal` | OK (no improvement-detail file listed) | confirms free is limited |
| WebFetch on `collincad.org/open-records/` | OK | confirms PIA path exists at collincadtx.justfoia.com |
| WebFetch on `agent.collincad.org/data.php` | 403 | Agent portal — requires registration |
| WebFetch on `esearch.collincad.org/Property/View/<id>` | 403 | bot-blocked |
| WebFetch on `propaccess.trueautomation.com/clientdb/Property.aspx?cid=111&prop_id=1` | 504 timeout | rate-limited or bot-detected |
| curl HEAD on `link.collincad.org/.../LiteDatabaseCurrent.zip` | 200 but returned 7KB HTML, not 200MB zip | portal HTML wrapper; needs browser click |
| Range-download partial zip | corrupt | partial download = invalid ZIP central directory |
| `dentoncad.net/data/.../CertifiedDataAllProperty/` directory listing | OK | confirms 21 free files including IMPROVEMENT_DETAIL + IMPROVEMENT_DETAIL_ATTR |

---

## Sources (for future re-research)

- DCAD baseline: `data/RES_DETAIL.CSV` (local)
- TAD downloads: https://www.tad.org/data-download/
- TAD downloads alt: https://www.tad.org/resources/data-downloads
- TAD search: https://tarrant.prodigycad.com/property-search
- Denton downloads (live HTTP, browse-accessible): https://dentoncad.net/data/_uploaded/files/datafiles/2024/CertifiedDataAllProperty/
- Denton portal page: https://www.dentoncad.com/data-downloads
- Denton data extracts: https://www.dentoncad.com/data-extracts/
- Denton public search: https://esearch.dentoncad.com/
- Collin appraisal exports: https://collincad.org/category/appraisal-data-exports/
- Collin LiteDatabase file (portal): https://link.collincad.org/public/folder/1j1vp-rhx06rqkh3vz2ipw/AppraisalData/LiteDatabaseCurrent.zip
- Collin LiteDatabase layout PDF: https://link.collincad.org/public/folder/1j1vp-rhx06rqkh3vz2ipw/AppraisalData/LiteDatabase_FileLayout.pdf
- Collin TrueAutomation: https://propaccess.trueautomation.com/ClientDB/PropertySearch.aspx?cid=111
- Collin Open Records (PIA portal): https://collincadtx.justfoia.com
- Texas Comptroller CAD directory: https://comptroller.texas.gov/taxes/property-tax/county-directory/

---

## Decision (locked 2026-05-21)

**Phase 3 = Denton ingest expansion (CertifiedDataAllProperty source). Phase 4 = TAD ingest expansion (Res Comp Attribute Data). Collin paid/PIA paths stay deferred until after Phase 3+4 land.**

Spec + implementation tracked in separate per-county docs under `docs/`.
