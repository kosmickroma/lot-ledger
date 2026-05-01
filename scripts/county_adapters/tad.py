# scripts/county_adapters/tad.py
#
# Tarrant Appraisal District (TAD) county adapter profile.
# Encodes TAD-specific ingest expectations and validates common pitfalls.
#
# Connects to:
#   scripts/county_adapters/base.py  - shared adapter validation logic
#   scripts/validate_tad_extract.py  - CLI validation entrypoint
#   ingest/counties/tarrant/tad/...  - local extracted TAD datasets

from __future__ import annotations

from pathlib import Path

from scripts.county_adapters.base import CountyAdapterBase, CountyIngestConfig, ValidationIssue


TAD_CONFIG = CountyIngestConfig(
    county="tarrant",
    district="tad",
    expected_dataset_dirs=[
        "ParcelView",
        "Parcels_GeoDatabase",
        "County",
        "Cities",
        "Schools",
        "Neighborhoods",
        "Subdivisions",
        "MUDS",
        "PIDS",
        "TIFS",
        "Lakes",
        "Creeks",
    ],
    required_shapefile_dirs=[
        "ParcelView",
        "County",
        "Cities",
        "Schools",
        "Neighborhoods",
        "Subdivisions",
        "MUDS",
        "PIDS",
        "TIFS",
        "Lakes",
        "Creeks",
    ],
    fallback_encodings=["utf-8", "latin-1", "cp1252"],
    text_delimiter="|",
    id_fields=["ACCOUNT_NUM", "PID", "PARCEL_ID", "GIS_PARCEL_ID"],
)


class TarrantAdapter(CountyAdapterBase):
    def __init__(self) -> None:
        super().__init__(TAD_CONFIG)

    def validate_extract(self, unzipped_root: Path) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        issues.extend(self.validate_required_dirs(unzipped_root))
        issues.extend(self.validate_shapefile_parts(unzipped_root))

        parcel_prj = unzipped_root / "ParcelView" / "ParcelView.prj"
        proj = self.inspect_projection(parcel_prj)
        if proj.appears_state_plane_feet:
            issues.append(
                ValidationIssue(
                    level="warning",
                    code="projection_not_wgs84",
                    message="ParcelView appears to be state-plane/feet; reproject to EPSG:4326 before Leaflet output.",
                    path=str(parcel_prj),
                )
            )
        elif not proj.is_wgs84:
            issues.append(
                ValidationIssue(
                    level="warning",
                    code="projection_unknown",
                    message="Could not confirm WGS84 projection from ParcelView .prj.",
                    path=str(parcel_prj),
                )
            )

        # Optional text-dump checks: run only when those files exist.
        for probe_name in ["Residential_Property_Data.txt", "Commercial_Property_Data.txt"]:
            probe_path = unzipped_root / probe_name
            if not probe_path.exists():
                continue

            delim = self.sniff_delimiter(probe_path)
            if delim != self.config.text_delimiter:
                issues.append(
                    ValidationIssue(
                        level="warning",
                        code="unexpected_delimiter",
                        message=f"{probe_name} delimiter detected as '{delim}', expected '|'.",
                        path=str(probe_path),
                    )
                )

            enc = self.detect_encoding(probe_path)
            if enc is None:
                issues.append(
                    ValidationIssue(
                        level="error",
                        code="encoding_unreadable",
                        message=f"Could not decode {probe_name} with fallback encodings.",
                        path=str(probe_path),
                    )
                )
                continue

            df = self.load_text_table(probe_path, delimiter=delim, encoding=enc, id_fields=self.config.id_fields)
            issues.extend(self.validate_id_columns_as_text(df, self.config.id_fields))

        gdb_path = unzipped_root / "Parcels_GeoDatabase" / "ParcelsGDB" / "TADData.gdb"
        if not gdb_path.exists():
            issues.append(
                ValidationIssue(
                    level="error",
                    code="missing_gdb",
                    message="Parcels GeoDatabase folder TADData.gdb is missing.",
                    path=str(gdb_path),
                )
            )

        return issues
