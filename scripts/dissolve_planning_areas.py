#!/usr/bin/env python3
# scripts/dissolve_planning_areas.py
#
# docs/AI/SPEC_MCKINNEY_ZONES_INGEST_2026-08-26.md deliverable 2 -- dissolve a
# raw planning-area GeoJSON (many small polygons, each carrying a group name
# field) into one polygon per distinct group name.
#
# Snapshot-artefact tool only: its output is never read by
# scripts/ingest_school_zones.py or any adapter, and is never referenced by
# a config.json. It exists to turn a pre-realignment planning-area layer
# (e.g. McKinney's 449-row ELEMENTARY BOUNDARIES) into a human-inspectable
# "21 elementary attendance areas" artefact while that level stays HELD out
# of the DB (spec §0, §2: "No elementary rows reach any DB, throwaway
# included, under any flag").
#
# Reused by docs/AI/SPEC_COPPELL_ZONES_INGEST_2026-08-26.md, where the output
# IS ingested (Coppell's 236 planning areas -> 10 elementary zones) and where a
# non-campus placeholder value has to be dropped -- hence --exclude-value.
#
# Run:
#   .venv/bin/python3 scripts/dissolve_planning_areas.py \
#       --in ingest/schools/2026-08-26/mckinney/elementary.geojson \
#       --group-field ELEM_NAME \
#       --out ingest/schools/2026-08-26/mckinney/elementary_dissolved.geojson
#
#   .venv/bin/python3 scripts/dissolve_planning_areas.py \
#       --in ingest/schools/2026-08-26/coppell/raw_layer5_elementary.geojson \
#       --group-field ELEM_NAME --exclude-value LAKE \
#       --out ingest/schools/2026-08-26/coppell/elementary_dissolved.geojson
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from shapely.geometry import mapping, shape
from shapely.ops import unary_union
from shapely.validation import explain_validity, make_valid

# Total area (in the source file's own coordinate units -- no reprojection
# happens here, same units in and out) must be conserved by the dissolve to
# within this fraction. A clean dissolve of non-overlapping input polygons
# conserves area exactly (area is additive over any partition, merging two
# polygons that share an edge doesn't change their combined area) -- this
# tolerance exists only to absorb floating-point noise from unary_union, not
# because area loss/gain is expected.
AREA_TOLERANCE = 0.001


