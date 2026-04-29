# scripts/build_db.py
#
# One-time DCAD loader that reads local CSV/shapefile data and upserts
# normalized records into Supabase tables used by the app.
# Uses pure Python shapefile parsing with struct and the existing projection math.
#
# Connects to:
#   api/config.py  - imports shared database connection helpers
#   data/          - reads DCAD CSV files and PARCEL_GEOM shapefile inputs
#   PostgreSQL tables - writes parcels/appraisal/res_detail/land_detail/exempt_accounts

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pandas as pd

from api.config import get_conn, release_conn


BATCH_SIZE = 500
DIVISION_CODES = {"RES", "COM"}


ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
PARCEL_GEOM_DIR = DATA_DIR / "PARCEL_GEOM"


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    if pd.isna(value):
        return None
    text = str(value).strip()
    return text if text else None


def _to_float(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: object) -> int | None:
    if value is None or pd.isna(value):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _lcc_batch(xs: np.ndarray, ys: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """NAD83 State Plane Texas North Central (US Survey Feet) -> WGS84 lat/lng arrays."""
    a = 6378137.0
    e2 = 2 / 298.257222101 - (1 / 298.257222101) ** 2
    e = np.sqrt(e2)
    ft2m = 0.3048006096012192
    phi1 = np.radians(32 + 8 / 60)
    phi2 = np.radians(33 + 58 / 60)
    phi0 = np.radians(31 + 40 / 60)
    lam0 = np.radians(-98.5)
    fe = 1968500.0 * ft2m
    fn = 6561666.6666667 * ft2m
    xm = np.asarray(xs, dtype=float) * ft2m
    ym = np.asarray(ys, dtype=float) * ft2m

    def _m(p: np.ndarray) -> np.ndarray:
        return np.cos(p) / np.sqrt(1 - e2 * np.sin(p) ** 2)

    def _t(p: np.ndarray) -> np.ndarray:
        s = np.sin(p)
        return np.tan(np.pi / 4 - p / 2) * ((1 + e * s) / (1 - e * s)) ** (e / 2)

    m1, m2 = _m(phi1), _m(phi2)
    t0, t1, t2 = _t(phi0), _t(phi1), _t(phi2)
    n = (np.log(m1) - np.log(m2)) / (np.log(t1) - np.log(t2))
    f = m1 / (n * t1**n)
    r0 = a * f * t0**n
    dx = xm - fe
    dy = r0 - (ym - fn)
    r = np.sign(n) * np.sqrt(dx**2 + dy**2)
    ti = (r / (a * f)) ** (1 / n)
    lam = np.arctan2(dx, dy) / n + lam0
    phi = np.pi / 2 - 2 * np.arctan(ti)
    for _ in range(10):
        phi = np.pi / 2 - 2 * np.arctan(ti * ((1 - e * np.sin(phi)) / (1 + e * np.sin(phi))) ** (e / 2))
    return np.degrees(phi), np.degrees(lam)


def _read_parcel_dbf(dbf_path: Path) -> tuple[list[str] | None, int, int, int]:
    """Return list of Acct strings from PARCEL_GEOM.dbf."""
    with dbf_path.open("rb") as file:
        hdr = file.read(32)
        num_records = struct.unpack("<I", hdr[4:8])[0]
        header_size = struct.unpack("<H", hdr[8:10])[0]
        record_size = struct.unpack("<H", hdr[10:12])[0]
        fields: list[dict[str, int | str]] = []
        offset = 1
        while True:
            field_descriptor = file.read(32)
            if field_descriptor[0] == 0x0D or len(field_descriptor) < 32:
                break
            name = field_descriptor[:11].replace(b"\x00", b"").decode("ascii", errors="ignore")
            fields.append({"name": name, "len": field_descriptor[16], "off": offset})
            offset += field_descriptor[16]

        acct_field = next((field for field in fields if str(field["name"]).upper() == "ACCT"), None)
        if not acct_field:
            return None, num_records, header_size, record_size

        file.seek(header_size)
        accounts: list[str] = []
        for _ in range(num_records):
            record = file.read(record_size)
            accounts.append(
                record[acct_field["off"] : acct_field["off"] + acct_field["len"]]
                .decode("ascii", errors="ignore")
                .strip()
            )
    return accounts, num_records, header_size, record_size


def _load_parcel_geometry(target_keys: set[str]) -> dict[str, dict[str, object]]:
    """Load centroid per parcel key from PARCEL_GEOM shapefile (bounding box midpoint only)."""
    shp_path = PARCEL_GEOM_DIR / "PARCEL_GEOM.shp"
    dbf_path = PARCEL_GEOM_DIR / "PARCEL_GEOM.dbf"

    if not shp_path.exists() or not dbf_path.exists():
        raise FileNotFoundError("Missing PARCEL_GEOM shapefile files under data/PARCEL_GEOM")

    accts, num_records, _, _ = _read_parcel_dbf(dbf_path)
    if accts is None:
        raise RuntimeError("Could not read ACCT field from PARCEL_GEOM.dbf")

    index_to_acct = {index: acct for index, acct in enumerate(accts) if acct in target_keys}
    print(f"Target shapefile records: {len(index_to_acct):,} of {num_records:,}")

    centroid_x: list[float] = []
    centroid_y: list[float] = []
    centroid_accts: list[str] = []

    with shp_path.open("rb") as shp_file:
        shp_file.seek(100)
        for record_index in range(num_records):
            rec_hdr = shp_file.read(8)
            if len(rec_hdr) < 8:
                break

            content_len = struct.unpack(">I", rec_hdr[4:8])[0] * 2
            if record_index not in index_to_acct:
                shp_file.seek(content_len, 1)
                continue

            content = shp_file.read(content_len)
            if len(content) < 36:
                continue

            shape_type = struct.unpack("<i", content[:4])[0]
            if shape_type not in (5, 15, 25):
                continue

            xmin, ymin, xmax, ymax = struct.unpack("<4d", content[4:36])
            centroid_x.append((xmin + xmax) / 2)
            centroid_y.append((ymin + ymax) / 2)
            centroid_accts.append(index_to_acct[record_index])

            if (record_index + 1) % 50000 == 0:
                print(f"Read {record_index + 1:,} shapefile records...")

    if not centroid_accts:
        return {}

    centroid_lat, centroid_lng = _lcc_batch(np.array(centroid_x), np.array(centroid_y))

    geometry_map: dict[str, dict[str, object]] = {}
    for idx, acct in enumerate(centroid_accts):
        geometry_map[acct] = {
            "lat": float(centroid_lat[idx]),
            "lng": float(centroid_lng[idx]),
        }

    print(f"Loaded centroids for {len(geometry_map):,} parcels")
    return geometry_map


def _upsert_rows(table_name: str, rows: list[dict[str, object]], on_conflict_col: str, update_cols: list[str]) -> None:
    if not rows:
        print(f"{table_name}: 0 rows")
        return

    conn = get_conn()
    try:
        cols = list(rows[0].keys())
        placeholders = ", ".join(["%s"] * len(cols))
        col_names = ", ".join(cols)

        if update_cols:
            updates = ", ".join(f"{col} = EXCLUDED.{col}" for col in update_cols)
            sql = (
                f"INSERT INTO {table_name} ({col_names}) VALUES ({placeholders}) "
                f"ON CONFLICT ({on_conflict_col}) DO UPDATE SET {updates}"
            )
            batch = [tuple(row[col] for col in cols) for row in rows]
        else:
            sql = f"INSERT INTO exempt_accounts (account_num) VALUES (%s) ON CONFLICT DO NOTHING"
            batch = [(row["account_num"],) for row in rows]

        with conn.cursor() as cur:
            cur.executemany(sql, batch)
        conn.commit()
        print(f"{table_name}: {len(rows):,} rows upserted")
    except Exception:
        conn.rollback()
        raise
    finally:
        release_conn(conn)


def _count_rows(table_name: str) -> int:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {table_name}")
            return cur.fetchone()[0]
    finally:
        release_conn(conn)


def _build_parcels_table() -> list[dict[str, object]]:
    account_info_path = DATA_DIR / "ACCOUNT_INFO.CSV"
    account_df = pd.read_csv(account_info_path, dtype=str, encoding="latin-1")
    account_df = account_df[account_df["DIVISION_CD"].isin(DIVISION_CODES)].copy()
    account_df = account_df.fillna("")

    account_nums = account_df["ACCOUNT_NUM"].astype(str).str.strip()
    gis_ids = account_df["GIS_PARCEL_ID"].astype(str).str.strip() if "GIS_PARCEL_ID" in account_df.columns else pd.Series([], dtype=str)
    target_keys = set(account_nums[account_nums != ""]) | set(gis_ids[gis_ids != ""])

    print(f"ACCOUNT_INFO rows (RES/COM): {len(account_df):,}")
    geometry_map = _load_parcel_geometry(target_keys)

    rows: list[dict[str, object]] = []
    missing_geometry = 0

    for row in account_df.itertuples(index=False):
        account_num = _clean_text(getattr(row, "ACCOUNT_NUM", None))
        if not account_num:
            continue

        street_num = _clean_text(getattr(row, "STREET_NUM", None)) or ""
        full_street = _clean_text(getattr(row, "FULL_STREET_NAME", None)) or ""
        property_address = f"{street_num} {full_street}".strip().upper() or None

        gis_parcel_id = _clean_text(getattr(row, "GIS_PARCEL_ID", None))

        if account_num in geometry_map:
            parcel_key = account_num
        elif gis_parcel_id and gis_parcel_id in geometry_map:
            parcel_key = gis_parcel_id
        else:
            parcel_key = gis_parcel_id or account_num

        geom = geometry_map.get(parcel_key)
        centroid = None
        if geom:
            centroid = f"SRID=4326;POINT({geom['lng']} {geom['lat']})"
        else:
            missing_geometry += 1

        rows.append(
            {
                "account_num": account_num,
                "parcel_key": parcel_key,
                "gis_parcel_id": gis_parcel_id,
                "owner_name": _clean_text(getattr(row, "OWNER_NAME1", None)),
                "owner_address": _clean_text(getattr(row, "OWNER_ADDRESS_LINE1", None)),
                "owner_city": _clean_text(getattr(row, "OWNER_CITY", None)),
                "owner_state": _clean_text(getattr(row, "OWNER_STATE", None)),
                "owner_zip": _clean_text(getattr(row, "OWNER_ZIPCODE", None)),
                "street_num": _clean_text(getattr(row, "STREET_NUM", None)),
                "full_street_name": _clean_text(getattr(row, "FULL_STREET_NAME", None)),
                "property_address": property_address,
                "property_zip": _clean_text(getattr(row, "PROPERTY_ZIPCODE", None)),
                "division_cd": _clean_text(getattr(row, "DIVISION_CD", None)),
                "sptd_code": _clean_text(getattr(row, "SPTD_CODE", None)),
                "nbhd_cd": _clean_text(getattr(row, "NBHD_CD", None)),
                "legal1": _clean_text(getattr(row, "LEGAL1", None)),
                "legal2": _clean_text(getattr(row, "LEGAL2", None)),
                "legal3": _clean_text(getattr(row, "LEGAL3", None)),
                "legal4": _clean_text(getattr(row, "LEGAL4", None)),
                "legal5": _clean_text(getattr(row, "LEGAL5", None)),
                "centroid": centroid,
            }
        )

    print(f"Parcels prepared: {len(rows):,}; missing geometry: {missing_geometry:,}")
    return rows


def _build_appraisal_table() -> list[dict[str, object]]:
    appraisal_path = DATA_DIR / "ACCOUNT_APPRL_YEAR.CSV"
    appraisal_df = pd.read_csv(appraisal_path, dtype=str, encoding="latin-1").fillna("")

    rows: list[dict[str, object]] = []
    for row in appraisal_df.itertuples(index=False):
        account_num = _clean_text(getattr(row, "ACCOUNT_NUM", None))
        if not account_num:
            continue
        rows.append(
            {
                "account_num": account_num,
                "land_val": _to_float(getattr(row, "LAND_VAL", None)),
                "impr_val": _to_float(getattr(row, "IMPR_VAL", None)),
                "tot_val": _to_float(getattr(row, "TOT_VAL", None)),
                "isd_desc": _clean_text(getattr(row, "ISD_JURIS_DESC", None)),
                "sptd_code": _clean_text(getattr(row, "SPTD_CODE", None)),
            }
        )

    print(f"Appraisal prepared: {len(rows):,}")
    return rows


def _build_res_detail_table() -> list[dict[str, object]]:
    res_detail_path = DATA_DIR / "RES_DETAIL.CSV"
    res_df = pd.read_csv(res_detail_path, dtype=str, encoding="latin-1").fillna("")
    res_df = res_df.groupby("ACCOUNT_NUM", as_index=False).first()

    rows: list[dict[str, object]] = []
    for row in res_df.itertuples(index=False):
        account_num = _clean_text(getattr(row, "ACCOUNT_NUM", None))
        if not account_num:
            continue
        rows.append(
            {
                "account_num": account_num,
                "yr_built": _to_int(getattr(row, "YR_BUILT", None)),
                "tot_living_area": _to_float(getattr(row, "TOT_LIVING_AREA_SF", None)),
                "tot_main_sf": _to_float(getattr(row, "TOT_MAIN_SF", None)),
            }
        )

    print(f"Residential detail prepared: {len(rows):,}")
    return rows


def _build_land_detail_table() -> list[dict[str, object]]:
    land_path = DATA_DIR / "LAND.CSV"
    land_df = pd.read_csv(land_path, dtype=str, encoding="latin-1").fillna("")
    land_df = land_df.groupby("ACCOUNT_NUM", as_index=False).first()

    rows: list[dict[str, object]] = []
    for row in land_df.itertuples(index=False):
        account_num = _clean_text(getattr(row, "ACCOUNT_NUM", None))
        if not account_num:
            continue

        area_size = _to_float(getattr(row, "AREA_SIZE", None))
        area_uom = _clean_text(getattr(row, "AREA_UOM_DESC", None))
        if area_size is not None and area_uom and area_uom.upper() == "ACRE":
            area_size = area_size * 43560

        rows.append(
            {
                "account_num": account_num,
                "zoning": _clean_text(getattr(row, "ZONING", None)),
                "front_dim": _to_float(getattr(row, "FRONT_DIM", None)),
                "depth_dim": _to_float(getattr(row, "DEPTH_DIM", None)),
                "area_size": area_size,
                "area_uom": area_uom,
            }
        )

    print(f"Land detail prepared: {len(rows):,}")
    return rows


def _build_exempt_accounts_table() -> list[dict[str, object]]:
    exempt_path = DATA_DIR / "ACCT_EXEMPT_VALUE.CSV"
    exempt_df = pd.read_csv(exempt_path, dtype=str, encoding="latin-1").fillna("")
    exempt_df = exempt_df[exempt_df["EXEMPTION_CD"].astype(str).str.strip() == "14"].copy()

    rows: list[dict[str, object]] = []
    for account_num in exempt_df["ACCOUNT_NUM"].astype(str).str.strip().unique().tolist():
        if account_num:
            rows.append({"account_num": account_num})

    print(f"Exempt accounts prepared: {len(rows):,}")
    return rows


def _validate_inputs() -> None:
    required_files = [
        DATA_DIR / "ACCOUNT_INFO.CSV",
        DATA_DIR / "ACCOUNT_APPRL_YEAR.CSV",
        DATA_DIR / "RES_DETAIL.CSV",
        DATA_DIR / "LAND.CSV",
        DATA_DIR / "ACCT_EXEMPT_VALUE.CSV",
        PARCEL_GEOM_DIR / "PARCEL_GEOM.shp",
        PARCEL_GEOM_DIR / "PARCEL_GEOM.dbf",
    ]

    missing = [str(path.relative_to(ROOT_DIR)) for path in required_files if not path.exists()]
    if missing:
        raise FileNotFoundError("Missing required Phase 3 inputs:\n - " + "\n - ".join(missing))


def main() -> None:
    print("Phase 3 build: loading DCAD data into PostgreSQL")
    _validate_inputs()

    parcels_rows = _build_parcels_table()
    appraisal_rows = _build_appraisal_table()
    res_detail_rows = _build_res_detail_table()
    land_detail_rows = _build_land_detail_table()
    exempt_rows = _build_exempt_accounts_table()

    _upsert_rows("parcels", parcels_rows, "account_num", [col for col in parcels_rows[0] if col != "account_num"])
    _upsert_rows("appraisal", appraisal_rows, "account_num", ["land_val", "impr_val", "tot_val", "isd_desc", "sptd_code"])
    _upsert_rows("res_detail", res_detail_rows, "account_num", ["yr_built", "tot_living_area", "tot_main_sf"])
    _upsert_rows("land_detail", land_detail_rows, "account_num", ["zoning", "front_dim", "depth_dim", "area_size", "area_uom"])
    _upsert_rows("exempt_accounts", exempt_rows, "account_num", [])

    print("\nFinal row counts:")
    for table_name in ["parcels", "appraisal", "res_detail", "land_detail", "exempt_accounts"]:
        print(f"  {table_name}: {_count_rows(table_name):,}")


if __name__ == "__main__":
    main()
