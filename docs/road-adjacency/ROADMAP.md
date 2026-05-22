---
created: 2026-05-22
status: brainstorm-captured
last_updated: 2026-05-22
---

# Road-Adjacency Roadmap

**As of 2026-05-22, brainstorm session.** Expect to evolve as KK thinks through the model further and as the POC validates assumptions.

## The vision

A fully automated comp-rating layer where road-adjacency value-loss is detected without manual review, validated by accumulated team labels, and explainable in plain language. Eventually: Mike's team's hand-labeled flags become training data for a tabular ML model that predicts value impact from structured features — no LLM required at the core.

We get there in phases. Each phase has a clear gate to the next.

---

## Phase 0 — Documentation & brainstorm capture (CURRENT)
**Goal:** Capture current state of thinking so KK can mull and refine before any code.
**Deliverables:** This folder (`docs/road-adjacency/`).
**Status:** In progress (this commit).

---

## Phase 1 — POC: Layer 1 main-road detector
**Goal:** Prove we can automatically identify main-road-adjacent parcels accurately enough that Mike's team agrees with the flags in a chosen Dallas sample area.

### Scope (locked in 2026-05-22 brainstorm)

- **Detection target:** binary `on_main_road` yes/no per parcel
- **Detection signals (multi-source):**
  - OSM `highway` tag in configurable set (default: `motorway`, `trunk`, `primary`, `secondary`, `tertiary`)
  - OSM `lanes` tag (default threshold: lanes >= 4)
  - OSM `maxspeed` tag (default threshold: maxspeed >= 35mph)
  - TxDOT AADT where available (default threshold: AADT >= 8000)
  - A parcel is "on main road" if it touches ANY way meeting ANY of the above criteria
- **Adjacency definition:** parcel boundary within ~10m of a qualifying way (handles right-of-way variability between counties)
- **Sample area:** a specific Dallas neighborhood KK identifies — characterized by two-lane non-highway "considered main" roads (the hard case for the model)
- **Validation:** new flag button on the parcel popup; team marks ground truth; iterate signal rules until detection matches team judgment

### Schema additions

- New boolean column `on_main_road` on `parcels` (system-computed, nullable until computed for that parcel)
- New boolean column `team_road_flag` on `parcels` (manual ground truth, nullable)
- New PostGIS table `osm_main_roads` — filtered road geometries from OSM, small and fast for adjacency joins
- New PostGIS table `txdot_aadt` — AADT counts joined to road segments

### Data ingestion (one-time, refreshable later)

- OSM extract for DFW counties (or all of TX if simpler) → filter to main-road tags → load into `osm_main_roads`
- TxDOT AADT shapefile (free, public ArcGIS REST endpoint) → load into `txdot_aadt`
- Both refreshable via scheduled jobs later — for POC, manual one-time ingest

### UI surface

- One new button on the parcel popup: **"On main road? [Yes] [No]"**
- Map shading: parcels with `on_main_road=true` visually distinguished in the sample area for eyeball comparison
- No changes to the comp panel or other UI surfaces in this phase

### Branch / folder structure

- New branch: `feat/road-adjacency` (broader feature branch, not POC-specific — multiple phases will live here)
- New backend module: `app/road_adjacency/` (clean isolation from existing modules)
- Docs continue in `docs/road-adjacency/`
- Monorepo for now; no repo split unless Phase 4+ grows the surface significantly

### Effort estimate

- OSM ingestion + main-road filter: 0.5 day
- TxDOT AADT ingestion: 1-2 days
- Detection logic + schema migration: 1 day
- UI button + frontend wiring: 0.5 day
- Map shading: 0.5 day
- **Total engineering: ~3-5 days**
- Plus team labeling time: ~1-2 hours of Mike's team's effort spread across a few sessions

### Gate to Phase 2

Team's eyeball check on the shaded map agrees with the detection in ≥80% of parcels in the sample area. If lower, iterate the multi-signal rule (adjust thresholds, add/remove tags from the qualifying set, etc.) within Phase 1.

---

## Phase 2 — Generalize Layer 1 to all of DFW (and beyond)
**Goal:** Make the main-road detector work universally with local-relative thresholds, not just in the urban Dallas POC area.

### Scope

- Replace absolute thresholds with local-relative ones: a road is "main" if it's in the top X% of OSM-class/lanes/AADT within a configurable radius (e.g., 5km) of the parcel
- Validate across multiple counties: Dallas (urban), Collin (suburban), Denton (mixed), Tarrant (urban), Bandera (rural)
- Calibrate thresholds per neighborhood-type tier (urban / suburban / rural)
- Bulk-compute `on_main_road` for every parcel in the DB (~50k+ parcels), not just sample area

### Effort estimate
~3-5 days

### Gate to Phase 3
Detector achieves ≥80% team-flag agreement across at least 3 counties.

---

## Phase 3 — Value-deviation engine (the actual problem)
**Goal:** Detect properties whose price is lower than structural peers — what Mike's team REALLY cares about. The road is one explanation; the price gap is the signal.

### Scope

- Cluster Propelio comps by structural similarity (build, sqft, lot, age, year-built tolerance)
- Compute each parcel's price deviation from its peer-cluster median (z-score or % deviation)
- Surface deviations on the map and in the comp panel
- Cross-reference with Layer 1 (`on_main_road`) — does deviation correlate with main-road status in this market?

### Output
Per-parcel `peer_price_deviation` column on `parcels` and/or `propelio_comps`.

