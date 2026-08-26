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
# Run:
#   .venv/bin/python3 scripts/dissolve_planning_areas.py \
#       --in ingest/schools/2026-08-26/mckinney/elementary.geojson \
#       --group-field ELEM_NAME \
#       --out ingest/schools/2026-08-26/mckinney/elementary_dissolved.geojson
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from shapely.geometry import mapping, shape
from shapely.ops import unary_union

# Total area (in the source file's own coordinate units -- no reprojection
# happens here, same units in and out) must be conserved by the dissolve to
# within this fraction. A clean dissolve of non-overlapping input polygons
# conserves area exactly (area is additive over any partition, merging two
# polygons that share an edge doesn't change their combined area) -- this
# tolerance exists only to absorb floating-point noise from unary_union, not
# because area loss/gain is expected.
AREA_TOLERANCE = 0.001


def dissolve_by_field(doc: dict[str, Any], group_field: str) -> dict[str, Any]:
    """Groups every Feature by `properties[group_field]` and unary_unions
    each group's geometry into one output Feature (MultiPolygon allowed --
    a district's planning areas for one campus name are not guaranteed
    contiguous). Raises if any input feature is missing the group field or
    has an invalid/absent geometry -- never silently drops a row into a
    fabricated "unknown" bucket."""
    groups: dict[str, list] = defaultdict(list)
    for feat in doc["features"]:
        name = feat["properties"].get(group_field)
        if not name:
            raise ValueError(f"feature {feat.get('id')} missing {group_field!r}")
        geom = shape(feat["geometry"])
        if not geom.is_valid:
            raise ValueError(f"feature {feat.get('id')} ({name}) has an invalid geometry")
        groups[name].append(geom)

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
            "output_feature_count": len(out_features),
            "input_area": input_area,
            "output_area": output_area,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="in_path", required=True)
    parser.add_argument("--group-field", required=True)
    parser.add_argument("--out", dest="out_path", required=True)
    args = parser.parse_args()

    doc = json.loads(Path(args.in_path).read_text())
    result = dissolve_by_field(doc, args.group_field)
    Path(args.out_path).write_text(json.dumps(result))
    print(
        f"[dissolve_planning_areas] {args.in_path}: "
        f"{result['properties']['input_feature_count']} -> "
        f"{result['properties']['output_feature_count']} features "
        f"dissolved by {args.group_field!r}, area conserved "
        f"(input={result['properties']['input_area']:.6f}, "
        f"output={result['properties']['output_area']:.6f})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