def _polygonal_parts(geom):
    """The Polygon/MultiPolygon components of a make_valid() result.
    make_valid can return a GeometryCollection mixing polygons with
    zero-area lines/points where a ring pinched; those carry no area and
    are not zones. Dropping them is only safe because the caller asserts
    area preservation immediately afterwards."""
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return [geom]
    if geom.geom_type == "GeometryCollection":
        return [g for g in geom.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
    return []


def dissolve_by_field(
    doc: dict[str, Any],
    group_field: str,
    exclude_values: "set[str] | None" = None,
    repair_invalid: bool = False,
) -> dict[str, Any]:
    """Groups every Feature by `properties[group_field]` and unary_unions
    each group's geometry into one output Feature (MultiPolygon allowed --
    a district's planning areas for one campus name are not guaranteed
    contiguous). Raises if any input feature is missing the group field or
    has an invalid/absent geometry -- never silently drops a row into a
    fabricated "unknown" bucket.

    `exclude_values` names group values that are NOT campuses and must not
    become an output feature -- e.g. Coppell's `ELEM_NAME='LAKE'` placeholder
    row, a water body carried in the district's planning-area layer
    (docs/AI/SPEC_COPPELL_ZONES_INGEST_2026-08-26.md gate G-LAKE: "zero rows
    named LAKE reach any DB"). Excluding here rather than by hand-editing the
    raw snapshot keeps the snapshot faithful to the service. An exclusion that
    matches NOTHING raises: a silently-ineffective drop filter is exactly the
    failure this gate exists to catch (a renamed placeholder would otherwise
    sail through into the DB). Excluded rows are reported in the output
    `properties` and are removed from the area-conservation accounting --
    the invariant is "no area lost among the groups we KEEP," not "input
    total == output total," which exclusion legitimately breaks.

    `repair_invalid` opts in to repairing an invalid input geometry with
    shapely's make_valid instead of raising. It stays OFF by default: an
    invalid polygon is normally a signal worth stopping on, and silently
    repairing every source would hide real defects. The repair is only
    accepted when it preserves the feature's area to within AREA_TOLERANCE
    -- a make_valid that materially changes a zone's footprint is still a
    hard error -- and every repair is counted into the output `properties`
    so it is visible downstream rather than invisible. Argyle ISD's GIBSON
    middle polygon is the case this exists for: one ring self-intersection,
    repaired with zero area change (docs/AI/SPEC_COPPELL_ZONES_INGEST_2026-08-26.md
    ADDENDUM 2)."""
    excluded = set(exclude_values or ())
    groups: dict[str, list] = defaultdict(list)
    excluded_counts: dict[str, int] = defaultdict(int)
    repaired: list[str] = []
    for feat in doc["features"]:
        name = feat["properties"].get(group_field)
        if not name:
            raise ValueError(f"feature {feat.get('id')} missing {group_field!r}")
        geom = shape(feat["geometry"])
        if not geom.is_valid:
            if not repair_invalid:
                raise ValueError(
                    f"feature {feat.get('id')} ({name}) has an invalid geometry: "
                    f"{explain_validity(geom)}. Pass repair_invalid/--make-valid to "
                    "repair it with make_valid (area preservation is asserted)."
                )
            before = geom.area
            parts = _polygonal_parts(make_valid(geom))
            if not parts:
                raise ValueError(
                    f"feature {feat.get('id')} ({name}) is invalid and make_valid left "
                    "no polygonal geometry -- refusing to drop a zone"
                )
            repaired_geom = unary_union(parts)
            if before > 0 and abs(repaired_geom.area - before) / before > AREA_TOLERANCE:
                raise ValueError(
                    f"feature {feat.get('id')} ({name}): make_valid changed the area by "
                    f"{abs(repaired_geom.area - before) / before:.4%}, over tolerance "
                    f"{AREA_TOLERANCE:.2%} -- this is a real geometry defect, not a topology nit"
                )
            geom = repaired_geom
            repaired.append(str(name))
        if name in excluded:
            excluded_counts[name] += 1
            continue
        groups[name].append(geom)

    unmatched = excluded - set(excluded_counts)
    if unmatched:
        raise ValueError(
            f"exclude_values {sorted(unmatched)!r} matched no feature's {group_field!r} -- "
            "refusing a silently-ineffective exclusion (the placeholder may have been "
            "renamed upstream; re-check the source before ingesting)"
        )

    input_area = sum(g.area for parts in groups.values() for g in parts)

    out_features = []
    output_area = 0.0
    for name in sorted(groups):
        dissolved = unary_union(groups[name])
        output_area += dissolved.area
        out_features.append({
            "type": "Feature",
            "properties": {group_field: name, "source_planning_areas": len(groups[name])},
            "geometry": mapping(dissolved),
        })

    if input_area > 0:
        rel_diff = abs(output_area - input_area) / input_area
        if rel_diff > AREA_TOLERANCE:
            raise ValueError(
                f"dissolve area not conserved: input={input_area!r} output={output_area!r} "
                f"rel_diff={rel_diff:.4%} exceeds tolerance {AREA_TOLERANCE:.2%}"
            )

    return {
        "type": "FeatureCollection",
        "features": out_features,
        "properties": {
            "dissolved_by": group_field,
            "input_feature_count": len(doc["features"]),
            "excluded_values": {k: excluded_counts[k] for k in sorted(excluded_counts)},
            "excluded_feature_count": sum(excluded_counts.values()),
            "repaired_input_geometries": sorted(repaired),
            "output_feature_count": len(out_features),
            "input_area": input_area,
            "output_area": output_area,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="in_path", required=True)
    parser.add_argument("--group-field", required=True)
    parser.add_argument(
        "--exclude-value",
        dest="exclude_values",
        action="append",
        default=[],
        metavar="VALUE",
        help="A --group-field value that is NOT a campus and must not become an "
             "output feature (repeatable). Errors if it matches no feature.",
    )
    parser.add_argument(
        "--make-valid",
        dest="repair_invalid",
        action="store_true",
        help="Repair invalid input geometries with make_valid instead of aborting. "
             "Area preservation is asserted and every repair is reported.",
    )
    parser.add_argument("--out", dest="out_path", required=True)
    args = parser.parse_args()

    doc = json.loads(Path(args.in_path).read_text())
    result = dissolve_by_field(
        doc, args.group_field, set(args.exclude_values), repair_invalid=args.repair_invalid
    )
    Path(args.out_path).write_text(json.dumps(result))
    print(
        f"[dissolve_planning_areas] {args.in_path}: "
        f"{result['properties']['input_feature_count']} -> "
        f"{result['properties']['output_feature_count']} features "
        f"dissolved by {args.group_field!r} "
        f"(excluded {result['properties']['excluded_feature_count']} feature(s): "
        f"{result['properties']['excluded_values'] or 'none'}; "
        f"repaired {len(result['properties']['repaired_input_geometries'])}: "
        f"{result['properties']['repaired_input_geometries'] or 'none'}), area conserved "
        f"(input={result['properties']['input_area']:.6f}, "
        f"output={result['properties']['output_area']:.6f})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