### Effort estimate
~5-10 days

### Gate to Phase 4
Deviation signal visibly correlates with team's existing "Good Comp" / "Bad Comp" ratings on retrospective data.

---

## Phase 4 — Layer 2: Impact model (the AI/ML piece)
**Goal:** Predict whether main-road status actually depresses value in a given neighborhood context.

### Scope

- Tabular ML (XGBoost / random forest / logistic regression) trained on accumulated team flags + Layer 1 features + Layer 3 deviation + neighborhood density / wealth context
- Output: predicted % value impact per parcel
- This is the "AI without LLM" piece KK envisioned — the team's hand-labeled flags ARE the training data
- Model is retrained periodically as more flags accumulate

### Pre-requisites
- ~500+ accumulated team flags (from Phase 1 sample area + ongoing team usage)
- Phase 3 deviation data computed

### Effort estimate
~5-10 days for first model; ongoing tuning

### Gate to Phase 5
Predicted impact correlates with team's "Good Comp" downgrades on a held-out test set.

---

## Phase 5 — Traffic heat-map UI layer
**Goal:** Visualize traffic intensity on the map for team's situational awareness. The "heat-map" KK mentioned.

### Scope

- TxDOT AADT data (already ingested in Phase 1) rendered as a Leaflet overlay
- Color gradient by AADT value (e.g., yellow → orange → red as AADT increases)
- Small file size — vector tiles or simplified GeoJSON to avoid map lag
- Toggle in the existing layer panel alongside CNTY, etc.

### Note
This is visualization-only. The detection logic already uses AADT under the hood from Phase 1. This layer exists for the team to see traffic patterns themselves, not for the system to use.

### Effort estimate
~2-3 days

---

## Phase 6 — Front/back/side orientation
**Goal:** Distinguish whether the main road touches the front, back, or side of the property. KK's original intuition that back-vs-front matters.

### Pre-requisite
Address-point precision audit. If our geocoded address points are rooftop-grade (not parcel-centroid), this works; otherwise it doesn't. Need to sample existing data and measure.

### Scope

- Use geocoded address point + parcel polygon to derive the front edge
- Opposite edge = back; remaining edges = sides
- Cross-check with Microsoft US Building Footprints (free dataset, ~130M US polygons) for the actual structure position
- New column `road_side` on parcels: `{front, back, side, none}`

### Why later
The binary `on_main_road` gives most of the lift in Phase 4's impact model. Orientation is a refinement that should follow Phase 4 demonstrating the binary signal alone isn't sufficient — i.e., we know we need orientation when the impact model's error is dominated by orientation-confounded cases.

### Effort estimate
~5-10 days (heavily dependent on address-point quality)

---

## Phase 7 — Auto-suggest comp ratings
**Goal:** Surface the road-adjacency value impact in the existing "Good Comp" / "Bad Comp" rating UI so Mike's team gets system recommendations.

### Scope

- When a comp is `on_main_road=true` AND Phase 4 impact model predicts >5% value depression, surface a "Possible bad comp — main road" suggestion in the comp panel
- Team can confirm or override; either action is more training data for the impact model
- Eventually: predicted impact becomes a column in the CSV export Mike already uses

### Effort estimate
~3-5 days

---

## Phase 8 (optional, very late) — LLM chat interface
**Goal:** Allow Mike to ask plain-language questions about properties via Telegram or similar.

### Scope

- Wrap Layer 1 + Layer 2 + Layer 3 outputs with an LLM that can answer "is 123 Main St a good comp? why or why not?" — the ML outputs are the LLM's INPUT, not its reasoning, which keeps it grounded
- Telegram bot integration if that's Mike's preferred channel

### Why optional
Adds significant cost and ops complexity. Only do this if Mike actively wants conversational access vs the existing map UI.

---

## Dependencies summary

```
Phase 1 (POC)
    ├── Phase 2 (generalize to all DFW)
    │       └── Phase 3 (value-deviation engine)
    │               └── Phase 4 (impact ML model — the core AI piece)
    │                       ├── Phase 7 (auto-suggest comp ratings)
    │                       └── Phase 8 (LLM chat — optional)
    ├── Phase 5 (heat-map UI — independent, can ship after Phase 1)
    └── Phase 6 (orientation — independent, needs address-point audit)
```

Phases 5 and 6 can happen in parallel with the value-deviation/impact work.

---

## Status as of 2026-05-22

- Phase 0: in progress (this commit writes the docs)
- Phase 1: ready to spec after KK reviews + thinks more
- Phase 2-8: roadmap-only, not designed yet

## Open meta-questions to resolve before kicking off Phase 1

1. Exact Dallas sample-area polygon (KK to specify)
2. Branch name confirmation: `feat/road-adjacency`?
3. Mike-time budget for labeling
4. Flag UI states: strict yes/no, or include "skip / uncertain"?
5. Priority relative to other open items in the master TODO

---

## Decisions locked 2026-05-22

- Detection is binary touch/don't-touch (NOT distance-based)
- "Main" is contextual but Phase 1 starts with absolute thresholds in an urban Dallas sample, then Phase 2 generalizes with local-relative thresholds
- Two-layer model: detection (POC) is separable from impact (V3+)
- Multi-signal detection (OSM tags + lanes + maxspeed + TxDOT AADT), not pure tag-matching
- Branch in monorepo until Phase 4+
- AI = tabular ML, not LLM; LLM is optional Phase 8
