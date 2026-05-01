# scripts/county_adapters/base.py
#
# Shared county adapter primitives for ingest validation and safe tabular loading.
# Provides delimiter/encoding/projection checks and ID-preservation safeguards.
#
# Connects to:
#   scripts/county_adapters/tad.py      - concrete county adapter implementation
#   scripts/validate_tad_extract.py     - CLI validation entrypoint
#   ingest/counties/.../unzipped/       - source dataset folders

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import csv
from typing import Iterable

import pandas as pd


@dataclass
class ValidationIssue:
    level: str
    code: str
    message: str
    path: str | None = None


@dataclass
class ProjectionStatus:
    is_wgs84: bool
    appears_state_plane_feet: bool
    raw_hint: str


@dataclass
class CountyIngestConfig:
    county: str
    district: str
    expected_dataset_dirs: list[str]
    required_shapefile_dirs: list[str]
    fallback_encodings: list[str] = field(default_factory=lambda: ["utf-8", "latin-1", "cp1252"])
    text_delimiter: str = "|"
    id_fields: list[str] = field(default_factory=list)


class CountyAdapterBase:
    def __init__(self, config: CountyIngestConfig) -> None:
        self.config = config

    def validate_required_dirs(self, unzipped_root: Path) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for folder in self.config.expected_dataset_dirs:
            target = unzipped_root / folder
            if not target.exists():
                issues.append(
                    ValidationIssue(
                        level="error",
                        code="missing_dataset_dir",
                        message=f"Missing expected dataset folder: {folder}",
                        path=str(target),
                    )
                )
        return issues

    def validate_shapefile_parts(self, unzipped_root: Path) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        needed = {".shp", ".shx", ".dbf", ".prj"}
        for folder in self.config.required_shapefile_dirs:
            target = unzipped_root / folder
            if not target.exists():
                continue
            exts = {p.suffix.lower() for p in target.iterdir() if p.is_file()}
            missing = sorted(needed - exts)
            if missing:
                issues.append(
                    ValidationIssue(
                        level="error",
                        code="missing_shapefile_sidecars",
                        message=f"{folder} missing required components: {', '.join(missing)}",
                        path=str(target),
                    )
                )
        return issues

    def inspect_projection(self, prj_path: Path) -> ProjectionStatus:
        if not prj_path.exists():
            return ProjectionStatus(False, False, "missing .prj")

        text = prj_path.read_text(errors="ignore").upper()
        is_wgs84 = "WGS_1984" in text or "EPSG\",\"4326" in text or "GEOGCS[\"GCS_WGS_1984\"" in text
        appears_state_plane_feet = (
            "STATEPLANE" in text
            or "TEXAS_NORTH_CENTRAL" in text
            or ("FOOT" in text and "PROJCS" in text)
        )
        hint = "WGS84" if is_wgs84 else ("state-plane/feet" if appears_state_plane_feet else "unknown")
        return ProjectionStatus(is_wgs84, appears_state_plane_feet, hint)

    def sniff_delimiter(self, file_path: Path, sample_lines: int = 30) -> str:
        lines: list[str] = []
        with file_path.open("r", encoding="latin-1", errors="replace") as fh:
            for _ in range(sample_lines):
                line = fh.readline()
                if not line:
                    break
                lines.append(line)

        sample = "".join(lines)
        if not sample.strip():
            return ","

        try:
            dialect = csv.Sniffer().sniff(sample, delimiters="|,\t;")
            return dialect.delimiter
        except csv.Error:
            pipe_count = sample.count("|")
            comma_count = sample.count(",")
            tab_count = sample.count("\t")
            if pipe_count >= comma_count and pipe_count >= tab_count:
                return "|"
            if tab_count >= comma_count:
                return "\t"
            return ","

    def detect_encoding(self, file_path: Path, candidates: Iterable[str] | None = None) -> str | None:
        options = list(candidates or self.config.fallback_encodings)
        raw = file_path.read_bytes()[:200000]
        for enc in options:
            try:
                raw.decode(enc)
                return enc
            except UnicodeDecodeError:
                continue
        return None

    def load_text_table(self, file_path: Path, delimiter: str, encoding: str, id_fields: list[str]) -> pd.DataFrame:
        dtype_map = {field: "string" for field in id_fields}
        return pd.read_csv(
            file_path,
            sep=delimiter,
            dtype=dtype_map or "string",
            encoding=encoding,
            keep_default_na=False,
            low_memory=False,
        )

    def validate_id_columns_as_text(self, df: pd.DataFrame, id_fields: list[str]) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for field in id_fields:
            if field not in df.columns:
                issues.append(
                    ValidationIssue(
                        level="warning",
                        code="missing_id_column",
                        message=f"ID field not present: {field}",
                    )
                )
                continue

            series = df[field].astype("string")
            has_numeric_cast_risk = series.str.match(r"^\d+$", na=False).any()
            has_leading_zero = series.str.match(r"^0\d+", na=False).any()
            if has_numeric_cast_risk and has_leading_zero:
                # This is informative: confirms we must preserve text type.
                issues.append(
                    ValidationIssue(
                        level="info",
                        code="leading_zero_ids_detected",
                        message=f"Leading-zero IDs detected in {field}; preserve as text on every import.",
                    )
                )
        return issues
