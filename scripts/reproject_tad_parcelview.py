# scripts/reproject_tad_parcelview.py
#
# Reproject Tarrant ParcelView shapefile from TAD state-plane feet to WGS84.
# Uses pure Python so it works even when ogr2ogr/GDAL is unavailable.
#
# Connects to:
#   ingest/counties/tarrant/tad/<snapshot>/unzipped/ParcelView/ParcelView.shp
#   ingest/counties/tarrant/tad/<snapshot>/normalized/ParcelView_4326/
#   scripts/validate_tad_extract.py (Phase N)

from __future__ import annotations

import argparse
import importlib
from pathlib import Path
import sys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reproject TAD ParcelView shapefile to EPSG:4326 (WGS84)."
    )
    parser.add_argument(
        "--source-shp",
        default="ingest/counties/tarrant/tad/2026-05-01/unzipped/ParcelView/ParcelView.shp",
        help="Path to source ParcelView .shp file.",
    )
    parser.add_argument(
        "--target-dir",
        default="ingest/counties/tarrant/tad/2026-05-01/normalized/ParcelView_4326",
        help="Output folder for reprojected shapefile components.",
    )
    parser.add_argument(
        "--source-epsg",
        type=int,
        default=2276,
        help="Source EPSG code (TAD is typically 2276).",
    )
    parser.add_argument(
        "--target-epsg",
        type=int,
        default=4326,
        help="Target EPSG code (Leaflet expects 4326).",
    )
    parser.add_argument(
        "--encoding",
        default="latin-1",
        help="DBF text encoding to use while reading/writing records.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite output shapefile if it already exists.",
    )
    return parser.parse_args()


def _delete_output_components(base_path: Path) -> None:
    for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        candidate = base_path.with_suffix(ext)
        if candidate.exists():
            candidate.unlink()


def _split_parts(points: list[tuple[float, float]], parts: list[int]) -> list[list[tuple[float, float]]]:
    if not parts:
        return [points] if points else []
    boundaries = list(parts) + [len(points)]
    chunks = [points[boundaries[i] : boundaries[i + 1]] for i in range(len(boundaries) - 1)]
    return [chunk for chunk in chunks if chunk]


def _write_transformed_shape(writer, shapefile_module, shape, points: list[tuple[float, float]]) -> bool:
    polygon_types = {
        shapefile_module.POLYGON,
        getattr(shapefile_module, "POLYGONM", -1),
        getattr(shapefile_module, "POLYGONZ", -1),
    }
    polyline_types = {
        shapefile_module.POLYLINE,
        getattr(shapefile_module, "POLYLINEM", -1),
        getattr(shapefile_module, "POLYLINEZ", -1),
    }
    point_types = {
        shapefile_module.POINT,
        getattr(shapefile_module, "POINTM", -1),
        getattr(shapefile_module, "POINTZ", -1),
    }
    multipoint_types = {
        shapefile_module.MULTIPOINT,
        getattr(shapefile_module, "MULTIPOINTM", -1),
        getattr(shapefile_module, "MULTIPOINTZ", -1),
    }

    if shape.shapeType in polygon_types:
        rings = [ring for ring in _split_parts(points, list(shape.parts)) if len(ring) >= 3]
        if not rings:
            return False
        closed_rings: list[list[tuple[float, float]]] = []
        for ring in rings:
            if ring[0] != ring[-1]:
                ring = ring + [ring[0]]
            closed_rings.append(ring)
        writer.poly(closed_rings)
        return True
    elif shape.shapeType in polyline_types:
        lines = [line for line in _split_parts(points, list(shape.parts)) if len(line) >= 2]
        if not lines:
            return False
        writer.line(lines)
        return True
    elif shape.shapeType in point_types:
        if not points:
            return False
        writer.point(*points[0])
        return True
    elif shape.shapeType in multipoint_types:
        if not points:
            return False
        writer.multipoint(points)
        return True
    else:
        raise ValueError(f"Unsupported shape type: {shape.shapeType}")


def main() -> int:
    args = parse_args()

    src_shp = Path(args.source_shp)
    target_dir = Path(args.target_dir)
    out_base = target_dir / src_shp.stem

    if not src_shp.exists():
        print(f"ERROR: source shapefile not found: {src_shp}")
        return 2

    try:
        shapefile = importlib.import_module("shapefile")
        pyproj = importlib.import_module("pyproj")
        CRS = pyproj.CRS
        Transformer = pyproj.Transformer
    except ModuleNotFoundError as exc:
        print("ERROR: missing dependency for reprojection.")
        print("Install with: pip install pyshp pyproj")
        print(f"Missing module: {exc.name}")
        return 2

    out_exists = any(out_base.with_suffix(ext).exists() for ext in (".shp", ".shx", ".dbf"))
    if out_exists and not args.overwrite:
        print(f"ERROR: output already exists: {out_base}")
        print("Re-run with --overwrite to replace existing files.")
        return 2

    target_dir.mkdir(parents=True, exist_ok=True)
    if args.overwrite:
        _delete_output_components(out_base)

    transformer = Transformer.from_crs(
        CRS.from_epsg(args.source_epsg),
        CRS.from_epsg(args.target_epsg),
        always_xy=True,
    )

    reader = shapefile.Reader(str(src_shp), encoding=args.encoding)
    writer = shapefile.Writer(str(out_base), shapeType=reader.shapeType, encoding=args.encoding)

    try:
        for field in reader.fields[1:]:
            writer.field(*field)

        processed = 0
        skipped = 0
        for shape_record in reader.iterShapeRecords():
            shape = shape_record.shape
            transformed_points = [transformer.transform(x, y) for x, y in shape.points]

            wrote = _write_transformed_shape(writer, shapefile, shape, transformed_points)
            if not wrote:
                skipped += 1
                continue
            writer.record(*shape_record.record)

            processed += 1
            if processed % 50000 == 0:
                print(f"Processed {processed:,} features...")
    finally:
        writer.close()
        reader.close()

    prj_path = out_base.with_suffix(".prj")
    prj_path.write_text(CRS.from_epsg(args.target_epsg).to_wkt(), encoding="utf-8")

    cpg_path = out_base.with_suffix(".cpg")
    cpg_path.write_text(args.encoding, encoding="utf-8")

    print("DONE")
    print(f"Input:  {src_shp}")
    print(f"Output: {out_base.with_suffix('.shp')}")
    print(f"CRS:    EPSG:{args.source_epsg} -> EPSG:{args.target_epsg}")
    print(f"Kept:   {processed:,}")
    print(f"Skipped malformed features: {skipped:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
