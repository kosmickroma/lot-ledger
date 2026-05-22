---
created: 2026-05-22
status: brainstorm-captured
last_updated: 2026-05-22
---

# Road-Adjacency Detection — Long-Term Initiative

**Status:** Brainstorm state captured 2026-05-22. POC scope and design under review by KK. Not yet approved for implementation.

## What this is

A multi-phase initiative to automate one of Mike's team's most frequent manual judgments: identifying properties whose value is depressed by proximity to a main/busy road. Long-term, this becomes the foundation for the broader value-deviation engine that powers automated comp rating.

## Why now

- Mike's team currently does this entirely by visual inspection on a map. Manual, slow, varies between team members.
- Every existing "Good Comp / Bad Comp" rating implicitly weighs road-adjacency. Capturing this signal explicitly creates training data for future automation — and that automation does not need to be an LLM.
- Research dive 2026-05-22 confirmed no flipper-tool competitor (Propelio, PropStream, PropertyRadar, BatchLeads, DealMachine, Privy, REIPro, ListSource) solves this. It's a market gap LotLedger can fill.
- The closest direct buy is HowLoud SoundScore ($100/mo for 50k queries). The DIY path with OSM + TxDOT AADT in PostGIS is free and Texas-native, and we already run PostGIS.

## The two-layer mental model (key framing after 2026-05-22 discussion)

**Layer 1: Main-road detection (the primitive)**
- "Is this parcel touching a main road, yes/no?"
- Must work everywhere in DFW automatically, on any parcel, regardless of whether sale data exists in that area
- Multi-signal: OSM `highway` class + OSM `lanes` + OSM `maxspeed` + TxDOT AADT (where available)
- "Main" is relative to local context — an urban arterial and a small-town main thoroughfare both qualify; the threshold needs to be local-relative not absolute

**Layer 2: Value-impact (the consequence)**
- "Does being on this main road actually hurt the property's value here?"
- Depends on neighborhood character (urban vs rural, working-class vs luxury — same road type can have opposite impact)
- Built on top of Layer 1; takes the primitive plus neighborhood context
- Eventually learned from team flags + Propelio sale data via tabular ML

**The POC owns Layer 1 only.** Layer 2 is V3+ work. This separation came from KK's observation that what the team ACTUALLY cares about is value loss, and the main road is just the most common observable cause — distinguishing detection from impact lets each layer be solved on its own merits.

## What we ARE building (POC scope, locked)

- Binary on_main_road detection per parcel
- Multi-signal rule (not pure OSM tag matching — KK explicitly redirected away from that)
- New flag button on parcel popup for team to mark ground truth
- New boolean column on parcels for both system-computed and team-flagged values
- Sample area: a specific Dallas neighborhood with two-lane non-highway "considered main" roads (KK has area in mind — exact polygon to be specified)

## What we are NOT building yet

- Distance-to-nearest-main-road continuous score (KK's framing is binary adjacency, not distance)
- Front/back/side orientation (deferred to Phase 6; needs address-point precision audit first)
- The value-deviation engine itself (Phase 3+)
- Traffic heat-map UI layer (Phase 5)
- LLM chat interface (Phase 8, optional)

## Why not just LLM this

A learned tabular model — logistic regression, decision tree, XGBoost — trained on structured signals (OSM class, lanes, AADT, neighborhood density) is the right tool. An LLM would be wasteful and ungrounded for "given these signals, predict main-road yes/no." LLM could later layer on for Mike-conversational Q&A via Telegram or similar, but that's Phase 8 and not load-bearing for the core engine.

## Documents in this folder

- [ROADMAP.md](./ROADMAP.md) — phased plan from now through the full vision, including detailed POC scope in Phase 1
- [RESEARCH_DEEP_DIVE.md](./RESEARCH_DEEP_DIVE.md) — consolidated findings from 4 parallel research dives 2026-05-22 (commercial landscape, technical approaches, academic literature on hedonic pricing, computer-vision approaches)

## Open meta-questions for KK to mull

1. **Sample area specifics** — which exact Dallas ZIP or neighborhood polygon? KK has an area in mind with two-lane non-highway "considered main" roads.
2. **Branch / repo structure** — current proposal: feature branch `feat/road-adjacency` in monorepo until at least Phase 4. Promote to separate repo only if the surface grows significantly.
3. **Priority** — where does this sit relative to other open items (TIGER Places city resolution, multifamily/duplex split, regular-users-inherit-filters bug, GCP SQL password rotation)?
4. **Mike-time investment for team labeling in POC** — 1 hour total? 4 hours? 10? Affects how many ground-truth parcels we collect before iterating.
5. **Flag UI granularity** — strict yes/no, or include a "skip / uncertain" state?
6. **CSV column communication** — Phase 1 adds a column to parcels and eventually to the propelio_comps export. When and how to communicate to Mike.

## Decisions locked in this session (2026-05-22)

- Detection target: binary "on main road" (not distance, not orientation in POC)
- Ground-truth source: team flags in a sample area (KK picks the area)
- Flag mechanism: new button on parcel popup, T/F column
- Data source: multi-signal — OSM + TxDOT AADT (NOT OSM-only; KK's correction: must capture actual busy-ness, not just tag-matching)
- Two-layer model: detection (POC) vs impact (V3+)
- "Main" is relative to local context (v2 generalization)
- AI = tabular ML, not LLM (LLM is optional V8)
