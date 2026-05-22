---
created: 2026-05-22
status: research-snapshot
sources: 4 parallel research dives 2026-05-22 (OSM/PostGIS, CV/aerial, commercial AVMs, academic hedonic)
---

# Road-Adjacency Detection — Research Deep Dive

Consolidated findings from 4 parallel research dives 2026-05-22. Each dive returned a full report; this is the synthesis.

---

## Five key findings

### 1. Nobody in the flipper-tool segment solves this
Propelio, PropStream, PropertyRadar, BatchLeads, DealMachine, Privy, REIPro, ListSource — all checked. None advertise a "main road" / "busy street" flag or score. Site-quality is conspicuously absent across the entire segment. This is an open market gap LotLedger can fill.

### 2. The front-vs-back asymmetry is a documented gap in academic literature
Hedonic-pricing papers (Hughes & Sirmans 1992, Wilhelmsson 2000, Theebe 2004, Madrid 2020, FHWA TNM-based studies) all collapse orientation in their models. Appraisers acknowledge the asymmetry in industry forums (AppraisersForum, Sacramento Appraisal Blog) but no published coefficient exists. KK's intuition that back-vs-front matters is correct AND a novel measurement opportunity.

Opendoor blog: "Homes that back to a busy road experience a smaller reduction… The house itself acts as a buffer, and buyers often find backyard noise more tolerable than front-yard exposure to headlights and traffic." — but they don't quantify it.

