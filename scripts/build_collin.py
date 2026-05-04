# scripts/build_collin.py
#
# Collin CAD parcel loader with dry-run validation and optional DB write.
# Reads parcels_with_appraisal_data_R5 shapefile, reprojects to WGS84, and upserts
# into collin_parcels with optional per-property enrichment summaries.
#
# Connects to:
#   api/config.py                                              - shared DB connection helpers
#   ingest/counties/collin/cad/current/unzipped/...           - Collin shapefile and CSV exports
#   PostgreSQL/PostGIS                                         - writes collin_parcels table (Phase N)

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import re
import sys
from typing import Any

import pandas as pd
import shapefile
from psycopg2.extras import execute_values
from pyproj import CRS, Transformer

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.config import get_conn, release_conn


DEFAULT_SOURCE = Path(
    "ingest/counties/collin/cad/current/unzipped/parcels_with_appraisal_data_R5/parcels_with_appraisal_data_R5.shp"
)
DEFAULT_BASE_DIR = Path("ingest/counties/collin/cad/current/unzipped")
BATCH_SIZE = 4000


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "none" else text


def _to_float(value: Any) -> float | None:
    text = _clean_text(value).replace(",", "").replace("$", "").replace("%", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    num = _to_float(value)
    if num is None:
        return None
    try:
        return int(num)
    except (TypeError, ValueError):
        return None


def _to_bool_str(value: Any) -> str:
    text = _clean_text(value).lower()
    if text in {"true", "1", "y", "yes"}:
        return "true"
    if text in {"false", "0", "n", "no"}:
        return "false"
    return ""


def _split_codes(value: Any) -> list[str]:
    text = _clean_text(value)
    if not text:
        return []
    return [p.strip() for p in text.split(",") if p.strip()]


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = _clean_text(value)
        if text:
            return text
    return ""


def _resolve_csv(base_dir: Path, patterns: list[str]) -> Path | None:
    for pattern in patterns:
        matches = sorted(base_dir.glob(pattern))
        if matches:
            return matches[-1]
    return None


def _build_code_lookup(csv_path: Path | None) -> dict[str, str]:
    if csv_path is None or not csv_path.exists():
        return {}
    df = pd.read_csv(csv_path, dtype=str, encoding="utf-8", low_memory=False)
    if "Code" not in df.columns or "Name" not in df.columns:
        return {}
    lookup: dict[str, str] = {}
    for row in df.itertuples(index=False):
        code = _clean_text(getattr(row, "Code", "")).upper()
        name = _clean_text(getattr(row, "Name", ""))
        if code:
            lookup[code] = name
    return lookup


def _build_agent_summary(csv_path: Path | None) -> dict[str, dict[str, Any]]:
    if csv_path is None or not csv_path.exists():
        return {}

    cols = [
        "propID",
        "taxAgentID",
        "taxAgentName",
        "taxAgentAuthProtest",
        "taxAgentAuthResolve",
        "taxAgentMailings",
        "taxAgentEffectiveDate",
    ]
    df = pd.read_csv(csv_path, dtype=str, usecols=cols, encoding="utf-8", low_memory=False)
    df["propID"] = df["propID"].fillna("").astype(str).str.strip()
    df = df[df["propID"] != ""].copy()

    # Prefer the latest effective date when multiple agent rows exist.
    df["taxAgentEffectiveDate"] = pd.to_datetime(df["taxAgentEffectiveDate"], errors="coerce")
    df = df.sort_values(["propID", "taxAgentEffectiveDate"], ascending=[True, False])
    df = df.groupby("propID", as_index=False).first()

    out: dict[str, dict[str, Any]] = {}
    for row in df.itertuples(index=False):
        prop_id = _clean_text(getattr(row, "propID", ""))
        if not prop_id:
            continue
        out[prop_id] = {
            "tax_agent_id": _clean_text(getattr(row, "taxAgentID", "")),
            "tax_agent_name": _clean_text(getattr(row, "taxAgentName", "")),
            "tax_agent_auth_protest": _to_bool_str(getattr(row, "taxAgentAuthProtest", "")),
            "tax_agent_auth_resolve": _to_bool_str(getattr(row, "taxAgentAuthResolve", "")),
            "tax_agent_mailings": _clean_text(getattr(row, "taxAgentMailings", "")),
        }
    return out


def _build_permit_summary(csv_path: Path | None) -> dict[str, dict[str, Any]]:
    if csv_path is None or not csv_path.exists():
        return {}

    cols = ["propID", "permitIssuedDate", "permitTypeDescr", "permitValue"]
    df = pd.read_csv(csv_path, dtype=str, usecols=cols, encoding="utf-8", low_memory=False)
    df["propID"] = df["propID"].fillna("").astype(str).str.strip()
    df = df[df["propID"] != ""].copy()
    df["permitIssuedDate"] = pd.to_datetime(df["permitIssuedDate"], errors="coerce")
    df["permitValueNum"] = pd.to_numeric(
        df["permitValue"].fillna("").astype(str).str.replace(",", "").str.replace("$", "", regex=False),
        errors="coerce",
    )

    counts = df.groupby("propID").size().rename("permit_count")
    latest = df.sort_values(["propID", "permitIssuedDate"], ascending=[True, False]).groupby("propID").first()

    out: dict[str, dict[str, Any]] = {}
    for prop_id, row in latest.iterrows():
        out[prop_id] = {
            "permit_count": int(counts.get(prop_id, 0)),
            "latest_permit_date": "" if pd.isna(row.get("permitIssuedDate")) else row["permitIssuedDate"].date().isoformat(),
            "latest_permit_type": _clean_text(row.get("permitTypeDescr")),
            "latest_permit_value": None if pd.isna(row.get("permitValueNum")) else float(row.get("permitValueNum")),
        }
    return out


def _build_protest_summary(csv_path: Path | None) -> dict[str, dict[str, Any]]:
    if csv_path is None or not csv_path.exists():
        return {}

    cols = ["propID", "Year", "protStatusDescr", "finalMarketVal", "protActive"]
    df = pd.read_csv(csv_path, dtype=str, usecols=cols, encoding="utf-8", low_memory=False)
    df["propID"] = df["propID"].fillna("").astype(str).str.strip()
    df = df[df["propID"] != ""].copy()

    df["YearNum"] = pd.to_numeric(df["Year"], errors="coerce")
    df["finalMarketValNum"] = pd.to_numeric(
        df["finalMarketVal"].fillna("").astype(str).str.replace(",", "").str.replace("$", "", regex=False),
        errors="coerce",
    )
    df["protActiveNorm"] = df["protActive"].fillna("").astype(str).str.strip().str.lower()

    counts = df.groupby("propID").size().rename("protest_case_count")
    latest = df.sort_values(["propID", "YearNum"], ascending=[True, False]).groupby("propID").first()
    active = df.groupby("propID")["protActiveNorm"].apply(lambda s: "true" if (s == "true").any() else "false")

    out: dict[str, dict[str, Any]] = {}
    for prop_id, row in latest.iterrows():
        out[prop_id] = {
            "protest_case_count": int(counts.get(prop_id, 0)),
            "latest_protest_year": None if pd.isna(row.get("YearNum")) else int(row.get("YearNum")),
            "latest_protest_status": _clean_text(row.get("protStatusDescr")),
            "latest_protest_final_market": None if pd.isna(row.get("finalMarketValNum")) else float(row.get("finalMarketValNum")),
            "protest_active": active.get(prop_id, "false"),
        }
    return out


def _build_ag_summary(csv_path: Path | None) -> dict[str, dict[str, Any]]:
    if csv_path is None or not csv_path.exists():
        return {}

    cols = ["propID", "agType", "agAcres", "agValue", "marketValue"]
    df = pd.read_csv(csv_path, dtype=str, usecols=cols, encoding="utf-8", low_memory=False)
    df["propID"] = df["propID"].fillna("").astype(str).str.strip()
    df = df[df["propID"] != ""].copy()

    out: dict[str, dict[str, Any]] = {}
    for row in df.itertuples(index=False):
        prop_id = _clean_text(getattr(row, "propID", ""))
        if not prop_id:
            continue
        out[prop_id] = {
            "ag_type": _clean_text(getattr(row, "agType", "")),
            "ag_acres": _to_float(getattr(row, "agAcres", None)),
            "ag_value": _to_float(getattr(row, "agValue", None)),
            "ag_market_value": _to_float(getattr(row, "marketValue", None)),
        }
    return out


def _build_appraisal_summary(csv_path: Path | None) -> dict[str, dict[str, Any]]:
    if csv_path is None or not csv_path.exists():
        return {}

    cols = [
        "propID",
        "propYear",
        "entitySchoolCode",
        "landSizeSqft",
        "landSizeAcres",
        "imprvMainArea",
        "imprvYearBuilt",
        "landTypeCode",
        "propUseCode",
        "propSubType",
        "propType",
        "legalDescription",
        "legalAbsSubName",
        "nbhdCode",
    ]
    df = pd.read_csv(csv_path, dtype=str, usecols=[c for c in cols if c], encoding="utf-8", low_memory=False)
    if "propID" not in df.columns:
        return {}

    df["propID"] = df["propID"].fillna("").astype(str).str.strip()
    df = df[df["propID"] != ""].copy()

    if "propYear" in df.columns:
        df["propYearNum"] = pd.to_numeric(df["propYear"], errors="coerce")
        df = df.sort_values(["propID", "propYearNum"], ascending=[True, False])
    df = df.groupby("propID", as_index=False).first()

    out: dict[str, dict[str, Any]] = {}
    for row in df.itertuples(index=False):
        prop_id = _clean_text(getattr(row, "propID", ""))
        if not prop_id:
            continue
        out[prop_id] = {
            "school_code": _clean_text(getattr(row, "entitySchoolCode", "")),
            "land_sqft": _to_float(getattr(row, "landSizeSqft", None)),
            "land_acres": _to_float(getattr(row, "landSizeAcres", None)),
            "living_area": _to_float(getattr(row, "imprvMainArea", None)),
            "year_built": _to_int(getattr(row, "imprvYearBuilt", None)),
            "land_type_code": _clean_text(getattr(row, "landTypeCode", "")).upper(),
            "prop_use_code": _clean_text(getattr(row, "propUseCode", "")).upper(),
            "prop_sub_type": _clean_text(getattr(row, "propSubType", "")),
            "prop_type": _clean_text(getattr(row, "propType", "")),
            "legal_descr": _clean_text(getattr(row, "legalDescription", "")),
            "subdivision": _clean_text(getattr(row, "legalAbsSubName", "")),
            "nbhd_code": _clean_text(getattr(row, "nbhdCode", "")),
        }
    return out


def _project_point(transformer: Transformer | None, x: float, y: float) -> tuple[float, float]:
    if transformer is None:
        return x, y
    lng, lat = transformer.transform(x, y)
    return lng, lat


def _project_geometry(geom: dict[str, Any], transformer: Transformer | None) -> dict[str, Any]:
    gtype = geom.get("type")
    coords = geom.get("coordinates")

    if not gtype or coords is None:
        raise ValueError("Invalid geometry")

    def transform_ring(ring: list[list[float]]) -> list[list[float]]:
        out: list[list[float]] = []
        for x, y in ring:
            lng, lat = _project_point(transformer, x, y)
            out.append([float(lng), float(lat)])
        return out

    if gtype == "Polygon":
        return {"type": "Polygon", "coordinates": [transform_ring(r) for r in coords]}
    if gtype == "MultiPolygon":
        return {
            "type": "MultiPolygon",
            "coordinates": [[transform_ring(r) for r in poly] for poly in coords],
        }
    raise ValueError(f"Unsupported geometry type: {gtype}")


def _make_transformer(prj_path: Path) -> Transformer | None:
    if not prj_path.exists():
        raise FileNotFoundError(f"Missing PRJ file: {prj_path}")

    prj_text = prj_path.read_text(encoding="utf-8", errors="ignore").strip()
    print("CRS WKT:")
    print(prj_text)

    src = CRS.from_wkt(prj_text)
    dst = CRS.from_epsg(4326)

    if src.to_epsg() == 4326:
        return None
    return Transformer.from_crs(src, dst, always_xy=True)


@dataclass
class Stats:
    total_seen: int = 0
    kept: int = 0
    skipped_empty_geom: int = 0
    skipped_no_id: int = 0
    skipped_duplicate_batch_key: int = 0


def _dedupe_batch_on_parcel_key(batch: list[tuple]) -> tuple[list[tuple], int]:
    """Keep only the last row per parcel_key inside a single SQL VALUES batch."""
    if not batch:
        return batch, 0

    deduped: dict[str, tuple] = {}
    for row in batch:
        deduped[str(row[0])] = row
    dropped = len(batch) - len(deduped)
    return list(deduped.values()), dropped


def _shape_geojson(shape_obj: shapefile.Shape, transformer: Transformer | None) -> str | None:
    try:
        geom = shape_obj.__geo_interface__
    except Exception:
        return None

    if not isinstance(geom, dict) or not geom.get("type") or not geom.get("coordinates"):
        return None

    try:
        projected = _project_geometry(geom, transformer)
    except Exception:
        return None

    return json.dumps(projected)


def _derive_row(record_map: dict[str, Any], geometry_geojson: str, snapshot_date: date, lookups: dict[str, dict[str, Any]]) -> tuple:
    prop_id = _clean_text(record_map.get("PROP_ID"))
    geo_id = _clean_text(record_map.get("GEO_ID"))
    if prop_id and geo_id:
        parcel_key = f"{prop_id}:{geo_id}"
    elif geo_id:
        parcel_key = f"GEO:{geo_id}"
    elif prop_id:
        parcel_key = f"PROP:{prop_id}"
    else:
        parcel_key = f"ROW:{_clean_text(record_map.get('GlobalID'))}"

    owner_address = _first_non_empty(
        record_map.get("addr_line1"),
        record_map.get("addr_line2"),
        record_map.get("addr_line3"),
    )

    state_cd = _clean_text(record_map.get("state_cd")).upper()
    prop_use_code = _clean_text(record_map.get("property_u")).upper()
    land_type_code = _clean_text(record_map.get("land_type_")).upper()

    appraisal = lookups["appraisal"].get(prop_id, {})
    agent = lookups["agent"].get(prop_id, {})
    permits = lookups["permit"].get(prop_id, {})
    protests = lookups["protest"].get(prop_id, {})
    ag = lookups["ag"].get(prop_id, {})

    school_code = _first_non_empty(record_map.get("school"), appraisal.get("school_code"))
    legal_descr = _first_non_empty(record_map.get("legal_desc"), appraisal.get("legal_descr"))
    subdivision = _first_non_empty(record_map.get("abs_subd_1"), appraisal.get("subdivision"))

    # Column definitions mark land_sqft as "DO NOT USE"; prefer total land area.
    land_sqft = _to_float(record_map.get("land_total")) or _to_float(record_map.get("land_sqft"))
    if not land_sqft or land_sqft <= 0:
        app_land_sqft = _to_float(appraisal.get("land_sqft"))
        if app_land_sqft and app_land_sqft > 0:
            land_sqft = app_land_sqft

    land_acres = _to_float(record_map.get("legal_acre"))
    if not land_acres or land_acres <= 0:
        app_land_acres = _to_float(appraisal.get("land_acres"))
        if app_land_acres and app_land_acres > 0:
            land_acres = app_land_acres

    living_area = _to_float(record_map.get("living_are"))
    if not living_area or living_area <= 0:
        app_living_area = _to_float(appraisal.get("living_area"))
        if app_living_area and app_living_area > 0:
            living_area = app_living_area

    year_built = _to_int(record_map.get("yr_blt"))
    if not year_built:
        app_year_built = _to_int(appraisal.get("year_built"))
        if app_year_built:
            year_built = app_year_built

    if not land_type_code:
        land_type_code = _clean_text(appraisal.get("land_type_code")).upper()
    if not prop_use_code:
        prop_use_code = _clean_text(appraisal.get("prop_use_code")).upper()

    prop_type = _first_non_empty(record_map.get("prop_type_"), appraisal.get("prop_type"))
    prop_sub_type = _first_non_empty(appraisal.get("prop_sub_type"), record_map.get("property_u"))

    return (
        parcel_key,
        prop_id or None,
        geo_id or None,
        _clean_text(record_map.get("file_as_na")) or None,
        owner_address or None,
        _clean_text(record_map.get("addr_city")) or None,
        _clean_text(record_map.get("addr_state")) or None,
        _clean_text(record_map.get("addr_zip")) or None,
        _clean_text(record_map.get("situs_disp")) or None,
        _clean_text(record_map.get("situs_city")) or None,
        _clean_text(record_map.get("situs_zip")) or None,
        state_cd or None,
        lookups["state_category"].get(state_cd) or None,
        _clean_text(record_map.get("class_cd")) or None,
        subdivision or None,
        legal_descr or None,
        school_code or None,
        _clean_text(record_map.get("city")) or None,
        _clean_text(record_map.get("zoning")) or None,
        land_sqft,
        land_acres,
        living_area,
        year_built,
        _to_float(record_map.get("curr_land_")),
        _to_float(record_map.get("curr_imprv")),
        _to_float(record_map.get("curr_appra")),
        _to_float(record_map.get("cert_appra")),
        _to_float(record_map.get("curr_marke")),
        _to_float(record_map.get("curr_asses")),
        _to_float(record_map.get("curr_ag_us")),
        _to_float(record_map.get("curr_ag_ma")),
        _to_float(record_map.get("curr_ag_lo")),
        _clean_text(record_map.get("curr_val_y")) or None,
        _clean_text(record_map.get("cert_val_y")) or None,
        _clean_text(record_map.get("deed_num")) or None,
        _clean_text(record_map.get("deed_type_")) or None,
        _clean_text(record_map.get("deed_dt")) or None,
        land_type_code or None,
        lookups["land_type"].get(land_type_code) or None,
        prop_use_code or None,
        lookups["prop_use"].get(prop_use_code) or None,
        prop_type or None,
        prop_sub_type or None,
        _clean_text(record_map.get("commercial")) or None,
        _clean_text(record_map.get("pool")) or None,
        _to_float(record_map.get("beds")),
        _to_float(record_map.get("baths")),
        _to_float(record_map.get("stories")),
        _to_float(record_map.get("units")),
        _clean_text(record_map.get("exemptions")) or None,
        "true" if "HS" in _split_codes(record_map.get("exemptions")) else "false",
        _clean_text(record_map.get("all_entiti")) or None,
        _clean_text(record_map.get("tif")) or None,
        _clean_text(record_map.get("hood_cd")) or None,
        _clean_text(record_map.get("mapsco")) or None,
        _clean_text(record_map.get("property_s")) or None,
        _clean_text(record_map.get("prop_creat")) or None,
        _clean_text(record_map.get("prop_id_1")) or None,
        _clean_text(record_map.get("geo_id_1")) or None,
        _clean_text(record_map.get("protest_code")) or None,
        _clean_text(record_map.get("curr_ten_p")) or None,
        _clean_text(record_map.get("taxAgentID")) if "taxAgentID" in record_map else (agent.get("tax_agent_id") or None),
        agent.get("tax_agent_name") or None,
        agent.get("tax_agent_auth_protest") or None,
        agent.get("tax_agent_auth_resolve") or None,
        agent.get("tax_agent_mailings") or None,
        permits.get("permit_count"),
        permits.get("latest_permit_date"),
        permits.get("latest_permit_type"),
        permits.get("latest_permit_value"),
        protests.get("protest_case_count"),
        protests.get("latest_protest_year"),
        protests.get("latest_protest_status"),
        protests.get("latest_protest_final_market"),
        protests.get("protest_active"),
        ag.get("ag_type"),
        ag.get("ag_acres"),
        ag.get("ag_value"),
        ag.get("ag_market_value"),
        geometry_geojson,
        geometry_geojson,
        snapshot_date,
    )


def _ensure_table() -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS collin_parcels (
                    parcel_key VARCHAR(128) PRIMARY KEY,
                    account_num VARCHAR(32),
                    geo_id VARCHAR(64),
                    owner_name TEXT,
                    owner_address TEXT,
                    owner_city TEXT,
                    owner_state TEXT,
                    owner_zip TEXT,
                    property_address TEXT,
                    property_city TEXT,
                    property_zip TEXT,
                    state_cd VARCHAR(16),
                    state_cd_name TEXT,
                    class_cd VARCHAR(32),
                    subdivision TEXT,
                    legal_descr TEXT,
                    school_code TEXT,
                    city_code TEXT,
                    zoning TEXT,
                    land_sqft DOUBLE PRECISION,
                    land_acres DOUBLE PRECISION,
                    living_area DOUBLE PRECISION,
                    year_built INTEGER,
                    land_value DOUBLE PRECISION,
                    improvement_value DOUBLE PRECISION,
                    total_value DOUBLE PRECISION,
                    cert_total_value DOUBLE PRECISION,
                    curr_market_value DOUBLE PRECISION,
                    curr_assessed_value DOUBLE PRECISION,
                    curr_ag_use_value DOUBLE PRECISION,
                    curr_ag_market_value DOUBLE PRECISION,
                    curr_ag_loss_value DOUBLE PRECISION,
                    curr_val_year TEXT,
                    cert_val_year TEXT,
                    deed_num TEXT,
                    deed_type TEXT,
                    deed_date TEXT,
                    land_type_code TEXT,
                    land_type_name TEXT,
                    prop_use_code TEXT,
                    prop_use_name TEXT,
                    prop_type TEXT,
                    prop_sub_type TEXT,
                    commercial_flag TEXT,
                    pool_flag TEXT,
                    beds DOUBLE PRECISION,
                    baths DOUBLE PRECISION,
                    stories DOUBLE PRECISION,
                    units DOUBLE PRECISION,
                    exemptions TEXT,
                    exempt_homestead TEXT,
                    entity_codes TEXT,
                    tif_code TEXT,
                    neighborhood_code TEXT,
                    mapsco TEXT,
                    property_status TEXT,
                    property_create_date TEXT,
                    prop_ref_id1 TEXT,
                    prop_ref_id2 TEXT,
                    protest_code TEXT,
                    curr_ten_percent_loss TEXT,
                    tax_agent_id TEXT,
                    tax_agent_name TEXT,
                    tax_agent_auth_protest TEXT,
                    tax_agent_auth_resolve TEXT,
                    tax_agent_mailings TEXT,
                    permit_count INTEGER,
                    latest_permit_date TEXT,
                    latest_permit_type TEXT,
                    latest_permit_value DOUBLE PRECISION,
                    protest_case_count INTEGER,
                    latest_protest_year INTEGER,
                    latest_protest_status TEXT,
                    latest_protest_final_market DOUBLE PRECISION,
                    protest_active TEXT,
                    ag_type TEXT,
                    ag_acres DOUBLE PRECISION,
                    ag_value DOUBLE PRECISION,
                    ag_market_value DOUBLE PRECISION,
                    geom geometry(Geometry, 4326),
                    centroid geometry(Point, 4326),
                    source_snapshot DATE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_collin_parcels_account_num ON collin_parcels (account_num)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_collin_parcels_geo_id ON collin_parcels (geo_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_collin_parcels_geom ON collin_parcels USING GIST (geom)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_collin_parcels_centroid ON collin_parcels USING GIST (centroid)")
        conn.commit()
    finally:
        release_conn(conn)


def _upsert_batch(batch: list[tuple]) -> None:
    if not batch:
        return

    batch, _ = _dedupe_batch_on_parcel_key(batch)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO collin_parcels (
                    parcel_key, account_num, geo_id,
                    owner_name, owner_address, owner_city, owner_state, owner_zip,
                    property_address, property_city, property_zip,
                    state_cd, state_cd_name, class_cd,
                    subdivision, legal_descr, school_code, city_code, zoning,
                    land_sqft, land_acres, living_area, year_built,
                    land_value, improvement_value, total_value, cert_total_value,
                    curr_market_value, curr_assessed_value, curr_ag_use_value, curr_ag_market_value,
                    curr_ag_loss_value, curr_val_year, cert_val_year,
                    deed_num, deed_type, deed_date,
                    land_type_code, land_type_name,
                    prop_use_code, prop_use_name,
                    prop_type, prop_sub_type, commercial_flag, pool_flag,
                    beds, baths, stories, units,
                    exemptions, exempt_homestead, entity_codes, tif_code,
                    neighborhood_code, mapsco, property_status, property_create_date,
                    prop_ref_id1, prop_ref_id2, protest_code, curr_ten_percent_loss,
                    tax_agent_id, tax_agent_name, tax_agent_auth_protest, tax_agent_auth_resolve, tax_agent_mailings,
                    permit_count, latest_permit_date, latest_permit_type, latest_permit_value,
                    protest_case_count, latest_protest_year, latest_protest_status, latest_protest_final_market, protest_active,
                    ag_type, ag_acres, ag_value, ag_market_value,
                    geom, centroid, source_snapshot
                ) VALUES %s
                ON CONFLICT (parcel_key) DO UPDATE SET
                    account_num = EXCLUDED.account_num,
                    geo_id = EXCLUDED.geo_id,
                    owner_name = EXCLUDED.owner_name,
                    owner_address = EXCLUDED.owner_address,
                    owner_city = EXCLUDED.owner_city,
                    owner_state = EXCLUDED.owner_state,
                    owner_zip = EXCLUDED.owner_zip,
                    property_address = EXCLUDED.property_address,
                    property_city = EXCLUDED.property_city,
                    property_zip = EXCLUDED.property_zip,
                    state_cd = EXCLUDED.state_cd,
                    state_cd_name = EXCLUDED.state_cd_name,
                    class_cd = EXCLUDED.class_cd,
                    subdivision = EXCLUDED.subdivision,
                    legal_descr = EXCLUDED.legal_descr,
                    school_code = EXCLUDED.school_code,
                    city_code = EXCLUDED.city_code,
                    zoning = EXCLUDED.zoning,
                    land_sqft = EXCLUDED.land_sqft,
                    land_acres = EXCLUDED.land_acres,
                    living_area = EXCLUDED.living_area,
                    year_built = EXCLUDED.year_built,
                    land_value = EXCLUDED.land_value,
                    improvement_value = EXCLUDED.improvement_value,
                    total_value = EXCLUDED.total_value,
                    cert_total_value = EXCLUDED.cert_total_value,
                    curr_market_value = EXCLUDED.curr_market_value,
                    curr_assessed_value = EXCLUDED.curr_assessed_value,
                    curr_ag_use_value = EXCLUDED.curr_ag_use_value,
                    curr_ag_market_value = EXCLUDED.curr_ag_market_value,
                    curr_ag_loss_value = EXCLUDED.curr_ag_loss_value,
                    curr_val_year = EXCLUDED.curr_val_year,
                    cert_val_year = EXCLUDED.cert_val_year,
                    deed_num = EXCLUDED.deed_num,
                    deed_type = EXCLUDED.deed_type,
                    deed_date = EXCLUDED.deed_date,
                    land_type_code = EXCLUDED.land_type_code,
                    land_type_name = EXCLUDED.land_type_name,
                    prop_use_code = EXCLUDED.prop_use_code,
                    prop_use_name = EXCLUDED.prop_use_name,
                    prop_type = EXCLUDED.prop_type,
                    prop_sub_type = EXCLUDED.prop_sub_type,
                    commercial_flag = EXCLUDED.commercial_flag,
                    pool_flag = EXCLUDED.pool_flag,
                    beds = EXCLUDED.beds,
                    baths = EXCLUDED.baths,
                    stories = EXCLUDED.stories,
                    units = EXCLUDED.units,
                    exemptions = EXCLUDED.exemptions,
                    exempt_homestead = EXCLUDED.exempt_homestead,
                    entity_codes = EXCLUDED.entity_codes,
                    tif_code = EXCLUDED.tif_code,
                    neighborhood_code = EXCLUDED.neighborhood_code,
                    mapsco = EXCLUDED.mapsco,
                    property_status = EXCLUDED.property_status,
                    property_create_date = EXCLUDED.property_create_date,
                    prop_ref_id1 = EXCLUDED.prop_ref_id1,
                    prop_ref_id2 = EXCLUDED.prop_ref_id2,
                    protest_code = EXCLUDED.protest_code,
                    curr_ten_percent_loss = EXCLUDED.curr_ten_percent_loss,
                    tax_agent_id = EXCLUDED.tax_agent_id,
                    tax_agent_name = EXCLUDED.tax_agent_name,
                    tax_agent_auth_protest = EXCLUDED.tax_agent_auth_protest,
                    tax_agent_auth_resolve = EXCLUDED.tax_agent_auth_resolve,
                    tax_agent_mailings = EXCLUDED.tax_agent_mailings,
                    permit_count = EXCLUDED.permit_count,
                    latest_permit_date = EXCLUDED.latest_permit_date,
                    latest_permit_type = EXCLUDED.latest_permit_type,
                    latest_permit_value = EXCLUDED.latest_permit_value,
                    protest_case_count = EXCLUDED.protest_case_count,
                    latest_protest_year = EXCLUDED.latest_protest_year,
                    latest_protest_status = EXCLUDED.latest_protest_status,
                    latest_protest_final_market = EXCLUDED.latest_protest_final_market,
                    protest_active = EXCLUDED.protest_active,
                    ag_type = EXCLUDED.ag_type,
                    ag_acres = EXCLUDED.ag_acres,
                    ag_value = EXCLUDED.ag_value,
                    ag_market_value = EXCLUDED.ag_market_value,
                    geom = EXCLUDED.geom,
                    centroid = EXCLUDED.centroid,
                    source_snapshot = EXCLUDED.source_snapshot,
                    updated_at = NOW()
                """,
                batch,
                template=(
                    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                    "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                    "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                    "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                    "ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)),"
                    "ST_PointOnSurface(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326))),"
                    "%s)"
                ),
                page_size=500,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_conn(conn)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build collin_parcels table from Collin shapefile + optional enrichments")
    parser.add_argument("--source-shp", default=str(DEFAULT_SOURCE), help="Path to parcels_with_appraisal_data_R5.shp")
    parser.add_argument("--base-dir", default=str(DEFAULT_BASE_DIR), help="Base dir holding Collin CSVs and code files")
    parser.add_argument("--snapshot-date", default="2026-05-03", help="Snapshot date label (YYYY-MM-DD)")
    parser.add_argument("--limit", type=int, default=0, help="Optional max features to process")
    parser.add_argument("--write-db", action="store_true", help="Write to Postgres (default is dry-run)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source_shp = Path(args.source_shp)
    base_dir = Path(args.base_dir)

    if not source_shp.exists():
        raise FileNotFoundError(f"Source shapefile not found: {source_shp}")

    snapshot = date.fromisoformat(args.snapshot_date)
    prj_path = source_shp.with_suffix(".prj")
    transformer = _make_transformer(prj_path)

    print("Collin build: starting")
    print(f"Source: {source_shp}")
    print(f"Mode: {'WRITE_DB' if args.write_db else 'DRY_RUN'}")
    if args.limit > 0:
        print(f"Limit: {args.limit:,}")

    state_category = _build_code_lookup(_resolve_csv(base_dir, ["Collin_CAD_Code_File_-_State_Category_*.csv"]))
    prop_use = _build_code_lookup(_resolve_csv(base_dir, ["Collin_CAD_Code_File_-_Prop_Use_*.csv"]))
    land_type = _build_code_lookup(_resolve_csv(base_dir, ["Collin_CAD_Code_File_-_Land_Type_*.csv"]))

    appraisal = _build_appraisal_summary(
        _resolve_csv(
            base_dir,
            [
                "Collin_CAD_Appraisal_Data_-_Preliminary_*.csv",
                "Collin_CAD_Appraisal_Data_-_20*.csv",
            ],
        )
    )
    agent = _build_agent_summary(_resolve_csv(base_dir, ["Collin_CAD_Agent_Prop_List_*.csv"]))
    permit = _build_permit_summary(_resolve_csv(base_dir, ["Collin_CAD_Building_Permits_*.csv"]))
    protest = _build_protest_summary(_resolve_csv(base_dir, ["Collin_CAD_Protest_Data_*.csv"]))
    ag = _build_ag_summary(_resolve_csv(base_dir, ["Collin_CAD_Public_List_-_Ag_Property_*.csv"]))

    lookups = {
        "state_category": state_category,
        "prop_use": prop_use,
        "land_type": land_type,
        "appraisal": appraisal,
        "agent": agent,
        "permit": permit,
        "protest": protest,
        "ag": ag,
    }

    print("Lookup sizes:")
    print(f"  state_category: {len(state_category):,}")
    print(f"  prop_use:       {len(prop_use):,}")
    print(f"  land_type:      {len(land_type):,}")
    print(f"  appraisal:      {len(appraisal):,}")
    print(f"  agent:          {len(agent):,}")
    print(f"  permit:         {len(permit):,}")
    print(f"  protest:        {len(protest):,}")
    print(f"  ag:             {len(ag):,}")

    if args.write_db:
        _ensure_table()

    reader = shapefile.Reader(str(source_shp), encoding="utf-8")
    fields = [field[0] for field in reader.fields[1:]]
    total_records = len(reader)

    required = ["PROP_ID", "GEO_ID", "file_as_na", "situs_disp", "state_cd", "curr_appra"]
    missing = [name for name in required if name not in fields]
    if missing:
        raise ValueError(f"Missing expected fields: {', '.join(missing)}")

    stats = Stats()
    batch: list[tuple] = []

    for idx in range(total_records):
        if args.limit > 0 and stats.total_seen >= args.limit:
            break

        stats.total_seen += 1
        sr = reader.shapeRecord(idx)
        record_map = {fields[i]: sr.record[i] for i in range(len(fields))}

        prop_id = _clean_text(record_map.get("PROP_ID"))
        geo_id = _clean_text(record_map.get("GEO_ID"))
        if not prop_id and not geo_id:
            stats.skipped_no_id += 1
            continue

        geometry_geojson = _shape_geojson(sr.shape, transformer)
        if not geometry_geojson:
            stats.skipped_empty_geom += 1
            continue

        row = _derive_row(record_map, geometry_geojson, snapshot, lookups)
        stats.kept += 1

        if args.write_db:
            batch.append(row)
            if len(batch) >= BATCH_SIZE:
                deduped_batch, dropped = _dedupe_batch_on_parcel_key(batch)
                stats.skipped_duplicate_batch_key += dropped
                _upsert_batch(deduped_batch)
                print(f"Upserted {stats.kept:,} rows...")
                batch.clear()
        elif stats.kept % 50000 == 0:
            print(f"Validated {stats.kept:,} rows...")

    if args.write_db and batch:
        deduped_batch, dropped = _dedupe_batch_on_parcel_key(batch)
        stats.skipped_duplicate_batch_key += dropped
        _upsert_batch(deduped_batch)

    print("---")
    print("Collin build complete")
    print(f"Rows seen: {stats.total_seen:,}")
    print(f"Rows kept: {stats.kept:,}")
    print(f"Rows skipped (missing id): {stats.skipped_no_id:,}")
    print(f"Rows skipped (empty geometry): {stats.skipped_empty_geom:,}")
    print(f"Rows de-duplicated in batch: {stats.skipped_duplicate_batch_key:,}")


if __name__ == "__main__":
    main()
