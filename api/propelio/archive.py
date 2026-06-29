# api/propelio/archive.py
#
# Session-DB archive for workspace-scoped Propelio comps.
# Stores append-only comp history per saved area and preserves user ratings.
#
# Connects to:
#   api/config.py          - session DB connection helpers
#   api/redfin.py          - normalize_addr_key key canonicalization
#   api/propelio/routes.py - merge + load helpers for by-polygon/refresh endpoints

from __future__ import annotations

from typing import Any, Literal

from psycopg2.extras import Json

from api.config import get_session_conn, release_session_conn
from api.geo import haversine_miles, polygon_centroid
from api.redfin import normalize_addr_key


def _comp_address_key(comp: dict[str, Any]) -> str:
    address = str(comp.get("address") or "").strip()
    street_only = address.split(",", 1)[0].strip() if address else ""
    key = normalize_addr_key(street_only or address).strip().upper()
    if key:
        return key

    extra = comp.get("extra") if isinstance(comp.get("extra"), dict) else {}
    mls = str(extra.get("mls") or "").strip().upper()
    if mls:
        return f"MLS:{mls}"

    lat_raw = extra.get("lat")
    lng_raw = extra.get("lon", extra.get("lng"))
    try:
        lat = float(lat_raw)
        lng = float(lng_raw)
    except (TypeError, ValueError):
        return ""
    return f"LL:{lat:.6f},{lng:.6f}"