### 3. Closest off-the-shelf product: HowLoud / SoundScore
- 0–100 score per address built from the FHWA Traffic Noise Model
- Premium tier $100/mo for ≤50k queries (matches our scale exactly)
- Already in use by Better Homes & Gardens Winans of Dallas — literally Mike's market
- URL: [howloud.com/faqs](https://howloud.com/faqs), pricing: [howloud.com/pricing](https://howloud.com/pricing)
- API: REST `/address` endpoint, plus raster heat-map tile endpoint
- Enterprise tier exposes raw data including the traffic noise model output

### 4. The DIY path is cheap, free, and Texas-native
- **TxDOT AADT** (Annual Average Daily Traffic): free GIS layer covering every state/national/FM road plus a sample of county roads. ArcGIS REST + shapefile/KML. URL: [TxDOT Open Data Portal](https://gis-txdot.opendata.arcgis.com/), specifically [TxDOT AADT Counts](https://gis-txdot.opendata.arcgis.com/maps/TXDOT::txdot-annual-average-daily-traffic-counts-public)
- **OSM `highway=*` tags**: 7-level taxonomy (motorway → trunk → primary → secondary → tertiary → unclassified → residential). Free. Already widely used.
- Joined to parcel polygons in PostGIS via `ST_DWithin` or `ST_Touches`. We already run PostGIS.
- Caveat: AADT coverage is sparse on local 2-lane "considered main" roads — exactly the case KK described. This is why detection needs multi-signal (OSM + AADT), not just AADT.

### 5. Academic ground-truth: 5–10% value discount typical
- Hughes & Sirmans 1992: arterial-abutting homes sell at ~7.8% discount; linear ~−0.8% per +1,000 AADT
- Wilhelmsson 2000 (Stockholm): −0.6% per dB(A) traffic noise; full-noise lot ~30% total discount
- Theebe 2004 (Netherlands, n>100k): up to 12%, mean ~5%
- Nelson 1982 meta-analysis: mean NDSI = 0.40% per dB, range 0.16–0.63%
- Madrid 2020 (Pérez et al.): −1% per 100,000 vehicles/day; +0.7% per km distance
- Appraisal industry rule of thumb: 3–5% interior busy road, ~10% major arterial, 15–20% extreme cases

A defensible point estimate for a DFW parcel abutting a 4-lane arterial: **5–10% discount**, with 15–20% reserved for direct frontage on 6+ lane principal arterials with no setback.

---

## Commercial landscape

### Directly applicable products

| Product | What it gives you | Pricing | Verdict |
|---|---|---|---|
| **HowLoud SoundScore** | 0–100 per address, traffic component dominant, FHWA-TNM-based | $100/mo @ 50k | Closest direct buy. Used by a Dallas brokerage. |
| **Redfin Estimate** | Internally uses "busy street" feature | Not exposed via API | Confirms feature is valuable but unbuyable |
| **HouseCanary** | "Privacy Score" + AVM features | Enterprise only | Worth a sales call; white paper unparseable in session |

### Insurance-tech CV (relevant but oblique)

**CAPE Analytics, Betterview/Nearmap, Zesty.ai** — all extract building features from aerial imagery (roof, debris, pools, paved area). None expose "front vs back of property" directly. CAPE's `paved_area_condition` + building-centroid offset from parcel-centroid could be reverse-engineered into an orientation signal. All enterprise pricing, sales call required.

### Raw data sources you'd compose

- **TxDOT AADT** — free, statewide, ArcGIS REST. Strongest for Texas.
- **OSM highway tags** — free, 7-level taxonomy.
- **TIGER/Line MTFCC** — free Census road codes (S1100/S1200/S1400). Coarser than OSM.
- **EPA EJScreen** — `AADT / distance_m` formula, block-group resolution. Validation-grade only.
- **FHWA HPMS** — national AADT on Federal-Aid roads.
- **Microsoft US Building Footprints** — 130M polygons, free (CDLA Permissive 2.0). Critical for the orientation problem.
- **NAIP aerial** — 0.6m statewide TX, free on AWS as Cloud Optimized GeoTIFF. For any CV layer.

### Mapping APIs (commercial fallbacks)

- HERE Traffic API — "functional road class" filter, 250k free, then $449/mo Pro
- TomTom — 50k tiles/day free, $0.08/1k beyond. Tile-oriented.
- Google Roads API — $10/1k requests = $500 just to seed 50k parcels. Too expensive.
- Mapbox Tilequery — workable but no advantage over self-hosted OSM.

---

## Master ranking of approaches

Best → worst for LotLedger specifically (small-team, PostGIS-native, Texas focus):

| # | Approach | Effort | Cost | Accuracy | Fits us? |
|---|---|---|---|---|---|
| 1 | OSM `highway=*` + PostGIS adjacency | 2–4 days | $0 | 85–92% on "touches main road" | Yes — already on PostGIS |
| 2 | Add TxDOT AADT overlay (continuous traffic-volume signal on top of #1) | +3–5 days | $0 | Highest precision on "actually busy" | Yes — TX-native, free |
| 3 | Address-point-derived front + opposite-edge back-abut check | +1–2 days on top of #1 | $0 | 75–85% on orientation | Yes — directly answers business question |
| 4 | MS Building Footprints centroid-vs-parcel-centroid orientation vector | +1 hour | $0 | Improves #3 by ~5–10pp | Yes — free, one extra ETL |
| 5 | HowLoud SoundScore baseline cross-check | 1 day to wire | $100/mo | Black-box but turnkey | Yes — sanity-check our DIY score |
| 6 | Mapillary entrance detection (CV verification layer) | 1–2 weeks | $0 imagery | 83% precision, 4m localization | Maybe — coverage patchy in suburban DFW |
| 7 | Google Street View + YOLOv8 front-door detection | 1–2 weeks | ~$350 for 50k | Better coverage | Maybe — per-call cost |
| 8 | HERE / TomTom / Mapbox commercial road-class API | 1 week | $449/mo+ | Equivalent to OSM | No — no advantage over self-hosted OSM |
| 9 | CAPE Analytics / Betterview / Zesty.ai vendor API | 1 week | Enterprise (5-6 figures likely) | Robust but indirect | No — wrong tool, no road-orientation primitive |
| 10 | NAIP + custom U-Net driveway segmentation | 3–6 weeks | Training infra | ~60–80 IoU, unproven | No — driveways are 2–4px at 0.6m |
| 11 | EagleView/Pictometry oblique imagery | 1 week | $32–105 per property = $1.6M–$5M for 50k | Highest if affordable | No — pricing-prohibitive |
| 12 | Pure ML on aerial imagery (no OSM/AADT) | 6+ weeks | Infra | Unknown | No — reinventing what tags solve |

---

## Top 5 GitHub repos worth studying

1. **`gboeing/osmnx`** — 5k+ stars, gold-standard Python lib for OSM road network analysis
2. **`amillb/streetwidths`** — small repo, directly relevant: PostGIS-based parcel/street-width valuation, published in Journal of American Planning Association
3. **`microsoft/USBuildingFootprints`** — the dataset for orientation work
4. **`migurski/HighRoad`** — Postgres views simplifying "give me the important roads" on osm2pgsql
5. **Mapillary entrance-detection code (2026 blog)** — YOLOv8 fine-tune + ray-casting to building polygons; closest published end-to-end pipeline

---

## Methodology recommendation (academic synthesis)

Given the hedonic literature, the most defensible per-parcel score:

1. **Compute three core features per parcel:**
   - `abuts_arterial` (bool): parcel polygon shares an edge with a road classified as arterial in OSM/TxDOT (functional class ≥ minor arterial)
   - `nearest_arterial_aadt` (int): AADT value at the closest arterial segment within ~150m
   - `arterial_setback_m` (float): perpendicular distance from nearest house corner to nearest arterial edge

2. **Compute orientation flag** (LotLedger-distinctive, literature gap):
   - `road_relative_to_house` ∈ {front, back, side, none}, determined by comparing bearing from parcel centroid to driveway/garage front (proxied by side facing access road) vs bearing to the arterial

3. **Apply a literature-anchored discount:**
   - Base discount = `0.008 × (AADT / 1000)`, capped at 12% (Hughes & Sirmans)
   - Floor at 3% if `abuts_arterial=true` (appraiser practice floor)
   - Orientation factor: front 1.0, side 0.7, back 0.5, none 0.0 (derived by analogy to noise-barrier attenuation — flag as tunable since literature doesn't quantify)
   - Optional setback adjustment: `discount × max(0.3, 1 − setback_m/100)` (Madrid distance-decay echo)

4. **Calibrate against Mike's actual rated comps.** Run a regression on rows Mike has personally downgraded — extract LotLedger-specific coefficients. This IS the matched-pair method the Appraisal Institute endorses.

5. **Document the score auditably** so Mike's team sees "−6% (4-lane arterial, 25k AADT, back-facing, 18m setback)" — not a black box.

Note: this methodology fits Phase 3–4 of the [ROADMAP](./ROADMAP.md), not the Phase 1 POC. The POC's job is just the binary primitive.

---

## Key URLs

### Academic / hedonic
- Hughes & Sirmans 1992 (Journal of Regional Science): https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1467-9787.1992.tb00201.x
- Wilhelmsson 2000 (Stockholm): https://www.tandfonline.com/doi/abs/10.1080/09640560020001692
- Theebe 2004 (Netherlands, SSRN): https://papers.ssrn.com/sol3/papers.cfm?abstract_id=436944
- Nelson 2008 (Springer noise meta-analysis): https://link.springer.com/chapter/10.1007/978-0-387-76815-1_4
- Madrid case study (Pérez et al., PMC6950656): https://pmc.ncbi.nlm.nih.gov/articles/PMC6950656/
- FHWA Traffic Noise Model: https://www.fhwa.dot.gov/Environment/noise/traffic_noise_model/
- Appraisal Institute external obsolescence: https://www.appraisalinstitute.org/getattachment/4dc20c66-65db-44cd-9acb-2c36e8c6e8f2/2021-spring-feat2-external-obsolescence.pdf
- Longhofer (Wichita State) 2018 Land Values & External Obsolescence: https://realestate.wichita.edu/wp-content/uploads/2018/07/Land-Values-and-External-Obsolescence-July-2018.pdf
- Opendoor "How busy roads affect home values": https://www.opendoor.com/articles/understanding-how-busy-roads-affect-home-values
- Sacramento Appraisal Blog: https://sacramentoappraisalblog.com/2014/12/09/how-much-does-a-busy-street-impact-the-appraised-value/

### Data sources
- TxDOT Open Data Portal: https://gis-txdot.opendata.arcgis.com/
- TxDOT AADT Counts (public layer): https://gis-txdot.opendata.arcgis.com/maps/TXDOT::txdot-annual-average-daily-traffic-counts-public
- FHWA HPMS shapefiles: https://www.fhwa.dot.gov/policyinformation/hpms/shapefiles.cfm
- Census MAF/TIGER Feature Class Codes: https://www.census.gov/library/reference/code-lists/mt-feature-class-codes.html
- OSM Key:highway: https://wiki.openstreetmap.org/wiki/Key:highway
- US 2021 Highway Classification Guidance: https://wiki.openstreetmap.org/wiki/United_States/2021_Highway_Classification_Guidance
- Microsoft US Building Footprints: https://github.com/microsoft/USBuildingFootprints
- NAIP on AWS: https://registry.opendata.aws/naip/

### Commercial / products
- HowLoud FAQs: https://howloud.com/faqs
- HowLoud pricing: https://howloud.com/pricing
- HouseCanary valuation white paper: https://cdn.prod.website-files.com/659c81c0f2b2def2180e9b9f/65b19c356809d3de910f5399_hc_valuation-methodology_white-paper%20(1).pdf
- CAPE Analytics: https://capeanalytics.com/real-estate-property-intelligence/
- Betterview developer docs: https://dev.betterview.com/docs
- Zesty.ai Z-PROPERTY: https://zesty.ai/products/property-insights
- Zillow Tech Hub (Neural Zestimate): https://www.zillow.com/tech/building-the-neural-zestimate/
- Redfin Estimate methodology: https://www.redfin.com/redfin-estimate

### PostGIS / technical
- ST_ShortestLine: https://postgis.net/docs/ST_ShortestLine.html
- ST_ClosestPoint: https://postgis.net/docs/ST_ClosestPoint.html
- ST_DWithin: https://postgis.net/documentation/tips/st-dwithin/
- ST_Azimuth: https://postgis.net/docs/ST_Azimuth.html
- KNN nearest-neighbour searching: https://postgis.net/workshops/postgis-intro/knn.html
- Frontage problem canonical writeup: https://www.spdba.com.au/the-frontage-problem-creating-references-from-land-parcel-street-frontage-boundary-to-point-in-street/

---

## Open follow-ups identified during research

1. Audit geocoder precision on existing `propelio_comps` rows — orientation work in Phase 6 depends on rooftop-grade address points
2. NTREIS MLS field check — does it carry "backs to" / "busy street" / "corner lot" tags in listing remarks that we could NLP-extract?
3. Do our TX CAD parcel layers carry `frontage_street` attributes already? (Some counties publish this — would be free signal)
4. DFW OSM tag audit — sample 50 known main roads in Plano/Frisco/McKinney/Denton/Dallas/Fort Worth and confirm consistent tagging
5. Frontage road semantics — when a parcel touches a service road of I-635, does Mike rate it as freeway-back or service-road-front? Business-rule decision.
6. HouseCanary white paper deep read — to confirm whether road-class is an explicit feature
7. ATTOM Data dictionary sales call — they claim 125+ fields but don't enumerate publicly
8. TxDOT AADT coverage on county roads in target counties (Collin, Denton, Dallas, Tarrant) — confirm density before committing to AADT-heavy detection
