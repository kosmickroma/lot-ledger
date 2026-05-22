---
created: 2026-05-22
status: findings-captured-pre-decision
last_updated: 2026-05-22
---

# Property-Type Classification — Audit Findings & Open Questions

**Status:** Findings captured 2026-05-22 from code review. No use case locked in yet — KK to mull and/or talk to Mike about which property types matter for which workflows before any design. Parallel to [docs/road-adjacency/](../road-adjacency/) in that we're in think-through mode, not implementation.

## What triggered this

KK was considering adding a separate "Duplex" filter/color bucket (originally bundled with the BANDERA bug fix in early planning, but the BANDERA fix shipped standalone). Discussion broadened to "what does multifamily actually mean across our 4 counties" and "where are townhouses." Pulled the cross-county classification code to see what's really there before designing anything.

## What the code currently does

### DCAD (`api/counties/dcad.py`)
- Has explicit `A12: "Townhouses"` label — **currently classified as `single_family`** (residential_sptd set includes A12; MF set is only B11/B12/A14/A13)
- B12 = Duplexes (cleanly separable — DCAD is the easy case)
- A14 = unknown/undocumented code, currently classified as multifamily
- Mobile homes (A20 = Mobile Home on Owner's Land) treated as residential SFR

### TAD (`api/counties/tad.py`)
- `_MF_CODES = {B1, B2, B3, B4, M1, M2}` — **mobile homes (M1, M2) are in the MF bucket** (likely wrong; inconsistent with DCAD)
- `_SFR_CODES = {A, A1, A2, A4, E1, EC}` — no townhouse mention
- A3 is in neither set (implicit other)
- Duplexes lumped into B1 with triplex/4-plex

### Collin (`api/counties/collin.py`)
- `_COLLIN_MF_CODES = {A3, B1, B2, B3, B4, B6, B9}` — **A3 (condo) is MF here**
- No townhouse code surfaced

### Denton (`api/counties/denton.py`)
- `_DENTON_MF_CODES = {B1, B2}`
- `_DENTON_SFR_CODES = {A1, A2, A3, A4, A5, A6, OA1, OA5}` — **A3 is SFR here, opposite of Collin**
- No townhouse code surfaced

### Frontend (`frontend/map.js`)
- 7 filter checkboxes / buckets: `active`, `sold`, `off_market`, `vacant`, `multifamily`, `commercial`, `exempt`
- Multifamily color: `#2c2c2c` (dark gray)
- Propelio comp categorization maps everything multi-unit (Duplex, Triplex, Quadruplex, MultiFamily, Apartment, Condominium, ResidentialIncome) → all roll up to "multifamily"
- No "townhouse" bucket exists anywhere in UI

## Cross-county inconsistencies (the actual mess)

1. **Townhouses are invisible.** No county surfaces them as their own bucket. DCAD has the A12 label but routes it to SFR. Others bundle townhouses into A1/A2 (general SFR) or A3 where present.
2. **A3 discrepancy.** Condo→MF in Collin, SFR in Denton. Same state code, opposite classification.
3. **Mobile homes.** MF in TAD (M1/M2), SFR in DCAD (A20). Different counties, different treatment.
4. **Duplex granularity.** Only DCAD (B12) cleanly distinguishes duplexes. TAD/Collin/Denton bundle duplexes with triplex/4-plex under B1 "2-4 units." Splitting just duplexes from B1 needs unit-count or another field.
5. **Unknown codes.** DCAD A14, Collin B6/B9 are in the MF set with no documented meaning. Worth investigating before any taxonomy change.

## Resolved framing (2026-05-22)

KK clarified the core use case: this is about **flipping and teardown targets**, not a general property-type browser. That asymmetrically constrains how detailed each bucket needs to be.

**Granularity follows flip-target relevance:**

| Tier | Buckets | Why detailed (or not) |
|---|---|---|
| **Detailed buckets** (own filter + color) | SFR, **Duplex (new)**, **Townhouse (new)**, possibly Mobile-on-OWNED-land, Vacant residential | Each could be a flip or teardown target with different economics |
| **Skip-marker buckets** (visible as "not for me" but no internal detail) | "Large MF" (apartments + condos lumped — Mike doesn't care to distinguish), Commercial, Exempt | Mike treats them all as "huge area we don't care about" |
| **Probably-skip buckets** (need a call) | Trailer parks + mobile-on-leased-land | Likely skip-bucket; not flip-targets |

**Implications for the earlier grouping questions:**

- **"Condos + apartments together?"** → **Yes, lump them.** Mike doesn't distinguish; both are "skip." No reason to spend UI surface or code complexity separating them.
- **"Duplexes + townhouses together?"** → **Probably keep separate.** Their flip economics differ (duplex = often income-producing or BRRRR-style; townhouse = SFR-like single-unit flip). Lean toward two distinct buckets, but not locked.
- **Trailer-park 3-way split simplifies** → mobile-on-OWNED-land might be a teardown target (low building value + dirt = teardown math worth running). Mobile-on-leased-land + trailer-parks-as-parcels are skip. Two buckets total instead of three.

**Still open under this framing:**

- Should mobile-on-owned-land actually get its own bucket, or roll into SFR? (Depends on whether Mike sees it as a teardown source.)
- Townhouse handling: separate bucket vs roll into SFR? (Lean separate, but verify with Mike.)
- ADUs / garage apartments / casitas — relevant or not? (Probably not at this stage; flag for later.)

## Open questions for KK to think about (and/or ask Mike)

### Bucket-structure questions (the actual grouping problem)

KK's framing 2026-05-22: what are the natural buckets, and what gets grouped with what? Examples he surfaced:

- **Duplexes + townhouses in one bucket?** Both are "small attached units, often individually owned." Or split them because townhouses are usually owner-occupied / SFR-like, and duplexes are usually income / MF-like?
- **Condos + apartments in one bucket?** Both are "units inside a larger building." Or split them because condos = individually owned (flip target style), apartments = single-owner rented (income property style)?
- **Trailer parks — where?** This is actually **3 different things** that need disentangling:
  1. The PARK itself (one parcel, many mobile homes on it) = commercial income property
  2. A single mobile home on OWNED land (DCAD A20, similar elsewhere) = behaves like SFR
  3. A single mobile home on LEASED land (DCAD M31/M32, TAD M1/M2) = chattel asset on someone else's dirt — neither fish nor fowl
  These probably need three different buckets, not one.

#### Possible grouping axes (any combo could define a bucket)

1. **Ownership style:**
   - Fee-simple (own dirt + structure): SFR, some townhouses, owned-outright duplexes
   - Condominium-style (own unit, share land): condos, condo-style townhouses
   - Tenant-rental (one owner rents out the whole property): small MF, apartments
   - Chattel (mobile home on someone else's land): DCAD M31/M32, TAD M1/M2

2. **Unit count:**
   - 1 unit (SFR, single condo, single townhouse)
   - 2 units (duplex)
   - 3-4 units (triplex, fourplex)
   - 5-24 units (small apartment / garden complex)
   - 25+ units (commercial-scale apartment)

3. **Flip strategy / Mike's use case:**
   - "Hunt" — what Mike actively wants to buy
   - "Avoid" — never wants to see, even as comps
   - "Comp-only" — useful for comparison, not as a target
   - "Income-alt" — different flip strategy (e.g., buy-and-rent for duplexes)

4. **Structural form:**
   - Detached (free-standing structure)
   - Attached (shared wall with neighbor — townhouse, duplex, rowhouse)
   - Mobile / movable (mobile home, manufactured home, RV)
   - Mixed multi-structure on one parcel (trailer park, garage apartment, ADU)

#### Mike-dependent questions (need his actual input)

- Which axis does Mike actually think in? Ownership style? Unit count? Flip strategy?
- Do duplexes and townhouses feel similar to him or different?
- Is a condo-style townhouse the same as a fee-simple townhouse in his head?
- Where does he see mobile-home-on-owned-land — closer to SFR, closer to mobile, or its own thing?
- Does he ever look at trailer parks as flip targets? Or are they pure commercial-bucket "never look here"?
- Are condos and apartments the same in his head, or fundamentally different deals?
- What about garage apartments / ADUs / casitas — own bucket or roll into SFR?

#### Data-availability questions (constrain what's even possible)

- Do our four county sources expose **ownership style** (fee-simple vs condo) anywhere? Probably not directly — would need to derive from legal description text or assume from state code, which is lossy.
- Do they expose **unit count**? DCAD likely has `NUM_UNITS` or similar; TAD might have `unit_count`. Need to check before designing buckets that depend on this.
- Are **detached vs attached** distinguishable from state codes alone? Townhouses are attached, SFR is detached — but DCAD's A11 could include both in practice.
- For trailer-parks-as-parcels: is there a parcel-level signal that says "this lot contains multiple mobile homes"? Or do they just show up as commercial / vacant?

### Use-case questions (drive everything)
- Does Mike want duplexes as a SEPARATE flip strategy (own filter, own color)?
- Does Mike want to AVOID duplexes when hunting SFR comps (better comp filtering)?
- Are townhouses interesting to Mike at all?
- Are there OTHER property types currently invisible that Mike actually wants to see?

### Scope questions
- Just a duplex split (smallest change, mostly DCAD-clean)?
- Full cross-county property-type re-taxonomy (largest change)?
- Audit-first approach (run code-distribution queries, then decide based on actual counts)?

### Cross-county harmonization questions
- Should A3 mean the same thing in Collin and Denton? If yes, which side is wrong — should A3 be MF (Collin's interpretation) or SFR (Denton's)?
- Should mobile homes be MF or SFR? Consistent across counties either way.
- Should the "multifamily" UI label become more granular? (e.g., "2-4 units," "5+ units," "apartments" as sub-categories rather than one bucket)

### Data-gap questions (could be resolved by a future audit)
- How many rows actually fall into each contested bucket? Some "inconsistencies" might be 5-row edge cases not worth fixing.
- Does each county's source data include a `unit_count` field we could use to split B1 into duplex / triplex / 4-plex sub-buckets?
- What are A14, B6, B9 — meaningful row counts or near-zero edge cases?
- How many M31/M32 DCAD rows exist (mobile-on-leased-land)? Is this even visible in our app right now or silently dropped?
- Are trailer-park parcels showing up as commercial currently, or as something else? Worth a targeted query.

## Possible next steps (when KK has direction)

- **Data audit** — Query the parcel DB for actual code distributions per county. Reveals what's worth caring about.
- **Talk to Mike** — Which property types matter to him? Which workflow does each serve? (Hunting vs avoiding vs comp-filtering are distinct surfaces.)
- **Design canonical taxonomy** — A single cross-county classification with explicit per-county code mapping. Resolves A3, mobile homes, A14 unknowns.
- **Filter UI decision** — Keep 7-bucket scheme + add a Duplex toggle, OR restructure MF into sub-types (Apartments / Duplex / Small MF / Condo).
- **Townhouse handling decision** — Surface separately, roll into SFR (DCAD's current behavior), or roll into condo-adjacent bucket.

## Why we're not coding yet

KK explicitly wants to think this through more. With no clear use case yet, designing a duplex filter risks shipping the wrong shape — e.g., if Mike actually cares about excluding duplexes from comps rather than hunting them, the right surface might be a comp-panel filter setting, not a parcel-color bucket. Different use cases imply very different UIs.

This doc captures current state. When KK has a direction (from talking to Mike, or from his own conviction), we can run the data audit and design the actual feature.

## File reference (for future Claude session continuity)

- `api/counties/dcad.py` — `SPTD_LABELS` dict + `classify_property` logic
- `api/counties/tad.py` — `_MF_CODES`, `_SFR_CODES`, `classify_property`
- `api/counties/collin.py` — `_COLLIN_MF_CODES`, `classify_property`
- `api/counties/denton.py` — `_DENTON_MF_CODES`, `_DENTON_SFR_CODES`, `classify_property`
- `frontend/map.js` — `PARCEL_LAYER_KEYS`, filter UI, `COMP_CATEGORY` map