def ensure_tables() -> None:
    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS propelio_comp_archive (
                    id                 SERIAL PRIMARY KEY,
                    saved_area_id      TEXT NOT NULL REFERENCES saved_areas(area_id) ON DELETE CASCADE,
                    comp_address_key   TEXT NOT NULL,
                    comp_mls           TEXT,
                    comp_data          JSONB NOT NULL,
                    parcel_geom        JSONB,
                    parcel_account_num TEXT,
                    status             TEXT,
                    last_status        TEXT,
                    last_price         NUMERIC,
                    user_rating        TEXT,
                    rating_at          TIMESTAMPTZ,
                    first_seen_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_seen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (saved_area_id, comp_address_key)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS propelio_comp_archive_area
                    ON propelio_comp_archive (saved_area_id)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS propelio_comp_archive_rating
                    ON propelio_comp_archive (saved_area_id, user_rating)
                    WHERE user_rating IS NOT NULL
                """
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


def merge_comps_into_archive(saved_area_id: str, comps: list[dict[str, Any]]) -> dict[str, int]:
    normalized_area_id = str(saved_area_id or "").strip()
    if not normalized_area_id:
        raise ValueError("saved_area_id is required")

    inserted = 0
    updated = 0

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            for comp in comps or []:
                if not isinstance(comp, dict):
                    continue

                comp_key = _comp_address_key(comp)
                if not comp_key:
                    continue

                extra = comp.get("extra") if isinstance(comp.get("extra"), dict) else {}
                comp_mls = str(extra.get("mls") or "").strip() or None
                parcel_geom = comp.get("parcel_geom")
                parcel_account_num = str(comp.get("parcel_account_num") or "").strip() or None
                status = str(comp.get("status") or "").strip().lower() or None

                price_raw = comp.get("price")
                try:
                    last_price = float(price_raw) if price_raw is not None else None
                except (TypeError, ValueError):
                    last_price = None

                # Stamp the canonical address-key onto the comp dict before
                # we serialize so the saved JSONB blob carries it. This lets
                # the frontend round-trip the same key when POSTing ratings.
                comp["comp_address_key"] = comp_key

                cur.execute(
                    """
                    INSERT INTO propelio_comp_archive (
                        saved_area_id,
                        comp_address_key,
                        comp_mls,
                        comp_data,
                        parcel_geom,
                        parcel_account_num,
                        status,
                        last_status,
                        last_price
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (saved_area_id, comp_address_key)
                    DO UPDATE SET
                        comp_mls = EXCLUDED.comp_mls,
                        comp_data = EXCLUDED.comp_data,
                        parcel_geom = EXCLUDED.parcel_geom,
                        parcel_account_num = EXCLUDED.parcel_account_num,
                        status = EXCLUDED.status,
                        last_status = EXCLUDED.last_status,
                        last_price = EXCLUDED.last_price,
                        last_seen_at = NOW()
                    RETURNING (xmax = 0) AS inserted
                    """,
                    (
                        normalized_area_id,
                        comp_key,
                        comp_mls,
                        Json(comp),
                        Json(parcel_geom) if isinstance(parcel_geom, (dict, list)) else None,
                        parcel_account_num,
                        status,
                        status,
                        last_price,
                    ),
                )
                row = cur.fetchone()
                if row and bool(row[0]):
                    inserted += 1
                else:
                    updated += 1

            cur.execute(
                """
                SELECT COUNT(*)
                FROM propelio_comp_archive
                WHERE saved_area_id = %s
                """,
                (normalized_area_id,),
            )
            total = int(cur.fetchone()[0] or 0)

        conn.commit()
        return {"inserted": inserted, "updated": updated, "total": total}
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


def set_comp_rating(
    saved_area_id: str,
    comp_address_key: str,
    rating: str | None,
    rated_by_user_id: int | None = None,
    view: Literal["arv", "nbv", "export"] | None = None,
) -> int:
    """Set or clear a workspace-scoped rating for a comp.

    Writes to comp_ratings (Phase 2 canonical store) keyed on
    (workspace_id, comp_id). Looks up comp_id via comp_address_key on
    propelio_comps. Returns rows-affected count for endpoint 404 logic
    (0 = comp not found in global cache).

    view routing (per-view ratings spec §3.1, FMEA C2):
    - view='arv' (default for absent/None — coexistence with old clients)
      → the EXISTING comp_ratings table, byte-for-byte unchanged.
    - view in ('nbv','export') → the additive comp_ratings_views table,
      UPSERT ON CONFLICT (workspace_id, comp_id, view); clear = DELETE
      that view's row only.
    view='arv' is NEVER written to the _views table (CHECK excludes it).
    """
    # Intentionally no longer writes propelio_comp_archive.user_rating;
    # readers should query comp_ratings for canonical rating state.
    area_id = str(saved_area_id or "").strip()
    if not area_id:
        raise ValueError("saved_area_id is required")
    addr_key = str(comp_address_key or "").strip()
    if not addr_key:
        raise ValueError("comp_address_key is required")

    # View normalization: absent/None → 'arv' (old clients coexist; the
    # flag-ON frontend always sends an explicit view, so a missing view is
    # always a flag-off old client). Present-but-invalid → ValueError → 400
    # (NEVER silently default an invalid present view to ARV). FMEA C2.
    norm_view: Literal["arv", "nbv", "export"]
    if view is None:
        norm_view = "arv"
    else:
        candidate_view = str(view).strip().lower()
        if candidate_view not in ("arv", "nbv", "export"):
            raise ValueError(f"view must be 'arv', 'nbv', 'export', or null; got {view!r}")
        norm_view = candidate_view  # type: ignore[assignment]

    norm_rating: str | None
    if rating is None:
        norm_rating = None
    elif isinstance(rating, str):
        candidate = rating.strip().lower()
        if candidate == "":
            norm_rating = None
        elif candidate not in {"good", "bad"}:
            raise ValueError(f"rating must be 'good', 'bad', or null; got {rating!r}")
        else:
            norm_rating = candidate
    else:
        raise ValueError(f"rating must be a string or null; got type {type(rating).__name__}")

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT comp_id FROM propelio_comps WHERE comp_address_key = %s LIMIT 1",
                (addr_key,),
            )
            row = cur.fetchone()
            if row is None:
                return 0
            comp_id = int(row[0])

            if norm_view == "arv":
                # ARV path — EXISTING comp_ratings table, byte-for-byte
                # unchanged. This is the load-bearing coexistence invariant.
                if norm_rating is None:
                    cur.execute(
                        """
                        DELETE FROM comp_ratings
                        WHERE workspace_id = %s AND comp_id = %s
                        """,
                        (area_id, comp_id),
                    )
                    affected = cur.rowcount
                else:
                    cur.execute(
                        """
                        INSERT INTO comp_ratings (workspace_id, comp_id, rating, rated_by_user_id, rated_at)
                        VALUES (%s, %s, %s, %s, NOW())
                        ON CONFLICT (workspace_id, comp_id) DO UPDATE
                            SET rating = EXCLUDED.rating,
                                rated_by_user_id = EXCLUDED.rated_by_user_id,
                                rated_at = NOW()
                        """,
                        (area_id, comp_id, norm_rating, rated_by_user_id),
                    )
                    affected = cur.rowcount
            else:
                # NBV/Export path — additive comp_ratings_views table. view
                # CHECK excludes 'arv' (structurally impossible to shadow ARV).
                if norm_rating is None:
                    cur.execute(
                        """
                        DELETE FROM comp_ratings_views
                        WHERE workspace_id = %s AND comp_id = %s AND view = %s
                        """,
                        (area_id, comp_id, norm_view),
                    )
                    affected = cur.rowcount
                else:
                    cur.execute(
                        """
                        INSERT INTO comp_ratings_views (workspace_id, comp_id, view, rating, rated_by_user_id, rated_at)
                        VALUES (%s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (workspace_id, comp_id, view) DO UPDATE
                            SET rating = EXCLUDED.rating,
                                rated_by_user_id = EXCLUDED.rated_by_user_id,
                                rated_at = NOW()
                        """,
                        (area_id, comp_id, norm_view, norm_rating, rated_by_user_id),
                    )
                    affected = cur.rowcount
        conn.commit()
        if norm_rating is None and affected == 0:
            return 1
        return int(affected or 0)
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)


def load_archived_comps(saved_area_id: str) -> list[dict[str, Any]]:
    """Load archived comps for a saved area, hydrating ratings from the
    AUTHORITATIVE store (comp_ratings for ARV + comp_ratings_views for
    nbv/export) — NOT the legacy propelio_comp_archive.user_rating column.

    Per-view ratings spec §3.3 (gap-hunt critical catch): this function feeds
    5 endpoints (by-polygon cache-hit fallback, refresh, deep-pull,
    attach-to-area, archived-comps). Previously it sourced user_rating from
    propelio_comp_archive.user_rating, which the rate endpoint stopped writing
    (set_comp_rating writes only comp_ratings) → latent ARV desync where a
    rating saved via /comp/rate was invisible on this fallback path. This
    reconciliation reads comp_ratings (ARV) + comp_ratings_views (nbv/export)
    in the same Alt-D shape as load_comps_by_polygon (§3.2): user_rating from
    the ARV table (unchanged semantics), ratings_by_view aggregated with
    COALESCE('{}') + workspace_id-scoped JOIN (C4) + GROUP BY (H5).

    The comp DATA still comes from propelio_comp_archive (it is the comp-data
    store); only the rating source changes. comp_address_key is needed to
    bridge archive → propelio_comps → comp_ratings (archive has comp_address_key,
    comp_ratings needs comp_id).
    """
    normalized_area_id = str(saved_area_id or "").strip()
    if not normalized_area_id:
        return []

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    pa.comp_data,
                    pa.parcel_geom,
                    pa.parcel_account_num,
                    pa.status,
                    pa.comp_address_key,
                    cr.rating AS user_rating,
                    COALESCE(
                        json_object_agg(crv.view, crv.rating)
                            FILTER (WHERE crv.view IN ('nbv','export')),
                        '{}'
                    ) AS ratings_by_view
                FROM propelio_comp_archive pa
                LEFT JOIN propelio_comps pc
                    ON pc.comp_address_key = pa.comp_address_key
                LEFT JOIN comp_ratings cr
                    ON cr.comp_id = pc.comp_id
                    AND cr.workspace_id = pa.saved_area_id
                LEFT JOIN comp_ratings_views crv
                    ON crv.comp_id = pc.comp_id
                    AND crv.workspace_id = pa.saved_area_id
                WHERE pa.saved_area_id = %s
                GROUP BY
                    pa.comp_data,
                    pa.parcel_geom,
                    pa.parcel_account_num,
                    pa.status,
                    pa.comp_address_key,
                    cr.rating
                ORDER BY pa.last_seen_at DESC, pa.id DESC
                """,
                (normalized_area_id,),
            )
            rows = cur.fetchall() or []

        hydrated: list[dict[str, Any]] = []
        for comp_data, parcel_geom, parcel_account_num, status, comp_address_key, user_rating, ratings_by_view in rows:
            comp = dict(comp_data) if isinstance(comp_data, dict) else {}
            comp["parcel_geom"] = parcel_geom if isinstance(parcel_geom, (dict, list)) else None
            comp["parcel_account_num"] = str(parcel_account_num or "").strip() or None
            # comp_address_key is now explicitly set (parity with
            # load_comps_by_polygon) so the popup rating buttons work on this
            # fallback path too. The archive row always has it (NOT NULL).
            if comp_address_key:
                comp["comp_address_key"] = str(comp_address_key)
            if status and not comp.get("status"):
                comp["status"] = status
            comp["user_rating"] = str(user_rating).strip().lower() if user_rating is not None else None
            # ratings_by_view: {nbv?, export?} — COALESCE guarantees '{}' (H5).
            if isinstance(ratings_by_view, dict):
                comp["ratings_by_view"] = ratings_by_view
            elif isinstance(ratings_by_view, str):
                try:
                    import json as _json3
                    parsed = _json3.loads(ratings_by_view)
                    comp["ratings_by_view"] = parsed if isinstance(parsed, dict) else {}
                except Exception:
                    comp["ratings_by_view"] = {}
            else:
                comp["ratings_by_view"] = {}
            hydrated.append(comp)

        return hydrated
    finally:
        release_session_conn(conn)


def merge_comps_into_global(comps: list[dict[str, Any]], source: str) -> dict[str, int]:
    """Upsert a list of asdict(Property) comps into the global propelio_comps table.

    Uses the same idempotent ON CONFLICT DO UPDATE pattern as the backfill
    script.  Returns {"inserted": N, "updated": M}.  Caller is responsible
    for wrapping this in try/except so failures are non-fatal.
    """
    from datetime import datetime, timezone

    from psycopg2.extras import Json as _Json

    inserted = 0
    updated = 0

    if not comps:
        return {"inserted": 0, "updated": 0}

    now_iso = datetime.now(timezone.utc).isoformat()

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            for comp in comps:
                if not isinstance(comp, dict):
                    continue

                comp_key = _comp_address_key(comp)
                if not comp_key:
                    continue

                address = str(comp.get("address") or "").strip()
                if not address:
                    continue

                extra = comp.get("extra") if isinstance(comp.get("extra"), dict) else {}
                raw = extra.get("raw") if isinstance(extra.get("raw"), dict) else {}

                def _flt(v: Any) -> float | None:
                    if v in (None, ""):
                        return None
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        return None

                def _int(v: Any) -> int | None:
                    if v in (None, ""):
                        return None
                    try:
                        return int(float(v))
                    except (TypeError, ValueError):
                        return None

                def _iso(v: Any) -> str | None:
                    if v is None:
                        return None
                    text = str(v).strip()[:10]
                    try:
                        from datetime import datetime as _dt
                        return _dt.strptime(text, "%Y-%m-%d").date().isoformat()
                    except ValueError:
                        return None

                lat = _flt(extra.get("lat"))
                lng = _flt(extra.get("lon") or extra.get("lng"))

                fields = {
                    "comp_address_key": comp_key,
                    "address": address,
                    "neighborhood": str(comp.get("neighborhood") or "").strip() or None,
                    "lat": lat,
                    "lng": lng,
                    "status": str(comp.get("status") or "").strip().lower() or None,
                    "price": _flt(comp.get("price")),
                    "sold_date": _iso(extra.get("close_date")),
                    "close_date": _iso(extra.get("close_date")),
                    "dom": _int(extra.get("dom")),
                    "beds": _flt(extra.get("beds")),
                    "baths": _flt(extra.get("baths")),
                    "baths_full": _int(extra.get("baths_full")),
                    "baths_half": _int(extra.get("baths_half")),
                    "garage": _int(extra.get("garage")),
                    "sqft": _flt(comp.get("sqft")),
                    "lot_size": _flt(comp.get("lot_size")),
                    "year_built": _int(comp.get("year_built")),
                    "mls": str(extra.get("mls") or "").strip() or None,
                    "property_type": str(extra.get("property_type") or "").strip() or None,
                    "property_category": str(extra.get("property_category") or "").strip() or None,
                    "list_price": _flt(extra.get("list_price")),
                    "remarks": str(extra.get("remarks") or raw.get("remarks") or "").strip() or None,
                    "listing_agent_name": str(raw.get("listing_agent_name") or "").strip() or None,
                    "listing_agent_phone": str(raw.get("listing_agent_phone") or "").strip() or None,
                    "listing_agent_email": str(raw.get("listing_agent_email") or "").strip() or None,
                    "listing_office_name": str(raw.get("listing_office_name") or "").strip() or None,
                    "listing_office_phone": str(raw.get("listing_office_phone") or "").strip() or None,
                    "buyer_agent_name": str(raw.get("buyer_agent_name") or "").strip() or None,
                    "buyer_agent_phone": str(raw.get("buyer_agent_phone") or "").strip() or None,
                    "buyer_agent_email": str(raw.get("buyer_agent_email") or "").strip() or None,
                    "buyer_office_name": str(raw.get("buyer_office_name") or "").strip() or None,
                    "buyer_office_phone": str(raw.get("buyer_office_phone") or "").strip() or None,
                    "photo_count": _int(raw.get("photo_count")),
                    "photos": raw.get("photos") if isinstance(raw.get("photos"), list) else None,
                    "parcel_account_num": str(comp.get("parcel_account_num") or "").strip() or None,
                    "parcel_county": str(comp.get("parcel_county") or "").strip() or None,
                    "parcel_geom": comp.get("parcel_geom") if isinstance(comp.get("parcel_geom"), (dict, list)) else None,
                    "parsed_payload": dict(comp),
                    "raw_payload": raw if raw else None,
                    "first_seen_source": source,
                    "first_seen_at": now_iso,
                    "last_seen_at": now_iso,
                }

                cur.execute(
                    """
                    INSERT INTO propelio_comps (
                        comp_address_key, address, neighborhood, lat, lng, geom,
                        status, last_status, price, last_price,
                        sold_date, close_date, dom,
                        beds, baths, baths_full, baths_half, garage,
                        sqft, lot_size, year_built, mls,
                        property_type, property_category, list_price, remarks,
                        listing_agent_name, listing_agent_phone, listing_agent_email,
                        listing_office_name, listing_office_phone,
                        buyer_agent_name, buyer_agent_phone, buyer_agent_email,
                        buyer_office_name, buyer_office_phone,
                        photo_count, photos,
                        parcel_account_num, parcel_county, parcel_geom,
                        parsed_payload, raw_payload,
                        first_seen_source, first_seen_at, last_seen_at
                    )
                    VALUES (
                        %(comp_address_key)s, %(address)s, %(neighborhood)s,
                        %(lat)s, %(lng)s,
                        CASE
                            WHEN %(lat)s IS NOT NULL AND %(lng)s IS NOT NULL
                            THEN ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)
                            ELSE NULL
                        END,
                        %(status)s, %(status)s, %(price)s, %(price)s,
                        %(sold_date)s, %(close_date)s, %(dom)s,
                        %(beds)s, %(baths)s, %(baths_full)s, %(baths_half)s, %(garage)s,
                        %(sqft)s, %(lot_size)s, %(year_built)s, %(mls)s,
                        %(property_type)s, %(property_category)s,
                        %(list_price)s, %(remarks)s,
                        %(listing_agent_name)s, %(listing_agent_phone)s, %(listing_agent_email)s,
                        %(listing_office_name)s, %(listing_office_phone)s,
                        %(buyer_agent_name)s, %(buyer_agent_phone)s, %(buyer_agent_email)s,
                        %(buyer_office_name)s, %(buyer_office_phone)s,
                        %(photo_count)s, %(photos)s,
                        %(parcel_account_num)s, %(parcel_county)s, %(parcel_geom)s,
                        %(parsed_payload)s, %(raw_payload)s,
                        %(first_seen_source)s, %(first_seen_at)s, %(last_seen_at)s
                    )
                    ON CONFLICT (comp_address_key) DO UPDATE SET
                        address             = EXCLUDED.address,
                        neighborhood        = EXCLUDED.neighborhood,
                        lat                 = EXCLUDED.lat,
                        lng                 = EXCLUDED.lng,
                        geom                = CASE
                            WHEN EXCLUDED.lat IS NOT NULL AND EXCLUDED.lng IS NOT NULL
                            THEN ST_SetSRID(ST_MakePoint(EXCLUDED.lng, EXCLUDED.lat), 4326)
                            ELSE propelio_comps.geom
                        END,
                        last_status         = propelio_comps.status,
                        status              = EXCLUDED.status,
                        last_price          = propelio_comps.price,
                        price               = EXCLUDED.price,
                        sold_date           = EXCLUDED.sold_date,
                        close_date          = EXCLUDED.close_date,
                        dom                 = EXCLUDED.dom,
                        beds                = EXCLUDED.beds,
                        baths               = EXCLUDED.baths,
                        baths_full          = EXCLUDED.baths_full,
                        baths_half          = EXCLUDED.baths_half,
                        garage              = EXCLUDED.garage,
                        sqft                = EXCLUDED.sqft,
                        lot_size            = EXCLUDED.lot_size,
                        year_built          = EXCLUDED.year_built,
                        mls                 = EXCLUDED.mls,
                        property_type       = EXCLUDED.property_type,
                        property_category   = EXCLUDED.property_category,
                        list_price          = EXCLUDED.list_price,
                        remarks             = EXCLUDED.remarks,
                        listing_agent_name  = EXCLUDED.listing_agent_name,
                        listing_agent_phone = EXCLUDED.listing_agent_phone,
                        listing_agent_email = EXCLUDED.listing_agent_email,
                        listing_office_name = EXCLUDED.listing_office_name,
                        listing_office_phone = EXCLUDED.listing_office_phone,
                        buyer_agent_name    = EXCLUDED.buyer_agent_name,
                        buyer_agent_phone   = EXCLUDED.buyer_agent_phone,
                        buyer_agent_email   = EXCLUDED.buyer_agent_email,
                        buyer_office_name   = EXCLUDED.buyer_office_name,
                        buyer_office_phone  = EXCLUDED.buyer_office_phone,
                        photo_count         = EXCLUDED.photo_count,
                        photos              = EXCLUDED.photos,
                        parcel_account_num  = EXCLUDED.parcel_account_num,
                        parcel_county       = EXCLUDED.parcel_county,
                        parcel_geom         = EXCLUDED.parcel_geom,
                        parsed_payload      = EXCLUDED.parsed_payload,
                        raw_payload         = COALESCE(EXCLUDED.raw_payload, propelio_comps.raw_payload),
                        last_seen_at        = GREATEST(propelio_comps.last_seen_at, EXCLUDED.last_seen_at)
                    RETURNING (xmax = 0) AS is_insert
                    """,
                    {
                        **fields,
                        "parsed_payload": _Json(fields["parsed_payload"]),
                        "raw_payload": _Json(fields["raw_payload"]) if fields["raw_payload"] is not None else None,
                        "parcel_geom": _Json(fields["parcel_geom"]) if fields["parcel_geom"] is not None else None,
                        "photos": _Json(fields["photos"]) if fields["photos"] is not None else None,
                    },
                )
                row = cur.fetchone()
                if row and row[0]:
                    inserted += 1
                else:
                    updated += 1

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_session_conn(conn)

    return {"inserted": inserted, "updated": updated}


def load_comps_by_polygon(
    polygon_latlngs: list[list[float]],
    saved_area_id: str | None,
) -> list[dict[str, Any]]:
    """Query propelio_comps for all comps whose geom falls inside the given polygon.

    polygon_latlngs: raw [[lng, lat], ...] ring from the frontend (NOT closed).
    Returns a list of dicts ready for direct inclusion in the API response —
    each dict is {**parsed_payload, comp_address_key, user_rating, ratings_by_view,
    parcel_geom, parcel_account_num, parcel_county}.  Comps with NULL geom are excluded.

    Per-view ratings spec §3.2 (Alt-D, FMEA C3/C4/H5): user_rating stays the ARV
    value from the existing comp_ratings JOIN (unchanged). ratings_by_view is the
    additive per-view (nbv/export) blob from comp_ratings_views, aggregated via
    json_object_agg + COALESCE('{}') so unrated comps → {} (never SQL NULL). The
    _views JOIN is workspace_id-scoped (C4 cross-tenant leak guard) and GROUP BY
    every non-aggregated SELECT column (H5 — json_object_agg requires it or comp
    rows duplicate).
    """
    if not polygon_latlngs or len(polygon_latlngs) < 3:
        return []

    # Close the ring: PostGIS requires the first coord repeated at the end.
    ring = list(polygon_latlngs)
    if ring[0] != ring[-1]:
        ring = ring + [ring[0]]

    centroid_lat, centroid_lng = polygon_centroid(polygon_latlngs)
    circumradius_mi = max(
        haversine_miles(centroid_lat, centroid_lng, point_lat, point_lng)
        for point_lng, point_lat in polygon_latlngs
    )
    buffered_radius_mi = circumradius_mi * 1.05
    # Floor at 2mi so tight polygons still catch nearby pendings/actives
    # within reasonable distance (matches user mental model — when drawing
    # a tight polygon, you still expect to see comps a couple miles away).
    effective_radius_mi = min(max(buffered_radius_mi, 2.0), 10.0)
    circumradius_meters = effective_radius_mi * 1609.344

    geojson_polygon = {
        "type": "Polygon",
        "coordinates": [ring],
    }
    import json as _json
    geojson_str = _json.dumps(geojson_polygon)

    workspace_id = str(saved_area_id or "").strip() or None

    conn = get_session_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    pc.parsed_payload,
                    pc.comp_address_key,
                    pc.parcel_geom,
                    pc.parcel_account_num,
                    pc.parcel_county,
                    cr.rating AS user_rating,
                    COALESCE(
                        json_object_agg(crv.view, crv.rating)
                            FILTER (WHERE crv.view IN ('nbv','export')),
                        '{}'
                    ) AS ratings_by_view,
                    NOT ST_Within(
                        pc.geom,
                        ST_GeomFromGeoJSON(%(polygon)s)::geometry
                    ) AS is_outside_polygon
                FROM propelio_comps pc
                LEFT JOIN comp_ratings cr
                    ON cr.comp_id = pc.comp_id
                    AND cr.workspace_id = %(workspace_id)s
                LEFT JOIN comp_ratings_views crv
                    ON crv.comp_id = pc.comp_id
                    AND crv.workspace_id = %(workspace_id)s
                WHERE pc.geom IS NOT NULL
                    AND (
                        ST_Within(pc.geom, ST_GeomFromGeoJSON(%(polygon)s)::geometry)
                        OR ST_DWithin(
                            pc.geom::geography,
                            ST_SetSRID(ST_MakePoint(%(centroid_lng)s, %(centroid_lat)s), 4326)::geography,
                            %(circumradius_meters)s
                        )
                    )
                GROUP BY
                    pc.parsed_payload,
                    pc.comp_address_key,
                    pc.parcel_geom,
                    pc.parcel_account_num,
                    pc.parcel_county,
                    cr.rating,
                    is_outside_polygon
                """,
                {
                    "workspace_id": workspace_id,
                    "polygon": geojson_str,
                    "centroid_lat": centroid_lat,
                    "centroid_lng": centroid_lng,
                    "circumradius_meters": circumradius_meters,
                },
            )
            rows = cur.fetchall() or []
    finally:
        release_session_conn(conn)

    result: list[dict[str, Any]] = []
    for row in rows:
        (
            parsed_payload,
            comp_address_key,
            parcel_geom,
            parcel_account_num,
            parcel_county,
            user_rating,
            ratings_by_view,
            is_outside_polygon,
        ) = row
        comp = dict(parsed_payload) if isinstance(parsed_payload, dict) else {}
        comp["comp_address_key"] = comp_address_key
        comp["parcel_geom"] = parcel_geom if isinstance(parcel_geom, (dict, list)) else None
        comp["parcel_account_num"] = str(parcel_account_num or "").strip() or None
        comp["parcel_county"] = str(parcel_county or "").strip() or None
        comp["user_rating"] = str(user_rating).strip().lower() if user_rating is not None else None
        # ratings_by_view: {nbv?, export?} — COALESCE guarantees '{}' (never
        # NULL) so frontend Object.keys()/spread never crashes (H5). psycopg2
        # returns JSON as a Python dict already; normalize defensively.
        if isinstance(ratings_by_view, dict):
            comp["ratings_by_view"] = ratings_by_view
        elif isinstance(ratings_by_view, str):
            # Some driver configs return json as text; parse defensively.
            try:
                import json as _json2
                parsed = _json2.loads(ratings_by_view)
                comp["ratings_by_view"] = parsed if isinstance(parsed, dict) else {}
            except Exception:
                comp["ratings_by_view"] = {}
        else:
            comp["ratings_by_view"] = {}
        if is_outside_polygon:
            extra = comp.setdefault("extra", {})
            if isinstance(extra, dict):
                extra["is_outside_polygon"] = True
        result.append(comp)

    return result


ensure_tables()
