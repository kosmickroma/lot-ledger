// frontend/map.js
//
// Leaflet map init, draw tool, and all client-side UI logic.
// Sends drawn polygon to the backend, renders the GeoJSON response,
// populates the sidebar, and handles CSV download.
//
// Connects to:
//   frontend/index.html   — loaded as a script tag
//   frontend/style.css    — classes referenced here must exist there
//   POST /api/analyze     — sends polygon, receives GeoJSON + counts
//   GET  /api/download/:id — triggers CSV file download
//   GET  /api/job/:id/verification — persists verify/target tags

const DALLAS_CENTER = [32.78, -96.8];
const DEFAULT_ZOOM = 13;
// 2026-05-22: lowered from 9 → 5 so labels stay visible at low zooms
// (where they're MOST useful — analyst can see "you're looking at Tarrant"
// without zooming in). Font size scales inversely with zoom — see
// _updateCountyLabelStyles. Below 5 (continental US view) the labels
// would overlap into noise so we still gate at 5.
const COUNTY_LABEL_MIN_ZOOM = 5;

const COLORS = {
  single_family: "#2980b9",
  off_market: "#2980b9",
  vacant: "#27ae60",
  multifamily: "#2c2c2c",
  duplexes: "#9C7B8C",
  commercial: "#8B7355",
  exempt: "#95a5a6",
  active: "#D92228",
  sold: "#5C2D91",
};

const BORDER_COLORS = {
  single_family: "#1a6a9a",
  off_market: "#1a6a9a",
  vacant: "#1e8449",
  multifamily: "#1f1f1f",
  duplexes: "#7E6373",
  commercial: "#6e5c42",
  exempt: "#7f8c8d",
  active: "#a3161a",
};

// Browse layer — renders all county parcels from PMTiles file on GCS.
// URL is injected by the backend (TILES_BASE_URL env var) so each deployment
// can point at its own tiles bucket. Falls back to KK's public bucket.
const PMTILES_URL = (window.LL_CONFIG && window.LL_CONFIG.tilesUrl && window.LL_CONFIG.tilesUrl !== "__TILES_URL__")
  ? window.LL_CONFIG.tilesUrl
  : "https://storage.googleapis.com/lot-ledger-tiles/parcels.pmtiles";

const BUILD_ID = (window.LL_CONFIG && window.LL_CONFIG.buildId && window.LL_CONFIG.buildId !== "__BUILD_ID__")
  ? window.LL_CONFIG.buildId
  : "dev";

const APP_VERSION = (window.LL_CONFIG && window.LL_CONFIG.appVersion && window.LL_CONFIG.appVersion !== "__APP_VERSION__")
  ? window.LL_CONFIG.appVersion
  : "dev";

// Instant-reshape flag. Ships OFF via LL_CONFIG (see index.html). For preview
// testing it can be enabled per-browser WITHOUT committing the flag on:
//   - URL param  ?instantReshape=1   (one-off, current load)
//   - localStorage ll_instant_reshape="1"  (sticky for this browser)
// The committed default stays false so dev/main inherit it OFF.
const INSTANT_RESHAPE_ENABLED = Boolean(
  (window.LL_CONFIG && window.LL_CONFIG.instantReshape) ||
  /[?&]instantReshape=1\b/.test(window.location.search) ||
  (() => { try { return localStorage.getItem("ll_instant_reshape") === "1"; } catch (_e) { return false; } })()
);

// ARV · NBV · Export filter-view toggle. Ships OFF via LL_CONFIG. Per-browser
// override for preview soak without committing the flag on:
//   - URL param  ?arvNbvExport=1   (one-off, current load)
//   - localStorage ll_arv_nbv_export="1"  (sticky for this browser)
const ARV_NBV_EXPORT_ENABLED = Boolean(
  (window.LL_CONFIG && window.LL_CONFIG.arvNbvExport) ||
  /[?&]arvNbvExport=1\b/.test(window.location.search) ||
  (() => { try { return localStorage.getItem("ll_arv_nbv_export") === "1"; } catch (_e) { return false; } })()
);

function _formatAppVersionForDisplay(raw) {
  // Pill is white-space:nowrap and shares the sidebar header row with the
  // user dropdown. Long preview APP_VERSION (e.g. "0.28-feat-foo-bar-pre")
  // overflows and pushes the user bar off-screen. Strip the branch suffix
  // for display; keep "-pre" as the preview marker. Tooltip carries the
  // full string so the full build identity stays available.
  if (!raw || raw === "dev") return raw || "dev";
  if (raw.endsWith("-pre")) {
    const firstDash = raw.indexOf("-");
    if (firstDash > 0) return `${raw.slice(0, firstDash)}-pre`;
  }
  return raw;
}

(function _renderAppVersionPill() {
  const el = document.getElementById("ll-app-version");
  if (!el) return;
  el.textContent = `v${_formatAppVersionForDisplay(APP_VERSION)}`;
  el.title = `LotLedger v${APP_VERSION} (build ${BUILD_ID})`;
})();

let _serverVersionBaseline = null;
let _pendingMismatchVersion = null;
let _updateAvailable = false;
let _appShellReady = false;
let _bannerShown = false;

function _isSameOriginRequest(input) {
  try {
    let urlStr;
    if (typeof input === "string") {
      urlStr = input;
    } else if (input instanceof Request) {
      urlStr = input.url;
    } else if (input instanceof URL) {
      urlStr = input.href;
    } else {
      return true;
    }
    if (urlStr.startsWith("/")) return true;
    const url = new URL(urlStr, window.location.origin);
    return url.origin === window.location.origin;
  } catch (e) {
    return false;
  }
}

const _originalFetch = window.fetch.bind(window);
window.fetch = async function(input, init) {
  init = init || {};
  const headers = new Headers(init.headers || {});
  if (_isSameOriginRequest(input) && !headers.has("X-Client-Version")) {
    headers.set("X-Client-Version", BUILD_ID);
  }
  init.headers = headers;

  const response = await _originalFetch(input, init);

  if (_isSameOriginRequest(input)) {
    const serverVersion = response.headers.get("X-Version");
    if (serverVersion) {
      if (_serverVersionBaseline === null) {
        _serverVersionBaseline = serverVersion;
      } else if (serverVersion !== _serverVersionBaseline && !_updateAvailable) {
        if (_pendingMismatchVersion === serverVersion) {
          _updateAvailable = true;
          _maybeShowUpdateBanner();
        } else {
          _pendingMismatchVersion = serverVersion;
        }
      } else if (serverVersion === _serverVersionBaseline) {
        _pendingMismatchVersion = null;
      }
    }
  }
  return response;
};

function _maybeShowUpdateBanner() {
  if (_bannerShown) return;
  if (!_updateAvailable) return;
  if (!_appShellReady) return;
  try {
    if (typeof _currentUser === "undefined" || !_currentUser) return;
  } catch (e) {
    return;
  }
  _renderUpdateBanner();
}

function _renderUpdateBanner() {
  if (document.getElementById("ll-update-banner")) return;
  _bannerShown = true;
  const banner = document.createElement("div");
  banner.id = "ll-update-banner";
  banner.style.cssText = [
    "position:fixed",
    "top:0",
    "left:50%",
    "transform:translateX(-50%)",
    "max-width:480px",
    "background:#2a7",
    "color:#fff",
    "padding:10px 20px",
    "text-align:center",
    "z-index:100000",
    "cursor:pointer",
    "font-family:inherit",
    "font-size:14px",
    "box-shadow:0 2px 8px rgba(0,0,0,.2)",
    "border-radius:0 0 8px 8px",
  ].join(";");
  banner.innerHTML = 'A new version is available &mdash; <strong>click to refresh</strong>';
  banner.addEventListener("click", () => window.location.reload());
  document.body.appendChild(banner);
}

setInterval(() => {
  fetch("/version", { cache: "no-store" }).catch(() => {});
}, 60000);

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    fetch("/version", { cache: "no-store" }).catch(() => {});
  }
});

// Cross-tab coherence (see SUBJECT_PROPERTY_VISUAL_REDESIGN_SPEC.md):
// on tab refocus, refetch saved resources so renames/deletes/forks done in
// another tab propagate here. _reloadSavedResources defined later in the
// file; lookup is dynamic so this listener can register early.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  // FA8 (filter-autosave §2.4) — extended gate: skip while EITHER a
  // subject-save OR a filter-save is in-flight. The final settling
  // save (whichever happens last) triggers its own _reloadSavedResources,
  // so deferring here is safe — state catches up after.
  if (_pendingSubjectSaves + _pendingFilterSaves > 0) return;
  if (typeof _reloadSavedResources === "function") {
    _reloadSavedResources().catch((err) => console.warn("[visibilitychange] _reloadSavedResources failed:", err));
  }
});

const TYPE_LABELS = {
  single_family: "Off-Market SFR",
  vacant: "Vacant Lot",
  multifamily: "Multifamily",
  duplexes: "Duplexes",
  commercial: "Commercial",
  exempt: "Exempt",
  active: "Active Listing",
  sold: "Sold",
  off_market: "Off Market",
};

const SOLD_MARKER_COLOR = "#d4af37";
const SOLD_MARKER_BORDER = "#8b6b1f";
const SOLD_OUTLINE_COLOR = "#5C2D91";
const SOLD_FALLBACK_DOT_COLOR = "#004225";
const SOLD_FALLBACK_DOT_BORDER = "#5C2D91";
const FILTER_STORAGE_KEY = "lotledger.map.filters.v1";
const SIDEBAR_SECTION_STATE_STORAGE_KEY = "lot_ledger_sidebar_sections.v1";

const DEFAULT_FILTERS = {
  // Legacy R.F. filters (Redfin Listed + Redfin Sold) default OFF.
  // They live in the collapsed Legacy Filters card and are opt-in only.
  active: false,
  sold: false,
  contact_status: false,
  off_market: true,
  vacant: true,
  multifamily: false,
  duplexes: true,  // Mike request 2026-06-06: Duplexes is a primary deal type, default ON
  commercial: false,
  exempt: false,
};

const FILTER_INPUT_IDS = {
  active: "filter-active",
  sold: "filter-sold",
  contact_status: "filter-contact-status",
  off_market: "filter-off-market",
  vacant: "filter-vacant",
  multifamily: "filter-multifamily",
  duplexes: "filter-duplexes",
  commercial: "filter-commercial",
  exempt: "filter-exempt",
};

// Propelio comp → parcel-type bucket map (granular category preferred,
// coarse property_type as fallback). Used by compPassesPropelioFilters to
// gate visible comps the same way Property Type Filters gate parcels.
const PROPELIO_CATEGORY_TO_BUCKET = {
  SingleFamilyResidence: "single_family",
  Townhouse: "single_family",
  Ranch: "single_family",
  MobileHome: "single_family",
  ManufacturedHome: "single_family",
  Duplex: "multifamily",
  Triplex: "multifamily",
  Quadruplex: "multifamily",
  MultiFamily: "multifamily",
  Apartment: "multifamily",
  Condominium: "multifamily",
  Business: "commercial",
  Office: "commercial",
  Retail: "commercial",
  Industrial: "commercial",
  Warehouse: "commercial",
  MixedUse: "commercial",
  UnimprovedLand: "vacant",
};
const PROPELIO_TYPE_FALLBACK = {
  Residential: "single_family",
  ResidentialIncome: "multifamily",
  CommercialSale: "commercial",
  Land: "vacant",
  // Farm + anything blank → undefined → default visible
};

const NUMERIC_FILTER_INPUTS = [
  { id: "nf-lot-min",  key: "lot_sqft_min" },
  { id: "nf-lot-max",  key: "lot_sqft_max" },
  { id: "nf-val-min",  key: "appr_val_min" },
  { id: "nf-val-max",  key: "appr_val_max" },
  { id: "nf-yr-min",   key: "yr_built_min" },
  { id: "nf-yr-max",   key: "yr_built_max" },
  { id: "nf-sqft-min", key: "sqft_min" },
  { id: "nf-sqft-max", key: "sqft_max" },
];

const numericFilters = {
  lot_sqft_min: null, lot_sqft_max: null,
  appr_val_min: null, appr_val_max: null,
  yr_built_min: null, yr_built_max: null,
  sqft_min: null,     sqft_max: null,
};

// Comp Filters: same numeric schema as Property Filters, but applied only to listings + sold-matched
const compNumericFilters = {
  lot_sqft_min: null, lot_sqft_max: null,
  appr_val_min: null, appr_val_max: null,
  yr_built_min: null, yr_built_max: null,
  sqft_min: null,     sqft_max: null,
};

function parseShorthand(str) {
  const raw = String(str || "").trim();
  if (!raw) return null;
  const cleaned = raw.replace(/[,$\s]/g, "").toLowerCase();
  const match = cleaned.match(/^(-?\d+(?:\.\d+)?)([mk])?$/);
  if (!match) {
    const plain = Number(cleaned);
    return Number.isFinite(plain) ? plain : null;
  }
  const base = Number(match[1]);
  if (!Number.isFinite(base)) return null;
  const suffix = match[2] || "";
  if (suffix === "m") return base * 1_000_000;
  if (suffix === "k") return base * 1_000;
  return base;
}

function formatNumberWithCommas(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "";
  return Math.round(n).toLocaleString("en-US");
}

// -- Auth helpers ----------------------------------------------------------
// Reads the ll_csrf cookie set by the server middleware.
// The double-submit pattern: send the same value in X-CSRF-Token header.
function getCsrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)ll_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

function authHeaders() {
  return { "X-CSRF-Token": getCsrfToken() };
}

function _readNumericInputs() {
  NUMERIC_FILTER_INPUTS.forEach(({ id, key }) => {
    const el = document.getElementById(id);
    const raw = el ? el.value.trim() : "";
    if (raw === "") {
      numericFilters[key] = null;
      return;
    }

    if (key === "appr_val_min" || key === "appr_val_max") {
      numericFilters[key] = parseShorthand(raw);
      return;
    }

    if (key === "lot_sqft_min" || key === "lot_sqft_max") {
      const acres = Number(raw);
      numericFilters[key] = Number.isFinite(acres) ? acres * 43_560 : null;
      return;
    }

    const n = Number(raw);
    numericFilters[key] = Number.isFinite(n) ? n : null;
  });
}

function _clearNumericInputs() {
  NUMERIC_FILTER_INPUTS.forEach(({ id, key }) => {
    const el = document.getElementById(id);
    if (el) el.value = "";
    numericFilters[key] = null;
  });
}

function passesNumericFilters(feature) {
  const p = feature?.properties || {};
  // Display strings need parsing — asNumber() alone won't handle "7,500 sf" or "$250,000"
  const lotRaw = String(p.lot_sqft || "").replace(/,/g, "").match(/^[\d.]+/);
  const lot = lotRaw ? Number(lotRaw[0]) : null;

  const valRaw = String(p.tot_val || "").replace(/[$,]/g, "").match(/^[\d.]+/);
  const val = valRaw ? Number(valRaw[0]) : null;

  const yr = asNumber(p.yr_built); // "1972" -> 1972 — Number() handles plain strings fine

  const sqftRaw = String(p.sqft || "").replace(/,/g, "").match(/^[\d.]+/);
  const sqft = sqftRaw ? Number(sqftRaw[0]) : null;

  if (numericFilters.lot_sqft_min != null && (lot == null || lot < numericFilters.lot_sqft_min)) return false;
  if (numericFilters.lot_sqft_max != null && (lot == null || lot > numericFilters.lot_sqft_max)) return false;
  if (numericFilters.appr_val_min != null && (val == null || val < numericFilters.appr_val_min)) return false;
  if (numericFilters.appr_val_max != null && (val == null || val > numericFilters.appr_val_max)) return false;
  if (numericFilters.yr_built_min != null && (yr == null || yr < numericFilters.yr_built_min)) return false;
  if (numericFilters.yr_built_max != null && (yr == null || yr > numericFilters.yr_built_max)) return false;
  if (numericFilters.sqft_min != null && (sqft == null || sqft < numericFilters.sqft_min)) return false;
  if (numericFilters.sqft_max != null && (sqft == null || sqft > numericFilters.sqft_max)) return false;
  return true;
}

// Comp Filters (applied only to listings + sold-matched): same parsing logic as Property Filters
function passesCompFilters(feature) {
  const p = feature?.properties || {};
  const lotRaw = String(p.lot_sqft || "").replace(/,/g, "").match(/^[\d.]+/);
  const lot = lotRaw ? Number(lotRaw[0]) : null;

  const valRaw = String(p.tot_val || "").replace(/[$,]/g, "").match(/^[\d.]+/);
  const val = valRaw ? Number(valRaw[0]) : null;

  const yr = asNumber(p.yr_built);

  const sqftRaw = String(p.sqft || "").replace(/,/g, "").match(/^[\d.]+/);
  const sqft = sqftRaw ? Number(sqftRaw[0]) : null;

  if (compNumericFilters.lot_sqft_min != null && (lot == null || lot < compNumericFilters.lot_sqft_min)) return false;
  if (compNumericFilters.lot_sqft_max != null && (lot == null || lot > compNumericFilters.lot_sqft_max)) return false;
  if (compNumericFilters.appr_val_min != null && (val == null || val < compNumericFilters.appr_val_min)) return false;
  if (compNumericFilters.appr_val_max != null && (val == null || val > compNumericFilters.appr_val_max)) return false;
  if (compNumericFilters.yr_built_min != null && (yr == null || yr < compNumericFilters.yr_built_min)) return false;
  if (compNumericFilters.yr_built_max != null && (yr == null || yr > compNumericFilters.yr_built_max)) return false;
  if (compNumericFilters.sqft_min != null && (sqft == null || sqft < compNumericFilters.sqft_min)) return false;
  if (compNumericFilters.sqft_max != null && (sqft == null || sqft > compNumericFilters.sqft_max)) return false;
  return true;
}

const PARCEL_LAYER_KEYS = ["active", "sold", "off_market", "vacant", "multifamily", "duplexes", "commercial", "exempt"];

// -- Click mode helpers (Jump vs Stay) --
let currentClickMode = "stay";

function getClickMode() {
  return currentClickMode;
}

function setClickMode(mode) {
  if (mode !== "stay" && mode !== "jump") mode = "jump";
  currentClickMode = mode;
  updateClickModeButtonState();
}

function updateClickModeButtonState() {
  const jumpBtn = document.querySelector(".click-mode-btn.jump-mode");
  const stayBtn = document.querySelector(".click-mode-btn.stay-mode");
  if (jumpBtn) jumpBtn.classList.toggle("active", currentClickMode === "jump");
  if (stayBtn) stayBtn.classList.toggle("active", currentClickMode === "stay");
  // Mirror state onto the toolbar ZOOM button. Active (green) = jump mode.
  const zoomToolbarBtn = document.getElementById("btn-zoom-toggle");
  if (zoomToolbarBtn) {
    zoomToolbarBtn.classList.toggle("active", currentClickMode === "jump");
  }
}

// Keep the toolbar OAC button visually in sync with the Map Filters
// #prop-outside-area checkbox. Listener attached at startup (see init
// block at the bottom of this file). Updates active class whenever the
// checkbox changes — whether the change came from clicking the checkbox
// directly, clicking the toolbar OAC button (which dispatches a change),
// or programmatic state restoration on saved-area load.
function _updateOACButtonState() {
  const checkbox = document.getElementById("prop-outside-area");
  const btn = document.getElementById("btn-outside-area-toggle");
  if (!checkbox || !btn) return;
  btn.classList.toggle("active", Boolean(checkbox.checked));
}

function isPointInViewport(latlng) {
  if (!latlng) return false;
  const bounds = map.getBounds();
  return bounds.contains(latlng);
}

function areaBoundsInViewport(bounds) {
  if (!bounds) return false;
  const mapBounds = map.getBounds();
  return mapBounds.intersects(bounds);
}

// Analysis state is initialized early because zoom-nudge logic references it
// during startup before the rest of the module wiring runs.
let lastAnalysisGeojson = null;

// zoomSnap/zoomDelta=0.5 — Mike's +/- buttons now move in half-steps so the
// jump per click is less drastic. Mouse-wheel zoom is unaffected.
const map = L.map("map", { zoomControl: true, zoomSnap: 0.5, zoomDelta: 0.5 }).setView(DALLAS_CENTER, DEFAULT_ZOOM);
const MAP_CANVAS_RENDERER = L.canvas();
const MAP_SVG_RENDERER = L.svg();
L.control.scale({ position: "bottomright", metric: false, imperial: true }).addTo(map);

// crossOrigin (2026-06-16): tiles MUST be fetched as CORS requests. Without it,
// <img> tiles are cross-origin "no-cors" responses, and the browser's Opaque
// Response Blocking (Firefox ORB / Chromium ORB) blanks any tile whose response
// isn't a clean image — which happens when CARTO returns a rate-limit/error body
// (CARTO also sends `x-content-type-options: nosniff`, so the browser won't sniff).
// Both CARTO and ArcGIS send `Access-Control-Allow-Origin: *`, so CORS is safe here.
const streetLayer = L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", {
  attribution: "&copy; OpenStreetMap contributors &copy; CARTO",
  subdomains: "abcd",
  maxZoom: 20,
  crossOrigin: true,
});

const contrastLayer = L.tileLayer("https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png", {
  attribution: "&copy; OpenStreetMap contributors &copy; CARTO",
  subdomains: "abcd",
  maxZoom: 20,
  crossOrigin: true,
});

const satelliteLayer = L.tileLayer(
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  {
    attribution: "Tiles &copy; Esri",
    maxZoom: 20,
    crossOrigin: true,
  }
);

streetLayer.addTo(map);
let activeBasemap = "street";
const BASEMAP_STORAGE_KEY = "lotledger.activeBasemap";

// Transparent labels overlay — shown on top of satellite tiles.
// Must live in its own pane (not overlayPane) to avoid corrupting the
// protomaps canvas when Leaflet removes/re-adds this tile layer.
map.createPane("labelsPane");
map.getPane("labelsPane").style.zIndex = "450";
map.getPane("labelsPane").style.pointerEvents = "none";
const labelsLayer = L.tileLayer(
  "https://{s}.basemaps.cartocdn.com/rastertiles/voyager_only_labels/{z}/{x}/{y}{r}.png",
  { subdomains: "abcd", maxZoom: 20, opacity: 1, pane: "labelsPane", crossOrigin: true }
);

// Tile-error self-heal (2026-06-16): Leaflet does NOT retry a failed tile by
// default — a transient CARTO rate-limit / ORB block / network blip leaves a
// permanent grey hole until the user happens to pan across that exact tile.
// Retry each failed tile up to 2× with backoff (cache-busted only on retry, so
// normal tiles stay CDN-cacheable). Bounded per-tile, so no retry storm.
[streetLayer, contrastLayer, satelliteLayer, labelsLayer].forEach((layer) => {
  layer.on("tileerror", (e) => {
    const tile = e.tile;
    if (!tile || !e.coords) return;
    const attempt = (tile._llRetry || 0) + 1;
    if (attempt > 2) return;
    tile._llRetry = attempt;
    let url = layer.getTileUrl(e.coords);
    url += (url.includes("?") ? "&" : "?") + "_r=" + attempt;
    setTimeout(() => { tile.src = url; }, 700 * attempt);
  });
});

map.createPane("soldPane");
map.getPane("soldPane").style.zIndex = "640";

map.createPane("countyLabelPane");
map.getPane("countyLabelPane").style.zIndex = "645";
map.getPane("countyLabelPane").style.pointerEvents = "none";

// Saved-parcel (Target) pane sits above parcel fills + markers but below
// soldPane (640) and tooltipPane (650) so sold-price labels remain readable
// over orange targets.
map.createPane("savedParcelPane");
map.getPane("savedParcelPane").style.zIndex = "620";
map.getPane("savedParcelPane").style.pointerEvents = "none";

// Saved-target star pane sits above savedParcelPane so stars remain visible
// when gold parcel outlines are also rendered.
map.createPane("savedTargetStarPane");
map.getPane("savedTargetStarPane").style.zIndex = "635";
map.getPane("savedTargetStarPane").style.pointerEvents = "auto";

// Subject-property gold outline pane. Above savedParcelPane (cyan halo for
// orphan saved parcels, going away in chunk 3) but below selectedOutlinePane
// (purple selection wins visually). Pointer events enabled so outlines are
// clickable to open the popup with Load Area section.
map.createPane("subjectPropertyOutlinePane");
map.getPane("subjectPropertyOutlinePane").style.zIndex = "622";
map.getPane("subjectPropertyOutlinePane").style.pointerEvents = "auto";

// Selected-item outline pane — sits above the gold-halo savedParcelPane
// so when a saved target is also the current selection, the crisp purple
// line stays visible on top of the gold halo's diffuse glow. Decorative
// only; pointer-events: none lets clicks pass through to layers below.
map.createPane("selectedOutlinePane");
map.getPane("selectedOutlinePane").style.zIndex = "625";
map.getPane("selectedOutlinePane").style.pointerEvents = "none";

// Flood zone overlay pane — sits ABOVE the basemap (default tile pane at
// 200) but BELOW the parcel polygon panes (620+). Lets parcel polygons
// stay readable on top while flood fills tint the surrounding map. Spec
// decision (Phase 3): pane below parcels, above basemap, with debounced
// viewport-bounded refetch on moveend.
map.createPane("floodZonesPane");
map.getPane("floodZonesPane").style.zIndex = "410";
map.getPane("floodZonesPane").style.pointerEvents = "none";

// Apply saved basemap BEFORE browseLayer is added. If we switch after protomaps
// is on the map, the tile layer removal fires viewprereset → _invalidateAll on
// protomaps → _tileZoom = undefined → browse layer goes blank until next pan/zoom.
try {
  const _savedBm = localStorage.getItem(BASEMAP_STORAGE_KEY);
  if (_savedBm === "contrast") {
    streetLayer.remove(); contrastLayer.addTo(map); activeBasemap = "contrast";
  } else if (_savedBm === "satellite") {
    streetLayer.remove(); satelliteLayer.addTo(map); labelsLayer.addTo(map); activeBasemap = "satellite";
  }
} catch (_) {}

const drawControl = new L.Control.Draw({
  draw: {
    polygon: { shapeOptions: { color: "#f1c40f", weight: 3, fill: false } },
    rectangle: false,
    circle: false,
    circlemarker: false,
    marker: false,
    polyline: false,
  },
  edit: false,
});
map.addControl(drawControl);

// Browse layer — renders all county parcels from PMTiles file on GCS.
// Uses canvas rendering (not DOM). Always visible; draw results render on top
// because markerLayer is added to the map after this.
// One rule per prop_type per layer — v3 PolygonSymbolizer doesn't support
// function-based fill reliably; filter approach is the safe v3 pattern.
//
// opacity in PolygonSymbolizer controls stroke opacity, NOT fill opacity.
// Alpha must be embedded directly in the fill color string via rgba().
function _hexRgba(hex, alpha) {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

function _browseRules(dataLayer) {
  return [{
    dataLayer,
    symbolizer: new protomapsL.PolygonSymbolizer({
      fill:    (zoom, feature) => _hexRgba(COLORS[feature.props.prop_type] || COLORS.exempt, 0.1),
      stroke:  (zoom, feature) => BORDER_COLORS[feature.props.prop_type] || BORDER_COLORS.exempt,
      width: 1.5,
      opacity: 1.0,
      perFeature: true,
    }),
  }];
}

const browseLayer = protomapsL.leafletLayer({
  url: PMTILES_URL,
  paintRules: [..._browseRules("dcad"), ..._browseRules("tad"), ..._browseRules("collin"), ..._browseRules("denton")],
  labelRules: [],
  minZoom: 14,
});
browseLayer.addTo(map);
// PMTiles preflight health check — protomaps-leaflet v3.1.2 swallows all tile
// fetch errors (tile-done callback always fires as success; tileerror never fires
// for browseLayer). A missing or corrupt parcels.pmtiles silently blanks the map.
// Catches WHOLE-FILE failures only (404, missing file, total network failure) —
// NOT per-tile failures. That narrower scope is intentional.
// Banner is intentionally persistent until page reload — a 404 on parcels.pmtiles
// is a real infrastructure failure, not a transient blip worth auto-retrying.
// Fire-and-forget; never awaited.
fetch(PMTILES_URL, { headers: { Range: "bytes=0-16383" } })
  .then((r) => {
    if (r.status !== 206) {
      throw new Error(`PMTiles preflight: expected 206, got ${r.status}`);
    }
    return r.arrayBuffer();
  })
  .then((buf) => {
    const magic = [0x50, 0x4d, 0x54, 0x69, 0x6c, 0x65, 0x73, 0x03];
    if (buf.byteLength < magic.length) {
      throw new Error("PMTiles preflight: short header");
    }
    const view = new DataView(buf);
    const valid = magic.every((b, i) => view.getUint8(i) === b);
    if (!valid) throw new Error("PMTiles preflight: bad magic number");
  })
  .catch((err) => {
    try {
      console.error("[pmtiles-preflight]", err);
      if (!document.getElementById("ll-tiles-banner")) {
        const banner = document.createElement("div");
        banner.id = "ll-tiles-banner";
        banner.setAttribute("role", "alert");
        banner.textContent = "Parcel data is temporarily unavailable.";
        banner.style.position = "fixed";
        banner.style.top = "0";
        banner.style.left = "0";
        banner.style.right = "0";
        banner.style.zIndex = "12001";
        banner.style.padding = "10px 14px";
        banner.style.textAlign = "center";
        banner.style.fontSize = "13px";
        banner.style.fontWeight = "600";
        banner.style.background = "#ffe5e5";
        banner.style.color = "#7a1111";
        banner.style.borderBottom = "1px solid #ffc9c9";
        banner.style.boxShadow = "0 2px 8px rgba(0,0,0,0.12)";
        document.body.appendChild(banner);
      }
    } catch (_) {}
  });
// Disable pointer events on the canvas so draw result polygons beneath it
// receive clicks normally. queryTileFeaturesDebug still works via map.on("click").
const _browseContainer = browseLayer.getContainer && browseLayer.getContainer();
if (_browseContainer) _browseContainer.style.pointerEvents = "none";

// Hard cutoff: hide the browseLayer canvas at any zoom below 14.0. The layer's
// own `minZoom: 14` interacts with Leaflet's `Math.round(zoom)` tile-zoom
// calculation, which rounds 13.5 → 14, so half-steps would otherwise render
// the parcel layer at zoom 13.5 — producing flicker on pan as new tiles
// repaint from blank canvas. CSS display:none sidesteps that without touching
// Leaflet's add/remove lifecycle (which is coordinated with analysis mode).
function _updateBrowseLayerVisibility() {
  const container = browseLayer.getContainer && browseLayer.getContainer();
  if (!container) return;
  container.style.display = map.getZoom() >= 14 ? "" : "none";
}
map.on("zoomend", _updateBrowseLayerVisibility);
_updateBrowseLayerVisibility();

// === GPU pressure mitigation (2026-05-21) ===
// Gate the .saved-parcel-glow filter-stack animation by zoom level.
// At zoom < 15 (city/neighborhood overview), the gold halo's pulse is
// too small to perceive AND many halos may be visible at once — each
// runs its own GPU-composited animation. Suspending the pulse below
// the threshold cuts GPU load without hurting visual fidelity (the
// filter stack stays on; only the keyframe animation pauses).
// Pairs with the .saved-glow-suspended CSS rule in style.css.
const SAVED_GLOW_ZOOM_THRESHOLD = 15;
function _updateSavedGlowAnimationGate() {
  const container = map.getContainer();
  if (!container) return;
  if (map.getZoom() < SAVED_GLOW_ZOOM_THRESHOLD) {
    container.classList.add("saved-glow-suspended");
  } else {
    container.classList.remove("saved-glow-suspended");
  }
}
map.on("zoomend", _updateSavedGlowAnimationGate);
_updateSavedGlowAnimationGate();

// Nudge chip: visible below zoom 13 when not in draw-results mode.
const _zoomNudge = document.getElementById("zoom-nudge");
function _updateZoomNudge() {
  if (!_zoomNudge) return;
  const tooFarOut = map.getZoom() < 14;
  _zoomNudge.classList.toggle("hidden", !tooFarOut || Boolean(lastAnalysisGeojson));
}
map.on("zoomend", _updateZoomNudge);
map.on("moveend", () => {
  if (viewportRenderMode) _scheduleViewportRender();
});
map.on("zoomend", () => {
  if (viewportRenderMode) _scheduleViewportRender();
  refreshSoldPriceLabels();
  refreshRedfinPriceLabels();
  refreshPropelioPriceLabels();
  _updateCountyLabelVisibility();
});
_updateZoomNudge();

let drawLayer = L.layerGroup().addTo(map);
let maskLayer = L.layerGroup().addTo(map);
let markerLayer = L.layerGroup().addTo(map);
let redfinLayer = L.layerGroup().addTo(map);
let soldLayer = L.layerGroup().addTo(map);
const parcelTypeLayers = Object.fromEntries(
  PARCEL_LAYER_KEYS.map((key) => [key, L.layerGroup().addTo(markerLayer)])
);
const redfinToggleInput = document.getElementById("toggle-redfin");
let redfinLayerVisible = false;
let soldLayerVisible = true;
if (!redfinLayerVisible) {
  map.removeLayer(redfinLayer);
}
if (!soldLayerVisible) {
  map.removeLayer(soldLayer);
}
let verificationBadgeLayer = L.layerGroup().addTo(map);
let targetBadgeLayer = L.layerGroup().addTo(map);
// Persistent saved-parcel outlines (cyan). Keyed by account_num for dedup + removal.
const savedParcelLayer = L.layerGroup().addTo(map);
const savedParcelLayers = {};
const SAVED_TARGET_STAR_MAX_ZOOM = 14; // stars hide at this zoom and above
const savedTargetStarLayer = L.layerGroup().addTo(map);
const savedTargetStarMarkers = {};
const _ORIGINATOR_STAR_LAYER = L.layerGroup().addTo(map);
let _originatorStarMarker = null;

// === Subject Property layers (see docs/SUBJECT_PROPERTY_VISUAL_REDESIGN_SPEC.md) ===
// New visual language. Gold = subject property of a saved area. Stars at low zoom
// for nav (all subjects), stars at high zoom only on loaded area's subject,
// gold outlines at high zoom for all subjects.
// Keyed by `${county}::${account_num}` per county-aware identity rule (Copilot
// critique: account_num alone collides across counties).
const SUBJECT_PROPERTY_STAR_MAX_ZOOM = 14;
const subjectPropertyStarLayer = L.layerGroup().addTo(map);
const subjectPropertyOutlineLayer = L.layerGroup().addTo(map);
// Map: `${county}::${account_num}` -> {county, account_num, lat, lng, areas[]}
let _subjectPropertiesByKey = new Map();
// Map: `${county}::${account_num}` -> Leaflet marker (star)
const _subjectPropertyStarMarkers = new Map();
// Map: `${county}::${account_num}` -> Leaflet geoJSON layer (outline polygon)
const _subjectPropertyOutlineLayers = new Map();
// Geometry cache to avoid refetching the same parcel polygon. Keyed same way.
const _subjectPropertyGeometryCache = new Map();
// Track in-flight geometry fetches so we don't fire duplicates.
const _subjectPropertyGeometryInFlight = new Set();

// Bonded saved-parcels of the currently-loaded area. Populated by
// restoreSavedArea via GET /api/areas/{id} → seed_parcels. Cleared when no
// area is loaded. These render bold gold outlines (no star) at zoom >= 14
// alongside the subject-property outlines so the user sees:
//   - the area's CURRENT staged subject (star + glow, follows _currentTargetParcel)
//   - the area's persisted subject (glow only, from subject_properties)
//   - every other parcel they saved in this area (glow only)
// Keyed by `${county}::${account_num}`.
let _loadedAreaSeedParcelsByKey = new Map();

function _subjectPropertyKey(county, accountNum) {
  const c = String(county || "").trim().toLowerCase();
  const a = String(accountNum || "").trim();
  if (!c || !a) return null;
  return `${c}::${a}`;
}

function _subjectPropertyEscape(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Shared "Load Area" popup section. Consumed by BOTH _buildParcelDetailPanelHtml
// AND makePopupHtml so the popup and sidebar panel render identical Load Area
// affordances. Returns empty string when the parcel is NOT a Subject Property
// of any of the user's saved areas.
function _buildSubjectPropertyLoadAreaHtml(parcelProps) {
  if (!parcelProps || !parcelProps.account_num) return "";
  const county = String(parcelProps.source_county || "dcad").trim().toLowerCase();
  const key = _subjectPropertyKey(county, parcelProps.account_num);
  if (!key) return "";

  // v1.1 §2.4 — re-derive from _savedAreasCache on every popup render.
  // Eliminates the stale-dropdown bug: when a subject auto-save changes
  // an area's originator_parcel_*, the cached _subjectPropertiesByKey
  // view doesn't reflect it until the next _reloadSavedResources cycle.
  // .filter() is O(N) over N≈dozens; zero perf concern. Also handles
  // area-delete / rename / copy-as without manual invalidation.
  const _normCounty = county;  // already lowercased+trimmed above
  const _normAccount = String(parcelProps.account_num || "").trim();
  const areas = (_savedAreasCache || [])
    .filter((a) =>
      a.type === "area"
      && String(a.originator_parcel_county || "").trim().toLowerCase() === _normCounty
      && String(a.originator_parcel_account_num || "").trim() === _normAccount,
    )
    .map((a) => ({
      area_id: a.id,
      name: a.name || "",
      updated_at: a.updated_at || "",
    }));
  if (areas.length === 0) return "";

  const loadedId = String(_currentLoadedAreaId || "");

  if (areas.length === 1) {
    const a = areas[0];
    const isLoaded = String(a.area_id) === loadedId;
    const buttonHtml = isLoaded
      ? `<button type="button" class="subject-property-load-btn" disabled>Currently loaded</button>`
      : `<button type="button" class="subject-property-load-btn" data-area-id="${_subjectPropertyEscape(a.area_id)}">Load Area</button>`;
    return `<div class="subject-property-load-section" data-mode="single">
      <div class="subject-property-load-label">Subject of saved area:</div>
      <div class="subject-property-load-row">
        <span class="subject-property-load-name">${_subjectPropertyEscape(a.name)}</span>
        ${buttonHtml}
      </div>
    </div>`;
  }

  // Multi-area: dropdown defaults to most-recent (areas[0]).
  const options = areas
    .map(a => `<option value="${_subjectPropertyEscape(a.area_id)}">${_subjectPropertyEscape(a.name)}</option>`)
    .join("");
  const firstId = String(areas[0].area_id);
  const firstIsLoaded = firstId === loadedId;
  return `<div class="subject-property-load-section" data-mode="multi">
    <div class="subject-property-load-label">Subject of saved area:</div>
    <div class="subject-property-load-row">
      <select class="subject-property-load-select" aria-label="Select saved area">${options}</select>
      <button type="button" class="subject-property-load-btn"
        data-area-id="${_subjectPropertyEscape(firstId)}"
        ${firstIsLoaded ? "disabled" : ""}>${firstIsLoaded ? "Currently loaded" : "Load Area"}</button>
    </div>
  </div>`;
}
// Invisible click-catcher polygons that mirror saved-parcel outlines. The
// decorative halo on `savedParcelPane` is pointer-events:none so its drop-shadow
// bleed doesn't catch stray clicks; this parallel layer (default overlay pane)
// is what actually opens the popup. Keyed by account_num.
const savedParcelClickLayer = L.layerGroup().addTo(map);
const savedParcelClickLayers = {};
// Selected-item outline. Holds at most one L.geoJSON at a time — a new
// selection clears the previous. Cleared by any map click. Sources:
// saved-areas-list click + propelio-comp-list click.
const selectedOutlineLayer = L.layerGroup().addTo(map);
const measureLayer = L.layerGroup().addTo(map);
// Propelio comps rendered from address/polygon pulls.
// Parcel geometry is preferred (purple glowing footprints); missing geometry
// falls back to compact purple dots.
const propelioCompLayer = L.layerGroup().addTo(map);
// Outreach overlay layer. Kept below cadRatingLayer so CAD rating marks stay
// readable when both are present on the same parcel.
const outreachOverlayLayer = L.layerGroup().addTo(map);
const outreachOverlayLayerByKey = new Map();
const outreachOverlayGeomSeen = new Set();
// Parcel (CAD) rating marks layer. Independent from propelioCompLayer
// so the marks survive comp re-renders. Per PARCEL_RATINGS_SPEC.md v2.
const cadRatingLayer = L.layerGroup().addTo(map);
const cadRatingLayerByKey = new Map();  // "county:account_num" → leaflet marker
// (Old marker-based "Get Comps" button retired. The sticky DOM-based
// version lives in propelioStickyAnchor / propelioStickyBtn declared
// near _ensureStickyPropelioButton.)
let propelioPolygonPullInFlight = false;
let countyLayer = null;
let countyLabelLayer = null;
let countyVisible = false;
let hoaLayer = null;
let hoaVisible = false;
// Flood zones overlay state. PMTiles-backed via protomaps-leaflet — the
// layer is created lazily on first toggle-on and reused across toggles.
// OFF by default; user opts in via the FLOOD button in the map toolbar.
let floodZonesLayer = null;
let floodZonesVisible = false;
let currentJobId = null;
let lastPolygon = null;
let lastDrawnLatLngs = null;
let lastAnalysisCounts = null;
let lastIncludedRedfin = false;
let lastIncludedSold = false;
let lastSoldPoints = [];
let lastSoldPanelPoints = [];
let matchedSoldLabelPoints = [];
// Tracks account_nums of sold-matched parcels that actually rendered in the
// current renderFeatures pass (after passesNumericFilters). renderSoldPoints
// uses this to suppress price labels for parcels filtered out by numeric
// filters (e.g., lot-size min/max), so labels follow parcel visibility.
let _currentlyRenderedSoldAccounts = new Set();
let soldMarkers = [];
let redfinMarkers = [];
let transientSoldSidebarPopup = null;
let soldCompsSortMode = "price";
let soldCompsCollapsed = true;
const DEFAULT_SOLD_COMPS_FILTER = {
  maxDaysAgo: 365,
  minPrice: null,
  maxPrice: null,
  minYearBuilt: null,
  maxYearBuilt: null,
};
let soldCompsFilter = { ...DEFAULT_SOLD_COMPS_FILTER };
let allSoldPointsRef = [];
let filterState = { ...DEFAULT_FILTERS };
const verificationByAccount = new Map();
const potentialTargetByAccount = new Map();
const verificationBadgeMarkers = new Map();
const targetBadgeMarkers = new Map();
let allAnalysisFeatures = null;   // full feature set from last analysis
let viewportRenderMode = false;   // true when feature count exceeds render threshold
let _vpRenderTimeout = null;      // debounce handle for viewport re-render
const LARGE_DRAW_THRESHOLD = 500;  // viewport-only rendering above this count
const BROWSE_ONLY_THRESHOLD = 30000; // skip all polygon rendering above this; use browse layer
let _activeParcelPopupState = null;
let _isRefreshingParcelLayers = false;
let _suspendViewportRenderUntil = 0;
const _renderedParcelPopupLayers = new Map();
let _analysisRequestSeq = 0;
let _activeAnalysisRequestId = 0;
let _activeAnalysisAbortController = null;
let undoPillVersion = 0;
let _activeUndoSnapshot = null;
let _undoPillTimer = null;
let _savedAreasCache = [];
let _savedParcelsCache = [];
// Saved-list filter state for the search bars added in Mike bundle 4.
// Updated by the input event listeners wired below; consumed by
// renderSavedAreasList() to scope the items passed to _renderList.
let _savedAreasSearchQuery = "";
let _savedTargetsSearchQuery = "";
let _currentSessionIsNamed = false;
let _savedSessionsCache = [];
let _currentLoadedAreaId = null;
let _reshapeTargetAreaId = null;
let _preReshapeFeatures = null;        // full pre-clip set, for restore-on-error / reconcile (Chunk C)
let _reshapeClippedSubset = null;      // the optimistic clip result (Chunk C compares against analyze)
let _reshapeOptimisticApplied = false; // did we optimistic-render this reshape? (Chunk C)
// ARV · NBV · Export filter-view toggle state (Feature #3). Both are purely
// in-memory and per-tab — not persisted. _viewFilterCache is null per view
// until hydrated from the loaded area's filter_state on area load.
let _activeView = "arv";   // 'arv' | 'nbv' | 'export'
let _viewFilterCache = { arv: null, nbv: null, export: null };
let _currentTargetParcel = null; // { county, account, lat?, lng? } | null
// v1.1 §2.6 — gates cross-tab refetch to prevent flicker when concurrent
// subject-saves are in-flight. See TkDodo "Concurrent Optimistic Updates
// in React Query" pattern. Decremented in finally blocks. Read by SA5.
let _pendingSubjectSaves = 0;

// v1 §2.1 filter-state auto-save — mirrors stored-values save machinery
// at frontend/map.js:12183+. _filterSavePending is a single boolean
// (vs stored-values' per-field Set) because filter_state is one whole
// JSON blob written by a single PUT. _filterSaveInflight serializes
// concurrent saves. _pendingFilterSaves counter (parallel to
// _pendingSubjectSaves) gates the visibilitychange cross-tab refetch.
const _FILTER_SAVE_DEBOUNCE_MS = 600;
let _filterSaveDebounceTimer = null;
let _filterSaveFlashTimer = null;
let _filterSavePending = false;
let _filterSaveInflight = false;
let _pendingFilterSaves = 0;
// Sprint 2 multi-user collab: per-field PATCH queue state.
// Per docs/MULTIUSER_COLLAB_SPRINT2_SPEC.md v2 §5.
// Map<field_key, value> — pending PATCHes drained on flush.
const _filterSavePendingFields = new Map();
// Monotonic counter incremented on each PATCH dispatch.
let _filterSaveClientSeq = 0;
// Last successfully-PATCHed (or restored) state, used to diff against
// captureFilterState() output and decide which fields actually changed.
// null = no area loaded; reset on clearDrawResults, set on restoreSavedArea.
let _filterSaveLastSnapshot = null;
// When true, _filterSaveQueueSave() is a no-op. Set during restoreFilterState
// so LOADING a view/area (incl. the comp re-filter it triggers) never generates
// save traffic — a view switch must not "save," only display (KK 2026-06-29).
// Genuine user edits (input handlers) and the explicit seed persist run with
// this false, so they still save normally.
let _suppressFilterAutosave = false;

// Sprint 3 multi-user collab: SSE subscriber state.
// Per docs/MULTIUSER_COLLAB_SPRINT3_SPEC.md v1 §4.1.
let _sseEventSource = null;           // current EventSource instance
let _sseAreaId = null;                 // which area it's subscribed to
// KK debug 2026-06-06: zombie-connection watchdog. _sseLastMessageAt
// updated by every SSE event handler (including the synthetic 'connected'
// fired at stream open). The 60s watchdog (see _sseWatchdogTick) checks
// the gap and force-reconnects if no events arrived for >90s while the
// tab is visible and an area is loaded. Covers the Edge tab-throttling
// case where the EventSource thinks it's open but the backend's keepalive
// is being silently swallowed.
let _sseLastMessageAt = 0;
let _sseWatchdogInterval = null;
const _sseSessionUuid = (typeof crypto !== "undefined" && crypto.randomUUID)
  ? crypto.randomUUID()
  : `sess-${Math.random().toString(36).slice(2)}-${Date.now()}`;
// Track last-dispatched client_seq per field for self-echo filtering.
// Updated after each successful PATCH dispatch in _filterSaveProcessQueue.
const _dispatchedSeqByField = new Map();  // field_key -> client_seq we sent

// v1.1 §2.6 — 50ms debounce on the popup Save Parcel link. Absorbs
// event-bubbling and mobile double-tap. Single deliberate clicks
// (>50ms apart) work normally.
const SAVE_PARCEL_DEBOUNCE_MS = 50;
let _lastSaveParcelClickAt = 0;
let _targetCoordsResolvePromise = null;
let _measureModeEnabled = false;
let _measurePoints = [];
// Tracks the most recent address the user searched or selected via typeahead.
// Deep Pull uses this as the target address for the experimental run.
let _lastSearchedAddress = null;
let _selectedSavedItemId = null;
const _initialAreaShareId = (() => {
  try {
    const v = new URLSearchParams(window.location.search).get("area");
    if (v && /^area_[A-Za-z0-9]{10}$/.test(v)) return v;
  } catch {}
  return null;
})();
let _pendingAreaShareId = _initialAreaShareId;

const HOA_COLOR = "#b8860b";

function _normalizeTargetParcel(parcel) {
  if (!parcel || typeof parcel !== "object") return null;
  const county = String(parcel.county || "").trim().toLowerCase();
  const account = String(parcel.account || "").trim();
  if (!county || !account) return null;
  const lat = Number(parcel.lat);
  const lng = Number(parcel.lng);
  return {
    county,
    account,
    lat: Number.isFinite(lat) ? lat : null,
    lng: Number.isFinite(lng) ? lng : null,
  };
}

function _sameParcelIdentity(a, b) {
  if (!a || !b) return false;
  return String(a.county || "").trim().toLowerCase() === String(b.county || "").trim().toLowerCase()
    && String(a.account || "").trim() === String(b.account || "").trim();
}

// Per-county normalizer: produces "STREET CITY" with no comma, no state,
// no ZIP, no duplicate city. Each county's source data is shaped
// differently — this is the single place that handles the variance.
//
// - collin: property_address bundles "STREET CITY, TX ZIP"  → strip from first comma.
// - denton: property_address is "STREET, CITY, TX"          → take street, append property_city.
// - tad   : property_address is "STREET" only (no city)     → return street.
// - dcad  : property_address is "STREET" only (no city)     → return street.
//
// TAD + DCAD will appear without a city until KK lands a city-resolver
// for those two (memory: project_save_vs_update_model notes scope).
function _formatPropertyAddress(county, rawAddr, rawCity, _rawOwnerCity) {
  // Normalize internal whitespace (Collin source ships some addrs with a
  // literal CRLF between street + city — see _formatFullPropertyAddress
  // for the longer comment). Done once at the top so every branch below
  // sees a clean single-space-delimited string.
  const addr = String(rawAddr || "").replace(/\s+/g, " ").trim();
  if (!addr) return "";
  // 2026-05-22: filter 'NO CITY' (TAD unincorporated marker) / NONE / N/A
  // placeholders. owner_city fallback REMOVED 2026-05-22 — confirmed wrong
  // for absentee owners (e.g. Houston resident owning Fort Worth area
  // parcel would show 'HOUSTON' as property city). Until TIGER Places
  // spatial-join backfill lands (master_todo: 'TIGER Places + PostGIS
  // city resolution'), unincorporated parcels just omit the city.
  const city = _normalizeCityForDisplay(rawCity);
  const c = String(county || "").trim().toLowerCase();

  switch (c) {
    case "collin": {
      // "1713 N COLLEGE ST MCKINNEY, TX 75069" → "1713 N COLLEGE ST MCKINNEY"
      const beforeComma = addr.split(",")[0].trim();
      return beforeComma || addr;
    }
    case "denton": {
      // "8812 ENCLAVE WAY, NORTHLAKE, TX" → "8812 ENCLAVE WAY" + " NORTHLAKE"
      const street = addr.split(",")[0].trim();
      if (city && street) {
        const lower = street.toLowerCase();
        const cityLower = city.toLowerCase();
        if (!lower.endsWith(cityLower)) {
          return `${street} ${city}`;
        }
      }
      return street || addr;
    }
    case "tad": {
      // 2026-05-21: TAD now ships property_city via the tad_city_lookup
      // join populated by scripts/build_tad_city_lookup.py from
      // Cities.shp's DBF (CITY_TDC → CITY_NAME). addr is still
      // street-only from the ParcelView source; city comes from the
      // lookup. Same shape as Denton's path.
      const street = addr.split(",")[0].trim();
      if (city && street) {
        const lower = street.toLowerCase();
        const cityLower = city.toLowerCase();
        if (!lower.endsWith(cityLower)) {
          return `${street} ${city}`;
        }
      }
      return street || addr;
    }
    case "dcad": {
      // 2026-05-21: DCAD now ships property_city populated from
      // ACCOUNT_INFO.CSV via scripts/build_dcad_property_city.py.
      // The "(DALLAS CO)" multi-county-disambiguation suffix is
      // stripped at ingest time, so city is a clean uppercase name
      // (e.g., "GARLAND", "MESQUITE", "DALLAS"). Same shape as Denton.
      const street = addr.split(",")[0].trim();
      if (city && street) {
        const lower = street.toLowerCase();
        const cityLower = city.toLowerCase();
        if (!lower.endsWith(cityLower)) {
          return `${street} ${city}`;
        }
      }
      return street || addr;
    }
    default: {
      // Unknown county: conservative — return addr, append city only if
      // it isn't already the trailing token.
      if (city) {
        const tokens = addr.split(/\s+/);
        const last = (tokens[tokens.length - 1] || "").toLowerCase();
        if (last !== city.toLowerCase()) return `${addr} ${city}`.trim();
      }
      return addr;
    }
  }
}

function _setCurrentTargetParcel(parcel) {
  const normalized = _normalizeTargetParcel(parcel);
  _currentTargetParcel = normalized;
  _targetCoordsResolvePromise = null;
  if (
    normalized
    && (!Number.isFinite(normalized.lat) || !Number.isFinite(normalized.lng))
  ) {
    void _ensureCurrentTargetParcelCoords();
  }
  // Keep the Target row in the active-item slot in lock-step with the
  // current bonded originator. Non-null → resolve address + show row;
  // null → hide row. Address resolution is async (cache-first, fetch
  // fallback) — the row stays hidden until resolution completes to
  // avoid a "Target —" placeholder flash.
  if (normalized) {
    void _refreshOriginatorTargetLabel(normalized.county, normalized.account);
  } else {
    _setOriginatorTargetLabel(null);
  }
}

function _setOriginatorTargetLabel(addr) {
  // Two surfaces share this label: the top active-item slot row AND the
  // mirror row inside the Comps List block. Both are updated atomically in
  // this single synchronous function — no separate state, no async hop, no
  // way for the two to diverge. The mirror has its own styling but reads
  // the same text + hidden state as the slot.
  // Top active-item slot only writes the simple "STREET CITY" address.
  // The Comps List block CARD has its own full-address element
  // (#comps-block-target-card-addr) populated by
  // _populateSubjectPropertyCard with "STREET, CITY, TX ZIP". Both
  // surfaces still update together via _refreshOriginatorTargetLabel —
  // top + bottom are linked, just different render formats.
  const row = document.getElementById("active-item-target-row");
  const nameEl = document.getElementById("active-item-target-name");
  const cardRow = document.getElementById("comps-block-target-row");
  const text = (addr || "").toString().trim();
  if (!text) {
    if (nameEl) nameEl.textContent = "";
    if (row) row.classList.add("hidden");
    if (cardRow) cardRow.classList.add("hidden");
    // Also wipe the card's rich fields so a direct clearActiveItem path
    // (which doesn't go through _refreshOriginatorTargetLabel) doesn't
    // leave stale price/nbhd/meta behind for the next render.
    _populateSubjectPropertyCard(null);
    return;
  }
  if (nameEl) nameEl.textContent = text;
  if (row) row.classList.remove("hidden");
  if (cardRow) cardRow.classList.remove("hidden");
}

async function _resolveTargetParcelFeatureProps(county, account) {
  // Returns the parcel's feature.properties dict (or null). Two-tier:
  //   1) Cache-first against lastAnalysisGeojson — when the target is
  //      inside the current polygon analysis, we already have the props
  //      and skip the network round-trip.
  //   2) Otherwise, fetch /api/parcel/{county}/{account} and use its
  //      properties.
  // Caller is responsible for race-guarding against _currentTargetParcel
  // moving on between fetch start and resolve.
  const c = String(county || "").trim().toLowerCase();
  const a = String(account || "").trim();
  if (!c || !a) return null;

  if (Array.isArray(lastAnalysisGeojson?.features)) {
    for (const f of lastAnalysisGeojson.features) {
      const p = f?.properties || {};
      if (String(p.account_num || "").trim() === a
        && String(p.source_county || "").trim().toLowerCase() === c) {
        return p;
      }
    }
  }

  try {
    const resp = await fetch(`/api/parcel/${encodeURIComponent(c)}/${encodeURIComponent(a)}`);
    if (!resp.ok) return null;
    const detail = await resp.json();
    return detail?.properties || detail || null;
  } catch (err) {
    console.warn("[subject-property] parcel props resolve failed:", err);
    return null;
  }
}

function _popupHeaderAddress(props) {
  // Parcel popup header — full "STREET, CITY, TX ZIP" address format
  // matching the Subject Property card. Previously the popup used
  // raw props.addr which is street-only for DCAD/TAD parcels (city
  // sits on a separate prop). All four counties populate the
  // canonical city field after the 2026-05-20/21 city-resolution work,
  // so this works uniformly across DCAD / TAD / Collin / Denton.
  // Falls back to props.addr if county is unknown or city missing.
  const county = String(props?.source_county || "").trim().toLowerCase();
  if (!county) return String(props?.addr || "").trim();
  return _formatFullPropertyAddress(county, props);
}

// City placeholder strings that mean "no incorporated city" in source data.
// TAD encodes unincorporated Tarrant County parcels with city_code='000' which
// the lookup table maps to the literal "NO CITY". DCAD uses blank. Anything
// that matches this set should display as if there's no city at all.
const _CITY_PLACEHOLDER_VALUES = new Set([
  "NO CITY",
  "NONE",
  "N/A",
  "UNKNOWN",
  "UNINCORPORATED",
]);

function _normalizeCityForDisplay(rawCity) {
  const city = String(rawCity || "").trim();
  if (!city) return "";
  if (_CITY_PLACEHOLDER_VALUES.has(city.toUpperCase())) return "";
  return city;
}

function _formatFullPropertyAddress(county, props) {
  // Full USPS-style address for the Subject Property card:
  //   "STREET, CITY, TX ZIP"
  // Distinct from _formatPropertyAddress (which trims to "STREET CITY"
  // for the top slot label). The card wants all the disambiguating
  // info — analysts cross-reference against MLS / public records.
  //
  // Per-county quirks on the source addr field:
  //   - collin / denton: addr is bundled (e.g. "1713 N COLLEGE ST MCKINNEY, TX 75069"
  //                      or "8812 ENCLAVE WAY, NORTHLAKE, TX") — extract the street
  //                      portion by splitting on first comma; if the street still has
  //                      the city as the last token, strip it so we don't repeat.
  //   - dcad / tad:     addr is street-only — use as-is.
  const c = String(county || "").trim().toLowerCase();
  const rawAddr = String(props?.addr || "").trim();
  // 2026-05-22: treat 'NO CITY' (TAD's unincorporated-county marker) /
  // NONE / N/A as empty. owner_city fallback REMOVED — absentee owners
  // would inject the WRONG city (e.g. Houston resident with Fort Worth
  // area parcel). Until TIGER Places spatial-join backfill lands (see
  // master_todo), unincorporated parcels simply omit the city portion.
  const city = _normalizeCityForDisplay(props?.city);
  const zip = String(props?.property_zip || "").trim();

  let street = rawAddr;
  if (c === "collin" || c === "denton") {
    street = rawAddr.split(",")[0].trim();
  }
  // Normalize internal whitespace — some Collin source rows have a
  // literal CRLF between street and city ("4416 QUERIDA AVE \r\nMCKINNEY")
  // which renders visually as a space (CSS whitespace collapse) but
  // breaks the endsWith(" CITY") strip below since the char before
  // the city is \n not " ". Collapse any \s+ run to a single space so
  // the strip can detect the city suffix reliably.
  street = street.replace(/\s+/g, " ").trim();
  // For all counties: if the extracted street ends with the city
  // (Collin's bundled format does this), trim it so we don't show
  // "1713 N COLLEGE ST MCKINNEY, MCKINNEY, TX 75069". Loop the strip
  // for the rare case the city is literally repeated in the source
  // ("STREET CITY CITY") — defensive, single-iteration is the common path.
  if (city) {
    const cityUpperWithSpace = " " + city.toUpperCase();
    while (street.toUpperCase().endsWith(cityUpperWithSpace)) {
      street = street.slice(0, -(city.length + 1)).trim();
      if (!street) break;
    }
  }

  const parts = [];
  if (street) parts.push(street);
  if (city) parts.push(city);
  // State always "TX" for our four counties; zip is optional.
  if (zip) {
    parts.push(`TX ${zip}`);
  } else {
    parts.push("TX");
  }
  return parts.join(", ");
}

// Last resolved subject-property props (display-formatted strings), stashed so
// AI mode's band math (_autoMatchSubjectDims) can read the subject's lot/sqft
// off the current target.
let _lastSubjectProps = null;

function _populateSubjectPropertyCard(props, county) {
  _lastSubjectProps = props || null;
  // The AI-mode button's enabled/disabled state depends on the subject's dims
  // (no subject -> nothing to build a lot/sqft band from), so it has to be
  // re-evaluated the moment the subject changes under it.
  try { _renderAiBar(); } catch { /* bar not mounted yet — harmless */ }
  // Deliberately NOT recomputed here on a target change (Task 4, 2026-07-14
  // AI bar spec — this was auto-match's job via _applyAutoMatchIfEnabled,
  // now removed). AI mode is pressed by a human; it does not silently
  // recompute its bands in the background when the subject changes under it.
  // Fills the rich Subject Property card in the Comps List block:
  //   - Total Value (cyan #22d3ee, top-left)
  //   - Subject badge (cyan, top-right) — static text, always "Subject"
  //   - Full address: "STREET, CITY, TX ZIP" (separate from top slot's
  //     trimmed "STREET CITY" form so the card carries the disambiguating
  //     state + zip)
  //   - Subdivision line (italic gold)
  //   - Meta line: sqft · ac lot · year built · school district
  // Called atomically from _refreshOriginatorTargetLabel — no separate
  // state, no possibility of divergence from the top slot row.
  const priceEl = document.getElementById("comps-block-target-card-price");
  const addrEl = document.getElementById("comps-block-target-card-addr");
  const nbhdEl = document.getElementById("comps-block-target-card-nbhd");
  const metaEl = document.getElementById("comps-block-target-card-meta");

  if (!props) {
    if (priceEl) priceEl.textContent = "—";
    if (addrEl) addrEl.textContent = "—";
    if (nbhdEl) nbhdEl.textContent = "";
    if (metaEl) metaEl.textContent = "";
    return;
  }

  // Full address with state + zip on the card (top slot keeps the simple
  // "STREET CITY" form for compactness).
  if (addrEl) {
    addrEl.textContent = _formatFullPropertyAddress(county, props);
  }

  // Price = CAD total value. tot_val on feature.properties is a
  // pre-formatted "$NNN,NNN" string (see build_feature in dcad.py:500-ish).
  if (priceEl) {
    const totVal = String(props.tot_val || "").trim();
    priceEl.textContent = totVal || "—";
  }

  if (nbhdEl) {
    const sub = String(props.subdivision || "").trim();
    nbhdEl.textContent = sub;
  }

  // Line 1: dimensional + identity strip.
  // Order: sqft, lot acres, year built, beds, baths, garage, school.
  // Pool moved to line 2 amenity strip per v3 spec.
  if (metaEl) {
    const parts = [];
    const sqft = String(props.sqft || "").trim();
    if (sqft && sqft !== "N/A") parts.push(`${sqft} sqft`);
    const acres = String(props.lot_acres || "").trim();
    if (acres && acres !== "N/A") parts.push(acres);
    const yr = String(props.yr_built || "").trim();
    if (yr && yr !== "N/A") parts.push(yr);
    const beds = props.beds;
    if (beds && beds !== "N/A") parts.push(`${beds}bd`);
    const baths = props.baths;
    if (baths && baths !== "N/A") {
      const bathsNum = Number(baths);
      const bathsStr = Number.isFinite(bathsNum)
        ? (bathsNum % 1 === 0 ? `${bathsNum}ba` : `${bathsNum.toFixed(1)}ba`)
        : null;
      if (bathsStr) parts.push(bathsStr);
    }
    const garage = props.garage_capacity;
    if (garage && garage !== "N/A") {
      const garageNum = Number(garage);
      if (Number.isFinite(garageNum) && garageNum > 0) {
        parts.push(`${garageNum}-car garage`);
      }
    }
    // Pool on line 1 (always visible) — the line-2 amenity cap can hide
    // it when DCAD parcels have rich structural data filling the first
    // 5 slots. Pool is the most investor-relevant amenity flag → top.
    const poolFlag = String(props.pool_flag || "").trim().toUpperCase();
    if (poolFlag === "T") parts.push("pool");
    const school = String(props.school || "").trim();
    if (school && school !== "N/A") parts.push(school);
    metaEl.textContent = parts.join(" · ");
  }

  // Helpers reused across line 2 + line 3.
  const isTruthyFlag = (v) => String(v || "").trim().toUpperCase() === "T";
  const titleCase = (s) => {
    const text = String(s || "").trim();
    if (!text || text === "N/A") return "";
    return text.split(/\s+/).map((w) =>
      w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()
    ).join(" ");
  };

  // Line 2 (v3): structural materials + systems. Cap 6 tokens.
  // Order: structure type → foundation → ext wall → roof material →
  // HVAC summary → construction frame (if structure_type was missing,
  // already covered there; this lets DCAD show Frame separately when
  // we have both structure and frame info).
  const meta2El = document.getElementById("comps-block-target-card-meta2");
  if (meta2El) {
    const tokens = [];
    const MAX_TOKENS = 6;

    // 1. Structure type — prefer explicit structure_type (TAD), else
    //    derive from construction_frame_type + stories (DCAD-ish).
    const structureType = String(props.structure_type || "").trim();
    if (structureType && structureType !== "N/A") {
      tokens.push(structureType);
    } else {
      const frame = String(props.construction_frame_type || "").trim();
      const stories = String(props.stories || "").trim();
      if (frame && frame !== "N/A") {
        const titled = titleCase(frame);
        if (stories && stories !== "N/A" && Number(stories) > 0) {
          const storiesNum = Number(stories);
          const storiesLabel = storiesNum % 1 === 0 ? `${storiesNum}-Story` : `${storiesNum.toFixed(1)}-Story`;
          tokens.push(`${storiesLabel} ${titled}`);
        } else {
          tokens.push(titled);
        }
      }
    }

    // 2. Foundation
    if (tokens.length < MAX_TOKENS) {
      const t = titleCase(props.foundation_type);
      if (t) tokens.push(t);
    }

    // 3. Exterior wall
    if (tokens.length < MAX_TOKENS) {
      const t = titleCase(props.ext_wall);
      if (t) tokens.push(t);
    }

    // 4. Roof — prefer roof_material (more specific) else roof_type.
    if (tokens.length < MAX_TOKENS) {
      const t = titleCase(props.roof_material) || titleCase(props.roof_type);
      if (t) tokens.push(t);
    }

    // 5. HVAC summary — collapse heating + ac into one token when they
    //    match, else show whichever is present.
    if (tokens.length < MAX_TOKENS) {
      const heat = String(props.heating_type || "").trim();
      const ac = String(props.ac_type || "").trim();
      const hasHeat = heat && heat !== "N/A";
      const hasAc = ac && ac !== "N/A";
      if (hasHeat && hasAc && heat.toUpperCase() === ac.toUpperCase()) {
        if (heat.toUpperCase().startsWith("CENTRAL")) {
          tokens.push("Central HVAC");
        } else {
          tokens.push(titleCase(heat));
        }
      } else if (hasHeat) {
        tokens.push(`${titleCase(heat)} Heat`);
      } else if (hasAc) {
        tokens.push(`${titleCase(ac)} AC`);
      }
    }

    meta2El.textContent = tokens.join(" · ");
  }

  // Line 3 (v3): amenities + quality + record metadata. Cap 6 tokens.
  // Order: spa → sauna → fireplaces → sprinkler → deck → CDU rating →
  // building class → effective yr built → % complete (when != 100).
  // Pool is on line 1 (most investor-relevant), so excluded here.
  const meta3El = document.getElementById("comps-block-target-card-meta3");
  if (meta3El) {
    const tokens = [];
    const MAX_TOKENS = 6;

    // Amenity flags (T/F)
    const amenityChecks = [
      { flag: props.spa_flag, label: "Spa" },
      { flag: props.sauna_flag, label: "Sauna" },
      { flag: props.fireplaces, label: "Fireplace", numeric: true },
      { flag: props.sprinkler_flag, label: "Sprinkler" },
      { flag: props.deck_flag, label: "Deck" },
    ];
    for (const a of amenityChecks) {
      if (tokens.length >= MAX_TOKENS) break;
      if (a.numeric) {
        const n = Number(a.flag);
        if (Number.isFinite(n) && n > 0) {
          tokens.push(n > 1 ? `${n} ${a.label}s` : a.label);
        }
      } else if (isTruthyFlag(a.flag)) {
        tokens.push(a.label);
      }
    }

    // CDU rating (DCAD condition rating: Excellent / Very Good / Good / etc.)
    if (tokens.length < MAX_TOKENS) {
      const cdu = titleCase(props.cdu_rating);
      if (cdu) tokens.push(cdu);
    }

    // Building class (DCAD numeric grade like "09" — not user-friendly
    // but visible per KK's "let the client decide what to take out").
    if (tokens.length < MAX_TOKENS) {
      const cls = String(props.bldg_class || "").trim();
      if (cls && cls !== "N/A") tokens.push(`Class ${cls}`);
    }

    // Effective year built (renovation/replacement year, often != yr_built)
    if (tokens.length < MAX_TOKENS) {
      const eff = String(props.eff_yr_built || "").trim();
      const yr = String(props.yr_built || "").trim();
      // Show eff_yr only when it differs from regular yr_built (otherwise redundant)
      if (eff && eff !== "N/A" && eff !== yr) {
        tokens.push(`Eff. ${eff}`);
      }
    }

    // % complete — show only when not 100% (incomplete construction)
    if (tokens.length < MAX_TOKENS) {
      const pct = String(props.pct_complete || "").trim();
      if (pct && pct !== "N/A" && pct !== "100" && pct !== "100.00") {
        tokens.push(`${pct}% complete`);
      }
    }

    meta3El.textContent = tokens.join(" · ");
  }
}

async function _refreshOriginatorTargetLabel(county, account) {
  const c = String(county || "").trim().toLowerCase();
  const a = String(account || "").trim();
  if (!c || !a) {
    _setOriginatorTargetLabel(null);
    _populateSubjectPropertyCard(null);
    return;
  }
  const props = await _resolveTargetParcelFeatureProps(c, a);
  // Race guard: if the user switched workspaces between fetch firing
  // and resolving, _currentTargetParcel has moved on; skip the stomp.
  if (!_sameParcelIdentity(_currentTargetParcel, { county: c, account: a })) return;

  if (!props) {
    // Fallback path: parcel not in analysis AND /api/parcel failed. Try
    // the saved-parcels cache for a plain address string so at least the
    // top slot + card address line still show something.
    const cached = (Array.isArray(_savedParcelsCache) ? _savedParcelsCache : []).find((p) =>
      String(p?.account_num || "").trim() === a
      && String(p?.county || "").trim().toLowerCase() === c,
    );
    const fallbackAddr = String(cached?.name || "").trim() || null;
    _setOriginatorTargetLabel(fallbackAddr);
    _populateSubjectPropertyCard(null);
    return;
  }

  const formattedAddr = _formatPropertyAddress(c, props.addr, props.city, props.owner_city);
  _setOriginatorTargetLabel(formattedAddr);
  _populateSubjectPropertyCard(props, c);
}

async function _ensureCurrentTargetParcelCoords() {
  const target = _currentTargetParcel;
  if (!target) return null;
  if (Number.isFinite(target.lat) && Number.isFinite(target.lng)) return target;
  if (_targetCoordsResolvePromise) return _targetCoordsResolvePromise;

  const county = String(target.county || "").trim().toLowerCase();
  const account = String(target.account || "").trim();
  if (!county || !account) return null;

  _targetCoordsResolvePromise = (async () => {
    try {
      const resp = await fetch(`/api/parcel/${encodeURIComponent(county)}/${encodeURIComponent(account)}`);
      if (!resp.ok) return null;
      const detail = await resp.json();
      const props = detail.properties || detail;
      const lat = Number(props.lat);
      const lng = Number(props.lng);
      if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;

      if (_sameParcelIdentity(_currentTargetParcel, { county, account })) {
        _currentTargetParcel.lat = lat;
        _currentTargetParcel.lng = lng;
        _refreshOpenTargetDistanceSurfaces();
        return _currentTargetParcel;
      }
      return null;
    } catch (err) {
      console.warn("[target-distance] coord resolve failed:", err);
      return null;
    } finally {
      _targetCoordsResolvePromise = null;
    }
  })();

  return _targetCoordsResolvePromise;
}

function _refreshOpenTargetDistanceSurfaces() {
  const popup = map?._popup;
  if (popup && map.hasLayer(popup)) {
    let popupParcelProps = _activeParcelPopupState?.props || null;
    const popupMeta = popup?._source?._lotLedgerPopupMeta;
    if (!popupParcelProps && popupMeta?.type === "parcel") {
      const popupAccount = String(popupMeta.accountNum || "").trim();
      if (popupAccount && Array.isArray(allAnalysisFeatures)) {
        const matched = allAnalysisFeatures.find(
          (f) => String(f?.properties?.account_num || "").trim() === popupAccount,
        );
        popupParcelProps = matched?.properties || null;
      }
    }
    if (popupParcelProps) {
      popup.setContent(makePopupHtml(popupParcelProps));
    }
  }

  const panel = document.getElementById("parcel-detail-panel");
  if (panel && panel.classList.contains("is-open") && _activeParcelPopupState?.props) {
    openParcelDetailPanel(_activeParcelPopupState.props, {
      latlng: _activeParcelPopupState.latlng || null,
      matchedComp: _activeParcelPopupState.matchedComp || null,
      geometry: _activeParcelPopupState.geometry || null,
      suppressFly: true,
    });
  }
}

function _haversineFeet(lat1, lng1, lat2, lng2) {
  const toRad = (deg) => (deg * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLng = toRad(lng2 - lng1);
  const a = Math.sin(dLat / 2) ** 2
    + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLng / 2) ** 2;
  const c = 2 * Math.asin(Math.min(1, Math.sqrt(a)));
  return 20902231.6 * c;
}

function _formatDistanceLabel(feet) {
  if (!Number.isFinite(feet)) return "N/A";
  const miles = feet / 5280;
  const roundedFeet = Math.round(feet).toLocaleString("en-US");
  if (feet < 500) {
    return `${Math.round(feet)} ft`;
  }
  if (feet < 5280) {
    return `${roundedFeet} ft (${miles.toFixed(2)} mi)`;
  }
  return `${miles.toFixed(2)} mi (${roundedFeet} ft)`;
}

function _updateMeasureModeUi() {
  const btn = document.getElementById("btn-measure-toggle");
  if (btn) btn.classList.toggle("active", _measureModeEnabled);
  const mapContainer = map?.getContainer?.();
  if (mapContainer) mapContainer.classList.toggle("measure-active", _measureModeEnabled);
}

function _clearMeasurement() {
  _measurePoints = [];
  measureLayer.clearLayers();
}

function _renderMeasurement() {
  measureLayer.clearLayers();
  if (!_measurePoints.length) return;

  _measurePoints.forEach((point) => {
    L.circleMarker(point, {
      radius: 5,
      color: "#0b5394",
      weight: 2,
      fillColor: "#e8f3ff",
      fillOpacity: 1,
      pane: "markerPane",
      interactive: false,
    }).addTo(measureLayer);
  });

  if (_measurePoints.length < 2) return;

  const [a, b] = _measurePoints;
  L.polyline([a, b], {
    color: "#0b5394",
    weight: 3,
    opacity: 0.95,
    dashArray: "6 6",
    interactive: false,
  }).addTo(measureLayer);

  const feet = _haversineFeet(a.lat, a.lng, b.lat, b.lng);
  const mid = L.latLng((a.lat + b.lat) / 2, (a.lng + b.lng) / 2);
  L.marker(mid, {
    interactive: false,
    icon: L.divIcon({
      className: "measure-distance-label",
      html: `<span>${_formatDistanceLabel(feet)}</span>`,
    }),
  }).addTo(measureLayer);
}

function _measurementPointFromInteraction(latlng, parcelProps) {
  const parcelLat = Number(parcelProps?.lat);
  const parcelLng = Number(parcelProps?.lng);
  if (Number.isFinite(parcelLat) && Number.isFinite(parcelLng)) {
    return L.latLng(parcelLat, parcelLng);
  }
  if (!latlng) return null;
  return Array.isArray(latlng) ? L.latLng(latlng[0], latlng[1]) : L.latLng(latlng);
}

function _handleMeasureInteraction(latlng, parcelProps = null) {
  if (!_measureModeEnabled) return false;
  const point = _measurementPointFromInteraction(latlng, parcelProps);
  if (!point) return true;
  if (_measurePoints.length >= 2) {
    _clearMeasurement();
  }
  _measurePoints.push(point);
  _renderMeasurement();
  return true;
}

function _setMeasureModeEnabled(enabled) {
  const next = Boolean(enabled);
  if (_measureModeEnabled === next) return;

  _measureModeEnabled = next;
  if (_measureModeEnabled) {
    const drawHandler = getPolygonDrawHandler();
    if (drawHandler && drawHandler.enabled()) drawHandler.disable();
    map.getContainer().classList.remove("drawing-active");
    drawHelper.classList.add("hidden");
    document.getElementById("btn-draw")?.classList.remove("active");
    document.getElementById("btn-draw-cancel")?.classList.add("hidden");
    closeParcelDetailPanel();
    map.closePopup();
    _clearMeasurement();
  } else {
    _clearMeasurement();
  }
  _updateMeasureModeUi();
}

function _buildDistanceToTargetMeta(p) {
  const target = _currentTargetParcel;
  if (!target) return "";

  const parcelCounty = String(p?.source_county || "").trim().toLowerCase();
  const parcelAccount = String(p?.account_num || "").trim();
  if (!parcelCounty || !parcelAccount) return "";

  if (_sameParcelIdentity(target, { county: parcelCounty, account: parcelAccount })) {
    return '<span class="parcel-target-distance">⭐ Target parcel</span>';
  }

  const parcelLat = Number(p?.lat);
  const parcelLng = Number(p?.lng);
  if (!Number.isFinite(parcelLat) || !Number.isFinite(parcelLng)) return "";

  const targetLat = Number(target.lat);
  const targetLng = Number(target.lng);
  if (!Number.isFinite(targetLat) || !Number.isFinite(targetLng)) {
    void _ensureCurrentTargetParcelCoords();
    return "";
  }

  return `<span class="parcel-target-distance">⭐ ${_propelioEscape(_formatDistanceLabel(_haversineFeet(targetLat, targetLng, parcelLat, parcelLng)))}</span>`;
}

function beginLatestAnalysisRequest() {
  if (_activeAnalysisAbortController) {
    _activeAnalysisAbortController.abort();
  }
  _activeAnalysisAbortController = new AbortController();
  _analysisRequestSeq += 1;
  _activeAnalysisRequestId = _analysisRequestSeq;
  return {
    requestId: _activeAnalysisRequestId,
    signal: _activeAnalysisAbortController.signal,
  };
}

function isActiveAnalysisRequest(requestId) {
  return requestId === _activeAnalysisRequestId;
}

function isAbortError(err) {
  return err?.name === "AbortError" || err?.code === "ANALYSIS_ABORTED";
}

function getAnalysisErrorMessage(err, fallback = "Analysis failed. Please try again.") {
  if (err?.userMessage && String(err.userMessage).trim()) return err.userMessage;
  if (err?.message && String(err.message).trim()) return err.message;
  return fallback;
}

function captureFilterState() {
  // Always serializes the live UI as an ARV-flat blob regardless of _activeView.
  // Correct for diff baselines on any view. Whole-blob POST paths
  // (saveCurrentArea, saveCurrentSession) only run while _activeView === 'arv'
  // (draw severs the bond on drawstart; session-save has no view context).
  // Do NOT use for non-ARV whole-blob writes — see spec §5.7.
  //
  // AI MODE FIX (docs/AI/CODER_SPEC_AIMODE_FIX_2026-07-14.md §0/§2.2) — THE
  // INVARIANT: with AI mode ON, this function returns BYTE-IDENTICAL output
  // to the same UI with AI mode OFF. This is the ONLY place that holds. No
  // caller-selectable fork, no `raw` parameter — every caller gets user
  // truth, unconditionally, because "which caller needs to see the screen?"
  // is NONE. See the substitution block below.
  const state = {
    v: 1,
    checkboxes: { ...filterState },
    numeric: { ...numericFilters },
    sold: {
      maxDaysAgo: soldCompsFilter.maxDaysAgo,
      minPrice: soldCompsFilter.minPrice,
      maxPrice: soldCompsFilter.maxPrice,
      minYearBuilt: soldCompsFilter.minYearBuilt,
      maxYearBuilt: soldCompsFilter.maxYearBuilt,
    },
    comp: { ...compNumericFilters },
    propelio: { ...propelioFilterState, sortMode: propelioCompSortMode },
  };
  // AI mode writes exactly six propelio fields into the live DOM so the owner
  // SEES the picked bands (§2.1) — but the persistence layer must never see
  // them. Substitute those six from _aiModeUserSnapshot[_activeView], the
  // canonical real-state object (kept fresh by the Hole-C SSE stash in
  // _writeFilterFieldDirect) — never re-derived from the DOM, never a second
  // source of truth to drift from this one.
  if (_aiModeOn && (_activeView === "arv" || _activeView === "nbv")) {
    const real = _aiModeUserSnapshot[_activeView];
    if (real && real.propelio) {
      // Iterate _AI_OVERLAY_FIELDS rather than listing the six names again. A
      // second hardcoded copy of this list is exactly how the fifth hole got in:
      // two places deciding "which fields are AI's", free to drift apart.
      state.propelio = { ...state.propelio };
      for (const k of _AI_OVERLAY_FIELDS) state.propelio[k] = real.propelio[k];
    }
  }
  return state;
}

// Normalizes a flat ARV-form capture for equality comparison. Operates on
// the same shape as captureFilterState() output (always flat). _views keys
// in a raw server blob are ignored — comparisons never cross view boundaries.
function _normalizeFilterStateForCompare(state) {
  if (!state || typeof state !== "object") {
    return { v: 1, checkboxes: {}, numeric: {}, sold: {}, comp: {} };
  }
  const sold = state.sold && typeof state.sold === "object" ? state.sold : {};
  return {
    v: 1,
    checkboxes: { ...(state.checkboxes || {}) },
    numeric: { ...(state.numeric || {}) },
    sold: {
      maxDaysAgo: sold.maxDaysAgo ?? DEFAULT_SOLD_COMPS_FILTER.maxDaysAgo,
      minPrice: sold.minPrice ?? null,
      maxPrice: sold.maxPrice ?? null,
      minYearBuilt: sold.minYearBuilt ?? null,
      maxYearBuilt: sold.maxYearBuilt ?? null,
    },
    comp: { ...(state.comp || {}) },
    propelio: { ...(state.propelio || {}) },
  };
}

// Restore Propelio filter state into UI inputs from a persisted state blob.
// Called during saved-area load. Pure DOM writes — no API calls.
function applyPropelioFilterStateToUI(persisted) {
  if (!persisted || typeof persisted !== "object") return;
  const setVal = (id, v) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = v == null ? "" : String(v);
  };
  const setChk = (id, v) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.checked = Boolean(v);
  };
  const merged = { ...DEFAULT_PROPELIO_FILTERS, ...persisted };
  setVal("prop-months", merged.months);
  setVal("prop-range", merged.range);
  _setPropStatusFilter("sold", Boolean(merged.statusSold), { apply: false });
  _setPropStatusFilter("active", Boolean(merged.statusActive), { apply: false });
  _setPropStatusFilter("pending", Boolean(merged.statusPending), { apply: false });
  setChk("prop-outside-area", merged.showOutsideArea);
  setVal("prop-sold-within", merged.soldWithinDays);
  setVal("prop-lot-min", merged.lotMin);
  setVal("prop-lot-max", merged.lotMax);
  setVal("prop-sqft-min", merged.sqftMin);
  setVal("prop-sqft-max", merged.sqftMax);
  setVal("prop-year-min", merged.yearMin);
  setVal("prop-year-max", merged.yearMax);
  setVal("prop-price-min", merged.priceMin);
  setVal("prop-price-max", merged.priceMax);
  // Comp-list sort mode lives outside propelioFilterState but is part of
  // the workspace's visible state, so it persists alongside.
  if (typeof persisted.sortMode === "string" && persisted.sortMode) {
    propelioCompSortMode = persisted.sortMode;
    const sortEl = document.getElementById("propelio-comp-sort");
    if (sortEl) sortEl.value = propelioCompSortMode;
  }
  // Neighborhood hidden input (chip render wired in Chunk 3)
  const _nbhdEl = document.getElementById("prop-neighborhood");
  if (_nbhdEl) _nbhdEl.value = merged.neighborhood ?? "";
  _renderNbhdChip(merged.neighborhood || null);
  propelioFilterState = readPropelioFiltersFromUI();
}

function _filterStatesEqual(a, b) {
  const left = _normalizeFilterStateForCompare(a);
  const right = _normalizeFilterStateForCompare(b);
  return JSON.stringify(left) === JSON.stringify(right);
}

// ─── Sprint 2 multi-user collab: per-field PATCH helpers (spec §5) ────

// Coerce empty-string / undefined to null for diff comparison.
function _coerceForDiff(v) {
  if (v === "" || v === undefined) return null;
  return v;
}

// Diff captureFilterState() output against _filterSaveLastSnapshot.
// Returns Array<[field_key, value]> for each (section.key) pair whose
// value differs. Spec §5.2 edge-case semantics:
//   null vs null         -> skip
//   null vs <value>      -> emit
//   <value> vs null      -> emit (value=null)
//   <value> vs same      -> skip
//   <value> vs different -> emit (value=new)
//   null vs ""           -> skip (coerced)
//   missing in current   -> skip (never emit absent keys)
//   missing in snapshot  -> emit if non-null
//   checkboxes.active    -> always skip (legacy force-clear churn loop)
function _diffFilterState(current, lastSnapshot) {
  if (!current || typeof current !== "object") return [];
  const out = [];
  const sections = ["checkboxes", "numeric", "sold", "comp", "propelio"];
  const snap = lastSnapshot && typeof lastSnapshot === "object" ? lastSnapshot : {};
  // 🔴 CRITICAL #1: emit view-prefixed field keys on non-ARV views so the
  // per-field PATCH writes to _views.<view>.* rows, never the flat ARV rows.
  // Flag-gated: when off, _viewPrefix is "" and behavior is byte-for-byte today's.
  //
  // AI MODE FIX (docs/AI/CODER_SPEC_AIMODE_FIX_2026-07-14.md §2.3) — there is
  // no AI-mode branch here anymore. captureFilterState() already returns
  // user truth while AI mode is on (it substitutes the six AI-written
  // fields before this function ever sees them), so a normal edit during AI
  // mode diffs and routes to the user's REAL keys exactly as it would with
  // AI mode off — which is what "structurally unreachable" was supposed to
  // mean, and what the previous AI-only view namespace only half-built (it
  // required every OTHER caller to know to route around it; this deletes
  // the need to know at all).
  const _viewPrefix = (ARV_NBV_EXPORT_ENABLED && _activeView !== "arv")
    ? `_views.${_activeView}.`
    : "";
  for (const section of sections) {
    const currSection = (current[section] && typeof current[section] === "object") ? current[section] : {};
    const snapSection = (snap[section] && typeof snap[section] === "object") ? snap[section] : {};
    for (const key of Object.keys(currSection)) {
      const fieldKey = `${_viewPrefix}${section}.${key}`;
      // §5.2 special case: skip checkboxes.active and its view-prefixed variant
      // (force-cleared on restore — would create a save-restore churn loop).
      if (fieldKey === "checkboxes.active" || fieldKey.endsWith(".checkboxes.active")) continue;
      const a = _coerceForDiff(currSection[key]);
      const b = _coerceForDiff(snapSection[key]);
      if (a === b) continue;
      // Convert empty-string back to null for the wire (JSONB null)
      out.push([fieldKey, currSection[key] === "" ? null : currSection[key]]);
    }
  }
  return out;
}

// Map dotted field_key to its DOM input element for the §5.5 anti-flicker
// hook. Only sustained-focus inputs (numeric/text) return an element;
// checkboxes return null (click is momentary, no flicker hazard).
function _resolveFieldInput(fieldKey) {
  // Strip _views.<view>. prefix so section routing works for all views.
  const _fk = (ARV_NBV_EXPORT_ENABLED && fieldKey.startsWith("_views."))
    ? fieldKey.split(".").slice(2).join(".")
    : fieldKey;
  const [section, key] = _fk.split(".");
  if (section === "numeric") {
    const map = {
      lot_sqft_min: "nf-lot-min", lot_sqft_max: "nf-lot-max",
      appr_val_min: "nf-val-min", appr_val_max: "nf-val-max",
      yr_built_min: "nf-yr-min",  yr_built_max: "nf-yr-max",
      sqft_min:     "nf-sqft-min", sqft_max:    "nf-sqft-max",
    };
    return map[key] ? document.getElementById(map[key]) : null;
  }
  if (section === "comp") {
    const map = {
      lot_sqft_min: "nf-comp-lot-min", lot_sqft_max: "nf-comp-lot-max",
      appr_val_min: "nf-comp-val-min", appr_val_max: "nf-comp-val-max",
      yr_built_min: "nf-comp-yr-min",  yr_built_max: "nf-comp-yr-max",
      sqft_min:     "nf-comp-sqft-min", sqft_max:    "nf-comp-sqft-max",
    };
    return map[key] ? document.getElementById(map[key]) : null;
  }
  if (section === "sold") {
    const map = {
      maxDaysAgo:   "sold-days-max",
      minPrice:     "sold-price-min", maxPrice:     "sold-price-max",
      minYearBuilt: "sold-yr-min",    maxYearBuilt: "sold-yr-max",
    };
    return map[key] ? document.getElementById(map[key]) : null;
  }
  if (section === "propelio") {
    const map = {
      months:         "prop-months",  range:          "prop-range",
      soldWithinDays: "prop-sold-within",
      lotMin:  "prop-lot-min",  lotMax:  "prop-lot-max",
      sqftMin: "prop-sqft-min", sqftMax: "prop-sqft-max",
      yearMin: "prop-year-min", yearMax: "prop-year-max",
      priceMin: "prop-price-min", priceMax: "prop-price-max",
    };
    return map[key] ? document.getElementById(map[key]) : null;
  }
  // checkboxes + propelio status toggles + sortMode: click-based, no anti-flicker.
  return null;
}

// Direct UI write — bypasses the queue (used by stale-write reconciliation
// after a PATCH loses to a concurrent write). Writes the value into the
// in-memory state object AND syncs the DOM input element.
function _writeFilterFieldDirect(fieldKey, value) {
  // When the flag is on, _views.<view>.<section>.<key> keys are routed to a
  // specific view. Only apply to the live UI if it matches the active view.
  let _effectiveKey = fieldKey;
  let _keyView = "arv";   // flat keys ARE ARV (§2.3 asymmetry) -- the one Hole C names
  if (ARV_NBV_EXPORT_ENABLED && fieldKey.startsWith("_views.")) {
    const _parts = fieldKey.split(".");
    if (_parts[1] !== _activeView) return; // Not the active view — skip UI write.
    _keyView = _parts[1];
    _effectiveKey = _parts.slice(2).join(".");
  }
  // §2.5 Hole C: an external write (SSE echo of a co-viewer's edit, or a
  // stale-write reconciliation) targeting the view AI mode is CURRENTLY
  // substituting must not scribble over the owner's live comparison.
  // Generalized past the spec's literal "flat keys" framing to also cover
  // NBV-while-active — the existing _views. guard above only checks "is
  // this the active view," which for NBV-while-viewing-NBV still lets a
  // co-viewer's real edit straight through to the DOM; same hole, same
  // shape, same fix (flagged, not silently narrowed to match the letter of
  // the spec text). It's still the user's real, current truth, so stash it
  // into the AI-mode snapshot (read back on drop-out/off) instead of the
  // live DOM -- never dropped, never applied to what's on screen.
  // ⛔ FIFTH HOLE (found by the coder, 2026-07-14) -- SCOPED TO THE OVERLAY FIELDS.
  // This guard used to check only WHICH VIEW the write targeted, never WHICH FIELD,
  // so it diverted EVERY incoming field into the snapshot while AI mode was
  // overlaying only six of them. Same root cause as the four it was written to fix:
  // NARROW LENS, WIDE MACHINERY.
  //
  // Under the overlay model the DOM IS the user's truth for every field AI is not
  // displaying -- so a co-viewer's edit to a Vacant checkbox BELONGS on screen.
  // Diverting it made the owner's screen silently stale AND lost the edit on exit
  // (_disableAiMode only ever restores real.propelio back to the DOM).
  //
  // So: divert ONLY the six fields AI is actually painting. For those, stash into the
  // snapshot (the co-viewer's edit is never lost) and leave the DOM alone (the owner's
  // comparison is never scribbled over). Everything else writes through, normally.
  if (_aiModeOn && (_keyView === "arv" || _keyView === "nbv") && _keyView === _activeView) {
    const [section, key] = _effectiveKey.split(".");
    if (section === "propelio" && _AI_OVERLAY_FIELDS.has(key)) {
      const real = _aiModeUserSnapshot[_keyView];
      if (real && real[section] && typeof real[section] === "object") {
        real[section][key] = value;
      }
      return;
    }
    // Not an overlay field -> fall through and write the DOM like any other edit.
  }
  const [section, key] = _effectiveKey.split(".");
  if (section === "checkboxes") {
    filterState[key] = Boolean(value);
    syncFilterInputs();
  } else if (section === "numeric") {
    numericFilters[key] = value;
    _hydrateNumericInputsFromState();
  } else if (section === "comp") {
    compNumericFilters[key] = value;
    _hydrateCompNumericInputsFromState();
  } else if (section === "sold") {
    soldCompsFilter[key] = value;
    _hydrateSoldCompInputsFromState();
  } else if (section === "propelio") {
    if (key === "sortMode") {
      propelioCompSortMode = String(value || "");
      const sortEl = document.getElementById("propelio-comp-sort");
      if (sortEl) sortEl.value = propelioCompSortMode;
    } else {
      propelioFilterState[key] = value;
      applyPropelioFilterStateToUI({ ...propelioFilterState, sortMode: propelioCompSortMode });
    }
  }
  // After in-memory state updated, re-render dependent layers if a real area loaded.
  if (lastAnalysisGeojson) {
    try { applyAndRenderSoldFilters(); } catch (_) {}
    try { applyMapVisibilityFilters(); } catch (_) {}
    try { _rebuildOutreachOverlays(); } catch (_) {}
    // Mirror the popup-edit + CSV-import fix: keep the sidebar count
    // badges fresh when filter state arrives via SSE from another tab
    // or user.
    try { _updateMergedSidebarCounts(); } catch (_) {}
  }
  // Sprint 3 hotfix (2026-06-02): propelio comp layer + comp list also
  // need to re-render when ANY filter changes remotely. parcelType*
  // mirrors propagate via the propelio object; sold/comp filters affect
  // compPassesPropelioFilters gating. applyPropelioClientFilters is a
  // safe no-op if no comps have been searched yet.
  try { applyPropelioClientFilters(); } catch (_) {}
}

// Apply a server-reconciled value to the UI. Anti-flicker rule (§5.5):
// if the user has focus on the corresponding input and is actively
// typing, defer the apply until blur.
function _applyFilterFieldToUI(fieldKey, value) {
  // Strip _views.<view>. prefix; only apply to the visible UI when it's the active view.
  let _effectiveKey = fieldKey;
  if (ARV_NBV_EXPORT_ENABLED && fieldKey.startsWith("_views.")) {
    const _parts = fieldKey.split(".");
    if (_parts[1] !== _activeView) return; // Not the active view — skip.
    _effectiveKey = _parts.slice(2).join(".");
  }
  if (_effectiveKey === "propelio.neighborhood") {
    propelioFilterState.neighborhood = value ?? null;
    const _nbhdEl = document.getElementById("prop-neighborhood");
    if (_nbhdEl) _nbhdEl.value = value ?? "";
    _renderNbhdChip(value || null);
    try { applyPropelioClientFilters(); } catch (_) {}
    return;
  }
  const inputEl = _resolveFieldInput(_effectiveKey);
  if (inputEl && document.activeElement === inputEl) {
    // Defer apply until blur (Figma's anti-flicker pattern).
    inputEl.dataset.pendingReconcile = JSON.stringify(value);
    if (!inputEl._reconcileBlurHook) {
      inputEl._reconcileBlurHook = () => {
        const stashed = inputEl.dataset.pendingReconcile;
        delete inputEl.dataset.pendingReconcile;
        inputEl.removeEventListener("blur", inputEl._reconcileBlurHook);
        inputEl._reconcileBlurHook = null;
        // Pass original fieldKey so _writeFilterFieldDirect can strip prefix if needed.
        if (stashed != null) _writeFilterFieldDirect(fieldKey, JSON.parse(stashed));
      };
      inputEl.addEventListener("blur", inputEl._reconcileBlurHook, { once: true });
    }
    return;
  }
  _writeFilterFieldDirect(fieldKey, value);
}

// ─── Sprint 3 multi-user collab: SSE EventSource lifecycle (spec §4.4) ───

function _openSseStream(areaId) {
  // Already open for this area? no-op.
  if (_sseEventSource && _sseAreaId === areaId) return;
  _closeSseStream();
  if (!areaId) return;
  _sseAreaId = areaId;
  const es = new EventSource(
    `/api/areas/${encodeURIComponent(areaId)}/events`,
    { withCredentials: true }
  );
  _sseEventSource = es;
  // KK debug 2026-06-06 watchdog (paired with area_meta_change broadcast):
  // track the most recent SSE message timestamp so the 60s health probe
  // (declared below the listeners) can detect a zombie EventSource and
  // force-reconnect. EventSource.readyState only flips to CLOSED on a
  // non-200 response; transport blips + Edge tab throttling can leave the
  // stream "open" but silently dead. Backend sse-starlette pings every
  // 30s as SSE comments (`: ping\n\n`) which the EventSource API hides
  // from JavaScript, so we can't use pings — only real events bump this.
  _sseLastMessageAt = Date.now();
  es.addEventListener("connected", (e) => {
    _sseLastMessageAt = Date.now();
    try {
      const data = JSON.parse(e.data);
      console.debug("[sse] connected", data);
    } catch (_) {}
  });
  // Heartbeat (2026-06-16): the backend now emits a real `heartbeat` event every
  // ~30s (api/main.py SSE generator). The watchdog only treats *real* events as
  // liveness (ping comments are hidden from JS), so before this, a healthy-but-
  // quiet idle stream looked "dead" and the watchdog force-reconnected every
  // ~120s — triggering a resync + refetch storm. Counting the heartbeat as
  // liveness stops that misfire while still letting the watchdog catch a stream
  // that has genuinely gone silent (no heartbeat for >90s).
  es.addEventListener("heartbeat", () => {
    _sseLastMessageAt = Date.now();
  });
  es.addEventListener("message", (e) => {
    _sseLastMessageAt = Date.now();
    let msg;
    try { msg = JSON.parse(e.data); } catch (_) { return; }
    _handleSseFieldChange(msg);
  });
  es.addEventListener("resync", () => {
    _sseLastMessageAt = Date.now();
    console.debug("[sse] resync event — refetching area state");
    _sseRefetchArea(areaId);
  });
  es.addEventListener("blob_explode", () => {
    _sseLastMessageAt = Date.now();
    console.debug("[sse] blob_explode event — refetching area state");
    _sseRefetchArea(areaId);
  });
  // Sprint 3 hotfix (2026-06-02): stored value live sync.
  es.addEventListener("stored_value", (e) => {
    _sseLastMessageAt = Date.now();
    let msg;
    try { msg = JSON.parse(e.data); } catch (_) { return; }
    _handleSseStoredValue(msg);
  });
  // Mike report 2026-06-06: "stars don't show up when my assistants are
  // adding areas with saved parcels until I view the area with a saved
  // parcel." The backend now fires this event when create_saved_parcel
  // bonds a parcel to a shared area (or delete_saved_parcel removes one).
  // Refetch the saved-resources cache so the gold star renders immediately
  // for every other connected member of this area.
  es.addEventListener("saved_parcel_change", () => {
    _sseLastMessageAt = Date.now();
    // No self-echo gate. Copilot deep dive 2026-06-06: the gate I added
    // at commit 99be884 was over-engineered for a speculative race that
    // doesn't actually exist (`_renderSubjectPropertyOutlineLazy` has
    // an `_subjectPropertyGeometryInFlight` early-return that prevents
    // the duplicate fetch). What the gate actually did was kill the
    // FAST path for the writer's own tab: the SSE self-echo arrives
    // ~100ms after the backend commits, well before the local awaited
    // PUT chain returns. Suppressing it meant the writer had to wait
    // the full HTTP round trip + post-PUT reload before the new gold
    // star appeared. The OLD pre-99be884 behavior was instant precisely
    // because the SSE self-echo was the fast path.
    console.debug("[sse] saved_parcel_change — refreshing saved resources");
    _reloadSavedResources().catch((err) =>
      console.warn("[sse] _reloadSavedResources after saved_parcel_change failed:", err)
    );
  });
  // KK debug 2026-06-06: PUT /api/areas/{id} now fires this for name +
  // originator changes (was previously silent — only blob_explode fired
  // when filter_state was in the body). User B's subject-property and
  // area-name display refresh live instead of waiting for tab refocus.
  es.addEventListener("area_meta_change", () => {
    _sseLastMessageAt = Date.now();
    // Same reasoning as saved_parcel_change above — gate removed.
    console.debug("[sse] area_meta_change — refreshing saved resources");
    _reloadSavedResources().catch((err) =>
      console.warn("[sse] _reloadSavedResources after area_meta_change failed:", err)
    );
  });
  es.addEventListener("error", () => {
    // EventSource enters CLOSED only on non-200 response. Transport
    // blips DON'T close — native reconnect handles those. If CLOSED,
    // probe auth and route through _handle401 on 401.
    if (es.readyState === EventSource.CLOSED) {
      _sseProbeAuthAndMaybeReconnect(areaId);
    }
  });
}

function _closeSseStream() {
  if (_sseEventSource) {
    try { _sseEventSource.close(); } catch (_) {}
    _sseEventSource = null;
  }
  _sseAreaId = null;
}

// KK debug 2026-06-06 watchdog: detect zombie EventSource that thinks it's
// open but isn't delivering events (Edge tab throttling, dropped TCP that
// the browser didn't notice, etc.). Backend sends ping every 30s as an SSE
// comment which the API hides from JS — so we only see "real" events. A
// healthy stream WILL see at least one real message per minute under any
// normal usage. We're generous: only force-reconnect after 90s of silence.
function _sseWatchdogTick() {
  if (!_sseEventSource) return;
  if (!_sseAreaId) return;
  // Don't fire while tab is hidden — browser may have legitimately frozen
  // the timer + EventSource together; tab-focus visibilitychange handler
  // will trigger _reloadSavedResources anyway.
  if (document.visibilityState !== "visible") return;
  const silenceMs = Date.now() - _sseLastMessageAt;
  if (silenceMs <= 90_000) return;
  // Zombie. Force-reconnect.
  const areaId = _sseAreaId;
  console.warn(
    `[sse] watchdog: no events for ${Math.round(silenceMs / 1000)}s; force-reconnect`
  );
  _closeSseStream();
  _openSseStream(areaId);
}

// Single global interval — starts when the module loads, ticks every 30s,
// is a no-op when no stream is open or tab is hidden. Cheap, idempotent.
if (typeof window !== "undefined" && !_sseWatchdogInterval) {
  _sseWatchdogInterval = setInterval(_sseWatchdogTick, 30_000);
}

function _handleSseFieldChange(msg) {
  if (!msg) return;
  // Type-tagged messages (resync / blob_explode / saved_parcel_change /
  // area_meta_change) are handled by their dedicated addEventListener
  // bindings; this catches only field-change deltas.
  if (
    msg.type === "resync"
    || msg.type === "blob_explode"
    || msg.type === "saved_parcel_change"
    || msg.type === "area_meta_change"
  ) return;
  const { field_key, value, client_seq, by_session_id } = msg;
  if (!field_key) return;
  // Sprint 3 hotfix (2026-06-02 evening): seq-based self-echo + LWW filter.
  // The earlier session-UUID approach had a subtle bug in concurrent
  // same-field edits: the WINNER (higher seq) would receive the LOSER's
  // NOTIFY first via SSE commit-order delivery, apply it to the UI,
  // then ignore their own NOTIFY as self-echo — leaving the UI showing
  // the loser's value while the DB has the winner's.
  //
  // Fix: compare incoming client_seq against _dispatchedSeqByField
  // (which we already track post-PATCH-dispatch). If incoming seq <=
  // applied seq, the event is either (a) our own write echoed back, or
  // (b) an older write that lost the LWW race. Either way, ignore.
  // If incoming seq > applied seq, it's a newer change we haven't seen —
  // apply + update map. Order-correct under all conflict scenarios.
  const incomingSeq = Number(client_seq || 0);
  const appliedSeq = Number(_dispatchedSeqByField.get(field_key) || 0);
  const shouldApply = incomingSeq > appliedSeq;
  console.debug("[sse] event received", {
    field_key, value, client_seq: incomingSeq,
    applied_seq: appliedSeq, by_session_id,
    my_session: _sseSessionUuid,
    action: shouldApply ? "APPLY" : "IGNORED (stale or self)",
  });
  if (!shouldApply) return;
  // ─── Chunk D: active-view gate (Feature #3) ───
  // Any filter change for a view that ISN'T the active one must update that
  // view's in-memory cache + seq bookkeeping ONLY — never the live UI / re-render.
  // FLAT (unprefixed) keys belong to the ARV view; _views.<view>.* keys belong to
  // that view. Without gating the FLAT case too, an ARV echo bleeds into the
  // visible NBV/Export view while hammering switches (KK 2026-06-29).
  if (ARV_NBV_EXPORT_ENABLED) {
    const _isViewKey = field_key.startsWith("_views.");
    const _parts = field_key.split(".");
    const _eventView = _isViewKey ? _parts[1] : "arv";
    if (_eventView !== _activeView) {
      // Update the background view's cache (best-effort) without touching UI.
      const _vc = _viewFilterCache[_eventView];
      const _section = _isViewKey ? _parts[2] : _parts[0];
      const _key = _isViewKey ? _parts[3] : _parts[1];
      if (_vc && _vc.v && _vc[_section] && typeof _vc[_section] === "object") {
        _vc[_section][_key] = value;
      }
      _dispatchedSeqByField.set(field_key, incomingSeq);  // LWW bookkeeping
      return;  // no UI write, no re-render
    }
  }
  // Apply via Sprint 2 anti-flicker hook (defers to blur if user is
  // focused on the input).
  _applyFilterFieldToUI(field_key, value);
  // Update applied-seq map so any subsequent older NOTIFY (or our own
  // self-echo) is correctly ignored.
  _dispatchedSeqByField.set(field_key, incomingSeq);
  // Refresh diff baseline so the next captureFilterState() comparison
  // doesn't treat this remote change as a local edit pending PATCH.
  _filterSaveLastSnapshot = captureFilterState();
  // Sprint 3 polish (KK ask 2026-06-02): reuse the existing save-status
  // chip to flash "Synced ✓" so the user knows a remote change just
  // landed. Same animation as "Saved ✓"; different label distinguishes
  // outgoing vs incoming.
  try { _filterSaveSetStatus("flash", "Synced ✓"); } catch (_) {}
}

// Sprint 3 hotfix: apply incoming stored-value SSE event.
// Mirrors the filter-field handler shape — self-echo filter on
// by_session_id, apply via existing _storedValueState + recalc-and-render.
function _handleSseStoredValue(msg) {
  if (!msg) return;
  const { field_key, numeric_value, comment_text, client_seq, by_session_id } = msg;
  if (!field_key) return;
  if (!_storedValueState) return;
  const fieldData = _storedValueState[field_key];
  if (!fieldData) return;
  // Sprint 3 hotfix (2026-06-02 evening): seq-based filter, not session-UUID.
  // Stored values already track fieldData.client_seq as the highest applied
  // seq (set on GET response, PUT response, and 409 reconciliation). Use
  // that as the LWW gate: only apply incoming if its seq > local applied.
  const incomingSeq = Number(client_seq || 0);
  const appliedSeq = Number(fieldData.client_seq || 0);
  const shouldApply = incomingSeq > appliedSeq;
  console.debug("[sse] stored_value event received", {
    field_key, numeric_value, comment_text,
    client_seq: incomingSeq, applied_seq: appliedSeq, by_session_id,
    my_session: _sseSessionUuid,
    action: shouldApply ? "APPLY" : "IGNORED (stale or self)",
  });
  if (!shouldApply) return;
  fieldData.numeric_value = (numeric_value !== undefined && numeric_value !== null) ? Number(numeric_value) : null;
  fieldData.comment_text = String(comment_text || "");
  fieldData.client_seq = incomingSeq;
  _storedValueClientSeq = Math.max(_storedValueClientSeq, incomingSeq);
  try { _storedValueRecalcAndRender(); } catch (_) {}
  // Sprint 3 polish (KK ask 2026-06-02): flash "Synced ✓" on the
  // stored-value status chip so the user knows a remote change landed.
  try { _storedValueSetStatus("flash", "Synced ✓"); } catch (_) {}
}

function _sseRefetchArea(areaId) {
  // resync / blob_explode events fall through to a full area refetch.
  _reloadSavedResources().catch((err) =>
    console.warn("[sse] refetch failed", err)
  );
}

async function _sseProbeAuthAndMaybeReconnect(areaId) {
  try {
    const resp = await fetch("/api/auth/me", { credentials: "same-origin" });
    if (resp.status === 401) {
      // Session expired mid-stream — route through existing 401 path.
      _closeSseStream();
      if (typeof _handle401 === "function") _handle401();
      return;
    }
    if (resp.ok) {
      // Session still valid — likely transport blip or Cloud Run
      // timeout. EventSource gave up; manually reconnect.
      console.debug("[sse] auth ok, reconnecting EventSource");
      _openSseStream(areaId);
      return;
    }
  } catch (err) {
    console.warn("[sse] auth probe failed", err);
  }
}

function setActiveItem(type, name) {
  const typeEl = document.getElementById("active-item-type");
  const nameEl = document.getElementById("active-item-name");
  if (!typeEl || !nameEl) return;
  typeEl.textContent = type || "Workspace";
  nameEl.textContent = name || "—";
  _updateActiveItemRenameVisibility();
}

function clearActiveItem() {
  _selectedSavedItemId = null;
  renderSavedAreasList();
  const typeEl = document.getElementById("active-item-type");
  const nameEl = document.getElementById("active-item-name");
  if (typeEl) typeEl.textContent = "Workspace";
  if (nameEl) nameEl.textContent = "—";
  _setOriginatorTargetLabel(null);
  _updateActiveItemRenameVisibility();
}

// Pencil shows whenever there's a real workspace/area context loaded.
// 2026-05-21 hotfix: previously gated solely on _currentLoadedAreaId.
// Multiple side-effect paths reset that variable to null transiently
// (between session restore, area reload, snapshot bounce, etc.), leaving
// the pencil hidden after a rename even though the workspace name is
// still real and renameable. Now we ALSO accept the case where the
// displayed name is non-placeholder ("—") — if the user sees a workspace
// name on screen, the rename pencil belongs next to it. Click handler
// still gates the actual API call on _currentLoadedAreaId at the moment
// of rename so we never attempt to rename a transient state.
function _updateActiveItemRenameVisibility() {
  const btn = document.getElementById("active-item-rename");
  if (!btn) return;
  const nameEl = document.getElementById("active-item-name");
  const visibleName = (nameEl?.textContent || "").trim();
  const hasRealName = Boolean(visibleName) && visibleName !== "—";
  // Sprint 1 multi-user collab (spec §3.3): rename is owner-only on a
  // loaded shared area. If no loaded area, allow (covers the transient
  // pre-load state). Editors hit a hidden pencil + 403 server-side.
  const loadedArea = _currentLoadedAreaId
    ? _savedAreasCache.find((a) => String(a.id) === String(_currentLoadedAreaId))
    : null;
  const isOwnerOfLoaded = loadedArea ? (loadedArea.role || "owner") === "owner" : true;
  // Fix B (2026-07-12): app-role admins (owner + developer) may rename any area.
  const canRenameLoaded = isOwnerOfLoaded || _isAdmin();
  const shouldShow = (Boolean(_currentLoadedAreaId) || hasRealName) && canRenameLoaded;
  btn.classList.toggle("hidden", !shouldShow);
  // Share button: shown whenever the loaded area is shareable (has a share_id).
  // NOT owner-only — anyone can copy a share link (mirrors the saved-areas-list
  // 🔗 button). Rides the same visibility triggers as the rename pencil.
  const shareBtn = document.getElementById("active-item-share");
  if (shareBtn) {
    const shareId = loadedArea ? String(loadedArea.share_id || "").trim() : "";
    shareBtn.classList.toggle("hidden", !(Boolean(_currentLoadedAreaId) && shareId));
  }
}

(function _initActiveItemRenamePencil() {
  const btn = document.getElementById("active-item-rename");
  if (!btn) return;
  btn.addEventListener("click", (e) => {
    e.preventDefault();
    void _handleActiveItemRenameClick();
  });
})();

// Share button next to the rename pencil — copies the loaded area's share link,
// reusing the exact copy logic from the saved-areas-list 🔗 handler (no new
// backend, no new share logic). Isolated handler: reads _savedAreasCache, writes
// to clipboard, toasts — no autosave/restore side effects.
(function _initActiveItemShareButton() {
  const btn = document.getElementById("active-item-share");
  if (!btn) return;
  btn.addEventListener("click", async (e) => {
    e.preventDefault();
    if (!_currentLoadedAreaId) return;
    const loadedArea = _savedAreasCache.find((a) => String(a.id) === String(_currentLoadedAreaId));
    const shareId = loadedArea ? String(loadedArea.share_id || "").trim() : "";
    if (!shareId) return;
    const url = `${window.location.origin}/?area=${shareId}`;
    try {
      if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
        throw new Error("Clipboard API unavailable");
      }
      await navigator.clipboard.writeText(url);
      _showToast("Link copied");
    } catch {
      _showToast("Copy failed - try again", "error");
    }
  });
})();

async function _handleActiveItemRenameClick() {
  if (!_currentLoadedAreaId) return;
  const nameEl = document.getElementById("active-item-name");
  const btn = document.getElementById("active-item-rename");
  if (!nameEl || !btn) return;
  if (nameEl.classList.contains("is-editing")) return;

  // The pencil + 🔗 live inside .active-item-name-actions (index.html:52). Resolve the
  // container once; fall back to the button so a future markup change degrades, not crashes.
  const actionsEl = btn.closest(".active-item-name-actions") || btn;

  const currentName = (nameEl.textContent || "").trim();
  const input = document.createElement("input");
  input.type = "text";
  input.className = "active-item-name-input";
  input.value = currentName === "—" ? "" : currentName;
  input.maxLength = 120;

  nameEl.classList.add("is-editing");
  nameEl.style.display = "none";
  // The pencil is a GRANDCHILD of nameEl.parentElement since the buttons were wrapped
  // in .active-item-name-actions (d6459bb) — insertBefore(input, btn) threw NotFoundError.
  // insertAdjacentElement needs no reference node, so it survives markup reshuffles.
  nameEl.insertAdjacentElement("afterend", input);
  actionsEl.classList.add("hidden");   // hide pencil AND 🔗 — the 🔗 must not sit beside the input
  input.focus();
  input.select();

  let resolved = false;
  const cleanup = () => {
    nameEl.classList.remove("is-editing");
    nameEl.style.display = "";
    if (input.parentElement) input.parentElement.removeChild(input);
    // Un-hide the SPAN before re-deriving per-button visibility.
    // _updateActiveItemRenameVisibility() toggles the individual BUTTONS, not this span —
    // if the span stayed hidden, pencil and 🔗 would both remain invisible while that
    // function believed it had shown them.
    actionsEl.classList.remove("hidden");
    _updateActiveItemRenameVisibility();   // single source of truth (was: ad-hoc btn toggle)
  };

  const finish = async (mode) => {
    if (resolved) return;
    resolved = true;
    if (mode === "cancel") {
      cleanup();
      return;
    }
    const nextName = String(input.value || "").trim();
    if (!nextName || nextName === currentName) {
      cleanup();
      return;
    }
    try {
      bumpUndoPillVersion();
      await _apiJson(`/api/areas/${encodeURIComponent(_currentLoadedAreaId)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ name: nextName }),
      });
      const cached = _savedAreasCache.find((a) => a.id === _currentLoadedAreaId);
      if (cached) cached.name = nextName;
      nameEl.textContent = nextName;
      cleanup();
      renderSavedAreasList();
      // Active-item pencil rename was missing the tab-title update that the
      // sidebar-row rename had. Now they match.
      _syncTabTitle();
    } catch (err) {
      console.error("[rename] active-item rename failed:", err);
      cleanup();
    }
  };

  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); void finish("cancel"); return; }
    if (e.key === "Enter") { e.preventDefault(); void finish("save"); }
  });
  input.addEventListener("blur", () => { void finish("save"); });
}

function _refreshLoadedAreaUi() {
  if (!_currentLoadedAreaId) return;
  renderSavedAreasList();
  _renderViewToggle();  // show + sync the ARV/NBV/Export toggle (flag-gated)
}

function _setSessionCacheNote(message) {
  const note = document.getElementById("session-cache-note");
  if (!note) return;
  const text = String(message || "").trim();
  if (!text) {
    note.classList.add("hidden");
    note.textContent = "";
    return;
  }
  note.textContent = text;
  note.classList.remove("hidden");
}

function _hydrateNumericInputsFromState() {
  NUMERIC_FILTER_INPUTS.forEach(({ id, key }) => {
    const el = document.getElementById(id);
    if (!el) return;
    const val = numericFilters[key];
    if (val == null) {
      el.value = "";
      return;
    }
    if (key === "lot_sqft_min" || key === "lot_sqft_max") {
      el.value = String((Number(val) / 43560).toFixed(2)).replace(/\.00$/, "");
      return;
    }
    if (key === "appr_val_min" || key === "appr_val_max") {
      el.value = formatNumberWithCommas(val);
      return;
    }
    el.value = String(val);
  });
}

function _hydrateSoldCompInputsFromState() {
  const soldDaysMax = document.getElementById("sold-days-max");
  const soldPriceMin = document.getElementById("sold-price-min");
  const soldPriceMax = document.getElementById("sold-price-max");
  const soldYrMin = document.getElementById("sold-yr-min");
  const soldYrMax = document.getElementById("sold-yr-max");

  if (soldDaysMax) {
    soldDaysMax.value = String(soldCompsFilter.maxDaysAgo ?? DEFAULT_SOLD_COMPS_FILTER.maxDaysAgo);
  }
  if (soldPriceMin) {
    soldPriceMin.value = soldCompsFilter.minPrice == null ? "" : formatNumberWithCommas(soldCompsFilter.minPrice);
  }
  if (soldPriceMax) {
    soldPriceMax.value = soldCompsFilter.maxPrice == null ? "" : formatNumberWithCommas(soldCompsFilter.maxPrice);
  }
  if (soldYrMin) {
    soldYrMin.value = soldCompsFilter.minYearBuilt == null ? "" : String(soldCompsFilter.minYearBuilt);
  }
  if (soldYrMax) {
    soldYrMax.value = soldCompsFilter.maxYearBuilt == null ? "" : String(soldCompsFilter.maxYearBuilt);
  }
}

// Hydrate Comp Filter numeric inputs (similar to Property but with nf-comp- prefix)
function _hydrateCompNumericInputsFromState() {
  const compInputs = [
    { id: "nf-comp-lot-min", key: "lot_sqft_min" },
    { id: "nf-comp-lot-max", key: "lot_sqft_max" },
    { id: "nf-comp-val-min", key: "appr_val_min" },
    { id: "nf-comp-val-max", key: "appr_val_max" },
    { id: "nf-comp-yr-min", key: "yr_built_min" },
    { id: "nf-comp-yr-max", key: "yr_built_max" },
    { id: "nf-comp-sqft-min", key: "sqft_min" },
    { id: "nf-comp-sqft-max", key: "sqft_max" },
  ];
  compInputs.forEach(({ id, key }) => {
    const el = document.getElementById(id);
    if (!el) return;
    const val = compNumericFilters[key];
    if (val == null) {
      el.value = "";
      return;
    }
    if (key === "lot_sqft_min" || key === "lot_sqft_max") {
      el.value = String((Number(val) / 43560).toFixed(2)).replace(/\.00$/, "");
      return;
    }
    if (key === "appr_val_min" || key === "appr_val_max") {
      el.value = formatNumberWithCommas(val);
      return;
    }
    el.value = String(val);
  });
}

function restoreFilterState(state, { _isAreaLoad = false } = {}) {
  // Restoring = LOAD, not EDIT: suppress autosave for the whole restore so a
  // view switch / area load never generates save traffic (incl. the comp
  // re-filter below, which otherwise auto-saves). try/finally guarantees the
  // flag is cleared even on the early-return guards. (KK 2026-06-29)
  _suppressFilterAutosave = true;
  try {
  if (!state || typeof state !== "object") return;
  if (Number(state.v || 0) > 1) {
    console.info("[saved-area] skipping newer filter_state version", state.v);
    return;
  }
  if (Number(state.v || 0) !== 1) return;
  // On area load, hydrate the per-view cache from the raw blob (which carries
  // _views from the server) and reset to ARV. On view-switch restores, the
  // caller already set _activeView and cached the departing view, so skip.
  if (ARV_NBV_EXPORT_ENABLED && _isAreaLoad) {
    _activeView = "arv";
    _viewFilterCache = {
      arv: {
        v: 1,
        checkboxes: (state.checkboxes && typeof state.checkboxes === "object") ? { ...state.checkboxes } : {},
        numeric:    (state.numeric    && typeof state.numeric    === "object") ? { ...state.numeric    } : {},
        sold:       (state.sold       && typeof state.sold       === "object") ? { ...state.sold       } : {},
        comp:       (state.comp       && typeof state.comp       === "object") ? { ...state.comp       } : {},
        propelio:   (state.propelio   && typeof state.propelio   === "object") ? { ...state.propelio   } : {},
      },
      nbv: null,
      export: null,
    };
    const _vv = (state._views && typeof state._views === "object") ? state._views : {};
    if (_vv.nbv    && typeof _vv.nbv    === "object") _viewFilterCache.nbv    = { v: 1, ..._vv.nbv    };
    if (_vv.export && typeof _vv.export === "object") _viewFilterCache.export = { v: 1, ..._vv.export };
  }
  // Rebase to defaults first so keys MISSING from the saved blob (e.g.,
  // older saved_areas with no `duplexes` checkbox key) resolve to the
  // current default rather than inheriting whatever the user's in-memory
  // toggle happened to be. Without this, restoring an old area would
  // silently leak the current toggle state for any new filter key.
  Object.assign(filterState, DEFAULT_FILTERS);
  if (state.checkboxes && typeof state.checkboxes === "object") Object.assign(filterState, state.checkboxes);
  // Legacy R.F. Active filter never auto-enables from saved state — mirrors the
  // localStorage-restore guard above. Old saved areas baked R.F. Active on when
  // it lived in Map Filters; chunk 2 made it opt-in only. The Sold filter is
  // no longer force-reset here — Phase 6 of the CSV export refactor enabled
  // sold-comp inclusion in analyze by default when the user opts in via the
  // filter checkbox, and forcing it off on every load prevented include_sold
  // from ever firing on the analyze endpoint.
  filterState.active = false;
  // Sprint 2 §6 (resolves deferred Sprint 1 §6): gate Property Filters
  // inheritance on power_user+. Regular `user`-role members loading a
  // shared area no longer inherit hidden numericFilters into their
  // module-global state. Under per-field PATCH this is also structurally
  // safe (regular user PATCHes only fields they touched, never numeric.*)
  // — gate stays as belt-and-suspenders for the blob-PUT compat path.
  if (state.numeric && typeof state.numeric === "object" && _isPowerUserOrAbove()) {
    Object.assign(numericFilters, state.numeric);
  }
  if (state.comp && typeof state.comp === "object") Object.assign(compNumericFilters, state.comp);
  if (state.sold && typeof state.sold === "object") {
    Object.assign(soldCompsFilter, {
      maxDaysAgo: state.sold.maxDaysAgo ?? DEFAULT_SOLD_COMPS_FILTER.maxDaysAgo,
      minPrice: state.sold.minPrice ?? null,
      maxPrice: state.sold.maxPrice ?? null,
      minYearBuilt: state.sold.minYearBuilt ?? null,
      maxYearBuilt: state.sold.maxYearBuilt ?? null,
    });
  }
  if (state.propelio && typeof state.propelio === "object") {
    applyPropelioFilterStateToUI(state.propelio);
  }
  console.debug("[restoreFilterState] restored", {
    checkboxes: { ...filterState },
    numeric: { ...numericFilters },
    comp: { ...compNumericFilters },
    sold: { ...soldCompsFilter },
    propelio: { ...propelioFilterState },
  });
  syncFilterInputs();
  _hydrateNumericInputsFromState();
  _hydrateCompNumericInputsFromState();
  _hydrateSoldCompInputsFromState();
  if (lastAnalysisGeojson) {
    applyAndRenderSoldFilters();
    const markers = viewportRenderMode ? renderViewportFeatures() : renderFeatures(lastAnalysisGeojson);
    // After re-render, parcel-type layers are all attached to the map. Re-apply
    // the visibility filter so the restored checkbox state actually hides the
    // correct categories on the map. Without this, the checkboxes show "off"
    // but the highlights remain visible.
    applyMapVisibilityFilters();
    const counts = getVisibleFeatureCounts(lastAnalysisGeojson.features || []);
    if (lastAnalysisCounts) renderSidebar(counts, markers || {});
  }
  // Re-filter the Propelio comps to the restored filter state. Without this a
  // view switch (ARV/NBV/Export) re-rendered parcels but left comps FROZEN at
  // the previous view's filter pass — so comps lagged/mismatched the active
  // view (root cause confirmed by two independent investigations 2026-06-29).
  // Self-guards on window._propelioLast, so it's a no-op when no comps are
  // loaded (e.g. during initial area load before comps arrive). Suppressed
  // autosave (see top) means this re-filter does NOT generate a save.
  applyPropelioClientFilters();
  _refreshLoadedAreaUi();
  } finally {
    _suppressFilterAutosave = false;
  }
}

function bumpUndoPillVersion() {
  undoPillVersion += 1;
  _dismissUndoPill();
}

function _dismissUndoPill() {
  const host = document.getElementById("undo-pill-host");
  if (host) host.innerHTML = "";
  if (_undoPillTimer) clearTimeout(_undoPillTimer);
  _undoPillTimer = null;
  _activeUndoSnapshot = null;
}

// One-shot localStorage marker for the duplexes-default-on migration
// (KK 2026-06-06: Mike asked for Duplexes default ON, but existing users
// have lotledger.map.filters.v1 with duplexes:false from the old default,
// which wins via the spread in loadFilters). On first visit after this
// change, force-apply the new default + set the marker. After the
// marker exists, normal localStorage precedence resumes — a user who
// turns Duplexes off later keeps their preference.
const FILTER_DUPLEXES_DEFAULT_ON_MIGRATION_KEY = "lotledger.map.filters.v1.duplexes_default_on_migration";

function loadFilters() {
  try {
    const raw = localStorage.getItem(FILTER_STORAGE_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return;
    // One-shot duplexes-default-on migration (see marker constant above).
    // Runs at most once per browser; after the marker is set, future
    // loads honor whatever the user has explicitly chosen.
    try {
      if (!localStorage.getItem(FILTER_DUPLEXES_DEFAULT_ON_MIGRATION_KEY)) {
        parsed.duplexes = DEFAULT_FILTERS.duplexes;
        localStorage.setItem(FILTER_DUPLEXES_DEFAULT_ON_MIGRATION_KEY, "1");
      }
    } catch (_) { /* fall through — non-blocking */ }
    filterState = { ...DEFAULT_FILTERS, ...parsed };
  } catch (_) {
    filterState = { ...DEFAULT_FILTERS };
  }
  // Legacy R.F. Active filter is tucked away in the collapsed "Legacy Filters"
  // card and should ONLY turn on when the user opts in by clicking it.
  // localStorage might hold a stale `true` from before the 2026-05-10
  // restructure when it lived in Map Filters and defaulted on — force off on
  // every load so it never fires on a search without explicit opt-in.
  // The Sold filter is no longer force-reset here — see the matching guard
  // above for the rationale (CSV export refactor Phase 6).
  filterState.active = false;
}

function saveFilters() {
  try {
    localStorage.setItem(FILTER_STORAGE_KEY, JSON.stringify(filterState));
  } catch (_) {}
}

function syncFilterInputs() {
  Object.entries(FILTER_INPUT_IDS).forEach(([key, id]) => {
    const input = document.getElementById(id);
    if (input) input.checked = Boolean(filterState[key]);
  });
  soldLayerVisible = Boolean(filterState.sold);
}

function classifyFeatureForFilter(feature) {
  const p = feature?.properties || {};
  if (p.on_redfin) return "active";
  if (p.prop_type === "vacant") return "vacant";
  if (p.prop_type === "multifamily") return "multifamily";
  if (p.prop_type === "duplexes") return "duplexes";
  if (p.prop_type === "commercial") return "commercial";
  if (p.prop_type === "exempt") return "exempt";
  return "off_market";
}

function isFeatureVisible(feature) {
  // The workspace's bonded originator parcel is the anchor of the analysis;
  // hiding it breaks the "compare everything else to this" mental model.
  // Always surface it through the filter — it shows up in counts, CSV export,
  // and any other consumer of isFeatureVisible. (The gold star marker on
  // _ORIGINATOR_STAR_LAYER is already independent of the bucket-type layers,
  // so the star stays visible on the map regardless of parcel-type toggles.)
  const p = feature?.properties || {};
  if (_currentTargetParcel
    && String(p.account_num || "").trim() === String(_currentTargetParcel.account || "").trim()
    && String(p.source_county || "").trim().toLowerCase() === String(_currentTargetParcel.county || "").trim().toLowerCase()) {
    return true;
  }
  const bucket = classifyFeatureForFilter(feature);
  if (!filterState[bucket]) return false;
  if (!passesNumericFilters(feature)) return false;
  const isListingOrSold = Boolean(p.on_redfin || p.sold_comp);
  if (isListingOrSold && !passesCompFilters(feature)) return false;
  return true;
}

function getVisibleFeatureCounts(features, options = {}) {
  const counts = {
    active: 0,
    off_market: 0,
    vacant: 0,
    multifamily: 0,
    duplexes: 0,
    commercial: 0,
    exempt: 0,
    contact_status: 0,
  };

  const list = Array.isArray(features) ? features : [];
  // Dedupe by (account_num + source_county) so the same parcel counted across
  // tile boundaries or cross-county merges only contributes once. Falls back
  // to feature index if account_num is missing so we never silently drop a row.
  const seen = new Set();
  let rawSeen = 0;
  let dupSkipped = 0;
  let unknownPropType = 0;
  const ignoreBucketToggles = options.ignoreBucketToggles === true;

  list.forEach((feature, idx) => {
    rawSeen += 1;
    const p = feature?.properties || {};
    const key = `${p.account_num || `__noacct_${idx}`}|${p.source_county || "dcad"}`;
    if (seen.has(key)) {
      dupSkipped += 1;
      return;
    }
    seen.add(key);

    const bucket = classifyFeatureForFilter(feature);
    if (!(bucket in counts)) return;
    if (!passesNumericFilters(feature)) return;
    const isListingOrSold = Boolean(p.on_redfin || p.sold_comp);
    if (isListingOrSold && !passesCompFilters(feature)) return;
    if (!ignoreBucketToggles && !filterState[bucket]) return;

    // Track features falling through to off_market that are NOT recognized
    // single_family — that signals a misclassification path we can investigate.
    if (bucket === "off_market" && p.prop_type && p.prop_type !== "single_family") {
      unknownPropType += 1;
    }

    counts[bucket] += 1;

    // Contact Status is orthogonal to the property-type bucket — a parcel
    // can be off_market AND have outreach data. Count it independently so
    // the sidebar badge reflects "how many parcels in this area have any
    // outreach activity," not "how many are bucketed as contact_status."
    if (p.outreach_contact_info_retrieved || p.outreach_mailer_date) {
      counts.contact_status += 1;
    }
  });

  if (rawSeen > 0) {
    console.debug("[counts]", {
      raw: rawSeen,
      deduped: seen.size,
      dupSkipped,
      unknownPropType,
      off_market: counts.off_market,
      active: counts.active,
      vacant: counts.vacant,
      multifamily: counts.multifamily,
      duplexes: counts.duplexes,
      commercial: counts.commercial,
      exempt: counts.exempt,
    });
  }

  return counts;
}

function abbreviatePrice(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return "";
  if (n >= 1000000) {
    const m = n / 1000000;
    return `$${m >= 10 ? m.toFixed(0) : m.toFixed(1)}m`;
  }
  if (n >= 1000) return `$${Math.round(n / 1000)}k`;
  return `$${Math.round(n)}`;
}

function formatSoldDateLabel(value) {
  if (!value) return "";
  const d = new Date(String(value));
  if (Number.isNaN(d.getTime())) return "";
  const month = d.toLocaleString("en-US", { month: "short" });
  const year2 = String(d.getFullYear()).slice(-2);
  return `${month} '${year2}`;
}

function refreshSoldPriceLabels() {
  soldMarkers.forEach(({ marker }) => marker.unbindTooltip());
  if (!soldLayerVisible || map.getZoom() < 15) return;

  const maxLabels = map.getZoom() >= 18 ? 220 : map.getZoom() >= 17 ? 140 : 80;
  const cellPx = map.getZoom() >= 18 ? 20 : map.getZoom() >= 17 ? 26 : 34;
  const occupied = new Set();
  let shown = 0;

  for (const { marker, priceLabel, soldDateLabel } of soldMarkers) {
    if (shown >= maxLabels) break;
    if (!priceLabel) continue;
    const p = map.latLngToContainerPoint(marker.getLatLng());
    const key = `${Math.floor(p.x / cellPx)}:${Math.floor(p.y / cellPx)}`;
    if (occupied.has(key)) continue;
    occupied.add(key);
    const labelText = soldDateLabel ? `${priceLabel} · ${soldDateLabel}` : priceLabel;
    marker.bindTooltip(labelText, {
      permanent: true,
      direction: "top",
      offset: [10, -8],
      className: "sold-price-label",
      interactive: false,
    });
    shown += 1;
  }
}

function refreshRedfinPriceLabels() {
  redfinMarkers.forEach(({ marker }) => marker.unbindTooltip());
  if (!filterState.active || map.getZoom() < 15) return;

  const maxLabels = map.getZoom() >= 18 ? 220 : map.getZoom() >= 17 ? 140 : 80;
  const cellPx = map.getZoom() >= 18 ? 20 : map.getZoom() >= 17 ? 26 : 34;
  const occupied = new Set();
  let shown = 0;

  for (const { marker, priceLabel } of redfinMarkers) {
    if (shown >= maxLabels) break;
    if (!priceLabel) continue;
    const p = map.latLngToContainerPoint(marker.getLatLng());
    const key = `${Math.floor(p.x / cellPx)}:${Math.floor(p.y / cellPx)}`;
    if (occupied.has(key)) continue;
    occupied.add(key);
    marker.bindTooltip(priceLabel, {
      permanent: true,
      direction: "top",
      offset: [10, -8],
      className: "redfin-price-label",
      interactive: false,
    });
    shown += 1;
  }
}

// Zoom-gated permanent price tooltips for Propelio comps. Mirrors the
// sold + redfin patterns. Status drives the chip color: sold→purple,
// active→red, pending→amber-with-dashed-border (visually distinct
// from active red so they don't blur together at a glance). Bad-rated
// comps are excluded at marker-build time so they never get a balloon.
function refreshPropelioPriceLabels() {
  propelioPriceMarkers.forEach(({ marker }) => marker.unbindTooltip());
  if (map.getZoom() < 15) return;
  if (!propelioPriceMarkers.length) return;

  const maxLabels = map.getZoom() >= 18 ? 220 : map.getZoom() >= 17 ? 140 : 80;
  const cellPx = map.getZoom() >= 18 ? 20 : map.getZoom() >= 17 ? 26 : 34;
  const occupied = new Set();
  let shown = 0;

  for (const { marker, priceLabel, bucket, soldDateLabel } of propelioPriceMarkers) {
    if (shown >= maxLabels) break;
    if (!priceLabel) continue;
    const p = map.latLngToContainerPoint(marker.getLatLng());
    const key = `${Math.floor(p.x / cellPx)}:${Math.floor(p.y / cellPx)}`;
    if (occupied.has(key)) continue;
    occupied.add(key);
    const tooltipHtml = (bucket === "sold" && soldDateLabel)
      ? `<div class="propelio-price-label-price">${priceLabel}</div><div class="propelio-price-label-date">${soldDateLabel}</div>`
      : priceLabel;
    marker.bindTooltip(tooltipHtml, {
      permanent: true,
      direction: "top",
      offset: [10, -8],
      className: `propelio-price-label ${bucket || "sold"}`,
      interactive: false,
    });
    shown += 1;
  }
}

function asNumber(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function median(values) {
  const nums = values.filter((v) => Number.isFinite(v)).sort((a, b) => a - b);
  if (!nums.length) return null;
  const mid = Math.floor(nums.length / 2);
  if (nums.length % 2 === 0) return (nums[mid - 1] + nums[mid]) / 2;
  return nums[mid];
}

function soldAddressStreetOnly(address) {
  return String(address || "").split(",")[0].trim() || "Unknown address";
}

function soldPointKey(point) {
  return String(point?.listing_url || `${point?.lat},${point?.lng},${point?.sold_date || ""}`);
}

function closeTransientSoldSidebarPopup() {
  if (!transientSoldSidebarPopup) return;
  map.closePopup(transientSoldSidebarPopup);
  transientSoldSidebarPopup = null;
}

function findMatchedFeatureForSoldPoint(point) {
  if (!Array.isArray(allAnalysisFeatures) || !allAnalysisFeatures.length) return null;
  const listingUrl = String(point?.listing_url || "");
  const fallbackKey = soldPointKey(point);
  for (const feature of allAnalysisFeatures) {
    const p = feature?.properties || {};
    const sold = p.sold_comp;
    if (!sold) continue;
    if (listingUrl && String(sold.listing_url || "") === listingUrl) return feature;
    const featureKey = String(sold.listing_url || `${p.lat},${p.lng},${sold.sold_date || ""}`);
    if (featureKey === fallbackKey) return feature;
  }
  return null;
}

function zoomToSoldComp(point) {
  const lat = Number(point?.lat);
  const lng = Number(point?.lng);
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return;
  const matchedFeature = findMatchedFeatureForSoldPoint(point);
  const targetZoom = Math.max(map.getZoom(), 17);
  let opened = false;
  const openPopup = () => {
    if (opened) return;
    opened = true;
    if (!matchedFeature) return;
    const p = matchedFeature.properties || {};
    closeTransientSoldSidebarPopup();
    transientSoldSidebarPopup = L.popup({ autoPan: false })
      .setLatLng([lat, lng])
      .setContent(makePopupHtml(p))
      .openOn(map);
  };

  map.once("moveend", openPopup);
  map.flyTo([lat, lng], targetZoom, { duration: 0.35 });
  setTimeout(openPopup, 400);
}

// --- Sold comps filter helpers ---

function _soldPointPassesFilter(p, filter) {
  if (filter.maxDaysAgo != null) {
    const d = new Date(p.sold_date);
    if (isNaN(d.getTime())) return false;
    const ageDays = (Date.now() - d.getTime()) / 86_400_000;
    if (ageDays > filter.maxDaysAgo) return false;
  }

  const price = asNumber(p.sold_price);
  if (filter.minPrice != null && (price == null || price < filter.minPrice)) return false;
  if (filter.maxPrice != null && (price == null || price > filter.maxPrice)) return false;

  const yr = asNumber(p.yr_built);
  if (filter.minYearBuilt != null && (yr == null || yr < filter.minYearBuilt)) return false;
  if (filter.maxYearBuilt != null && (yr == null || yr > filter.maxYearBuilt)) return false;

  // The Sold Comps + Listings panel filter bar in renderSoldCompsPanel writes
  // Lot Size, Building Sqft, and Year Built inputs into compNumericFilters
  // (not soldCompsFilter). Without honoring those here, the sidebar list
  // silently ignored 3 of the 5 visible filters even while the same values
  // correctly filtered the on-map polygons.
  if (compNumericFilters.yr_built_min != null && (yr == null || yr < compNumericFilters.yr_built_min)) return false;
  if (compNumericFilters.yr_built_max != null && (yr == null || yr > compNumericFilters.yr_built_max)) return false;

  const lot = asNumber(p.lot_sqft);
  if (compNumericFilters.lot_sqft_min != null && (lot == null || lot < compNumericFilters.lot_sqft_min)) return false;
  if (compNumericFilters.lot_sqft_max != null && (lot == null || lot > compNumericFilters.lot_sqft_max)) return false;

  const sqft = asNumber(p.sqft);
  if (compNumericFilters.sqft_min != null && (sqft == null || sqft < compNumericFilters.sqft_min)) return false;
  if (compNumericFilters.sqft_max != null && (sqft == null || sqft > compNumericFilters.sqft_max)) return false;

  return true;
}

function soldCompsFiltersAreActive() {
  return soldCompsFilter.maxDaysAgo !== DEFAULT_SOLD_COMPS_FILTER.maxDaysAgo
    || soldCompsFilter.minPrice != null
    || soldCompsFilter.maxPrice != null
    || soldCompsFilter.minYearBuilt != null
    || soldCompsFilter.maxYearBuilt != null;
}

function updateMatchedSoldCompVisibility(filter) {
  const features = Array.isArray(allAnalysisFeatures) ? allAnalysisFeatures : [];
  const nextMatchedLabelPoints = [];

  features.forEach((feature) => {
    const props = feature?.properties;
    if (!props) return;

    const source = props._sold_comp_source;
    if (!source || !_soldPointPassesFilter(source, filter)) {
      delete props.sold_comp;
      return;
    }

    props.sold_comp = { ...source };
    const lat = Number(props.lat);
    const lng = Number(props.lng);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) return;
    nextMatchedLabelPoints.push({
      account_num: String(props.account_num || ""),
      lat,
      lng,
      sold_price: source.sold_price,
      sold_date: source.sold_date,
    });
  });

  matchedSoldLabelPoints = nextMatchedLabelPoints;
}

function updateSoldStatusText() {
  const soldStatus = document.getElementById("sold-toggle-status");
  if (!soldStatus) return;
  if (!filterState.sold) {
    soldStatus.textContent = "Some comps filtered out";
    return;
  }

  const filteredCount = lastSoldPanelPoints.length;
  const totalCount = allSoldPointsRef.length;
  if (soldCompsFiltersAreActive() && filteredCount < totalCount) {
    soldStatus.textContent = "Some comps filtered out";
    return;
  }

  // Default: all sold comps visible - clear so note doesn't read
  // "filtered out" when nothing actually is.
  soldStatus.textContent = "";
}

function applyAndRenderSoldFilters() {
  lastSoldPanelPoints = allSoldPointsRef.filter((p) =>
    _soldPointPassesFilter(p, soldCompsFilter)
  );
  updateMatchedSoldCompVisibility(soldCompsFilter);
  // renderFeatures auto-runs renderSoldPoints at its end, so anchors stay in sync.
  // For paths that don't re-render features (e.g., sold-only filter input changes
  // when no analysis is loaded), call renderSoldPoints directly as a fallback.
  if (lastAnalysisGeojson && Array.isArray(lastAnalysisGeojson.features) && lastAnalysisGeojson.features.length <= BROWSE_ONLY_THRESHOLD) {
    if (viewportRenderMode) {
      renderViewportFeatures();
    } else {
      renderFeatures(lastAnalysisGeojson);
    }
  } else {
    renderSoldPoints();
  }
  renderSoldCompsPanel();
  updateSoldStatusText();
}

// --- End sold comps filter helpers ---

function renderSoldCompsPanel() {
  const panel = document.getElementById("sold-comps-panel");
  if (!panel) return;
  const totalSoldCount = Array.isArray(allSoldPointsRef) ? allSoldPointsRef.length : 0;
  const soldCountNote = `<p class="sidebar-note sold-comps-count-note">${totalSoldCount} sold comp${totalSoldCount === 1 ? "" : "s"} in this area</p>`;

  const priceValues = lastSoldPanelPoints.map((p) => asNumber(p.sold_price)).filter((n) => n != null);
  const ppsfValues = lastSoldPanelPoints.map((p) => asNumber(p.price_per_sqft)).filter((n) => n != null);
  const medianPrice = median(priceValues);
  const medianPpsf = median(ppsfValues);

  const metricGetter = soldCompsSortMode === "ppsf"
    ? (p) => asNumber(p.price_per_sqft) ?? -Infinity
    : (p) => asNumber(p.sold_price) ?? -Infinity;

  const topRows = [...lastSoldPanelPoints]
    .sort((a, b) => metricGetter(b) - metricGetter(a))
    .slice(0, 5)
    .map((point, idx) => {
      const price = abbreviatePrice(point.sold_price) || "N/A";
      const ppsf = asNumber(point.price_per_sqft);
      const sqft = asNumber(point.sqft);
      const beds = asNumber(point.beds);
      const baths = asNumber(point.baths);
      const yrBuilt = asNumber(point.yr_built);
      const soldDate = formatSoldDateLabel(point.sold_date) || "N/A";
      const ppsfText = ppsf != null ? `$${Math.round(ppsf)}/sqft` : "N/A";
      const sizeText = sqft != null ? `${Math.round(sqft).toLocaleString()} sf` : "N/A sf";
      const lot = asNumber(point.lot_sqft);
      const lotText = lot != null ? `${(lot / 43560).toFixed(2)} ac` : "N/A ac";
      const bedBathText = `${beds != null ? beds : "?"}bd/${baths != null ? baths : "?"}ba`;
      const yearText = yrBuilt != null ? `${Math.round(yrBuilt)}` : "N/A";
      return `
        <div class="sold-row" data-sold-idx="${idx}" data-lat="${point.lat ?? ""}" data-lng="${point.lng ?? ""}">
          <div class="sold-row-top">
            <span class="sold-row-price">${price}</span>
            <span>${ppsfText}</span>
          </div>
          <div class="sold-row-meta">${sizeText} · ${lotText} · ${bedBathText} · Built ${yearText}</div>
          <div class="sold-row-sub">${soldDate} · ${soldAddressStreetOnly(point.address)}</div>
        </div>
      `;
    })
    .join("");

  const sortedForClick = [...lastSoldPanelPoints]
    .sort((a, b) => metricGetter(b) - metricGetter(a))
    .slice(0, 5);

  // --- Filter bar ---
  const filterBar = `
    <div class="sold-filter-bar">
      <div class="numeric-filter-row">
        <span class="numeric-filter-label">Sold Within (days)</span>
        <div class="numeric-filter-inputs">
          <input type="number" id="sold-days-max" placeholder="365" class="nf-input" min="1" value="${soldCompsFilter.maxDaysAgo ?? 365}">
        </div>
      </div>
      <div class="numeric-filter-row">
        <span class="numeric-filter-label">Sold Price ($)</span>
        <div class="numeric-filter-inputs">
          <input type="text" id="sold-price-min" placeholder="Min (500k)" class="nf-input" value="${soldCompsFilter.minPrice ?? ""}">
          <span class="nf-sep">–</span>
          <input type="text" id="sold-price-max" placeholder="Max (1m)" class="nf-input" value="${soldCompsFilter.maxPrice ?? ""}">
        </div>
      </div>
      <div class="numeric-filter-row">
        <span class="numeric-filter-label">Lot Size (acres)</span>
        <div class="numeric-filter-inputs">
          <input type="text" inputmode="decimal" id="nf-comp-lot-min" placeholder="Min acres" class="nf-input" value="${compNumericFilters.lot_sqft_min == null ? "" : (compNumericFilters.lot_sqft_min / 43560).toFixed(2).replace(/\.00$/, "")}">
          <span class="nf-sep">–</span>
          <input type="text" inputmode="decimal" id="nf-comp-lot-max" placeholder="Max acres" class="nf-input" value="${compNumericFilters.lot_sqft_max == null ? "" : (compNumericFilters.lot_sqft_max / 43560).toFixed(2).replace(/\.00$/, "")}">
        </div>
      </div>
      <div class="numeric-filter-row">
        <span class="numeric-filter-label">Building Sqft</span>
        <div class="numeric-filter-inputs">
          <input type="number" id="nf-comp-sqft-min" placeholder="Min" class="nf-input" min="0" value="${compNumericFilters.sqft_min ?? ""}">
          <span class="nf-sep">–</span>
          <input type="number" id="nf-comp-sqft-max" placeholder="Max" class="nf-input" min="0" value="${compNumericFilters.sqft_max ?? ""}">
        </div>
      </div>
      <div class="numeric-filter-row">
        <span class="numeric-filter-label">Year Built</span>
        <div class="numeric-filter-inputs">
          <input type="number" id="nf-comp-yr-min" placeholder="Min" class="nf-input" min="1800" max="2030" value="${compNumericFilters.yr_built_min ?? ""}">
          <span class="nf-sep">–</span>
          <input type="number" id="nf-comp-yr-max" placeholder="Max" class="nf-input" min="1800" max="2030" value="${compNumericFilters.yr_built_max ?? ""}">
        </div>
      </div>
    </div>`;
  // --- End filter bar ---

  const countLabel = soldCompsFiltersAreActive()
    ? `${lastSoldPanelPoints.length} of ${allSoldPointsRef.length} comps`
    : `${lastSoldPanelPoints.length} comps`;

  panel.innerHTML = `
    ${soldCountNote}
    <div class="sold-comps-panel">
      <button class="section-toggle" type="button" id="sold-comps-toggle" aria-expanded="${!soldCompsCollapsed}">
        <span class="sidebar-label">Legacy Filters</span>
      </button>
      <div id="sold-comps-body" class="collapsible-body${soldCompsCollapsed ? " hidden" : ""}">
        <div class="sold-comps-summary">
          <span class="sold-chip">${countLabel}</span>
          <span class="sold-chip">Median ${medianPrice != null ? abbreviatePrice(medianPrice) : "N/A"}</span>
          <span class="sold-chip">Median ${medianPpsf != null ? `$${Math.round(medianPpsf)}/sqft` : "N/A"}</span>
        </div>
        ${filterBar}
        <div class="sold-sort">
          <button type="button" class="sold-sort-btn${soldCompsSortMode === "price" ? " active" : ""}" data-sort="price">By Price</button>
          <button type="button" class="sold-sort-btn${soldCompsSortMode === "ppsf" ? " active" : ""}" data-sort="ppsf">By $/sqft</button>
        </div>
        <div class="sold-context">Recent sales — what finished homes are commanding in this pocket</div>
        <div class="sold-list">${topRows}</div>
      </div>
    </div>
  `;

  const soldToggle = document.getElementById("sold-comps-toggle");
  const soldBody = document.getElementById("sold-comps-body");
  soldToggle?.addEventListener("click", () => {
    soldCompsCollapsed = !soldCompsCollapsed;
    soldToggle.setAttribute("aria-expanded", String(!soldCompsCollapsed));
    soldBody?.classList.toggle("hidden", soldCompsCollapsed);
  });

  panel.querySelectorAll(".sold-sort-btn[data-sort]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const next = btn.getAttribute("data-sort") === "ppsf" ? "ppsf" : "price";
      if (next === soldCompsSortMode) return;
      soldCompsSortMode = next;
      renderSoldCompsPanel();
    });
  });

  const soldDaysMaxInput = panel.querySelector("#sold-days-max");
  const soldPriceMinInput = panel.querySelector("#sold-price-min");
  const soldPriceMaxInput = panel.querySelector("#sold-price-max");
  const compLotMinInput = panel.querySelector("#nf-comp-lot-min");
  const compLotMaxInput = panel.querySelector("#nf-comp-lot-max");
  const compYrMinInput = panel.querySelector("#nf-comp-yr-min");
  const compYrMaxInput = panel.querySelector("#nf-comp-yr-max");
  const compSqftMinInput = panel.querySelector("#nf-comp-sqft-min");
  const compSqftMaxInput = panel.querySelector("#nf-comp-sqft-max");

  const normalizeShorthandInput = (inputEl) => {
    if (!inputEl) return null;
    const raw = String(inputEl.value || "").trim();
    if (!raw) {
      inputEl.value = "";
      return null;
    }
    const parsed = parseShorthand(raw);
    if (parsed == null) {
      inputEl.value = "";
      return null;
    }
    const rounded = Math.round(parsed);
    inputEl.value = formatNumberWithCommas(rounded);
    return rounded;
  };

  const parseIntegerInput = (inputEl) => {
    if (!inputEl) return null;
    const raw = String(inputEl.value || "").trim();
    if (!raw) return null;
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? Math.round(parsed) : null;
  };

  const applySoldCompInputFilters = () => {
    bumpUndoPillVersion();
    const maxDays = parseIntegerInput(soldDaysMaxInput);
    soldCompsFilter.maxDaysAgo = maxDays == null ? DEFAULT_SOLD_COMPS_FILTER.maxDaysAgo : Math.max(1, maxDays);
    if (soldDaysMaxInput) soldDaysMaxInput.value = String(soldCompsFilter.maxDaysAgo);

    soldCompsFilter.minPrice = parseShorthand(soldPriceMinInput?.value);
    soldCompsFilter.maxPrice = parseShorthand(soldPriceMaxInput?.value);
    applyAndRenderSoldFilters();
    _refreshLoadedAreaUi();
    // v1 §2.1 — auto-save filter_state after sold-comp filter apply.
    _filterSaveQueueSave();
  };

  const applyCompNumericInputFilters = () => {
    bumpUndoPillVersion();
    // Read comp numeric filter inputs  and apply
    _applyCompNumericFilters();
  };

  soldDaysMaxInput?.addEventListener("blur", applySoldCompInputFilters);
  soldDaysMaxInput?.addEventListener("change", applySoldCompInputFilters);

  [soldPriceMinInput, soldPriceMaxInput].forEach((inputEl) => {
    inputEl?.addEventListener("blur", () => {
      normalizeShorthandInput(inputEl);
      applySoldCompInputFilters();
    });
    inputEl?.addEventListener("change", () => {
      normalizeShorthandInput(inputEl);
      applySoldCompInputFilters();
    });
  });

  // Comp numeric filter inputs: use blur + change (NOT input) to avoid re-render mid-keystroke
  [compLotMinInput, compLotMaxInput, compYrMinInput, compYrMaxInput, compSqftMinInput, compSqftMaxInput].forEach((inputEl) => {
    inputEl?.addEventListener("blur", applyCompNumericInputFilters);
    inputEl?.addEventListener("change", applyCompNumericInputFilters);
  });

  panel.querySelectorAll(".sold-row[data-sold-idx]").forEach((rowEl) => {
    rowEl.addEventListener("click", () => {
      const idx = Number(rowEl.getAttribute("data-sold-idx"));
      const point = sortedForClick[idx];
      if (point) zoomToSoldComp(point);
    });
  });

  panel.querySelectorAll(".sold-row").forEach((row) => {
    row.addEventListener("click", () => {
      const lat = parseFloat(row.dataset.lat);
      const lng = parseFloat(row.dataset.lng);
      if (!Number.isFinite(lat) || !Number.isFinite(lng)) return;
      map.flyTo([lat, lng], Math.max(map.getZoom(), 17), { duration: 0.6 });
    });
  });

  // Hydrate sold-comp filter inputs from current state. Required because the
  // sold comps panel renders dynamically — restoring a saved area sets the
  // soldCompsFilter state before this panel exists, so we have to push state
  // to DOM here, every time the panel mounts.
  _hydrateSoldCompInputsFromState();
}

async function toggleHoaLayer() {
  const btn = document.getElementById("btn-hoa-toggle");
  if (hoaVisible && hoaLayer) {
    map.removeLayer(hoaLayer);
    hoaVisible = false;
    btn.classList.remove("active");
    return;
  }
  if (hoaLayer) {
    hoaLayer.addTo(map);
    hoaVisible = true;
    btn.classList.add("active");
    return;
  }
  // First load — fetch from API. NOTE: do NOT touch btn.textContent — the
  // HOA button is now a row inside the LYRS popover with child spans
  // (dot + label), and textContent would wipe those. The .active class
  // is sufficient feedback; fetch is fast enough that a loading state
  // is barely visible.
  try {
    const res = await fetch("/api/hoa");
    const geojson = await res.json();
    hoaLayer = L.geoJSON(geojson, {
      style: {
        color: HOA_COLOR,
        weight: 2,
        fillColor: HOA_COLOR,
        fillOpacity: 0.08,
        dashArray: "5 4",
      },
      onEachFeature(feature, layer) {
        const p = feature.properties;
        const urlLine = p.url ? `<br><a href="http://${p.url}" target="_blank" rel="noopener noreferrer">${p.url}</a>` : "";
        layer.bindTooltip(`<strong>${p.name}</strong>${urlLine}`, {
          sticky: true,
          opacity: 0.95,
        });
      },
    }).addTo(map);
    hoaVisible = true;
    btn.classList.add("active");
  } catch (e) {
    console.error("HOA layer load failed", e);
  }
}

// FEMA NFHL flood zone overlay — PMTiles-backed via protomaps-leaflet.
//
// Original Phase 3 used a live GET /api/flood-zones?bbox=... endpoint serving
// GeoJSON. At wide zoom it OOM'd the Cloud Run preview instance (1Gi memory)
// → 503 errors. PMTiles serves pre-rendered vector tiles directly from GCS:
// browser pulls ~10KB tiles for the current viewport, zero per-request DB
// work, GPU-accelerated rendering. Build pipeline:
//   1. scripts/build_flood_pmtiles.py emits flood_zones.geojsonl from PostGIS
//   2. tippecanoe converts to flood_zones.pmtiles (Z8-Z16)
//   3. gsutil uploads to gs://lot-ledger-tiles/flood_zones.pmtiles
//   4. This layer fetches the .pmtiles file at toggle-on time and
//      protomaps-leaflet does the rest.
//
// Severity-gradient palette (locked spec, unchanged from the live-API
// implementation):
//   FLOODWAY              → dark red #8B0000, 45% opacity
//   AE / A / AH / AO / V  → red #DC2626, 30% opacity
//   X (500-yr / shaded)   → amber #F59E0B, 22% opacity
//   X (minimal)           → faint gray, basically invisible
//   anything else (D, B…) → gray outline fallback (defensive)
//
// Severity numeric code (computed at build time via PostGIS) lets the
// symbolizer fast-switch without parsing strings per feature:
//   5 = FLOODWAY, 4 = AE/A/AH/AO/V/VE, 3 = X-shaded, 2 = X-unshaded, 1 = other
const FLOOD_PMTILES_URL = (window.LL_CONFIG && window.LL_CONFIG.floodTilesUrl
                          && window.LL_CONFIG.floodTilesUrl !== "__FLOOD_TILES_URL__")
  ? window.LL_CONFIG.floodTilesUrl
  : "https://storage.googleapis.com/lot-ledger-tiles/flood_zones.pmtiles";

function _floodZoneFillByFeature(zoom, feature) {
  const sev = Number(feature?.props?.severity || 0);
  if (sev === 5) return "rgba(139,0,0,0.45)";       // FLOODWAY
  if (sev === 4) return "rgba(220,38,38,0.30)";     // AE / A / V
  if (sev === 3) return "rgba(245,158,11,0.22)";    // X-shaded (500-yr)
  if (sev === 2) return "rgba(156,163,175,0.18)";   // X-unshaded (Mike/KK 2026-06-08: 0.05 → 0.18 so it's actually visible)
  return "rgba(107,114,128,0.08)";                  // fallback
}

function _floodZoneStrokeByFeature(zoom, feature) {
  const sev = Number(feature?.props?.severity || 0);
  if (sev === 5) return "rgba(102,0,0,0.85)";
  if (sev === 4) return "rgba(153,27,27,0.7)";
  if (sev === 3) return "rgba(180,83,9,0.6)";
  if (sev === 2) return "rgba(156,163,175,0.4)";
  return "rgba(107,114,128,0.5)";
}

function _floodZonePaintRules() {
  return [{
    dataLayer: "flood_zones",
    symbolizer: new protomapsL.PolygonSymbolizer({
      fill: _floodZoneFillByFeature,
      stroke: _floodZoneStrokeByFeature,
      width: 1.0,
      opacity: 1.0,
      perFeature: true,
    }),
  }];
}

function _floodZoneFeatureLabel(p) {
  // Mirrors api/main.py:_flood_zone_csv_cell verbose plain-English format.
  const zone = String(p?.fld_zone || "").trim();
  if (!zone) return "";
  const subty = String(p?.zone_subty || "").trim().toUpperCase();
  const bfe = p?.static_bfe;
  if (subty === "FLOODWAY") return `${zone} — FLOODWAY (no build)`;
  if (zone === "AE" || zone === "A" || zone === "AH" || zone === "AO" || zone === "V" || zone === "VE") {
    if (bfe !== null && bfe !== undefined && Number.isFinite(Number(bfe))) {
      return `${zone} (BFE ${Number(bfe).toFixed(1)} ft)`;
    }
    return zone;
  }
  if (zone === "X" && subty.indexOf("0.2 PCT") !== -1) return "X — 500-yr floodplain";
  if (zone === "X" && subty.indexOf("MINIMAL") !== -1) return "X — minimal risk";
  return zone;
}

function toggleFloodZonesLayer() {
  const btn = document.getElementById("btn-flood-toggle");
  if (floodZonesVisible && floodZonesLayer) {
    map.removeLayer(floodZonesLayer);
    floodZonesVisible = false;
    if (btn) btn.classList.remove("active");
    return;
  }
  if (!floodZonesLayer) {
    floodZonesLayer = protomapsL.leafletLayer({
      url: FLOOD_PMTILES_URL,
      paintRules: _floodZonePaintRules(),
      labelRules: [],
      pane: "floodZonesPane",
    });
    // Match the browse layer pattern: disable pointer events so parcel
    // popups still receive clicks normally.
    const _container = floodZonesLayer.getContainer && floodZonesLayer.getContainer();
    if (_container) _container.style.pointerEvents = "none";
  }
  floodZonesLayer.addTo(map);
  floodZonesVisible = true;
  if (btn) btn.classList.add("active");
}

async function toggleCountyLayer() {
  const btn = document.getElementById("btn-county-toggle");
  if (countyVisible && countyLayer) {
    map.removeLayer(countyLayer);
    if (countyLabelLayer && map.hasLayer(countyLabelLayer)) map.removeLayer(countyLabelLayer);
    countyVisible = false;
    btn?.classList.remove("active");
    return;
  }
  if (countyLayer) {
    countyLayer.addTo(map);
    countyVisible = true;
    btn?.classList.add("active");
    _updateCountyLabelVisibility();
    return;
  }

  // NOTE: do NOT touch btn.textContent — the County button is now a row
  // inside the LYRS popover with child spans (dot + label), and
  // textContent would wipe those. The .active class is sufficient
  // feedback.
  try {
    const res = await fetch("/api/counties/boundaries");
    const geojson = await res.json();
    countyLayer = L.geoJSON(geojson, {
      style: {
        color: "#e8c96a",
        weight: 2,
        opacity: 0.95,
        fill: false,
      },
      interactive: false,
    }).addTo(map);
    countyLabelLayer = L.layerGroup();
    countyLayer.eachLayer((layer) => {
      const bounds = layer.getBounds?.();
      if (!bounds || !bounds.isValid()) return;
      const name = layer.feature?.properties?.name || layer.feature?.properties?.NAME || "";
      if (!name) return;
      // 2026-05-22: prefer the polygon's mass-center (Leaflet's getCenter on
      // the polygon layer = area-weighted centroid) over bounds.getCenter
      // (bbox center, can fall outside irregular shapes). Falls back to bbox
      // if mass-center isn't available.
      const labelLatLng = (typeof layer.getCenter === "function" ? layer.getCenter() : bounds.getCenter());
      L.marker(labelLatLng, {
        pane: "countyLabelPane",
        interactive: false,
        icon: L.divIcon({
          className: "county-label",
          html: name,
          iconSize: null,
        }),
      }).addTo(countyLabelLayer);
    });
    countyVisible = true;
    btn?.classList.add("active");
    _updateCountyLabelVisibility();
  } catch (e) {
    console.error("County layer load failed", e);
  }
}

function _updateCountyLabelVisibility() {
  if (!countyLabelLayer) return;
  const shouldShow = countyVisible && map.getZoom() >= COUNTY_LABEL_MIN_ZOOM;
  if (shouldShow) {
    if (!map.hasLayer(countyLabelLayer)) countyLabelLayer.addTo(map);
  } else if (map.hasLayer(countyLabelLayer)) {
    map.removeLayer(countyLabelLayer);
  }
  _updateCountyLabelStyles();
}

// 2026-05-22 v3: pixel-accurate label sizing via canvas measureText.
// Previous v2 used a char-count * fontSize heuristic that underestimated
// width because the CSS has font-weight:700, text-transform:uppercase,
// AND letter-spacing:0.04em — all of which expand rendered width.
//
// Now we MEASURE the actual text width at each candidate font-size and
// pick the largest size where the text fits inside the county polygon
// with comfortable padding. Conservative caps: never larger than 13px,
// max 70% of county width, max 28% of county height.
//
// Pattern follows the same approach used by D3 / Mapbox label engines:
// measure → fit → fallback to hide. Canvas measureText is fast (a few
// microseconds per call); ~60 labels × ~6 candidates = imperceptible.
const _LABEL_MAX_PX = 13;
const _LABEL_MIN_PX = 9;
const _LABEL_WIDTH_PAD = 0.70;
const _LABEL_HEIGHT_PAD = 0.28;
const _LABEL_MIN_COUNTY_PX = 110; // 2026-05-22 (220 → 110) — one zoom level
                                  // further out before disappearing

let _labelMeasureCanvas = null;
function _measureLabelWidth(text, fontPx) {
  if (!_labelMeasureCanvas) _labelMeasureCanvas = document.createElement("canvas");
  const ctx = _labelMeasureCanvas.getContext("2d");
  // Match .county-label CSS exactly: 700 weight, uppercase already in DOM text,
  // 0.04em letter-spacing approximated by adding (fontPx*0.04)*(text.length-1).
  ctx.font = `700 ${fontPx}px sans-serif`;
  const baseWidth = ctx.measureText(text).width;
  // letter-spacing: 0.04em × number of gaps between letters
  const spacing = fontPx * 0.04 * Math.max(0, text.length - 1);
  return baseWidth + spacing;
}

function _updateCountyLabelStyles() {
  if (!countyLabelLayer || !countyLayer) return;
  const boundsByName = {};
  countyLayer.eachLayer((layer) => {
    const name = layer.feature?.properties?.name || layer.feature?.properties?.NAME;
    if (name && typeof layer.getBounds === "function") {
      boundsByName[name] = layer.getBounds();
    }
  });
  countyLabelLayer.eachLayer((marker) => {
    const el = marker.getElement?.();
    if (!el) return;
    const name = (el.textContent || "").trim().toUpperCase();
    const bounds = boundsByName[name] || boundsByName[(el.textContent || "").trim()];
    if (!bounds || !bounds.isValid()) {
      el.style.opacity = "0";
      return;
    }
    const sw = map.latLngToContainerPoint(bounds.getSouthWest());
    const ne = map.latLngToContainerPoint(bounds.getNorthEast());
    const widthPx = Math.abs(ne.x - sw.x);
    const heightPx = Math.abs(sw.y - ne.y);
    const minDim = Math.min(widthPx, heightPx);
    if (minDim < _LABEL_MIN_COUNTY_PX) {
      el.style.opacity = "0";
      return;
    }
    // Iterate from MAX down to MIN, pick the first that fits.
    let chosen = 0;
    for (let px = _LABEL_MAX_PX; px >= _LABEL_MIN_PX; px--) {
      const measured = _measureLabelWidth(name, px);
      const fitsWidth = measured <= widthPx * _LABEL_WIDTH_PAD;
      const fitsHeight = px <= heightPx * _LABEL_HEIGHT_PAD;
      if (fitsWidth && fitsHeight) {
        chosen = px;
        break;
      }
    }
    if (chosen === 0) {
      el.style.opacity = "0";
      return;
    }
    el.style.opacity = "";
    el.style.fontSize = `${chosen}px`;
  });
}

async function _apiJson(url, options = {}) {
  const resp = await fetch(url, {
    credentials: "same-origin",
    ...options,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    if (resp.status === 401) _handle401?.();
    if (resp.status === 403 && data?.code === "FORCE_PASSWORD_CHANGE_REQUIRED") _handleForcePasswordChange?.();
    const err = new Error(data?.detail || `Request failed (${resp.status})`);
    err.status = resp.status;
    err.data = data;
    throw err;
  }
  return data;
}

function _normalizeSavedAreaRow(area) {
  const polygon = Array.isArray(area?.polygon) ? area.polygon : [];
  return {
    id: String(area.area_id || area.id || ""),
    type: String(area.type || "area"),
    name: String(area.name || "Untitled"),
    share_id: String(area.share_id || ""),
    user_id: area.user_id != null ? String(area.user_id) : null,
    latlngs: polygon,
    bounds: polygon.length >= 2 ? _savedAreaBoundsFromLatLngs(polygon) : null,
    savedAt: area.updated_at || area.created_at || new Date().toISOString(),
    filter_state: area.filter_state && typeof area.filter_state === "object" ? area.filter_state : null,
    lat: Number.isFinite(Number(area.lat)) ? Number(area.lat) : null,
    lng: Number.isFinite(Number(area.lng)) ? Number(area.lng) : null,
    originator_parcel_county: String(area.originator_parcel_county || "").trim().toLowerCase() || null,
    originator_parcel_account_num: String(area.originator_parcel_account_num || "").trim() || null,
    originator_unresolved: Boolean(area.originator_unresolved),
    shared_by_username: area.shared_by_username || null,
    // Sprint 1 multi-user collab: backend returns 'owner' or 'editor' per row.
    // Drives affordance gating + sidebar indicator. Default to 'owner' so
    // pre-Sprint-1 cache entries (no role field) behave like owned rows.
    role: typeof area.role === "string" && area.role ? area.role : "owner",
  };
}

function _normalizeSavedParcelRow(parcel) {
  const payload = parcel?.payload && typeof parcel.payload === "object" ? parcel.payload : {};
  return {
    id: `parcel:${parcel.county || "dcad"}:${parcel.account_num || ""}`,
    type: "parcel",
    account_num: String(parcel.account_num || ""),
    county: String(parcel.county || payload.county || "dcad").toLowerCase(),
    name: String(payload.name || payload.addr || parcel.account_num || "Parcel"),
    lat: Number(payload.lat),
    lng: Number(payload.lng),
    geometry: payload.geometry || null,
    savedAt: parcel.created_at || new Date().toISOString(),
  };
}

function _normalizeSessionRow(session) {
  return {
    id: String(session.session_id || ""),
    session_id: String(session.session_id || ""),
    name: String(session.name || "Untitled Session"),
    parcel_count: parseInt(session.parcel_count, 10) || 0,
    county_coverage: Array.isArray(session.county_coverage) ? session.county_coverage : [],
    latlngs: Array.isArray(session.latlngs) ? session.latlngs : [],
    filter_state: session.filter_state && typeof session.filter_state === "object" ? session.filter_state : null,
    originator_parcel_county: String(session.originator_parcel_county || "").trim().toLowerCase() || null,
    originator_parcel_account_num: String(session.originator_parcel_account_num || "").trim() || null,
    savedAt: session.created_at || new Date().toISOString(),
  };
}

function _updateSaveSessionButtonState() {
  const btn = document.getElementById("btn-save-session");
  if (!btn) return;
  if (_currentSessionIsNamed) {
    btn.classList.add("hidden");
  } else {
    btn.classList.remove("hidden");
    btn.disabled = !currentJobId;
  }
}

async function _reloadSavedResources() {
  const [areasData, parcelsData, sessionsData] = await Promise.all([
    _apiJson("/api/areas"),
    _apiJson("/api/parcels").catch(() => ({ parcels: [] })),
    _apiJson("/api/sessions").catch(() => ({ sessions: [] })),
  ]);
  _savedAreasCache = Array.isArray(areasData.areas) ? areasData.areas.map(_normalizeSavedAreaRow) : [];
  _savedParcelsCache = Array.isArray(parcelsData.parcels) ? parcelsData.parcels.map(_normalizeSavedParcelRow) : [];
  _savedSessionsCache = Array.isArray(sessionsData.sessions) ? sessionsData.sessions.map(_normalizeSessionRow) : [];

  // Subject-property data: rebuild from API payload (county-aware keying).
  // Geometry cache survives — same parcel doesn't need a fresh /api/parcel fetch.
  const nextSubjectsByKey = new Map();
  const incomingSubjects = Array.isArray(areasData.subject_properties) ? areasData.subject_properties : [];
  for (const sp of incomingSubjects) {
    const key = _subjectPropertyKey(sp?.county, sp?.account_num);
    if (!key) continue;
    nextSubjectsByKey.set(key, {
      county: String(sp.county || "").trim().toLowerCase(),
      account_num: String(sp.account_num || "").trim(),
      lat: Number(sp.lat),
      lng: Number(sp.lng),
      areas: Array.isArray(sp.areas) ? sp.areas.map(a => ({
        area_id: String(a?.area_id || ""),
        name: String(a?.name || ""),
        updated_at: String(a?.updated_at || ""),
      })) : [],
    });
  }
  _subjectPropertiesByKey = nextSubjectsByKey;

  _restoreAllSavedParcelOutlines();
  renderSavedAreasList();
  renderSavedSessionsList();

  // v1.1 §2.3 — when another tab auto-saves a new subject for a loaded
  // area, _savedAreasCache here gets refreshed by the lines above, but
  // the sidebar #active-item-target-name text isn't refreshed because
  // it's only ever written by _setOriginatorTargetLabel (called only
  // from _setCurrentTargetParcel). Push the loaded area's current
  // originator through _setCurrentTargetParcel so the sidebar label
  // catches up. No-op when no area is loaded or loaded area has no
  // subject.
  //
  // KK regression 2026-06-06: this block USED to sit AFTER
  // _renderSubjectProperties() below — which meant the high-zoom "staged
  // target star" branch (frontend/map.js ~4467) rendered using the STALE
  // _currentTargetParcel before this code corrected it. Result: on
  // tab B, the outline migrated to the new subject (driven by
  // _subjectPropertiesByKey) but the star stayed pinned on the old
  // subject until the next moveend re-render. Moved the block above
  // _renderSubjectProperties so the staged identity is current at
  // render time. _setCurrentTargetParcel has no map-render side effects
  // (only sidebar label + optional coord resolve), so the move is safe.
  if (_currentLoadedAreaId) {
    const loaded = _savedAreasCache.find(
      (a) => a.id === _currentLoadedAreaId && a.type === "area",
    );
    if (loaded) {
      const c = String(loaded.originator_parcel_county || "").trim().toLowerCase();
      const a = String(loaded.originator_parcel_account_num || "").trim();
      if (c && a) {
        // Only push if the staged in-memory state has actually drifted
        // from the freshly-fetched persisted state. Otherwise this fires
        // _setOriginatorTargetLabel unnecessarily every visibility flip.
        const staged = _currentTargetParcel;
        const stagedC = staged ? String(staged.county || "").trim().toLowerCase() : null;
        const stagedA = staged ? String(staged.account || "").trim() : null;
        if (c !== stagedC || a !== stagedA) {
          // KK / Copilot debug 2026-06-06: pull lat/lng from the freshly-
          // populated _subjectPropertiesByKey (built right above from
          // the /api/areas response) so _setCurrentTargetParcel receives
          // finite coords on the same tick. Without this, the call below
          // passes only county/account → _normalizeTargetParcel stores
          // lat/lng as null → _ensureCurrentTargetParcelCoords starts an
          // async fetch → _renderSubjectProperties fires RIGHT AFTER
          // with stale (null) coords → high-zoom staged-star branch
          // skips (Number.isFinite check fails) → no star until the
          // next moveend triggers a re-render. Reader-tab specific bug:
          // outline came from _subjectPropertiesByKey (had coords) but
          // star came from _currentTargetParcel (didn't).
          const _subjectKey = _subjectPropertyKey(c, a);
          const _subjectEntry = _subjectPropertiesByKey.get(_subjectKey);
          _setCurrentTargetParcel({
            county: c,
            account: a,
            lat: _subjectEntry ? _subjectEntry.lat : undefined,
            lng: _subjectEntry ? _subjectEntry.lng : undefined,
          });
        }
      }
    }
  }

  _renderSubjectProperties();
}

// Sync the browser tab title AND the ?area=<share_id> URL param to the
// currently-loaded workspace. Called after every _currentLoadedAreaId
// assignment plus after rename paths (so the title catches up even when
// the area_id didn't change).
//
// URL sync uses history.replaceState — doesn't push a history entry, so
// the Back button still does what the user expects. When no workspace is
// loaded, the ?area= param is removed from the URL bar entirely.
function _syncTabTitle() {
  let title = "LotLedger";
  let shareId = "";
  if (_currentLoadedAreaId) {
    const area = _savedAreasCache.find((a) => String(a.id) === String(_currentLoadedAreaId));
    title = String(area?.name || "").trim() || "LotLedger";
    shareId = String(area?.share_id || "").trim();
  }
  document.title = title;
  try {
    const url = new URL(window.location.href);
    if (shareId) {
      url.searchParams.set("area", shareId);
    } else {
      url.searchParams.delete("area");
    }
    const nextRelative = url.pathname + url.search + url.hash;
    const currentRelative = window.location.pathname + window.location.search + window.location.hash;
    if (nextRelative !== currentRelative) {
      window.history.replaceState({}, "", nextRelative);
    }
  } catch (err) {
    console.warn("[syncTabTitle] URL sync failed:", err);
  }
}

// Commits a subject (originator_parcel_*) change for the loaded area to
// the backend via PUT /api/areas/{id}. Returns true if a commit happened,
// false if no drift was detected. Mutates `area` in place on success so
// subsequent calls see the persisted state.
//
// Per spec v1.1 §2.5 + §4.2. Called from saveParcel (subject auto-save).
async function _commitOriginatorToArea(area, stagedCounty, stagedAccount) {
  if (!area || area.type !== "area") return false;
  const persistedCounty = String(area.originator_parcel_county || "").trim().toLowerCase() || null;
  const persistedAccount = String(area.originator_parcel_account_num || "").trim() || null;
  if (stagedCounty === persistedCounty && stagedAccount === persistedAccount) return false;
  // Backend requires county + account together (or neither). When staged
  // is "(none, none)" the user effectively unset the subject — current
  // backend doesn't accept that, so skip.
  if (!stagedCounty || !stagedAccount) return false;
  // Sprint 1 multi-user collab (spec §3.3 + §5): subject is owner-only.
  // Editors on a shared area can save parcels (creates saved_parcels row)
  // but cannot change the area's anchor subject. Backend would 403; this
  // surfaces a friendly toast instead of a silent failure.
  if ((area.role || "owner") !== "owner") {
    _showToast("Only the owner can change this workspace's subject parcel.");
    return false;
  }
  await _apiJson(`/api/areas/${encodeURIComponent(area.id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({
      originator_parcel_county: stagedCounty,
      originator_parcel_account_num: stagedAccount,
    }),
  });
  area.originator_parcel_county = stagedCounty;
  area.originator_parcel_account_num = stagedAccount;
  return true;
}

// Mirror of _storedValueSetStatus (frontend/map.js:12256+). Drives the
// #filter-save-status chip's data-state + text. Defensive: chip element
// may not exist yet (FA4 adds it).
function _filterSaveSetStatus(state, label) {
  const chip = document.getElementById("filter-save-status");
  if (!chip) return;
  if (_filterSaveFlashTimer) {
    clearTimeout(_filterSaveFlashTimer);
    _filterSaveFlashTimer = null;
  }
  chip.setAttribute("data-state", state);
  const defaults = { idle: "", saving: "Saving…", flash: "Saved ✓", error: "Retry" };
  chip.textContent = label || defaults[state] || state;
  if (state === "flash") {
    _filterSaveFlashTimer = setTimeout(() => {
      _filterSaveFlashTimer = null;
      const c = document.getElementById("filter-save-status");
      if (c && c.getAttribute("data-state") === "flash") {
        c.setAttribute("data-state", "idle");
        c.textContent = "";
      }
    }, 1200);
  }
}

// Per spec v1 §2.3 — debounced queue + serialized PUT. Mirror of
// _storedValueQueueSave / _storedValueProcessQueue at
// frontend/map.js:12446+.

// Sprint 2 multi-user collab (spec §5): per-field PATCH queue.
// _filterSaveQueueSave diffs current state vs last-saved snapshot and
// enqueues only changed fields. _filterSaveProcessQueue drains the queue
// serially, sending one PATCH per (field_key, value) with a monotonic
// client_seq. LWW reconciliation if the server's persisted seq is higher
// (defers to blur via _applyFilterFieldToUI if user is still typing).

function _filterSaveQueueSave() {
  if (_suppressFilterAutosave) return;  // restore/switch is load-only, never save
  if (!_currentLoadedAreaId) return;
  // Diff captureFilterState() against last-saved snapshot, enqueue changed fields.
  const current = captureFilterState();
  const changed = _diffFilterState(current, _filterSaveLastSnapshot);
  if (changed.length === 0) return;
  for (const [fieldKey, value] of changed) {
    _filterSavePendingFields.set(fieldKey, value);
  }
  if (_filterSaveDebounceTimer) clearTimeout(_filterSaveDebounceTimer);
  _filterSaveDebounceTimer = setTimeout(() => {
    _filterSaveDebounceTimer = null;
    void _filterSaveProcessQueue();
  }, _FILTER_SAVE_DEBOUNCE_MS);
}

async function _filterSaveProcessQueue() {
  if (_filterSaveInflight) return;
  if (_filterSavePendingFields.size === 0) return;
  if (!_currentLoadedAreaId) { _filterSavePendingFields.clear(); return; }
  const areaId = _currentLoadedAreaId;
  _filterSaveInflight = true;
  _pendingFilterSaves++;
  _filterSaveSetStatus("saving");
  // Snapshot + drain. New enqueues during this flight accumulate in the
  // cleared map for the next flush.
  const toFlush = Array.from(_filterSavePendingFields.entries());
  _filterSavePendingFields.clear();
  let hadError = false;
  try {
    for (let i = 0; i < toFlush.length; i++) {
      const [fieldKey, value] = toFlush[i];
      // Sprint 3 hotfix (2026-06-02): use Date.now() as the seq base so
      // _filterSaveClientSeq survives refreshes/new sessions. Pre-hotfix
      // every refresh reset to 0, immediately losing every LWW race
      // against persisted seqs. Date.now() guarantees monotonic across
      // all sessions/browsers; Math.max protects against multiple
      // dispatches within the same millisecond (rare but possible).
      _filterSaveClientSeq = Math.max(Date.now(), _filterSaveClientSeq + 1);
      const seq = _filterSaveClientSeq;
      try {
        const resp = await _apiJson(
          `/api/areas/${encodeURIComponent(areaId)}/filter-fields/${encodeURIComponent(fieldKey)}`,
          {
            method: "PATCH",
            headers: {
              "Content-Type": "application/json",
              "X-Session-Id": _sseSessionUuid,  // Sprint 3: echoed in NOTIFY for self-echo filter
              ...authHeaders(),
            },
            body: JSON.stringify({ value, client_seq: seq }),
          }
        );
        // Sprint 3 §4.5: track dispatched seq per field for self-echo filter.
        _dispatchedSeqByField.set(fieldKey, seq);
        // LWW reconciliation: if server's persisted client_seq > our outgoing
        // seq, a concurrent write beat us. Apply the server value to UI
        // (deferred to blur per anti-flicker rule §5.5).
        const serverSeq = Number(resp.client_seq) || 0;
        if (serverSeq > seq) {
          _applyFilterFieldToUI(fieldKey, resp.value);
        }
      } catch (err) {
        hadError = true;
        console.error("[filter-autosave] PATCH failed", fieldKey, err);
        const status = (err && typeof err === "object") ? Number(err.status || 0) : 0;
        if (status === 404) {
          // Sprint 2 §5.6: distinguish route-not-found (new frontend / old
          // backend during rolling deploy) from area-deleted-by-owner via
          // response detail body.
          const detail = (err && err.data && err.data.detail) || "";
          const isAreaDeleted = /saved area not found/i.test(detail);
          if (!isAreaDeleted) {
            _filterSavePendingFields.clear();
            _filterSaveSetStatus("idle");
            _showToast("Filter save failed. Your browser may need a refresh — the app updated.");
            return;
          }
          // Sprint 1 §3.4 area-deleted-by-owner recovery
          const deletedId = areaId;
          _currentLoadedAreaId = null;
          const idx = _savedAreasCache.findIndex((a) => a.id === deletedId);
          if (idx !== -1) _savedAreasCache.splice(idx, 1);
          try { clearActiveItem(); } catch (_) {}
          try { renderSavedAreasList(); } catch (_) {}
          _filterSaveSetStatus("idle");
          _showToast("This area was deleted by the owner.");
          return;
        }
        if (status === 403) {
          _filterSaveSetStatus("idle");
          _showToast("You can view this area but only the owner can change it.");
          return;
        }
        if (status === 400) {
          // Allowlist rejection or other validation failure. Re-enqueuing
          // would loop forever — drop THIS field, keep flushing the rest.
          // Surface to console for diagnosis; chip stays in "saving" until
          // the loop exits via either flash or a real transient error.
          console.warn("[filter-autosave] PATCH rejected (400), dropping field", fieldKey, err && err.data);
          continue;
        }
        // Other transient error — re-enqueue current + remaining fields for retry.
        if (!_filterSavePendingFields.has(fieldKey)) {
          _filterSavePendingFields.set(fieldKey, value);
        }
        for (let j = i + 1; j < toFlush.length; j++) {
          const [k, v] = toFlush[j];
          if (!_filterSavePendingFields.has(k)) _filterSavePendingFields.set(k, v);
        }
        _filterSaveSetStatus("error", "Retry");
        return;
      }
    }
    // All flushed successfully — update last-saved snapshot baseline.
    _filterSaveLastSnapshot = captureFilterState();
    _filterSaveSetStatus("flash");
  } finally {
    _filterSaveInflight = false;
    _pendingFilterSaves = Math.max(0, _pendingFilterSaves - 1);
    if (!hadError && _filterSavePendingFields.size > 0) void _filterSaveProcessQueue();
  }
}

async function _filterSaveFlushPending() {
  if (_filterSaveDebounceTimer) {
    clearTimeout(_filterSaveDebounceTimer);
    _filterSaveDebounceTimer = null;
  }
  if (_filterSavePending) {
    await _filterSaveProcessQueue();
  }
}

async function _filterSaveOnAreaChange(_newAreaId) {
  // Called BEFORE _currentLoadedAreaId changes. Flush any pending save
  // for the outgoing area so the changes commit before we swap state.
  // Mirror of _storedValueOnAreaChange at frontend/map.js:12314+.
  await _filterSaveFlushPending().catch(() => {});
}

// Retry click handler (spec v1 §2.2). Mirror of stored-values retry
// at frontend/map.js:12545+.
(function _filterSaveWireRetryClick() {
  const wire = () => {
    const status = document.getElementById("filter-save-status");
    if (!status) return;
    status.addEventListener("click", () => {
      if (status.getAttribute("data-state") !== "error") return;
      _filterSaveSetStatus("saving");
      _filterSavePending = true;
      void _filterSaveProcessQueue();
    });
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire, { once: true });
  } else {
    wire();
  }
})();


function _savedAreaBoundsFromLatLngs(latlngs) {
  let minLat = Infinity, maxLat = -Infinity, minLng = Infinity, maxLng = -Infinity;
  for (const [lat, lng] of latlngs) {
    if (lat < minLat) minLat = lat;
    if (lat > maxLat) maxLat = lat;
    if (lng < minLng) minLng = lng;
    if (lng > maxLng) maxLng = lng;
  }
  return [[minLat, minLng], [maxLat, maxLng]];
}

async function saveCurrentArea(name) {
  if (!lastDrawnLatLngs) return;
  const trimmed = String(name || "").trim();
  if (!trimmed) return;
  bumpUndoPillVersion();
  const origin = _currentTargetParcel;
  const created = await _apiJson("/api/areas", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({
      name: trimmed,
      type: "area",
      polygon: lastDrawnLatLngs,
      filter_state: captureFilterState(),
      job_id: currentJobId || null,
      ...(origin ? {
        originator_parcel_county: origin.county,
        originator_parcel_account_num: origin.account,
      } : {}),
    }),
  });
  const normalized = _normalizeSavedAreaRow(created);
  _savedAreasCache.unshift(normalized);
  // Mark the just-saved area as the currently-loaded one so the Update button
  // becomes available the moment the user tweaks any filter after saving.
  _clearOriginatorStar();
  _setCurrentTargetParcel(null);
  _currentLoadedAreaId = normalized.id;
  _renderViewToggle();  // area id set → reveal the ARV/NBV/Export toggle
    _updateActiveItemRenameVisibility();  // area id set → reveal rename pencil + share button (idempotent)
  _syncTabTitle();
  _storedValueOnAreaChange(_currentLoadedAreaId);
  void _filterSaveOnAreaChange(_currentLoadedAreaId);
  _selectedSavedItemId = normalized.id;
  // Render the originator star immediately if captured at save time.
  // Use lat/lng from origin (the pre-save _currentTargetParcel) to skip
  // the /api/parcel fetch round-trip since we already have coordinates.
  if (normalized.originator_parcel_county && normalized.originator_parcel_account_num) {
    _setCurrentTargetParcel({
      county: normalized.originator_parcel_county,
      account: normalized.originator_parcel_account_num,
      lat: origin?.lat,
      lng: origin?.lng,
    });
    void _renderOriginatorTargetStar(
      normalized.originator_parcel_county,
      normalized.originator_parcel_account_num,
      origin?.lat,
      origin?.lng,
    );
  }
  setActiveItem("Workspace", normalized.name);
  renderSavedAreasList();
  // If there's already a Propelio pull on screen for this polygon, attach
  // the existing comp list to the new saved area's archive so the comps
  // pick up stable comp_address_keys. We AWAIT this so the user can't
  // race a filter change against the merge — the buttons must be live by
  // the time saveCurrentArea returns.
  if (window._propelioLast && Array.isArray(window._propelioLast.comps) && window._propelioLast.comps.length) {
    await _reattachPropelioToSavedArea(normalized.id);
  }
  // Refetch saved resources so subject_properties picks up the new area's
  // originator. POST /api/areas only returns the new area — it doesn't
  // recompute the user's subject-property aggregate, so without this
  // refetch the new copy's gold outline + star wouldn't appear (and
  // wouldn't appear on the original either after a Copy Area As, since
  // the new copy shares the same originator and is now most-recent).
  await _reloadSavedResources().catch((err) =>
    console.warn("[saveCurrentArea] post-save resource reload failed:", err)
  );
  // The reload populates share_id from the server (the POST create response may
  // omit it), so re-check visibility now to reveal the share button on a freshly
  // drawn/saved area.
  _updateActiveItemRenameVisibility();
}

// On saved-area load: pull the archived comps for that workspace from
// the session DB and rehydrate the propelio panel. No Propelio quota
// hit, no scrape — just a DB read of comps the workspace already
// owns. Comps come back with comp_address_key + user_rating already
// stamped, so good/bad/dim states restore exactly as the user left
// them. Empty archive → leave UI quiet (no error noise).
async function _hydratePropelioFromArchive(savedAreaId) {
  if (!savedAreaId) return;
  // Reset any prior workspace's propelio state first.
  window._propelioLast = null;
  _updatePropelioStatusCounts();
  propelioCompLayer.clearLayers();
  propelioCompLayerByKey.clear();
  renderPropelioCompList([]);
  propelioCmaChip.hide();
  const countEl = document.getElementById("propelio-filter-count");
  if (countEl) countEl.textContent = "";

  try {
    const resp = await fetch(
      `/api/propelio/by-saved-area?saved_area_id=${encodeURIComponent(savedAreaId)}`,
      { headers: { ...authHeaders() } },
    );
    if (!resp.ok) {
      console.warn("[propelio] hydrate from archive failed:", resp.status);
      return;
    }
    const data = await resp.json();
    const comps = Array.isArray(data?.comps) ? data.comps : [];
    if (!comps.length) return;
    window._propelioLast = { comps };
    _updatePropelioStatusCounts();
    applyPropelioClientFilters();
  } catch (err) {
    console.error("[propelio] hydrate error:", err);
  }
}

async function _reattachPropelioToSavedArea(savedAreaId) {
  if (!savedAreaId) return;
  if (!window._propelioLast || !Array.isArray(window._propelioLast.comps) || !window._propelioLast.comps.length) return;
  try {
    const resp = await fetch("/api/propelio/attach-to-area", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({
        saved_area_id: savedAreaId,
        // Strip the client-internal projection transient `_ratingArv` before it
        // goes over the wire — it must never be persisted in the archive blob
        // (per-view ratings Chunk E; would poison the lazy-capture on reload).
        comps: window._propelioLast.comps.map((c) => {
          if (c && c._ratingArv !== undefined) {
            const { _ratingArv, ...rest } = c;
            return rest;
          }
          return c;
        }),
      }),
    });
    if (!resp.ok) {
      console.warn("[propelio] attach-to-area failed:", resp.status);
      return;
    }
    const data = await resp.json();
    window._propelioLast = {
      ...window._propelioLast,
      comps: Array.isArray(data?.comps) ? data.comps : window._propelioLast.comps,
      archive_meta: data?.archive_meta || null,
    };
    _updatePropelioStatusCounts();
    applyPropelioClientFilters();
  } catch (err) {
    console.error("[propelio] attach-to-area error:", err);
  }
}

async function deleteSavedArea(item) {
  if (!item) return;
  bumpUndoPillVersion();
  if (item.type === "parcel") {
    await _apiJson(`/api/parcels/${encodeURIComponent(item.county || "dcad")}/${encodeURIComponent(item.account_num || "")}`, {
      method: "DELETE",
      headers: { ...authHeaders() },
    });
    _savedParcelsCache = _savedParcelsCache.filter((p) => !(p.account_num === item.account_num && p.county === item.county));
    const layer = savedParcelLayers[item.account_num];
    if (layer) {
      savedParcelLayer.removeLayer(layer);
      delete savedParcelLayers[item.account_num];
    }
    _removeSavedTargetStar(item.account_num);
    const clickLayer = savedParcelClickLayers[item.account_num];
    if (clickLayer) {
      savedParcelClickLayer.removeLayer(clickLayer);
      delete savedParcelClickLayers[item.account_num];
    }
  } else {
    await _apiJson(`/api/areas/${encodeURIComponent(item.id)}`, {
      method: "DELETE",
      headers: { ...authHeaders() },
    });
    _savedAreasCache = _savedAreasCache.filter((a) => a.id !== item.id);
    if (_currentLoadedAreaId === item.id) {
      _clearOriginatorStar();
      _setCurrentTargetParcel(null);
      _currentLoadedAreaId = null;
      _syncTabTitle();
      _storedValueOnAreaChange(null);
      void _filterSaveOnAreaChange(null);
    }
    // Refetch subject_properties so the deleted area's originator drops
    // off the gold-outline / star layer (if it was the only area for that
    // parcel) or is removed from the popup's area dropdown (if multi).
    await _reloadSavedResources().catch((err) =>
      console.warn("[deleteSavedArea] post-delete resource reload failed:", err)
    );
  }
  if (_selectedSavedItemId === item.id) _selectedSavedItemId = null;
  renderSavedAreasList();
}

async function saveSearchLocation(name, lat, lng) {
  bumpUndoPillVersion();
  const created = await _apiJson("/api/areas", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({
      name,
      type: "location",
      lat,
      lng,
      filter_state: null,
    }),
  });
  _savedAreasCache.unshift(_normalizeSavedAreaRow(created));
  renderSavedAreasList();
}

async function saveCurrentSession(name) {
  if (!currentJobId) return;
  const trimmed = String(name || "").trim();
  if (!trimmed) return;
  bumpUndoPillVersion();
  const created = await _apiJson(`/api/sessions/${encodeURIComponent(currentJobId)}/save`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ name: trimmed, filter_state: captureFilterState() }),
  });
  const normalized = _normalizeSessionRow(created);
  _savedSessionsCache = _savedSessionsCache.filter((s) => s.session_id !== normalized.session_id);
  _savedSessionsCache.unshift(normalized);
  _currentSessionIsNamed = true;
  _updateSaveSessionButtonState();
  setActiveItem("Snapshot", normalized.name);
  renderSavedSessionsList();
}

async function deleteSession(session) {
  if (!session) return;
  bumpUndoPillVersion();
  // Optimistic: remove from cache immediately, roll back on failure
  _savedSessionsCache = _savedSessionsCache.filter((s) => s.session_id !== session.session_id);
  if (_currentSessionIsNamed && currentJobId === session.session_id) {
    _currentSessionIsNamed = false;
    _updateSaveSessionButtonState();
  }
  renderSavedSessionsList();
  try {
    await _apiJson(`/api/sessions/${encodeURIComponent(session.session_id)}`, {
      method: "DELETE",
      headers: { ...authHeaders() },
    });
  } catch (err) {
    console.error("[deleteSession] failed, rolling back", err);
    _savedSessionsCache.push(session);
    _savedSessionsCache.sort((a, b) => new Date(b.savedAt) - new Date(a.savedAt));
    renderSavedSessionsList();
  }
}

async function _renameSavedSessionInline(session, rowEl) {
  if (!session) return;
  const nameEl = rowEl.querySelector(".saved-area-name");
  if (!nameEl) return;
  const input = document.createElement("input");
  input.className = "saved-area-rename-input";
  input.value = session.name || "";
  nameEl.replaceWith(input);
  input.focus();
  input.select();

  const cancel = () => renderSavedSessionsList();
  const save = async () => {
    const nextName = String(input.value || "").trim();
    if (!nextName || nextName === session.name) {
      cancel();
      return;
    }
    bumpUndoPillVersion();
    // Optimistic: update cache immediately, reload on failure
    session.name = nextName;
    renderSavedSessionsList();
    try {
      await _apiJson(`/api/sessions/${encodeURIComponent(session.session_id)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ name: nextName }),
      });
    } catch (err) {
      console.error("[renameSavedSession] failed, reloading", err);
      await _reloadSavedResources();
    }
  };

  input.addEventListener("keydown", async (e) => {
    if (e.key === "Escape") { e.preventDefault(); cancel(); }
    else if (e.key === "Enter") { e.preventDefault(); await save(); }
  });
  input.addEventListener("blur", () => { if (!input.value.trim()) cancel(); });
}

const SAVED_PARCEL_COLOR = "#FFD700";
const savedTargetStarIcon = L.divIcon({
  className: "saved-target-star",
  html: '<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" focusable="false"><path d="M12 1.8l3.16 6.4 7.06 1.03-5.11 4.98 1.2 7.04L12 17.93 5.69 21.25l1.2-7.04-5.11-4.98 7.06-1.03L12 1.8z" fill="#e2c075" stroke="#8b6b1f" stroke-width="1.2"/></svg>',
  iconSize: [22, 22],
  iconAnchor: [11, 11],
});

function _clearOriginatorStar() {
  if (_originatorStarMarker) {
    _ORIGINATOR_STAR_LAYER.removeLayer(_originatorStarMarker);
    _originatorStarMarker = null;
  }
  // Clear bonded-saved-parcels cache too. If the caller is about to load a
  // different area, _renderOriginatorTargetStar will fire and re-populate.
  // If the caller is clearing the workspace entirely, this leaves the
  // bonded-glow layer empty as desired.
  if (typeof _loadedAreaSeedParcelsByKey !== "undefined") {
    _loadedAreaSeedParcelsByKey = new Map();
  }
  // Refresh subject-property layer in case the loaded area changed. Cheap —
  // just rebuilds from cached data.
  if (typeof _renderSubjectProperties === "function") _renderSubjectProperties();
}

async function _renderOriginatorTargetStar(_county, _account, _lat, _lng) {
  // Legacy entry point retained for the 6+ call sites that fire on
  // saveParcel / restoreSavedArea / fork / session-restore. In the
  // subject-property redesign, the loaded area's star is rendered by
  // _renderSubjectProperties() via the `is-loaded` modifier, driven by the
  // payload's lat/lng — so this function no longer creates a Leaflet marker
  // itself. It only triggers a layer refresh so the loaded-state changes
  // propagate immediately rather than waiting for the next zoom/reload.
  if (_originatorStarMarker) {
    _ORIGINATOR_STAR_LAYER.removeLayer(_originatorStarMarker);
    _originatorStarMarker = null;
  }
  if (typeof _renderSubjectProperties === "function") _renderSubjectProperties();
  // Refresh bonded-saved-parcels-of-loaded-area cache too. This function
  // is the canonical "loaded area changed" hook — it fires after
  // _currentLoadedAreaId has been set in restoreSavedArea/saveCurrentArea
  // /fork paths, so it always sees the new ID.
  if (typeof _loadAreaSeedParcelsByKey === "function") {
    void _loadAreaSeedParcelsByKey(_currentLoadedAreaId);
  }
}

function _updateSavedTargetStarVisibility() {
  // Regular gold target stars: zoom-gated. Purpose is wide-view nav so
  // they only show when zoomed out far enough that parcel outlines
  // aren't readable. Above the threshold the parcel outlines themselves
  // are the visible markers.
  if (map.getZoom() < SAVED_TARGET_STAR_MAX_ZOOM) {
    if (!map.hasLayer(savedTargetStarLayer)) map.addLayer(savedTargetStarLayer);
  } else if (map.hasLayer(savedTargetStarLayer)) {
    map.removeLayer(savedTargetStarLayer);
  }
  // Originator star is NOT zoom-gated. Its purpose is to distinguish
  // THE intended target from other gold-saved targets in the same
  // workspace at every zoom level — the user always needs to know
  // which parcel was the originator regardless of view scale.
  if (!map.hasLayer(_ORIGINATOR_STAR_LAYER)) map.addLayer(_ORIGINATOR_STAR_LAYER);
}

// === Subject Property render functions ===
// Single entry point: _renderSubjectProperties() rebuilds stars + outlines
// from _subjectPropertiesByKey based on current zoom and _currentLoadedAreaId.
// Called from _reloadSavedResources, zoomend, and area-load mutation sites.
// Cheap to rerun — geometry is cached so no extra fetches on simple rerender.

function _isSubjectPropertyLoaded(entry) {
  if (!_currentLoadedAreaId || !entry || !Array.isArray(entry.areas)) return false;
  return entry.areas.some(a => String(a.area_id) === String(_currentLoadedAreaId));
}

// Returns the (county, account) of the currently-staged subject — the parcel
// whose star carries the `is-loaded` modifier. Tracks _currentTargetParcel
// (which the user can stage by hitting Save Parcel inside a loaded area)
// rather than the area's persisted originator. The Update button commits
// any drift; Copy Area As ships the staged value as the new area's
// originator.
function _stagedSubjectIdentity() {
  if (!_currentTargetParcel) return null;
  const c = String(_currentTargetParcel.county || "").trim().toLowerCase();
  const a = String(_currentTargetParcel.account || "").trim();
  if (!c || !a) return null;
  return { county: c, account_num: a, lat: _currentTargetParcel.lat, lng: _currentTargetParcel.lng };
}

// Fetch GET /api/areas/{id} for its seed_parcels (bonded saved_parcels of
// that area) and populate _loadedAreaSeedParcelsByKey. Renders an extra
// pass of subject-property outlines so these parcels show gold at zoom
// >= 14 alongside the persisted originator. Called from area-load paths.
async function _loadAreaSeedParcelsByKey(areaId) {
  _loadedAreaSeedParcelsByKey = new Map();
  if (!areaId) return;
  try {
    const detail = await _apiJson(`/api/areas/${encodeURIComponent(areaId)}`);
    const seedParcels = Array.isArray(detail?.seed_parcels) ? detail.seed_parcels : [];
    for (const sp of seedParcels) {
      const payload = sp?.payload && typeof sp.payload === "object" ? sp.payload : {};
      const county = String(sp?.county || payload.county || "dcad").trim().toLowerCase();
      const account = String(sp?.account_num || "").trim();
      const key = _subjectPropertyKey(county, account);
      if (!key) continue;
      const lat = Number(payload.lat);
      const lng = Number(payload.lng);
      _loadedAreaSeedParcelsByKey.set(key, {
        county,
        account_num: account,
        lat: Number.isFinite(lat) ? lat : NaN,
        lng: Number.isFinite(lng) ? lng : NaN,
        // Synthetic areas[] keyed to the loaded area so the click handler
        // can still surface the Load Area popup section with that area's
        // name as the hover label.
        areas: [{ area_id: areaId, name: "", updated_at: "" }],
      });
    }
  } catch (err) {
    console.warn("[loadAreaSeedParcels] fetch failed for", areaId, err);
  }
  if (typeof _renderSubjectProperties === "function") _renderSubjectProperties();
}

function _subjectPropertyHoverLabel(entry) {
  // Backend pre-sorts entry.areas by updated_at DESC, so areas[0] is the
  // most-recently-updated area whose subject this is. Per spec, hover shows
  // that one's name only.
  if (!entry || !Array.isArray(entry.areas) || entry.areas.length === 0) return "";
  const top = entry.areas[0];
  return String(top?.name || "").trim() || "Saved area";
}

function _renderSubjectProperties() {
  // Full rebuild — clears existing markers + outlines and re-renders from
  // current data + zoom + loaded-area state + staged target. Geometry
  // cache survives clears so outline re-renders don't refetch.
  subjectPropertyStarLayer.clearLayers();
  subjectPropertyOutlineLayer.clearLayers();
  _subjectPropertyStarMarkers.clear();
  _subjectPropertyOutlineLayers.clear();

  const zoom = map.getZoom();
  const lowZoom = zoom < SUBJECT_PROPERTY_STAR_MAX_ZOOM;
  // Viewport gate at high zoom: only fetch + render outlines for parcels
  // whose centroid falls inside the current viewport. Without this, a user
  // with 200+ saved areas arriving at zoom 14+ would trigger 200+
  // /api/parcel fetches on a single render.
  const bounds = !lowZoom ? map.getBounds() : null;

  // Staged subject — the parcel whose star carries `is-loaded`. Tracks
  // _currentTargetParcel so Save Parcel inside a loaded area moves the
  // star visually, even before the change is committed via Update (or
  // shipped via Copy Area As).
  const staged = _stagedSubjectIdentity();
  const stagedKey = staged ? _subjectPropertyKey(staged.county, staged.account_num) : null;

  // v1.1 §2.2 — suppression rule: when a saved area is loaded, only THAT
  // area's subject shows gold + star + parcel highlight globally. All
  // other saved-area subjects go dormant on the map (sidebar list is
  // unaffected — separate UI surface). Restored on Clear / area switch
  // / no-area-loaded.
  const _loadedAreaId = _currentLoadedAreaId;
  const _loadedAreaSubjectKey = _loadedAreaId
    ? (() => {
        const loaded = _savedAreasCache.find((a) => a.id === _loadedAreaId && a.type === "area");
        if (!loaded) return null;
        const c = String(loaded.originator_parcel_county || "").trim().toLowerCase();
        const a = String(loaded.originator_parcel_account_num || "").trim();
        return c && a ? _subjectPropertyKey(c, a) : null;
      })()
    : null;

  // Outlines: every persisted subject_property in viewport gets one.
  for (const entry of _subjectPropertiesByKey.values()) {
    // Suppression: skip non-loaded-area subjects when an area is loaded.
    if (_loadedAreaId && _loadedAreaSubjectKey && _subjectPropertyKey(entry.county, entry.account_num) !== _loadedAreaSubjectKey) continue;
    if (!lowZoom && bounds
        && Number.isFinite(entry.lat) && Number.isFinite(entry.lng)
        && bounds.contains([entry.lat, entry.lng])) {
      _renderSubjectPropertyOutlineLazy(entry);
    }
  }

  // Outlines: bonded saved-parcels of the currently-loaded area. With
  // the v1.1 §2.2 suppression rule, ONLY the parcel matching the loaded
  // area's current subject (originator_parcel_*) gets gold here. Earlier
  // bonded saved-parcels from prior Save Parcel clicks (now superseded
  // by auto-save) stay invisible on the map. The first loop already
  // renders the persisted subject; this loop covers the brief window
  // before the next _reloadSavedResources lands _subjectPropertiesByKey
  // for a freshly-set originator.
  if (!lowZoom && _currentLoadedAreaId) {
    for (const sp of _loadedAreaSeedParcelsByKey.values()) {
      if (!bounds) continue;
      if (!Number.isFinite(sp.lat) || !Number.isFinite(sp.lng)) continue;
      if (!bounds.contains([sp.lat, sp.lng])) continue;
      // Suppression: skip seed parcels that aren't the loaded area's
      // current subject. Prevents stale-gold pileup when Mike clicks
      // Save Parcel multiple times in the same loaded area.
      if (_loadedAreaSubjectKey && _subjectPropertyKey(sp.county, sp.account_num) !== _loadedAreaSubjectKey) continue;
      // Dedupe against any subject_property already rendered at this key.
      const key = _subjectPropertyKey(sp.county, sp.account_num);
      if (key && _subjectPropertyOutlineLayers.has(key)) continue;
      _renderSubjectPropertyOutlineLazy(sp);
    }
  }

  // Stars at low zoom: every persisted subject_property gets a regular star.
  // The staged target (if it's not already a persisted subject) gets its own
  // star with `is-loaded` so the user sees their pending change.
  if (lowZoom) {
    for (const entry of _subjectPropertiesByKey.values()) {
      // Suppression: skip non-loaded-area subjects when an area is loaded.
      if (_loadedAreaId && _loadedAreaSubjectKey && _subjectPropertyKey(entry.county, entry.account_num) !== _loadedAreaSubjectKey) continue;
      const isStaged = stagedKey != null && _subjectPropertyKey(entry.county, entry.account_num) === stagedKey;
      _renderSubjectPropertyStarMarker(entry, isStaged);
    }
    if (staged && stagedKey && !_subjectPropertiesByKey.has(stagedKey)) {
      _renderSubjectPropertyStarMarker({
        county: staged.county,
        account_num: staged.account_num,
        lat: staged.lat,
        lng: staged.lng,
        areas: [],
      }, true);
    }
    return;
  }

  // Stars at high zoom: ONLY the staged target gets a star (with is-loaded).
  // Persisted subject_properties of other areas stay outline-only at this
  // zoom — visual focus is on the loaded area.
  if (staged && Number.isFinite(staged.lat) && Number.isFinite(staged.lng)) {
    // Use the persisted entry if present (so the click handler has the
    // full areas[] for the Load Area dropdown); otherwise synthesize a
    // minimal entry so the marker renders.
    const persistedEntry = stagedKey ? _subjectPropertiesByKey.get(stagedKey) : null;
    const entryForMarker = persistedEntry || {
      county: staged.county,
      account_num: staged.account_num,
      lat: staged.lat,
      lng: staged.lng,
      areas: [],
    };
    _renderSubjectPropertyStarMarker(entryForMarker, true);
  }
}

function _renderSubjectPropertyStarMarker(entry, isLoaded) {
  const key = _subjectPropertyKey(entry.county, entry.account_num);
  if (!key) return;
  if (!Number.isFinite(entry.lat) || !Number.isFinite(entry.lng)) return;

  const className = isLoaded
    ? "subject-property-star is-loaded"
    : "subject-property-star";

  const icon = L.divIcon({
    className,
    html: '<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" focusable="false"><path d="M12 1.8l3.16 6.4 7.06 1.03-5.11 4.98 1.2 7.04L12 17.93 5.69 21.25l1.2-7.04-5.11-4.98 7.06-1.03L12 1.8z" fill="#e2c075" stroke="#8b6b1f" stroke-width="1.2"/></svg>',
    iconSize: [22, 22],
    iconAnchor: [11, 11],
  });

  const marker = L.marker([entry.lat, entry.lng], {
    icon,
    pane: "savedTargetStarPane",
  });

  const hoverLabel = _subjectPropertyHoverLabel(entry);
  if (hoverLabel) {
    marker.bindTooltip(hoverLabel, { direction: "top", offset: [0, -10] });
  }

  marker.on("click", (ev) => _onSubjectPropertyClick(entry, ev));
  marker.addTo(subjectPropertyStarLayer);
  _subjectPropertyStarMarkers.set(key, marker);
}

function _renderSubjectPropertyOutlineLazy(entry) {
  const key = _subjectPropertyKey(entry.county, entry.account_num);
  if (!key) return;
  if (_subjectPropertyOutlineLayers.has(key)) return;

  const cached = _subjectPropertyGeometryCache.get(key);
  if (cached) {
    _renderSubjectPropertyOutlineFromGeometry(entry, cached);
    return;
  }

  if (_subjectPropertyGeometryInFlight.has(key)) return;
  _subjectPropertyGeometryInFlight.add(key);

  fetch(`/api/parcel/${encodeURIComponent(entry.county)}/${encodeURIComponent(entry.account_num)}`)
    .then(resp => resp.ok ? resp.json() : null)
    .then(detail => {
      _subjectPropertyGeometryInFlight.delete(key);
      if (!detail || !detail.geometry) return;
      _subjectPropertyGeometryCache.set(key, detail.geometry);
      if (map.getZoom() < SUBJECT_PROPERTY_STAR_MAX_ZOOM) return;
      if (!_subjectPropertiesByKey.has(key)) return;
      _renderSubjectPropertyOutlineFromGeometry(entry, detail.geometry);
    })
    .catch(err => {
      _subjectPropertyGeometryInFlight.delete(key);
      console.warn("[subject-property] geometry fetch failed", key, err);
    });
}

function _renderSubjectPropertyOutlineFromGeometry(entry, geometry) {
  const key = _subjectPropertyKey(entry.county, entry.account_num);
  if (!key) return;
  if (_subjectPropertyOutlineLayers.has(key)) return;
  if (!geometry || (geometry.type !== "Polygon" && geometry.type !== "MultiPolygon")) return;

  // Match the bold .saved-parcel-glow look from main (weight 6 + gold fill
  // at 16% alpha) so subject-property outlines are clearly visible from
  // zoom 14 onward, not a faint stroke. Per KK 2026-05-26 UX call.
  const layer = L.geoJSON({ type: "Feature", geometry, properties: {} }, {
    pane: "subjectPropertyOutlinePane",
    className: "subject-property-outline",
    style: {
      color: SAVED_PARCEL_COLOR,
      weight: 6,
      fill: true,
      fillColor: SAVED_PARCEL_COLOR,
      fillOpacity: 0.16,
      interactive: true,
    },
    interactive: true,
  });

  const hoverLabel = _subjectPropertyHoverLabel(entry);
  if (hoverLabel) {
    layer.bindTooltip(hoverLabel, { sticky: true, direction: "top" });
  }

  layer.on("click", (ev) => _onSubjectPropertyClick(entry, ev));
  layer.addTo(subjectPropertyOutlineLayer);
  _subjectPropertyOutlineLayers.set(key, layer);
}

async function _onSubjectPropertyClick(entry, ev) {
  if (ev && ev.originalEvent) L.DomEvent.stopPropagation(ev);
  const c = entry.county;
  const a = entry.account_num;
  if (_handleMeasureInteraction(ev?.latlng, { county: c, account_num: a })) return;

  // Honor btn-zoom-toggle (jump mode flies to parcel first).
  if (currentClickMode === "jump" && map.getZoom() < 16) {
    map.flyTo([entry.lat, entry.lng], 16, { duration: 0.35 });
  }

  try {
    const resp = await fetch(`/api/parcel/${encodeURIComponent(c)}/${encodeURIComponent(a)}`);
    if (!resp.ok) return;
    const detail = await resp.json();
    openParcelDetailPanel(detail.properties || detail, {
      latlng: ev?.latlng,
      geometry: detail.geometry,
      subjectPropertyEntry: entry,
    });
  } catch (err) {
    console.error("[subject-property] popup open failed:", err);
  }
}

// Load Area button delegation — works for both panel + popup since both
// render the same .subject-property-load-btn markup via the shared helper.
document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest(".subject-property-load-btn");
  if (!btn || btn.disabled) return;
  ev.preventDefault();
  ev.stopPropagation();
  const areaId = btn.getAttribute("data-area-id") || "";
  if (!areaId) return;
  const area = _savedAreasCache.find(a => String(a.id) === areaId);
  if (!area) {
    console.warn("[subject-property] Load Area clicked but area not in cache:", areaId);
    return;
  }
  // Mirror the guarded sidebar-row-click workspace-switch path. Copilot
  // audit flagged that calling restoreSavedArea directly skipped the
  // deep-pull guard, undo snapshot, and undo pill — silent footgun if a
  // user clicks Load Area mid-Quick-Sweep.
  if (!_navigationGuardForActiveDeepPull("switch workspaces")) {
    return;
  }
  _selectedSavedItemId = area.id;
  _clearSelectedOutline();
  document.querySelectorAll(".saved-area-row-active").forEach((el) => el.classList.remove("saved-area-row-active"));
  const matchingRow = document.querySelector(`.saved-area-row[data-id="${CSS.escape(area.id)}"]`);
  if (matchingRow) matchingRow.classList.add("saved-area-row-active");
  bumpUndoPillVersion();
  const snapshot = _createUndoSnapshot();
  try {
    await restoreSavedArea(area, { rowEl: matchingRow || null, undoSnapshot: snapshot });
    const restoredCount = _countRestoredFilterKeys(area.filter_state);
    _showUndoPill(snapshot, restoredCount);
  } catch (err) {
    console.error("[subject-property] restoreSavedArea failed:", err);
  }
});

// Multi-area dropdown change — sync button state + data-area-id to whichever
// area is currently selected.
document.addEventListener("change", (ev) => {
  const sel = ev.target.closest(".subject-property-load-select");
  if (!sel) return;
  const section = sel.closest(".subject-property-load-section");
  if (!section) return;
  const btn = section.querySelector(".subject-property-load-btn");
  if (!btn) return;
  const selectedId = String(sel.value || "");
  const loadedId = String(_currentLoadedAreaId || "");
  btn.setAttribute("data-area-id", selectedId);
  if (selectedId && selectedId === loadedId) {
    btn.disabled = true;
    btn.textContent = "Currently loaded";
  } else {
    btn.disabled = false;
    btn.textContent = "Load Area";
  }
});

function _removeSavedTargetStar(accountNum) {
  const key = String(accountNum || "");
  if (!key) return;
  const marker = savedTargetStarMarkers[key];
  if (!marker) return;
  savedTargetStarLayer.removeLayer(marker);
  delete savedTargetStarMarkers[key];
}

function _renderSavedTargetStar(_area) {
  // Subject-property redesign: orphan saved parcels no longer get a map
  // visual. The Saved Targets sidebar list is the only way to see them;
  // clicking a list row pans + applies the purple selection highlight (see
  // the bookmark click branch in _renderList). Function kept as a no-op so
  // the 4 call sites (saveParcel, _restoreAllSavedParcelOutlines, restore
  // paths) don't need touching.
}

function _renderSavedParcelOutline(_area) {
  // Subject-property redesign: orphan saved parcels no longer get a map
  // visual (cyan halo + click-catcher). Gold outlines on the map now belong
  // exclusively to Subject Properties of saved areas, rendered by
  // subjectPropertyOutlineLayer at zoom >= 14. The Saved Targets sidebar
  // list remains the entry point for bookmark-style navigation (pan +
  // purple highlight via the bookmark click branch in _renderList).
  // Function kept as a no-op so call sites (saveParcel, restoreSavedArea,
  // _restoreAllSavedParcelOutlines, fork paths) don't need touching.
}

function _clearSelectedOutline() {
  selectedOutlineLayer.clearLayers();
}

// Render a crisp purple outline of `geometry` (Polygon or MultiPolygon)
// in selectedOutlinePane. No-op if geometry is missing or not a polygon
// type — point-only selections (e.g., type=location saved items) don't
// get a polygon outline.
function _renderSelectedOutline(geometry) {
  _clearSelectedOutline();
  if (!geometry) return;
  if (geometry.type !== "Polygon" && geometry.type !== "MultiPolygon") return;
  L.geoJSON({ type: "Feature", geometry, properties: {} }, {
    pane: "selectedOutlinePane",
    className: "selected-outline-glow",
    style: { color: "#a855f7", weight: 5, fill: false, interactive: false },
    interactive: false,
  }).addTo(selectedOutlineLayer);
}

async function saveParcel(account_num, county, addr, lat, lng, geometry) {
  if (!account_num) return;
  bumpUndoPillVersion();
  // If a workspace is currently loaded, ask the server to also create a
  // bonded copy of this target into that workspace. Backend silently skips
  // if the user doesn't own that workspace.
  const requestBody = {
    account_num,
    county: county || "dcad",
    payload: {
      account_num,
      county,
      name: addr || account_num,
      lat,
      lng,
      geometry: geometry || null,
    },
  };
  if (_currentLoadedAreaId) {
    requestBody.area_id = _currentLoadedAreaId;
  }
  const created = await _apiJson("/api/parcels", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(requestBody),
  });
  const row = _normalizeSavedParcelRow(created);
  _savedParcelsCache = _savedParcelsCache.filter((p) => !(p.account_num === row.account_num && p.county === row.county));
  _savedParcelsCache.unshift(row);
  _selectedSavedItemId = row.id;
  renderSavedAreasList();
  _renderSavedParcelOutline(row);
  _renderSavedTargetStar(row);
  // Single choke point for both right-click and popup-save paths.
  // Setting the current target + rendering the TARGET-badged star
  // happens here, so any saveParcel caller (right-click handler,
  // popup .parcel-save-link, future paths) gets it for free.
  const targetCounty = String(county || "dcad").trim().toLowerCase();
  const targetAccount = String(account_num || "").trim();
  if (targetAccount) {
    _setCurrentTargetParcel({ county: targetCounty, account: targetAccount, lat, lng });
    void _renderOriginatorTargetStar(targetCounty, targetAccount, lat, lng);
  }
  // v1.1 §2.1 — auto-commit subject change when inside a loaded area.
  // Replaces the v0.29 "stage then Update" two-step. The helper writes
  // the new originator_parcel_* on the loaded area's row and refreshes
  // _savedAreasCache. Optimistic UI: _setCurrentTargetParcel above
  // already updated the in-memory state; if the PUT fails we roll back
  // to the previously-persisted subject here.
  if (_currentLoadedAreaId && targetAccount && targetCounty) {
    const area = _savedAreasCache.find(
      (a) => a.id === _currentLoadedAreaId && a.type === "area",
    );
    if (area) {
      // Capture rollback state BEFORE the helper mutates `area`.
      const rollbackCounty = String(area.originator_parcel_county || "").trim().toLowerCase() || null;
      const rollbackAccount = String(area.originator_parcel_account_num || "").trim() || null;
      // Capture BEFORE commit/reload: was this area still on its
      // auto-generated timestamp placeholder name? (draw-first-then-
      // save-parcel ordering — _autoCacheOnDraw couldn't suggest a name
      // because no parcel was inside the polygon yet.)
      const wasAutoNamed = _isAutoGeneratedWorkspaceName(area.name);
      _pendingSubjectSaves++;
      try {
        const committed = await _commitOriginatorToArea(area, targetCounty, targetAccount);
        if (committed) {
          await _reloadSavedResources().catch(() => {});
        }
        // First-flow workspace auto-name: if the area still had the
        // placeholder timestamp name, rename it after this subject's
        // address — matching the save-parcel-then-draw ordering, which
        // names the area after the contained parcel via
        // _suggestAreaNameFromContainedParcels. Uses row.name (addr ||
        // account_num) for symmetry with that path. Only fires when the
        // name is still the auto-generated placeholder, so it's
        // "first flow only" by construction.
        const newWorkspaceName = String(row.name || "").trim();
        if (wasAutoNamed && newWorkspaceName) {
          try {
            await _apiJson(`/api/areas/${encodeURIComponent(_currentLoadedAreaId)}`, {
              method: "PUT",
              headers: { "Content-Type": "application/json", ...authHeaders() },
              body: JSON.stringify({ name: newWorkspaceName }),
            });
            const reloaded = _savedAreasCache.find(
              (a) => a.id === _currentLoadedAreaId && a.type === "area",
            );
            if (reloaded) reloaded.name = newWorkspaceName;
            setActiveItem("Workspace", newWorkspaceName);
            renderSavedAreasList();
            _syncTabTitle();
          } catch (renameErr) {
            console.warn("[saveParcel] first-flow auto-rename failed", renameErr);
          }
        }
      } catch (err) {
        console.error("[saveParcel] originator auto-commit failed", err);
        // Roll back optimistic UI: restore previous target so the
        // gold + star migrate back to where they were before the click.
        if (rollbackAccount && rollbackCounty) {
          _setCurrentTargetParcel({
            county: rollbackCounty,
            account: rollbackAccount,
          });
          void _renderOriginatorTargetStar(rollbackCounty, rollbackAccount);
        } else {
          _setCurrentTargetParcel(null);
        }
        _showToast("Subject save failed — retry via Update", "error");
      } finally {
        _pendingSubjectSaves = Math.max(0, _pendingSubjectSaves - 1);
      }
    }
  }

  // Only seed the Workspace slot label when there is no loaded workspace.
  // When one is loaded (xyz), saving a different parcel as the new target
  // must NOT rename the workspace — the Target row (updated via
  // _setCurrentTargetParcel above) carries the new address; the workspace
  // name belongs to the user.
  if (!_currentLoadedAreaId) {
    setActiveItem("Workspace", row.name);
  }
}

async function _rightClickSaveParcel(p, knownGeometry) {
  if (!_isPowerUserOrAbove()) return;
  if (!_currentUser) {
    _showToast("Sign in to save", "error");
    return;
  }
  const account = String(p?.account_num || "").trim();
  const county = String(p?.source_county || "dcad").trim().toLowerCase();
  if (!account || !county) return;

  const already = _savedParcelsCache.find((a) =>
    String(a.account_num || "").trim() === account
    && String(a.county || "").trim().toLowerCase() === county,
  );
  if (already) {
    _showToast("Already saved");
    return;
  }

  let addr = String(p?.addr || "").trim();
  let lat = Number(p?.lat);
  let lng = Number(p?.lng);
  let geometry = knownGeometry;

  if (!addr || !Number.isFinite(lat) || !Number.isFinite(lng) || !geometry) {
    try {
      const resp = await fetch(`/api/parcel/${encodeURIComponent(county)}/${encodeURIComponent(account)}`);
      if (resp.ok) {
        const detail = await resp.json();
        const props = detail.properties || detail;
        // Normalize per-county to "STREET CITY" (no comma/state/zip/dupe).
        // Always rebuild from raw props rather than trusting the incoming
        // `addr` arg, since it can be pre-concatenated upstream.
        addr = _formatPropertyAddress(county, props.addr || addr, props.city, props.owner_city);
        if (!Number.isFinite(lat) && Number.isFinite(Number(props.lat))) lat = Number(props.lat);
        if (!Number.isFinite(lng) && Number.isFinite(Number(props.lng))) lng = Number(props.lng);
        if (!geometry && (detail.geometry?.type === "Polygon" || detail.geometry?.type === "MultiPolygon")) {
          geometry = detail.geometry;
        }
      }
    } catch (_) { /* proceed with what we have */ }
  }

  try {
    await saveParcel(account, county, addr, lat, lng, geometry);
    _showToast(addr ? `Saved: ${addr}` : "Saved");
  } catch (err) {
    console.error("right-click save failed", err);
    _showToast("Save failed", "error");
  }
}

function _restoreAllSavedParcelOutlines() {
  Object.values(savedParcelLayers).forEach((layer) => savedParcelLayer.removeLayer(layer));
  Object.keys(savedParcelLayers).forEach((key) => delete savedParcelLayers[key]);
  Object.values(savedParcelClickLayers).forEach((layer) => savedParcelClickLayer.removeLayer(layer));
  Object.keys(savedParcelClickLayers).forEach((key) => delete savedParcelClickLayers[key]);
  Object.values(savedTargetStarMarkers).forEach((marker) => savedTargetStarLayer.removeLayer(marker));
  Object.keys(savedTargetStarMarkers).forEach((key) => delete savedTargetStarMarkers[key]);
  _savedParcelsCache.forEach(_renderSavedParcelOutline);
  _savedParcelsCache.forEach(_renderSavedTargetStar);
  _updateSavedTargetStarVisibility();
}

map.on("zoomend", _updateSavedTargetStarVisibility);
_updateSavedTargetStarVisibility();

// Subject-property layer rebuilds on any view change (pan OR zoom) so the
// viewport-gated outline rendering picks up parcels that entered view, and
// the star/outline visibility rules switch at the zoom threshold. moveend
// fires after both pan + zoom completions, so a single listener covers
// both. Geometry cache survives rebuilds — panning back over previously-
// fetched parcels hits no network.
map.on("moveend", _renderSubjectProperties);

function _createUndoSnapshot() {
  return {
    filterState: { ...filterState },
    numericFilters: { ...numericFilters },
    soldCompsFilter: { ...soldCompsFilter },
    lastAnalysisGeojson,
    drawnLayer: lastDrawnLatLngs ? [...lastDrawnLatLngs.map((p) => [...p])] : null,
    jobId: currentJobId,
    abortCtrl: null,
    pillVersion: ++undoPillVersion,
    // Per-view state so undo correctly restores the active view + its cache.
    _snapshotActiveView: ARV_NBV_EXPORT_ENABLED ? _activeView : "arv",
    _snapshotViewCache: ARV_NBV_EXPORT_ENABLED
      ? { arv:    _viewFilterCache.arv    ? { ..._viewFilterCache.arv    } : null,
          nbv:    _viewFilterCache.nbv    ? { ..._viewFilterCache.nbv    } : null,
          export: _viewFilterCache.export ? { ..._viewFilterCache.export } : null }
      : null,
  };
}

function _restoreUndoSnapshot(snapshot) {
  if (!snapshot) return;
  snapshot.abortCtrl?.abort();
  // Restore view state before filter mutations so any re-render below sees
  // the correct _activeView.
  if (ARV_NBV_EXPORT_ENABLED && snapshot._snapshotActiveView) {
    _activeView = snapshot._snapshotActiveView;
    if (snapshot._snapshotViewCache) _viewFilterCache = { ...snapshot._snapshotViewCache };
  }
  filterState = { ...DEFAULT_FILTERS, ...(snapshot.filterState || {}) };
  Object.assign(numericFilters, snapshot.numericFilters || {});
  soldCompsFilter = { ...DEFAULT_SOLD_COMPS_FILTER, ...(snapshot.soldCompsFilter || {}) };
  currentJobId = snapshot.jobId || null;
  lastAnalysisGeojson = snapshot.lastAnalysisGeojson || null;
  drawLayer.clearLayers();
  maskLayer.clearLayers();
  if (Array.isArray(snapshot.drawnLayer) && snapshot.drawnLayer.length >= 3) {
    lastDrawnLatLngs = snapshot.drawnLayer;
    lastPolygon = snapshot.drawnLayer.map(([lat, lng]) => [lng, lat]);
    L.polygon(snapshot.drawnLayer, {
      color: "#f1c40f",
      weight: 2.5,
      fill: false,
      interactive: false,
    }).addTo(drawLayer);
    const _worldRing = [[-90, -180], [-90, 180], [90, 180], [90, -180], [-90, -180]];
    L.polygon([_worldRing, snapshot.drawnLayer], {
      fillColor: "#000000",
      fillOpacity: 0.32,
      stroke: false,
      interactive: false,
    }).addTo(maskLayer);
    document.getElementById("btn-draw-clear")?.classList.remove("hidden");
    document.getElementById("btn-saved-area-clear")?.classList.remove("hidden");
  } else {
    lastDrawnLatLngs = null;
    lastPolygon = null;
    document.getElementById("btn-saved-area-clear")?.classList.add("hidden");
  }
  syncFilterInputs();
  _hydrateNumericInputsFromState();
  _hydrateSoldCompInputsFromState();
  if (lastAnalysisGeojson) {
    applyAndRenderSoldFilters();
    const markers = viewportRenderMode ? renderViewportFeatures() : renderFeatures(lastAnalysisGeojson);
    const counts = getVisibleFeatureCounts(lastAnalysisGeojson.features || []);
    if (lastAnalysisCounts) renderSidebar(counts, markers || {});
  }
  _refreshLoadedAreaUi();
  _dismissUndoPill();
}

function _countRestoredFilterKeys(state) {
  if (!state || !state.v) return 0;
  let count = 0;
  count += Object.values(state.checkboxes || {}).filter((v) => v === false).length;
  count += Object.values(state.numeric || {}).filter((v) => v != null && v !== "").length;
  count += Object.values(state.sold || {}).filter((v) => v != null && v !== "").length;
  // Cosmetic: count non-empty _views sections so the "Restored N filters" pill
  // reflects that NBV / Export filter data is also present in the area.
  if (ARV_NBV_EXPORT_ENABLED && state._views && typeof state._views === "object") {
    const _vv = state._views;
    if (_vv.nbv    && typeof _vv.nbv    === "object" && Object.keys(_vv.nbv).length    > 0) count += 1;
    if (_vv.export && typeof _vv.export === "object" && Object.keys(_vv.export).length > 0) count += 1;
  }
  return count;
}

function _showUndoPill(snapshot, restoredCount) {
  if (!snapshot || restoredCount <= 0) return;
  const host = document.getElementById("undo-pill-host");
  if (!host) return;
  _activeUndoSnapshot = snapshot;
  host.innerHTML = `
    <span class="undo-pill">
      Restored ${restoredCount} filter${restoredCount === 1 ? "" : "s"}
      <button type="button" id="undo-pill-btn">Undo</button>
    </span>
  `;
  document.getElementById("undo-pill-btn")?.addEventListener("click", (e) => {
    e.preventDefault();
    if (!_activeUndoSnapshot || _activeUndoSnapshot.pillVersion !== snapshot.pillVersion) return;
    _restoreUndoSnapshot(_activeUndoSnapshot);
  });
  if (_undoPillTimer) clearTimeout(_undoPillTimer);
  _undoPillTimer = setTimeout(() => {
    if (_activeUndoSnapshot?.pillVersion === snapshot.pillVersion) {
      _dismissUndoPill();
      undoPillVersion += 1;
    }
  }, 6000);
}

function _suspendViewportRender(ms = 700) {
  _suspendViewportRenderUntil = Math.max(_suspendViewportRenderUntil, Date.now() + ms);
}

function _captureParcelPopupState(meta) {
  if (!meta?.accountNum) return;
  _activeParcelPopupState = {
    accountNum: String(meta.accountNum),
  };
}

function _restoreActiveParcelPopup() {
  if (!_activeParcelPopupState?.props) return;
  _suspendViewportRender();
  setTimeout(() => {
    if (!_activeParcelPopupState?.props) return;
    openParcelDetailPanel(_activeParcelPopupState.props, {
      latlng: _activeParcelPopupState.latlng || null,
      matchedComp: _activeParcelPopupState.matchedComp || null,
      geometry: _activeParcelPopupState.geometry || null,
      suppressFly: true,
    });
  }, 0);
}

async function restoreSavedArea(area, options = {}) {
  const rowEl = options.rowEl || null;
  _selectedSavedItemId = area?.id || null;
  // AI mode is per-tab and tied to a subject/area — never carries across a
  // load. A stale ON flag or cache from a DIFFERENT area's subject would
  // otherwise silently route this area's first filter edits into ai_* keys
  // that mean nothing for it.
  _resetAiMode();
  // Location pins (from address search) — just fly there and show the ring.
  if (area.type === "location") {
    const latlng = [area.lat, area.lng];
    const clickMode = getClickMode();
    if (clickMode === "jump") {
      map.flyTo(latlng, 16);
    } else {
      // Stay mode: only pan if location is off-screen
      if (!isPointInViewport(latlng)) {
        map.setView(latlng, Math.max(map.getZoom(), 15));
      }
    }
    window._clearSearchHighlight?.();
    if (window._searchMoveEndHandler) map.off("moveend", window._searchMoveEndHandler);
    window._clearSearchHighlight = () => {
      if (window._searchHighlight) { window._searchHighlight.remove(); window._searchHighlight = null; }
    };
    window._searchMoveEndHandler = () => {
      window._searchMoveEndHandler = null;
      (async () => {
        const [slat, slng] = latlng;
        let highlightLayer = null;
        try {
          const resp = await fetch(`/api/parcel/near?lat=${slat}&lng=${slng}`);
          if (resp.ok) {
            const detail = await resp.json();
            const geom = detail.geometry;
            if (geom && (geom.type === "Polygon" || geom.type === "MultiPolygon")) {
              // Match the click-to-select purple outline so location pins
              // look identical to mouse-clicked parcels.
              highlightLayer = L.geoJSON(detail, {
                pane: "selectedOutlinePane",
                className: "selected-outline-glow",
                style: { color: "#a855f7", weight: 5, fill: false, interactive: false },
                interactive: false,
              }).addTo(map);
            }
          }
        } catch (e) {
          console.warn("Saved location footprint lookup failed", e);
        }
        if (!highlightLayer) {
          highlightLayer = L.circleMarker(latlng, {
            pane: "selectedOutlinePane",
            radius: 14, color: "#a855f7", weight: 5,
            fillColor: "#a855f7", fillOpacity: 0.08,
            interactive: false,
          }).addTo(map);
        }
        window._searchHighlight = highlightLayer;
      })();
    };
    map.once("moveend", window._searchMoveEndHandler);
    setActiveItem("Location", area.name);
    return;
  }

  if (area.type === "parcel") {
    _renderSavedParcelOutline(area);
    _renderSavedTargetStar(area);
    const clickMode = getClickMode();
    if (clickMode === "jump") {
      map.flyTo([area.lat, area.lng], 16);
    } else {
      // Stay mode: only pan if parcel is off-screen
      if (!isPointInViewport([area.lat, area.lng])) {
        map.setView([area.lat, area.lng], map.getZoom());
      }
    }
    setActiveItem("Workspace", area.name);
    return;
  }

  if (rowEl) rowEl.classList.add("row-shimmer");
  _setSessionCacheNote("");
  const savedFilterState = area.filter_state && typeof area.filter_state === "object"
    ? area.filter_state
    : null;
  clearDrawResults();
  setActiveItem("Workspace", area.name);
  if (savedFilterState) {
    restoreFilterState(savedFilterState, { _isAreaLoad: true });
  }
  // Sprint 2 §5.3: capture the restored state as the diff baseline so
  // the first user edit after load only PATCHes the fields they touch.
  _filterSaveLastSnapshot = captureFilterState();
  // Sprint 3 §4.2: open SSE stream for live filter-change push.
  if (typeof _openSseStream === "function") _openSseStream(area.id);
  drawLayer.clearLayers();
  L.polygon(area.latlngs, {
    color: "#f1c40f",
    weight: 2.5,
    fill: false,
    interactive: false,
  }).addTo(drawLayer);
  maskLayer.clearLayers();
  const _worldRing = [[-90, -180], [-90, 180], [90, 180], [90, -180], [-90, -180]];
  L.polygon([_worldRing, area.latlngs], {
    fillColor: "#000000",
    fillOpacity: 0.32,
    stroke: false,
    interactive: false,
  }).addTo(maskLayer);
  const bounds = area.bounds || _savedAreaBoundsFromLatLngs(area.latlngs || []);
  
  // Respect click mode: Jump = fitBounds, Stay = auto-pan only if off-screen
  const clickMode = getClickMode();
  if (clickMode === "jump") {
    if (bounds) map.fitBounds(bounds, { padding: [40, 40], maxZoom: 16 });
  } else {
    // Stay mode: only auto-pan if bounds are off-screen
    if (bounds && !areaBoundsInViewport(bounds)) {
      map.fitBounds(bounds, { padding: [40, 40], maxZoom: map.getZoom() });
    }
  }
  
  document.getElementById("btn-draw-clear")?.classList.remove("hidden");
  document.getElementById("btn-saved-area-clear")?.classList.remove("hidden");

  // Convert saved [lat, lng] pairs → [lng, lat] for the analysis API.
  const polygon = area.latlngs.map(([lat, lng]) => [lng, lat]);
  lastDrawnLatLngs = area.latlngs;
  lastPolygon = polygon;

  if (map.hasLayer(browseLayer)) browseLayer.remove();

  const includeRedfin = Boolean(filterState.active);
  const includeSold = Boolean(filterState.sold);
  document.getElementById("sidebar-results")?.classList.add("hidden");
  document.getElementById("active-item-actions")?.classList.add("hidden");
  document.getElementById("sidebar-loading")?.classList.remove("hidden");
  document.getElementById("redfin-status").textContent = "Loading area analysis...";
  const analysisRequest = beginLatestAnalysisRequest();
  if (options.undoSnapshot) options.undoSnapshot.abortCtrl = _activeAnalysisAbortController;

  try {
    const data = await runAnalysis(polygon, includeRedfin, includeSold, { signal: analysisRequest.signal, areaId: area.id });
    if (analysisRequest.signal.aborted) return;
    if (!isActiveAnalysisRequest(analysisRequest.requestId)) return;
    if (data.source_status && (!data.source_status.dcad_ok || !data.source_status.tad_ok)) {
      throw new Error("Incomplete county result set.");
    }
    currentJobId = data.job_id;
    data.features.forEach(feature => {
      const p = feature.properties || {};
      if (!p.account_num) return;
      const normalized = normalizeVerificationValue(p.verified_vacant);
      verificationByAccount.set(p.account_num, normalized);
      p.verified_vacant = normalized;
      const potential = String(p.potential_target || "").trim().toLowerCase() === "yes" ? "Yes" : "";
      potentialTargetByAccount.set(p.account_num, potential);
      p.potential_target = potential;
    });
    lastIncludedRedfin = includeRedfin;
    lastIncludedSold = includeSold;
    lastSoldPoints = Array.isArray(data.sold_points) ? data.sold_points : [];
    allSoldPointsRef = [...lastSoldPoints];
    lastAnalysisGeojson = data;
    lastAnalysisCounts = data.counts;
    allAnalysisFeatures = data.features;
    if (data.features.length <= BROWSE_ONLY_THRESHOLD) {
      const soldJoin = attachSoldCompsToFeatures(allAnalysisFeatures, lastSoldPoints);
      lastSoldPoints = soldJoin.unmatchedSoldPoints;
      matchedSoldLabelPoints = soldJoin.matchedLabelPoints || [];
      data.sold_points = lastSoldPoints;
    } else {
      matchedSoldLabelPoints = [];
    }
    redfinLayerVisible = false;
    soldLayerVisible = Boolean(filterState.sold);
    map.removeLayer(redfinLayer);
    if (soldLayerVisible) soldLayer.addTo(map); else map.removeLayer(soldLayer);
    applyAndRenderSoldFilters();

    const soldStatus = document.getElementById("sold-toggle-status");
    if (soldStatus) updateSoldStatusText();

    let markers;
    if (data.features.length > BROWSE_ONLY_THRESHOLD) {
      viewportRenderMode = false;
      markers = {};
    } else {
      if (map.hasLayer(browseLayer)) browseLayer.remove();
      if (data.features.length > LARGE_DRAW_THRESHOLD) {
        viewportRenderMode = true;
        renderViewportFeatures();
        markers = {};
      } else {
        viewportRenderMode = false;
        markers = renderFeatures(data);
      }
    }
    renderSidebar(data.counts, markers);
    applyResultTags(data);
    _clearOriginatorStar();
    _setCurrentTargetParcel(null);
    _currentLoadedAreaId = area.id;
    _renderViewToggle();  // area id set → reveal the ARV/NBV/Export toggle
    _updateActiveItemRenameVisibility();  // area id set → reveal rename pencil + share button (idempotent)
    _syncTabTitle();
    _storedValueOnAreaChange(_currentLoadedAreaId);
    void _filterSaveOnAreaChange(_currentLoadedAreaId);
    if (area.originator_parcel_county && area.originator_parcel_account_num) {
      _setCurrentTargetParcel({
        county: area.originator_parcel_county,
        account: area.originator_parcel_account_num,
      });
      void _renderOriginatorTargetStar(
        area.originator_parcel_county,
        area.originator_parcel_account_num,
      );
    }
    // Show the sticky Get Comps button so a quick sweep is one click away
    // after loading a saved area. Guard against mid-sweep: if a deep-pull
    // is in flight, surface the anchor but skip _showPropelioPolygonButton's
    // state reset so we don't trample the running label/disabled/is-running.
    if (_activeDeepPullJobId) {
      _ensureStickyPropelioButton();
      propelioStickyAnchor?.classList.add("visible");
    } else {
      _showPropelioPolygonButton();
    }
    // Workspace = parcels + archived comps. Hydrate propelio comps from the
    // session DB so the analyst lands back in the exact state they left
    // (good/bad ratings, dimmed bad-comps, footprints, list).
    void _hydratePropelioFromArchive(area.id);
    // NOTE: don't re-call setActiveItem here. It was already set at line 1885
    // before the await. Calling it again post-analysis stomps any active
    // selection the user made during the analysis (e.g., clicking a target
    // while the workspace was still loading would briefly show the target,
    // then this would override it back to "Workspace").
    renderSavedAreasList();
    // Debug: sold-count restore diagnostics (remove after Bug 2 confirmed fixed)
    console.debug("[restoreSavedArea] post-render sold state — allSoldPointsRef:", allSoldPointsRef.length, "lastSoldPanelPoints:", lastSoldPanelPoints.length, "soldCompsFilter:", JSON.stringify(soldCompsFilter), "filterState.sold:", filterState.sold);
    applyAndRenderSoldFilters();
    applyMapVisibilityFilters();
  } catch (err) {
    if (isAbortError(err) || !isActiveAnalysisRequest(analysisRequest.requestId)) return;
    console.error("[restoreSavedArea] Analysis failed:", err);
    document.getElementById("redfin-status").textContent = getAnalysisErrorMessage(err, "Area analysis failed. Please try again.");
    document.getElementById("sidebar-loading")?.classList.add("hidden");
  } finally {
    if (rowEl) rowEl.classList.remove("row-shimmer");
  }
}

async function restoreNamedSession(session, options = {}) {
  const rowEl = options.rowEl || null;
  _selectedSavedItemId = null;
  _resetAiMode();   // see restoreSavedArea — a session load is also a different subject/context
  if (!session.latlngs || session.latlngs.length < 3) {
    console.warn("[restoreNamedSession] session has no polygon", session);
    return;
  }
  if (rowEl) rowEl.classList.add("row-shimmer");
  _clearOriginatorStar();
  _setCurrentTargetParcel(null);
  _currentLoadedAreaId = null;
  _renderViewToggle();  // no area loaded → hide the ARV/NBV/Export toggle
  // Sprint 2 §5.3: no area loaded -> no snapshot baseline + clear queue.
  _filterSaveLastSnapshot = null;
  _filterSavePendingFields.clear();
  // Sprint 3 §4.3: close any open SSE stream.
  if (typeof _closeSseStream === "function") _closeSseStream();
  _syncTabTitle();
  _storedValueOnAreaChange(null);
  void _filterSaveOnAreaChange(null);
  renderSavedAreasList();
  const savedFilterState = session.filter_state && typeof session.filter_state === "object"
    ? session.filter_state
    : null;
  clearDrawResults();
  if (savedFilterState) {
    restoreFilterState(savedFilterState, { _isAreaLoad: true });
  }
  // Sprint 2 §5.3: refresh diff baseline after snapshot restore so any
  // subsequent edit only PATCHes the fields actually changed.
  _filterSaveLastSnapshot = captureFilterState();
  drawLayer.clearLayers();
  L.polygon(session.latlngs, { color: "#f1c40f", weight: 2.5, fill: false, interactive: false }).addTo(drawLayer);
  maskLayer.clearLayers();
  const _worldRing = [[-90, -180], [-90, 180], [90, 180], [90, -180], [-90, -180]];
  L.polygon([_worldRing, session.latlngs], { fillColor: "#000000", fillOpacity: 0.32, stroke: false, interactive: false }).addTo(maskLayer);
  const bounds = _savedAreaBoundsFromLatLngs(session.latlngs);
  
  // Respect click mode: Jump = fitBounds, Stay = auto-pan only if off-screen
  const clickMode = getClickMode();
  if (clickMode === "jump") {
    if (bounds) map.fitBounds(bounds, { padding: [40, 40], maxZoom: 16 });
  } else {
    // Stay mode: only auto-pan if bounds are off-screen
    if (bounds && !areaBoundsInViewport(bounds)) {
      map.fitBounds(bounds, { padding: [40, 40], maxZoom: map.getZoom() });
    }
  }
  
  document.getElementById("btn-draw-clear")?.classList.remove("hidden");
  document.getElementById("btn-saved-area-clear")?.classList.remove("hidden");

  if (session.originator_parcel_county && session.originator_parcel_account_num) {
    _setCurrentTargetParcel({
      county: session.originator_parcel_county,
      account: session.originator_parcel_account_num,
    });
    void _renderOriginatorTargetStar(
      session.originator_parcel_county,
      session.originator_parcel_account_num,
    );
  }

  const polygon = session.latlngs.map(([lat, lng]) => [lng, lat]);
  lastDrawnLatLngs = session.latlngs;
  lastPolygon = polygon;
  if (map.hasLayer(browseLayer)) browseLayer.remove();
  const includeSold = Boolean(filterState.sold);
  document.getElementById("sidebar-loading")?.classList.remove("hidden");
  document.getElementById("redfin-status").textContent = "Loading session…";
  // Set the slot BEFORE the await so it shows up immediately and so a user
  // clicking a different item during the analysis isn't stomped post-analysis.
  setActiveItem("Snapshot", session.name);
  const analysisRequest = beginLatestAnalysisRequest();
  if (options.undoSnapshot) options.undoSnapshot.abortCtrl = _activeAnalysisAbortController;

  try {
    let data;
    let loadedFromSessionCache = false;
    // Try cached_jobs first (named sessions are pinned; avoids a fresh CAD query)
    try {
      data = await _apiJson(`/api/sessions/${encodeURIComponent(session.session_id)}/data`);
      loadedFromSessionCache = true;
    } catch (_cacheErr) {
      if (analysisRequest.signal.aborted) return;
      // Cache miss — fall back to fresh analysis
      data = await runAnalysis(polygon, true, includeSold, { signal: analysisRequest.signal });
    }
    if (analysisRequest.signal.aborted) return;
    if (!isActiveAnalysisRequest(analysisRequest.requestId)) return;

    currentJobId = data.job_id;
    _currentSessionIsNamed = true;
    _updateSaveSessionButtonState();

    data.features.forEach((feature) => {
      const p = feature.properties || {};
      if (!p.account_num) return;
      const normalized = normalizeVerificationValue(p.verified_vacant);
      verificationByAccount.set(p.account_num, normalized);
      p.verified_vacant = normalized;
      const potential = String(p.potential_target || "").trim().toLowerCase() === "yes" ? "Yes" : "";
      potentialTargetByAccount.set(p.account_num, potential);
      p.potential_target = potential;
    });
    lastIncludedRedfin = false;
    lastIncludedSold = includeSold;
    lastSoldPoints = Array.isArray(data.sold_points) ? data.sold_points : [];
    allSoldPointsRef = [...lastSoldPoints];
    lastAnalysisGeojson = data;
    lastAnalysisCounts = data.counts;
    allAnalysisFeatures = data.features;
    if (data.features.length <= BROWSE_ONLY_THRESHOLD) {
      const soldJoin = attachSoldCompsToFeatures(allAnalysisFeatures, lastSoldPoints);
      lastSoldPoints = soldJoin.unmatchedSoldPoints;
      matchedSoldLabelPoints = soldJoin.matchedLabelPoints || [];
      data.sold_points = lastSoldPoints;
    } else {
      matchedSoldLabelPoints = [];
    }
    redfinLayerVisible = false;
    soldLayerVisible = Boolean(filterState.sold);
    map.removeLayer(redfinLayer);
    if (soldLayerVisible) soldLayer.addTo(map); else map.removeLayer(soldLayer);
    applyAndRenderSoldFilters();
    const soldStatus = document.getElementById("sold-toggle-status");
    if (soldStatus) updateSoldStatusText();
    let markers;
    if (data.features.length > BROWSE_ONLY_THRESHOLD) {
      viewportRenderMode = false;
      markers = {};
    } else {
      if (map.hasLayer(browseLayer)) browseLayer.remove();
      if (data.features.length > LARGE_DRAW_THRESHOLD) {
        viewportRenderMode = true;
        renderViewportFeatures();
        markers = {};
      } else {
        viewportRenderMode = false;
        markers = renderFeatures(data);
      }
    }
    renderSidebar(data.counts, markers);
    applyResultTags(data);
    // NOTE: don't re-call setActiveItem here. Already set before the await.
    if (loadedFromSessionCache && data.redfin_skipped === true) {
      _setSessionCacheNote("Active listings not shown - re-analyze for current");
    } else {
      _setSessionCacheNote("");
    }
    applyAndRenderSoldFilters();
    applyMapVisibilityFilters();
  } catch (err) {
    if (isAbortError(err) || !isActiveAnalysisRequest(analysisRequest.requestId)) return;
    console.error("[restoreNamedSession] failed:", err);
    document.getElementById("redfin-status").textContent = getAnalysisErrorMessage(err, "Session load failed. Please try again.");
    document.getElementById("sidebar-loading")?.classList.add("hidden");
    _setSessionCacheNote("");
  } finally {
    if (rowEl) rowEl.classList.remove("row-shimmer");
  }
}

function _formatFilterDiffChip(area) {
  const st = area?.filter_state;
  if (!st || st.v !== 1) return "";
  const chips = [];
  const n = st.numeric || {};
  if (n.lot_sqft_min != null) chips.push(`Lot ≥ ${(Number(n.lot_sqft_min) / 43560).toFixed(2)}ac`);
  if (n.yr_built_min != null || n.yr_built_max != null) chips.push(`Built ${n.yr_built_min ?? "?"}–${n.yr_built_max ?? "?"}`);
  const s = st.sold || {};
  if (s.maxDaysAgo != null && s.maxDaysAgo !== DEFAULT_SOLD_COMPS_FILTER.maxDaysAgo) chips.push(`Sold ${s.maxDaysAgo}d`);
  if (!chips.length) return "";
  const extraCount = Object.values(n).filter((v) => v != null).length + Object.values(s).filter((v) => v != null).length - chips.length;
  return `${chips.slice(0, 2).join(" · ")}${extraCount > 0 ? ` · +${extraCount}` : ""}`;
}

async function _renameSavedItemInline(item, rowEl) {
  if (!item || item.type === "parcel") return;
  const nameEl = rowEl.querySelector(".saved-area-name");
  if (!nameEl) return;
  const input = document.createElement("input");
  input.className = "saved-area-rename-input";
  input.value = item.name || "";
  nameEl.replaceWith(input);
  input.focus();
  input.select();

  const cancel = () => renderSavedAreasList();
  const save = async () => {
    const nextName = String(input.value || "").trim();
    if (!nextName || nextName === item.name) {
      cancel();
      return;
    }
    bumpUndoPillVersion();
    await _apiJson(`/api/areas/${encodeURIComponent(item.id)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ name: nextName }),
    });
    await _reloadSavedResources();
    _syncTabTitle();
  };

  input.addEventListener("keydown", async (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      cancel();
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      await save();
    }
  });
  input.addEventListener("blur", cancel);
}

// ─── Multi-select selection state (spec v3 2026-05-23) ─────────────────
// Per-list selection. Keyed by the listKey extracted from the list
// container id ("saved-areas" or "saved-parcels"). Each entry tracks:
//   selectedIds        — Set of currently-selected row ids
//   lastAnchorId       — id of last clicked checkbox (for shift+range)
//   selectModeActive   — touch-mode: explicit Select toggle is on
const _listSelections = {
  "saved-areas":   { selectedIds: new Set(), lastAnchorId: null, selectModeActive: false },
  "saved-parcels": { selectedIds: new Set(), lastAnchorId: null, selectModeActive: false },
};

function _typeLabel(t) {
  if (t === "parcel") return "Parcel";
  if (t === "location") return "Location";
  return "Workspace";
}

function _renderList(sectionId, listId, items, options = {}) {
  const section = document.getElementById(sectionId);
  const list = document.getElementById(listId);
  if (!section || !list) return;
  const searchActive = Boolean(options.searchActive);
  const listKey = listId.replace(/-list$/, "");  // "saved-areas-list" → "saved-areas"
  // 2026-05-22 bugfix: only hide the section when the list is empty AND
  // the user is NOT actively searching. Previously, an unmatched search
  // hid the entire section (including the search input itself), forcing
  // a page refresh to recover. Now an unmatched search keeps the section
  // visible and shows a small "no matches" message.
  section.classList.toggle("hidden", items.length === 0 && !searchActive);
  if (items.length === 0 && searchActive) {
    list.innerHTML = `<div class="saved-list-empty-state">No matches for current search.</div>`;
    _refreshSelectionUI(listKey);
    return;
  }
  list.innerHTML = items.map((area) => {
    const date = new Date(area.savedAt).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    const icon = area.type === "parcel" ? "📌" : area.type === "location" ? "📍" : "▭";
    const displayName = area.type === "parcel" || area.type === "location"
      ? String(area.name || "").replace(/,\s*/g, " ")
      : area.name;
    const chip = _formatFilterDiffChip(area);
    const canRename = area.type !== "parcel";
    const canShare = area.type === "area" && Boolean(String(area.share_id || "").trim());
    const isActiveRow = area.id === _currentLoadedAreaId || area.id === _selectedSavedItemId;
    const activeClass = isActiveRow ? " saved-area-row-active" : "";
    // Sprint 1 multi-user collab (spec §3.5): editor rows render italic
    // name + a small 'shared' tag so users can tell owned vs joined
    // workspaces at a glance, pending the fuller member-badge UI in Sprint 4.
    const sharedClass = (area.role || "owner") === "editor" ? " is-shared" : "";
    const sharedTag = sharedClass ? '<span class="saved-area-shared-tag" title="You\'re an editor on this shared workspace">shared</span>' : "";
    const secondaryLine = [chip, `saved ${date}`, sharedTag].filter(Boolean).join(" · ");

    // Sprint 1 multi-user collab: role-based affordance gating (spec §3.3, §5).
    // Row-owner sees Rename; editor sees the Make-my-copy path.
    // Fix B (2026-07-12): app-role admins (owner + developer, via _isAdmin) also get
    // Rename on rows shared to them — the server still gates on area membership.
    // Rename and fork are now independent: admins see BOTH on a shared row, and
    // fork stays the default workflow for everyone else.
    const isOwnerRow = (area.role || "owner") === "owner";
    const canRenameRow = (isOwnerRow || _isAdmin()) && canRename;
    const canForkRow = !isOwnerRow && canShare;
    const nameId = `name-${listKey}-${area.id}`;
    const typeLabel = _typeLabel(area.type);

    return `
      <div class="saved-area-row${activeClass}${sharedClass}" tabindex="0" data-id="${area.id}" data-type="${area.type}">
        <div class="saved-area-main">
          <input type="checkbox" class="saved-area-checkbox" data-action="toggle-selection" data-id="${area.id}" aria-labelledby="${nameId}">
          <span class="visually-hidden">${typeLabel}</span>
          <span class="saved-area-name-wrap"><span class="saved-area-name" id="${nameId}">${displayName}</span></span>
          ${area.originator_unresolved ? `<span class="saved-area-originator-unresolved" title="The originator parcel for this area is no longer in CAD (deleted or renumbered). The gold outline/star won't render.">Originator unresolved</span>` : ""}
          ${canShare ? `<button type="button" class="saved-area-action-btn saved-area-share-btn" data-action="share" data-share-id="${_esc(area.share_id)}" title="Share">🔗</button>` : ""}
        </div>
        <div class="saved-area-secondary-line">${secondaryLine}</div>
        <div class="saved-area-row-secondary-actions">
          <hr class="saved-area-actions-divider">
          <div class="saved-area-secondary-btns">
            ${canForkRow ? `<button type="button" class="saved-area-action-btn" data-action="fork" data-share-id="${_esc(area.share_id)}" title="Make my own copy">📋 Make my copy</button>` : ""}
            ${canRenameRow ? `<button type="button" class="saved-area-action-btn rename" data-action="rename" title="Rename">✎ Rename</button>` : ""}
          </div>
        </div>
      </div>`;
  }).join("");
  list.querySelectorAll(".saved-area-row").forEach(row => {
    row.addEventListener("click", async (e) => {
      const actionEl = e.target.closest("[data-action]");
      const id = row.dataset.id;
      const all = [..._savedAreasCache, ..._savedParcelsCache];
      const area = all.find((a) => a.id === id);
      if (!area) return;
      // Multi-select checkbox toggle — let native toggle handle the
      // instant visual feedback (:checked::after needs the property
      // change to fire from the native click default action; setting
      // .checked programmatically before native toggle caused the
      // "doesn't register until next click" bug 2026-05-23). Defer
      // state sync to after the toggle via rAF.
      if (actionEl?.dataset.action === "toggle-selection") {
        e.stopPropagation();
        const cb = e.target.closest('.saved-area-checkbox');
        const shiftKey = e.shiftKey;
        requestAnimationFrame(() => _handleCheckboxClickDeferred(cb, row, shiftKey));
        return;
      }
      if (actionEl?.dataset.action === "share") {
        e.stopPropagation();
        const shareId = String(actionEl.dataset.shareId || "").trim();
        if (!shareId) return;
        const url = `${window.location.origin}/?area=${shareId}`;
        try {
          if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
            throw new Error("Clipboard API unavailable");
          }
          await navigator.clipboard.writeText(url);
          _showToast("Link copied");
        } catch {
          _showToast("Copy failed - try again", "error");
        }
        return;
      }
      if (actionEl?.dataset.action === "fork") {
        e.stopPropagation();
        const shareId = String(actionEl.dataset.shareId || "").trim();
        if (!shareId) return;
        try {
          const cloned = await _apiJson("/api/areas/from-share-id/" + encodeURIComponent(shareId), {
            method: "POST",
            headers: { "Content-Type": "application/json", ...authHeaders() },
            body: "{}",
          });
          const normalizedFork = _normalizeSavedAreaRow(cloned);
          _savedAreasCache.unshift(normalizedFork);
          _clearOriginatorStar();
          _setCurrentTargetParcel(null);
          _currentLoadedAreaId = cloned.area_id;
          _renderViewToggle();  // area id set → reveal the ARV/NBV/Export toggle
    _updateActiveItemRenameVisibility();  // area id set → reveal rename pencil + share button (idempotent)
          _syncTabTitle();
          _storedValueOnAreaChange(_currentLoadedAreaId);
          void _filterSaveOnAreaChange(_currentLoadedAreaId);
          // Refresh Good Comps section visibility — fork bypasses the
          // normal restoreSavedArea path, so applyPropelioClientFilters
          // isn't called automatically. Per spec v2 §Risk #1.
          // Per-view ratings (Chunk E): project to the active view first, since
          // _renderGoodCompsSection reads user_rating and nothing else projected
          // it on this path.
          _projectCompRatingsForActiveView();
          if (typeof _renderGoodCompsSection === "function") _renderGoodCompsSection();
          _selectedSavedItemId = cloned.area_id;
          // Carry the originator TARGET star through the fork.
          if (normalizedFork.originator_parcel_county && normalizedFork.originator_parcel_account_num) {
            _setCurrentTargetParcel({
              county: normalizedFork.originator_parcel_county,
              account: normalizedFork.originator_parcel_account_num,
            });
            void _renderOriginatorTargetStar(
              normalizedFork.originator_parcel_county,
              normalizedFork.originator_parcel_account_num,
            );
          }
          renderSavedAreasList();
          // Refetch subject_properties so the forked area's originator
          // shows the gold outline + star (subject-property redesign).
          await _reloadSavedResources().catch((err) =>
            console.warn("[fork] post-clone resource reload failed:", err)
          );
          _showToast(`Forked → "${cloned.name}"`);
        } catch {
          _showToast("Could not fork area", "error");
        }
        return;
      }
      if (actionEl?.dataset.action === "rename") {
        e.stopPropagation();
        await _renameSavedItemInline(area, row);
        return;
      }
      if (!_navigationGuardForActiveDeepPull("switch workspaces")) {
        return;
      }
      _selectedSavedItemId = area.id;
      // Purple selection outline highlights a single parcel footprint.
      // Skip for saved area drawings (type="area" — the polygon wraps many
      // parcels and reads as a ring around them) and saved locations
      // (type="location" — no polygon anyway). Saved parcels (type="parcel")
      // and comp-list clicks are the only paths that get the highlight.
      if (area.type === "parcel") {
        _renderSelectedOutline(area.geometry || null);
      } else {
        _clearSelectedOutline();
      }
      document.querySelectorAll(".saved-area-row-active").forEach((el) => {
        if (el !== row) el.classList.remove("saved-area-row-active");
      });
      row.classList.add("saved-area-row-active");

      // Bookmark click branch (subject-property redesign): a click on a
      // type="parcel" row should JUST select with purple + pan to it. No
      // popup auto-opens, no workspace restore. User clicks the parcel
      // itself on the map if they want the popup.
      if (area.type === "parcel") {
        const lat = Number(area.lat);
        const lng = Number(area.lng);
        if (Number.isFinite(lat) && Number.isFinite(lng)) {
          const clickMode = (typeof getClickMode === "function" ? getClickMode() : "stay");
          if (clickMode === "jump") {
            map.flyTo([lat, lng], Math.max(map.getZoom(), 16), { duration: 0.35 });
          } else if (!isPointInViewport([lat, lng])) {
            map.setView([lat, lng], Math.max(map.getZoom(), 15));
          }
        }
        return;
      }

      bumpUndoPillVersion();
      const snapshot = _createUndoSnapshot();
      await restoreSavedArea(area, { rowEl: row, undoSnapshot: snapshot });
      const restoredCount = _countRestoredFilterKeys(area.filter_state);
      _showUndoPill(snapshot, restoredCount);
    });
  });
  // After every render: intersect selectedIds with currently-rendered ids
  // (handles search-filter + server-side deletion), sync .is-selected +
  // checkbox.checked state, update bulk toolbar count + visibility.
  if (_listSelections[listKey]) _refreshSelectionUI(listKey);
}

// ─── Multi-select handlers (spec v3 2026-05-23) ────────────────────────

// Deferred state sync — runs after the native checkbox toggle has completed
// (via requestAnimationFrame in the row click handler), so cb.checked
// reflects the new state. Reads from cb.checked instead of toggling
// selectedIds independently — keeps DOM ↔ state in lockstep with whatever
// the browser just did.
function _handleCheckboxClickDeferred(checkbox, row, shiftKey) {
  if (!checkbox || !row) return;
  const listEl = row.closest('[id$="-list"]');
  const listKey = listEl?.id?.replace(/-list$/, '');
  if (!listKey || !_listSelections[listKey]) return;
  const sel = _listSelections[listKey];
  const id = checkbox.dataset.id;

  if (shiftKey && sel.lastAnchorId) {
    // Defensive: kill any stray text-selection the shift+click may have
    // extended across rows before we got here. user-select:none on
    // .saved-area-row prevents new text selection, but an existing
    // selection from a prior text-click can still be extended.
    try { window.getSelection()?.removeAllRanges(); } catch (_) { /* tolerate */ }
    // Range select from anchor to current. Compute DOM order at click
    // time — robust against cache mutations between clicks.
    const allRows = Array.from(listEl.querySelectorAll('.saved-area-row[data-id]'));
    const ids = allRows.map(r => r.dataset.id);
    const anchorIdx = ids.indexOf(sel.lastAnchorId);
    const currentIdx = ids.indexOf(id);
    if (anchorIdx >= 0 && currentIdx >= 0) {
      const lo = Math.min(anchorIdx, currentIdx);
      const hi = Math.max(anchorIdx, currentIdx);
      // Range adds (matches Linear/Gmail convention). _refreshSelectionUI
      // below will re-sync cb.checked for any rows the native click missed.
      for (let i = lo; i <= hi; i++) sel.selectedIds.add(ids[i]);
    } else {
      // Anchor lost — fall back: sync from native toggle on this one.
      if (checkbox.checked) sel.selectedIds.add(id);
      else sel.selectedIds.delete(id);
    }
    sel.lastAnchorId = id;
  } else {
    // Sync from native toggle — cb.checked is the new (post-toggle) state.
    if (checkbox.checked) sel.selectedIds.add(id);
    else sel.selectedIds.delete(id);
    sel.lastAnchorId = id;
  }
  _refreshSelectionUI(listKey);
}

function _refreshSelectionUI(listKey) {
  const sel = _listSelections[listKey];
  if (!sel) return;
  const listEl = document.getElementById(`${listKey}-list`);
  if (!listEl) return;
  const renderedRows = Array.from(listEl.querySelectorAll('.saved-area-row[data-id]'));
  const renderedIds = new Set(renderedRows.map(r => r.dataset.id));
  // Intersect selectedIds with currently-rendered (handles both server-side
  // deletion AND search-filter narrowing — visible-only selection per v3).
  const beforeSize = sel.selectedIds.size;
  for (const id of Array.from(sel.selectedIds)) {
    if (!renderedIds.has(id)) sel.selectedIds.delete(id);
  }
  const removed = beforeSize - sel.selectedIds.size;
  if (sel.lastAnchorId && !renderedIds.has(sel.lastAnchorId)) {
    sel.lastAnchorId = null;
  }
  // Sync DOM
  renderedRows.forEach(r => {
    const id = r.dataset.id;
    const isSelected = sel.selectedIds.has(id);
    r.classList.toggle('is-selected', isSelected);
    const cb = r.querySelector('.saved-area-checkbox');
    if (cb) cb.checked = isSelected;
  });
  listEl.classList.toggle('has-selection', sel.selectedIds.size > 0);
  // Toolbar count + visibility
  const toolbar = document.getElementById(`${listKey}-bulk-toolbar`);
  const countEl = document.getElementById(`${listKey}-bulk-count`);
  const n = sel.selectedIds.size;
  if (toolbar) toolbar.classList.toggle('hidden', n === 0);
  if (countEl) countEl.textContent = `${n} selected`;
  // Don't toast on the silent search-filter narrow; only when we suspect
  // server-side deletion (heuristic: items removed but search isn't
  // narrowing — i.e., a refresh happened without a search-text change).
  // For v3, keep this silent; future enhancement can add precise detection.
  void removed;
}

function _clearSelection(listKey) {
  const sel = _listSelections[listKey];
  if (!sel) return;
  sel.selectedIds.clear();
  sel.lastAnchorId = null;
  _refreshSelectionUI(listKey);
}

function _toggleSelectMode(listKey) {
  const sel = _listSelections[listKey];
  if (!sel) return;
  sel.selectModeActive = !sel.selectModeActive;
  const listEl = document.getElementById(`${listKey}-list`);
  listEl?.classList.toggle('select-mode-active', sel.selectModeActive);
  const btn = document.getElementById(`${listKey}-select-mode-btn`);
  btn?.classList.toggle('is-active', sel.selectModeActive);
  if (btn) btn.textContent = sel.selectModeActive ? 'Done' : 'Select';
  if (!sel.selectModeActive) _clearSelection(listKey);
}

async function _handleBulkDelete(listKey) {
  const sel = _listSelections[listKey];
  if (!sel || sel.selectedIds.size === 0) return;
  // Sprint 1 multi-user collab (spec §3.3, Copilot frontend audit): bulk-delete
  // is owner-only. Pre-filter editor-row IDs so the user gets a clear toast
  // up-front instead of N silent per-row 403 rejections.
  const ids = Array.from(sel.selectedIds);
  const all = [..._savedAreasCache, ..._savedParcelsCache];
  const items = ids.map(id => all.find(a => a.id === id)).filter(Boolean);
  const ownerItems = items.filter(it => (it.role || "owner") === "owner");
  const skipped = items.length - ownerItems.length;
  if (skipped > 0 && ownerItems.length === 0) {
    _showToast("Cannot delete: only the owner can delete shared areas.");
    return;
  }
  const n = ownerItems.length;
  const noun = (listKey === 'saved-areas') ? 'saved area' : 'target';
  const skippedSuffix = skipped > 0 ? ` (${skipped} shared, can't delete)` : '';
  const ok = window.confirm(`Delete ${n} ${noun}${n > 1 ? 's' : ''}${skippedSuffix}? This cannot be undone.`);
  if (!ok) return;
  // Bounded-concurrency pool of 4 workers — iterates ownerItems only
  // so editor-role rows never hit the delete endpoint (would 403 anyway).
  const queue = [...ownerItems];
  let successCount = 0;
  const failed = [];
  const alreadyDeleted = [];
  const workers = Array.from({ length: Math.min(4, queue.length) }).map(async () => {
    while (queue.length) {
      const item = queue.shift();
      try {
        await deleteSavedArea(item);
        successCount++;
      } catch (err) {
        const status = err && (err.status || err?.response?.status);
        if (status === 404) alreadyDeleted.push(item.id);
        else failed.push({ id: item.id, err });
      }
    }
  });
  await Promise.allSettled(workers);
  // Reset selection state — deleteSavedArea already mutated caches; re-render to sync UI.
  sel.selectedIds.clear();
  sel.lastAnchorId = null;
  try { renderSavedAreasList(); } catch (_) { /* tolerate */ }
  if (failed.length > 0) {
    _showToast(`Deleted ${successCount}. ${failed.length} failed — check console.`, "error");
    console.error("bulk delete failures", failed);
  } else if (alreadyDeleted.length > 0) {
    _showToast(`Deleted ${successCount}. ${alreadyDeleted.length} were already removed.`);
  } else {
    _showToast(`Deleted ${successCount} ${noun}${successCount > 1 ? 's' : ''}.`);
  }
}

// Bulk-toolbar + Select-mode click wiring (delegated on document for
// simplicity; targets are stable id-based elements).
document.addEventListener('click', (ev) => {
  const t = ev.target.closest('[data-action]');
  if (!t) return;
  const action = t.dataset.action;
  if (action === 'delete-selected') {
    const listKey = t.dataset.list;
    if (listKey) void _handleBulkDelete(listKey);
  } else if (action === 'clear-selection') {
    const listKey = t.dataset.list;
    if (listKey) _clearSelection(listKey);
  }
});

document.querySelectorAll('.select-mode-toggle[data-list]').forEach(btn => {
  btn.addEventListener('click', (ev) => {
    ev.preventDefault();
    _toggleSelectMode(btn.dataset.list);
  });
});

// Scoped keyboard handlers — Esc clears, Cmd/Ctrl+A selects all visible.
// Bail when target is an input/textarea/contenteditable so we don't steal
// keystrokes from search input or rename input.
document.addEventListener('keydown', (ev) => {
  const t = ev.target;
  if (t && t.matches && t.matches('input, textarea, [contenteditable=true]')) return;
  // Find which list (if any) has keyboard focus inside it
  const activeList = document.activeElement?.closest('[id$="-list"]');
  if (!activeList) return;
  const listKey = activeList.id?.replace(/-list$/, '');
  if (!_listSelections[listKey]) return;

  if (ev.key === 'Escape') {
    if (_listSelections[listKey].selectedIds.size > 0) {
      ev.preventDefault();
      _clearSelection(listKey);
    }
  } else if ((ev.metaKey || ev.ctrlKey) && (ev.key === 'a' || ev.key === 'A')) {
    ev.preventDefault();
    const sel = _listSelections[listKey];
    activeList.querySelectorAll('.saved-area-row[data-id]').forEach(r => sel.selectedIds.add(r.dataset.id));
    _refreshSelectionUI(listKey);
  }
});

function renderSavedAreasList() {
  const areasTokens = _savedAreasSearchQuery.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const targetsTokens = _savedTargetsSearchQuery.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const areas = _savedAreasCache
    .filter((a) => a.type === "area")
    .filter((a) => {
      if (areasTokens.length === 0) return true;
      const name = String(a.name || "").toLowerCase();
      return areasTokens.every((t) => name.includes(t));
    });
  const targets = [..._savedAreasCache.filter((a) => a.type === "location"), ..._savedParcelsCache]
    .filter((a) => {
      if (targetsTokens.length === 0) return true;
      const name = String(a.name || "").toLowerCase();
      return targetsTokens.every((t) => name.includes(t));
    });
  _renderList("saved-areas", "saved-areas-list", areas, { searchActive: areasTokens.length > 0 });
  _renderList("saved-parcels", "saved-parcels-list", targets, { searchActive: targetsTokens.length > 0 });
}

function _renderSessionsList(sectionId, listId, items) {
  const section = document.getElementById(sectionId);
  const list = document.getElementById(listId);
  if (!section || !list) return;
  section.classList.toggle("hidden", items.length === 0);
  list.innerHTML = items.map((session) => {
    const date = new Date(session.savedAt).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    const chip = _formatFilterDiffChip(session);
    const counties = session.county_coverage && session.county_coverage.length
      ? session.county_coverage.map((c) => c.toUpperCase()).join(" + ")
      : "";
    const meta = [session.parcel_count ? `${session.parcel_count} parcels` : "", counties].filter(Boolean).join(" · ");
    return `
      <div class="saved-area-row" tabindex="0" data-session-id="${session.session_id}">
        <div class="saved-area-main">
          <span class="saved-area-name-wrap" data-tooltip="${_esc(session.name || "")}"><span class="saved-area-name">${session.name}</span></span>
          <span class="saved-area-date">${date}</span>
        </div>
        ${meta ? `<div class="saved-item-meta">${meta}</div>` : ""}
        ${chip ? `<div class="saved-row-filter-chip">${chip}</div>` : ""}
        <div class="saved-area-row-actions">
          <button type="button" class="saved-area-action-btn rename" data-action="rename" title="Rename">✎ Rename</button>
          <button type="button" class="saved-area-action-btn delete" data-action="delete" title="Delete">🗑 Delete</button>
        </div>
      </div>`;
  }).join("");
  list.querySelectorAll(".saved-area-row").forEach((row) => {
    row.addEventListener("click", async (e) => {
      const actionEl = e.target.closest("[data-action]");
      const sid = row.dataset.sessionId;
      const session = _savedSessionsCache.find((s) => s.session_id === sid);
      if (!session) return;
      if (actionEl?.dataset.action === "delete") {
        e.stopPropagation();
        await deleteSession(session);
        return;
      }
      if (actionEl?.dataset.action === "rename") {
        e.stopPropagation();
        await _renameSavedSessionInline(session, row);
        return;
      }
      if (!_navigationGuardForActiveDeepPull("switch snapshots")) {
        return;
      }
      bumpUndoPillVersion();
      const snapshot = _createUndoSnapshot();
      await restoreNamedSession(session, { rowEl: row, undoSnapshot: snapshot });
      const restoredCount = _countRestoredFilterKeys(session.filter_state);
      _showUndoPill(snapshot, restoredCount);
    });
  });
}

function renderSavedSessionsList() {
  _renderSessionsList("saved-sessions", "saved-sessions-list", _savedSessionsCache);
}

function _readLegacySavedItems() {
  try {
    const raw = localStorage.getItem("lot_ledger_saved_areas");
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function _hideImportBanner() {
  document.getElementById("import-banner")?.classList.add("hidden");
}

let _toastTimer = null;
function _showToast(message, variant = "ok") {
  let toast = document.getElementById("ll-toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "ll-toast";
    toast.style.position = "fixed";
    toast.style.right = "16px";
    toast.style.bottom = "16px";
    toast.style.zIndex = "12000";
    toast.style.padding = "10px 12px";
    toast.style.borderRadius = "8px";
    toast.style.fontSize = "13px";
    toast.style.boxShadow = "0 10px 24px rgba(0,0,0,0.22)";
    toast.style.opacity = "0";
    toast.style.pointerEvents = "none";
    toast.style.transition = "opacity 150ms ease";
    document.body.appendChild(toast);
  }
  toast.textContent = String(message || "");
  if (variant === "error") {
    toast.style.background = "#ffe5e5";
    toast.style.color = "#7a1111";
    toast.style.border = "1px solid #ffc9c9";
  } else {
    toast.style.background = "#1f2937";
    toast.style.color = "#ffffff";
    toast.style.border = "1px solid #111827";
  }
  toast.style.opacity = "1";
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => {
    toast.style.opacity = "0";
  }, 2000);
}

async function _importLegacySavedItems() {
  bumpUndoPillVersion();
  const items = _readLegacySavedItems();
  if (!items.length) {
    _hideImportBanner();
    return;
  }
  for (const item of items) {
    const t = item?.type || "area";
    if (t === "parcel") {
      await _apiJson("/api/parcels", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({
          account_num: item.account_num,
          county: item.county || "dcad",
          payload: {
            account_num: item.account_num,
            county: item.county || "dcad",
            name: item.name,
            lat: item.lat,
            lng: item.lng,
            geometry: item.geometry || null,
          },
        }),
      });
      continue;
    }
    if (t === "location") {
      await _apiJson("/api/areas", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({
          name: item.name || "Saved location",
          type: "location",
          lat: item.lat,
          lng: item.lng,
          filter_state: null,
        }),
      });
      continue;
    }
    await _apiJson("/api/areas", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({
        name: item.name || "Saved area",
        type: "area",
        polygon: Array.isArray(item.latlngs) ? item.latlngs : [],
        filter_state: item.filter_state && typeof item.filter_state === "object" ? item.filter_state : null,
      }),
    });
  }
  localStorage.removeItem("lot_ledger_saved_areas");
  _hideImportBanner();
  await _reloadSavedResources();
}

function _maybeShowImportBanner() {
  const dismissed = localStorage.getItem("lot_ledger_import_dismissed");
  if (dismissed) return;
  const items = _readLegacySavedItems();
  if (!items.length) return;

  const banner = document.getElementById("import-banner");
  const text = document.getElementById("import-banner-text");
  const importBtn = document.getElementById("btn-import-local");
  const dismissBtn = document.getElementById("btn-dismiss-import");
  if (!banner || !text || !importBtn || !dismissBtn) return;

  text.textContent = `Found ${items.length} saved item${items.length === 1 ? "" : "s"} in this browser`;
  banner.classList.remove("hidden");

  const onImport = async () => {
    importBtn.disabled = true;
    dismissBtn.disabled = true;
    try {
      await _importLegacySavedItems();
    } catch (err) {
      console.error("import failed", err);
    } finally {
      importBtn.disabled = false;
      dismissBtn.disabled = false;
    }
  };
  const onDismiss = () => {
    localStorage.setItem("lot_ledger_import_dismissed", "1");
    _hideImportBanner();
  };

  importBtn.onclick = () => void onImport();
  dismissBtn.onclick = onDismiss;
}

const MapToolbar = L.Control.extend({
  options: { position: "topleft" },
  onAdd() {
    const container = L.DomUtil.create("div", "leaflet-bar map-toolbar");
    L.DomEvent.disableClickPropagation(container);

    const drawBtn = L.DomUtil.create("a", "", container);
    drawBtn.id = "btn-draw";
    drawBtn.href = "#";
    drawBtn.title = "Draw area to analyze";
    drawBtn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="16 3 21 8 8 21 3 21 3 16 16 3"></polygon></svg>';
    L.DomEvent.on(drawBtn, "click", (e) => {
      L.DomEvent.preventDefault(e);
      if (!_navigationGuardForActiveDeepPull("draw a new area")) return;
      _setMeasureModeEnabled(false);
      const handler = getPolygonDrawHandler();
      if (!handler) return;
      if (handler.enabled()) {
        handler.disable();
        map.getContainer().classList.remove("drawing-active");
      } else {
        handler.enable();
      }
    });

    const cancelBtn = L.DomUtil.create("a", "hidden", container);
    cancelBtn.id = "btn-draw-cancel";
    cancelBtn.href = "#";
    cancelBtn.title = "Cancel current drawing";
    cancelBtn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>';
    L.DomEvent.on(cancelBtn, "click", (e) => {
      L.DomEvent.preventDefault(e);
      const handler = getPolygonDrawHandler();
      if (handler && handler.enabled()) handler.disable();
      map.getContainer().classList.remove("drawing-active");
      drawHelper.classList.add("hidden");
      cancelBtn.classList.add("hidden");
      document.getElementById("btn-draw")?.classList.remove("active");
    });

    const clearBtn = L.DomUtil.create("a", "hidden", container);
    clearBtn.id = "btn-draw-clear";
    clearBtn.href = "#";
    clearBtn.title = "Clear results and draw a new area";
    clearBtn.textContent = "CLEAR";
    L.DomEvent.on(clearBtn, "click", (e) => {
      L.DomEvent.preventDefault(e);
      clearDrawResults();
      clearActiveItem();
    });

    const measureBtn = L.DomUtil.create("a", "", container);
    measureBtn.id = "btn-measure-toggle";
    measureBtn.href = "#";
    measureBtn.title = "Toggle ruler mode";
    measureBtn.textContent = "RULR";
    L.DomEvent.on(measureBtn, "click", (e) => {
      L.DomEvent.preventDefault(e);
      _setMeasureModeEnabled(!_measureModeEnabled);
    });

    // LYRS dropdown — collapses HOA / FLOOD / CNTY into a single toolbar
    // slot with a popover. Was 3 buttons before; the chevron in the
    // toolbar's vertical track started overlapping the bottom two
    // after FLOOD was added. Future overlays (PMTiles or otherwise) slot
    // into the popover instead of growing the toolbar height.
    const lyrsBtn = L.DomUtil.create("a", "", container);
    lyrsBtn.id = "btn-layers-toggle";
    lyrsBtn.href = "#";
    lyrsBtn.title = "Map overlay layers";
    lyrsBtn.textContent = "LYRS";

    const lyrsPopover = L.DomUtil.create("div", "map-toolbar-popover hidden", container);
    lyrsPopover.id = "map-layers-popover";
    lyrsPopover.innerHTML = `
      <a href="#" class="map-toolbar-popover-row" id="btn-hoa-toggle"
         title="Toggle HOA zone boundaries">
        <span class="map-toolbar-popover-dot" aria-hidden="true"></span>
        <span class="map-toolbar-popover-label">HOA</span>
      </a>
      <a href="#" class="map-toolbar-popover-row" id="btn-flood-toggle"
         title="Toggle FEMA flood zones overlay">
        <span class="map-toolbar-popover-dot" aria-hidden="true"></span>
        <span class="map-toolbar-popover-label">Flood Zones</span>
      </a>
      <a href="#" class="map-toolbar-popover-row" id="btn-county-toggle"
         title="Toggle county boundary lines">
        <span class="map-toolbar-popover-dot" aria-hidden="true"></span>
        <span class="map-toolbar-popover-label">County Lines</span>
      </a>
    `;

    // Stop popover clicks from propagating to the map (avoid drag/select).
    L.DomEvent.disableClickPropagation(lyrsPopover);
    L.DomEvent.disableScrollPropagation(lyrsPopover);

    function _closeLyrsPopover() {
      lyrsPopover.classList.add("hidden");
      lyrsBtn.classList.remove("popover-open");
    }
    function _toggleLyrsPopover() {
      const isOpen = !lyrsPopover.classList.contains("hidden");
      if (isOpen) {
        _closeLyrsPopover();
      } else {
        // Vertically anchor the popover to the LYRS button so it aligns
        // with the button regardless of where in the toolbar stack the
        // button sits. Without this the popover renders at top: 0 of the
        // toolbar container (aligned with the topmost button) which is
        // way above LYRS.
        lyrsPopover.style.top = `${lyrsBtn.offsetTop}px`;
        lyrsPopover.classList.remove("hidden");
        lyrsBtn.classList.add("popover-open");
      }
    }
    function _refreshLyrsActiveState() {
      // Tint the LYRS button "active" if ANY overlay is on, so the user
      // sees something is enabled even with the popover closed.
      const anyOn = hoaVisible || floodZonesVisible || countyVisible;
      lyrsBtn.classList.toggle("active", anyOn);
    }
    // Expose so the underlying togglers can call it after they flip
    // their visibility state.
    window._refreshLyrsActiveState = _refreshLyrsActiveState;

    L.DomEvent.on(lyrsBtn, "click", (e) => {
      L.DomEvent.preventDefault(e);
      L.DomEvent.stopPropagation(e);
      _toggleLyrsPopover();
    });

    // Click-outside-to-close. Use mousedown so we don't fight inner clicks.
    document.addEventListener("mousedown", (e) => {
      if (lyrsPopover.classList.contains("hidden")) return;
      if (lyrsPopover.contains(e.target) || lyrsBtn.contains(e.target)) return;
      _closeLyrsPopover();
    });

    // Wire each popover row to the existing toggle function. Each row keeps
    // its own id ("btn-hoa-toggle" etc.) so the existing toggleHoaLayer /
    // toggleCountyLayer / toggleFloodZonesLayer functions can keep flipping
    // .active on those elements unchanged.
    const hoaBtn = lyrsPopover.querySelector("#btn-hoa-toggle");
    L.DomEvent.on(hoaBtn, "click", (e) => {
      L.DomEvent.preventDefault(e);
      toggleHoaLayer();
      _refreshLyrsActiveState();
    });
    const floodBtn = lyrsPopover.querySelector("#btn-flood-toggle");
    L.DomEvent.on(floodBtn, "click", (e) => {
      L.DomEvent.preventDefault(e);
      toggleFloodZonesLayer();
      _refreshLyrsActiveState();
    });
    const countyBtn = lyrsPopover.querySelector("#btn-county-toggle");
    L.DomEvent.on(countyBtn, "click", (e) => {
      L.DomEvent.preventDefault(e);
      toggleCountyLayer();
      _refreshLyrsActiveState();
    });

    // ZOOM toggle — click-mode (jump = zoom on click, stay = keep current
    // zoom). Active (green) = jump mode. Bidirectional with setClickMode
    // so any caller mutating currentClickMode keeps this button visually
    // in sync via updateClickModeButtonState below.
    const zoomBtn = L.DomUtil.create("a", "", container);
    zoomBtn.id = "btn-zoom-toggle";
    zoomBtn.href = "#";
    zoomBtn.title = "Toggle auto-zoom on parcel/comp click";
    zoomBtn.textContent = "ZOOM";
    L.DomEvent.on(zoomBtn, "click", (e) => {
      L.DomEvent.preventDefault(e);
      setClickMode(currentClickMode === "jump" ? "stay" : "jump");
    });

    // OAC (Outside Area Comps) toggle — mirrors the #prop-outside-area
    // checkbox in the Map Filters card. Clicking either UI updates the
    // shared state and reflects on the other surface. Active (green) =
    // outside-polygon comps included.
    const oacBtn = L.DomUtil.create("a", "", container);
    oacBtn.id = "btn-outside-area-toggle";
    oacBtn.href = "#";
    oacBtn.title = "Toggle Outside Area Comps (also in Property Type Filters)";
    oacBtn.textContent = "OAC";
    L.DomEvent.on(oacBtn, "click", (e) => {
      L.DomEvent.preventDefault(e);
      const checkbox = document.getElementById("prop-outside-area");
      if (!checkbox) return;
      checkbox.checked = !checkbox.checked;
      // Bubble a change event so the existing checkbox-change wiring fires
      // (refilters comps, updates count chip, etc.). Toolbar button's own
      // visual state then updates via the change-listener below.
      checkbox.dispatchEvent(new Event("change", { bubbles: true }));
    });

    return container;
  },
});
new MapToolbar().addTo(map);

// Google Maps-style basemap switcher — bottom-left pill
const BasemapSwitcher = L.Control.extend({
  options: { position: "bottomleft" },
  onAdd() {
    const container = L.DomUtil.create("div", "basemap-switcher");
    L.DomEvent.disableClickPropagation(container);
    const mapBtn = L.DomUtil.create("button", "basemap-btn active", container);
    mapBtn.id = "bm-map";
    mapBtn.textContent = "Map";

    const satBtn = L.DomUtil.create("button", "basemap-btn", container);
    satBtn.id = "bm-sat";
    satBtn.textContent = "Satellite";

    const contrastBtn = L.DomUtil.create("button", "basemap-btn", container);
    contrastBtn.id = "bm-contrast";
    contrastBtn.textContent = "Contrast";

    function activateBasemap(name, silent = false) {
      [streetLayer, contrastLayer, satelliteLayer, labelsLayer].forEach((layer) => {
        if (map.hasLayer(layer)) map.removeLayer(layer);
      });

      if (name === "street") {
        streetLayer.addTo(map);
      } else if (name === "contrast") {
        contrastLayer.addTo(map);
      } else if (name === "satellite") {
        satelliteLayer.addTo(map);
        labelsLayer.addTo(map);
      }

      activeBasemap = name;
      mapBtn.classList.toggle("active", name === "street");
      contrastBtn.classList.toggle("active", name === "contrast");
      satBtn.classList.toggle("active", name === "satellite");
      try {
        localStorage.setItem(BASEMAP_STORAGE_KEY, name);
      } catch (_) {
        // Ignore storage failures (private mode, blocked storage, etc.).
      }
      // Silent mode: skip repaint on page-load restore — protomaps handles its
      // own initial paint. For user-triggered switches, remove and re-add the
      // layer to force a clean reinitialisation. This is the same path that
      // works on first page load and avoids relying on GridLayer.redraw() which
      // may not exist on the v3.1.2 instance.
      if (!silent && map.hasLayer(browseLayer)) {
        browseLayer.remove();
        browseLayer.addTo(map);
      }
    }

    L.DomEvent.on(mapBtn, "click", () => {
      if (activeBasemap === "street") return;
      activateBasemap("street");
    });

    L.DomEvent.on(contrastBtn, "click", () => {
      if (activeBasemap === "contrast") return;
      activateBasemap("contrast");
    });

    L.DomEvent.on(satBtn, "click", () => {
      if (activeBasemap === "satellite") return;
      activateBasemap("satellite");
    });

    // Tile layers already applied pre-init (before browseLayer was added).
    // Just sync button active states to match the already-correct activeBasemap.
    mapBtn.classList.toggle("active", activeBasemap === "street");
    contrastBtn.classList.toggle("active", activeBasemap === "contrast");
    satBtn.classList.toggle("active", activeBasemap === "satellite");

    return container;
  },
});
new BasemapSwitcher().addTo(map);

// ── Propelio per-address comp fetch + render ─────────────────────────────
// Fires after every address search. Hits /api/propelio/by-address (which is
// auth-gated and 7-day cached). Renders each returned comp as a pulsing
// cyan dot with a popup. Subject parcel data lands in window._propelioLast
// for future popup-enrichment work; not yet wired into the existing parcel
// popups (Chunk 4 territory).
function _propelioEscape(s) {
  return String(s ?? "").replace(/[&<>"']/g, (m) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m])
  );
}

// Find the Propelio comp matching a parcel by account number. Returns null
// if no comps loaded or no match. Used by the unified popup to enrich a
// CAD parcel popup with its matched comp's MLS data + rating buttons.
function _findMatchedCompForAccount(accountNum) {
  const account = String(accountNum || "").trim();
  if (!account) return null;
  const comps = window._propelioLast?.comps;
  if (!Array.isArray(comps)) return null;
  return comps.find((c) => String(c?.parcel_account_num || "").trim() === account) || null;
}

// Render the Good / Bad / Clear rating button row. Used for both
// standalone-comp contexts (e.g., sidebar comp list — pass comp only)
// AND unified parcel-popup contexts where a parcel + optional matched
// comp BOTH get rated by the same click (pass both). Per the 2026-05-24
// design call: ONE button row per popup, not two — even when both a
// parcel and a matched comp exist. The click handler at the bottom of
// this file reads the emitted data attrs and writes to whichever rating
// tables apply (parcel_ratings + comp_ratings together when both keys
// are present; just one when only one is present).
function _buildRatingButtonsHtml(comp, parcel) {
  const compKey = String(comp?.comp_address_key || "").trim();
  const parcelCounty = String(parcel?.source_county || parcel?.county || "").trim().toLowerCase();
  const parcelAccount = String(parcel?.account_num || "").trim();
  const hasComp = Boolean(compKey);
  const hasParcel = Boolean(parcelCounty && parcelAccount);
  if (!hasComp && !hasParcel) return "";
  // Active rating reflected from whichever source we have. Prefer the
  // parcel-level judgment when present, but FALL BACK to the comp rating
  // when the parcel has none. This matters for spillover (outside-area)
  // comps: they get a throwaway dummy parcel with no user_rating that is
  // never cached, so a strict parcel-priority read would lose the comp's
  // own good/bad highlight on popup reopen (regression from the
  // 2026-05-24 single-button consolidation). comp.user_rating persists in
  // window._propelioLast.comps, so the fallback restores the highlight.
  const sourceRating = parcel?.user_rating ?? comp?.user_rating;
  const currentRating = sourceRating === "good" || sourceRating === "bad" ? sourceRating : null;
  const ratingsEnabled = Boolean(_currentLoadedAreaId && (hasComp || hasParcel));
  const goodActive = ratingsEnabled && currentRating === "good" ? " is-active" : "";
  const badActive = ratingsEnabled && currentRating === "bad" ? " is-active" : "";
  const compAttr = hasComp ? ` data-comp-key="${_propelioEscape(compKey)}"` : "";
  const parcelAttrs = hasParcel
    ? ` data-county="${_propelioEscape(parcelCounty)}" data-account-num="${_propelioEscape(parcelAccount)}"`
    : "";
  const disabledAttr = ratingsEnabled ? "" : " disabled";
  const wrapTitle = ratingsEnabled ? "" : ' title="Save this area to enable ratings"';
  const hintHtml = ratingsEnabled ? "" : `<div class="propelio-rate-hint">Save area to enable ratings</div>`;
  return `
      <div class="propelio-popup-rating${ratingsEnabled ? "" : " is-disabled"}"${compAttr}${parcelAttrs}${wrapTitle}>
        <button type="button" class="propelio-rate-btn good${goodActive}" data-rating="good"${compAttr}${parcelAttrs}${disabledAttr}>Good</button>
        <button type="button" class="propelio-rate-btn bad${badActive}" data-rating="bad"${compAttr}${parcelAttrs}${disabledAttr}>Bad</button>
        <button type="button" class="propelio-rate-btn clear" data-rating="clear"${compAttr}${parcelAttrs}${disabledAttr}>Clear</button>
      </div>${hintHtml}`;
}

// Render the MLS-comp section for a unified popup. Mirrors the layout of
// the Propelio standalone popup but is meant to live underneath the CAD
// table inside makePopupHtml. Includes price, sold/list info, dims,
// beds/baths, MLS metadata, school/agent enrichment, photo count, full
// remarks, and a best-effort external MLS lookup link.
function _buildPropelioCompSectionHtml(c) {
  if (!c) return "";
  const fmtPrice = (n) => Number.isFinite(n) ? `$${Math.round(n).toLocaleString()}` : "—";
  const fmtNum = (n) => Number.isFinite(n) ? Number(n).toLocaleString() : null;
  const fmtDate = (s) => {
    if (!s) return null;
    const d = new Date(s);
    return Number.isFinite(d.getTime()) ? d.toISOString().slice(0, 10) : null;
  };
  const ex = c?.extra || {};
  const raw = c?.extra?.raw || {};
  const status = String(c?.status || "");
  const isSold = status === "sold";
  const firstSeenDate = fmtDate(c?.first_seen_at);
  const todayDate = new Date().toISOString().slice(0, 10);
  const capturedLabel = firstSeenDate
    ? (firstSeenDate === todayDate ? "🔥 Captured today" : `Captured ${firstSeenDate}`)
    : "";

  const sqft = fmtNum(c?.sqft);
  const lot = fmtNum(c?.lot_size);
  const year = Number.isFinite(c?.year_built) ? c.year_built : null;
  const beds = ex.beds;
  const baths = ex.baths;
  const bathsFull = ex.baths_full;
  const bathsHalf = ex.baths_half;
  const garage = ex.garage;
  const dom = ex.dom;
  const listPrice = Number(ex.list_price);
  const closeDate = fmtDate(ex.close_date);
  const modifiedTs = fmtDate(ex.modified_timestamp);
  const propertyType = ex.property_type;
  const mls = ex.mls || "";
  const source = ex.source || "";
  const zipCode = ex.zip || "";
  const remarks = String(ex.remarks || raw.remarks || "").trim();
  const listingAgent = {
    name: raw.listing_agent_name,
    phone: raw.listing_agent_phone,
    email: raw.listing_agent_email,
    officeName: raw.listing_office_name,
    officePhone: raw.listing_office_phone,
    officeEmail: raw.listing_office_email,
  };
  const buyerAgent = {
    name: raw.buyer_agent_name,
    phone: raw.buyer_agent_phone,
    email: raw.buyer_agent_email,
    officeName: raw.buyer_office_name,
    officePhone: raw.buyer_office_phone,
    officeEmail: raw.buyer_office_email,
  };
  const schools = {
    elementary: raw.elementary_school,
    middle: raw.middle_school || raw.junior_high_school || raw.intermediate_school,
    high: raw.high_school || raw.senior_high_school,
  };
  const photoCountValue = Number(raw.photo_count);
  const photoCount = Number.isFinite(photoCountValue) ? photoCountValue : 0;

  const dims = [];
  if (sqft) dims.push(`${sqft} sqft`);
  if (lot) dims.push(`${lot} sqft lot`);
  if (year) dims.push(`built ${year}`);

  const bbLine = [];
  if (beds != null) bbLine.push(`${beds}bd`);
  if (Number.isFinite(bathsFull) || Number.isFinite(bathsHalf)) {
    const fullStr = Number.isFinite(bathsFull) ? `${bathsFull}` : "0";
    const halfStr = Number.isFinite(bathsHalf) && bathsHalf > 0 ? ` + ${bathsHalf}½` : "";
    bbLine.push(`${fullStr}ba${halfStr}`);
  } else if (baths != null) {
    bbLine.push(`${baths}ba`);
  }
  if (Number.isFinite(garage) && garage > 0) bbLine.push(`${garage}-car gar`);

  const priceLineParts = [fmtPrice(c?.price), _propelioEscape(status)];
  let listVsCloseHtml = "";
  if (isSold && Number.isFinite(listPrice) && Number.isFinite(c?.price) && listPrice > 0 && Math.abs(listPrice - c.price) > 1) {
    const delta = c.price - listPrice;
    const deltaPct = Math.round((delta / listPrice) * 100);
    const sign = delta > 0 ? "+" : "";
    listVsCloseHtml = `<div class="propelio-popup-meta">List was ${fmtPrice(listPrice)} (${sign}${deltaPct}% close vs list)</div>`;
  } else if (!isSold && Number.isFinite(listPrice) && listPrice > 0 && Number(c?.price) !== listPrice) {
    listVsCloseHtml = `<div class="propelio-popup-meta">List: ${fmtPrice(listPrice)}</div>`;
  }

  const soldMeta = [];
  if (isSold && closeDate) soldMeta.push(`closed ${closeDate}`);
  if (Number.isFinite(dom)) soldMeta.push(`DOM ${dom}`);

  const subLine = [];
  if (c?.neighborhood) subLine.push(c.neighborhood);
  if (zipCode) subLine.push(zipCode);

  const idLine = [];
  if (mls) idLine.push(`MLS ${mls}`);
  if (propertyType) idLine.push(propertyType);

  const schoolsHtml = schools.elementary || schools.middle || schools.high
    ? `<div class="propelio-popup-schools">
        ${schools.elementary ? `<span class="propelio-popup-school"><span class="label">ES</span> ${_propelioEscape(schools.elementary)}</span>` : ""}
        ${schools.middle ? `<span class="propelio-popup-school"><span class="label">MS</span> ${_propelioEscape(schools.middle)}</span>` : ""}
        ${schools.high ? `<span class="propelio-popup-school"><span class="label">HS</span> ${_propelioEscape(schools.high)}</span>` : ""}
      </div>`
    : "";
  const listingAgentHtml = listingAgent.name
    ? `<div class="propelio-popup-agent-block">
        <div class="propelio-popup-agent-label">Listing Agent</div>
        <div class="propelio-popup-agent-name">${_propelioEscape(listingAgent.name || "—")}</div>
        ${listingAgent.officeName ? `<div class="propelio-popup-agent-line">${_propelioEscape(listingAgent.officeName)}</div>` : ""}
        <div class="propelio-popup-agent-contact">
          ${listingAgent.phone ? `<a href="tel:${encodeURIComponent(listingAgent.phone)}">${_propelioEscape(listingAgent.phone)}</a>` : ""}
          ${listingAgent.email ? `<a href="mailto:${encodeURIComponent(listingAgent.email)}">${_propelioEscape(listingAgent.email)}</a>` : ""}
          ${listingAgent.officePhone && listingAgent.officePhone !== listingAgent.phone ? `<a href="tel:${encodeURIComponent(listingAgent.officePhone)}" class="muted">office: ${_propelioEscape(listingAgent.officePhone)}</a>` : ""}
        </div>
      </div>`
    : "";
  const buyerAgentHtml = isSold && buyerAgent.name
    ? `<div class="propelio-popup-agent-block">
        <div class="propelio-popup-agent-label">Buyer Agent</div>
        <div class="propelio-popup-agent-name">${_propelioEscape(buyerAgent.name || "—")}</div>
        ${buyerAgent.officeName ? `<div class="propelio-popup-agent-line">${_propelioEscape(buyerAgent.officeName)}</div>` : ""}
        <div class="propelio-popup-agent-contact">
          ${buyerAgent.phone ? `<a href="tel:${encodeURIComponent(buyerAgent.phone)}">${_propelioEscape(buyerAgent.phone)}</a>` : ""}
          ${buyerAgent.email ? `<a href="mailto:${encodeURIComponent(buyerAgent.email)}">${_propelioEscape(buyerAgent.email)}</a>` : ""}
          ${buyerAgent.officePhone && buyerAgent.officePhone !== buyerAgent.phone ? `<a href="tel:${encodeURIComponent(buyerAgent.officePhone)}" class="muted">office: ${_propelioEscape(buyerAgent.officePhone)}</a>` : ""}
        </div>
      </div>`
    : "";
  const photoCountHtml = photoCount > 0
    ? `<div class="propelio-popup-meta-mute">${photoCount} listing photo${photoCount === 1 ? "" : "s"} (Propelio-hosted)</div>`
    : "";
  const remarksHtml = remarks
    ? `<div class="propelio-popup-remarks-full">${_propelioEscape(remarks)}</div>`
    : "";
  const realtorLinkHtml = mls
    ? `<a class="propelio-popup-realtor-link" href="https://www.realtor.com/realestateandhomes-search/MLSID-${encodeURIComponent(mls)}" target="_blank" rel="noopener noreferrer">🔗 Look up MLS# ${_propelioEscape(mls)} on Realtor.com (best-effort)</a>`
    : "";

  return `
    <div class="popup-propelio-section">
      <div class="popup-propelio-header">MLS Comp</div>
      <div class="propelio-popup-price">${priceLineParts.join(" · ")}</div>
      ${capturedLabel ? `<div class="propelio-popup-meta">${_propelioEscape(capturedLabel)}</div>` : ""}
      ${listVsCloseHtml}
      ${soldMeta.length ? `<div class="propelio-popup-meta">${_propelioEscape(soldMeta.join(" · "))}</div>` : ""}
      ${dims.length ? `<div class="propelio-popup-meta">${_propelioEscape(dims.join(" · "))}</div>` : ""}
      ${bbLine.length ? `<div class="propelio-popup-meta">${_propelioEscape(bbLine.join(" · "))}</div>` : ""}
      ${subLine.length ? `<div class="propelio-popup-meta">${_propelioEscape(subLine.join(" · "))}</div>` : ""}
      ${idLine.length ? `<div class="propelio-popup-meta">${_propelioEscape(idLine.join(" · "))}${source ? ` <span class="propelio-popup-source">(${_propelioEscape(source)})</span>` : ""}</div>` : ""}
      ${modifiedTs ? `<div class="popup-propelio-meta-mute">Last Pulled from MLS ${_propelioEscape(modifiedTs)}</div>` : ""}
      ${schoolsHtml}
      ${listingAgentHtml}
      ${buyerAgentHtml}
      ${photoCountHtml}
      ${remarksHtml}
      ${realtorLinkHtml}
    </div>
  `;
}

// Async resolver for Propelio comp popup content. Returns the unified
// CAD+MLS popup when the comp's parcel is reachable (either already in the
// current draw analysis OR fetchable from the parcels API for spillover
// comps via parcel_county + parcel_account_num set in api/propelio/
// parcel_match.py). Falls back to the standalone Propelio-only popup
// (_propelioBuildPopup) if the comp doesn't have a matched parcel or the
// fetch fails. Used by the click handler and flyToAndOpenPropelioComp.
async function _resolvePropelioPopupContent(comp) {
  const accountNum = String(comp?.parcel_account_num || "").trim();

  // Fast path: in-memory match against the current draw analysis.
  if (accountNum && lastAnalysisGeojson && Array.isArray(lastAnalysisGeojson.features)) {
    const matched = lastAnalysisGeojson.features.find(
      (f) => String(f?.properties?.account_num || "").trim() === accountNum
    );
    if (matched?.properties) return makePopupHtml(matched.properties);
  }

  // Slow path: spillover comps (Propelio search circumradius extends past
  // the polygon) still have parcel_county + parcel_account_num attached by
  // parcel_match.py. Fetch the parcel detail so the unified popup shows
  // for those too — same behavior as clicking the parcel from browse mode.
  const county = String(comp?.parcel_county || "").trim();
  if (accountNum && county) {
    try {
      const resp = await fetch(`/api/parcel/${county}/${accountNum}`);
      if (resp.ok) {
        const detail = await resp.json();
        return makePopupHtml(detail.properties || detail);
      }
    } catch (err) {
      console.error("Propelio spillover parcel fetch failed", err);
    }
  }

  // Final fallback: unmatched comp (no account_num at all, or fetch
  // failed) gets the standalone Propelio popup.
  return _propelioBuildPopup(comp);
}

async function _openUnifiedPropelioPopup(comp, latlng) {
  if (!comp || !latlng) return;
  const accountNum = String(comp?.parcel_account_num || "").trim();

  if (accountNum && lastAnalysisGeojson && Array.isArray(lastAnalysisGeojson.features)) {
    const matched = lastAnalysisGeojson.features.find(
      (f) => String(f?.properties?.account_num || "").trim() === accountNum
    );
    if (matched?.properties) {
      openParcelDetailPanel(matched.properties, { latlng, matchedComp: comp, geometry: matched.geometry });
      return;
    }
  }

  const county = String(comp?.parcel_county || "").trim();
  if (accountNum && county) {
    try {
      const resp = await fetch(`/api/parcel/${county}/${accountNum}`);
      if (resp.ok) {
        const detail = await resp.json();
        openParcelDetailPanel(detail.properties || detail, { latlng, matchedComp: comp, geometry: detail.geometry });
        return;
      }
    } catch (err) {
      console.error("Propelio spillover parcel fetch failed", err);
    }
  }

  openParcelDetailPanel({
    addr: comp?.address || "Unknown address",
    owner: "N/A",
    land_val: "N/A",
    tot_val: "N/A",
    land_pct: "N/A",
    lot_sqft: Number.isFinite(Number(comp?.lot_size)) ? `${Math.round(Number(comp.lot_size)).toLocaleString()} sf` : "N/A",
    lot_acres: Number.isFinite(Number(comp?.lot_size)) ? (Number(comp.lot_size) / 43560).toFixed(2) : "N/A",
    frontage: "N/A",
    depth: "N/A",
    state_code: "N/A",
    zoning: "N/A",
    school: "N/A",
    yr_built: Number.isFinite(Number(comp?.year_built)) ? Number(comp.year_built) : "N/A",
    sqft: Number.isFinite(Number(comp?.sqft)) ? Math.round(Number(comp.sqft)).toLocaleString() : "N/A",
    source_county: county || "dcad",
    account_num: accountNum || "",
    lat: comp?.extra?.lat || null,
    lng: comp?.extra?.lon || comp?.extra?.lng || null,
  }, { latlng, matchedComp: comp, geometry: comp?.parcel_geom || null });
}

function _propelioBuildPopup(c) {
  // Standalone Propelio-only popup. Used as the final fallback by
  // _resolvePropelioPopupContent — direct callers should prefer that
  // resolver so spillover comps get the unified popup.
  const accountNum = String(c?.parcel_account_num || "").trim();
  if (accountNum && lastAnalysisGeojson && Array.isArray(lastAnalysisGeojson.features)) {
    const matched = lastAnalysisGeojson.features.find(
      (f) => String(f?.properties?.account_num || "").trim() === accountNum
    );
    if (matched?.properties) {
      return makePopupHtml(matched.properties);
    }
  }

  const fmtPrice = (n) => Number.isFinite(n) ? `$${Math.round(n).toLocaleString()}` : "—";
  const fmtNum = (n) => Number.isFinite(n) ? Number(n).toLocaleString() : null;
  const fmtDate = (s) => {
    if (!s) return null;
    const d = new Date(s);
    return Number.isFinite(d.getTime()) ? d.toISOString().slice(0, 10) : null;
  };
  const ex = c?.extra || {};
  const status = String(c?.status || "");
  const isSold = status === "sold";

  const sqft = fmtNum(c?.sqft);
  const lot = fmtNum(c?.lot_size);
  const year = Number.isFinite(c?.year_built) ? c.year_built : null;
  const beds = ex.beds;
  const baths = ex.baths;
  const bathsFull = ex.baths_full;
  const bathsHalf = ex.baths_half;
  const garage = ex.garage;
  const dom = ex.dom;
  const listPrice = Number(ex.list_price);
  const closeDate = fmtDate(ex.close_date);
  const modifiedTs = fmtDate(ex.modified_timestamp);
  const propertyType = ex.property_type;
  const mls = ex.mls || "";
  const source = ex.source || "";
  const zipCode = ex.zip || "";
  const remarks = String(ex.remarks || "").trim();

  // Dimensions line: sqft · lot · year built
  const dims = [];
  if (sqft) dims.push(`${sqft} sqft`);
  if (lot) dims.push(`${lot} sqft lot`);
  if (year) dims.push(`built ${year}`);

  // Bed/bath/garage line — prefer baths_full+baths_half breakdown if present
  const bbLine = [];
  if (beds != null) bbLine.push(`${beds}bd`);
  if (Number.isFinite(bathsFull) || Number.isFinite(bathsHalf)) {
    const fullStr = Number.isFinite(bathsFull) ? `${bathsFull}` : "0";
    const halfStr = Number.isFinite(bathsHalf) && bathsHalf > 0 ? ` + ${bathsHalf}½` : "";
    bbLine.push(`${fullStr}ba${halfStr}`);
  } else if (baths != null) {
    bbLine.push(`${baths}ba`);
  }
  if (Number.isFinite(garage) && garage > 0) bbLine.push(`${garage}-car gar`);

  // Sold-or-active price line
  const priceLineParts = [fmtPrice(c?.price), _propelioEscape(status)];
  // If sold and list price was different, show the delta in parens
  let listVsCloseHtml = "";
  if (isSold && Number.isFinite(listPrice) && Number.isFinite(c?.price) && listPrice > 0 && Math.abs(listPrice - c.price) > 1) {
    const delta = c.price - listPrice;
    const deltaPct = Math.round((delta / listPrice) * 100);
    const sign = delta > 0 ? "+" : "";
    listVsCloseHtml = `<div class="propelio-popup-meta">List was ${fmtPrice(listPrice)} (${sign}${deltaPct}% close vs list)</div>`;
  } else if (!isSold && Number.isFinite(listPrice) && listPrice > 0 && Number(c?.price) !== listPrice) {
    listVsCloseHtml = `<div class="propelio-popup-meta">List: ${fmtPrice(listPrice)}</div>`;
  }

  // Sold-specific metadata (close date, DOM)
  const soldMeta = [];
  if (isSold && closeDate) soldMeta.push(`closed ${closeDate}`);
  if (Number.isFinite(dom)) soldMeta.push(`DOM ${dom}`);

  // Subdivision + zip
  const subLine = [];
  if (c?.neighborhood) subLine.push(c.neighborhood);
  if (zipCode) subLine.push(zipCode);

  // MLS / source / property type
  const idLine = [];
  if (mls) idLine.push(`MLS ${mls}`);
  if (propertyType) idLine.push(propertyType);

  // Remarks excerpt (truncated)
  const REMARKS_MAX = 280;
  const remarksHtml = remarks
    ? `<div class="propelio-popup-remarks">${_propelioEscape(remarks.length > REMARKS_MAX ? remarks.slice(0, REMARKS_MAX).trim() + "…" : remarks)}</div>`
    : "";

  const ratingHtml = _buildRatingButtonsHtml(c);

  return `
    <div class="propelio-popup">
      <div class="propelio-popup-addr">${_propelioEscape(c?.address || "")}</div>
      <div class="propelio-popup-price">${priceLineParts.join(" · ")}</div>
      ${listVsCloseHtml}
      ${soldMeta.length ? `<div class="propelio-popup-meta">${_propelioEscape(soldMeta.join(" · "))}</div>` : ""}
      ${dims.length ? `<div class="propelio-popup-meta">${_propelioEscape(dims.join(" · "))}</div>` : ""}
      ${bbLine.length ? `<div class="propelio-popup-meta">${_propelioEscape(bbLine.join(" · "))}</div>` : ""}
      ${subLine.length ? `<div class="propelio-popup-meta">${_propelioEscape(subLine.join(" · "))}</div>` : ""}
      ${idLine.length ? `<div class="propelio-popup-meta">${_propelioEscape(idLine.join(" · "))}${source ? ` <span class="propelio-popup-source">(${_propelioEscape(source)})</span>` : ""}</div>` : ""}
      ${modifiedTs ? `<div class="propelio-popup-meta-mute">Last Pulled from MLS ${_propelioEscape(modifiedTs)}</div>` : ""}
      ${remarksHtml}
      ${ratingHtml}
    </div>
  `;
}

// CMA settings chip — bottom-left Leaflet control showing the filter
// Propelio applied + count + Propelio's own ARV estimate for the most
// recent fetch. Updated by firePropelioFetch when that path comes back
// online via the planned "Comps" button (see firePropelioFetch comment
// below). Currently dormant — no auto-pull on address search.
const PropelioCmaChip = L.Control.extend({
  options: { position: "bottomleft" },
  onAdd() {
    const el = L.DomUtil.create("div", "propelio-cma-chip hidden");
    L.DomEvent.disableClickPropagation(el);
    L.DomEvent.disableScrollPropagation(el);
    this._el = el;
    return el;
  },
  setData(data) {
    if (!this._el) return;
    if (!data || !Array.isArray(data.comps)) {
      this._el.classList.add("hidden");
      this._el.innerHTML = "";
      return;
    }
    const settings = data.cma_settings || {};
    const params = settings.params || {};
    const fmtMoney = (n) => Number.isFinite(n) ? `$${Math.round(n).toLocaleString()}` : null;
    const escape = _propelioEscape;
    const pieces = [];
    if (Number.isFinite(params.range)) pieces.push(`${params.range} mi radius`);
    if (Number.isFinite(params.months)) pieces.push(`last ${params.months} mo`);
    const lotMax = params.lot_size_acres?.max;
    if (Number.isFinite(lotMax)) pieces.push(`lot ≤ ${lotMax} ac`);
    const filterLine = pieces.join(" · ") || "Propelio default filter";

    const arv = fmtMoney(settings.arv);
    const arvType = settings.arv_type ? ` (${settings.arv_type})` : "";
    const cmaId = settings.cma_id ? `CMA #${settings.cma_id}` : "";
    const cached = data.cached ? " · cached" : "";
    const totalSales = settings.sales_count;
    const fetchedCount = Array.isArray(data.comps) ? data.comps.length : 0;
    const polygonMeta = data?.polygon_meta && typeof data.polygon_meta === "object" ? data.polygon_meta : null;
    const insideCount = Number.isFinite(Number(polygonMeta?.comps_in_polygon))
      ? Number(polygonMeta.comps_in_polygon)
      : null;
    const outsideCount = Number.isFinite(Number(polygonMeta?.comps_outside_polygon))
      ? Number(polygonMeta.comps_outside_polygon)
      : null;
    const inWindowCount = Number.isFinite(Number(totalSales)) ? Number(totalSales) : null;

    // Empty-result branch: render a friendly "no comps" notice with the warning text.
    if (data.comps.length === 0) {
      const warning = data.warning || "No comps returned by Propelio for this address.";
      this._el.innerHTML = `
        <div class="propelio-cma-chip-title">Propelio CMA — 0 comps</div>
        <div class="propelio-cma-chip-row">${escape(warning)}</div>
        <div class="propelio-cma-chip-row muted">Try: 4044 Williamsburg Rd · 6710 Northport Dr · 9012 Hunters Creek Dr</div>
      `;
      this._el.classList.remove("hidden");
      return;
    }

    this._el.innerHTML = `
      <div class="propelio-cma-chip-title">Propelio CMA${cached}</div>
      <div class="propelio-cma-chip-row">${escape(filterLine)}</div>
      <div class="propelio-cma-chip-row">${insideCount != null && outsideCount != null
        ? `${insideCount} inside · ${outsideCount} outside · ${fetchedCount} fetched · ${inWindowCount != null ? inWindowCount : "—"} total in window`
        : `${fetchedCount} comp${fetchedCount === 1 ? "" : "s"} returned${Number.isFinite(totalSales) && totalSales !== fetchedCount ? ` (of ${totalSales} sales)` : ""}`
      }</div>
      ${arv ? `<div class="propelio-cma-chip-row"><strong>Propelio ARV:</strong> ${escape(arv)}${escape(arvType)}</div>` : ""}
      ${cmaId ? `<div class="propelio-cma-chip-row muted">${escape(cmaId)}</div>` : ""}
    `;
    this._el.classList.remove("hidden");
  },
  hide() {
    if (this._el) {
      this._el.classList.add("hidden");
      this._el.innerHTML = "";
    }
  },
});
const propelioCmaChip = new PropelioCmaChip().addTo(map);


const PROPELIO_STATUS_PRIORITY = { sold: 3, pending: 2, active: 1 };

// Compute the dedup key for one comp. Pure function — no side effects.
// Strongest signal first: parcel_geom shape (when present, multiple
// listings on the same building/parcel collapse to one), then
// account+county (single-family fallback), then rounded lat/lng
// (geocode-drift fallback), then comp_address_key (last resort).
// Returns "" when nothing usable found (comp gets pushed into noKey list
// and rendered separately).
//
// EXTRACTED 2026-06-03 PM from _dedupCompsForRender so the click handler
// can reuse the same logic to find the winning comp when a dedup-loser
// is clicked from the sidebar list. KK bug: "1825 & 1827 Pollard Street"
// in the comps list does nothing on click because that listing lost its
// dedup election to another 1827 Pollard comp on the same parcel.
function _compDedupKey(c) {
  const round4 = (n) => {
    const x = Number(n);
    return Number.isFinite(x) ? x.toFixed(4) : null;
  };
  const geom = c?.parcel_geom;
  if (geom && (geom.type === "Polygon" || geom.type === "MultiPolygon")) {
    const gkey = geometryKey(geom);
    if (gkey) return `geom:${gkey}`;
  }
  const acct = String(c?.parcel_account_num || "").trim();
  const county = String(c?.parcel_county || "").trim().toLowerCase();
  if (acct && county) return `acct:${county}|${acct}`;
  const latlng = _propelioCompLatLng(c);
  const lat = latlng ? round4(latlng[0]) : null;
  const lng = latlng ? round4(latlng[1]) : null;
  if (lat && lng) return `ll:${lat},${lng}`;
  return String(c?.comp_address_key || "").trim();
}


function _dedupCompsForRender(comps) {
  // "One comp per footprint" — multiple condo units share a single
  // parcel_geom (the building outline) but each unit has its own
  // parcel_account_num. Dedup-by-account_num leaves units stacked on
  // top of each other → green active footprint over red sold footprint
  // produces incoherent visuals. The parcel-render side handles this
  // via condoOutlineSeen + geometryKey() (see renderFeatures line ~7219);
  // mirror the same approach here for comp footprints.
  //
  // Dedup-key calculation extracted into _compDedupKey() so the click
  // handler can find the winning comp when a dedup-loser is clicked
  // from the sidebar list.
  //
  // Winner-resolution tie-break: good-rating > status priority
  // (sold > pending > active per PROPELIO_STATUS_PRIORITY).
  const winners = new Map();
  const noKey = [];
  for (const c of comps) {
    const key = _compDedupKey(c);
    if (!key) {
      noKey.push(c);
      continue;
    }
    const current = winners.get(key);
    if (!current) {
      winners.set(key, c);
      continue;
    }
    // Good-rating tie-break BEFORE status priority.
    const currentGood = current?.user_rating === "good";
    const newGood = c?.user_rating === "good";
    if (newGood && !currentGood) {
      winners.set(key, c);
      continue;
    }
    if (currentGood && !newGood) continue;
    // Both good or both non-good → status priority.
    const curPri = PROPELIO_STATUS_PRIORITY[_propelioStatusBucket(current)] || 0;
    const newPri = PROPELIO_STATUS_PRIORITY[_propelioStatusBucket(c)] || 0;
    if (newPri > curPri) winners.set(key, c);
  }
  return [...winners.values(), ...noKey];
}

const PROPELIO_POLYGON_MONTHS = 24;


function _updatePropelioStatusCounts(_unusedFullList) {
  // Two-pass count calc per docs/propelio/STATUS_BADGE_OAC_AWARENESS_SPEC.md:
  //
  // Pass 1 (status badges): answer "if I turn this status ON with my
  //   current settings, how many comps will I see?" — honors the user's
  //   actual OAC (showOutsideArea) toggle state, so out-of-polygon comps
  //   are excluded when OAC is off.
  // Pass 2 (OAC badge): informational "how many comps are currently
  //   out-of-view because OAC is off?" — forces showOutsideArea=true so
  //   the OAC count reflects the full out-of-polygon population.
  //
  // Other filters (price, sqft, year, sold-within, lot, year-built) apply
  // in both passes via propelioFilterState inheritance.
  if (!window._propelioLast || !Array.isArray(window._propelioLast.comps)) {
    const ids = [
      "prop-count-sold", "prop-count-active", "prop-count-pending",
      "prop-count-oac",
    ];
    ids.forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.textContent = "0";
    });
    return;
  }
  // Pass 1: status counts — honor the user's actual showOutsideArea state.
  // Each status badge answers: "if I turn this status ON with my current
  // settings, how many comps will I see right now?" When OAC is off, the
  // out-of-polygon comps don't render, so they shouldn't be counted here.
  // See docs/propelio/STATUS_BADGE_OAC_AWARENESS_SPEC.md.
  const statusBaselineFilters = {
    ...propelioFilterState,
    statusSold: true,
    statusActive: true,
    statusPending: true,
    // showOutsideArea NOT forced — inherits from propelioFilterState
  };
  const statusVisible = window._propelioLast.comps.filter(
    (c) => compPassesPropelioFilters(c, statusBaselineFilters)
  );
  const statusWinners = _dedupCompsForRender(statusVisible);

  let sold = 0, active = 0, pending = 0;
  for (const c of statusWinners) {
    const bucket = _propelioStatusBucket(c);
    if (bucket === "sold") sold++;
    else if (bucket === "pending") pending++;
    else active++;
  }

  // Pass 2: OAC count — informational, always counts out-of-polygon comps
  // regardless of toggle state. The OAC badge answers "how many comps are
  // currently filtered out by OAC being off?"
  const oacBaselineFilters = {
    ...propelioFilterState,
    statusSold: true,
    statusActive: true,
    statusPending: true,
    showOutsideArea: true,
  };
  const oacVisible = window._propelioLast.comps.filter(
    (c) => compPassesPropelioFilters(c, oacBaselineFilters)
  );
  const oacWinners = _dedupCompsForRender(oacVisible);

  let oac = 0;
  for (const c of oacWinners) {
    if (c?.extra?.is_outside_polygon) oac++;
  }

  const setText = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.textContent = String(val);
  };
  setText("prop-count-sold", sold);
  setText("prop-count-active", active);
  setText("prop-count-pending", pending);
  setText("prop-count-oac", oac);
}

_updatePropelioStatusCounts();

function _propelioCompLatLng(comp) {
  const lat = Number(comp?.extra?.lat);
  const lng = Number(comp?.extra?.lon ?? comp?.extra?.lng);
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
  return [lat, lng];
}

// Returns [lng, lat] of where the comp will actually render on the map.
// Prefers the parcel_geom ring centroid (matches the visible footprint)
// over the MLS lat/lng (which can be street-address-geocoded and diverge
// from the parcel boundary, especially for condos and large complexes).
function _compRenderPointLngLat(comp) {
  const geom = comp?.parcel_geom;
  let ring = null;
  if (geom?.type === "Polygon") {
    ring = Array.isArray(geom.coordinates) ? geom.coordinates[0] : null;
  } else if (geom?.type === "MultiPolygon") {
    ring = Array.isArray(geom.coordinates?.[0]) ? geom.coordinates[0][0] : null;
  }
  if (Array.isArray(ring) && ring.length > 0) {
    let sx = 0, sy = 0, n = 0;
    for (const pt of ring) {
      const x = Number(pt?.[0]);
      const y = Number(pt?.[1]);
      if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
      sx += x; sy += y; n += 1;
    }
    if (n > 0) return [sx / n, sy / n];
  }
  const latlng = _propelioCompLatLng(comp);
  return latlng ? [latlng[1], latlng[0]] : null;
}

function _propelioStatusBucket(comp) {
  const raw = String(comp?.status || "").trim().toLowerCase();
  if (raw === "sold") return "sold";
  if (raw === "pending") return "pending";
  if (raw === "for_sale") return "active";
  return "active";
}

function _pointInPolygonLngLat(lng, lat, polygon) {
  if (!Array.isArray(polygon) || polygon.length < 3) return false;
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const xi = Number(polygon[i]?.[0]);
    const yi = Number(polygon[i]?.[1]);
    const xj = Number(polygon[j]?.[0]);
    const yj = Number(polygon[j]?.[1]);
    if (!Number.isFinite(xi) || !Number.isFinite(yi) || !Number.isFinite(xj) || !Number.isFinite(yj)) continue;

    const intersects = ((yi > lat) !== (yj > lat)) &&
      (lng < ((xj - xi) * (lat - yi)) / ((yj - yi) || 1e-12) + xi);
    if (intersects) inside = !inside;
  }
  return inside;
}

function _propelioFootprintStyle(statusClass) {
  return {
    weight: 2.5,
    className: `propelio-footprint-glow ${statusClass}`,
  };
}

// Map: comp_address_key → leaflet layer (geoJSON or marker). Used so the
// sidebar list can fly-to and open the matching popup on click and toggle a
// hover highlight class on the matching footprint.
const propelioCompLayerByKey = new Map();

// Invisible CircleMarker anchors at each comp's centroid, used to bind
// permanent zoom-gated price tooltips ("price balloons"). Mirrors the
// soldMarkers / redfinMarkers pattern. Cleared and rebuilt on every
// _renderPropelioComps call. Refresh is called on zoom changes too.
let propelioPriceMarkers = [];

// Drop a green checkmark badge at the geometric center of a good-rated comp.
// For polygon footprints we use the bounds center (visually close enough to
// a true centroid for our parcel sizes); for fallback dots we just stack
// the badge on top of the dot's latlng.
function _maybeAddGoodCompMark(comp, footprint, fallbackLatLng) {
  if (comp?.user_rating !== "good") return;
  // Dedup: if the comp's matched parcel ALSO has its own rating, skip
  // the comp checkmark — the parcel mark will render on the same spot
  // via _maybeAddParcelRatingMark and we don't want two stacked ✓s.
  // Per KK 2026-05-24: "I am seeing two check marks on good comps...
  // They should visually only get one bud."
  const matchedCounty = String(comp?.parcel_county || "").trim().toLowerCase();
  const matchedAccount = String(comp?.parcel_account_num || "").trim();
  if (matchedCounty && matchedAccount) {
    const parcelRating = _getCachedParcelRating(matchedCounty, matchedAccount);
    if (parcelRating === "good" || parcelRating === "bad") return;
  }
  let target = null;
  if (footprint && typeof footprint.getBounds === "function") {
    try {
      const b = footprint.getBounds();
      if (b && b.isValid()) target = b.getCenter();
    } catch (_) { /* noop */ }
  }
  if (!target && fallbackLatLng) target = fallbackLatLng;
  if (!target) return;
  const goodIcon = L.divIcon({
    className: "propelio-good-mark-wrap",
    html: `<div class="propelio-good-mark">&#10003;</div>`,
    iconSize: [16, 16],
    iconAnchor: [8, 8],
  });
  const goodMarker = L.marker(target, {
    icon: goodIcon,
    interactive: false,
    keyboard: false,
  });
  goodMarker.addTo(propelioCompLayer);
}

// ─── Parcel (CAD) rating support (spec v2 2026-05-24) ─────────────────
// Workspace-scoped Good/Bad/Clear ratings on CAD parcels, parallel to
// comp ratings. Bundled race-condition fix uses optimistic UI: synchronous
// map mark on click + per-key mutation versioning for safe rollback.

// Per-key mutation sequence — prevents stale rollbacks from rapid clicks.
// Key format: `${kind}:${id}` where kind ∈ {comp, parcel}.
const _ratingMutationSeq = new Map();
function _bumpMutationSeq(kind, id) {
  const key = `${kind}:${id}`;
  const next = (_ratingMutationSeq.get(key) || 0) + 1;
  _ratingMutationSeq.set(key, next);
  return next;
}
function _isLatestMutation(kind, id, capturedSeq) {
  return _ratingMutationSeq.get(`${kind}:${id}`) === capturedSeq;
}

// ─── Per-view rating projection (Chunk E, per-view ratings spec §4) ───────
// ARV ratings live in the canonical `_ratingArv`; NBV/Export live in
// `ratings_by_view` (both stamped by the backend on hydrate — comps via
// load_comps_by_polygon, parcels via _load_parcel_ratings_by_view_for_workspace).
// Every renderer reads `user_rating`, so we keep `user_rating` as the
// PROJECTION of the ACTIVE view — recomputed on each render + view switch.
// Writes update the canonical store for the view captured AT CLICK TIME, never
// `user_rating` directly (avoids the view-switch-mid-flight race, C2/H4).
// Flag-OFF (main) skips all of this: `user_rating` stays exactly as the
// backend sent it (ARV) so the wire + behavior are byte-identical to today.
// The helpers operate on comp objects AND parcel `properties` objects — both
// carry `user_rating` + `ratings_by_view` at the same shape.

function _ratingForView(arvRating, ratingsByView, view) {
  if (view === "arv") {
    return (arvRating === "good" || arvRating === "bad") ? arvRating : null;
  }
  const r = ratingsByView && ratingsByView[view];
  return (r === "good" || r === "bad") ? r : null;
}

function _ensureRatingCanonical(obj) {
  // Lazy-capture the ARV canonical the first time we see this object — i.e.
  // while `user_rating` still equals the backend-sent ARV value, before any
  // projection has overwritten it. Then normalize the per-view map. Idempotent.
  if (!obj) return;
  if (obj._ratingArv === undefined) {
    obj._ratingArv = (obj.user_rating === "good" || obj.user_rating === "bad") ? obj.user_rating : null;
  }
  if (!obj.ratings_by_view || typeof obj.ratings_by_view !== "object") {
    obj.ratings_by_view = {};
  }
}

function _setRatingCanonical(obj, rating, view) {
  // Write `rating` into the object's canonical store for `view`.
  _ensureRatingCanonical(obj);
  const r = (rating === "good" || rating === "bad") ? rating : null;
  if (view === "arv") {
    obj._ratingArv = r;
  } else if (r) {
    obj.ratings_by_view[view] = r;
  } else {
    delete obj.ratings_by_view[view];
  }
}

function _projectRatingActive(obj) {
  // Recompute user_rating as the active view's projection from the canonical.
  _ensureRatingCanonical(obj);
  obj.user_rating = _ratingForView(obj._ratingArv, obj.ratings_by_view, _activeView);
}

function _projectCompRatingsForActiveView() {
  if (!ARV_NBV_EXPORT_ENABLED) return;
  const comps = window._propelioLast && window._propelioLast.comps;
  if (!Array.isArray(comps)) return;
  for (const c of comps) if (c) _projectRatingActive(c);
}

function _projectParcelRatingsForActiveView() {
  if (!ARV_NBV_EXPORT_ENABLED) return;
  if (!Array.isArray(allAnalysisFeatures)) return;
  for (const f of allAnalysisFeatures) {
    if (f && f.properties) _projectRatingActive(f.properties);
  }
}

function _resolveParcelAnchor(county, accountNum) {
  // Find a centroid lat/lng for the parcel via the analysis features cache.
  // Chain: rendered polygon bounds (if we have a registered layer) →
  // featureCentroidLngLat → properties.lat/lng. Per spec §4B.
  if (!Array.isArray(allAnalysisFeatures)) return null;
  const c = String(county || "").toLowerCase();
  const a = String(accountNum || "").trim();
  for (const f of allAnalysisFeatures) {
    const fp = f?.properties || {};
    if (String(fp.source_county || "").toLowerCase() !== c) continue;
    if (String(fp.account_num || "").trim() !== a) continue;
    // Try featureCentroidLngLat (exists at map.js:~8832; returns [lng, lat]
    // for the polygon/multipolygon — handles weird geometries cleanly).
    try {
      const lngLat = featureCentroidLngLat(f);
      if (Array.isArray(lngLat) && Number.isFinite(lngLat[0]) && Number.isFinite(lngLat[1])) {
        return L.latLng(lngLat[1], lngLat[0]);
      }
    } catch (_) { /* fall through */ }
    // Last resort: properties.lat/lng
    if (Number.isFinite(fp.lat) && Number.isFinite(fp.lng)) {
      return L.latLng(fp.lat, fp.lng);
    }
    return null;
  }
  return null;
}

function _getCachedParcelRating(county, accountNum) {
  if (!Array.isArray(allAnalysisFeatures)) return null;
  const c = String(county || "").toLowerCase();
  const a = String(accountNum || "").trim();
  for (const f of allAnalysisFeatures) {
    const fp = f?.properties || {};
    if (String(fp.source_county || "").toLowerCase() === c && String(fp.account_num || "").trim() === a) {
      const r = fp.user_rating;
      return r === "good" || r === "bad" ? r : null;
    }
  }
  return null;
}

function _updateParcelUserRatingInCache(county, accountNum, rating) {
  if (!Array.isArray(allAnalysisFeatures)) return;
  const c = String(county || "").toLowerCase();
  const a = String(accountNum || "").trim();
  for (const f of allAnalysisFeatures) {
    const fp = f?.properties || {};
    if (String(fp.source_county || "").toLowerCase() === c && String(fp.account_num || "").trim() === a) {
      fp.user_rating = rating;
      return;
    }
  }
}

// Per-view variant (Chunk E): write into the feature's canonical store for
// `view`, then re-project user_rating for the active view. Used by the
// flag-ON parcel write path so a rating in NBV/Export doesn't bleed into ARV.
function _updateParcelRatingCanonicalInCache(county, accountNum, rating, view) {
  if (!Array.isArray(allAnalysisFeatures)) return;
  const c = String(county || "").toLowerCase();
  const a = String(accountNum || "").trim();
  for (const f of allAnalysisFeatures) {
    const fp = f?.properties;
    if (!fp) continue;
    if (String(fp.source_county || "").toLowerCase() === c && String(fp.account_num || "").trim() === a) {
      _setRatingCanonical(fp, rating, view);
      _projectRatingActive(fp);
      return;
    }
  }
}

function _setParcelRatingMarkOptimistic(county, accountNum, rating) {
  // Remove any existing mark for this parcel.
  const key = `${String(county || "").toLowerCase()}:${String(accountNum || "").trim()}`;
  const existing = cadRatingLayerByKey.get(key);
  if (existing) {
    cadRatingLayer.removeLayer(existing);
    cadRatingLayerByKey.delete(key);
  }
  if (rating !== "good" && rating !== "bad") return;
  const target = _resolveParcelAnchor(county, accountNum);
  if (!target) return;
  const isGood = rating === "good";
  const icon = L.divIcon({
    className: isGood ? "cad-good-mark-wrap" : "cad-bad-mark-wrap",
    html: isGood
      ? `<div class="cad-good-mark">&#10003;</div>`
      : `<div class="cad-bad-mark">&#10007;</div>`,
    iconSize: [16, 16],
    iconAnchor: [8, 8],
  });
  const marker = L.marker(target, { icon, interactive: false, keyboard: false });
  marker.addTo(cadRatingLayer);
  cadRatingLayerByKey.set(key, marker);
}

function _maybeAddParcelRatingMark(parcel, footprint, fallbackLatLng) {
  const rating = parcel?.user_rating;
  if (rating !== "good" && rating !== "bad") return;
  const county = String(parcel?.source_county || "").trim().toLowerCase();
  const accountNum = String(parcel?.account_num || "").trim();
  if (!county || !accountNum) return;
  // Use footprint bounds if available, else _resolveParcelAnchor's chain.
  let target = null;
  if (footprint && typeof footprint.getBounds === "function") {
    try {
      const b = footprint.getBounds();
      if (b && b.isValid()) target = b.getCenter();
    } catch (_) { /* noop */ }
  }
  if (!target) {
    target = _resolveParcelAnchor(county, accountNum) || fallbackLatLng;
  }
  if (!target) return;
  const key = `${county}:${accountNum}`;
  // Remove stale mark if one exists (e.g., re-render after rating change).
  const existing = cadRatingLayerByKey.get(key);
  if (existing) {
    cadRatingLayer.removeLayer(existing);
    cadRatingLayerByKey.delete(key);
  }
  const isGood = rating === "good";
  const icon = L.divIcon({
    className: isGood ? "cad-good-mark-wrap" : "cad-bad-mark-wrap",
    html: isGood
      ? `<div class="cad-good-mark">&#10003;</div>`
      : `<div class="cad-bad-mark">&#10007;</div>`,
    iconSize: [16, 16],
    iconAnchor: [8, 8],
  });
  const marker = L.marker(target, { icon, interactive: false, keyboard: false });
  marker.addTo(cadRatingLayer);
  cadRatingLayerByKey.set(key, marker);
}


function _formatMailerDateUS(iso) {
  const m = String(iso || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return String(iso || "");
  return `${m[2]}/${m[3]}/${m[1]}`;
}

function _maybeAddOutreachOverlay(parcel, feature) {
  if (!filterState.contact_status) return;
  const contactRetrieved = Boolean(parcel?.outreach_contact_info_retrieved);
  const mailerDateRaw = parcel?.outreach_mailer_date;
  const hasMailer = Boolean(mailerDateRaw);
  if (!contactRetrieved && !hasMailer) return;

  const county = String(parcel?.source_county || "").trim().toLowerCase();
  const accountNum = String(parcel?.account_num || "").trim();
  if (!county || !accountNum) return;

  const geom = feature?.geometry;
  const gkey = (geom && (geom.type === "Polygon" || geom.type === "MultiPolygon"))
    ? geometryKey(geom)
    : "";
  if (gkey) {
    if (outreachOverlayGeomSeen.has(gkey)) return;
    outreachOverlayGeomSeen.add(gkey);
  }

  let baseTarget = _resolveParcelAnchor(county, accountNum);
  if (!baseTarget && Number.isFinite(parcel?.lat) && Number.isFinite(parcel?.lng)) {
    baseTarget = L.latLng(parcel.lat, parcel.lng);
  }
  if (!baseTarget) return;

  // Anchor at the parcel centroid in lat/lng (no container-pixel offset).
  // A pixel offset is fixed on screen but the parcel scales with zoom, so
  // any offset that looks fine at high zoom drifts off the parcel as the
  // map zooms out. Matches the CAD rating mark behavior, which stays
  // glued to the centroid at all zoom levels.
  const offsetLatLng = baseTarget;
  const key = `${county}:${accountNum}`;
  const existing = outreachOverlayLayerByKey.get(key);
  if (existing) {
    outreachOverlayLayer.removeLayer(existing);
    outreachOverlayLayerByKey.delete(key);
  }

  const dateDisplay = hasMailer ? _formatMailerDateUS(String(mailerDateRaw)) : "";
  const phoneSvg = `<svg viewBox="0 0 24 24" width="11" height="11" aria-hidden="true">
    <path fill="currentColor" d="M6.6 10.8c1.4 2.8 3.7 5.1 6.5 6.5l2.2-2.2c.3-.3.7-.4 1-.2 1.1.4 2.3.6 3.6.6.6 0 1 .4 1 1V20c0 .6-.4 1-1 1-9.4 0-17-7.6-17-17 0-.6.4-1 1-1h3.5c.6 0 1 .4 1 1 0 1.3.2 2.5.6 3.6.1.4 0 .8-.2 1.1L6.6 10.8z"/>
  </svg>`;
  const html = `<div class="outreach-overlay-stack">
    <div class="outreach-overlay-icon">${phoneSvg}</div>
    ${dateDisplay ? `<div class="outreach-overlay-date">${dateDisplay}</div>` : ""}
  </div>`;
  const iconHeight = dateDisplay ? 34 : 18;
  const icon = L.divIcon({
    className: "outreach-overlay-wrap",
    html,
    iconSize: [60, iconHeight],
    iconAnchor: [30, iconHeight / 2],
  });
  const marker = L.marker(offsetLatLng, {
    icon,
    interactive: false,
    keyboard: false,
    zIndexOffset: -50,
  });
  marker.addTo(outreachOverlayLayer);
  outreachOverlayLayerByKey.set(key, marker);
}

function _rebuildOutreachOverlays() {
  outreachOverlayLayer.clearLayers();
  outreachOverlayLayerByKey.clear();
  outreachOverlayGeomSeen.clear();
  if (!filterState.contact_status) return;
  if (!lastAnalysisGeojson || !Array.isArray(lastAnalysisGeojson.features)) return;
  for (const feature of lastAnalysisGeojson.features) {
    const p = feature?.properties;
    if (!p) continue;
    if (!isFeatureVisible(feature)) continue;
    _maybeAddOutreachOverlay(p, feature);
  }
}
// (_buildParcelRatingButtonsHtml removed 2026-05-24 — replaced by
// _buildRatingButtonsHtml accepting an optional `parcel` arg, which
// emits a SINGLE button row with both data-comp-key + data-county +
// data-account-num and writes to both rating tables on click.)

async function rateParcel(county, accountNum, rating, view) {
  const areaId = (typeof _currentLoadedAreaId === "string" ? _currentLoadedAreaId : "") || "";
  if (!areaId || !county || !accountNum) return false;
  // View captured at click time (per-view ratings §4). Invalid/absent → the
  // current active view. Flag-OFF: _activeView is always "arv".
  const _view = (view === "arv" || view === "nbv" || view === "export") ? view : _activeView;
  const body = {
    saved_area_id: areaId,
    county,
    account_num: accountNum,
    rating: rating === "good" || rating === "bad" ? rating : null,
  };
  // Only send `view` when the feature is enabled — keeps the flag-OFF wire
  // byte-identical to today (backend treats absent view as 'arv', C2).
  if (ARV_NBV_EXPORT_ENABLED) body.view = _view;
  try {
    const resp = await fetch("/api/parcels/rate", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      console.warn("[cad] rate parcel failed:", resp.status);
      return false;
    }
    if (ARV_NBV_EXPORT_ENABLED) {
      _updateParcelRatingCanonicalInCache(county, accountNum, body.rating, _view);
    } else {
      _updateParcelUserRatingInCache(county, accountNum, body.rating);
    }
    return true;
  } catch (err) {
    console.error("[cad] rate parcel error:", err);
    return false;
  }
}

// Comp optimistic mark — companion to _setParcelRatingMarkOptimistic.
// Bundled race-fix per PARCEL_RATINGS_SPEC.md v2 §5: addresses the "first
// Good Comp checkmark slow" lag KK observed. Synchronous; no server wait.
function _setCompRatingMarkOptimistic(compKey, rating) {
  // Comp goods render with a checkmark; comp bads have no mark (they're
  // dimmed via the bad-comp visual treatment elsewhere — we mirror that
  // by simply not rendering a mark for "bad" or "null").
  const layer = propelioCompLayer;
  // propelioCompLayerByKey holds the footprint OR fallback marker per
  // compKey. We compute the anchor from it.
  const existingLayer = propelioCompLayerByKey.get(String(compKey || "").trim());
  let anchor = null;
  if (existingLayer) {
    if (typeof existingLayer.getBounds === "function") {
      try {
        const b = existingLayer.getBounds();
        if (b && b.isValid()) anchor = b.getCenter();
      } catch (_) { /* noop */ }
    }
    if (!anchor && typeof existingLayer.getLatLng === "function") {
      anchor = existingLayer.getLatLng();
    }
  }
  if (rating !== "good") return;  // no synchronous mark for bad/null on comps
  if (!anchor) return;
  // Tagged key for the optimistic checkmark so re-renders + clear paths
  // can find it. Reuses propelioCompLayer.
  const goodIcon = L.divIcon({
    className: "propelio-good-mark-wrap",
    html: `<div class="propelio-good-mark">&#10003;</div>`,
    iconSize: [16, 16],
    iconAnchor: [8, 8],
  });
  const marker = L.marker(anchor, { icon: goodIcon, interactive: false, keyboard: false });
  marker.addTo(layer);
  // No keymap registration — the subsequent applyPropelioClientFilters
  // re-render will rebuild the canonical good marks from cache and clear
  // this optimistic one along with the rest of the layer. The user sees
  // immediate feedback; canonical render takes over within ~150-500ms.
}

// (Old .cad-rate-btn handler removed 2026-05-24 — replaced by unified
// .propelio-rate-btn handler below, which now reads both data-comp-key
// AND data-county/data-account-num and writes to whichever rating tables
// apply on a single click.)

function _renderPropelioComps(data) {
  // NOTE 2026-05-24: do NOT clear cadRatingLayer here. _renderPropelioComps
  // is a comp-only re-render path (fires on every rating click + filter
  // change). Clearing the parcel rating layer here wipes the on-screen
  // marks the user just set on OTHER parcels — KK's "marked a comp bad
  // and it wiped out all the other stuff" bug. Parcel layer lifecycle is
  // owned by renderFeatures (line ~8606) and the clear-all-map-layers
  // path near line 9776, NOT by comp re-renders.
  propelioCompLayer.clearLayers();
  propelioCompLayerByKey.clear();
  propelioPriceMarkers = [];
  if (!data || !Array.isArray(data.comps)) return { total: 0, footprintCount: 0, fallbackCount: 0 };

  let footprintCount = 0;
  let fallbackCount = 0;
  let insideCount = 0;
  const hasPolygonContext = Boolean(data?.polygon_meta) && Array.isArray(lastPolygon) && lastPolygon.length >= 3;

  data.comps.forEach((comp) => {
    const statusClass = _propelioStatusBucket(comp);
    const isBad = comp?.user_rating === "bad";
    const compClass = isBad ? `${statusClass} bad-comp` : statusClass;
    const compKey = String(comp?.comp_address_key || "").trim();
    const latlng = _propelioCompLatLng(comp);
    if (hasPolygonContext && latlng && _pointInPolygonLngLat(latlng[1], latlng[0], lastPolygon)) {
      insideCount += 1;
    }

    const geom = comp?.parcel_geom;
    const hasGeom = geom && (geom.type === "Polygon" || geom.type === "MultiPolygon");

    if (hasGeom) {
      const footprint = L.geoJSON(geom, {
        style: () => _propelioFootprintStyle(compClass),
        onEachFeature: (_feature, layer) => {
          // No bindPopup — we resolve unified-vs-standalone popup content
          // asynchronously on click so spillover comps (outside the drawn
          // polygon) still get the unified CAD+MLS popup via on-demand
          // parcel fetch.
          layer.on("click", async (ev) => {
            L.DomEvent.stopPropagation(ev);
            await _openUnifiedPropelioPopup(comp, ev.latlng);
          });
        },
      });
      footprint.addTo(propelioCompLayer);
      footprint._lotLedgerComp = comp;
      if (compKey) propelioCompLayerByKey.set(compKey, footprint);
      footprintCount += 1;
      _maybeAddGoodCompMark(comp, footprint, latlng);
      return;
    }

    if (!latlng) return;

    const fallbackIcon = L.divIcon({
      className: "propelio-fallback-dot-wrap",
      html: `<div class="propelio-fallback-dot ${compClass}"></div>`,
      iconSize: [14, 14],
      iconAnchor: [7, 7],
    });

    const marker = L.marker(latlng, {
      icon: fallbackIcon,
      riseOnHover: true,
    });
    marker._lotLedgerComp = comp;
    marker.on("click", async (ev) => {
      L.DomEvent.stopPropagation(ev);
      await _openUnifiedPropelioPopup(comp, ev.latlng);
    });
    marker.addTo(propelioCompLayer);
    if (compKey) propelioCompLayerByKey.set(compKey, marker);
    fallbackCount += 1;
    _maybeAddGoodCompMark(comp, null, latlng);
  });

  // After visible footprints/dots are placed, lay down invisible
  // CircleMarker anchors at each comp's centroid so we can bind
  // permanent price tooltips on top. Bad-rated comps are excluded from
  // the balloon set entirely (they're already dim — no need to add
  // a price chip for a comp the analyst already rejected).
  data.comps.forEach((comp) => {
    if (comp?.user_rating === "bad") return;
    const priceLabel = abbreviatePrice(Number(comp?.price));
    if (!priceLabel) return;
    let anchorLatLng = null;
    const compKey = String(comp?.comp_address_key || "").trim();
    const layer = compKey ? propelioCompLayerByKey.get(compKey) : null;
    if (layer && typeof layer.getBounds === "function") {
      try {
        const b = layer.getBounds();
        if (b && b.isValid()) anchorLatLng = b.getCenter();
      } catch (_) { /* noop */ }
    }
    if (!anchorLatLng) anchorLatLng = _propelioCompLatLng(comp);
    if (!anchorLatLng) return;
    if (Array.isArray(anchorLatLng)) anchorLatLng = L.latLng(anchorLatLng[0], anchorLatLng[1]);
    const anchor = L.circleMarker(anchorLatLng, {
      radius: 1,
      opacity: 0,
      fillOpacity: 0,
      interactive: false,
    });
    anchor.addTo(propelioCompLayer);
    const bucket = _propelioStatusBucket(comp);
    const soldDateLabel = bucket === "sold" ? formatSoldDateLabel(comp?.extra?.close_date) : "";
    propelioPriceMarkers.push({
      marker: anchor,
      priceLabel,
      bucket,
      soldDateLabel,
    });
  });
  refreshPropelioPriceLabels();

  // Cross-layer mute: when a parcel polygon has a matching Propelio comp
  // footprint, set the parcel's fill opacity to 0 so the comp color is the
  // sole visible signal. Without this, the parcel layer (multifamily=gray,
  // commercial=brown, on_redfin=red) blends with the translucent comp
  // footprint and produces a muddy/brown stack. The existing code at the
  // parcel render path zeros fill for CAD-side sold_comp matches only —
  // this extends it to live Propelio matches.
  //
  // Restores fill on parcels that LOST their Propelio comp in the latest
  // filter pass, so toggling filters cleanly repaints. Each parcel layer
  // caches its original fill opacity on first encounter so restoration
  // works after multiple re-renders.
  const accountsWithComps = new Set();
  for (const comp of (data?.comps || [])) {
    const acct = String(comp?.parcel_account_num || "").trim();
    if (acct) accountsWithComps.add(acct);
  }
  _renderedParcelPopupLayers.forEach((layer, acct) => {
    if (!layer || typeof layer.setStyle !== "function") return;
    if (typeof layer._lotLedgerOriginalFillOpacity !== "number") {
      let captured = null;
      layer.eachLayer((child) => {
        if (captured === null && typeof child?.options?.fillOpacity === "number") {
          captured = child.options.fillOpacity;
        }
      });
      layer._lotLedgerOriginalFillOpacity = (typeof captured === "number") ? captured : 0.12;
    }
    const targetOpacity = accountsWithComps.has(acct) ? 0 : layer._lotLedgerOriginalFillOpacity;
    layer.setStyle({ fillOpacity: targetOpacity });
  });

  if (data?.polygon_meta) {
    const total = data.comps.length;
    data.polygon_meta.comps_in_polygon = insideCount;
    data.polygon_meta.comps_outside_polygon = Math.max(total - insideCount, 0);
  }

  return {
    total: data.comps.length,
    footprintCount,
    fallbackCount,
  };
}

// === Propelio sidebar filter card (Phase 3 Chunk C) ========================
// Two tiers: API-side filters (months, range) require an explicit Refresh
// button (1 credit). Client-side filters (status checkboxes, sold-within,
// lot/sqft/year/price min-max) apply instantly to the existing comp pool
// without re-hitting Propelio.

const normalizeNbhd = (s) => String(s || "").trim().replace(/\s+/g, " ").toLowerCase();

const DEFAULT_PROPELIO_FILTERS = {
  months: 24,
  range: 1.0,
  statusSold: true,
  statusActive: true,
  statusPending: true,
  showOutsideArea: false,
  soldWithinDays: null,
  lotMin: null, lotMax: null,
  sqftMin: null, sqftMax: null,
  yearMin: null, yearMax: null,
  priceMin: null, priceMax: null,
  neighborhood: null,
};
let propelioFilterState = { ...DEFAULT_PROPELIO_FILTERS };

// ── Auto-match target (comp POC, Rung 0) ────────────────────────────────────
// A reversible checkbox that fills the lot/sqft comp filters with a ± band
// around the selected subject. EPHEMERAL: in-memory, per-tab, never persisted.
// The band VALUES persist via the normal autosave (WYSIWYG-honest); the MODE
// does not. Spec: docs/COMP_ENGINE_POC_PLAN_2026-07-11.md.
const AUTO_MATCH_BAND = 0.2; // ±20% — v1 default; the band is an upsell hook.
// The CLIENT'S OWN LINE, from his team's saved filters and confirmed on the
// 2026-07-13 call: ARV comps are OLD houses (what you'd flip, year <= 2008);
// NBV comps are NEW builds (what you'd construct, year >= 2008).
// ⚠️ OPEN with the client: is 2008 the line everywhere, or does it move by
// neighbourhood? If it moves, this becomes a knob, not a constant.
const AUTO_MATCH_YEAR_PIVOT = 2008;

// Which year field the band math writes for a given view, per the client's
// own line (§2.1): ARV = old houses (year <= pivot) -> prop-year-max, NBV =
// new builds (year >= pivot) -> prop-year-min. Export is a review/export
// view, not a valuation view — it gets no year constant.
function _autoMatchYearFieldForView() {
  if (_activeView === "arv") return "prop-year-max";
  if (_activeView === "nbv") return "prop-year-min";
  return null;
}

// Parse a subject-card display value ("2,450", "0.29 ac", "N/A") to a number.
// Mirrors the _compMatchedTotVal precedent (map.js:8291): strip $ and commas,
// take the leading numeric run. Returns null when there is no parseable number.
function _parseSubjectNum(v) {
  const m = String(v == null ? "" : v).replace(/[$,]/g, "").match(/^[\d.]+/);
  return m ? Number(m[0]) : null;
}

// Resolve the subject's lot (acres) + living area (sqft) from the stashed props.
// lot_acres is ALREADY acres (no 43560 conversion). Returns null if neither
// dimension is available.
function _autoMatchSubjectDims() {
  const p = _lastSubjectProps;
  if (!p) return null;
  const acres = _parseSubjectNum(p.lot_acres);
  const sqft = _parseSubjectNum(p.sqft);
  if (acres == null && sqft == null) return null;
  return { acres, sqft };
}

// Build a ± band around a center value. `round` shapes each end (int for sqft,
// 2dp for acres). Returns null for missing/non-positive centers.
function _autoMatchBand(center, frac, round) {
  if (center == null || !Number.isFinite(center) || center <= 0) return null;
  return { min: round(center * (1 - frac)), max: round(center * (1 + frac)) };
}

// Write the lot/sqft filter inputs from the subject dims. Programmatic .value
// writes do NOT fire input events, so the existing debounced listeners won't
// double-apply. Does NOT call applyPropelioClientFilters (callers do).
function _writeAutoMatchBands(dims) {
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v == null ? "" : String(v); };
  const roundAcre = (x) => Math.round(x * 100) / 100;
  const acreBand = _autoMatchBand(dims.acres, AUTO_MATCH_BAND, roundAcre);
  const sqftBand = _autoMatchBand(dims.sqft, AUTO_MATCH_BAND, Math.round);
  if (acreBand) { set("prop-lot-min", acreBand.min); set("prop-lot-max", acreBand.max); }
  if (sqftBand) { set("prop-sqft-min", sqftBand.min); set("prop-sqft-max", sqftBand.max); }
  const yearField = _autoMatchYearFieldForView();
  if (yearField) set(yearField, AUTO_MATCH_YEAR_PIVOT);
}

// ── AI mode (2026-07-14 AI bar spec; persistence fix 2026-07-14) ─────────
// An A/B lens, not a filter. A VA sets up an area's filters; the owner
// presses AI mode to see what our automation would have picked instead, and
// presses it again to be back to exactly what the VA had. AI mode DISPLAYS
// -- it never overwrites the user's work.
//
// AI MODE FIX (docs/AI/CODER_SPEC_AIMODE_FIX_2026-07-14.md) — AI's own
// filter-view persistence namespace is GONE. It was the wrong layer: a
// write-only buffer that nothing ever read back, while the mode's actual
// footprint is six propelio fields. The fix moves AI's state out of the filter-blob
// world entirely -- _aiOverlay (below) is a small computed object, never a
// captured blob -- and lets captureFilterState() (the one sacred function)
// hide it from every caller, unconditionally. The persistence layer no
// longer needs to KNOW about AI mode; it's structurally incapable of
// seeing it. EPHEMERAL throughout: in-memory, per-tab, never persisted
// (dies on reload).
let _aiModeOn = false;
// AI's own six picked values per view -- NOT a filter blob, NOT captured
// from the DOM. Built ONCE at enable, purely by computation (subject dims +
// AUTO_MATCH_YEAR_PIVOT -- see _buildAiOverlayFields). Display paints this
// on top of the user's real, restored state; it is never itself captured,
// diffed, or persisted. No `export` entry: AI does not touch Final.
let _aiOverlay = { arv: null, nbv: null };
// The user's REAL filters for arv/nbv, captured once when AI mode turns ON.
// This is the "untouched cache/store" Hole B's restore step reads from --
// deliberately NOT _viewFilterCache, which can itself be stale relative to
// the live DOM (a hand-edit with no view-switch in between never lands
// there). Hole C also writes into this -- never the live DOM -- when a
// co-viewer's SSE edit targets the view AI mode is currently substituting,
// so the freshest real value is what comes back on drop-out/off, AND what
// captureFilterState() substitutes from while AI mode is on.
let _aiModeUserSnapshot = { arv: null, nbv: null };

// The user's real ARV base to snapshot at AI-mode-enable time: the live DOM
// if ARV is currently active (the one moment it's guaranteed current even
// past a hand-edit with no view-switch), else the last real capture.
function _aiRealArvBase() {
  if (_activeView === "arv") return captureFilterState();
  const real = _viewFilterCache.arv;
  return (real && real.v)
    ? JSON.parse(JSON.stringify(real))
    : { v: 1, checkboxes: { ...DEFAULT_FILTERS }, numeric: {}, sold: {}, comp: {}, propelio: { ...DEFAULT_PROPELIO_FILTERS } };
}

// The user's real NBV base to snapshot at AI-mode-enable time. Mirrors
// _setActiveView's OWN seed-fallback order (incl. the NBV-must-not-inherit-
// ARV's-year-gate fix, d2c5788) but read-only against the real cache --
// never writes _viewFilterCache. Deliberately NOT calling _setActiveView
// itself (§2.4 Do NOT: faking a view switch fires the capture/restore/seed
// machinery and repaints twice -- exactly how that bug was born).
function _aiRealNbvBase() {
  if (_activeView === "nbv") return captureFilterState();
  const real = _viewFilterCache.nbv;
  if (real && real.v) return JSON.parse(JSON.stringify(real));
  const arv = _viewFilterCache.arv;
  const base = (arv && arv.v)
    ? JSON.parse(JSON.stringify(arv))
    : { v: 1, checkboxes: { ...DEFAULT_FILTERS }, numeric: {}, sold: {}, comp: {}, propelio: { ...DEFAULT_PROPELIO_FILTERS } };
  if (base.propelio) { delete base.propelio.yearMin; delete base.propelio.yearMax; }
  return base;
}

// Compute AI's six picked fields for one view -- pure function of the
// subject's dims + AUTO_MATCH_YEAR_PIVOT, exactly the band math
// _writeAutoMatchBands uses. NEVER reads propelioFilterState, the DOM, or
// any cache -- this is the "built by computation, never by DOM capture"
// guarantee the fix depends on. Returns exactly the six keys
// captureFilterState() knows how to substitute; nothing else.
// The SIX propelio fields AI mode paints onto the DOM -- and the ONLY fields any
// AI-mode-aware code path may treat specially. Single source of truth: keep
// _buildAiOverlayFields' keys and this set in lockstep, or the fifth hole is back.
const _AI_OVERLAY_FIELDS = new Set(["lotMin", "lotMax", "sqftMin", "sqftMax", "yearMin", "yearMax"]);

function _buildAiOverlayFields(dims, view) {
  const roundAcre = (x) => Math.round(x * 100) / 100;
  const acreBand = dims ? _autoMatchBand(dims.acres, AUTO_MATCH_BAND, roundAcre) : null;
  const sqftBand = dims ? _autoMatchBand(dims.sqft, AUTO_MATCH_BAND, Math.round) : null;
  return {
    lotMin: acreBand ? acreBand.min : null,
    lotMax: acreBand ? acreBand.max : null,
    sqftMin: sqftBand ? sqftBand.min : null,
    sqftMax: sqftBand ? sqftBand.max : null,
    yearMin: view === "nbv" ? AUTO_MATCH_YEAR_PIVOT : null,
    yearMax: view === "arv" ? AUTO_MATCH_YEAR_PIVOT : null,
  };
}

// Paint _aiOverlay[view]'s six fields onto the live DOM -- display only.
// Used both at enable time and on every view-switch restore while AI mode
// is on, so there is exactly one code path that ever writes AI's picks to
// screen. Programmatic .value writes do not fire input events (map.js
// precedent), so sync propelioFilterState explicitly afterward.
function _applyAiOverlayToDom(view) {
  const overlay = _aiOverlay[view];
  if (!overlay) return;
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v == null ? "" : String(v); };
  set("prop-lot-min", overlay.lotMin);
  set("prop-lot-max", overlay.lotMax);
  set("prop-sqft-min", overlay.sqftMin);
  set("prop-sqft-max", overlay.sqftMax);
  set("prop-year-min", overlay.yearMin);
  set("prop-year-max", overlay.yearMax);
  propelioFilterState = readPropelioFiltersFromUI();
}

// Turning AI mode ON (§2.1). Snapshots both views' real state FIRST (so Hole
// B / OFF can always restore exactly), computes both views' overlay fields
// by pure math, then paints the currently-active view's overlay to the
// DOM. No baseline manipulation: captureFilterState() already hides AI's
// six fields from any diff, so there is nothing to protect against here --
// turning AI mode on cannot itself produce a PATCH.
function _enableAiMode() {
  if (_aiModeOn) return;
  if (!_currentLoadedAreaId) return;   // AI mode is pressed by a human, on a loaded area
  _aiModeUserSnapshot = { arv: _aiRealArvBase(), nbv: _aiRealNbvBase() };
  const dims = _autoMatchSubjectDims();
  _aiOverlay = {
    arv: _buildAiOverlayFields(dims, "arv"),
    nbv: _buildAiOverlayFields(dims, "nbv"),
  };
  _aiModeOn = true;
  if (_activeView === "arv" || _activeView === "nbv") {
    _applyAiOverlayToDom(_activeView);
  }
  _renderAiBar();
  applyPropelioClientFilters();
}

// Turning AI mode OFF (§2.2). Re-read from the user's real, untouched
// snapshot -- never a repair, since nothing real was ever modified (Hole A).
// No rebaseline: baseline and capture live in the same space as always
// (captureFilterState() has been returning user truth this whole time), so
// there is nothing to re-synchronize.
function _disableAiMode() {
  if (!_aiModeOn) return;
  const real = (_activeView === "arv" || _activeView === "nbv") ? _aiModeUserSnapshot[_activeView] : null;
  _aiModeOn = false;
  _aiOverlay = { arv: null, nbv: null };
  _aiModeUserSnapshot = { arv: null, nbv: null };
  if (real && real.propelio) applyPropelioFilterStateToUI(real.propelio);
  _renderAiBar();
  applyPropelioClientFilters();
}

// Full reset with no restore/apply -- for paths where the caller is already
// replacing the filter state wholesale (area load, session load, Reset).
// Discards the mode + both caches so a later _setActiveView / restore never
// routes through stale AI state left over from a DIFFERENT area's subject.
function _resetAiMode() {
  _aiModeOn = false;
  _aiOverlay = { arv: null, nbv: null };
  _aiModeUserSnapshot = { arv: null, nbv: null };
}

// Manual edit of a field AI mode wrote (§2.3, §2.5 Hole B). The user has
// taken the wheel: their edit stands, but the OTHER AI-written fields must
// not survive into the user's real keys. The naive "drop the mode, keep the
// edit" leaves the other boxes holding AI's values with the key-building
// prefix now real/flat -- the very next autosave diffs them against the
// pre-AI snapshot and PATCHes every AI-differing field into the user's real
// filters. Exact 4-step sequence, in this order, or it reproduces the bug
// it exists to fix.
function _dropAiModeForEdit(editedFieldId) {
  if (!_aiModeOn) return;
  if (_activeView !== "arv" && _activeView !== "nbv") return;   // AI never wrote Final's fields
  const editedEl = document.getElementById(editedFieldId);
  const editedValue = editedEl ? editedEl.value : "";
  const real = _aiModeUserSnapshot[_activeView];
  // 1. Restore the user's real filters to the UI (their untouched snapshot).
  if (real && real.propelio) applyPropelioFilterStateToUI(real.propelio);
  // 2. Re-apply the single edited field on top -- the user's edit wins.
  if (editedEl) editedEl.value = editedValue;
  propelioFilterState = readPropelioFiltersFromUI();
  // 3. Rebaseline to the user's REAL, PRE-EDIT state -- NOT captureFilterState().
  // captureFilterState() here would snapshot the UI *including* the fresh edit,
  // so step 4's diff would find current == snapshot, emit ZERO fields, and the
  // user's edit would never be PATCHed at all -- present on screen, absent from
  // the DB, gone on reload. The snapshot must equal what the DB actually holds
  // for the user's real keys (their pre-AI filters), so the edit shows up as
  // exactly one diff and lands in their real namespace. Everything AI wrote was
  // already restored away in step 1, so it cannot diff.
  _filterSaveLastSnapshot = real ? JSON.parse(JSON.stringify(real)) : captureFilterState();
  // 4. THEN drop the mode and apply -- key-building is real/flat again, so
  // this PATCHes exactly the user's edit, nothing else.
  _aiModeOn = false;
  _aiOverlay = { arv: null, nbv: null };
  _aiModeUserSnapshot = { arv: null, nbv: null };
  _renderAiBar();
  applyPropelioClientFilters();
}

// Option-list cache: rebuilt once per comp-load (reference-equality guard),
// never on keystrokes. Null until the first comp set arrives.
let _nbhdOptionsCache = null;
let _nbhdOptionsCacheRef = null;
let _nbhdOptionsCacheSig = null;

function _propNumIn(id) {
  const el = document.getElementById(id);
  if (!el) return null;
  const v = String(el.value || "").trim();
  if (!v) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function _propIntIn(id) {
  const v = _propNumIn(id);
  return v != null && Number.isFinite(v) ? Math.round(v) : null;
}

function _propPriceIn(id) {
  const el = document.getElementById(id);
  if (!el) return null;
  const v = parseShorthand(el.value);
  return Number.isFinite(v) ? v : null;
}

function readPropelioFiltersFromUI() {
  const sold = document.getElementById("prop-status-sold");
  const active = document.getElementById("prop-status-active");
  const pending = document.getElementById("prop-status-pending");
  const outside = document.getElementById("prop-outside-area");
  return {
    months: _propNumIn("prop-months") ?? DEFAULT_PROPELIO_FILTERS.months,
    range: _propNumIn("prop-range") ?? DEFAULT_PROPELIO_FILTERS.range,
    statusSold: sold ? sold.checked : true,
    statusActive: active ? active.checked : true,
    statusPending: pending ? pending.checked : true,
    showOutsideArea: outside ? outside.checked : false,
    soldWithinDays: _propIntIn("prop-sold-within"),
    lotMin: _propNumIn("prop-lot-min"),
    lotMax: _propNumIn("prop-lot-max"),
    sqftMin: _propIntIn("prop-sqft-min"),
    sqftMax: _propIntIn("prop-sqft-max"),
    yearMin: _propIntIn("prop-year-min"),
    yearMax: _propIntIn("prop-year-max"),
    priceMin: _propPriceIn("prop-price-min"),
    priceMax: _propPriceIn("prop-price-max"),
    neighborhood: (document.getElementById("prop-neighborhood")?.value || "").trim() || null,
    // Property Type Filter toggles — read from the parcel-side filterState
    // so the same toggles gate both parcels and comps.
    parcelTypeMultifamily: filterState.multifamily,
    parcelTypeDuplexes:    filterState.duplexes,
    parcelTypeCommercial:  filterState.commercial,
    parcelTypeVacant:      filterState.vacant,
    parcelTypeExempt:      filterState.exempt,
    parcelTypeOffMarket:   filterState.off_market,
  };
}

// Resolve a comp's parcel-type bucket. Matched-parcel lookup wins (we trust
// our own classification over Propelio's MLS taxonomy). Falls back to
// Propelio's property_category then property_type. Unknown → null → comp
// defaults to visible (no parcel-type gate fires).
function _compPropertyTypeBucket(comp) {
  const acct = String(comp?.parcel_account_num || "").trim();
  const county = String(comp?.parcel_county || "").trim().toLowerCase();
  if (acct && county && lastAnalysisGeojson && Array.isArray(lastAnalysisGeojson.features)) {
    for (const f of lastAnalysisGeojson.features) {
      const p = f?.properties || {};
      if (String(p.account_num || "").trim() === acct
        && String(p.source_county || "").trim().toLowerCase() === county) {
        const baseType = p.prop_type || null;
        // Single-family parcels split into "off_market" (default) or "active"
        // (Redfin listing live) — same derivation the parcel layer uses, so
        // Property Type Filter "Off Market" toggle gates SFR comps too.
        if (baseType === "single_family") {
          return p.on_redfin ? "active" : "off_market";
        }
        return baseType;
      }
    }
  }
  // Propelio nests the MLS classification under `extra`; the top-level
  // property_category / property_type fields are not populated in the
  // current API shape (verified empirically against propelio_cache, 2026-05-19).
  // Defensive: fall back to top-level in case the shape ever changes.
  const extra = (comp && typeof comp.extra === "object" && comp.extra) || {};
  const cat = String(extra.property_category || comp?.property_category || "").trim();
  if (cat && PROPELIO_CATEGORY_TO_BUCKET[cat]) return PROPELIO_CATEGORY_TO_BUCKET[cat];
  const t = String(extra.property_type || comp?.property_type || "").trim();
  if (t && PROPELIO_TYPE_FALLBACK[t]) return PROPELIO_TYPE_FALLBACK[t];
  return null;
}

// Resolve a Propelio comp's tax appraised value via its matched CAD
// parcel. Propelio data itself carries sold/list price, never tax
// assessment — but propelio_comps rows store (parcel_account_num,
// parcel_county) pointing back to whichever CAD parcel matched at
// scrape time, and the CAD parcel feature in lastAnalysisGeojson
// carries `tot_val`. Returns a number, or null when the comp has no
// CAD match (orphan / cross-county spillover) or the parcel isn't in
// the current analysis geojson.
function _compMatchedTotVal(comp) {
  const acct = String(comp?.parcel_account_num || "").trim();
  if (!acct) return null;
  const features = lastAnalysisGeojson?.features;
  if (!Array.isArray(features) || features.length === 0) return null;
  const cnty = String(comp?.parcel_county || "").trim().toLowerCase();
  const match = features.find((f) => {
    const p = f?.properties || {};
    if (String(p.account_num || "").trim() !== acct) return false;
    // Match on county when the comp specifies one — protects against
    // cross-county account_num collisions (rare but possible).
    if (cnty && String(p.source_county || "").trim().toLowerCase() !== cnty) return false;
    return true;
  });
  if (!match) return null;
  const raw = String(match.properties?.tot_val || "").replace(/[$,]/g, "").match(/^[\d.]+/);
  return raw ? Number(raw[0]) : null;
}

function compPassesPropelioFilters(comp, filters, _nbhdNorm) {
  const status = String(comp?.status || "").toLowerCase();
  // Status checkbox filters
  if (status === "sold" && !filters.statusSold) return false;
  if ((status === "for_sale" || status === "active") && !filters.statusActive) return false;
  if (status === "pending" && !filters.statusPending) return false;
  // If status is unknown/other, fall through (don't filter out)

  // Outside-area gate. Default off → only comps inside the drawn polygon
  // pass. Toggle on → also let through Propelio's spillover comps from
  // its circumradius search. Falls open when there is no polygon yet
  // (e.g. by-address pulls), so that path is unaffected.
  //
  // Test point priority: parcel_geom centroid (where the comp actually
  // RENDERS) > _propelioCompLatLng. Condos in particular often have an
  // MLS-geocoded lat/lng pointing to a building entrance inside the
  // polygon while parcel_geom from CAD points to a specific unit
  // boundary outside — checking lat/lng would let the comp through and
  // it would render visibly far outside the drawn area.
  if (!filters.showOutsideArea && Array.isArray(lastPolygon) && lastPolygon.length >= 3) {
    const testPoint = _compRenderPointLngLat(comp);  // [lng, lat] or null
    if (!testPoint || !_pointInPolygonLngLat(testPoint[0], testPoint[1], lastPolygon)) {
      return false;
    }
  }

  // Sold-within (days) — only applies to sold comps
  if (status === "sold" && filters.soldWithinDays != null) {
    const cd = comp?.extra?.close_date;
    if (!cd) return false;
    const dt = new Date(cd);
    if (!Number.isFinite(dt.getTime())) return false;
    const days = (Date.now() - dt.getTime()) / 86400000;
    if (days > filters.soldWithinDays) return false;
  }

  // Lot acres (lot_size is in sqft from Propelio)
  const lotSqft = Number(comp?.lot_size);
  const acres = Number.isFinite(lotSqft) && lotSqft > 0 ? lotSqft / 43560 : null;
  if (filters.lotMin != null && (acres == null || acres < filters.lotMin)) return false;
  if (filters.lotMax != null && (acres == null || acres > filters.lotMax)) return false;

  // Living sqft
  const sqft = Number(comp?.sqft);
  if (filters.sqftMin != null && (!Number.isFinite(sqft) || sqft < filters.sqftMin)) return false;
  if (filters.sqftMax != null && (!Number.isFinite(sqft) || sqft > filters.sqftMax)) return false;

  // Year built
  const yr = Number(comp?.year_built);
  if (filters.yearMin != null && (!Number.isFinite(yr) || yr < filters.yearMin)) return false;
  if (filters.yearMax != null && (!Number.isFinite(yr) || yr > filters.yearMax)) return false;

  // Price
  const price = Number(comp?.price);
  if (filters.priceMin != null && (!Number.isFinite(price) || price < filters.priceMin)) return false;
  if (filters.priceMax != null && (!Number.isFinite(price) || price > filters.priceMax)) return false;

  // Property Filters (the global #numeric-filters / "Property Filters"
  // section) also gate comps as of 2026-05-20. Lot/sqft/year translate
  // directly from comp data; appraised value comes from the comp's
  // MATCHED CAD parcel (Propelio comps don't carry tot_val themselves —
  // we follow the parcel_account_num + parcel_county link into
  // lastAnalysisGeojson). When both Property Filters and Comp Filters
  // constrain the same dimension, the stricter wins (AND). `lotSqft`,
  // `sqft`, `yr` are reused from the comp-filter blocks above.
  if (numericFilters.lot_sqft_min != null && (!Number.isFinite(lotSqft) || lotSqft < numericFilters.lot_sqft_min)) return false;
  if (numericFilters.lot_sqft_max != null && (!Number.isFinite(lotSqft) || lotSqft > numericFilters.lot_sqft_max)) return false;
  if (numericFilters.yr_built_min != null && (!Number.isFinite(yr) || yr < numericFilters.yr_built_min)) return false;
  if (numericFilters.yr_built_max != null && (!Number.isFinite(yr) || yr > numericFilters.yr_built_max)) return false;
  if (numericFilters.sqft_min != null && (!Number.isFinite(sqft) || sqft < numericFilters.sqft_min)) return false;
  if (numericFilters.sqft_max != null && (!Number.isFinite(sqft) || sqft > numericFilters.sqft_max)) return false;
  if (numericFilters.appr_val_min != null || numericFilters.appr_val_max != null) {
    const compTotVal = _compMatchedTotVal(comp);
    if (numericFilters.appr_val_min != null && (compTotVal == null || compTotVal < numericFilters.appr_val_min)) return false;
    if (numericFilters.appr_val_max != null && (compTotVal == null || compTotVal > numericFilters.appr_val_max)) return false;
  }

  // Parcel-type gate: hide comp if its bucket is toggled off in Property Type
  // Filters. CAD is the source of truth — when a comp matches a parcel, the
  // parcel's prop_type wins. When CAD has nothing to say, Propelio's coarse
  // category fills in.
  //
  // Three buckets route through Off Market because they all represent
  // "residential without an active listing or with no other classification":
  //   - "active"        — single_family parcel that's on Redfin (the same
  //                       toggle that hides off_market parcels should hide
  //                       these comps; the active/off_market distinction
  //                       matters for parcel rendering, not comp filtering)
  //   - "single_family" — Propelio fallback for Residential (~92.5% of comps
  //                       without a CAD parcel match)
  //   - null            — neither CAD nor Propelio could classify; treat as
  //                       residential-default
  const bucket = _compPropertyTypeBucket(comp);
  if (bucket === "multifamily"   && filters.parcelTypeMultifamily === false) return false;
  if (bucket === "duplexes"      && filters.parcelTypeDuplexes    === false) return false;
  if (bucket === "commercial"    && filters.parcelTypeCommercial  === false) return false;
  if (bucket === "vacant"        && filters.parcelTypeVacant      === false) return false;
  if (bucket === "exempt"        && filters.parcelTypeExempt      === false) return false;
  if (bucket === "off_market"    && filters.parcelTypeOffMarket   === false) return false;
  if (bucket === "active"        && filters.parcelTypeOffMarket   === false) return false;
  if (bucket === "single_family" && filters.parcelTypeOffMarket   === false) return false;
  if (bucket === null            && filters.parcelTypeOffMarket   === false) return false;

  // Neighborhood filter — null/blank comp drops out when a filter is active
  if (filters.neighborhood) {
    const norm = (_nbhdNorm !== undefined) ? _nbhdNorm : normalizeNbhd(filters.neighborhood);
    if (normalizeNbhd(comp?.neighborhood) !== norm) return false;
  }

  return true;
}

let _propelioFilterDebounceId = null;
let propelioCompSortMode = "price_desc";


function applyPropelioClientFilters() {
  if (!window._propelioLast || !Array.isArray(window._propelioLast.comps)) {
    const countEl = document.getElementById("propelio-filter-count");
    if (countEl) countEl.textContent = "";
    renderPropelioCompList([]);
    _renderGoodCompsSection();  // hides section when no comp cache
    return;
  }
  // Chunk E (per-view ratings §4): project each comp's user_rating to the
  // ACTIVE view before any filter/dedup/render reads it. No-op when the flag
  // is off (user_rating stays the backend ARV value). This is also the
  // view-switch re-render path (_setActiveView → restoreFilterState → here).
  _projectCompRatingsForActiveView();
  // Read the current UI filter state FIRST, then build the neighborhood
  // options cache from it. _buildNbhdOptionsCache() derives its options
  // from the active propelioFilterState (minus the neighborhood gate), so
  // building it before refreshing that state used STALE filters — the
  // options lagged one apply cycle behind toggles like OAC.
  propelioFilterState = readPropelioFiltersFromUI();
  _buildNbhdOptionsCache();
  const all = window._propelioLast.comps;
  // Map view: render every passing comp (good/unrated AND bad — bad gets
  // the `.bad-comp` class for visual de-emphasis but stays on the map).
  const _nbhdNorm = normalizeNbhd(propelioFilterState.neighborhood);
  const visibleOnMap = all.filter((c) => compPassesPropelioFilters(c, propelioFilterState, _nbhdNorm));
  _updatePropelioStatusCounts();
  // Render-only dedup: collapse multi-status records on the same parcel
  // to only the highest-priority status (good-rated wins tie-break).
  // Status counts above use two-pass logic — status badges respect the
  // user's actual OAC toggle state ("what would this status deliver right
  // now?"), while the OAC badge stays informational ("how many comps are
  // currently filtered out by OAC?"). See
  // docs/propelio/STATUS_BADGE_OAC_AWARENESS_SPEC.md.
  const visibleOnMapForRender = _dedupCompsForRender(visibleOnMap);
  _renderPropelioComps({ ...window._propelioLast, comps: visibleOnMapForRender });
  if (window._propelioLast) propelioCmaChip.setData(window._propelioLast);
  // List view: hide bad-rated comps entirely from the sidebar list.
  const visibleInList = visibleOnMap.filter((c) => c?.user_rating !== "bad");
  renderPropelioCompList(visibleInList);
  // Update the card-head count chip
  const countEl = document.getElementById("propelio-filter-count");
  if (countEl) {
    countEl.textContent = visibleOnMapForRender.length === all.length
      ? `${all.length}`
      : `${visibleOnMapForRender.length} / ${all.length}`;
  }
  // Good Comps section — sibling list inside #comps-list-block-body. Per
  // SAVED_AREA_GOOD_COMPS_BLOCK_SPEC.md v2 §Goal #7: always shows ALL good-
  // rated comps in the workspace, IGNORING active filter chips. We read
  // from the unfiltered `all` array (not visibleOnMap).
  _renderGoodCompsSection();
  // v1 §2.1 — auto-save filter_state after propelio client filter apply.
  _filterSaveQueueSave();
  window.__aiVisibleComps = visibleOnMap;   // AI module seam (read-only mirror of the VISIBLE set)
}

function _sortPropelioComps(comps, mode) {
  const list = Array.isArray(comps) ? comps.slice() : [];
  const num = (v) => {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  };
  const cmp = (a, b, dir) => {
    if (a == null && b == null) return 0;
    if (a == null) return 1;
    if (b == null) return -1;
    return dir === "asc" ? a - b : b - a;
  };
  switch (mode) {
    case "price_asc":
      list.sort((a, b) => cmp(num(a?.price), num(b?.price), "asc"));
      break;
    case "sqft_desc":
      list.sort((a, b) => cmp(num(a?.sqft), num(b?.sqft), "desc"));
      break;
    case "year_desc":
      list.sort((a, b) => cmp(num(a?.year_built), num(b?.year_built), "desc"));
      break;
    case "distance_asc":
      list.sort((a, b) => cmp(num(a?.distance_mi), num(b?.distance_mi), "asc"));
      break;
    case "price_desc":
    default:
      list.sort((a, b) => cmp(num(a?.price), num(b?.price), "desc"));
      break;
  }
  return list;
}

function _propelioCompRowHtml(comp) {
  const fmtPrice = (n) => Number.isFinite(n) ? `$${Math.round(n).toLocaleString()}` : "—";
  const fmtNum = (n) => Number.isFinite(n) ? Number(n).toLocaleString() : null;
  const ex = comp?.extra || {};
  const status = String(comp?.status || "").toLowerCase();
  const statusClass = _propelioStatusBucket(comp);
  const sqft = fmtNum(Number(comp?.sqft));
  const yr = Number.isFinite(comp?.year_built) ? comp.year_built : null;
  const beds = ex.beds;
  const baths = ex.baths;
  const lotSqft = Number(comp?.lot_size);
  const lotAcres = Number.isFinite(lotSqft) && lotSqft > 0 ? (lotSqft / 43560) : null;
  const lotAcresStr = lotAcres != null ? `${lotAcres.toFixed(lotAcres < 1 ? 2 : 1)} ac lot` : null;
  const neighborhood = String(comp?.neighborhood || "").trim();

  const dim = [];
  if (sqft) dim.push(`${sqft} sqft`);
  if (lotAcresStr) dim.push(lotAcresStr);
  if (yr) dim.push(`${yr}`);
  if (beds != null) dim.push(`${beds}bd`);
  if (baths != null) dim.push(`${baths}ba`);

  const compKey = String(comp?.comp_address_key || "").trim();
  const keyAttr = _propelioEscape(compKey);
  return `
    <div class="propelio-comp-row" data-comp-key="${keyAttr}">
      <div class="propelio-comp-row-top">
        <span class="propelio-comp-row-price">${fmtPrice(Number(comp?.price))}</span>
        <span class="propelio-comp-row-statuswrap">
          <span class="propelio-comp-row-status ${statusClass}">${_propelioEscape(status || "—")}</span>
          ${comp?.user_rating === "good" ? `<span class="propelio-comp-row-good-check" title="Good comp">✓</span>` : ""}
        </span>
      </div>
      <div class="propelio-comp-row-mid">${_propelioEscape(comp?.address || "")}</div>
      ${neighborhood ? `<div class="propelio-comp-row-nbhd">${_propelioEscape(neighborhood)}</div>` : ""}
      ${dim.length ? `<div class="propelio-comp-row-meta">${_propelioEscape(dim.join(" · "))}</div>` : ""}
    </div>
  `;
}

function renderPropelioCompList(comps) {
  const listEl = document.getElementById("propelio-comp-list");
  if (!listEl) return;
  const sorted = _sortPropelioComps(comps, propelioCompSortMode);
  if (!sorted.length) {
    listEl.innerHTML = `<div class="propelio-comp-list-empty">No comps to show.</div>`;
    return;
  }
  listEl.innerHTML = sorted.map(_propelioCompRowHtml).join("");
}

function _findPropelioCompByKey(key) {
  const k = String(key || "").trim();
  if (!k) return null;
  const all = window._propelioLast?.comps;
  if (!Array.isArray(all)) return null;
  return all.find((c) => String(c?.comp_address_key || "").trim() === k) || null;
}

// ─── Good Comps section (saved-area-bonded, filter-independent) ─────────
// Per SAVED_AREA_GOOD_COMPS_BLOCK_SPEC.md v2. Sibling section inside
// #comps-list-block-body between the Subject card and the main comp list.
// Renders one rich card per comp with user_rating === "good" in the
// current workspace. Always shows ALL good comps regardless of active
// filter chips. Click row body → fly to the comp on the map. Per-card
// × Remove clears the rating; Bad flips good → bad. Both use optimistic
// UI with revert + toast on server failure.

function _propelioGoodCompCardHtml(comp) {
  const fmtPrice = (n) => Number.isFinite(n) ? `$${Math.round(n).toLocaleString()}` : "—";
  const fmtNum = (n) => Number.isFinite(n) ? Number(n).toLocaleString() : null;
  const ex = comp?.extra || {};
  const status = String(comp?.status || "").trim();
  const sqft = fmtNum(Number(comp?.sqft));
  const yr = Number.isFinite(comp?.year_built) ? comp.year_built : null;
  const beds = ex.beds;
  const baths = ex.baths;
  const lotSqft = Number(comp?.lot_size);
  const lotAcres = Number.isFinite(lotSqft) && lotSqft > 0 ? (lotSqft / 43560) : null;
  const lotAcresStr = lotAcres != null ? `${lotAcres.toFixed(lotAcres < 1 ? 2 : 1)} ac lot` : null;
  const neighborhood = String(comp?.neighborhood || "").trim();
  const soldDate = String(comp?.sold_date || comp?.close_date || "").trim();
  const dom = Number.isFinite(comp?.dom) ? comp.dom : null;
  const distance = Number.isFinite(comp?.distance_mi) ? comp.distance_mi : null;
  const priceNum = Number(comp?.price);
  const sqftNum = Number(comp?.sqft);
  const ppsf = (Number.isFinite(priceNum) && Number.isFinite(sqftNum) && sqftNum > 0)
    ? `$${Math.round(priceNum / sqftNum)}/sqft` : null;

  const meta1 = [sqft ? `${sqft} sqft` : null, lotAcresStr, yr ? `${yr}` : null,
                 (beds != null ? `${beds}bd` : null), (baths != null ? `${baths}ba` : null)]
                .filter(Boolean).join(" · ");
  const meta2 = [soldDate || null, (dom != null ? `${dom} DOM` : null), status || null]
                .filter(Boolean).join(" · ");
  const meta3 = [(distance != null ? `${distance.toFixed(2)} mi` : null), ppsf]
                .filter(Boolean).join(" · ");

  const compKey = String(comp?.comp_address_key || "").trim();
  const keyAttr = _propelioEscape(compKey);
  const addr = _propelioEscape(comp?.address || "");
  const nbhd = _propelioEscape(neighborhood);

  return `
    <article class="propelio-good-comp-card" tabindex="0" role="button" data-comp-key="${keyAttr}" aria-label="Good comp: ${addr}. Press Enter to focus on map.">
      <div class="propelio-good-comp-card-top">
        <span class="propelio-good-comp-card-price">${fmtPrice(priceNum)}</span>
        <span class="propelio-good-comp-card-badge">Good</span>
        <div class="propelio-good-comp-card-actions">
          <button type="button" class="propelio-good-comp-action-btn remove" data-action="remove" data-comp-key="${keyAttr}" aria-label="Remove Good rating from this comp">×</button>
          <button type="button" class="propelio-good-comp-action-btn bad" data-action="bad" data-comp-key="${keyAttr}" aria-label="Change rating to Bad">Bad</button>
        </div>
      </div>
      <div class="propelio-good-comp-card-addr">${addr}</div>
      <div class="propelio-good-comp-card-nbhd">${nbhd}</div>
      <div class="propelio-good-comp-card-meta">${_propelioEscape(meta1)}</div>
      <div class="propelio-good-comp-card-meta2">${_propelioEscape(meta2)}</div>
      <div class="propelio-good-comp-card-meta3">${_propelioEscape(meta3)}</div>
    </article>`;
}

function _renderGoodCompsSection() {
  const section = document.getElementById("propelio-good-comps-section");
  const list = document.getElementById("propelio-good-comps-list");
  const countEl = document.getElementById("propelio-good-comps-count");
  const emptyEl = document.getElementById("propelio-good-comps-empty");
  if (!section || !list) return;

  // Visibility: only when a saved area is loaded. No saved area =
  // ratings aren't meaningful (comp_ratings is workspace-scoped).
  if (!_currentLoadedAreaId) {
    section.classList.add("hidden");
    list.innerHTML = "";
    if (countEl) countEl.textContent = "0";
    if (emptyEl) emptyEl.classList.add("hidden");
    return;
  }
  section.classList.remove("hidden");

  // Source: ALL comps in the workspace cache with user_rating === "good".
  // NOT visibleOnMap — filter chips are intentionally ignored here per
  // spec §Goal #7 (good comps are a curated bookmark; filters are for
  // hunting new comps).
  const all = Array.isArray(window._propelioLast?.comps) ? window._propelioLast.comps : [];
  const goodComps = all.filter((c) => c?.user_rating === "good");
  // Sort using the same sort selection as the main comp list — single
  // source of truth (propelioCompSortMode + _sortPropelioComps).
  const sorted = _sortPropelioComps(goodComps, propelioCompSortMode);

  if (countEl) countEl.textContent = String(sorted.length);
  if (sorted.length === 0) {
    list.innerHTML = "";
    if (emptyEl) emptyEl.classList.remove("hidden");
    return;
  }
  if (emptyEl) emptyEl.classList.add("hidden");
  list.innerHTML = sorted.map(_propelioGoodCompCardHtml).join("");
}

// Optimistic rating mutation: update local cache immediately + re-render
// so the card disappears from the good list instantly, then persist to
// the server in the background. Revert on failure with a toast.
async function _setGoodCompRatingOptimistic(compKey, newRating) {
  const comp = _findPropelioCompByKey(compKey);
  if (!comp) return;
  // Capture the active view synchronously (per-view ratings §4 / H4).
  const _view = _activeView;
  if (!ARV_NBV_EXPORT_ENABLED) {
    // Flag-OFF: today's behavior, byte-identical.
    const oldRating = comp.user_rating;
    comp.user_rating = newRating;
    _renderGoodCompsSection();
    const ok = await ratePropelioComp(compKey, newRating, _view);
    if (!ok) {
      comp.user_rating = oldRating;
      _renderGoodCompsSection();
      _showToast("Rating update failed — reverted", "error");
    }
    return;
  }
  // Flag-ON: optimistic via the canonical store + active-view projection, so a
  // revert restores the exact per-view state (not just user_rating).
  _ensureRatingCanonical(comp);
  const oldArv = comp._ratingArv;
  const oldByView = { ...comp.ratings_by_view };
  _setRatingCanonical(comp, newRating, _view);
  _projectRatingActive(comp);
  _renderGoodCompsSection();
  const ok = await ratePropelioComp(compKey, newRating, _view);
  if (!ok) {
    comp._ratingArv = oldArv;
    comp.ratings_by_view = oldByView;
    _projectRatingActive(comp);
    _renderGoodCompsSection();
    _showToast("Rating update failed — reverted", "error");
  }
}

// Document-level delegation — matches the existing rating-button pattern
// at line ~6338. Works regardless of when map.js runs vs DOMContentLoaded,
// and survives every re-render of the good-comps list innerHTML.
document.addEventListener("click", (ev) => {
  const card = ev.target.closest(".propelio-good-comp-card");
  if (!card) return;
  // Action button (Remove × or Bad) inside the card — stopProp so the
  // card's fly-to behavior doesn't ALSO fire.
  const actionEl = ev.target.closest(".propelio-good-comp-action-btn[data-action]");
  if (actionEl) {
    ev.stopPropagation();
    const compKey = actionEl.getAttribute("data-comp-key");
    const action = actionEl.getAttribute("data-action");
    if (!compKey) return;
    if (action === "remove") void _setGoodCompRatingOptimistic(compKey, null);
    else if (action === "bad") void _setGoodCompRatingOptimistic(compKey, "bad");
    return;
  }
  // Row body click → fly to comp on map.
  const k = card.getAttribute("data-comp-key");
  if (k) flyToAndOpenPropelioComp(k);
});

// Outreach save-on-blur / change handler (Mailer + Phone Tracking, 2026-06-03).
// Delegated at document level so it survives every popup re-render. Each
// outreach input PUTs to /api/parcels/outreach with the correct _set flag.
async function _putOutreachField(county, parcelId, field, value) {
  const body = { county, parcel_id: parcelId };
  if (field === "contact_info_retrieved") {
    body.contact_info_retrieved = Boolean(value);
    body.contact_info_retrieved_set = true;
  } else if (field === "mailer_date") {
    body.mailer_date = value || null;
    body.mailer_date_set = true;
  } else {
    throw new Error(`Unknown outreach field: ${field}`);
  }
  const resp = await fetch("/api/parcels/outreach", {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const txt = await resp.text().catch(() => "");
    throw new Error(`Outreach save failed (${resp.status}): ${txt}`);
  }
  const data = await resp.json().catch(() => null);

  // Update the in-memory feature so popup re-open / CSV path within the
  // same client session see the new value WITHOUT a full page reload.
  // KK reported 2026-06-03: edit popup → export CSV → cell was blank
  // because the cached job rows + cached feature properties hadn't been
  // updated. Backend now re-hydrates on cache-read; frontend mirrors that
  // discipline by updating the local feature properties in place.
  try {
    _updateLocalFeatureOutreach(county, parcelId, field, value);
    _rebuildOutreachOverlays();
    // Mike report 2026-06-05: typing a date in the popup didn't bump the
    // Contact Status filter count badge — overlay refreshed but the
    // sidebar number stayed stale until something else triggered a
    // re-render. Count is derived from feature.outreach_* properties,
    // so refresh it now that we've mutated them locally.
    _updateMergedSidebarCounts();
  } catch (err) {
    console.warn("[outreach] local feature update failed", err);
  }
  return data;
}

function _updateLocalFeatureOutreach(county, parcelId, field, value) {
  if (!lastAnalysisGeojson || !Array.isArray(lastAnalysisGeojson.features)) return;
  for (const feat of lastAnalysisGeojson.features) {
    const props = feat?.properties;
    if (!props) continue;
    const featCounty = String(props.source_county || "").trim().toLowerCase();
    if (featCounty !== county) continue;
    const featKey = featCounty === "dcad"
      ? String(props.account_num || "").trim()
      : String(props.parcel_key || props.account_num || "").trim();
    if (featKey !== parcelId) continue;
    if (field === "contact_info_retrieved") {
      props.outreach_contact_info_retrieved = Boolean(value);
    } else if (field === "mailer_date") {
      props.outreach_mailer_date = value || null;
    }
    break;
  }
}

document.addEventListener("change", (ev) => {
  const el = ev.target;
  if (!(el instanceof HTMLElement)) return;
  if (!el.classList.contains("parcel-panel-outreach-input")) return;
  const field = el.getAttribute("data-outreach-field");
  const county = el.getAttribute("data-outreach-county");
  const parcelId = el.getAttribute("data-outreach-parcel-id");
  if (!field || !county || !parcelId) return;

  let value;
  if (field === "contact_info_retrieved") {
    value = el.checked;
    const labelEl = el.parentElement?.querySelector(".parcel-panel-outreach-contact-label");
    if (labelEl) labelEl.textContent = value ? "yes" : "";
  } else {
    value = el.value || "";
  }

  // Optimistic UI — assume save will succeed. Revert + toast on failure.
  void _putOutreachField(county, parcelId, field, value).catch((err) => {
    console.warn("[outreach] save failed", err);
    if (field === "contact_info_retrieved" && el instanceof HTMLInputElement) {
      el.checked = !value;
      const labelEl = el.parentElement?.querySelector(".parcel-panel-outreach-contact-label");
      if (labelEl) labelEl.textContent = el.checked ? "yes" : "";
    }
    try {
      window?.alert?.(`Outreach save failed — please retry. ${String(err).slice(0, 120)}`);
    } catch (_) {}
  });
});

document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Enter" && ev.key !== " ") return;
  const card = ev.target.closest(".propelio-good-comp-card");
  if (!card) return;
  // Don't intercept Enter/Space when focus is on an action button — let
  // the native button handle it (which fires a click event we delegate).
  if (ev.target.closest(".propelio-good-comp-action-btn")) return;
  ev.preventDefault();
  const k = card.getAttribute("data-comp-key");
  if (k) flyToAndOpenPropelioComp(k);
});

async function flyToAndOpenPropelioComp(compKey) {
  let layer = propelioCompLayerByKey.get(String(compKey || "").trim());

  // Dedup-loser fallback (KK reported 2026-06-03 PM):
  // The sidebar comp list shows ALL comps (including ones that lost the
  // _dedupCompsForRender election); only the WINNERS are registered in
  // propelioCompLayerByKey. Without this fallback, clicking a loser comp
  // (e.g. multi-address listing "1825 & 1827 Pollard Street" that shares
  // a parcel with another 1827 Pollard listing) is a silent no-op.
  //
  // When we don't find a direct layer, look up the clicked comp's data,
  // compute its dedup key, and walk the registered winners to find one
  // that shares the same dedup key. Use the winner's layer for fly-to +
  // outline. Visible behavior: clicking any of the duplicates flies to
  // the same parcel footprint (the winner's), which is what the analyst
  // expects since they all share a parcel.
  if (!layer) {
    const all = (window._propelioLast && Array.isArray(window._propelioLast.comps))
      ? window._propelioLast.comps
      : [];
    const clicked = all.find(
      (c) => String(c?.comp_address_key || "").trim() === String(compKey || "").trim()
    );
    if (clicked) {
      const targetDedupKey = _compDedupKey(clicked);
      if (targetDedupKey) {
        for (const [renderedKey, candidateLayer] of propelioCompLayerByKey.entries()) {
          const winner = all.find(
            (c) => String(c?.comp_address_key || "").trim() === renderedKey
          );
          if (winner && _compDedupKey(winner) === targetDedupKey) {
            layer = candidateLayer;
            break;
          }
        }
      }
    }
  }

  if (!layer) return;

  // Show a crisp purple outline on the map for the clicked comp. If the
  // comp rendered as a footprint, outline its polygon; if it's a fallback
  // dot, skip the outline (no polygon to draw) — the fly-to still happens.
  try {
    if (typeof layer.toGeoJSON === "function") {
      const gj = layer.toGeoJSON();
      // L.geoJSON returns a FeatureCollection wrapping one Feature, OR a
      // single Feature, depending on Leaflet version. Handle both shapes.
      const feat = gj?.type === "FeatureCollection"
        ? (gj.features && gj.features[0])
        : gj;
      _renderSelectedOutline(feat?.geometry || null);
    } else {
      _clearSelectedOutline();
    }
  } catch (_) {
    _clearSelectedOutline();
  }

  // Resolve a bounds (preferred) and a center point for the layer.
  let bounds = null;
  let center = null;
  if (typeof layer.getBounds === "function") {
    try {
      const b = layer.getBounds();
      if (b && b.isValid()) {
        bounds = b;
        center = b.getCenter();
      }
    } catch (_) { /* noop */ }
  }
  if (!center && typeof layer.getLatLng === "function") {
    center = layer.getLatLng();
  }

  // Honor the global click-mode toggle so list-clicks behave the same as
  // saved-area / target clicks.
  const mode = (typeof getClickMode === "function" ? getClickMode() : "jump");
  if (mode === "jump") {
    if (bounds) {
      map.fitBounds(bounds, { padding: [40, 40], maxZoom: 17 });
    } else if (center) {
      map.flyTo(center, Math.max(map.getZoom(), 17), { duration: 0.4 });
    }
  } else if (center) {
    if (!isPointInViewport(center)) {
      if (bounds) map.fitBounds(bounds, { padding: [40, 40], maxZoom: map.getZoom() });
      else map.panTo(center);
    }
  }
}

function _setPropelioFootprintHighlight(compKey, on) {
  const layer = propelioCompLayerByKey.get(String(compKey || "").trim());
  if (!layer) return;
  const apply = (l) => {
    const el = typeof l.getElement === "function" ? l.getElement() : null;
    if (el && el.classList) {
      el.classList.toggle("propelio-footprint-highlight", !!on);
    }
  };
  if (typeof layer.eachLayer === "function") layer.eachLayer(apply);
  else apply(layer);
}

async function ratePropelioComp(compKey, rating, view) {
  const areaId = (typeof _currentLoadedAreaId === "string" ? _currentLoadedAreaId : "") || "";
  if (!areaId || !compKey) return false;
  // View captured at click time (per-view ratings §4). Invalid/absent → the
  // current active view. Flag-OFF: _activeView is always "arv".
  const _view = (view === "arv" || view === "nbv" || view === "export") ? view : _activeView;
  const body = {
    saved_area_id: areaId,
    comp_address_key: compKey,
    rating: rating === "good" || rating === "bad" ? rating : null,
  };
  // Only send `view` when the feature is enabled — keeps the flag-OFF wire
  // byte-identical to today (backend treats absent view as 'arv', C2).
  if (ARV_NBV_EXPORT_ENABLED) body.view = _view;
  try {
    const resp = await fetch("/api/propelio/comp/rate", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      console.warn("[propelio] rate failed:", resp.status);
      return false;
    }
    // Update the in-memory comp so re-render reflects the new rating without a
    // round-trip to the archive. Flag-ON writes the canonical store for the
    // captured view; applyPropelioClientFilters re-projects user_rating for the
    // active view. Flag-OFF keeps today's direct user_rating write.
    const comp = _findPropelioCompByKey(compKey);
    if (comp) {
      if (ARV_NBV_EXPORT_ENABLED) _setRatingCanonical(comp, body.rating, _view);
      else comp.user_rating = body.rating;
    }
    applyPropelioClientFilters();
    return true;
  } catch (err) {
    console.error("[propelio] rate error:", err);
    return false;
  }
}

function applyPropelioClientFiltersDebounced() {
  if (_propelioFilterDebounceId) clearTimeout(_propelioFilterDebounceId);
  _propelioFilterDebounceId = setTimeout(applyPropelioClientFilters, 150);
}

async function pullPropelioRefresh() {
  const btn = document.getElementById("btn-propelio-refresh");
  if (!btn) return;
  if (_activeDeepPullJobId) {
    _showDeepPullBanner("A quick sweep is running — wait for it to finish.");
    setTimeout(_hideDeepPullBanner, 4000);
    return;
  }
  if (!lastAnalysisGeojson) {
    _showToast("Draw a polygon first", "error");
    return;
  }
  const filters = readPropelioFiltersFromUI();
  propelioFilterState = filters;
  // v1 §2.1 — auto-save filter_state after propelio refresh
  // (covers prop-months + prop-range which only get read here).
  _filterSaveQueueSave();
  const targetAddress = _deriveDeepPullTargetAddress();
  const savedAreaId = (typeof _currentLoadedAreaId === "string" ? _currentLoadedAreaId : "") || "";
  btn.disabled = true;
  _showDeepPullBanner("Running custom search...");
  try {
    let data;
    if (savedAreaId) {
      const resp = await fetch("/api/propelio/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({
          saved_area_id: savedAreaId,
          months: filters.months,
          range_override_mi: filters.range,
          target_address: targetAddress || null,
        }),
      });
      if (!resp.ok) throw new Error(`refresh failed: ${resp.status}`);
      data = await resp.json();
    } else {
      if (!Array.isArray(lastPolygon) || lastPolygon.length < 3) {
        console.warn("[propelio] no polygon to refresh against");
        return;
      }
      const resp = await fetch("/api/propelio/by-polygon", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({
          polygon: lastPolygon,
          months: filters.months,
          range_override_mi: filters.range,
          target_address: targetAddress || null,
        }),
      });
      if (!resp.ok) throw new Error(`by-polygon failed: ${resp.status}`);
      data = await resp.json();
    }
    // Reload polygon cache so display filters operate on accumulated
    // cache, not on this narrow scrape's response. Server-side merge
    // already inserted any net-new comps. Fall back to the response if
    // the reload didn't populate (missing polygon, empty cache, error).
    let cacheLoaded = false;
    try {
      const cacheResp = await _fetchPolygonCacheOnly();
      cacheLoaded = !!(
        cacheResp
        && Array.isArray(cacheResp.comps)
        && cacheResp.comps.length > 0
      );
    } catch (cacheErr) {
      console.warn("[propelio] cache reload after custom search failed:", cacheErr);
    }
    if (!cacheLoaded) {
      window._propelioLast = data;
      _updatePropelioStatusCounts();
      applyPropelioClientFilters();
    }
    const returned = Number(data?.ingestion_stats?.returned || 0);
    const newToCache = Number(data?.ingestion_stats?.new_to_cache || 0);
    if (returned > 0 && newToCache > 0) {
      _showDeepPullBanner(`Returned ${returned} comps · ${newToCache} new to cache`);
    } else if (returned > 0) {
      _showDeepPullBanner(`Returned ${returned} comps · 0 new since last pull`);
    } else {
      _showDeepPullBanner("0 comps returned for these filters");
    }
    setTimeout(_hideDeepPullBanner, 6000);
  } catch (err) {
    console.error("[propelio] refresh error:", err);
    _showDeepPullBanner(`Custom search failed: ${err?.message || err}`);
    setTimeout(_hideDeepPullBanner, 6000);
  } finally {
    btn.disabled = false;
  }
}

function resetPropelioFilters() {
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
  const setChk = (id, v) => { const el = document.getElementById(id); if (el) el.checked = v; };
  set("prop-months", String(DEFAULT_PROPELIO_FILTERS.months));
  set("prop-range", String(DEFAULT_PROPELIO_FILTERS.range));
  _setPropStatusFilter("sold", true, { apply: false });
  _setPropStatusFilter("active", true, { apply: false });
  _setPropStatusFilter("pending", true, { apply: false });
  setChk("prop-outside-area", false);
  set("prop-sold-within", "");
  set("prop-lot-min", ""); set("prop-lot-max", "");
  set("prop-sqft-min", ""); set("prop-sqft-max", "");
  set("prop-year-min", ""); set("prop-year-max", "");
  set("prop-price-min", ""); set("prop-price-max", "");
  set("prop-neighborhood", "");
  _renderNbhdChip(null);
  _resetAiMode();
  _renderAiBar();
  applyPropelioClientFilters();
}

function _setPropStatusFilter(status, checked, options = {}) {
  const apply = options.apply !== false;
  const next = Boolean(checked);
  const mapBox = document.getElementById(`prop-status-${status}`);
  const cfBox = document.getElementById(`cf-status-${status}`);
  if (mapBox) mapBox.checked = next;
  if (cfBox) cfBox.checked = next;
  if (apply) applyPropelioClientFilters();
}

// === Neighborhood filter helpers ============================================

function _buildNbhdOptionsCache() {
  const comps = window._propelioLast?.comps;
  if (!Array.isArray(comps) || comps.length === 0) {
    _nbhdOptionsCache = [];
    _nbhdOptionsCacheRef = null;
    _nbhdOptionsCacheSig = null;
    return;
  }
  // Options mirror what's actually VISIBLE: build from comps that pass the
  // active filters EXCEPT the neighborhood filter itself (so you can still
  // re-pick). This makes the list + counts react live to OAC and every other
  // filter — OAC off shows only in-area neighborhoods, OAC on shows all.
  const filtersNoNbhd = { ...propelioFilterState, neighborhood: null };
  // Signature so a filter change (e.g. OAC toggle — same comps array) forces a
  // rebuild, while typing (no filter change) stays a cheap no-op. No geometry
  // math in the signature itself; the point-in-polygon work happens only on the
  // rebuild below, which fires on filter/comp change, not per keystroke.
  const polySig = Array.isArray(lastPolygon) && lastPolygon.length
    ? lastPolygon.length + ":" + JSON.stringify(lastPolygon[0]) + JSON.stringify(lastPolygon[lastPolygon.length - 1])
    : "0";
  const sig = JSON.stringify(filtersNoNbhd) + "|" + polySig;
  if (comps === _nbhdOptionsCacheRef && sig === _nbhdOptionsCacheSig) return;
  _nbhdOptionsCacheRef = comps;
  _nbhdOptionsCacheSig = sig;
  const keyMap = new Map();
  for (const c of comps) {
    // Same visibility gate the map/list use, minus the neighborhood filter.
    if (!compPassesPropelioFilters(c, filtersNoNbhd)) continue;
    const raw = c?.neighborhood;
    if (!raw || !String(raw).trim()) continue;
    const key = normalizeNbhd(raw);
    if (!keyMap.has(key)) keyMap.set(key, { counts: new Map(), total: 0 });
    const entry = keyMap.get(key);
    entry.total++;
    entry.counts.set(raw, (entry.counts.get(raw) || 0) + 1);
  }
  _nbhdOptionsCache = Array.from(keyMap.values())
    .map(({ counts, total }) => {
      let bestDisplay = "", bestCount = 0;
      for (const [variant, cnt] of counts) {
        if (cnt > bestCount) { bestCount = cnt; bestDisplay = variant; }
      }
      return { display: bestDisplay, count: total };
    })
    .sort((a, b) => a.display.localeCompare(b.display));
}

function _renderNbhdChip(display) {
  const chipEl = document.getElementById("prop-neighborhood-chip");
  const searchEl = document.getElementById("prop-neighborhood-search");
  const optionsEl = document.getElementById("prop-neighborhood-options");
  if (!chipEl) return;
  if (!display) {
    chipEl.hidden = true;
    chipEl.innerHTML = "";
    if (searchEl) searchEl.hidden = false;
    return;
  }
  chipEl.innerHTML = `<span class="nbhd-chip-label">${_propelioEscape(display)}</span><span class="nbhd-chip-x" role="button" aria-label="Clear neighborhood filter" tabindex="0">✕</span>`;
  chipEl.hidden = false;
  if (searchEl) { searchEl.value = ""; searchEl.hidden = true; }
  if (optionsEl) { optionsEl.hidden = true; optionsEl.innerHTML = ""; }
}

function _renderNbhdOptions(query) {
  const optionsEl = document.getElementById("prop-neighborhood-options");
  if (!optionsEl) return;
  if (!query) { optionsEl.hidden = true; optionsEl.innerHTML = ""; return; }
  // Build/refresh the options cache on demand. It's ref-guarded (instant no-op
  // when comps are unchanged), so the typeahead is self-sufficient even when
  // comps were loaded via a path that didn't run applyPropelioClientFilters
  // (e.g. saved-area restore) — otherwise the cache could be empty when typing.
  _buildNbhdOptionsCache();
  const q = query.toLowerCase();
  const qNo = q.replace(/\s+/g, "");
  // Whitespace-insensitive matching: the source has letter-spaced names like
  // "J V C", so also compare with spaces stripped on both sides — typing "jvc"
  // matches "J V C" (and "j v c" still works).
  const dispLc = (o) => o.display.toLowerCase();
  const hit = (o) => dispLc(o).includes(q) || dispLc(o).replace(/\s+/g, "").includes(qNo);
  const startsHit = (o) => dispLc(o).startsWith(q) || dispLc(o).replace(/\s+/g, "").startsWith(qNo);
  const matches = (_nbhdOptionsCache || []).filter(hit);
  if (!matches.length) { optionsEl.hidden = true; optionsEl.innerHTML = ""; return; }
  // Rank prefix matches first, then the remaining substring matches. The cache
  // is already alphabetical and Array.sort is stable, so each group stays
  // alphabetical — typing "s" surfaces the S-neighborhoods on top, with the
  // contains-an-s matches below instead of an unordered pile.
  matches.sort((a, b) => (startsHit(a) ? 0 : 1) - (startsHit(b) ? 0 : 1));
  optionsEl.innerHTML = matches
    .map((o) => `<div class="nbhd-option" data-display="${_propelioEscape(o.display)}">${_propelioEscape(o.display)} (${o.count})</div>`)
    .join("");
  optionsEl.hidden = false;
}

function _selectNbhdOption(display) {
  const hiddenEl = document.getElementById("prop-neighborhood");
  if (hiddenEl) hiddenEl.value = display;
  _renderNbhdChip(display);
  applyPropelioClientFiltersDebounced();
  _filterSaveQueueSave();
}

function _clearNbhdFilter() {
  const hiddenEl = document.getElementById("prop-neighborhood");
  if (hiddenEl) hiddenEl.value = "";
  _renderNbhdChip(null);
  applyPropelioClientFiltersDebounced();
  _filterSaveQueueSave();
}

// Deep Pull sidebar button state. Declared BEFORE the IIFE below so that
// _ensureStickyPropelioButton() can be safely called from inside the IIFE
// (without hitting a TDZ ReferenceError as the original J3 init did).
let propelioStickyAnchor = null;
let propelioStickyBtn = null;

(function _initPropelioFilterListeners() {
  const liveIds = [
    "prop-sold-within",
    "prop-lot-min", "prop-lot-max",
    "prop-sqft-min", "prop-sqft-max",
    "prop-year-min", "prop-year-max",
    "prop-price-min", "prop-price-max",
  ];
  liveIds.forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("input", applyPropelioClientFiltersDebounced);
    el.addEventListener("change", applyPropelioClientFiltersDebounced);
  });
  // OAC (Outside-Area Comps) is a TOGGLE, not a typed range — apply
  // DIRECTLY on change like the status toggles (_setPropStatusFilter),
  // not through the 150ms debounce the numeric ranges use. The debounce
  // made the first click feel dead and invited the double/triple-click.
  // The toolbar OAC button dispatches a "change" on this checkbox, so a
  // single button click now applies immediately.
  const oacBox = document.getElementById("prop-outside-area");
  if (oacBox) {
    oacBox.addEventListener("change", applyPropelioClientFilters);
  }
  // AI mode toggle (Task 2/3) — the button IS the mode switch, direct apply.
  const aiModeToggleBtn = document.getElementById("ai-mode-toggle");
  if (aiModeToggleBtn) {
    aiModeToggleBtn.addEventListener("click", () => {
      if (_aiModeOn) _disableAiMode();
      else _enableAiMode();
    });
  }
  // Manual edit of ANY field AI mode may have written → drop the mode via
  // the exact 4-step Hole-B sequence (§2.5), never the naive "just clear the
  // flag" — see _dropAiModeForEdit's own comment for why.
  ["prop-lot-min", "prop-lot-max", "prop-sqft-min", "prop-sqft-max", "prop-year-min", "prop-year-max"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("input", () => _dropAiModeForEdit(id));
  });
  ["sold", "active", "pending"].forEach((status) => {
    const mapBox = document.getElementById(`prop-status-${status}`);
    const cfBox = document.getElementById(`cf-status-${status}`);
    mapBox?.addEventListener("change", (ev) => {
      _setPropStatusFilter(status, Boolean(ev.target?.checked));
    });
    cfBox?.addEventListener("change", (ev) => {
      _setPropStatusFilter(status, Boolean(ev.target?.checked));
    });
  });
  const refreshBtn = document.getElementById("btn-propelio-refresh");
  if (refreshBtn) refreshBtn.addEventListener("click", () => void pullPropelioRefresh());
  // Wire the Deep Pull sidebar button's click handler at init time so it
  // fires the "Draw a polygon first" toast even before any polygon has been
  // drawn. Now safe because propelioStickyBtn is declared above this IIFE.
  _ensureStickyPropelioButton();
  const resetBtn = document.getElementById("btn-propelio-reset");
  if (resetBtn) resetBtn.addEventListener("click", resetPropelioFilters);

  const sortEl = document.getElementById("propelio-comp-sort");
  if (sortEl) {
    sortEl.value = propelioCompSortMode;
    sortEl.addEventListener("change", () => {
      propelioCompSortMode = sortEl.value || "price_desc";
      applyPropelioClientFilters();
      // v1 §2.1 — auto-save filter_state after sort mode change.
      _filterSaveQueueSave();
    });
  }

  const listEl = document.getElementById("propelio-comp-list");
  if (listEl) {
    listEl.addEventListener("click", (ev) => {
      const row = ev.target.closest(".propelio-comp-row");
      if (!row) return;
      const k = row.getAttribute("data-comp-key");
      if (k) flyToAndOpenPropelioComp(k);
    });
    listEl.addEventListener("mouseover", (ev) => {
      const row = ev.target.closest(".propelio-comp-row");
      if (!row) return;
      const k = row.getAttribute("data-comp-key");
      if (k) _setPropelioFootprintHighlight(k, true);
    });
    listEl.addEventListener("mouseout", (ev) => {
      const row = ev.target.closest(".propelio-comp-row");
      if (!row) return;
      const k = row.getAttribute("data-comp-key");
      if (k) _setPropelioFootprintHighlight(k, false);
    });
  }

  // Neighborhood filter typeahead
  const nbhdSearchEl = document.getElementById("prop-neighborhood-search");
  if (nbhdSearchEl) {
    nbhdSearchEl.addEventListener("input", () => {
      _renderNbhdOptions(nbhdSearchEl.value.trim());
    });
    nbhdSearchEl.addEventListener("blur", () => {
      // Delay so an option click fires before the dropdown closes
      setTimeout(() => {
        const optEl = document.getElementById("prop-neighborhood-options");
        if (optEl) { optEl.hidden = true; optEl.innerHTML = ""; }
      }, 200);
    });
  }
  // Option selection (delegation on the dropdown container)
  const nbhdOptionsEl = document.getElementById("prop-neighborhood-options");
  if (nbhdOptionsEl) {
    nbhdOptionsEl.addEventListener("click", (ev) => {
      const opt = ev.target.closest(".nbhd-option");
      if (!opt) return;
      _selectNbhdOption(opt.dataset.display || "");
    });
  }
  // Chip clear (delegation on chip container)
  const nbhdChipEl = document.getElementById("prop-neighborhood-chip");
  if (nbhdChipEl) {
    nbhdChipEl.addEventListener("click", (ev) => {
      if (ev.target.closest(".nbhd-chip-x")) _clearNbhdFilter();
    });
    nbhdChipEl.addEventListener("keydown", (ev) => {
      if (ev.target.closest(".nbhd-chip-x") && (ev.key === "Enter" || ev.key === " ")) {
        ev.preventDefault();
        _clearNbhdFilter();
      }
    });
  }

  // Close dropdown on outside click. Bubbles from child → document, so
  // the option-click delegation on #prop-neighborhood-options fires first
  // and hides the dropdown before this runs — making optEl.hidden true
  // before the check, safe no-op after a selection.
  document.addEventListener("click", (ev) => {
    const optEl = document.getElementById("prop-neighborhood-options");
    if (!optEl || optEl.hidden) return;
    if (!ev.target.closest(".nbhd-filter-wrap")) {
      optEl.hidden = true;
      optEl.innerHTML = "";
    }
  });

  // Document-level delegation for popup rating buttons. Popups are recreated
  // on every render so a single delegated listener is simpler than per-popup
  // wiring.
  //
  // Bundled race-fix per PARCEL_RATINGS_SPEC.md v2 §5: optimistic map
  // mark (synchronous) BEFORE the async ratePropelioComp POST. Eliminates
  // the "first Good Comp checkmark slow" lag — the mark now appears the
  // instant the user clicks Good, not after the server round-trip + full
  // re-render. The eventual applyPropelioClientFilters re-render
  // re-establishes the canonical mark from cache (briefly removes +
  // re-adds the optimistic mark, but the visual gap is sub-frame). Per-
  // key mutation versioning prevents stale rollback from rapid clicks.
  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".propelio-rate-btn");
    if (!btn) return;
    const compKey = btn.getAttribute("data-comp-key") || "";
    const parcelCounty = (btn.getAttribute("data-county") || "").trim().toLowerCase();
    const parcelAccount = (btn.getAttribute("data-account-num") || "").trim();
    const rating = btn.getAttribute("data-rating");
    const hasComp = Boolean(compKey);
    const hasParcel = Boolean(parcelCounty && parcelAccount);
    if (!hasComp && !hasParcel) return;
    const newRating = rating === "clear" ? null : rating;
    // Per-view ratings §4 / H4: capture the active view AT CLICK TIME (a
    // synchronous read), then thread it through the async writes + view-key the
    // mutation seqs so a view switch mid-flight can't cross-contaminate.
    const _view = _activeView;

    // Optimistic popup button highlighting — instant feedback
    const container = btn.parentElement;
    if (container) {
      container.querySelectorAll(".propelio-rate-btn").forEach((b) => {
        if (rating === "good") {
          b.classList.toggle("is-active", b.classList.contains("good"));
        } else if (rating === "bad") {
          b.classList.toggle("is-active", b.classList.contains("bad"));
        } else {
          b.classList.remove("is-active");
        }
      });
    }

    // Per the 2026-05-24 design call: one click writes to BOTH
    // parcel_ratings AND comp_ratings when both keys are present.
    // Independent versioning per (kind, id) so a comp-only fast retry
    // doesn't roll back a parcel-only mark and vice versa.

    if (hasComp) {
      _bumpMutationSeq("comp", `${compKey}:${_view}`);
      // Suppress comp optimistic mark when a parcel rating is ALSO being
      // written this click — the parcel mark covers the same spot and
      // two stacked ✓s is the bug KK reported 2026-05-24.
      if (!hasParcel) {
        _setCompRatingMarkOptimistic(compKey, newRating);
      }
      void ratePropelioComp(compKey, newRating, _view);
    }

    if (hasParcel) {
      const mutId = `${parcelCounty}:${parcelAccount}:${_view}`;
      const seq = _bumpMutationSeq("parcel", mutId);
      const previousRating = _getCachedParcelRating(parcelCounty, parcelAccount);
      _setParcelRatingMarkOptimistic(parcelCounty, parcelAccount, newRating);
      void rateParcel(parcelCounty, parcelAccount, newRating, _view).then((ok) => {
        if (!ok && _isLatestMutation("parcel", mutId, seq)) {
          // Only repaint the optimistic mark if we're STILL on the view the
          // click happened in (H4). If the user switched views mid-flight, the
          // switch already re-rendered marks from the canonical store (which the
          // failed write never touched), so repainting previousRating here would
          // paint a wrong-view mark. The toast still fires either way.
          if (_view === _activeView) {
            _setParcelRatingMarkOptimistic(parcelCounty, parcelAccount, previousRating);
          }
          _showToast("Rating update failed — reverted", "error");
        }
      });
    }
  });
})();

// Sticky button state — declarations moved above the
// _initPropelioFilterListeners IIFE so that IIFE can safely call
// _ensureStickyPropelioButton() at init time (TDZ-free).
let propelioCacheEmptyChip = null;

function _hideCacheEmptyChip() {
  if (propelioCacheEmptyChip) propelioCacheEmptyChip.classList.add("hidden");
}

function _showCacheEmptyChip() {
  if (!propelioStickyAnchor) return;
  if (!propelioCacheEmptyChip) {
    propelioCacheEmptyChip = document.createElement("div");
    propelioCacheEmptyChip.className = "propelio-cache-empty-chip hidden";
    propelioCacheEmptyChip.textContent = "Cache empty - click Deep Pull for fresh data";
    propelioStickyAnchor.appendChild(propelioCacheEmptyChip);
  }
  propelioCacheEmptyChip.classList.remove("hidden");
  setTimeout(_hideCacheEmptyChip, 6000);
}

function _setPropelioGetCompsLabel(main, subtitle = "~3 min") {
  if (!propelioStickyBtn) return;
  const mainEl = propelioStickyBtn.querySelector(".propelio-refresh-main");
  const subtitleEl = propelioStickyBtn.querySelector(".propelio-refresh-subtitle");
  if (mainEl) mainEl.textContent = main || "Deep Pull";
  if (subtitleEl) subtitleEl.textContent = subtitle;
}

async function _fetchPolygonCacheOnly() {
  if (!Array.isArray(lastPolygon) || lastPolygon.length < 3) return null;
  const savedAreaId = (typeof _currentLoadedAreaId === "string" ? _currentLoadedAreaId : "") || "";
  const reqBody = {
    polygon: lastPolygon,
    months: PROPELIO_POLYGON_MONTHS,
  };
  if (savedAreaId) reqBody.saved_area_id = savedAreaId;

  const resp = await _apiJson("/api/propelio/by-polygon?cache_only=true", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(reqBody),
  });

  if (Array.isArray(resp?.comps) && resp.comps.length > 0) {
    window._propelioLast = resp;
    _updatePropelioStatusCounts();
    applyPropelioClientFilters();
    _hideCacheEmptyChip();
  } else {
    _showCacheEmptyChip();
  }
  return resp;
}

async function _autoCacheOnDraw() {
  const suggestedName = _suggestAreaNameFromContainedParcels()
    || `Workspace ${new Date().toISOString().slice(0, 16).replace("T", " ")}`;

  try {
    await saveCurrentArea(suggestedName);
  } catch (err) {
    console.warn("[auto-cache-on-draw] auto-save failed, continuing without saved area id:", err);
  }

  // Fire-and-forget: saveCurrentArea above already set _currentLoadedAreaId.
  // The caller (draw:created) awaits _autoCacheOnDraw before calling
  // runAnalysis, so /api/analyze receives the correct area_id immediately and
  // the new cached_jobs row is born with saved_area_id populated.  Cache
  // pre-warm doesn't need to block analysis.
  void _fetchPolygonCacheOnly().catch((err) =>
    console.warn("[auto-cache-on-draw] cache lookup failed:", err)
  );
}

function _navigationGuardForActiveDeepPull(actionDescription) {
  if (!_activeDeepPullJobId) return true;
  return window.confirm(
    "A deep pull is still running on this workspace. "
    + "It will continue saving comps in the background even if you switch. "
    + `Do you want to ${actionDescription} anyway?`
  );
}

function _deriveDeepPullTargetAddress() {
  const currentWorkspace = _currentLoadedAreaId
    ? _savedAreasCache.find((a) => a.id === _currentLoadedAreaId)
    : null;
  return _lastSearchedAddress
    || _suggestAreaNameFromContainedParcels()
    || (currentWorkspace?.name || null);
}

function _ensureStickyPropelioButton() {
  if (propelioStickyBtn) return propelioStickyBtn;
  propelioStickyBtn = document.getElementById("btn-propelio-get-comps");
  if (!propelioStickyBtn) return null;
  L.DomEvent.on(propelioStickyBtn, "click", (evt) => {
    L.DomEvent.stopPropagation(evt);
    if (!lastAnalysisGeojson) {
      _showToast("Draw a polygon first", "error");
      return;
    }
    void _refreshRecentByPolygon();
  });
  return propelioStickyBtn;
}

function _removePropelioPolygonButton() {
  if (propelioStickyAnchor) {
    propelioStickyAnchor.classList.remove("visible");
  }
}

function _setPropelioPolygonButtonState({ text, disabled }) {
  if (!propelioStickyBtn) return;
  if (typeof text === "string") _setPropelioGetCompsLabel(text, "~3 min");
  if (typeof disabled === "boolean") propelioStickyBtn.disabled = disabled;
  propelioStickyBtn.classList.toggle("is-running", Boolean(disabled));
}

function _polygonButtonLatLng(latlngs) {
  const pts = Array.isArray(latlngs) ? latlngs : [];
  if (!pts.length) return null;

  let north = -Infinity;
  let south = Infinity;
  let east = -Infinity;
  let west = Infinity;

  pts.forEach((p) => {
    const lat = Number(p?.lat);
    const lng = Number(p?.lng);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) return;
    if (lat > north) north = lat;
    if (lat < south) south = lat;
    if (lng > east) east = lng;
    if (lng < west) west = lng;
  });

  if (!Number.isFinite(north) || !Number.isFinite(south) || !Number.isFinite(east) || !Number.isFinite(west)) {
    return null;
  }

  const centerLng = (east + west) / 2;
  const latPad = Math.max((north - south) * 0.12, 0.00035);
  return L.latLng(Math.min(89.9999, north + latPad), centerLng);
}

async function _pullPropelioByPolygon() {
  if (propelioPolygonPullInFlight || !Array.isArray(lastPolygon) || lastPolygon.length < 3) return;
  if (_activeDeepPullJobId) return;

  const targetAddress = _deriveDeepPullTargetAddress();
  if (!targetAddress) {
    _showDeepPullBanner("Search for an address or save a parcel first.");
    setTimeout(_hideDeepPullBanner, 4000);
    return;
  }

  propelioPolygonPullInFlight = true;
  _setPropelioPolygonButtonState({ text: "Deep Pull", disabled: true });
  _showDeepPullBanner("Pass 0/6, queued - don't refresh, comps are saving in the background...");
  try {
    const resp = await _apiJson("/api/propelio/deep-pull/start", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({
        target_address: targetAddress,
        saved_area_id: _currentLoadedAreaId || null,
      }),
    });
    _activeDeepPullJobId = resp.job_id;
    if (_deepPullPollTimer) clearInterval(_deepPullPollTimer);
    _deepPullPollTimer = setInterval(_pollDeepPullStatus, 5000);
  } catch (err) {
    console.error("[propelio] deep-pull start failed:", err);
    _showDeepPullBanner("Deep pull failed to start (see console)");
    setTimeout(_hideDeepPullBanner, 4000);
  } finally {
    propelioPolygonPullInFlight = false;
    if (!_activeDeepPullJobId) {
      _setPropelioPolygonButtonState({ text: "Deep Pull", disabled: false });
    }
  }
}

async function _refreshRecentByPolygon() {
  // "Refresh Recent" — a tighter, recency-focused deep-pull that catches
  // stragglers (recent pendings, just-listed actives) the broad 24mo
  // sweep missed due to Propelio's 100-cap per CMA call. Same job
  // infrastructure as Get Comps, different pass config.
  if (propelioPolygonPullInFlight || !Array.isArray(lastPolygon) || lastPolygon.length < 3) return;
  if (_activeDeepPullJobId) return;

  const targetAddress = _deriveDeepPullTargetAddress();
  if (!targetAddress) {
    _showDeepPullBanner("Search for an address or save a parcel first.");
    setTimeout(_hideDeepPullBanner, 4000);
    return;
  }

  propelioPolygonPullInFlight = true;
  _setPropelioPolygonButtonState({ text: "Deep Pull", disabled: true });
  _showDeepPullBanner("Quick sweep · queued - 3 passes (1mo, 2mo, 3mo), ~2-3 min");
  try {
    const resp = await _apiJson("/api/propelio/refresh-recent/start", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({
        target_address: targetAddress,
        saved_area_id: _currentLoadedAreaId || null,
      }),
    });
    _activeDeepPullJobId = resp.job_id;
    if (_deepPullPollTimer) clearInterval(_deepPullPollTimer);
    _deepPullPollTimer = setInterval(_pollDeepPullStatus, 5000);
  } catch (err) {
    console.error("[propelio] refresh-recent start failed:", err);
    _showDeepPullBanner("Quick sweep failed: see console");
    setTimeout(_hideDeepPullBanner, 4000);
  } finally {
    propelioPolygonPullInFlight = false;
    if (!_activeDeepPullJobId) {
      _setPropelioPolygonButtonState({ text: "Deep Pull", disabled: false });
    }
  }
}

function _showPropelioPolygonButton(_latlngs) {
  // _latlngs no longer needed — button is sticky bottom-center, not
  // anchored to polygon geometry. Argument retained so existing call
  // sites don't have to change.
  _ensureStickyPropelioButton();
  if (!propelioStickyBtn) return;
  _setPropelioGetCompsLabel("Deep Pull");
  propelioStickyBtn.disabled = false;
  propelioStickyBtn.classList.remove("is-running");
  if (propelioStickyAnchor) {
    propelioStickyAnchor.classList.add("visible");
  }
}

// TODO(comps-button): intentionally retained. As of 2026-05-10 this
// function has ZERO callers — the two address-search auto-pull sites
// were removed by design (chunk 1 of the 2026-05-10 UI initiative).
// The planned explicit "Comps" button next to the address search bar
// will reconnect this code path. See memory:
//   project_propelio_comp_harvest.md  (two-button search split + deep-pull)
// If you are reading this AFTER the Comps button has shipped AND this
// function is still unreferenced, then it really is dead — remove it
// and the matching CMA-chip dormancy note above. Otherwise: leave it.
async function firePropelioFetch(addressString) {
  if (!addressString || typeof addressString !== "string") return;
  propelioCompLayer.clearLayers();
  propelioCmaChip.hide();
  try {
    const resp = await fetch(`/api/propelio/by-address?address=${encodeURIComponent(addressString)}`);
    if (!resp.ok) {
      console.warn("[propelio] fetch failed:", resp.status);
      return;
    }
    const data = await resp.json();
    window._propelioLast = data;
    _updatePropelioStatusCounts();
    // applyPropelioClientFilters renders + sets chip + updates count chip
    applyPropelioClientFilters();
    console.info(
      `[propelio] received ${Array.isArray(data?.comps) ? data.comps.length : 0} address comp(s)${data.cached ? " (cached)" : ""}`
    );
  } catch (e) {
    console.error("[propelio] fetch error:", e);
  }
}

const AddressSearch = L.Control.extend({
  options: { position: "topright" },
  onAdd() {
    const container = L.DomUtil.create("div", "address-search");
    L.DomEvent.disableClickPropagation(container);
    L.DomEvent.disableScrollPropagation(container);

    const row = L.DomUtil.create("div", "address-search-row", container);

    const input = L.DomUtil.create("input", "address-search-input", row);
    input.type = "text";
    input.placeholder = "Search address or place...";
    input.setAttribute("autocomplete", "off");

    const btn = L.DomUtil.create("button", "address-search-btn", row);
    btn.textContent = "Go";

    const suggestList = L.DomUtil.create("div", "address-suggest-list hidden", container);
    let suggestItems = [];
    let activeSuggestIndex = -1;
    let suggestTimer = null;
    let suggestAbort = null;
    let suggestRequestId = 0;
    const suggestCache = new Map();
    const SUGGEST_CACHE_TTL_MS = 30000;

    const clearSuggestList = () => {
      suggestItems = [];
      activeSuggestIndex = -1;
      suggestList.innerHTML = "";
      delete suggestList.dataset.loadingRequestId;
      suggestList.classList.add("hidden");
    };

    const renderSuggestLoading = (requestId) => {
      suggestList.dataset.loadingRequestId = String(requestId);
      suggestList.innerHTML = '<div class="address-suggest-loading">Searching Texas addresses...</div>';
      suggestList.classList.remove("hidden");
    };

    const renderSuggestList = () => {
      suggestList.innerHTML = "";
      if (!suggestItems.length) {
        suggestList.classList.add("hidden");
        return;
      }

      suggestItems.forEach((item, idx) => {
        const rowEl = L.DomUtil.create("button", "address-suggest-item", suggestList);
        rowEl.type = "button";
        rowEl.setAttribute("aria-selected", idx === activeSuggestIndex ? "true" : "false");
        rowEl.innerHTML = `
          <span class="address-suggest-main">${item.address}</span>
          <span class="address-suggest-sub">${item.city ? `${item.city}, ` : ""}TX · ${item.county.toUpperCase()}</span>
        `;
        L.DomEvent.on(rowEl, "mousedown", (e) => {
          e.preventDefault();
          selectSuggestion(idx);
        });
      });
      suggestList.classList.remove("hidden");
    };

    const highlightSearchResult = (latlng) => {
      window._clearSearchHighlight?.();
      // Also clear the click-to-select purple outline (a separate mechanism
      // from _searchHighlight). Without this, doing an address search after
      // selecting a parcel by click leaves BOTH purple outlines on the map.
      // KK reported 2026-06-03 PM. Map-click handler at line ~12235 already
      // clears _searchHighlight on the other side of this asymmetry; this
      // line restores symmetry the other way.
      try { _clearSelectedOutline(); } catch (_) {}
      // Mike report 2026-06-06: a parcel that was selected by click and
      // THEN the user does an address search → the previous parcel's
      // popup state (_activeParcelPopupState) was never reset, so the
      // user couldn't unselect it (clicking the map elsewhere reopened
      // the OLD parcel's popup because the state still held it). The
      // outline got cleared above; the in-memory popup state needs the
      // same treatment so the map.click reset path works.
      _activeParcelPopupState = null;
      if (window._searchMoveEndHandler) map.off("moveend", window._searchMoveEndHandler);

      window._clearSearchHighlight = () => {
        if (window._searchHighlight) {
          window._searchHighlight.remove();
          window._searchHighlight = null;
        }
      };

      window._searchMoveEndHandler = () => {
        window._searchMoveEndHandler = null;
        (async () => {
          const [slat, slng] = latlng;
          let highlightLayer = null;

          // Direct DB lookup — works for any parcel size, no tile dependency.
          // Style matches the click-to-select purple outline (chunk-5 selection
          // visual), so address-search-selected parcels and mouse-click-selected
          // parcels look identical. Same z-index pane + drop-shadow glow.
          try {
            const resp = await fetch(`/api/parcel/near?lat=${slat}&lng=${slng}`);
            if (resp.ok) {
              const detail = await resp.json();
              const geom = detail.geometry;
              if (geom && (geom.type === "Polygon" || geom.type === "MultiPolygon")) {
                highlightLayer = L.geoJSON(detail, {
                  pane: "selectedOutlinePane",
                  className: "selected-outline-glow",
                  style: { color: "#a855f7", weight: 5, fill: false, interactive: false },
                  interactive: false,
                }).addTo(map);
              }
            }
          } catch (e) {
            console.warn("Search footprint lookup failed", e);
          }

          if (!highlightLayer) {
            // No parcel polygon available — drop a purple ring at the lat/lng
            // as a fallback. Same visual family as the polygon outline above.
            highlightLayer = L.circleMarker(latlng, {
              pane: "selectedOutlinePane",
              radius: 14,
              color: "#a855f7",
              weight: 5,
              fillColor: "#a855f7",
              fillOpacity: 0.08,
              interactive: false,
            }).addTo(map);
          }
          window._searchHighlight = highlightLayer;
        })();
      };

      map.once("moveend", window._searchMoveEndHandler);
      map.flyTo(latlng, 17);
      input.value = "";
      clearSuggestList();
    };

    const selectSuggestion = (idx) => {
      const item = suggestItems[idx];
      if (!item) return;
      _lastSearchedAddress = item.address;
      highlightSearchResult([Number(item.lat), Number(item.lng)]);
    };

    const fetchSuggestions = async () => {
      const q = input.value.trim();
      if (q.length < 3) {
        clearSuggestList();
        return;
      }

      const normalized = q.toUpperCase();
      const now = Date.now();
      const cached = suggestCache.get(normalized);
      if (cached && now - cached.ts < SUGGEST_CACHE_TTL_MS) {
        suggestItems = Array.isArray(cached.items) ? cached.items : [];
        activeSuggestIndex = suggestItems.length ? 0 : -1;
        renderSuggestList();
        return;
      }

      const requestId = ++suggestRequestId;
      renderSuggestLoading(requestId);
      let silentAbort = false;

      if (suggestAbort) suggestAbort.abort();
      suggestAbort = new AbortController();

      try {
        const resp = await fetch(`/api/address/suggest?q=${encodeURIComponent(q)}&limit=8`, {
          signal: suggestAbort.signal,
        });
        if (!resp.ok) {
          if (requestId === suggestRequestId) suggestItems = [];
          return;
        }
        const data = await resp.json();
        if (requestId !== suggestRequestId) {
          return;
        }
        suggestItems = Array.isArray(data.items) ? data.items : [];
        suggestCache.set(normalized, { ts: now, items: suggestItems.slice(0, 8) });
        if (suggestCache.size > 120) {
          const firstKey = suggestCache.keys().next().value;
          if (firstKey) suggestCache.delete(firstKey);
        }
      } catch (e) {
        if (e?.name === "AbortError") {
          silentAbort = true;
          return;
        }
        if (requestId === suggestRequestId) suggestItems = [];
      } finally {
        if (silentAbort) return;
        if (requestId !== suggestRequestId) {
          const stillOwnsLoading = suggestList.dataset.loadingRequestId === String(requestId);
          if (stillOwnsLoading) clearSuggestList();
          return;
        }
        activeSuggestIndex = suggestItems.length ? 0 : -1;
        if (suggestItems.length) {
          renderSuggestList();
        } else {
          clearSuggestList();
        }
      }
    };

    const doSearch = async () => {
      const q = input.value.trim();
      if (!q) return;
      _lastSearchedAddress = q;
      clearSuggestList();
      btn.disabled = true;
      btn.textContent = "…";
      try {
        // bounded=1 + Texas viewbox keeps results inside Texas.
        const resp = await fetch(
          `https://nominatim.openstreetmap.org/search?q=${encodeURIComponent(q)}&format=json&limit=1&countrycodes=us&viewbox=-106.65,36.5,-93.51,25.84&bounded=1`,
          { headers: { "Accept-Language": "en" } }
        );
        const results = await resp.json();
        if (!results.length) {
          btn.textContent = "Not found";
          setTimeout(() => { btn.textContent = "Go"; btn.disabled = false; }, 2000);
          return;
        }
        const { lat, lon } = results[0];
        const latlng = [parseFloat(lat), parseFloat(lon)];
        highlightSearchResult(latlng);
      } catch {
        btn.textContent = "Error";
        setTimeout(() => { btn.textContent = "Go"; btn.disabled = false; }, 2000);
        return;
      }
      btn.textContent = "Go";
      btn.disabled = false;
    };

    L.DomEvent.on(btn, "click", doSearch);
    L.DomEvent.on(input, "input", () => {
      if (suggestTimer) clearTimeout(suggestTimer);
      suggestTimer = setTimeout(fetchSuggestions, 220);
    });

    L.DomEvent.on(input, "keydown", (e) => {
      if (e.key === "ArrowDown" && suggestItems.length) {
        e.preventDefault();
        activeSuggestIndex = (activeSuggestIndex + 1) % suggestItems.length;
        renderSuggestList();
        return;
      }
      if (e.key === "ArrowUp" && suggestItems.length) {
        e.preventDefault();
        activeSuggestIndex = (activeSuggestIndex - 1 + suggestItems.length) % suggestItems.length;
        renderSuggestList();
        return;
      }
      if (e.key === "Escape") {
        clearSuggestList();
        return;
      }
      if (e.key === "Enter") {
        if (suggestItems.length && activeSuggestIndex >= 0) {
          e.preventDefault();
          selectSuggestion(activeSuggestIndex);
        } else {
          doSearch();
        }
      }
    });

    L.DomEvent.on(input, "blur", () => {
      setTimeout(clearSuggestList, 120);
    });

    return container;
  },
});
new AddressSearch().addTo(map);
const appShell = document.querySelector(".app-shell");
const sidebarToggleBtn = document.getElementById("sidebar-toggle");
const drawHelper = document.getElementById("draw-helper");

function initSidebarCollapsibles() {
  let savedStates = {};
  try {
    const raw = localStorage.getItem(SIDEBAR_SECTION_STATE_STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object") savedStates = parsed;
    }
  } catch (_) {}

  document.querySelectorAll(".section-toggle[data-target]").forEach((btn) => {
    const targetId = btn.dataset.target;
    const body = targetId ? document.getElementById(targetId) : null;
    if (!body) return;

    if (Object.prototype.hasOwnProperty.call(savedStates, targetId)) {
      const expanded = Boolean(savedStates[targetId]);
      btn.setAttribute("aria-expanded", String(expanded));
      body.classList.toggle("hidden", !expanded);
    }

    btn.addEventListener("click", () => {
      const expanded = btn.getAttribute("aria-expanded") !== "false";
      const nextExpanded = !expanded;
      btn.setAttribute("aria-expanded", String(nextExpanded));
      body.classList.toggle("hidden", !nextExpanded);
      try {
        savedStates[targetId] = nextExpanded;
        localStorage.setItem(SIDEBAR_SECTION_STATE_STORAGE_KEY, JSON.stringify(savedStates));
      } catch (_) {}
    });
  });
}

function initClickModeToggle() {
  // 2026-05-20: flipped default to "stay" per KK — analysts found the auto-
  // zoom-on-every-click disorienting. The toolbar ZOOM button is the user-
  // facing toggle; mid-session toggles work via currentClickMode but do not
  // persist across refreshes (always re-init to "stay" on page load).
  // (Original Mike feedback was jump-default; superseded by this 2026-05-20
  // change after real-world workflow review.)
  currentClickMode = "stay";

  // Set up button click handlers (legacy saved-areas-section toggle —
  // markup may have been removed in the 2026-05-11 toolbar relocation,
  // so these querySelectors might return null; that's fine, the toolbar
  // ZOOM button updated via updateClickModeButtonState is the active UI).
  const jumpBtn = document.querySelector(".click-mode-btn.jump-mode");
  const stayBtn = document.querySelector(".click-mode-btn.stay-mode");
  if (jumpBtn) jumpBtn.addEventListener("click", () => setClickMode("jump"));
  if (stayBtn) stayBtn.addEventListener("click", () => setClickMode("stay"));

  updateClickModeButtonState();
}

// Bidirectional sync between the toolbar OAC button and the Map Filters
// prop-outside-area checkbox. Run once at startup after the checkbox
// + toolbar button both exist in the DOM.
function initOACToggleSync() {
  const checkbox = document.getElementById("prop-outside-area");
  if (!checkbox) return;
  checkbox.addEventListener("change", _updateOACButtonState);
  _updateOACButtonState();
}

function getPolygonDrawHandler() {
  return drawControl?._toolbars?.draw?._modes?.polygon?.handler || null;
}

function isDrawInputTarget(target) {
  if (!target) return false;
  const tag = (target.tagName || "").toLowerCase();
  return tag === "input" || tag === "textarea" || target.isContentEditable;
}

function setSidebarCollapsed(collapsed) {
  appShell.classList.toggle("sidebar-collapsed", collapsed);
  sidebarToggleBtn.setAttribute("aria-expanded", String(!collapsed));
  setTimeout(() => map.invalidateSize(), 250);
}

sidebarToggleBtn.addEventListener("click", () => {
  const collapsed = appShell.classList.contains("sidebar-collapsed");
  setSidebarCollapsed(!collapsed);
});

initSidebarCollapsibles();
initClickModeToggle();
initOACToggleSync();

(function _initSavedListSearchInputs() {
  const areasInput = document.getElementById("saved-areas-search");
  const targetsInput = document.getElementById("saved-parcels-search");
  if (areasInput) {
    areasInput.addEventListener("input", () => {
      _savedAreasSearchQuery = areasInput.value;
      renderSavedAreasList();
    });
  }
  if (targetsInput) {
    targetsInput.addEventListener("input", () => {
      _savedTargetsSearchQuery = targetsInput.value;
      renderSavedAreasList();
    });
  }
})();

function applyMapVisibilityFilters() {
  const previousSoldLayerVisible = soldLayerVisible;

  PARCEL_LAYER_KEYS.forEach((key) => {
    const layer = parcelTypeLayers[key];
    if (!layer) return;
    if (Boolean(filterState[key])) {
      if (!markerLayer.hasLayer(layer)) markerLayer.addLayer(layer);
    } else if (markerLayer.hasLayer(layer)) {
      markerLayer.removeLayer(layer);
    }
  });

  soldLayerVisible = Boolean(filterState.sold);
  if (soldLayerVisible) soldLayer.addTo(map);
  else map.removeLayer(soldLayer);

  applyAndRenderSoldFilters();

  // Sold outlines are embedded in parcel styling, so sold toggle changes need
  // a parcel rerender to immediately reflect outline visibility state.
  const soldToggleChanged = previousSoldLayerVisible !== soldLayerVisible;
  if (soldToggleChanged && lastAnalysisGeojson && Array.isArray(lastAnalysisGeojson.features) && lastAnalysisGeojson.features.length <= BROWSE_ONLY_THRESHOLD) {
    if (viewportRenderMode) {
      renderViewportFeatures();
    } else {
      const markers = renderFeatures(lastAnalysisGeojson);
      if (lastAnalysisCounts) renderSidebar(lastAnalysisCounts, markers);
    }
  }

  _updateMergedSidebarCounts();
  updateSoldStatusText();
}

// Light-touch count refresh for the merged Map Filters counts. Avoids the full
// renderSidebar (which re-renders the sold panel) when all we need is the
// number next to each checkbox.
function _updateMergedSidebarCounts() {
  if (!Array.isArray(allAnalysisFeatures) || !allAnalysisFeatures.length) return;
  const visibleCounts = getVisibleFeatureCounts(allAnalysisFeatures, { ignoreBucketToggles: true });
  const soldCount = Array.isArray(lastSoldPanelPoints) && lastSoldPanelPoints.length
    ? lastSoldPanelPoints.length
    : (Array.isArray(allSoldPointsRef) ? allSoldPointsRef.length : 0);
  const rows = [
    ["active", visibleCounts.active],
    ["sold", soldCount],
    ["contact_status", visibleCounts.contact_status],
    ["off_market", visibleCounts.off_market],
    ["vacant", visibleCounts.vacant],
    ["multifamily", visibleCounts.multifamily],
    ["duplexes", visibleCounts.duplexes],
    ["commercial", visibleCounts.commercial],
    ["exempt", visibleCounts.exempt],
  ];
  rows.forEach(([key, val]) => {
    const el = document.getElementById(`filter-count-${key}`);
    if (el) el.textContent = String(Number(val) || 0);
  });
}

loadFilters();
syncFilterInputs();

function _applyNumericFilters() {
  _readNumericInputs();
  if (!lastAnalysisGeojson) return;
  const markers = viewportRenderMode
    ? renderViewportFeatures()
    : renderFeatures(lastAnalysisGeojson);
  const counts = getVisibleFeatureCounts(lastAnalysisGeojson.features || [], { ignoreBucketToggles: true });
  if (lastAnalysisCounts) renderSidebar(counts, markers || {});
  // Property Filters now also gate Propelio comps (compPassesPropelioFilters
  // reads numericFilters). Keep the comp overlay + list in sync when the
  // user edits a Property Filter input.
  applyPropelioClientFilters();
  _refreshLoadedAreaUi();
  // v1 §2.1 — auto-save filter_state after numeric-filter apply.
  _filterSaveQueueSave();
}

NUMERIC_FILTER_INPUTS.forEach(({ id }) => {
  document.getElementById(id)?.addEventListener("input", () => {
    bumpUndoPillVersion();
    _applyNumericFilters();
  });
});

["nf-val-min", "nf-val-max"].forEach((id) => {
  const inputEl = document.getElementById(id);
  if (!inputEl) return;
  const normalize = () => {
    const raw = String(inputEl.value || "").trim();
    if (!raw) {
      inputEl.value = "";
      return;
    }
    const parsed = parseShorthand(raw);
    inputEl.value = parsed == null ? "" : formatNumberWithCommas(parsed);
  };
  inputEl.addEventListener("blur", () => {
    bumpUndoPillVersion();
    normalize();
    _applyNumericFilters();
    void _filterSaveFlushPending();
  });
  inputEl.addEventListener("change", () => {
    bumpUndoPillVersion();
    normalize();
    _applyNumericFilters();
  });
});

document.getElementById("btn-numeric-reset")?.addEventListener("click", () => {
  bumpUndoPillVersion();
  _clearNumericInputs();
  _applyNumericFilters();
});

Object.entries(FILTER_INPUT_IDS).forEach(([key, id]) => {
  const input = document.getElementById(id);
  if (!input) return;
  input.addEventListener("change", () => {
    bumpUndoPillVersion();
    filterState[key] = Boolean(input.checked);
    saveFilters();
    applyMapVisibilityFilters();
    applyPropelioClientFilters();
    _refreshLoadedAreaUi();
    // v1 §2.1 — auto-save filter_state after checkbox toggle.
    _filterSaveQueueSave();
    if (key === "contact_status") {
      _rebuildOutreachOverlays();
    }
  });
});

document.getElementById("btn-filters-reset")?.addEventListener("click", () => {
  bumpUndoPillVersion();
  filterState = { ...DEFAULT_FILTERS };
  saveFilters();
  syncFilterInputs();
  applyMapVisibilityFilters();
  applyPropelioClientFilters();
  _refreshLoadedAreaUi();
  // v1 §2.1 — auto-save filter_state after filter reset.
  _filterSaveQueueSave();
  _rebuildOutreachOverlays();
});

// Comp Filters: read from nf-comp-* inputs and apply
function _readCompNumericInputs() {
  const compInputs = [
    { id: "nf-comp-lot-min", key: "lot_sqft_min" },
    { id: "nf-comp-lot-max", key: "lot_sqft_max" },
    { id: "nf-comp-val-min", key: "appr_val_min" },
    { id: "nf-comp-val-max", key: "appr_val_max" },
    { id: "nf-comp-yr-min", key: "yr_built_min" },
    { id: "nf-comp-yr-max", key: "yr_built_max" },
    { id: "nf-comp-sqft-min", key: "sqft_min" },
    { id: "nf-comp-sqft-max", key: "sqft_max" },
  ];
  compInputs.forEach(({ id, key }) => {
    const el = document.getElementById(id);
    const raw = el ? el.value.trim() : "";
    if (raw === "") {
      compNumericFilters[key] = null;
      return;
    }
    if (key === "appr_val_min" || key === "appr_val_max") {
      compNumericFilters[key] = parseShorthand(raw);
      return;
    }
    if (key === "lot_sqft_min" || key === "lot_sqft_max") {
      const acres = Number(raw);
      compNumericFilters[key] = Number.isFinite(acres) ? acres * 43_560 : null;
      return;
    }
    const n = Number(raw);
    compNumericFilters[key] = Number.isFinite(n) ? n : null;
  });
}

function _applyCompNumericFilters() {
  _readCompNumericInputs();
  if (!lastAnalysisGeojson) return;
  // Recompute the sold-points sidebar list so it reflects the new comp filters.
  // _soldPointPassesFilter now reads compNumericFilters for lot/sqft/year-built,
  // but lastSoldPanelPoints would otherwise stay stale until a separate sold-comps
  // input change. Run the filter pass before rendering so renderSidebar ->
  // renderSoldCompsPanel sees the up-to-date list.
  lastSoldPanelPoints = allSoldPointsRef.filter((p) =>
    _soldPointPassesFilter(p, soldCompsFilter)
  );
  const markers = viewportRenderMode
    ? renderViewportFeatures()
    : renderFeatures(lastAnalysisGeojson);
  const counts = getVisibleFeatureCounts(lastAnalysisGeojson.features || []);
  if (lastAnalysisCounts) renderSidebar(counts, markers || {});
  _refreshLoadedAreaUi();
  // v1 §2.1 — auto-save filter_state after comp-numeric apply.
  _filterSaveQueueSave();
}

// Comp numeric filter event listeners are wired INSIDE renderSoldCompsPanel
// (using blur + change) since the inputs are part of dynamically rendered
// HTML. Don't re-attach at module level — the renderSoldCompsPanel-internal
// setup is correct and complete. Module-level attachment with an `input`
// event would also re-introduce the focus-stealing-mid-keystroke regression
// we just fixed for the lot-size filter.

applyMapVisibilityFilters();

function isRedfinVisualActive(feature) {
  return Boolean(feature?.properties?.on_redfin);
}

function getColor(feature) {
  if (isRedfinVisualActive(feature)) return COLORS.active;
  return COLORS[feature.properties.prop_type] || COLORS.exempt;
}

function getBorderColor(feature) {
  if (isRedfinVisualActive(feature)) return BORDER_COLORS.active;
  return BORDER_COLORS[feature.properties.prop_type] || BORDER_COLORS.exempt;
}

function getStatusLabel(feature) {
  if (isRedfinVisualActive(feature)) return "ACTIVE LISTING";
  if (feature.properties.prop_type === "multifamily") return "MULTIFAMILY";
  if (feature.properties.prop_type === "duplexes") return "DUPLEXES";
  if (feature.properties.prop_type === "vacant") return "VACANT LOT";
  if (feature.properties.prop_type === "commercial") return "COMMERCIAL";
  if (feature.properties.prop_type === "exempt") return "EXEMPT (church/school/nonprofit)";
  return "OFF MARKET";
}

function normalizeVerificationValue(value) {
  const text = String(value || "").trim().toLowerCase();
  if (text === "yes") return "Yes";
  if (text === "no") return "No";
  return "";
}

function verificationDisplay(value) {
  const normalized = normalizeVerificationValue(value);
  return normalized || "Unverified";
}

function isCondoParcel(properties) {
  const stateCode = String(properties?.state_code || "").toLowerCase();
  return stateCode.includes("condominium");
}

function geometryKey(geometry) {
  if (!geometry) return "";
  try {
    return JSON.stringify(geometry);
  } catch {
    return "";
  }
}

function _panelDisplayValue(value) {
  // 2026-05-21 fix: distinguish missing data (null/undefined/"N/A" → "N/A")
  // from explicit zero (0 → "0"). The old `value &&` short-circuit treated
  // 0 as falsy, which was wrong for fields like half_baths where 0 is real
  // information (parcel has zero half-baths, not unknown).
  if (value === null || value === undefined || value === "" || value === "N/A") {
    return "N/A";
  }
  return value;
}

function _panelFlagDisplay(value) {
  // Canonical T/F/empty (ingest-normalized by _normalize_flag in build_db /
  // dcad ingests) → Yes/No/N/A for the parcel detail panel display.
  const t = String(value || "").trim().toUpperCase();
  if (t === "T" || t === "Y" || t === "TRUE" || t === "YES" || t === "1") return "Yes";
  if (t === "F" || t === "N" || t === "FALSE" || t === "NO" || t === "0") return "No";
  return "N/A";
}

function _panelTitleCaseDisplay(value) {
  // Display helper for DCAD's all-caps descriptive fields (FOUNDATION_TYP_DESC,
  // ROOF_MAT_DESC, etc.). Title-cases per word so they read cleanly in the
  // panel ("PIER AND BEAM" → "Pier And Beam"). Preserves "N/A".
  const text = String(value || "").trim();
  if (!text || text === "N/A") return "N/A";
  return text.split(/\s+/).map((w) =>
    w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()
  ).join(" ");
}

// Build the "Neighborhood" popup cell content. Top line is always the
// subdivision name (the existing short label, level with the
// "Neighborhood" cell label). When the parcel feature carries a full
// `legal_description` from build_feature (joined from legal1..legal5
// across all four counties, with DCAD admin markers trimmed off in
// _trim_dcad_legal_admin_tail) and that string carries a tail beyond
// the subdivision name, append the legal-description tail as a second
// line under the subdivision — gives Mike the Block/Lot detail he asked
// for without losing the quick-scan subdivision label up top. Falls
// back to subdivision-only when legal_description is missing.
function _neighborhoodCellHtml(subdivision, legalDescription) {
  const sub = String(subdivision || "").trim();
  const legal = String(legalDescription || "").trim();
  if (!sub) return legal ? _propelioEscape(legal) : "";
  const subEsc = _propelioEscape(sub);
  if (!legal || legal.toUpperCase() === sub.toUpperCase()) return subEsc;
  // Strip the subdivision prefix from the legal description (case-
  // insensitive). When the legal doesn't START with the subdivision
  // (rare — DCAD's joined legal can occasionally lead with BLK/LOT
  // before the subdivision name), show the full legal as the tail so
  // no info is hidden.
  let tail = legal;
  if (legal.toUpperCase().startsWith(sub.toUpperCase())) {
    tail = legal.slice(sub.length).trim();
  }
  if (!tail) return subEsc;
  return `${subEsc}<br><span class="parcel-popup-legal-tail">${_propelioEscape(tail)}</span>`;
}


// Total Value display with prior-year provenance tag. When build_feature
// (api/counties/dcad.py) emits a non-empty total_value_source flag on the
// parcel properties — currently Collin-only via _normalize_collin_row's
// cert_total_value fallback — append " (prior year YYYY)" to the displayed
// value so analysts know the number is from last certification, not this
// year's roll. Keeps p.tot_val itself numeric-clean for passesNumericFilters.
function _totalValueDisplay(p) {
  const raw = p?.tot_val;
  if (!raw || raw === "N/A") return "N/A";
  const source = String(p?.total_value_source || "").trim();
  if (!source) return raw;
  const yearMatch = source.startsWith("prior_year_cert_") ? source.slice("prior_year_cert_".length) : "";
  const suffix = yearMatch ? `(prior year ${yearMatch})` : "(prior year)";
  return `${raw} <span class="prior-year-tag" style="opacity:0.7;font-style:italic;font-size:11px;">${suffix}</span>`;
}

function _buildParcelDetailTableRow(label, value) {
  return `<tr><td class="popup-label">${label}</td><td class="popup-val">${value || "N/A"}</td></tr>`;
}

function _buildParcelDeltaMeta(p, label, numeric) {
  if (!Number.isFinite(numeric) || numeric <= 0) return null;
  const dcadRaw = String(p?.tot_val || "").replace(/[^0-9]/g, "");
  const dcadNum = dcadRaw ? parseInt(dcadRaw, 10) : NaN;
  if (!Number.isFinite(dcadNum) || dcadNum <= 0) return null;
  const delta = Math.round(numeric) - dcadNum;
  const pct = ((delta / dcadNum) * 100).toFixed(1);
  const sign = delta >= 0 ? "+" : "";
  return {
    label,
    text: `${sign}$${Math.abs(delta).toLocaleString()} (${sign}${pct}%)`,
    color: delta >= 0 ? "#16a34a" : "#dc2626",
  };
}

function _getParcelPanelCompDetails(comp) {
  if (!comp) return null;
  const fmtPrice = (n) => Number.isFinite(n) ? `$${Math.round(n).toLocaleString()}` : "—";
  const fmtNum = (n) => Number.isFinite(n) ? Number(n).toLocaleString() : null;
  const fmtDate = (s) => {
    if (!s) return null;
    const d = new Date(s);
    return Number.isFinite(d.getTime()) ? d.toISOString().slice(0, 10) : null;
  };

  const ex = comp?.extra || {};
  const raw = comp?.extra?.raw || {};
  const status = String(comp?.status || "");
  const isSold = status === "sold";
  const sqft = fmtNum(comp?.sqft);
  const lot = fmtNum(comp?.lot_size);
  const year = Number.isFinite(comp?.year_built) ? comp.year_built : null;
  const beds = ex.beds;
  const baths = ex.baths;
  const bathsFull = ex.baths_full;
  const bathsHalf = ex.baths_half;
  const garage = ex.garage;
  const dom = ex.dom;
  const listPrice = Number(ex.list_price);
  const closeDate = fmtDate(ex.close_date);
  const modifiedTs = fmtDate(ex.modified_timestamp);
  const propertyType = ex.property_type;
  const mls = ex.mls || "";
  const source = ex.source || "";
  const remarks = String(ex.remarks || raw.remarks || "").trim();
  const schools = {
    elementary: raw.elementary_school,
    middle: raw.middle_school || raw.junior_high_school || raw.intermediate_school,
    high: raw.high_school || raw.senior_high_school,
  };
  const listingAgent = {
    name: raw.listing_agent_name,
    phone: raw.listing_agent_phone,
    email: raw.listing_agent_email,
    officeName: raw.listing_office_name,
    officePhone: raw.listing_office_phone,
  };
  const buyerAgent = {
    name: raw.buyer_agent_name,
    phone: raw.buyer_agent_phone,
    email: raw.buyer_agent_email,
    officeName: raw.buyer_office_name,
    officePhone: raw.buyer_office_phone,
  };
  const seenPhotoUrls = new Set();
  const photos = Array.isArray(raw.photos)
    ? raw.photos.filter((photo) => {
        const url = typeof photo?.url === "string" ? photo.url.trim() : "";
        if (!url || seenPhotoUrls.has(url)) return false;
        seenPhotoUrls.add(url);
        return true;
      })
    : [];
  const photoCountValue = Number(raw.photo_count);
  const photoCount = photos.length || (Number.isFinite(photoCountValue) ? photoCountValue : 0);

  const dims = [];
  if (sqft) dims.push(`${sqft} sqft`);
  if (lot) dims.push(`${lot} sqft lot`);
  if (year) dims.push(`built ${year}`);

  const bbLine = [];
  if (beds != null) bbLine.push(`${beds}bd`);
  if (Number.isFinite(bathsFull) || Number.isFinite(bathsHalf)) {
    const fullStr = Number.isFinite(bathsFull) ? `${bathsFull}` : "0";
    const halfStr = Number.isFinite(bathsHalf) && bathsHalf > 0 ? ` + ${bathsHalf}½` : "";
    bbLine.push(`${fullStr}ba${halfStr}`);
  } else if (baths != null) {
    bbLine.push(`${baths}ba`);
  }
  if (Number.isFinite(garage) && garage > 0) bbLine.push(`${garage}-car gar`);

  let listVsCloseText = "";
  if (isSold && Number.isFinite(listPrice) && Number.isFinite(comp?.price) && listPrice > 0 && Math.abs(listPrice - comp.price) > 1) {
    const delta = comp.price - listPrice;
    const deltaPct = Math.round((delta / listPrice) * 100);
    const sign = delta > 0 ? "+" : "";
    listVsCloseText = `List was ${fmtPrice(listPrice)} (${sign}${deltaPct}% close vs list)`;
  } else if (!isSold && Number.isFinite(listPrice) && listPrice > 0 && Number(comp?.price) !== listPrice) {
    listVsCloseText = `List: ${fmtPrice(listPrice)}`;
  }

  const soldMeta = [];
  if (isSold && closeDate) soldMeta.push(`closed ${closeDate}`);
  if (Number.isFinite(dom)) soldMeta.push(`DOM ${dom}`);

  const idLine = [];
  if (mls) idLine.push(`MLS ${mls}`);
  if (propertyType) idLine.push(propertyType);

  return {
    status,
    priceText: fmtPrice(comp?.price),
    soldMetaText: soldMeta.join(" · "),
    dimsText: dims.join(" · "),
    bbText: bbLine.join(" · "),
    idText: idLine.join(" · "),
    source,
    modifiedTs,
    listVsCloseText,
    remarks,
    schools,
    listingAgent,
    buyerAgent,
    photoCount,
    photos,
    mls,
  };
}

function _buildPanelAgentBlockHtml(title, agent, emptyText) {
  if (agent?.name) {
    return `
      <div class="propelio-popup-agent-block">
        <div class="propelio-popup-agent-label">${title}</div>
        <div class="propelio-popup-agent-name">${_propelioEscape(agent.name || "—")}</div>
        ${agent.officeName ? `<div class="propelio-popup-agent-line">${_propelioEscape(agent.officeName)}</div>` : ""}
        <div class="propelio-popup-agent-contact">
          ${agent.phone ? `<a href="tel:${encodeURIComponent(agent.phone)}">${_propelioEscape(agent.phone)}</a>` : ""}
          ${agent.email ? `<a href="mailto:${encodeURIComponent(agent.email)}">${_propelioEscape(agent.email)}</a>` : ""}
          ${agent.officePhone && agent.officePhone !== agent.phone ? `<a href="tel:${encodeURIComponent(agent.officePhone)}" class="muted">office: ${_propelioEscape(agent.officePhone)}</a>` : ""}
        </div>
      </div>`;
  }
  return `
    <div class="propelio-popup-agent-block parcel-panel-agent-empty">
      <div class="propelio-popup-agent-label">${title}</div>
      <div class="parcel-panel-empty-copy">${emptyText}</div>
    </div>`;
}

// Owner-history panel section. Server attaches `owner_history` (a
// reconciled chain dict — see _owner_history_for_popup in api/main.py)
// only when the requesting user is a superuser (developer/owner/power_user)
// AND the parcel's county has ingested rows in ownership_snapshots.
//
// The chain reconciles the live CAD owner with the snapshot data: when
// they match (most parcels), the section shows "Current Owner" + "Acquired"
// straight from the snapshot. When they mismatch (recent sale, snapshot
// lags), the server promotes the live CAD owner to "Current Owner" with
// the live deed_date for "Acquired", and demotes the snapshot's "current"
// into the first "Previously" entry — preserves chain order so the user
// doesn't have to mentally reconcile two competing "Current Owner" rows.
//
// Per Mike's 2026-06-01 ask, this section sits above Remarks in the
// parcel detail panel.
// Outreach section v2 (Mailer + Phone Tracking, 2026-06-03 PM).
//
// Two editable fields per parcel:
//   - Contact Info Retrieved (checkbox) — "have I done skip-trace prep?"
//   - Last Mailer Sent (date input) — when the last physical mail went out
//
// v1 had three fields (Phone Number text + Mailer Sent boolean + Mailer
// Date). Mike's call after preview smoke: LotLedger doesn't store phones
// (those live in his CRM), and a single date field is cleaner than a
// redundant boolean+date pair.
//
// Save-on-change pattern (mirrors filter-state autosave). Each field PUTs
// to /api/parcels/outreach with the appropriate _set flag on. No save
// button. Optimistic UI — failed PUT reverts + shows toast.
function _buildPanelOutreachHtml(p) {
  if (!_isPowerUserOrAbove()) return "";
  const county = String(p?.source_county || "").trim().toLowerCase();
  const parcelKey = String(p?.parcel_key || p?.account_num || "").trim();
  const accountNum = String(p?.account_num || "").trim();
  if (!county || !parcelKey || !accountNum) return "";
  // DCAD matches by account_num; the others by parcel_key.
  const matchKey = county === "dcad" ? accountNum : (p?.parcel_key || parcelKey);
  const contactRetrieved = Boolean(p?.outreach_contact_info_retrieved);
  const mailerDate = String(p?.outreach_mailer_date || "").trim();
  const escapedKey = _propelioEscape(matchKey);
  const escapedCounty = _propelioEscape(county);
  return `
        <section class="parcel-panel-outreach" data-outreach-section>
          <div class="parcel-panel-section-title">Outreach</div>
          <table class="popup-table">
            <tr>
              <td class="popup-label">Contact info retrieved</td>
              <td class="popup-val">
                <label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer;">
                  <input type="checkbox" class="parcel-panel-outreach-input"
                         data-outreach-field="contact_info_retrieved"
                         data-outreach-county="${escapedCounty}"
                         data-outreach-parcel-id="${escapedKey}"
                         ${contactRetrieved ? "checked" : ""}>
                  <span class="parcel-panel-outreach-contact-label">${contactRetrieved ? "yes" : ""}</span>
                </label>
              </td>
            </tr>
            <tr>
              <td class="popup-label">Last mailer sent date</td>
              <td class="popup-val">
                <input type="date" class="parcel-panel-outreach-input"
                       data-outreach-field="mailer_date"
                       data-outreach-county="${escapedCounty}"
                       data-outreach-parcel-id="${escapedKey}"
                       value="${_propelioEscape(mailerDate)}"
                       style="font:inherit;padding:2px 4px;">
              </td>
            </tr>
          </table>
        </section>`;
}


function _buildPanelOwnerHistoryHtml(p) {
  const block = p?.owner_history && typeof p.owner_history === "object" ? p.owner_history : null;
  if (!block) return "";
  const currentOwner = String(block.current_owner || "").trim();
  const currentAcquired = String(block.current_acquired || "").trim();
  const previous = Array.isArray(block.previous) ? block.previous : [];
  if (!currentOwner && previous.length === 0) return "";

  const rows = [];
  if (currentOwner) {
    rows.push(_buildParcelDetailTableRow("Current Owner", _propelioEscape(currentOwner)));
  }
  if (currentAcquired) {
    rows.push(_buildParcelDetailTableRow("Acquired", _propelioEscape(currentAcquired)));
  }
  for (const entry of previous) {
    const text = String(entry || "").trim();
    if (!text) continue;
    rows.push(_buildParcelDetailTableRow("Previously", _propelioEscape(text)));
  }
  if (rows.length === 0) return "";
  return `
        <section class="parcel-panel-owner-history">
          <div class="parcel-panel-section-title">Owner History</div>
          <table class="popup-table">
            ${rows.join("")}
          </table>
        </section>`;
}


function _buildParcelDetailPanelHtml(p, matchedComp) {
  const pseudoFeature = { properties: p };
  const hasVisibleSoldComp = Boolean(p?.sold_comp);
  const effectiveMatchedComp = matchedComp || _findMatchedCompForAccount(p?.account_num);
  const PROPELIO_HEADER_COLORS = { sold: "#dc2626", active: "#22c55e", pending: "#0284c7" };
  const matchedBucket = effectiveMatchedComp ? _propelioStatusBucket(effectiveMatchedComp) : null;
  const matchedHeaderColor = matchedBucket ? PROPELIO_HEADER_COLORS[matchedBucket] : null;
  const matchedHeaderText = effectiveMatchedComp
    ? String(effectiveMatchedComp?.status || matchedBucket || "").toUpperCase()
    : "";
  const matchedHeaderPrice = effectiveMatchedComp && Number.isFinite(Number(effectiveMatchedComp.price))
    ? `$${Math.round(Number(effectiveMatchedComp.price)).toLocaleString()}`
    : "";
  const statusColor = matchedHeaderColor || (hasVisibleSoldComp ? SOLD_OUTLINE_COLOR : getColor(pseudoFeature));
  const statusText = matchedHeaderText || (hasVisibleSoldComp ? "SOLD" : getStatusLabel(pseudoFeature));
  const soldHeaderPrice = matchedHeaderPrice
    || (hasVisibleSoldComp
      ? (typeof p.sold_comp?.sold_price === "number"
        ? `$${p.sold_comp.sold_price.toLocaleString()}`
        : String(p.sold_comp?.sold_price || ""))
      : "");
  const activeListingPrice = p.on_redfin && p.redfin_price ? String(p.redfin_price) : "";
  const listingDelta = p.on_redfin && p.redfin_price
    ? _buildParcelDeltaMeta(p, "LP vs CAD", parseInt(String(p.redfin_price).replace(/[^0-9]/g, ""), 10))
    : null;
  const compDelta = effectiveMatchedComp && Number.isFinite(Number(effectiveMatchedComp.price))
    ? _buildParcelDeltaMeta(p, "Comp vs CAD", Number(effectiveMatchedComp.price))
    : null;
  const headerDelta = compDelta || listingDelta;
  const compDetails = _getParcelPanelCompDetails(effectiveMatchedComp);
  // Unified rating buttons — one row that rates BOTH the parcel AND the
  // matched comp (when present) on click. Per 2026-05-24 design call:
  // single set of buttons, comp-style visual, dual-write semantics.
  // _buildRatingButtonsHtml renders even when there's no matched comp
  // as long as the parcel has identifying fields (county + account_num).
  const ratingButtonsHtml = _buildRatingButtonsHtml(effectiveMatchedComp, p);
  const realtorLinkHtml = compDetails?.mls
    ? `<a class="propelio-popup-realtor-link" href="https://www.realtor.com/realestateandhomes-search/MLSID-${encodeURIComponent(compDetails.mls)}" target="_blank" rel="noopener noreferrer">Look up MLS# ${_propelioEscape(compDetails.mls)} on Realtor.com</a>`
    : "";
  const saveLinkHtml = p.account_num
    ? `<a href="#" class="parcel-panel-save-link parcel-save-link" data-account="${_propelioEscape(p.account_num)}" data-county="${_propelioEscape(p.source_county || "dcad")}" data-addr="${_propelioEscape(p.addr || "")}" data-city="${_propelioEscape(p.city || "")}" data-lat="${_propelioEscape(p.lat || "")}" data-lng="${_propelioEscape(p.lng || "")}">📌 Save parcel</a>`
    : `<span class="parcel-panel-action-muted">Parcel save unavailable</span>`;
  const photoHeroHtml = compDetails
    ? (compDetails.photos.length
      ? `<div class="parcel-panel-photo-hero">
          <img class="parcel-panel-photo-image" data-photo-hero src="/api/propelio/photo?url=${encodeURIComponent(compDetails.photos[0].url)}" alt="MLS listing photo 1 of ${compDetails.photos.length}">
          ${compDetails.photos.length > 1 ? `<button type="button" class="parcel-panel-photo-nav prev" data-photo-nav="prev" aria-label="Previous photo">&#8249;</button>
          <button type="button" class="parcel-panel-photo-nav next" data-photo-nav="next" aria-label="Next photo">&#8250;</button>
          <div class="parcel-panel-photo-counter" data-photo-counter>1 / ${compDetails.photos.length}</div>` : ""}
        </div>`
      : `<div class="parcel-panel-photo-hero is-empty"><div class="parcel-panel-photo-placeholder">No photos available</div></div>`)
    : `<div class="parcel-panel-photo-hero is-empty"><div class="parcel-panel-photo-placeholder">No MLS comp</div></div>`;

  const schoolsHtml = compDetails && (compDetails.schools.elementary || compDetails.schools.middle || compDetails.schools.high)
    ? `<div class="propelio-popup-schools">
        ${compDetails.schools.elementary ? `<span class="propelio-popup-school"><span class="label">ES</span> ${_propelioEscape(compDetails.schools.elementary)}</span>` : ""}
        ${compDetails.schools.middle ? `<span class="propelio-popup-school"><span class="label">MS</span> ${_propelioEscape(compDetails.schools.middle)}</span>` : ""}
        ${compDetails.schools.high ? `<span class="propelio-popup-school"><span class="label">HS</span> ${_propelioEscape(compDetails.schools.high)}</span>` : ""}
      </div>`
    : "";
  const targetDistanceMeta = _buildDistanceToTargetMeta(p);

  const soldCompRows = p.sold_comp
    ? `
      <tr><td colspan="2" class="parcel-panel-table-break">Recent Sold Comp</td></tr>
      ${_buildParcelDetailTableRow("Sold Price", typeof p.sold_comp?.sold_price === "number" ? `$${p.sold_comp.sold_price.toLocaleString()}` : p.sold_comp?.sold_price)}
      ${_buildParcelDetailTableRow("Sold Date", p.sold_comp?.sold_date ? String(p.sold_comp.sold_date).slice(0, 10) : "N/A")}
      ${_buildParcelDetailTableRow("Days on Market", p.sold_comp?.dom == null ? "N/A" : String(p.sold_comp.dom))}
      ${_buildParcelDetailTableRow("Listing", p.sold_comp?.listing_url ? `<a href="${p.sold_comp.listing_url}" target="_blank" rel="noopener noreferrer">View listing</a>` : "N/A")}
    `
    : "";

  const mlsHtml = compDetails
    ? `
      ${photoHeroHtml}
      <div class="propelio-popup-price">${_propelioEscape(compDetails.priceText)} · ${_propelioEscape(compDetails.status.toUpperCase())}</div>
      ${compDetails.listVsCloseText ? `<div class="propelio-popup-meta">${_propelioEscape(compDetails.listVsCloseText)}</div>` : ""}
      ${compDetails.soldMetaText ? `<div class="propelio-popup-meta">${_propelioEscape(compDetails.soldMetaText)}</div>` : ""}
      ${compDetails.dimsText ? `<div class="propelio-popup-meta">${_propelioEscape(compDetails.dimsText)}</div>` : ""}
      ${compDetails.bbText ? `<div class="propelio-popup-meta">${_propelioEscape(compDetails.bbText)}</div>` : ""}
      ${compDetails.idText ? `<div class="propelio-popup-meta">${_propelioEscape(compDetails.idText)}${compDetails.source ? ` <span class="propelio-popup-source">(${_propelioEscape(compDetails.source)})</span>` : ""}</div>` : ""}
      ${compDetails.modifiedTs ? `<div class="popup-propelio-meta-mute">Last Pulled from MLS ${_propelioEscape(compDetails.modifiedTs)}</div>` : ""}
      ${schoolsHtml}
      ${compDetails.photoCount > 0 ? `<div class="propelio-popup-meta-mute">${compDetails.photoCount} listing photo${compDetails.photoCount === 1 ? "" : "s"} (Propelio-hosted)</div>` : ""}
    `
    : `
      ${photoHeroHtml}
      <div class="parcel-panel-empty-copy">No MLS comp for this parcel. Pull comps from a polygon draw to see matched listings here.</div>
    `;

  return {
    photos: compDetails?.photos || [],
    html: `
      <div class="parcel-panel-header">
        <div>
          <div class="parcel-panel-address">${_propelioEscape(_popupHeaderAddress(p) || p.addr || "Unknown address")}</div>
          <div class="parcel-panel-header-meta">
            <span class="parcel-panel-status-pill" style="--status-color:${statusColor}">${_propelioEscape(statusText)}</span>
            ${matchedHeaderPrice || activeListingPrice || soldHeaderPrice ? `<span class="parcel-panel-header-price">${matchedHeaderPrice || activeListingPrice || soldHeaderPrice}</span>` : ""}
            ${headerDelta ? `<span class="parcel-panel-header-delta" style="color:${headerDelta.color}">${_propelioEscape(headerDelta.label)} ${_propelioEscape(headerDelta.text)}</span>` : ""}
            ${targetDistanceMeta}
          </div>
        </div>
        <button type="button" class="parcel-panel-close" aria-label="Close parcel details">&times;</button>
      </div>
      <div class="parcel-panel-body">
        <div class="parcel-panel-body-grid">
          <section class="parcel-panel-cad">
            <div class="parcel-panel-section-title">CAD</div>
            <table class="popup-table">
              ${_buildParcelDetailTableRow("Owner", _panelDisplayValue(p.owner))}
              ${_buildParcelDetailTableRow("Land Value", _panelDisplayValue(p.land_val))}
              ${_buildParcelDetailTableRow("Total Value", _totalValueDisplay(p))}
              ${listingDelta ? _buildParcelDetailTableRow(listingDelta.label, `<span style="color:${listingDelta.color}">${_propelioEscape(listingDelta.text)}</span>`) : ""}
              ${compDelta ? _buildParcelDetailTableRow(compDelta.label, `<span style="color:${compDelta.color}">${_propelioEscape(compDelta.text)}</span>`) : ""}
              ${_buildParcelDetailTableRow("Land % of Total", _panelDisplayValue(p.land_pct))}
              ${_buildParcelDetailTableRow("Living Area", p.sqft && p.sqft !== "N/A" ? `${p.sqft} sf` : "N/A")}
              ${_buildParcelDetailTableRow("Lot Size", _panelDisplayValue(p.lot_sqft))}
              ${_buildParcelDetailTableRow("Acres", _panelDisplayValue(p.lot_acres))}
              ${_buildParcelDetailTableRow("Frontage", _panelDisplayValue(p.frontage))}
              ${_buildParcelDetailTableRow("Depth", _panelDisplayValue(p.depth))}
              ${_buildParcelDetailTableRow("State Code", _panelDisplayValue(p.state_code))}
              ${_buildParcelDetailTableRow("Zoning", _panelDisplayValue(p.zoning))}
              ${_buildParcelDetailTableRow("School District", _panelDisplayValue(p.school))}
              ${_buildParcelDetailTableRow("Year Built", _panelDisplayValue(p.yr_built))}
              <!-- v3 residential detail expansion (Phase 1): per-county
                   CAD source data exposed on feature.properties via the
                   canonical-field contract in
                   docs/CAD_RESIDENTIAL_DETAIL_EXPANSION_SPEC.md.
                   DCAD parcels populate most of these from RES_DETAIL.CSV;
                   Collin partial (beds/baths/pool/stories); TAD pending
                   the TAD-half PR; Denton no source data → N/A everywhere. -->
              ${_buildParcelDetailTableRow("Effective Year Built", _panelDisplayValue(p.eff_yr_built))}
              ${_buildParcelDetailTableRow("Actual Age", _panelDisplayValue(p.act_age))}
              ${p.subdivision ? _buildParcelDetailTableRow("Neighborhood", _neighborhoodCellHtml(p.subdivision, p.legal_description)) : ""}
              ${_buildParcelDetailTableRow("% Complete", _panelDisplayValue(p.pct_complete))}
              ${_buildParcelDetailTableRow("Beds", _panelDisplayValue(p.beds))}
              ${_buildParcelDetailTableRow("Full Baths", _panelDisplayValue(p.full_baths))}
              ${_buildParcelDetailTableRow("Half Baths", _panelDisplayValue(p.half_baths))}
              ${_buildParcelDetailTableRow("Fireplaces", _panelDisplayValue(p.fireplaces))}
              ${_buildParcelDetailTableRow("Kitchens", _panelDisplayValue(p.kitchens))}
              ${_buildParcelDetailTableRow("Wet Bars", _panelDisplayValue(p.wet_bars))}
              ${_buildParcelDetailTableRow("Units", _panelDisplayValue(p.units))}
              ${_buildParcelDetailTableRow("Garage Capacity", _panelDisplayValue(p.garage_capacity))}
              ${_buildParcelDetailTableRow("Stories", _panelDisplayValue(p.stories))}
              ${p.stories_desc ? _buildParcelDetailTableRow("Stories (raw)", _propelioEscape(p.stories_desc)) : ""}
              ${_buildParcelDetailTableRow("Foundation Type", _panelTitleCaseDisplay(p.foundation_type))}
              ${_buildParcelDetailTableRow("Construction Frame", _panelTitleCaseDisplay(p.construction_frame_type))}
              ${_buildParcelDetailTableRow("Exterior Wall", _panelTitleCaseDisplay(p.ext_wall))}
              ${_buildParcelDetailTableRow("Heating Type", _panelTitleCaseDisplay(p.heating_type))}
              ${_buildParcelDetailTableRow("AC Type", _panelTitleCaseDisplay(p.ac_type))}
              ${_buildParcelDetailTableRow("Roof Type", _panelTitleCaseDisplay(p.roof_type))}
              ${_buildParcelDetailTableRow("Roof Material", _panelTitleCaseDisplay(p.roof_material))}
              ${_buildParcelDetailTableRow("Fence Type", _panelTitleCaseDisplay(p.fence_type))}
              ${_buildParcelDetailTableRow("Basement", _panelTitleCaseDisplay(p.basement))}
              ${_buildParcelDetailTableRow("Building Class", _panelDisplayValue(p.bldg_class))}
              ${_buildParcelDetailTableRow("CDU Rating", _panelTitleCaseDisplay(p.cdu_rating))}
              ${_buildParcelDetailTableRow("Pool", _panelFlagDisplay(p.pool_flag))}
              ${_buildParcelDetailTableRow("Spa", _panelFlagDisplay(p.spa_flag))}
              ${_buildParcelDetailTableRow("Sauna", _panelFlagDisplay(p.sauna_flag))}
              ${_buildParcelDetailTableRow("Sprinkler System", _panelFlagDisplay(p.sprinkler_flag))}
              ${_buildParcelDetailTableRow("Deck", _panelFlagDisplay(p.deck_flag))}
              <!-- Phase 3 — Denton-only / Denton-rich canonical keys
                   (2026-05-21). Show "N/A" for DCAD/Collin/TAD parcels when
                   those CADs don't publish the field. Phase 3 patch v3
                   removed the wrong "Plumbing Fixtures" label — Denton's
                   "Plumbing" attribute IS actually bath count (decimal,
                   half-baths). That now flows through Full/Half/Baths rows
                   above instead.   -->
              ${_buildParcelDetailTableRow("Total Rooms", _panelDisplayValue(p.total_rooms))}
              ${_buildParcelDetailTableRow("Outdoor Fireplace", _panelDisplayValue(p.outdoor_fireplaces))}
              ${_buildParcelDetailTableRow("End Unit (Condo/TH)", _panelFlagDisplay(p.end_unit))}
              ${_buildParcelDetailTableRow("Interior Finish", _panelDisplayValue(p.interior_finish))}
              ${_buildParcelDetailTableRow("Flooring", _panelDisplayValue(p.flooring))}
              ${p.on_redfin && p.redfin_url ? _buildParcelDetailTableRow("Listing", `<a href="${p.redfin_url}" target="_blank" rel="noopener noreferrer">View listing</a>`) : ""}
              ${soldCompRows}
              ${(() => {
                // Flood Zone row (FEMA NFHL, Phase 3). Always rendered
                // per KK 2026-06-06 — parcels outside any mapped FEMA
                // polygon show "N/A" (matching every other CAD row's
                // unknown-data convention), parcels inside get the
                // verbose plain-English wording. FLOODWAY warning stays
                // loud so Mike can't miss it.
                const _fz = String(p?.flood_zone || "").trim();
                const _sub = String(p?.flood_zone_subtype || "").trim().toUpperCase();
                const _bfe = p?.flood_bfe;
                let _label;
                if (!_fz) {
                  _label = "N/A";
                } else if (_sub === "FLOODWAY") {
                  _label = `${_fz} — FLOODWAY (no build)`;
                } else if (["AE","A","AH","AO","V","VE"].includes(_fz)) {
                  _label = (_bfe !== null && _bfe !== undefined && Number.isFinite(Number(_bfe)))
                    ? `${_fz} (BFE ${Number(_bfe).toFixed(1)} ft)`
                    : _fz;
                } else if (_fz === "X" && _sub.indexOf("0.2 PCT") !== -1) {
                  _label = "X — 500-yr floodplain";
                } else if (_fz === "X" && _sub.indexOf("MINIMAL") !== -1) {
                  _label = "X — minimal risk";
                } else {
                  _label = _fz;
                }
                return _buildParcelDetailTableRow("Flood Zone", _propelioEscape(_label));
              })()}
              ${(() => {
                // Parcel ID at the bottom of the CAD detail table (KK
                // request 2026-06-05) — same value as the "Parcel ID"
                // CSV column at position B. DCAD uses account_num; the
                // other 3 counties use parcel_key.
                const _cnty = String(p?.source_county || "").trim().toLowerCase();
                const _pid = _cnty === "dcad"
                  ? String(p?.account_num || "").trim()
                  : (String(p?.parcel_key || "").trim() || String(p?.account_num || "").trim());
                return _pid
                  ? _buildParcelDetailTableRow("Parcel ID", _propelioEscape(_pid))
                  : "";
              })()}
            </table>
          </section>
          <section class="parcel-panel-mls">
            <div class="parcel-panel-section-title">MLS</div>
            ${mlsHtml}
          </section>
        </div>
        <section class="parcel-panel-agents">
          ${compDetails
            ? _buildPanelAgentBlockHtml("Listing Agent", compDetails.listingAgent, "No listing agent details available.")
            : _buildPanelAgentBlockHtml("Listing Agent", null, "No listing agent details available.")}
          ${compDetails && compDetails.status === "sold"
            ? _buildPanelAgentBlockHtml("Buyer Agent", compDetails.buyerAgent, "No buyer agent details available.")
            : _buildPanelAgentBlockHtml("Buyer Agent", null, "Buyer agent only appears on sold comps.")}
        </section>
        ${_buildPanelOwnerHistoryHtml(p)}
        ${_buildPanelOutreachHtml(p)}
        <section class="parcel-panel-remarks">
          <div class="parcel-panel-section-title">Remarks</div>
          ${compDetails?.remarks
            ? `<div class="propelio-popup-remarks-full parcel-panel-remarks-box">${_propelioEscape(compDetails.remarks)}</div>`
            : `<div class="parcel-panel-empty-copy">No remarks available for this parcel.</div>`}
        </section>
      </div>
      <section class="parcel-panel-actions">
        <div class="parcel-panel-action-slot parcel-panel-action-ratings">
          ${ratingButtonsHtml || '<span class="parcel-panel-action-muted">Save area to rate</span>'}
        </div>
        <div class="parcel-panel-action-slot parcel-panel-action-save">
          ${saveLinkHtml}
        </div>
        ${_buildSubjectPropertyLoadAreaHtml(p)}
      </section>`,
  };
}

function _flyToParcelDetailLatLng(latlng) {
  if (!latlng) return;
  const ll = Array.isArray(latlng) ? L.latLng(latlng[0], latlng[1]) : L.latLng(latlng);
  const panel = document.getElementById("parcel-detail-panel");
  const xOffset = panel ? Math.round(Math.min(panel.offsetWidth || 0, 820) * 0.18) : 0;

  // Honor the global Keep View toggle. In stay mode we DO NOT change the
  // zoom level — only pan when the clicked parcel would otherwise sit
  // behind the panel or off-screen. In jump mode (default) we still
  // zoom toward 17 (or further in if the user was already zoomed deeper).
  const mode = (typeof getClickMode === "function" ? getClickMode() : "jump");

  if (mode === "stay") {
    const currentZoom = map.getZoom();
    const shiftedCenter = map.unproject(map.project(ll, currentZoom).add([xOffset, 0]), currentZoom);
    const inView = typeof isPointInViewport === "function" ? isPointInViewport(ll) : true;
    if (inView) return; // already visible at current zoom — nothing to do
    map.panTo(shiftedCenter, { animate: true, duration: 0.35 });
    return;
  }

  const targetZoom = Math.max(map.getZoom(), 17);
  const shiftedCenter = map.unproject(map.project(ll, targetZoom).add([xOffset, 0]), targetZoom);
  map.flyTo(shiftedCenter, targetZoom, { duration: 0.35, animate: true });
}

function closeParcelDetailPanel() {
  const panel = document.getElementById("parcel-detail-panel");
  if (!panel) return;
  panel.classList.add("hidden");
  panel.classList.remove("is-open");
  panel.setAttribute("aria-hidden", "true");
  panel.innerHTML = "";
  _activeParcelPopupState = null;
}

function openParcelDetailPanel(parcelProps, opts = {}) {
  const panel = document.getElementById("parcel-detail-panel");
  if (!panel || !parcelProps) return;
  if (_handleMeasureInteraction(opts.latlng || null, parcelProps)) return;

  const matchedComp = Object.prototype.hasOwnProperty.call(opts, "matchedComp")
    ? opts.matchedComp
    : _findMatchedCompForAccount(parcelProps?.account_num);
  const render = _buildParcelDetailPanelHtml(parcelProps, matchedComp);

  panel.innerHTML = render.html;
  panel.classList.remove("hidden");
  panel.classList.add("is-open");
  panel.setAttribute("aria-hidden", "false");
  const body = panel.querySelector(".parcel-panel-body");
  if (body) body.scrollTop = 0;

  panel.querySelector(".parcel-panel-close")?.addEventListener("click", closeParcelDetailPanel);
  _wireParcelInteractiveUi(panel, { close: closeParcelDetailPanel });

  // Pre-fetch every photo through the proxy as soon as the panel opens so
  // next/prev clicks pull from browser cache (Cache-Control: immutable from
  // the proxy) instead of doing a fresh round-trip each time. Skip the hero
  // — it's already loading via the visible <img>. Fire-and-forget; browser
  // limits concurrency naturally.
  for (let i = 1; i < render.photos.length; i += 1) {
    const url = render.photos[i]?.url;
    if (!url) continue;
    const preloader = new Image();
    preloader.src = `/api/propelio/photo?url=${encodeURIComponent(url)}`;
  }

  if (render.photos.length > 1) {
    let currentPhotoIndex = 0;
    const img = panel.querySelector("[data-photo-hero]");
    const counter = panel.querySelector("[data-photo-counter]");
    const updatePhoto = () => {
      if (!img) return;
      const photo = render.photos[currentPhotoIndex];
      if (!photo?.url) return;
      img.src = `/api/propelio/photo?url=${encodeURIComponent(photo.url)}`;
      img.alt = `MLS listing photo ${currentPhotoIndex + 1} of ${render.photos.length}`;
      if (counter) counter.textContent = `${currentPhotoIndex + 1} / ${render.photos.length}`;
    };
    panel.querySelector('[data-photo-nav="prev"]')?.addEventListener("click", () => {
      currentPhotoIndex = (currentPhotoIndex - 1 + render.photos.length) % render.photos.length;
      updatePhoto();
    });
    panel.querySelector('[data-photo-nav="next"]')?.addEventListener("click", () => {
      currentPhotoIndex = (currentPhotoIndex + 1) % render.photos.length;
      updatePhoto();
    });
  }

  _activeParcelPopupState = {
    accountNum: String(parcelProps?.account_num || ""),
    props: parcelProps,
    latlng: opts.latlng || null,
    matchedComp: matchedComp || null,
    geometry: opts.geometry || null,
  };

  // Drop a purple selected-outline on the parcel currently in the panel so the
  // user can still see WHICH parcel they were just looking at after they close
  // the panel. The outline persists until a different parcel is clicked OR
  // they click the empty map (which clears it via the chunk-5 map.on("click")
  // listener). Falls back gracefully when no geometry is available — the
  // outline just doesn't render for that case.
  if (opts.geometry) {
    _renderSelectedOutline(opts.geometry);
  }

  map.closePopup();
  closeTransientSoldSidebarPopup();

  if (opts.latlng && !opts.suppressFly) {
    _suspendViewportRender(500);
    _flyToParcelDetailLatLng(opts.latlng);
  }
}

function makePopupHtml(p) {
  const pseudoFeature = { properties: p };
  const hasVisibleSoldComp = Boolean(p?.sold_comp);

  // If this parcel has a matched Propelio comp, the popup header takes
  // its status + price from the comp instead of the CAD classification:
  // sold → purple, active → red, pending → amber. This mirrors the
  // legacy R.F. card pattern where the header signaled the comp's MLS
  // status and its price front-and-center.
  const matchedComp = _findMatchedCompForAccount(p?.account_num);
  const PROPELIO_HEADER_COLORS = { sold: "#dc2626", active: "#22c55e", pending: "#0284c7" };
  const matchedBucket = matchedComp ? _propelioStatusBucket(matchedComp) : null;
  const matchedHeaderColor = matchedBucket ? PROPELIO_HEADER_COLORS[matchedBucket] : null;
  const matchedHeaderText = matchedComp
    ? String(matchedComp?.status || matchedBucket || "").toUpperCase()
    : "";
  const matchedHeaderPrice = matchedComp && Number.isFinite(Number(matchedComp.price))
    ? `$${Math.round(Number(matchedComp.price)).toLocaleString()}`
    : "";

  const statusColor = matchedHeaderColor
    || (hasVisibleSoldComp ? SOLD_OUTLINE_COLOR : getColor(pseudoFeature));
  const statusText = matchedHeaderText
    || (hasVisibleSoldComp ? "SOLD" : getStatusLabel(pseudoFeature));
  const soldHeaderPrice = matchedHeaderPrice
    || (hasVisibleSoldComp
      ? (typeof p.sold_comp?.sold_price === "number"
        ? `$${p.sold_comp.sold_price.toLocaleString()}`
        : String(p.sold_comp?.sold_price || ""))
      : "");
  const verifiedVacant = normalizeVerificationValue(
    verificationByAccount.get(p.account_num) || p.verified_vacant
  );
  const subdivision = String(p.subdivision || "").trim();
  const row = (label, val) => `<tr><td class="popup-label">${label}</td><td class="popup-val">${val || "N/A"}</td></tr>`;
  const targetDistanceMeta = _buildDistanceToTargetMeta(p);

  // Helper — produce a colored over/under-CAD delta row for any price source.
  // dcadRaw is just `${p.tot_val}` (already a "$NNN,NNN" string); numeric is
  // the comp-side price (Propelio raw number, or parsed RF price string).
  const buildDeltaRow = (label, numeric) => {
    if (!Number.isFinite(numeric) || numeric <= 0) return "";
    const dcadRaw = String(p.tot_val || "").replace(/[^0-9]/g, "");
    const dcadNum = dcadRaw ? parseInt(dcadRaw, 10) : NaN;
    if (!Number.isFinite(dcadNum) || dcadNum <= 0) return "";
    const delta = Math.round(numeric) - dcadNum;
    const pct = ((delta / dcadNum) * 100).toFixed(1);
    const sign = delta >= 0 ? "+" : "";
    const color = delta >= 0 ? "#27ae60" : "#e74c3c";
    return `<tr><td class="popup-label">${label}</td><td class="popup-val" style="color:${color}">${sign}$${Math.abs(delta).toLocaleString()} (${sign}${pct}%)</td></tr>`;
  };

  // Active listing price in header + delta row in table.
  let activeListingPrice = "";
  let listingDeltaRow = "";
  let redfinListingRow = "";
  if (p.on_redfin && p.redfin_price) {
    activeListingPrice = p.redfin_url
      ? `<a href="${p.redfin_url}" target="_blank" rel="noopener noreferrer">${p.redfin_price}</a>`
      : p.redfin_price;

    const rfNum = parseInt(String(p.redfin_price).replace(/[^0-9]/g, ""), 10);
    listingDeltaRow = buildDeltaRow("LP vs DCAD", rfNum);

    if (p.redfin_url) {
      redfinListingRow = row("Listing", `<a href="${p.redfin_url}" target="_blank" rel="noopener noreferrer">View listing</a>`);
    }
  }

  // Propelio comp price (sold or list, whichever the matched comp carries) vs
  // CAD total value — same red/green delta treatment so investors can scan
  // over/under-CAD at a glance across all counties.
  let propelioDeltaRow = "";
  if (matchedComp && Number.isFinite(Number(matchedComp.price))) {
    propelioDeltaRow = buildDeltaRow("Comp vs CAD", Number(matchedComp.price));
  }

  let soldCompRows = "";
  if (p.sold_comp) {
    const sold = p.sold_comp;
    const soldPrice = typeof sold.sold_price === "number" ? `$${sold.sold_price.toLocaleString()}` : sold.sold_price;
    const soldDate = sold.sold_date ? String(sold.sold_date).slice(0, 10) : "N/A";
    const soldDom = sold.dom == null ? "N/A" : String(sold.dom);
    const soldListing = sold.listing_url
      ? `<a href="${sold.listing_url}" target="_blank" rel="noopener noreferrer">View listing</a>`
      : "N/A";

    soldCompRows = `
      <tr><td colspan="2" style="padding-top:8px;border-top:1px solid #e2e8f0;font-weight:600;color:${SOLD_MARKER_BORDER};">Recent Sold Comp</td></tr>
      ${row("Sold Price", soldPrice)}
      ${row("Sold Date", soldDate)}
      ${row("Days on Market", soldDom)}
      ${row("Listing", soldListing)}
    `;
  }

  // matchedComp resolved above (drives both the header recolor and these).
  const propelioSectionHtml = matchedComp ? _buildPropelioCompSectionHtml(matchedComp) : "";
  // Unified rating buttons (dual-write parcel + matched-comp). Per
  // 2026-05-24 design call.
  const ratingButtonsHtml = _buildRatingButtonsHtml(matchedComp, p);

  return `
      <div class="popup">
        <div class="popup-addr">${p.addr || "Unknown address"}</div>
        <div class="popup-status-row">
          <div class="popup-status" style="color:${statusColor};">${statusText}</div>
          <div class="popup-status-meta">
            ${matchedHeaderPrice
              ? `<div class="popup-sold-price" style="color:${statusColor};">${matchedHeaderPrice}</div>`
              : activeListingPrice
                ? `<div class="popup-list-price">${activeListingPrice}</div>`
                : soldHeaderPrice
                  ? `<div class="popup-sold-price">${soldHeaderPrice}</div>`
                  : ""}
            ${targetDistanceMeta}
          </div>
        </div>
        <table class="popup-table">
          ${row("Owner", p.owner)}
          ${row("Land Value", p.land_val)}
          ${row("Total Value", _totalValueDisplay(p))}
          ${listingDeltaRow}
          ${propelioDeltaRow}
          ${row("Land % of Total", p.land_pct)}
          ${row("Living Area", p.sqft && p.sqft !== "N/A" ? p.sqft + " sf" : "N/A")}
          ${row("Lot Size", p.lot_sqft)}
          ${row("Acres", p.lot_acres)}
          ${row("Frontage", p.frontage)}
          ${row("Depth", p.depth)}
          ${row("State Code", p.state_code)}
          ${row("Zoning", p.zoning)}
          ${row("School District", p.school)}
          ${row("Year Built", p.yr_built)}
          ${subdivision ? row("Neighborhood", _neighborhoodCellHtml(subdivision, p.legal_description)) : ""}
          ${redfinListingRow}
          ${soldCompRows}
        </table>
        ${propelioSectionHtml}
        ${ratingButtonsHtml}
        ${_buildSubjectPropertyLoadAreaHtml(p)}
        ${p.account_num ? `<div style="margin-top:8px;display:flex;gap:6px;align-items:center;justify-content:flex-end;font-size:11px;padding-top:6px;border-top:1px solid #e2e8f0;">
          <a href="#" class="parcel-save-link"
            data-account="${p.account_num}"
            data-county="${p.source_county || "dcad"}"
            data-addr="${(p.addr || "").replace(/"/g, "&quot;")}"
            data-city="${(p.city || "").replace(/"/g, "&quot;")}"
            data-lat="${p.lat || ""}"
            data-lng="${p.lng || ""}"
            style="color:#e67e22;text-decoration:none;">📌 Save parcel</a>
          <a href="#" class="parcel-clear-link" style="color:#aaa;text-decoration:none;">✕ Clear</a>
        </div>` : ""}
      </div>`;
}

function makeSoldPopupHtml(point) {
  const row = (label, val) => `<tr><td class="popup-label">${label}</td><td class="popup-val">${val || "N/A"}</td></tr>`;
  const price = typeof point.sold_price === "number" ? `$${point.sold_price.toLocaleString()}` : point.sold_price;
  const soldDate = point.sold_date ? String(point.sold_date).slice(0, 10) : "N/A";
  const dom = point.dom == null ? "N/A" : String(point.dom);
  const lotSqft = typeof point.lot_sqft === "number" ? `${point.lot_sqft.toLocaleString()} sf` : point.lot_sqft;
  const listing = point.listing_url
    ? `<a href="${point.listing_url}" target="_blank" rel="noopener noreferrer">View listing</a>`
    : "N/A";

  return `
      <div class="popup">
        <div class="popup-addr">${point.address || "Unknown address"}</div>
        <div class="popup-status sold-popup-status" style="color:${SOLD_MARKER_COLOR};">SOLD COMP</div>
        <table class="popup-table">
          ${row("Sold Price", price)}
          ${row("Sold Date", soldDate)}
          ${row("Days on Market", dom)}
          ${row("Lot Size", lotSqft)}
          ${row("Listing", listing)}
        </table>
      </div>`;
}

function soldDateTimestamp(value) {
  const t = Date.parse(String(value || ""));
  return Number.isFinite(t) ? t : -Infinity;
}

function soldPointMatchKey(point) {
  return String(point.listing_url || `${point.lat},${point.lng},${point.sold_date || ""}`);
}

function geometryOuterRings(geometry) {
  if (!geometry) return [];
  if (geometry.type === "Polygon") return Array.isArray(geometry.coordinates) ? geometry.coordinates : [];
  if (geometry.type === "MultiPolygon") {
    if (!Array.isArray(geometry.coordinates)) return [];
    return geometry.coordinates.flatMap((poly) => Array.isArray(poly) ? poly : []);
  }
  return [];
}

function pointInPolygonGeometry(pointLngLat, geometry) {
  if (!geometry) return false;
  if (geometry.type === "Polygon") {
    const rings = Array.isArray(geometry.coordinates) ? geometry.coordinates : [];
    if (!rings.length) return false;
    if (!pointInPolygonLngLat(pointLngLat, rings[0])) return false;
    for (let i = 1; i < rings.length; i += 1) {
      if (pointInPolygonLngLat(pointLngLat, rings[i])) return false;
    }
    return true;
  }
  if (geometry.type === "MultiPolygon") {
    const polygons = Array.isArray(geometry.coordinates) ? geometry.coordinates : [];
    for (const poly of polygons) {
      if (!Array.isArray(poly) || !poly.length) continue;
      if (!pointInPolygonLngLat(pointLngLat, poly[0])) continue;
      let inHole = false;
      for (let i = 1; i < poly.length; i += 1) {
        if (pointInPolygonLngLat(pointLngLat, poly[i])) {
          inHole = true;
          break;
        }
      }
      if (!inHole) return true;
    }
  }
  return false;
}

function geometryBounds(geometry) {
  const rings = geometryOuterRings(geometry);
  if (!rings.length) return null;

  let minLng = Infinity;
  let minLat = Infinity;
  let maxLng = -Infinity;
  let maxLat = -Infinity;

  for (const ring of rings) {
    for (const coord of ring) {
      const lng = Number(coord?.[0]);
      const lat = Number(coord?.[1]);
      if (!Number.isFinite(lng) || !Number.isFinite(lat)) continue;
      if (lng < minLng) minLng = lng;
      if (lng > maxLng) maxLng = lng;
      if (lat < minLat) minLat = lat;
      if (lat > maxLat) maxLat = lat;
    }
  }

  if (!Number.isFinite(minLng) || !Number.isFinite(minLat) || !Number.isFinite(maxLng) || !Number.isFinite(maxLat)) {
    return null;
  }

  return { minLng, minLat, maxLng, maxLat };
}

function attachSoldCompsToFeatures(features, soldPoints) {
  const list = Array.isArray(features) ? features : [];
  const points = Array.isArray(soldPoints) ? soldPoints : [];

  list.forEach((feature) => {
    if (!feature?.properties) return;
    delete feature.properties.sold_comp;
    delete feature.properties._sold_comp_source;
  });

  const polygonCandidates = [];
  list.forEach((feature) => {
    const geom = feature?.geometry;
    if (!geom || (geom.type !== "Polygon" && geom.type !== "MultiPolygon")) return;
    const bounds = geometryBounds(geom);
    if (!bounds) return;
    polygonCandidates.push({ feature, geometry: geom, bounds });
  });

  const sortedPoints = [...points].sort((a, b) => soldDateTimestamp(b?.sold_date) - soldDateTimestamp(a?.sold_date));
  const matchedSoldKeys = new Set();

  for (const point of sortedPoints) {
    const lng = Number(point?.lng);
    const lat = Number(point?.lat);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) continue;
    const key = soldPointMatchKey(point);
    const pointLngLat = [lng, lat];

    for (const candidate of polygonCandidates) {
      const b = candidate.bounds;
      if (lng < b.minLng || lng > b.maxLng || lat < b.minLat || lat > b.maxLat) continue;
      if (!pointInPolygonGeometry(pointLngLat, candidate.geometry)) continue;
      if (!candidate.feature.properties._sold_comp_source) {
        candidate.feature.properties._sold_comp_source = {
          sold_price: point.sold_price,
          sold_date: point.sold_date,
          yr_built: point.yr_built,
          dom: point.dom,
          lot_sqft: point.lot_sqft,
          listing_url: point.listing_url,
        };
        candidate.feature.properties.sold_comp = { ...candidate.feature.properties._sold_comp_source };
      }
      matchedSoldKeys.add(key);
      break;
    }
  }

  const unmatchedSoldPoints = points.filter((point) => !matchedSoldKeys.has(soldPointMatchKey(point)));
  const matchedLabelPoints = [];
  for (const feature of list) {
    const p = feature?.properties || {};
    if (!p.sold_comp) continue;
    const lat = Number(p.lat);
    const lng = Number(p.lng);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) continue;
    matchedLabelPoints.push({
      account_num: String(p.account_num || ""),
      lat,
      lng,
      sold_price: p.sold_comp.sold_price,
      sold_date: p.sold_comp.sold_date,
    });
  }

  return { unmatchedSoldPoints, matchedLabelPoints };
}

function renderSoldPoints() {
  // Unbind tooltips from prior anchor markers BEFORE clearing the array.
  // unbindTooltip alone is unreliable when the marker has already been
  // detached from its layer (which happens because renderFeatures runs
  // first and clears parcelTypeLayers.sold). Explicitly remove the tooltip
  // from the map first, THEN unbind the marker — and as a final safety
  // net, sweep the tooltipPane DOM for any orphaned .sold-price-label
  // elements that survived the cleanup.
  soldMarkers.forEach(({ marker }) => {
    const tooltip = marker?.getTooltip?.();
    if (tooltip) {
      try { tooltip.remove(); } catch {}
    }
    try { marker?.unbindTooltip?.(); } catch {}
  });
  document.querySelectorAll(".leaflet-tooltip.sold-price-label").forEach((el) => el.remove());
  soldMarkers = [];
  if (!filterState.sold) return;
  const soldParcelLayer = parcelTypeLayers.sold;
  if (!soldParcelLayer) return;

  // Matched sold comps are represented by sold parcel polygons. Create
  // invisible anchors only for zoom-gated price labels — and only for
  // parcels that ACTUALLY rendered (passed numeric filters like lot-size),
  // so labels follow the parcel visibility instead of leaking when filtered out.
  const visibleMatchedPoints = matchedSoldLabelPoints.filter(
    (point) => !point.account_num || _currentlyRenderedSoldAccounts.has(point.account_num)
  );
  visibleMatchedPoints.forEach((point) => {
    const marker = L.circleMarker([point.lat, point.lng], {
      pane: "soldPane",
      radius: 0,
      stroke: false,
      fill: false,
      opacity: 0,
      fillOpacity: 0,
      interactive: false,
      bubblingMouseEvents: false,
    }).addTo(soldParcelLayer);
    soldMarkers.push({
      marker,
      priceLabel: abbreviatePrice(point.sold_price),
      soldDateLabel: formatSoldDateLabel(point.sold_date),
    });
  });

  refreshSoldPriceLabels();
}

function renderRedfinPoints() {
  // Mirror of renderSoldPoints for active (Redfin) listings. Anchors are
  // invisible CircleMarkers placed at on_redfin parcel centroids; they exist
  // solely to host zoom-gated price tooltips. Cleaning up tooltips before
  // clearing the array uses the same belt-and-suspenders pattern as sold to
  // avoid orphaned .leaflet-tooltip.redfin-price-label nodes after re-renders.
  redfinMarkers.forEach(({ marker }) => {
    const tooltip = marker?.getTooltip?.();
    if (tooltip) {
      try { tooltip.remove(); } catch {}
    }
    try { marker?.unbindTooltip?.(); } catch {}
  });
  document.querySelectorAll(".leaflet-tooltip.redfin-price-label").forEach((el) => el.remove());
  redfinMarkers = [];
  if (!filterState.active) return;
  const activeParcelLayer = parcelTypeLayers.active;
  if (!activeParcelLayer) return;

  const features = Array.isArray(allAnalysisFeatures) ? allAnalysisFeatures : [];
  features.forEach((feature) => {
    const p = feature?.properties || {};
    if (!p.on_redfin) return;
    const lat = Number(p.lat);
    const lng = Number(p.lng);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) return;
    if (!passesNumericFilters(feature)) return;
    if (!passesCompFilters(feature)) return;
    const numeric = parseInt(String(p.redfin_price || "").replace(/[^0-9]/g, ""), 10);
    const priceLabel = Number.isFinite(numeric) && numeric > 0 ? abbreviatePrice(numeric) : "";
    if (!priceLabel) return;

    const marker = L.circleMarker([lat, lng], {
      pane: "soldPane",
      radius: 0,
      stroke: false,
      fill: false,
      opacity: 0,
      fillOpacity: 0,
      interactive: false,
      bubblingMouseEvents: false,
    }).addTo(activeParcelLayer);
    redfinMarkers.push({ marker, priceLabel });
  });

  refreshRedfinPriceLabels();
}

// Shared click handler for in-area parcel clicks (polygon + circle marker
// variants both go through this). Fetches a fresh /api/parcel detail
// before opening the side panel so the panel shows the latest CAD data
// (city in particular) regardless of how stale the cached_jobs.rows are
// for this workspace. Falls back to the cached feature props if the
// fetch fails so the panel still opens — just with whatever was in
// analyze's response.
//
// Background: analyze responses come from cached_jobs (a snapshot
// taken at workspace-save time). When a row's source data was updated
// later (e.g. the DCAD property_city backfill), the cache still holds
// the old shape. Polygon render is fine — it only needs geometry +
// classification — but the side panel reads address + city + every
// CAD field from props directly, so stale cache → wrong popup.
async function _openParcelDetailFromFeature(p, ev, feature) {
  if (ev && ev.originalEvent) L.DomEvent.stopPropagation(ev);
  const county = String(p?.source_county || "").trim();
  const account = String(p?.account_num || "").trim();
  if (county && account) {
    try {
      const resp = await fetch(`/api/parcel/${encodeURIComponent(county)}/${encodeURIComponent(account)}`);
      if (resp.ok) {
        const detail = await resp.json();
        openParcelDetailPanel(detail.properties || detail, {
          latlng: ev?.latlng,
          geometry: detail.geometry || feature?.geometry || null,
        });
        return;
      }
    } catch (err) {
      console.warn("[in-area-click] fresh /api/parcel fetch failed; falling back to cached props:", err);
    }
  }
  openParcelDetailPanel(p, { latlng: ev?.latlng, geometry: feature?.geometry || null });
}

function renderFeatures(geojson) {
  // Chunk E (per-view ratings §4): project every parcel's user_rating to the
  // ACTIVE view before the mark loop reads it (_maybeAddParcelRatingMark).
  // No-op when the flag is off. renderViewportFeatures delegates here, so this
  // single point covers both render modes + the view-switch re-render.
  _projectParcelRatingsForActiveView();
  const shouldRestorePopup = Boolean(_activeParcelPopupState?.accountNum);
  _isRefreshingParcelLayers = shouldRestorePopup;
  _renderedParcelPopupLayers.clear();
  _currentlyRenderedSoldAccounts.clear();
  PARCEL_LAYER_KEYS.forEach((key) => parcelTypeLayers[key]?.clearLayers());
  redfinLayer.clearLayers();
  verificationBadgeLayer.clearLayers();
  targetBadgeLayer.clearLayers();
  verificationBadgeMarkers.clear();
  targetBadgeMarkers.clear();
  outreachOverlayLayer.clearLayers();
  outreachOverlayLayerByKey.clear();
  outreachOverlayGeomSeen.clear();
  // Clear stale parcel rating marks — they get re-added below from the
  // fresh feature data (per PARCEL_RATINGS_SPEC.md v2 §4D lifecycle).
  cadRatingLayer.clearLayers();
  cadRatingLayerByKey.clear();
  const polygonGeometrySeen = new Set();
  const condoOutlineSeen = new Set();
  const accountRenderedAsPolygon = new Set(); // accounts that got a polygon fill — no dot needed
  const markers = {};
  geojson.features.forEach((feature) => {
    const p = feature.properties;
    const bucket = classifyFeatureForFilter(feature);
    // All parcels must pass Property Filters
    if (!passesNumericFilters(feature)) return;
    // Listings + sold-matched parcels must ALSO pass Comp Filters
    const isListingOrSold = Boolean(p.on_redfin || p.sold_comp);
    if (isListingOrSold && !passesCompFilters(feature)) return;
    const hasSoldComp = Boolean(p.sold_comp);
    const targetLayer = (hasSoldComp ? parcelTypeLayers.sold : parcelTypeLayers[bucket]) || markerLayer;
    if (hasSoldComp && p.account_num) {
      _currentlyRenderedSoldAccounts.add(String(p.account_num));
    }
    const color = getColor(feature);
    const borderColor = getBorderColor(feature);
    const hasVisibleSoldComp = hasSoldComp;
    const parcelBorderColor = borderColor;
    // Vacant parcels get the THICKEST border on the map (4px) so they stand
    // out at a glance. When a vacant lot is also matched to a Propelio comp,
    // the comp's footprint-glow renders on top of this polygon via a separate
    // layer, so "comp color inside, thick green ring outside" comes for free —
    // no second polygon needed.
    const parcelBorderWeight = hasVisibleSoldComp ? 5 : (p.on_redfin ? 2.8 : (bucket === "vacant" ? 4 : 1.5));
    if (p.lat == null || p.lng == null) return;
    const hasPolygonGeometry =
      feature.geometry?.type === "Polygon" || feature.geometry?.type === "MultiPolygon";
    const isCondo = isCondoParcel(p);
    const polygonKey = hasPolygonGeometry ? geometryKey(feature.geometry) : "";
    const firstPolygonInstance = Boolean(polygonKey) && !polygonGeometrySeen.has(polygonKey);
    if (firstPolygonInstance) polygonGeometrySeen.add(polygonKey);
    const duplicateNonCondoFootprint = hasPolygonGeometry && !isCondo && Boolean(polygonKey) && !firstPolygonInstance;

    // Shared rural footprints can contain multiple rows with conflicting types.
    // Render only the first non-condo footprint record so dot/outline/popup stay aligned.
    if (duplicateNonCondoFootprint) return;

    // Any duplicated polygon geometry renders only once to avoid stacked fill blocks.
    const renderPolygon = hasPolygonGeometry && !isCondo && (!polygonKey || firstPolygonInstance);
    const condoKey = isCondo && hasPolygonGeometry ? geometryKey(feature.geometry) : "";
    const renderCondoOutline = Boolean(condoKey) && !condoOutlineSeen.has(condoKey);
    if (renderCondoOutline) {
      condoOutlineSeen.add(condoKey);
    }

    // Render verification badge if already tagged
    if (p.verified_vacant && p.verified_vacant !== "") {
      renderVerificationBadge(p.account_num, p.lat, p.lng, p.verified_vacant);
    }
    if (String(p.potential_target || "").trim().toLowerCase() === "yes") {
      renderTargetBadge(p.account_num, p.lat, p.lng);
    }

    // Route each parcel to a stable per-type layer so filter toggles can
    // show/hide layers without rebuilding all geometries.
    const circleLayer = targetLayer;

    let layer;
    const condoNeedsFullRender = renderCondoOutline && (hasVisibleSoldComp || p.on_redfin);

    if (renderCondoOutline && !condoNeedsFullRender) {
      // Non-interactive visual outline for condo building footprints (no active status).
      L.geoJSON(feature, {
        renderer: MAP_CANVAS_RENDERER,
        interactive: false,
        style: {
          color: parcelBorderColor,
          fill: false,
          weight: 1.2,
          opacity: 0.75,
        },
      })
        .addTo(targetLayer);
    }

    if (renderPolygon || condoNeedsFullRender) {
      layer = L.geoJSON(feature, {
        renderer: MAP_SVG_RENDERER,
        bubblingMouseEvents: false,
        style: {
          color: parcelBorderColor,
          fillColor: p.on_redfin ? COLORS.active : color,
          fillOpacity: hasVisibleSoldComp ? 0 : (p.on_redfin ? 0.65 : 0.12),
          weight: parcelBorderWeight,
          opacity: 0.85,
        },
      });
      layer.on("click", (ev) => _openParcelDetailFromFeature(p, ev, feature));
      // L.geoJSON returns a FeatureGroup wrapper; click events fire on inner child layers,
      // so popup._source is the child, not the wrapper. Propagate metadata to children
      // so the popupopen handler can find it for suspend/restore protection.
      const polygonPopupMeta = { type: "parcel", accountNum: String(p.account_num || "") };
      layer._lotLedgerPopupMeta = polygonPopupMeta;
      layer.eachLayer((child) => { child._lotLedgerPopupMeta = polygonPopupMeta; });
      layer.addTo(targetLayer);
      if (p.account_num) {
        _renderedParcelPopupLayers.set(String(p.account_num), layer);
      }
      // No circle marker rendered when polygon geometry exists — polygon fill IS the click target.
      if (p.account_num) accountRenderedAsPolygon.add(p.account_num);
    } else {
      // Skip dot if another row for this account already rendered a polygon fill.
      if (p.account_num && accountRenderedAsPolygon.has(p.account_num)) {
        markers[p.addr] = markers[p.addr] || { layer: null, feature };
        return;
      }
      // Skip dot for condos with polygon geometry — the building outline is the click target.
      if (isCondo && hasPolygonGeometry) {
        markers[p.addr] = markers[p.addr] || { layer: null, feature };
        return;
      }
      layer = L.circleMarker([p.lat, p.lng], {
        renderer: MAP_SVG_RENDERER,
        radius: p.on_redfin ? 7 : 5,
        fillColor: p.on_redfin ? COLORS.active : color,
        color: parcelBorderColor,
        weight: 1.5,
        opacity: 1,
        fillOpacity: 0.9,
        bubblingMouseEvents: false,
      });
      layer.on("click", (ev) => _openParcelDetailFromFeature(p, ev, feature));
      layer._lotLedgerPopupMeta = { type: "parcel", accountNum: String(p.account_num || "") };
      layer.addTo(circleLayer);
      if (p.account_num) {
        _renderedParcelPopupLayers.set(String(p.account_num), layer);
      }
    }

    markers[p.addr] = { layer, feature };
  });
  _isRefreshingParcelLayers = false;
  if (shouldRestorePopup) {
    _restoreActiveParcelPopup();
  }
  // After re-render the parcelTypeLayers.sold layer was cleared, so the anchor
  // markers that price labels bind to are gone. Re-create them here so price
  // labels survive every render pass (including viewport re-renders triggered
  // by zoom/pan events).
  renderSoldPoints();
  renderRedfinPoints();

  // Parcel rating marks (red ✓ / black ✗) — per PARCEL_RATINGS_SPEC.md v2.
  // Single post-loop pass over the rendered features; uses cached polygon
  // bounds via _resolveParcelAnchor's chain. cadRatingLayer was cleared at
  // the top of this function so this rebuilds the full set cleanly.
  geojson.features.forEach((feature) => {
    const p = feature?.properties;
    if (!p) return;
    if (p.user_rating !== "good" && p.user_rating !== "bad") return;
    const fallbackLatLng = (Number.isFinite(p.lat) && Number.isFinite(p.lng))
      ? L.latLng(p.lat, p.lng)
      : null;
    _maybeAddParcelRatingMark(p, null, fallbackLatLng);
  });
  geojson.features.forEach((feature) => {
    const p = feature?.properties;
    if (!p) return;
    _maybeAddOutreachOverlay(p, feature);
  });
  return markers;
}

function renderVerificationBadge(accountNum, lat, lng, status) {
  if (!accountNum) return;
  clearVerificationBadge(accountNum);
  if (!status) return;

  const badgeIcon = L.divIcon({
    className: `verify-badge verify-badge-${status === "Yes" ? "vacant" : "not-vacant"}`,
    html: status === "Yes" ? "✓" : "✗",
    iconSize: [24, 24],
  });
  const marker = L.marker([lat, lng], { icon: badgeIcon, interactive: false }).addTo(verificationBadgeLayer);
  verificationBadgeMarkers.set(accountNum, marker);
}

function clearVerificationBadge(accountNum) {
  const marker = verificationBadgeMarkers.get(accountNum);
  if (!marker) return;
  verificationBadgeLayer.removeLayer(marker);
  verificationBadgeMarkers.delete(accountNum);
}

function renderTargetBadge(accountNum, lat, lng) {
  if (!accountNum) return;
  clearTargetBadge(accountNum);
  const badgeIcon = L.divIcon({
    className: "verify-badge verify-badge-target",
    html: "★",
    iconSize: [24, 24],
  });
  const marker = L.marker([lat, lng], { icon: badgeIcon, interactive: false }).addTo(targetBadgeLayer);
  targetBadgeMarkers.set(accountNum, marker);
}

function clearTargetBadge(accountNum) {
  const marker = targetBadgeMarkers.get(accountNum);
  if (!marker) return;
  targetBadgeLayer.removeLayer(marker);
  targetBadgeMarkers.delete(accountNum);
}

function _findParcelCoords(accountNum) {
  const features = lastAnalysisGeojson?.features;
  if (!Array.isArray(features)) return null;
  for (const feature of features) {
    const p = feature?.properties || {};
    if (String(p.account_num || "") !== String(accountNum || "")) continue;
    const lat = Number(p.lat);
    const lng = Number(p.lng);
    if (Number.isFinite(lat) && Number.isFinite(lng)) return { lat, lng };
  }
  return null;
}

function _setFeatureTag(accountNum, key, value) {
  const features = lastAnalysisGeojson?.features;
  if (!Array.isArray(features)) return;
  for (const feature of features) {
    const p = feature?.properties || {};
    if (String(p.account_num || "") === String(accountNum || "")) {
      p[key] = value;
    }
  }
}

function setVerification(accountNum, value, lat = null, lng = null) {
  if (!accountNum) return;
  const normalized = normalizeVerificationValue(value);
  verificationByAccount.set(accountNum, normalized);
  _setFeatureTag(accountNum, "verified_vacant", normalized);
  if (!normalized) {
    clearVerificationBadge(accountNum);
    return;
  }
  let markerLat = Number(lat);
  let markerLng = Number(lng);
  if (!Number.isFinite(markerLat) || !Number.isFinite(markerLng)) {
    const coords = _findParcelCoords(accountNum);
    if (!coords) return;
    markerLat = coords.lat;
    markerLng = coords.lng;
  }
  renderVerificationBadge(accountNum, markerLat, markerLng, normalized);
}

function setTarget(accountNum, enabled, lat = null, lng = null) {
  if (!accountNum) return;
  const normalized = enabled ? "Yes" : "";
  potentialTargetByAccount.set(accountNum, normalized);
  _setFeatureTag(accountNum, "potential_target", normalized);
  if (!enabled) {
    clearTargetBadge(accountNum);
    return;
  }
  let markerLat = Number(lat);
  let markerLng = Number(lng);
  if (!Number.isFinite(markerLat) || !Number.isFinite(markerLng)) {
    const coords = _findParcelCoords(accountNum);
    if (!coords) return;
    markerLat = coords.lat;
    markerLng = coords.lng;
  }
  renderTargetBadge(accountNum, markerLat, markerLng);
}

function persistSingleTag(accountNum, field, value) {
  if (!currentJobId || !accountNum) return;
  fetch("/api/tags/set", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({
      job_id: currentJobId,
      account_num: accountNum,
      field,
      value,
    }),
  }).catch(() => {});
}

function applyResultTags(result) {
  if (!result || !result.tags || typeof result.tags !== "object") return;
  Object.entries(result.tags).forEach(([account_num, t]) => {
    if (!t || typeof t !== "object") return;
    if (Object.prototype.hasOwnProperty.call(t, "verified_vacant")) {
      setVerification(account_num, t.verified_vacant || "");
    }
    if (Object.prototype.hasOwnProperty.call(t, "potential_target")) {
      setTarget(account_num, String(t.potential_target || "").trim().toLowerCase() === "yes");
    }
  });
}

// Render only the features whose centroid falls within the current map viewport.
// Called on moveend/zoomend when viewportRenderMode is true (large draw result).
function renderViewportFeatures() {
  if (!viewportRenderMode || !allAnalysisFeatures) return;
  const bounds = map.getBounds().pad(0.15);
  const visible = allAnalysisFeatures.filter(f => {
    const p = f.properties || {};
    if (p.lat == null || p.lng == null) return false;
    return bounds.contains([p.lat, p.lng]);
  });
  renderFeatures({ type: "FeatureCollection", features: visible });
  if (redfinLayerVisible) redfinLayer.addTo(map); else map.removeLayer(redfinLayer);
}

// Debounced wrapper so pan/zoom events don't fire renderViewportFeatures dozens of times.
function _scheduleViewportRender() {
  clearTimeout(_vpRenderTimeout);
  const delay = Math.max(150, _suspendViewportRenderUntil - Date.now());
  _vpRenderTimeout = setTimeout(renderViewportFeatures, delay);
}

function renderSidebar(counts, markers) {
  document.getElementById("sidebar-loading").classList.add("hidden");
  document.getElementById("sidebar-results").classList.remove("hidden");
  document.getElementById("active-item-actions")?.classList.remove("hidden");
  _renderViewToggle();  // reveal ARV/NBV/Export toggle alongside the workspace actions (flag-gated; self-hides if no area loaded)
  _updateActiveItemRenameVisibility();  // reveal rename pencil + share button now the area + cache are ready (idempotent)
  const visibleCounts = Array.isArray(allAnalysisFeatures) && allAnalysisFeatures.length
    ? getVisibleFeatureCounts(allAnalysisFeatures, { ignoreBucketToggles: true })
    : {
      active: counts.active,
      off_market: counts.off_market,
      vacant: counts.vacant,
      multifamily: counts.multifamily,
      duplexes: counts.duplexes,
      commercial: counts.commercial,
      exempt: counts.exempt,
      contact_status: counts.contact_status,
    };
  const soldCount = Array.isArray(lastSoldPanelPoints) && lastSoldPanelPoints.length
    ? lastSoldPanelPoints.length
    : (Array.isArray(allSoldPointsRef) ? allSoldPointsRef.length : 0);
  const orderedCountRows = [
    ["active", visibleCounts.active],
    ["sold", soldCount],
    ["contact_status", visibleCounts.contact_status],
    ["off_market", visibleCounts.off_market],
    ["vacant", visibleCounts.vacant],
    ["multifamily", visibleCounts.multifamily],
    ["duplexes", visibleCounts.duplexes],
    ["commercial", visibleCounts.commercial],
    ["exempt", visibleCounts.exempt],
  ];

  orderedCountRows.forEach(([key, val]) => {
    const countEl = document.getElementById(`filter-count-${key}`);
    if (countEl) countEl.textContent = String(Number(val) || 0);
  });

  renderSoldCompsPanel();

}

function makeDefaultCsvName() {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");

  // K5 (2026-05-27 roadmap): when a saved workspace is loaded, default
  // the CSV filename to "<workspace-name>_<YYYY-MM-DD>_<HHMMSS>.csv"
  // instead of "lotledger_…". Falls back to "lotledger_" for
  // analysis-only exports (no loaded workspace). User can still edit in
  // the prompt before confirming.
  let prefix = "lotledger";
  if (_currentLoadedAreaId && Array.isArray(_savedAreasCache)) {
    const loaded = _savedAreasCache.find((a) => String(a.id) === String(_currentLoadedAreaId));
    const loadedName = String(loaded?.name || "").trim();
    if (loadedName) prefix = loadedName;
  }

  const stamp = [
    now.getFullYear(),
    "-",
    pad(now.getMonth() + 1),
    "-",
    pad(now.getDate()),
    "_",
    pad(now.getHours()),
    pad(now.getMinutes()),
    pad(now.getSeconds()),
  ].join("");
  return `${prefix}_${stamp}.csv`;
}

function normalizeCsvFilename(rawName) {
  const fallback = makeDefaultCsvName();
  if (!rawName) return fallback;
  const cleaned = rawName
    .trim()
    .replace(/[\\/]/g, " ")
    .replace(/[^a-zA-Z0-9._-]+/g, "_")
    .replace(/^[._\s]+|[._\s]+$/g, "");

  if (!cleaned) return fallback;

  const stem = cleaned.toLowerCase().endsWith(".csv") ? cleaned.slice(0, -4) : cleaned;
  const safeStem = (stem || "parcels").slice(0, 96).replace(/[._\s]+$/g, "") || "parcels";
  return `${safeStem}.csv`;
}

// ---------------------------------------------------------------------------
// Phase 2: tile splitting for large-area draws
// ---------------------------------------------------------------------------
const TILE_AREA_THRESHOLD = 0.003; // sq-degrees; split bbox above this area
const TILE_MAX_SPLIT_DEPTH = 4; // max adaptive refinement depth per failing tile

function getPolygonBbox(polygon) {
  let minLng = Infinity, minLat = Infinity, maxLng = -Infinity, maxLat = -Infinity;
  for (const [lng, lat] of polygon) {
    if (lng < minLng) minLng = lng;
    if (lat < minLat) minLat = lat;
    if (lng > maxLng) maxLng = lng;
    if (lat > maxLat) maxLat = lat;
  }
  return { minLng, minLat, maxLng, maxLat };
}

function bboxArea(bbox) {
  return (bbox.maxLng - bbox.minLng) * (bbox.maxLat - bbox.minLat);
}

// Split bbox into an n×n grid of tiles.
function splitBboxIntoNxN(bbox, n) {
  const lngStep = (bbox.maxLng - bbox.minLng) / n;
  const latStep = (bbox.maxLat - bbox.minLat) / n;
  const tiles = [];
  for (let row = 0; row < n; row++) {
    for (let col = 0; col < n; col++) {
      tiles.push({
        minLng: bbox.minLng + col * lngStep,
        maxLng: bbox.minLng + (col + 1) * lngStep,
        minLat: bbox.minLat + row * latStep,
        maxLat: bbox.minLat + (row + 1) * latStep,
      });
    }
  }
  return tiles;
}

// Always-2×2 split used during adaptive tile refinement.
function splitBboxIntoTiles(bbox) {
  return splitBboxIntoNxN(bbox, 2);
}

// Choose initial grid size based on bbox area so huge draws start with more tiles.
// Each tile should stay well under Cloud Run's 60s timeout.
function getInitialTileGrid(bbox) {
  const area = bboxArea(bbox);
  if (area > 0.25) return splitBboxIntoNxN(bbox, 9); // multi-county / all-county → 81 tiles
  if (area > 0.1)  return splitBboxIntoNxN(bbox, 7); // county-scale → 49 tiles
  if (area > 0.04) return splitBboxIntoNxN(bbox, 5); // large metro area → 25 tiles
  if (area > 0.015) return splitBboxIntoNxN(bbox, 4); // very large → 16 tiles
  if (area > 0.006) return splitBboxIntoNxN(bbox, 3); // large → 9 tiles
  return splitBboxIntoTiles(bbox);                     // normal → 4 tiles
}

function tileToPolygon(tile) {
  return [
    [tile.minLng, tile.minLat],
    [tile.maxLng, tile.minLat],
    [tile.maxLng, tile.maxLat],
    [tile.minLng, tile.maxLat],
    [tile.minLng, tile.minLat],
  ];
}

// Ray-cast point-in-polygon. ring is [[lng, lat], ...] (GeoJSON order).
function pointInPolygonLngLat([lng, lat], ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if ((yi > lat) !== (yj > lat) && lng < (xj - xi) * (lat - yi) / (yj - yi) + xi) {
      inside = !inside;
    }
  }
  return inside;
}

// Returns a representative [lng, lat] centroid for a GeoJSON feature.
// Falls back to properties.lng/lat if geometry is unavailable.
function featureCentroidLngLat(feature) {
  const p = feature.properties || {};
  const geom = feature.geometry;
  if (geom?.type === "Point") return geom.coordinates;
  if (geom?.type === "Polygon" && geom.coordinates[0]?.length) {
    const ring = geom.coordinates[0];
    let x = 0, y = 0;
    for (const [px, py] of ring) { x += px; y += py; }
    return [x / ring.length, y / ring.length];
  }
  if (geom?.type === "MultiPolygon" && geom.coordinates[0]?.[0]?.length) {
    const ring = geom.coordinates[0][0];
    let x = 0, y = 0;
    for (const [px, py] of ring) { x += px; y += py; }
    return [x / ring.length, y / ring.length];
  }
  if (p.lng != null && p.lat != null) return [p.lng, p.lat];
  return null;
}

// Test point for client-side polygon clipping. Prefer the STORED parcel
// centroid (properties.lng/lat) because the server filters parcels by
// stored centroid-in-polygon; fall back to the geometry centroid. Keeping
// parity with the server minimizes reshape reconcile diffs.
function testPointLngLat(feature) {
  const p = feature?.properties || {};
  if (p.lng != null && p.lat != null) return [Number(p.lng), Number(p.lat)];
  return featureCentroidLngLat(feature);
}

function reshapeFeatureKey(f) {
  const p = f?.properties || {};
  const county = String(p.source_county || "").trim().toLowerCase();
  const id = county === "dcad"
    ? String(p.account_num || "").trim()
    : String(p.parcel_key || p.account_num || "").trim();
  return `${county}::${id}`;
}

// True if two feature arrays contain the same parcel identities (order-independent).
function reshapeSetsEqual(a, b) {
  if (!Array.isArray(a) || !Array.isArray(b)) return false;
  if (a.length !== b.length) return false;
  const sa = new Set(a.map(reshapeFeatureKey));
  if (sa.size !== a.length) { /* dupes — fall through to size-safe compare */ }
  for (const f of b) { if (!sa.has(reshapeFeatureKey(f))) return false; }
  return sa.size === new Set(b.map(reshapeFeatureKey)).size;
}

function _sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function _computeRetryDelayMs(attemptIndex) {
  const base = 450 * Math.pow(2, attemptIndex);
  const jitter = Math.floor(Math.random() * 200);
  return base + jitter;
}

function _isRetryableStatus(status) {
  return [500, 502, 503, 504].includes(status);
}

async function _parseJsonResponse(resp, endpoint) {
  let rawBody = "";
  try {
    rawBody = await resp.text();
  } catch {
    return {
      ok: false,
      error: {
        code: "BODY_READ_FAILED",
        endpoint,
        status: resp.status,
        retryable: false,
        userMessage: "We could not read the server response. Please try again.",
      },
    };
  }

  if (!rawBody || !rawBody.trim()) {
    return {
      ok: false,
      error: {
        code: "EMPTY_RESPONSE_BODY",
        endpoint,
        status: resp.status,
        retryable: false,
        userMessage: "Server returned an empty response. Please try again.",
      },
    };
  }

  try {
    return { ok: true, data: JSON.parse(rawBody) };
  } catch {
    return {
      ok: false,
      error: {
        code: "INVALID_JSON_RESPONSE",
        endpoint,
        status: resp.status,
        retryable: false,
        userMessage: "Server returned malformed data. Please try again.",
      },
    };
  }
}

function _buildApiError(resp, endpoint, parsedData = null) {
  const detail = parsedData?.detail;
  const detailText = typeof detail === "string" ? detail : "";
  const userMessage = detailText
    ? `Server error (${resp.status}): ${detailText}`
    : resp.status >= 500
      ? `Server is temporarily busy (${resp.status}). Please try again.`
      : `Request failed (${resp.status}).`;
  return {
    code: "HTTP_ERROR",
    endpoint,
    status: resp.status,
    retryable: _isRetryableStatus(resp.status),
    userMessage,
    detail: detailText,
  };
}

function _throwStructuredAnalysisError(apiError, fallbackMessage) {
  const err = new Error(apiError?.userMessage || fallbackMessage);
  err.code = apiError?.code || "ANALYSIS_REQUEST_FAILED";
  err.status = apiError?.status;
  err.userMessage = apiError?.userMessage || fallbackMessage;
  err.apiError = apiError;
  throw err;
}

async function postJsonWithRetry(endpoint, payload, options = {}) {
  const {
    signal,
    maxRetries = 2,
    statusElement,
    retryMessageBuilder,
  } = options;

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    let resp;
    try {
      resp = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify(payload),
        signal,
      });
    } catch (err) {
      if (err?.name === "AbortError") {
        const aborted = new Error("Analysis request canceled");
        aborted.name = "AbortError";
        aborted.code = "ANALYSIS_ABORTED";
        throw aborted;
      }
      _throwStructuredAnalysisError(
        {
          code: "NETWORK_ERROR",
          endpoint,
          retryable: false,
          userMessage: "Network error while contacting the server. Please try again.",
        },
        "Network error while contacting the server."
      );
    }

    const parsed = await _parseJsonResponse(resp, endpoint);
    if (resp.ok && parsed.ok) return parsed.data;

    let apiError;
    if (!parsed.ok) {
      apiError = parsed.error;
      if (_isRetryableStatus(resp.status)) {
        apiError.retryable = true;
        apiError.userMessage = `Server is temporarily busy (${resp.status}). Please try again.`;
      }
    } else {
      apiError = _buildApiError(resp, endpoint, parsed.data);
    }

    if (apiError.retryable && attempt < maxRetries) {
      const waitMs = _computeRetryDelayMs(attempt);
      if (statusElement && typeof retryMessageBuilder === "function") {
        statusElement.textContent = retryMessageBuilder(attempt + 1, maxRetries + 1, waitMs, apiError.status);
      }
      await _sleep(waitMs);
      continue;
    }

    _throwStructuredAnalysisError(
      apiError,
      `Request to ${endpoint} failed.`
    );
  }

  _throwStructuredAnalysisError(
    {
      code: "RETRY_EXHAUSTED",
      endpoint,
      retryable: false,
      userMessage: "Server is still busy after multiple attempts. Please try a smaller area.",
    },
    "Server is still busy after multiple attempts."
  );
}

async function fetchTileDataRecursive(tilePolygon, includeRedfin, includeSold, depth, tileLabel, options = {}) {
  const { signal, areaId } = options;
  const redfinStatus = document.getElementById("redfin-status");
  let data;
  try {
    data = await postJsonWithRetry(
      "/api/analyze",
      { polygon: tilePolygon, include_redfin: includeRedfin, include_sold: includeSold, area_id: areaId || null },
      {
        signal,
        maxRetries: 2,
        statusElement: redfinStatus,
        retryMessageBuilder: (attempt, total, waitMs, status) =>
          `Tile ${tileLabel} temporary server error (${status}). Retry ${attempt}/${total} in ${(waitMs / 1000).toFixed(1)}s...`,
      }
    );
  } catch (err) {
    if (isAbortError(err)) throw err;
    if (_isRetryableStatus(err?.status) && depth < TILE_MAX_SPLIT_DEPTH) {
      if (redfinStatus) {
        redfinStatus.textContent = `Tile ${tileLabel} still failing — splitting into subtiles...`;
      }
      const subTiles = splitBboxIntoTiles(getPolygonBbox(tilePolygon));
      const nestedResults = [];
      for (let i = 0; i < subTiles.length; i++) {
        await _sleep(500); // breathe between subtiles
        const subLabel = `${tileLabel}.${i + 1}`;
        const subPolygon = tileToPolygon(subTiles[i]);
        const subData = await fetchTileDataRecursive(subPolygon, includeRedfin, includeSold, depth + 1, subLabel, options);
        nestedResults.push(...subData);
      }
      return nestedResults;
    }
    throw err;
  }

  if (data.source_status && (!data.source_status.dcad_ok || !data.source_status.tad_ok)) {
    const err = new Error(`Incomplete county result on tile ${tileLabel}`);
    err.code = "INCOMPLETE_COUNTY_RESULT";
    err.status = 502;
    err.userMessage = `Tile ${tileLabel} returned incomplete county data.`;
    throw err;
  }
  return [data];
}

async function runTiledAnalysis(polygon, includeRedfin, includeSold, options = {}) {
  const bbox = getPolygonBbox(polygon);
  const tiles = getInitialTileGrid(bbox);
  const tileJobIds = [];
  const allFeatures = [];
  const seenParcelKeys = new Set();
  let anyRedfinOk = false;
  let anyRedfinSkipped = false;
  let lastSourceStatus = null;
  const soldPoints = [];
  const soldPointKeys = new Set();

  // Fetch tiles in parallel batches. Each tile uses 2 DB connections (DCAD + TAD);
  // cap at 8 concurrent tiles so we stay under the pool limit of 20.
  const PARALLEL_LIMIT = 8;
  for (let batchStart = 0; batchStart < tiles.length; batchStart += PARALLEL_LIMIT) {
    const batch = tiles.slice(batchStart, batchStart + PARALLEL_LIMIT);
    const batchEnd = Math.min(batchStart + PARALLEL_LIMIT, tiles.length);
    document.getElementById("redfin-status").textContent =
      tiles.length <= PARALLEL_LIMIT
        ? `Loading ${tiles.length} tile${tiles.length > 1 ? "s" : ""} in parallel...`
        : `Loading tiles ${batchStart + 1}–${batchEnd} of ${tiles.length}...`;

    const batchResults = await Promise.all(
      batch.map((tile, idx) =>
        fetchTileDataRecursive(tileToPolygon(tile), includeRedfin, includeSold, 0, `${batchStart + idx + 1}`, options)
      )
    );

    for (const tileDataList of batchResults) {
      for (const data of tileDataList) {
        tileJobIds.push(data.job_id);
        lastSourceStatus = data.source_status;
        if (data.redfin_ok) anyRedfinOk = true;
        if (data.redfin_skipped) anyRedfinSkipped = true;
        for (const feature of data.features) {
          const key = feature.properties?.parcel_key || feature.properties?.account_num;
          if (key && seenParcelKeys.has(key)) continue;
          if (key) seenParcelKeys.add(key);
          allFeatures.push(feature);
        }
        for (const point of data.sold_points || []) {
          const key = String(point.listing_url || `${point.lat},${point.lng},${point.sold_date || ""}`);
          if (soldPointKeys.has(key)) continue;
          soldPointKeys.add(key);
          soldPoints.push(point);
        }
      }
    }
  }

  // Strip parcels outside the drawn polygon — tile bboxes are rectangles that
  // extend beyond irregular drawn shapes; server filters to the tile rect, not the draw.
  const filteredFeatures = allFeatures.filter(f => {
    const pt = featureCentroidLngLat(f);
    return pt ? pointInPolygonLngLat(pt, polygon) : true;
  });

  const filteredSoldPoints = soldPoints.filter((point) => {
    const lng = Number(point.lng);
    const lat = Number(point.lat);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) return false;
    return pointInPolygonLngLat([lng, lat], polygon);
  });

  // Recount from deduplicated + clipped features
  const mergedCounts = { active: 0, off_market: 0, multifamily: 0, duplexes: 0, vacant: 0, commercial: 0, exempt: 0, total: filteredFeatures.length };
  for (const feature of filteredFeatures) {
    const p = feature.properties || {};
    if (p.on_redfin) mergedCounts.active++;
    else if (p.prop_type === "multifamily") mergedCounts.multifamily++;
    else if (p.prop_type === "duplexes") mergedCounts.duplexes++;
    else if (p.prop_type === "vacant") mergedCounts.vacant++;
    else if (p.prop_type === "commercial") mergedCounts.commercial++;
    else if (p.prop_type === "exempt") mergedCounts.exempt++;
    else mergedCounts.off_market++;
  }

  // Merge server-side so export + verification have a single stable job_id
  const mergeData = await postJsonWithRetry(
    "/api/merge-jobs",
    { job_ids: tileJobIds, area_id: options.areaId || null },
    {
      signal: options.signal,
      maxRetries: 2,
      statusElement: document.getElementById("redfin-status"),
      retryMessageBuilder: (attempt, total, waitMs) =>
        `Finalizing merged result (attempt ${attempt}/${total}) in ${(waitMs / 1000).toFixed(1)}s...`,
    }
  );

  return {
    type: "FeatureCollection",
    features: filteredFeatures,
    counts: mergedCounts,
    sold_points: filteredSoldPoints,
    job_id: mergeData.job_id,
    redfin_requested: includeRedfin,
    redfin_ok: anyRedfinOk,
    redfin_skipped: anyRedfinSkipped,
    source_status: lastSourceStatus,
    tiled: true,
  };
}

async function runAnalysis(polygon, includeRedfin, includeSold, options = {}) {
  const resolvedAreaId = (Object.prototype.hasOwnProperty.call(options, "areaId") ? options.areaId : _currentLoadedAreaId) || null;
  const requestOptions = { ...options, areaId: resolvedAreaId };
  const bbox = getPolygonBbox(polygon);
  if (bboxArea(bbox) > TILE_AREA_THRESHOLD) {
    return runTiledAnalysis(polygon, includeRedfin, includeSold, requestOptions);
  }
  return postJsonWithRetry(
    "/api/analyze",
    { polygon, include_redfin: includeRedfin, include_sold: includeSold, area_id: resolvedAreaId },
    {
      signal: requestOptions.signal,
      maxRetries: 2,
      statusElement: document.getElementById("redfin-status"),
      retryMessageBuilder: (attempt, total, waitMs, status) =>
        `Analysis retry ${attempt}/${total} after server error (${status}) in ${(waitMs / 1000).toFixed(1)}s...`,
    }
  );
}
// ---------------------------------------------------------------------------

async function refreshExpiredJob() {
  if (!lastPolygon || lastPolygon.length < 3) return false;
  const includeRedfin = Boolean(filterState.active);
  const includeSold = Boolean(filterState.sold);
  try {
    const data = await runAnalysis(lastPolygon, includeRedfin, includeSold);
    if (data.source_status && (!data.source_status.dcad_ok || !data.source_status.tad_ok)) {
      return false;
    }
    currentJobId = data.job_id || null;
    return Boolean(currentJobId);
  } catch {
    return false;
  }
}

async function persistTagStateForExport(statusUpdater = null) {
  const setStatus = (text) => {
    if (typeof statusUpdater === "function") statusUpdater(text);
  };

  if (!currentJobId) return false;
  setStatus("Saving tags…");

  const payload = {};
  verificationByAccount.forEach((value, accountNum) => {
    payload[accountNum] = String(value || "").toLowerCase();
  });
  const targetPayload = {};
  potentialTargetByAccount.forEach((value, accountNum) => {
    targetPayload[accountNum] = String(value || "").toLowerCase();
  });

  let resp;
  try {
    resp = await fetch(`/api/job/${currentJobId}/verification`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ verifications: payload, potential_targets: targetPayload }),
    });
  } catch {
    return false;
  }

  if (resp.ok) return true;
  if (resp.status !== 404) return false;

  setStatus("Session expired - re-running analysis…");

  try {
    const refreshed = await refreshExpiredJob();
    if (!refreshed) return false;
    setStatus("Saving tags…");

    try {
      const retry = await fetch(`/api/job/${currentJobId}/verification`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ verifications: payload, potential_targets: targetPayload }),
      });
      return retry.ok;
    } catch {
      return false;
    }
  } catch {
    return false;
  }
}

let _downloadInFlight = false;

function _setDownloadButtonState(label, disabled = true) {
  const btn = document.getElementById("btn-download");
  if (!btn) return;
  btn.textContent = label;
  btn.disabled = disabled;
}

function _resetDownloadButtonState() {
  const btn = document.getElementById("btn-download");
  if (!btn) return;
  btn.textContent = "Download CSV";
  btn.disabled = false;
}

map.on("draw:created", async (e) => {
  bumpUndoPillVersion();
  closeTransientSoldSidebarPopup();
  map.getContainer().classList.remove("drawing-active");
  const reshapeTarget = _reshapeTargetAreaId;
  _reshapeTargetAreaId = null;
  if (!reshapeTarget) {
    _currentSessionIsNamed = false;
    // Intentionally NOT clearing _currentTargetParcel or _originatorStarMarker
    // here — saveCurrentArea (called downstream via _autoCacheOnDraw) needs
    // to read _currentTargetParcel as the originator for the new workspace.
    // saveCurrentArea handles the clear-and-re-render with the bonded value
    // after the area is persisted.
    _currentLoadedAreaId = null;
    _syncTabTitle();
    _storedValueOnAreaChange(null);
    void _filterSaveOnAreaChange(null);
    _selectedSavedItemId = null;
    _setSessionCacheNote("");
    renderSavedAreasList();
  }
  drawLayer.clearLayers();
  PARCEL_LAYER_KEYS.forEach((key) => parcelTypeLayers[key]?.clearLayers());
  redfinLayer.clearLayers();
  soldLayer.clearLayers();
  verificationBadgeLayer.clearLayers();
  targetBadgeLayer.clearLayers();
  verificationByAccount.clear();
  potentialTargetByAccount.clear();
  verificationBadgeMarkers.clear();
  targetBadgeMarkers.clear();
  // Instructions section removed; results manage visibility
  document.getElementById("sidebar-results").classList.add("hidden");
  document.getElementById("active-item-actions")?.classList.add("hidden");
  document.getElementById("sidebar-loading").classList.remove("hidden");
  const includeRedfin = Boolean(filterState.active);
  const includeSold = Boolean(filterState.sold);
  document.getElementById("redfin-status").textContent = "Running analysis...";
  drawLayer.addLayer(e.layer);

  // Spotlight mask — dim everything outside the drawn polygon.
  maskLayer.clearLayers();
  const _worldRing = [[-90, -180], [-90, 180], [90, 180], [90, -180], [-90, -180]];
  const _holeRing = e.layer.getLatLngs()[0].map(ll => [ll.lat, ll.lng]);
  L.polygon([_worldRing, _holeRing], {
    fillColor: "#000000",
    fillOpacity: 0.32,
    stroke: false,
    interactive: false,
  }).addTo(maskLayer);

  const polygon = e.layer.getLatLngs()[0].map((ll) => [ll.lng, ll.lat]);
  lastDrawnLatLngs = e.layer.getLatLngs()[0].map((ll) => [ll.lat, ll.lng]);
  lastPolygon = polygon;
  // Clear any Propelio comps + chip from a prior polygon — those comps
  // were filtered to a different shape and shouldn't linger when the
  // user redraws. Parcel rating layer is left alone here — renderFeatures
  // will reconcile it once the new analyze response lands.
  propelioCompLayer.clearLayers();
  propelioCompLayerByKey.clear();
  renderPropelioCompList([]);
  propelioCmaChip.hide();
  if (reshapeTarget) {
    // Re-scope already-loaded comps to the new polygon instantly (client-side,
    // zero DB / zero Propelio). OAC comps inside the new shape appear
    // automatically because the OAC gate re-tests every comp.
    _buildNbhdOptionsCache();
    applyPropelioClientFilters();
    // Instant parcels: clip the already-loaded parcels to the new polygon and
    // render them now (display-only; runAnalysis below reconciles + is authoritative).
    // Flag-gated; reshape-only; skipped for large/browse sets (those keep today's path).
    _reshapeOptimisticApplied = false;
    _preReshapeFeatures = null;
    _reshapeClippedSubset = null;
    if (
      INSTANT_RESHAPE_ENABLED &&
      Array.isArray(allAnalysisFeatures) &&
      allAnalysisFeatures.length &&
      allAnalysisFeatures.length <= BROWSE_ONLY_THRESHOLD &&
      Array.isArray(lastPolygon) && lastPolygon.length >= 3
    ) {
      _preReshapeFeatures = allAnalysisFeatures;
      const clipped = allAnalysisFeatures.filter((f) => {
        const pt = testPointLngLat(f);
        return pt && pointInPolygonLngLat(pt, lastPolygon);
      });
      // Render instantly at the matching scale, mirroring the reconcile branch
      // (~12294-12303): viewport render above the large-draw threshold, normal
      // polygon render below it. clipped can't exceed BROWSE_ONLY_THRESHOLD
      // because the outer guard caps allAnalysisFeatures at it.
      _reshapeClippedSubset = clipped;
      _reshapeOptimisticApplied = true;
      allAnalysisFeatures = clipped;
      if (map.hasLayer(browseLayer)) browseLayer.remove();
      let markers;
      if (clipped.length > LARGE_DRAW_THRESHOLD) {
        viewportRenderMode = true;
        renderViewportFeatures();
        markers = {};
      } else {
        viewportRenderMode = false;
        markers = renderFeatures({ type: "FeatureCollection", features: clipped });
      }
      renderSidebar(getVisibleFeatureCounts(clipped, { ignoreBucketToggles: true }), markers);
    }
    // Persist the new polygon to the loaded area (owner-only).
    try {
      await _apiJson(`/api/areas/${encodeURIComponent(reshapeTarget)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ polygon: lastDrawnLatLngs }),
      });
      // Refresh the saved-areas cache so reopening the area shows the NEW shape.
      // A polygon-only PUT doesn't auto-update _savedAreasCache, so without this
      // reopening from the sidebar restores the stale cached polygon (mirrors
      // what the create path does via _reloadSavedResources).
      await _reloadSavedResources();
    } catch (err) {
      console.warn("[reshape] PUT polygon failed:", err);
    }
  } else {
    // Await the autosave so _currentLoadedAreaId is set before runAnalysis
    // fires.  Without this, /api/analyze receives area_id: null and the new
    // cached_jobs row is born with saved_area_id = NULL → Stored Values columns
    // come back empty on CSV export.  The cache pre-warm inside
    // _autoCacheOnDraw is fire-and-forget, so this only adds the ~100ms
    // POST /api/areas round-trip before analysis starts — invisible against
    // the several-second analysis itself.
    await _autoCacheOnDraw();
  }
  _showPropelioPolygonButton(e.layer.getLatLngs()[0]);
  const analysisRequest = beginLatestAnalysisRequest();

  try {
    const data = await runAnalysis(polygon, includeRedfin, includeSold, { signal: analysisRequest.signal });
    if (!isActiveAnalysisRequest(analysisRequest.requestId)) return;
    if (data.source_status && (!data.source_status.dcad_ok || !data.source_status.tad_ok)) {
      throw new Error("Incomplete county result set returned; analysis canceled to prevent partial export.");
    }
    currentJobId = data.job_id;
    data.features.forEach((feature) => {
      const p = feature.properties || {};
      if (!p.account_num) return;
      const normalized = normalizeVerificationValue(p.verified_vacant);
      verificationByAccount.set(p.account_num, normalized);
      p.verified_vacant = normalized;
      const potential = String(p.potential_target || "").trim().toLowerCase() === "yes" ? "Yes" : "";
      potentialTargetByAccount.set(p.account_num, potential);
      p.potential_target = potential;
    });
    document.getElementById("redfin-status").textContent = "Analysis complete";
    _updateSaveSessionButtonState();
    if (!_currentLoadedAreaId) {
      setActiveItem("Unsaved area", "Unsaved");
    }
    redfinLayerVisible = false;
    soldLayerVisible = Boolean(filterState.sold);
    map.removeLayer(redfinLayer);
    if (soldLayerVisible) {
      soldLayer.addTo(map);
    } else {
      map.removeLayer(soldLayer);
    }
    lastIncludedRedfin = true;
    lastIncludedSold = includeSold;
    lastSoldPoints = Array.isArray(data.sold_points) ? data.sold_points : [];
    allSoldPointsRef = [...lastSoldPoints];
    lastAnalysisGeojson = data;
    lastAnalysisCounts = data.counts;
    allAnalysisFeatures = data.features;
    if (data.features.length <= BROWSE_ONLY_THRESHOLD) {
      const soldJoin = attachSoldCompsToFeatures(allAnalysisFeatures, lastSoldPoints);
      lastSoldPoints = soldJoin.unmatchedSoldPoints;
      matchedSoldLabelPoints = soldJoin.matchedLabelPoints || [];
      data.sold_points = lastSoldPoints;
    } else {
      matchedSoldLabelPoints = [];
    }
    applyAndRenderSoldFilters();
    document.getElementById("btn-draw-clear")?.classList.remove("hidden");

    let markers;
    if (data.features.length > BROWSE_ONLY_THRESHOLD) {
      // Too many parcels to render as Leaflet polygons — keep browse layer on.
      // The spotlight mask is already applied so outside is dimmed; browse layer
      // shows the parcel detail as the user zooms in, same as normal browse mode.
      viewportRenderMode = false;
      markers = {};
    } else {
      // Under threshold — hide browse layer and render Leaflet polygons.
      if (map.hasLayer(browseLayer)) browseLayer.remove();
      if (data.features.length > LARGE_DRAW_THRESHOLD) {
        viewportRenderMode = true;
        renderViewportFeatures();
        markers = {};
      } else {
        viewportRenderMode = false;
        markers = renderFeatures(data);
      }
    }
    renderSidebar(data.counts, markers);
    applyResultTags(data);
    const soldStatus = document.getElementById("sold-toggle-status");
    if (soldStatus) updateSoldStatusText();
    applyAndRenderSoldFilters();
    applyMapVisibilityFilters();
  } catch (err) {
    if (isAbortError(err) || !isActiveAnalysisRequest(analysisRequest.requestId)) return;
    console.error("[draw:created] Analysis failed:", err);
    document.getElementById("redfin-status").textContent = getAnalysisErrorMessage(err, "Analysis failed. Please try drawing a smaller area.");
    document.getElementById("sidebar-loading").classList.add("hidden");
    // Instructions section removed
    document.getElementById("btn-draw-clear")?.classList.remove("hidden");
  }
});

function clearDrawResults() {
  closeTransientSoldSidebarPopup();
  viewportRenderMode = false;
  allAnalysisFeatures = null;
  clearTimeout(_vpRenderTimeout);
  drawLayer.clearLayers();
  maskLayer.clearLayers();
  _removePropelioPolygonButton();
  if (!map.hasLayer(browseLayer)) browseLayer.addTo(map);
  PARCEL_LAYER_KEYS.forEach((key) => parcelTypeLayers[key]?.clearLayers());
  redfinLayer.clearLayers();
  soldLayer.clearLayers();
  verificationBadgeLayer.clearLayers();
  targetBadgeLayer.clearLayers();
  verificationByAccount.clear();
  potentialTargetByAccount.clear();
  verificationBadgeMarkers.clear();
  targetBadgeMarkers.clear();
  // Propelio surface: footprints/dots/checkmarks on the map, the
  // sidebar comp list, the CMA chip, and the count chip on the card
  // header. The archive in the DB is untouched — clicking the saved
  // workspace again rehydrates everything exactly as it was.
  propelioCompLayer.clearLayers();
  cadRatingLayer.clearLayers();
  cadRatingLayerByKey.clear();
  propelioCompLayerByKey.clear();
  // Mike report 2026-06-05: blue phone overlays were lingering on the
  // map after Clear. Same pattern as the CAD rating + propelio comp
  // layers above — Clear must wipe the live-overlay state we attach to
  // the saved area, otherwise the icons stay glued to coordinates with
  // no underlying parcel.
  outreachOverlayLayer.clearLayers();
  outreachOverlayLayerByKey.clear();
  outreachOverlayGeomSeen.clear();
  window._propelioLast = null;
  _updatePropelioStatusCounts();
  propelioCmaChip.hide();
  renderPropelioCompList([]);
  const _propelioCountEl = document.getElementById("propelio-filter-count");
  if (_propelioCountEl) _propelioCountEl.textContent = "";
  currentJobId = null;
  lastPolygon = null;
  lastDrawnLatLngs = null;
  _currentSessionIsNamed = false;
  _clearOriginatorStar();
  _setCurrentTargetParcel(null);
  _currentLoadedAreaId = null;
  _renderViewToggle();  // no area loaded → hide the ARV/NBV/Export toggle
  // Sprint 2 §5.3: no area loaded -> no snapshot baseline + clear queue.
  _filterSaveLastSnapshot = null;
  _filterSavePendingFields.clear();
  // Sprint 3 §4.3: close any open SSE stream.
  if (typeof _closeSseStream === "function") _closeSseStream();
  _syncTabTitle();
  _storedValueOnAreaChange(null);
  void _filterSaveOnAreaChange(null);
  _updateSaveSessionButtonState();
  _setSessionCacheNote("");
  lastAnalysisGeojson = null;
  lastAnalysisCounts = null;
  lastIncludedRedfin = false;
  lastIncludedSold = false;
  lastSoldPoints = [];
  lastSoldPanelPoints = [];
  allSoldPointsRef = [];
  soldCompsFilter = { ...DEFAULT_SOLD_COMPS_FILTER };
  matchedSoldLabelPoints = [];
  ["sold-days-max", "sold-price-min", "sold-price-max", "sold-yr-min", "sold-yr-max"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.value = "";
  });

  // KK 2026-05-27: Clear button resets ALL left-sidebar filters to defaults.
  // Saved areas in DB are NOT touched — reloading a saved area still restores
  // its specific settings via the existing load path.
  //
  // _currentLoadedAreaId = null happened above (line ~10668) and
  // _filterSaveOnAreaChange(null) was called at line ~10671, so
  // _filterSaveQueueSave() will early-return during these mutations —
  // no spurious PUT/autosave fires here.

  // Per-view state: return to ARV and clear the cache so a subsequent area
  // load starts fresh.
  if (ARV_NBV_EXPORT_ENABLED) {
    _activeView = "arv";
    _viewFilterCache = { arv: null, nbv: null, export: null };
  }

  // Property type filter checkboxes (filterState)
  filterState = { ...DEFAULT_FILTERS };
  for (const [key, inputId] of Object.entries(FILTER_INPUT_IDS)) {
    const el = document.getElementById(inputId);
    if (el) el.checked = Boolean(DEFAULT_FILTERS[key]);
  }

  // Property numeric filters (lot/sqft/year/appraisal-value ranges)
  // numericFilters is const — reset in-place via Object.assign
  Object.assign(numericFilters, {
    lot_sqft_min: null, lot_sqft_max: null,
    appr_val_min: null, appr_val_max: null,
    yr_built_min: null, yr_built_max: null,
    sqft_min: null,     sqft_max: null,
  });
  ["nf-lot-min", "nf-lot-max", "nf-val-min", "nf-val-max",
   "nf-yr-min", "nf-yr-max", "nf-sqft-min", "nf-sqft-max"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.value = "";
  });

  // Comp numeric filters — same const pattern, reset in-place
  Object.assign(compNumericFilters, {
    lot_sqft_min: null, lot_sqft_max: null,
    appr_val_min: null, appr_val_max: null,
    yr_built_min: null, yr_built_max: null,
    sqft_min: null,     sqft_max: null,
  });
  ["nf-comp-lot-min", "nf-comp-lot-max", "nf-comp-val-min", "nf-comp-val-max",
   "nf-comp-yr-min", "nf-comp-yr-max", "nf-comp-sqft-min", "nf-comp-sqft-max"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.value = "";
  });

  // Propelio filter block (months/range/status checkboxes/sold-within/
  // lot/sqft/year/price ranges). applyPropelioFilterStateToUI also
  // calls readPropelioFiltersFromUI() at its end, so propelioFilterState
  // ends up in sync with the DOM automatically.
  propelioFilterState = { ...DEFAULT_PROPELIO_FILTERS };
  applyPropelioFilterStateToUI(DEFAULT_PROPELIO_FILTERS);
  // Comp-list sort mode: reset to the same default used at declaration
  propelioCompSortMode = "price_desc";
  const _clearSortEl = document.getElementById("propelio-comp-sort");
  if (_clearSortEl) _clearSortEl.value = "price_desc";
  const soldCompsPanel = document.getElementById("sold-comps-panel");
  if (soldCompsPanel) soldCompsPanel.innerHTML = "";
  document.getElementById("redfin-toggle-status").textContent = "";
  document.getElementById("sold-toggle-status").textContent = "";
  document.getElementById("sidebar-results")?.classList.add("hidden");
  document.getElementById("active-item-actions")?.classList.add("hidden");
  document.getElementById("sidebar-loading")?.classList.add("hidden");
  document.getElementById("btn-drawd-area-clear")?.classList.add("hidden");
  // NOTE: clearActiveItem is intentionally NOT called here. Callers that
  // immediately follow clearDrawResults() with setActiveItem(...) would
  // otherwise toggle .is-collapsed twice in the same JS tick, which the
  // browser batches and skips the slide-in animation. Explicit slot
  // management lives at the Deselect button + the slot × dismiss.
  renderSavedAreasList();
}

map.on("draw:drawstart", () => {
  if (!_navigationGuardForActiveDeepPull("draw a new area")) {
    const handler = getPolygonDrawHandler();
    if (handler && handler.enabled()) handler.disable();
    return;
  }
  _setMeasureModeEnabled(false);
  bumpUndoPillVersion();
  drawHelper.classList.remove("hidden");
  document.getElementById("btn-draw")?.classList.add("active");
  document.getElementById("btn-draw-cancel")?.classList.remove("hidden");
  document.getElementById("btn-draw-clear")?.classList.add("hidden");
  // CSS pointer-events:none (drawing-active class) blocks parcel layer clicks
  // so vertices never get swallowed by underlying markers.
  map.getContainer().classList.add("drawing-active");
  _reshapeTargetAreaId = _currentLoadedAreaId;
  if (!_reshapeTargetAreaId) {
    _currentSessionIsNamed = false;
    _currentLoadedAreaId = null;
    _syncTabTitle();
    _storedValueOnAreaChange(null);
    void _filterSaveOnAreaChange(null);
    _selectedSavedItemId = null;
    _setSessionCacheNote("");
    renderSavedAreasList();
  }
  _updateSaveSessionButtonState();
});

map.on("draw:drawstop", () => {
  drawHelper.classList.add("hidden");
  document.getElementById("btn-draw")?.classList.remove("active");
  document.getElementById("btn-draw-cancel")?.classList.add("hidden");
  map.getContainer().classList.remove("drawing-active");
  _reshapeTargetAreaId = null;
});

map.on("contextmenu", async (ev) => {
  const drawHandler = getPolygonDrawHandler();
  if (drawHandler && drawHandler.enabled()) {
    drawHandler.completeShape();
    return;
  }
});

// Sidebar-triggered sold popups should dismiss once the map is moved away.
map.on("movestart", () => {
  closeTransientSoldSidebarPopup();
});

document.addEventListener("keydown", (event) => {
  if (isDrawInputTarget(event.target)) return;

  if (event.key === "Escape" && _measureModeEnabled) {
    event.preventDefault();
    _clearMeasurement();
    return;
  }

  const handler = getPolygonDrawHandler();
  if (!handler || !handler.enabled()) return;

  if (event.key === "Enter") {
    event.preventDefault();
    handler.completeShape();
  }

  if (event.key === "Escape") {
    event.preventDefault();
    handler.disable();
    drawHelper.classList.add("hidden");
    map.getContainer().classList.remove("drawing-active");
    document.getElementById("btn-draw")?.classList.remove("active");
    document.getElementById("btn-draw-cancel")?.classList.add("hidden");
  }
});

// "Import from CRM" outreach upload (Mailer + Phone Tracking, 2026-06-03).
// File input is hidden; the button triggers the picker. After file selection,
// the flow is: POST preview → confirm dialog with diff counts → POST commit → toast.
let _outreachImportInFlight = false;

function _showOutreachToast(text) {
  // Reuse the existing setShareStatus toast if available; else alert.
  try {
    if (typeof setShareStatus === "function") {
      setShareStatus(text);
      return;
    }
  } catch (_) {}
  try { window.alert(text); } catch (_) {}
}

async function _postOutreachImport(file, mode) {
  const form = new FormData();
  form.append("file", file);
  const resp = await fetch(`/api/parcels/outreach/import?mode=${encodeURIComponent(mode)}`, {
    method: "POST",
    headers: { ...authHeaders() },
    body: form,
  });
  const data = await resp.json().catch(() => null);
  if (!resp.ok) {
    const detail = data?.detail || `${resp.status} ${resp.statusText}`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

function _formatOutreachPreviewMessage(preview) {
  const total = preview.total ?? 0;
  const matched = preview.matched ?? 0;
  const unmatched = preview.unmatched ?? 0;
  let msg = `CSV preview\n\nTotal rows: ${total}\nMatched to a parcel: ${matched}\nUnmatched (will be skipped): ${unmatched}`;
  const samples = Array.isArray(preview.sample_unmatched_ids) ? preview.sample_unmatched_ids : [];
  if (samples.length) {
    msg += `\n\nSample unmatched IDs:\n  ${samples.slice(0, 10).join("\n  ")}`;
  }
  msg += `\n\nProceed with import? (${matched} matching rows will be upserted into the outreach database.)`;
  return msg;
}

document.getElementById("btn-import-outreach")?.addEventListener("click", () => {
  if (_outreachImportInFlight) return;
  if (!_isPowerUserOrAbove()) {
    window.alert("Import from CRM requires power_user role or higher.");
    return;
  }
  const fileEl = document.getElementById("outreach-import-file");
  if (!fileEl) return;
  fileEl.value = "";  // reset so picking the same file twice re-fires change
  fileEl.click();
});

document.getElementById("outreach-import-file")?.addEventListener("change", async (ev) => {
  const file = ev.target?.files?.[0];
  if (!file) return;
  if (_outreachImportInFlight) return;
  _outreachImportInFlight = true;
  const btn = document.getElementById("btn-import-outreach");
  const originalLabel = btn?.textContent || "Import";
  try {
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Previewing…";
    }
    const preview = await _postOutreachImport(file, "preview");
    const total = preview.total ?? 0;
    const matched = preview.matched ?? 0;
    if (total === 0) {
      _showOutreachToast("CSV has no data rows.");
      return;
    }
    if (matched === 0) {
      _showOutreachToast(
        `CSV has ${total} rows but none matched a parcel in our DB. ` +
        `Check your "Parcel ID" column values. Nothing was changed.`
      );
      return;
    }
    const proceed = window.confirm(_formatOutreachPreviewMessage(preview));
    if (!proceed) {
      _showOutreachToast("Import cancelled. Nothing was changed.");
      return;
    }
    if (btn) btn.textContent = "Importing…";
    const commit = await _postOutreachImport(file, "commit");
    const updated = commit.updated ?? 0;
    const unmatched = commit.unmatched ?? 0;
    _showOutreachToast(
      `Outreach import complete. Updated ${updated} parcels.` +
      (unmatched ? ` ${unmatched} rows skipped (no matching parcel).` : "")
    );
    // Update lastAnalysisGeojson in place using the committed rows the
    // server returned. Without this, re-opening a parcel popup shows
    // STALE outreach state until the user re-runs analyze or reloads
    // the area. KK bug 2026-06-05: typing a date in the CSV column +
    // re-import → popup date stayed blank, checkbox stayed off.
    if (Array.isArray(commit.committed_rows)) {
      for (const cr of commit.committed_rows) {
        const cnty = String(cr?.county || "").trim().toLowerCase();
        const pid = String(cr?.parcel_id || "").trim();
        if (!cnty || !pid) continue;
        // _updateLocalFeatureOutreach mutates feature.properties for the
        // matching parcel in lastAnalysisGeojson.
        try {
          _updateLocalFeatureOutreach(cnty, pid, "contact_info_retrieved", Boolean(cr.outreach_contact_info_retrieved));
          _updateLocalFeatureOutreach(cnty, pid, "mailer_date", cr.outreach_mailer_date || null);
        } catch (e) {
          console.warn("[outreach-import] local update failed for", cnty, pid, e);
        }
      }
    }
    // Cheap re-render so any visible parcel state (color, bucket counts,
    // popup if currently open on a touched parcel) refreshes immediately.
    try { applyMapVisibilityFilters(); } catch (_) {}
    try { _rebuildOutreachOverlays(); } catch (_) {}
    // Mike report 2026-06-05: CSV import didn't refresh the Contact
    // Status count badge either. Same root cause as the popup-edit gap.
    try { _updateMergedSidebarCounts(); } catch (_) {}
  } catch (err) {
    console.error("[outreach-import] failed", err);
    window.alert(`Outreach import failed: ${String(err?.message || err).slice(0, 400)}`);
  } finally {
    _outreachImportInFlight = false;
    if (btn) {
      btn.disabled = false;
      btn.textContent = originalLabel;
    }
  }
});

document.getElementById("btn-download").addEventListener("click", async () => {
  if (_downloadInFlight) return;
  if (!currentJobId) return;

  _downloadInFlight = true;
  let blobUrl = null;
  try {
    _setDownloadButtonState("Preparing CSV…", true);

    const persisted = await persistTagStateForExport((statusText) => {
      _setDownloadButtonState(statusText, true);
    });
    if (!persisted) {
      alert("Your analysis session expired. Please re-run the draw/analyze step, then export again.");
      return;
    }

    _setDownloadButtonState("Name export…", true);
    const suggested = makeDefaultCsvName();
    const entered = window.prompt("Name this CSV export:", suggested);
    if (entered === null) return;
    const filename = normalizeCsvFilename(entered);

    // Compute visible parcels + visible Propelio comps. Null-guard: when
    // there's no analysis loaded (e.g. saved area without geometry), send
    // filter_ids:null so the backend falls back to the full-export path.
    _setDownloadButtonState("Computing visible parcels…", true);
    let filterIds = null;
    if (lastAnalysisGeojson && Array.isArray(lastAnalysisGeojson.features) && lastAnalysisGeojson.features.length > 0) {
      const visibleParcels = lastAnalysisGeojson.features
        .filter(isFeatureVisible)
        .map((f) => {
          const p = f?.properties || {};
          return {
            source_county: String(p.source_county || "").trim(),
            account_num: String(p.account_num || "").trim(),
          };
        })
        .filter((p) => p.source_county && p.account_num);
      const currentPropelioFilters = readPropelioFiltersFromUI();
      const visibleCompKeys = (window._propelioLast && Array.isArray(window._propelioLast.comps))
        ? window._propelioLast.comps
            .filter((c) => compPassesPropelioFilters(c, currentPropelioFilters))
            .map((c) => String(c?.comp_address_key || "").trim())
            .filter((k) => k.length > 0)
        : [];
      filterIds = { parcels: visibleParcels, comps: visibleCompKeys };
    }

    _setDownloadButtonState("Starting download…", true);
    const resp = await fetch(`/api/download/${currentJobId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      // loaded_area_id: 2026-05-24 fix for the Copy Area As share_id
      // bug — after copy, cached_jobs.saved_area_id stays pinned to the
      // SOURCE area, so the backend's _job_share_id lookup returns the
      // source's share_id for the CSV column. Sending the currently-
      // loaded area_id makes the backend use the active workspace's
      // share_id instead.
      body: JSON.stringify({
        filter_ids: filterIds,
        filename,
        loaded_area_id: _currentLoadedAreaId || null,
        // view was previously never sent -- the backend's DownloadFilterRequest
        // has defaulted view to "arv" unconditionally, so the CSV's Good Comp
        // column (and now the Filters column below) never reflected the tab
        // the export was actually taken from. Task 5's "Filters" column must
        // be view-correct (Final always reads "manual", never bare "ai"), so
        // this needed fixing to make that acceptance item pass at all.
        view: _activeView,
        // AI bar spec (2026-07-14) Task 5: drives the "Filters" provenance column.
        ai_mode: _aiModeOn,
      }),
    });
    if (!resp.ok) {
      const errText = await resp.text().catch(() => "");
      console.error("[csv-download] non-OK response", resp.status, errText);
      _showToast("Download failed", "error");
      return;
    }

    const blob = await resp.blob();
    blobUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = blobUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  } catch (err) {
    console.error("[csv-download] failed", err);
    _showToast("Download failed", "error");
  } finally {
    if (blobUrl) {
      // Defer revoke until after the browser kicks off the download (next
      // animation frame is more robust across browsers than a fixed timeout).
      requestAnimationFrame(() => URL.revokeObjectURL(blobUrl));
    }
    _downloadInFlight = false;
    _resetDownloadButtonState();
  }
});

function _isAutoGeneratedWorkspaceName(name) {
  // Matches the timestamp fallback name produced by _autoCacheOnDraw when
  // no saved parcel was inside the polygon at draw time:
  //   `Workspace ${new Date().toISOString().slice(0,16).replace("T"," ")}`
  //   e.g. "Workspace 2026-05-28 09:14"
  // Used by saveParcel to decide whether a draw-first-then-save-parcel
  // area still has its placeholder name and should be auto-renamed to the
  // subject's address (first-flow symmetry with save-parcel-then-draw).
  return /^Workspace \d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(String(name || "").trim());
}

function _suggestAreaNameFromContainedParcels() {
  // Save Area = Save As (always creates a new saved_areas row). If a
  // workspace is already loaded, the user's mental model is "fork xyz
  // into a new one" — pre-fill with the current workspace name so they
  // can hit Enter to keep it (dedupes server-side to "xyz (2)") or type
  // a replacement. Beats stomping it with whatever target address
  // happens to be in the polygon.
  if (_currentLoadedAreaId) {
    const loaded = _savedAreasCache.find((a) => String(a.id) === String(_currentLoadedAreaId));
    const loadedName = String(loaded?.name || "").trim();
    if (loadedName) return loadedName;
  }
  if (!Array.isArray(lastPolygon) || lastPolygon.length < 3) return null;
  if (!Array.isArray(_savedParcelsCache) || _savedParcelsCache.length === 0) return null;
  for (const p of _savedParcelsCache) {
    if (!Number.isFinite(p.lat) || !Number.isFinite(p.lng)) continue;
    if (pointInPolygonLngLat([p.lng, p.lat], lastPolygon)) {
      const name = String(p.name || "").trim();
      if (name) return name;
    }
  }
  return null;
}

function _openSaveAreaInlineInput() {
  const btn = document.getElementById("btn-save-area");
  if (!btn) return;
  const parent = btn.parentElement;
  if (!parent || parent.querySelector(".save-area-inline")) return;

  const wrap = document.createElement("div");
  wrap.className = "save-area-inline";
  // Class handles layout (flex: 1 1 100% to wrap onto its own row in .sidebar-actions)

  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = "Name this area...";
  input.className = "saved-area-rename-input";
  input.style.minWidth = "0";
  input.style.maxWidth = "100%";
  input.style.flex = "1 1 auto";

  const suggested = _suggestAreaNameFromContainedParcels();
  if (suggested) input.value = suggested;

  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "saved-area-action-btn";
  cancel.textContent = "×";

  const finish = () => {
    wrap.remove();
    btn.classList.remove("hidden");
  };

  const submit = async () => {
    const name = String(input.value || "").trim();
    if (!name) {
      finish();
      return;
    }
    input.disabled = true;
    try {
      await saveCurrentArea(name);
    } catch (err) {
      console.error("save area failed", err);
    } finally {
      finish();
    }
  };

  cancel.addEventListener("click", finish);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      finish();
    }
    if (e.key === "Enter") {
      e.preventDefault();
      void submit();
    }
  });
  input.addEventListener("blur", () => {
    if (!input.value.trim()) finish();
  });

  wrap.appendChild(input);
  wrap.appendChild(cancel);
  parent.appendChild(wrap);
  btn.classList.add("hidden");
  requestAnimationFrame(() => {
    input.focus();
    if (input.value) input.select();
  });
}

document.getElementById("btn-save-area")?.addEventListener("click", () => {
  _openSaveAreaInlineInput();
});

function _openSaveSessionInlineInput() {
  if (!currentJobId) { _flashSaveSessionHint(); return; }
  if (_currentSessionIsNamed) { _openRenameForCurrentSession(); return; }
  const btn = document.getElementById("btn-save-session");
  if (!btn) return;
  const parent = btn.parentElement;
  if (!parent || parent.querySelector(".save-session-inline")) return;

  const wrap = document.createElement("div");
  wrap.className = "save-session-inline";
  // Class handles layout (flex: 1 1 100% to wrap onto its own row in .sidebar-actions)
  wrap.style.minWidth = "0";
  wrap.style.width = "100%";

  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = "Name this snapshot\u2026";
  input.className = "saved-area-rename-input";
  input.style.minWidth = "0";
  input.style.maxWidth = "100%";
  input.style.flex = "1 1 auto";

  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.className = "saved-area-action-btn";
  cancelBtn.textContent = "\u00d7";

  const finish = () => { wrap.remove(); btn.classList.remove("hidden"); };

  const submit = async () => {
    const name = String(input.value || "").trim();
    if (!name) { finish(); return; }
    input.disabled = true;
    try {
      await saveCurrentSession(name);
      // Show brief success state on the button
      btn.textContent = "\u2713 Saved";
      btn.classList.remove("hidden");
      wrap.remove();
      setTimeout(() => { if (btn.textContent === "\u2713 Saved") btn.textContent = "Save Snapshot"; }, 1500);
    } catch (err) {
      console.error("save session failed", err);
      finish();
    }
  };

  cancelBtn.addEventListener("click", finish);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); finish(); }
    if (e.key === "Enter") { e.preventDefault(); void submit(); }
  });
  input.addEventListener("blur", () => { if (!input.value.trim()) finish(); });

  wrap.appendChild(input);
  wrap.appendChild(cancelBtn);
  parent.appendChild(wrap);
  btn.classList.add("hidden");
  requestAnimationFrame(() => input.focus());
}

document.getElementById("btn-save-session")?.addEventListener("click", () => {
  _openSaveSessionInlineInput();
});


document.getElementById("btn-clear").addEventListener("click", () => {
  clearDrawResults();
  clearActiveItem();
});

// ── Save Session helpers ─────────────────────────────────────────────────────

function _flashSaveSessionHint() {
  const btn = document.getElementById("btn-save-session");
  if (!btn) return;
  btn.classList.add("flash-hint");
  setTimeout(() => btn.classList.remove("flash-hint"), 1500);
}

function _openRenameForCurrentSession() {
  if (!currentJobId) return;
  const session = _savedSessionsCache.find((s) => s.session_id === currentJobId);
  if (!session) return;
  const row = document.querySelector(`[data-session-id="${CSS.escape(session.session_id)}"]`);
  if (!row) return;
  void _renameSavedSessionInline(session, row);
}

// Cmd/Ctrl+S — save or rename the current session
document.addEventListener("keydown", (e) => {
  const isSave = (e.metaKey || e.ctrlKey) && e.key === "s" && !e.shiftKey && !e.altKey;
  if (!isSave) return;
  e.preventDefault(); // always — prevents "Save Page As…" in Chrome
  const ae = document.activeElement;
  if (ae && (ae.tagName === "INPUT" || ae.tagName === "TEXTAREA" || ae.isContentEditable)) return;
  if (!currentJobId) { _flashSaveSessionHint(); return; }
  if (_currentSessionIsNamed) { _openRenameForCurrentSession(); return; }
  _openSaveSessionInlineInput();
});

// Legacy Redfin source-toggle flow (archived): retained for rollback safety.
// New UX uses map filters and DB-backed overlays instead of source toggles.
async function rerunWithRedfin() {
  if (!lastPolygon || lastPolygon.length < 3) return;
  const includeSold = Boolean(document.getElementById("toggle-sold")?.checked);
  const statusEl = document.getElementById("redfin-toggle-status");
  const toggleEl = document.getElementById("toggle-redfin");
  if (toggleEl) toggleEl.disabled = true;
  if (statusEl) statusEl.textContent = "Fetching Redfin\u2026";
  try {
    const data = await runAnalysis(lastPolygon, true, includeSold);
    currentJobId = data.job_id;
    // Merge server state without overwriting in-session tag changes.
    data.features.forEach((feature) => {
      const p = feature.properties || {};
      if (!p.account_num) return;
      if (!verificationByAccount.has(p.account_num)) {
        const normalized = normalizeVerificationValue(p.verified_vacant);
        verificationByAccount.set(p.account_num, normalized);
        p.verified_vacant = normalized;
      } else {
        p.verified_vacant = verificationByAccount.get(p.account_num);
      }
      if (!potentialTargetByAccount.has(p.account_num)) {
        const potential = String(p.potential_target || "").trim().toLowerCase() === "yes" ? "Yes" : "";
        potentialTargetByAccount.set(p.account_num, potential);
        p.potential_target = potential;
      } else {
        p.potential_target = potentialTargetByAccount.get(p.account_num);
      }
    });
    lastIncludedRedfin = true;
    lastIncludedSold = includeSold;
    lastSoldPoints = Array.isArray(data.sold_points) ? data.sold_points : [];
    allSoldPointsRef = [...lastSoldPoints];
    lastAnalysisGeojson = data;
    lastAnalysisCounts = data.counts;
    redfinLayerVisible = true;
    redfinLayer.addTo(map);
    soldLayerVisible = includeSold;
    if (soldLayerVisible) soldLayer.addTo(map); else map.removeLayer(soldLayer);
    applyAndRenderSoldFilters();
    const markers = renderFeatures(data);
    renderSidebar(data.counts, markers);
    applyResultTags(data);
    if (statusEl) {
      if (!data.redfin_ok) {
        statusEl.textContent = "Redfin unavailable";
      } else if (data.counts.active === 0) {
        statusEl.textContent = "No active listings found";
      } else {
        statusEl.textContent = `${data.counts.active} active listing${data.counts.active !== 1 ? "s" : ""} found`;
      }
    }
    applyAndRenderSoldFilters();
    applyMapVisibilityFilters();
  } catch (err) {
    if (statusEl) statusEl.textContent = "Redfin fetch failed";
    console.error("Redfin re-fetch failed", err);
    if (toggleEl) toggleEl.checked = false;
    redfinLayerVisible = false;
    map.removeLayer(redfinLayer);
  } finally {
    if (toggleEl) toggleEl.disabled = false;
  }
}

// Redfin source toggle: hides/shows Redfin-specific visual styling and markers.
// If Redfin data wasn't fetched for the current polygon, triggers a re-fetch.
document.getElementById("toggle-redfin")?.addEventListener("change", async (e) => {
  redfinLayerVisible = e.target.checked;

  if (redfinLayerVisible && !lastIncludedRedfin && lastPolygon) {
    await rerunWithRedfin();
    return;
  }

  if (redfinLayerVisible) {
    redfinLayer.addTo(map);
  } else {
    map.removeLayer(redfinLayer);
    const statusEl = document.getElementById("redfin-toggle-status");
    if (statusEl) statusEl.textContent = "";
  }

  // Re-render so active listings fall back to DCAD default styling when hidden.
  if (lastAnalysisGeojson) {
    const markers = renderFeatures(lastAnalysisGeojson);
    if (lastAnalysisCounts) {
      renderSidebar(lastAnalysisCounts, markers);
    }
    applyAndRenderSoldFilters();
    applyMapVisibilityFilters();
  }
});

async function rerunWithSold() {
  if (!lastPolygon || lastPolygon.length < 3) return;
  const includeRedfin = Boolean(document.getElementById("toggle-redfin")?.checked);
  const statusEl = document.getElementById("sold-toggle-status");
  const toggleEl = document.getElementById("toggle-sold");
  if (toggleEl) toggleEl.disabled = true;
  if (statusEl) statusEl.textContent = "Fetching sold comps...";

  try {
    const data = await runAnalysis(lastPolygon, includeRedfin, true);
    currentJobId = data.job_id;
    lastIncludedSold = true;
    lastIncludedRedfin = includeRedfin;
    lastSoldPoints = Array.isArray(data.sold_points) ? data.sold_points : [];
    allSoldPointsRef = [...lastSoldPoints];
    lastAnalysisGeojson = data;
    lastAnalysisCounts = data.counts;
    allAnalysisFeatures = data.features;
    if (Array.isArray(allAnalysisFeatures) && allAnalysisFeatures.length <= BROWSE_ONLY_THRESHOLD) {
      const soldJoin = attachSoldCompsToFeatures(allAnalysisFeatures, lastSoldPoints);
      lastSoldPoints = soldJoin.unmatchedSoldPoints;
      matchedSoldLabelPoints = soldJoin.matchedLabelPoints || [];
      data.sold_points = lastSoldPoints;
    } else {
      matchedSoldLabelPoints = [];
    }
    soldLayerVisible = true;
    soldLayer.addTo(map);
    applyAndRenderSoldFilters();
    if (lastAnalysisGeojson && Array.isArray(lastAnalysisGeojson.features) && lastAnalysisGeojson.features.length <= BROWSE_ONLY_THRESHOLD) {
      const markers = renderFeatures(lastAnalysisGeojson);
      if (lastAnalysisCounts) renderSidebar(lastAnalysisCounts, markers);
    }

    if (statusEl) updateSoldStatusText();
    applyAndRenderSoldFilters();
    applyMapVisibilityFilters();
  } catch (err) {
    if (statusEl) statusEl.textContent = "Sold comps unavailable";
    console.error("Sold re-fetch failed", err);
    if (toggleEl) toggleEl.checked = false;
    soldLayerVisible = false;
    map.removeLayer(soldLayer);
  } finally {
    if (toggleEl) toggleEl.disabled = false;
  }
}

document.getElementById("toggle-sold")?.addEventListener("change", async (e) => {
  soldLayerVisible = e.target.checked;

  if (soldLayerVisible && !lastIncludedSold && lastPolygon) {
    await rerunWithSold();
    return;
  }

  if (soldLayerVisible) {
    soldLayer.addTo(map);
  } else {
    map.removeLayer(soldLayer);
    const statusEl = document.getElementById("sold-toggle-status");
    if (statusEl) statusEl.textContent = "";
  }
});

// Cursor: show pointer when hovering over a parcel in browse mode.
map.on("mousemove", (ev) => {
  if (lastAnalysisGeojson) return;
  if (map.getZoom() < 14) return;
  // Defensive: browseLayer can be detached from the map during certain
  // saved-area restore + viewport-suspend cycles. Without this guard
  // queryTileFeaturesDebug throws TypeError "this._map is null" on every
  // mousemove and blocks all subsequent map interactions.
  if (!browseLayer._map) return;
  const result = browseLayer.queryTileFeaturesDebug(ev.latlng.lng, ev.latlng.lat);
  const hit = result instanceof Map
    ? [...result.values()].flat().length > 0
    : (Array.isArray(result) ? result.length > 0 : false);
  map.getContainer().style.cursor = hit ? "pointer" : "";
});

// Browse layer click — hit-test against canvas tiles, fetch full detail from API,
// open popup using same makePopupHtml used by draw results.
// Silent during active polygon draw so draw vertices aren't intercepted.
map.on("click", async (ev) => {
  const drawHandler = getPolygonDrawHandler();
  if (drawHandler && drawHandler._enabled) return;

  if (_measureModeEnabled) {
    window._clearSearchHighlight?.();
    if (!lastAnalysisGeojson && browseLayer._map) {
      const result = browseLayer.queryTileFeaturesDebug(ev.latlng.lng, ev.latlng.lat);
      const allFeatures = result instanceof Map
        ? [...result.values()].flat()
        : (Array.isArray(result) ? result : []);
      if (allFeatures.length) {
        const parcel = allFeatures.find((f) => {
          const props = (f.feature && f.feature.props) || f.props || {};
          return props.source_county === "dcad" || props.source_county === "tad" || props.source_county === "collin" || props.source_county === "denton";
        });
        if (parcel) {
          const pProps = (parcel.feature && parcel.feature.props) || parcel.props || {};
          _handleMeasureInteraction(ev.latlng, pProps);
          return;
        }
      }
    }
    _handleMeasureInteraction(ev.latlng, null);
    return;
  }

  // Don't fire browse popup when draw results are visible — let polygon clicks handle it.
  if (lastAnalysisGeojson) return;

  // Any map click clears an orphaned search highlight (search popup was replaced by this click).
  window._clearSearchHighlight?.();

  // Defensive: same browseLayer-detached guard as the mousemove handler above.
  if (!browseLayer._map) return;
  const result = browseLayer.queryTileFeaturesDebug(ev.latlng.lng, ev.latlng.lat);
  // v3 returns Map<string, PickedFeature[]> — flatten all values regardless of key name
  const allFeatures = result instanceof Map
    ? [...result.values()].flat()
    : (Array.isArray(result) ? result : []);
  if (allFeatures.length === 0) return;

  const parcel = allFeatures.find(f => {
    const props = (f.feature && f.feature.props) || f.props || {};
    return props.source_county === "dcad" || props.source_county === "tad" || props.source_county === "collin" || props.source_county === "denton";
  });
  if (!parcel) return;

  const pProps = (parcel.feature && parcel.feature.props) || parcel.props || {};
  const county = pProps.source_county;
  const accountNum = pProps.account_num;
  if (!county || !accountNum) return;

  try {
    const resp = await fetch(`/api/parcel/${county}/${accountNum}`);
    if (!resp.ok) return;
    const detail = await resp.json();
    openParcelDetailPanel(detail.properties || detail, { latlng: ev.latlng, geometry: detail.geometry });
  } catch (e) {
    console.error("Browse popup failed", e);
  }
});

// Clear the selected outline on any map click. Sidebar and comp-list
// clicks go through DOM event paths, NOT map clicks — so the only way
// to reach this listener is by clicking the map proper. Always-clears
// is intentional and runs even during an active draw.
map.on("click", () => {
  _clearSelectedOutline();
});

function _wireParcelInteractiveUi(root, options = {}) {
  if (!root) return;
  const close = typeof options.close === "function" ? options.close : () => {};

  const saveLink = root.querySelector(".parcel-save-link");
  if (saveLink) {
    saveLink.addEventListener("click", async (ev) => {
      ev.preventDefault();
      const { account, county, addr, city, lat, lng, ownerCity } = saveLink.dataset;
      // ownerCity may be undefined on older popup renders — falls back to ""
      // and _formatPropertyAddress handles empty fallback gracefully.
      const fullName = _formatPropertyAddress(county, addr, city, ownerCity || "");
      let geometry = null;
      try {
        const resp = await fetch(`/api/parcel/${county}/${account}`);
        if (resp.ok) {
          const detail = await resp.json();
          if (detail.geometry?.type === "Polygon" || detail.geometry?.type === "MultiPolygon") {
            geometry = detail.geometry;
          }
        }
      } catch {}
      try {
        const _now = Date.now();
        if (_now - _lastSaveParcelClickAt < SAVE_PARCEL_DEBOUNCE_MS) return;
        _lastSaveParcelClickAt = _now;
        await saveParcel(account, county, fullName, parseFloat(lat), parseFloat(lng), geometry);
        saveLink.textContent = "✓ Saved";
        saveLink.style.color = "#888";
        saveLink.style.pointerEvents = "none";
      } catch (err) {
        console.error("save parcel failed", err);
        saveLink.textContent = "Save failed";
      }
    });
  }

  const clearLink = root.querySelector(".parcel-clear-link");
  if (clearLink) {
    if (!window._searchHighlight) clearLink.style.display = "none";
    clearLink.addEventListener("click", (ev) => {
      ev.preventDefault();
      window._clearSearchHighlight?.();
      clearLink.style.display = "none";
    });
  }

  const verifyYes = root.querySelector(".parcel-verify-yes");
  if (verifyYes) {
    verifyYes.addEventListener("click", (ev) => {
      ev.preventDefault();
      const { account, lat, lng } = verifyYes.dataset;
      setVerification(account, "yes", parseFloat(lat), parseFloat(lng));
      persistSingleTag(account, "verified_vacant", "yes");
      close();
    });
  }

  const verifyNo = root.querySelector(".parcel-verify-no");
  if (verifyNo) {
    verifyNo.addEventListener("click", (ev) => {
      ev.preventDefault();
      const { account, lat, lng } = verifyNo.dataset;
      setVerification(account, "no", parseFloat(lat), parseFloat(lng));
      persistSingleTag(account, "verified_vacant", "no");
      close();
    });
  }

  const verifyClear = root.querySelector(".parcel-verify-clear");
  if (verifyClear) {
    verifyClear.addEventListener("click", (ev) => {
      ev.preventDefault();
      const { account } = verifyClear.dataset;
      setVerification(account, null);
      persistSingleTag(account, "verified_vacant", null);
      close();
    });
  }
}

// Wire up parcel action links for any remaining popup paths.
map.on("popupopen", (e) => {
  const popupMeta = e.popup?._source?._lotLedgerPopupMeta;
  if (popupMeta?.type === "parcel") {
    _captureParcelPopupState(popupMeta);
    _suspendViewportRender();
  }
  const el = e.popup.getElement();
  if (!el) return;
  _wireParcelInteractiveUi(el, { close: () => e.popup.close() });
});

map.on("popupclose", (e) => {
  const popupMeta = e.popup?._source?._lotLedgerPopupMeta;
  if (popupMeta?.type !== "parcel") return;
  if (_isRefreshingParcelLayers) return;
  if (_activeParcelPopupState?.accountNum === String(popupMeta.accountNum || "")) {
    _activeParcelPopupState = null;
  }
});

// =============================================================================
// Auth UI — login modal, user bar, admin panel
// All state changes from the server (401, 403 FORCE_PASSWORD_CHANGE) are handled
// globally here. Existing API calls just throw/catch normally; this layer catches
// the signals and reroutes to the correct modal state.
// =============================================================================

let _currentUser = null;        // null = not logged in, object = logged-in user dict
let _authDropdownOpen = false;  // tracks whether the username dropdown is expanded

// Role helpers — always read from _currentUser so they stay in sync with server state
const _currentRole    = () => (_currentUser?.role || "").toLowerCase();
const _isPowerUserOrAbove = () => ["power_user", "owner", "developer"].includes(_currentRole());
const _isAdmin        = () => ["owner", "developer"].includes(_currentRole());
const _canDownloadCsv = () => _currentRole() !== "user";
const _canEditAnyArea = () => _currentRole() === "developer";

function _applyRoleVisibility() {
  if (_isPowerUserOrAbove()) {
    document.body.classList.remove("is-not-power-user");
  } else {
    document.body.classList.add("is-not-power-user");
  }
  // Fix B: rename gating now depends on app role (_isAdmin), so the pencil must be
  // re-derived on EVERY auth transition — login, init, 401, signout. Folding the call
  // in here covers all nine _applyRoleVisibility() call sites, and any added later.
  // Idempotent + null-safe (no #active-item-rename → early return).
  _updateActiveItemRenameVisibility();
}

_applyRoleVisibility();

// AI module seam — the ONLY thing frontend/ai-card.js reads from map.js.
// Deleting the AI module = delete this function, the stash line in
// applyPropelioClientFilters(), and the 2 index.html lines.
window.__aiGetVisibleCompContext = () => ({
  areaId: _currentLoadedAreaId,
  comps: Array.isArray(window.__aiVisibleComps) ? window.__aiVisibleComps : [],
  isAdmin: _isAdmin(),
  headers: authHeaders(),
});

// Value Drafts module seam (docs/AI/VALUE_DRAFTS_SPEC_2026-07-13.md). No AI
// endpoint involved -- deterministic client-side arithmetic only. This helper
// + the two window functions below are the ENTIRE map.js touch point:
// deleting them + frontend/value-drafts.js/.css + the 3 index.html lines
// (script, stylesheet, LL_CONFIG key) leaves the app byte-identical.
//
// __valueDraftsViewComps(view) reconstructs what the ARV or NBV VIEW's
// visible+kept set would be WITHOUT switching _activeView, re-rendering, or
// autosaving -- it reuses the exact same filter-application function
// (compPassesPropelioFilters) map.js already uses, and the same per-view
// filter-cache + seed-from-ARV fallback _setActiveView itself uses
// (map.js:15230), purely read-only. For the active view it reads the live
// propelioFilterState; for the inactive view it reads that view's cached
// filter blob (or the ARV blob, or plain defaults, in that order -- same
// fallback order _setActiveView uses when a view has never been visited).
function __valueDraftsViewComps(view) {
  const all = (window._propelioLast && Array.isArray(window._propelioLast.comps))
    ? window._propelioLast.comps
    : [];
  let fstate;
  if (view === _activeView) {
    fstate = propelioFilterState;
  } else {
    const cached = _viewFilterCache[view];
    if (cached && cached.v && cached.propelio) {
      fstate = { ...DEFAULT_PROPELIO_FILTERS, ...cached.propelio };
    } else {
      // This view has never been opened, so it has no filters of its own. When it
      // IS opened, _setActiveView seeds it from the CURRENT view -- so that is what
      // we mirror here.
      //
      // The old code fell back to DEFAULT_PROPELIO_FILTERS (i.e. NO filters at all).
      // That is how the NBV chip reported $14.9M while a $2M max-price filter was
      // active: it was drafting from an unfiltered pool the user could not see.
      // Never fall back to defaults -- fall back to what the user is looking at.
      fstate = { ...propelioFilterState };
    }
  }
  const nbhdNorm = normalizeNbhd(fstate.neighborhood);
  return all
    .filter((c) => c && compPassesPropelioFilters(c, fstate, nbhdNorm))
    .map((c) => ({
      comp_address_key: c.comp_address_key,
      address: c.address,
      price: Number.isFinite(Number(c.price)) ? Number(c.price) : null,
      year_built: Number.isFinite(Number(c.year_built)) ? Number(c.year_built) : null,
      // permit_avm is not currently present on ANY comp-loading response the
      // frontend receives (verified -- no path sets it). Reading it
      // defensively means the math-exclusion check activates automatically
      // the day it IS wired onto the wire, with no further map.js change.
      // Until then this is always null and that exclusion reason never
      // fires. Flagged in the coder report.
      permit_avm: (typeof c.permit_avm === "boolean") ? c.permit_avm : null,
      user_rating: view === "arv"
        ? ((c._ratingArv === "good" || c._ratingArv === "bad") ? c._ratingArv : null)
        : _ratingForView(c._ratingArv, c.ratings_by_view, view),
    }));
}

window.__valueDraftsGetContext = () => ({
  areaId: _currentLoadedAreaId,
  isAdmin: _isAdmin(),
  headers: authHeaders(),
  // §3, docs/AI/CODER_SPEC_AIMODE_FIX_2026-07-14.md — nothing in the record
  // distinguishes a number signed under the AI lens from one signed under
  // the VA's own filters. Accept stays live while AI mode is on (the human
  // signature is real); this just makes that fact visible on the record.
  aiMode: _aiModeOn,
  // Which view the user is actually LOOKING at. Only the active view's filters are
  // live -- _viewFilterCache is written on view SWITCH (map.js:15321), so the
  // inactive view's filters are stale (and, if that view was never opened, null ->
  // DEFAULTS -> no price cap at all). Drafting from that is not WYSIWYG, it's a
  // number computed from filters the user cannot see. So value-drafts.js only
  // drafts the ACTIVE view and tells the user to switch for the other.
  activeView: _activeView,
  arv: { comps: __valueDraftsViewComps("arv") },
  nbv: { comps: __valueDraftsViewComps("nbv") },
  storedValues: _storedValueState
    ? { arv: _storedValueState.arv.numeric_value, nbv: _storedValueState.nbv.numeric_value }
    : { arv: null, nbv: null },
});

// The ONLY function that ever writes an ARV/NBV stored-value field from a
// draft (§0.1 / §3.3 -- the draft itself NEVER touches input.value). Goes
// through the exact path a keystroke would (_storedValueOnNumericInput ->
// _storedValueQueueSave), which also runs the shipped NBV->TDPP / MAO /
// TDPP-MAO cascade for free, then forces an immediate flush (reusing
// _storedValueFlushPending, already used by the beforeunload handler)
// instead of waiting out the debounce -- an explicit accept click is one
// final signed action, not a keystroke stream. Restricted to arv/nbv only --
// drafting TDPP is explicitly out of scope (§0.2).
window.__valueDraftsAcceptDraft = (fieldKey, value) => {
  if (fieldKey !== "arv" && fieldKey !== "nbv") return false;
  if (!_storedValueState) return false;
  _storedValueOnNumericInput(fieldKey, String(value));
  const input = document.getElementById(`sv-input-${fieldKey}`);
  if (input) input.value = _storedValueFormatDisplay(_storedValueState[fieldKey].numeric_value);
  void _storedValueFlushPending();
  return true;
};

// ---------------------------------------------------------------------------
// Helpers to show/hide the modal
// ---------------------------------------------------------------------------
function _showAuthModal() {
  document.getElementById("auth-modal").classList.remove("hidden");
}

function _hideAuthModal() {
  document.getElementById("auth-modal").classList.add("hidden");
  document.getElementById("auth-modal-box").innerHTML = "";
}

// Render an error string into an .auth-error element (by id).
function _setAuthError(elId, msg) {
  const el = document.getElementById(elId);
  if (!el) return;
  el.textContent = msg || "";
  el.classList.toggle("hidden", !msg);
}

// Set a submit button's disabled+text state during async work.
function _setAuthBusy(btnId, busy, label = "Sign In") {
  const btn = document.getElementById(btnId);
  if (!btn) return;
  btn.disabled = busy;
  btn.textContent = busy ? "Please wait…" : label;
}

// ---------------------------------------------------------------------------
// Show login form (modal state 1)
// ---------------------------------------------------------------------------
function _showLoginForm(errorMsg = "") {
  const box = document.getElementById("auth-modal-box");
  box.className = "auth-modal-box";
  box.innerHTML = `
    <span class="auth-modal-title">Sign In to Lot Ledger</span>
    <span class="auth-modal-subtitle">Enter your credentials to continue.</span>
    <form id="auth-login-form" class="auth-form" autocomplete="off">
      <div class="auth-field">
        <label for="auth-identifier">Username or Email</label>
        <input type="text" id="auth-identifier" name="identifier" autocomplete="off" autocapitalize="off" spellcheck="false" required placeholder="username or you@example.com">
      </div>
      <div class="auth-field">
        <label for="auth-password">Password</label>
        <input type="password" id="auth-password" name="password" autocomplete="new-password" data-lpignore="true" required>
      </div>
      <p id="auth-login-error" class="auth-error${errorMsg ? "" : " hidden"}">${errorMsg}</p>
      <button type="submit" id="auth-login-btn" class="auth-submit-btn">Sign In</button>
    </form>`;
  _showAuthModal();

  // Avoid misleading browser autofill bullets in auth password fields.
  const pwdEl = document.getElementById("auth-password");
  if (pwdEl) pwdEl.value = "";
  setTimeout(() => {
    const delayedPwdEl = document.getElementById("auth-password");
    if (delayedPwdEl) delayedPwdEl.value = "";
  }, 0);

  document.getElementById("auth-login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const identifier = document.getElementById("auth-identifier").value.trim();
    const password = document.getElementById("auth-password").value;
    _setAuthError("auth-login-error", "");
    _setAuthBusy("auth-login-btn", true, "Sign In");

    try {
      const resp = await fetch("/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ identifier, password }),
        credentials: "same-origin",
      });
      const data = await resp.json().catch(() => ({}));
      if (resp.ok) {
        _hideAuthModal();
        _currentUser = data.user || data;
        _renderUserBar(_currentUser);
        _applyRoleVisibility();
        _appShellReady = true;
        _maybeShowUpdateBanner();
        await _reloadSavedResources().catch((err) => console.error("load saved resources failed", err));
        _maybeShowImportBanner();
        const pendingShareId = _pendingAreaShareId;
        _pendingAreaShareId = null;
        if (pendingShareId) {
          await _loadAreaFromShareId(pendingShareId);
        }
        if (data.force_password_change) {
          _showChangePasswordForm(true);
        }
      } else {
        const msg = data?.detail || "Login failed. Check your credentials.";
        _setAuthError("auth-login-error", msg);
        _setAuthBusy("auth-login-btn", false, "Sign In");
      }
    } catch {
      _setAuthError("auth-login-error", "Network error. Please try again.");
      _setAuthBusy("auth-login-btn", false, "Sign In");
    }
  });

  // Autofocus identifier field
  requestAnimationFrame(() => document.getElementById("auth-identifier")?.focus());
}

// ---------------------------------------------------------------------------
// Show change-password form (modal state 2)
// forced=true → no close button, app is locked until done
// ---------------------------------------------------------------------------
function _showChangePasswordForm(forced = false) {
  const box = document.getElementById("auth-modal-box");
  box.className = "auth-modal-box";
  box.innerHTML = `
    ${forced ? "" : `<div class="auth-modal-close-row">
      <button class="auth-modal-close-btn" id="auth-close-chpw" title="Cancel">×</button>
    </div>`}
    <span class="auth-modal-title">${forced ? "Set a New Password" : "Change Password"}</span>
    <span class="auth-modal-subtitle">${forced ? "You must set a new password before continuing." : "Choose a new password for your account."}</span>
    <form id="auth-chpw-form" class="auth-form" autocomplete="off">
      <div class="auth-field">
        <label>Current Password</label>
        <input type="password" id="auth-chpw-current" autocomplete="off" data-lpignore="true" required>
      </div>
      <div class="auth-field">
        <label>New Password</label>
        <input type="password" id="auth-chpw-new" autocomplete="off" data-lpignore="true" required>
      </div>
      <div class="auth-field">
        <label>Confirm New Password</label>
        <input type="password" id="auth-chpw-confirm" autocomplete="off" data-lpignore="true" required>
      </div>
      <p id="auth-chpw-error" class="auth-error hidden"></p>
      <button type="submit" id="auth-chpw-btn" class="auth-submit-btn">Update Password</button>
    </form>`;
  _showAuthModal();

  // Stomp browser-autofilled password values. Some password managers re-fill
  // after the synchronous JS, so clear immediately, on the next tick, and
  // again at 100ms to catch slower autofill paths.
  const _clearChpwFields = () => {
    ["auth-chpw-current", "auth-chpw-new", "auth-chpw-confirm"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.value = "";
    });
  };
  _clearChpwFields();
  setTimeout(_clearChpwFields, 0);
  setTimeout(_clearChpwFields, 100);

  document.getElementById("auth-close-chpw")?.addEventListener("click", () => {
    _hideAuthModal();
  });

  document.getElementById("auth-chpw-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const current_password = document.getElementById("auth-chpw-current").value;
    const new_password = document.getElementById("auth-chpw-new").value;
    const confirm = document.getElementById("auth-chpw-confirm").value;

    if (new_password !== confirm) {
      _setAuthError("auth-chpw-error", "New passwords do not match.");
      return;
    }
    if (new_password.length < 10) {
      _setAuthError("auth-chpw-error", "New password must be at least 10 characters.");
      return;
    }

    _setAuthError("auth-chpw-error", "");
    _setAuthBusy("auth-chpw-btn", true, "Update Password");

    try {
      const resp = await fetch("/auth/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ current_password, new_password, confirm_password: confirm }),
        credentials: "same-origin",
      });
      const data = await resp.json().catch(() => ({}));
      if (resp.ok) {
        if (forced) {
          _hideAuthModal();
          _currentUser = null;
          _renderUserBar(null);
          _applyRoleVisibility();
          _showLoginForm("Password updated. Sign in with your new password.");
          return;
        }
        _currentUser = data.user || _currentUser;
        if (_currentUser) _currentUser.force_password_change = false;
        _renderUserBar(_currentUser);
        _applyRoleVisibility();
        _hideAuthModal();
      } else {
        const detail = data?.detail;
        const msg = typeof detail === "string"
          ? detail
          : Array.isArray(detail) && detail[0]?.msg
            ? detail[0].msg
            : "Password change failed. Please try again.";
        _setAuthError("auth-chpw-error", msg);
        _setAuthBusy("auth-chpw-btn", false, "Update Password");
      }
    } catch {
      _setAuthError("auth-chpw-error", "Network error. Please try again.");
      _setAuthBusy("auth-chpw-btn", false, "Update Password");
    }
  });

  requestAnimationFrame(() => document.getElementById("auth-chpw-current")?.focus());
}

// ---------------------------------------------------------------------------
// Show admin user management panel (modal state 3)
// ---------------------------------------------------------------------------
async function _showAdminPanel() {
  const box = document.getElementById("auth-modal-box");
  box.className = "auth-modal-box wide";
  box.innerHTML = `
    <div class="auth-modal-close-row">
      <button class="auth-modal-close-btn" id="auth-close-admin" title="Close">×</button>
    </div>
    <span class="auth-modal-title">User Management</span>
    <p class="admin-section-title">Members</p>
    <div id="admin-user-table-wrap">Loading…</div>
    <p class="admin-section-title">Add New User</p>
    <div class="admin-add-form">
      <input type="text"  id="admin-new-username" placeholder="Username" autocomplete="off">
      <input type="password" id="admin-new-temp-password" placeholder="Temp Password (10+ chars)" autocomplete="new-password">
      <select id="admin-new-role">
        <option value="user">User</option>
        <option value="power_user">Power User</option>
        ${_currentRole() === "developer" ? `<option value="owner">Owner</option>` : ""}
      </select>
      <button type="button" id="admin-toggle-email" class="admin-link-btn">+ Add email (optional)</button>
      <input type="email" id="admin-new-email" placeholder="Email (optional)" autocomplete="off" class="hidden">
      <button id="admin-add-btn" class="admin-add-btn">+ Add User</button>
      <p id="admin-add-msg" class="admin-msg"></p>
    </div>`;
  _showAuthModal();

  document.getElementById("auth-close-admin").addEventListener("click", _hideAuthModal);
  document.getElementById("admin-add-btn").addEventListener("click", _adminAddUser);
  document.getElementById("admin-toggle-email")?.addEventListener("click", () => {
    const emailEl = document.getElementById("admin-new-email");
    const toggleBtn = document.getElementById("admin-toggle-email");
    if (!emailEl || !toggleBtn) return;
    emailEl.classList.remove("hidden");
    toggleBtn.classList.add("hidden");
    emailEl.focus();
  });

  await _adminRefreshTable();
}

async function _adminRefreshTable() {
  const wrap = document.getElementById("admin-user-table-wrap");
  if (!wrap) return;
  try {
    const resp = await fetch("/admin/users", { credentials: "same-origin" });
    if (!resp.ok) { wrap.textContent = "Failed to load users."; return; }
    const data = await resp.json();
    const users = Array.isArray(data.users) ? data.users : [];
    if (!users.length) { wrap.textContent = "No users found."; return; }

    const isSelf = (u) => u.id === _currentUser?.id;
    const isDev  = (u) => u.role === "developer";
    const selfRole = _currentUser?.role || "";

    const rows = users.map(u => {
      const statusDot = `<span class="admin-status-dot ${u.is_active ? "active" : "disabled"}"></span>`;
      const roleBadge = `<span class="role-badge ${u.role}">${u.role}</span>`;
      let actions = "—";
      if (!isDev(u)) {
        const disableBtn = selfRole === "developer" || (selfRole === "owner" && !isSelf(u))
          ? `<button class="admin-action-btn danger" data-action="${u.is_active ? "disable" : "enable"}" data-uid="${u.id}" data-uname="${_esc(u.username)}">${u.is_active ? "Disable" : "Enable"}</button>`
          : "";
        const resetBtn = selfRole === "developer" || (selfRole === "owner" && !isSelf(u))
          ? `<button class="admin-action-btn" data-action="reset" data-uid="${u.id}" data-uname="${_esc(u.username)}">Reset Pw</button>`
          : "";
        const deleteBtn = selfRole === "developer" || (selfRole === "owner" && !isSelf(u))
          ? `<button class="admin-action-btn danger-strong" data-action="delete" data-uid="${u.id}" data-uname="${_esc(u.username)}">Delete</button>`
          : "";
        actions = (disableBtn + resetBtn + deleteBtn) || "—";
      }
      return `<tr>
        <td>${statusDot}${_esc(u.username)}</td>
        <td>${u.email ? _esc(u.email) : '<span class="admin-cell-empty">—</span>'}</td>
        <td>${roleBadge}</td>
        <td>${actions}</td>
      </tr>`;
    }).join("");

    wrap.innerHTML = `
      <table class="admin-user-table">
        <thead><tr><th>User</th><th>Email</th><th>Role</th><th>Actions</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;

    wrap.querySelectorAll("[data-action]").forEach(btn => {
      btn.addEventListener("click", () => _adminAction(btn.dataset.action, btn.dataset.uid, btn.dataset.uname, btn));
    });
  } catch {
    wrap.textContent = "Error loading users.";
  }
}

async function _adminAction(action, uid, uname, btn) {
  if (action === "delete") {
    const confirmed = confirm(`PERMANENTLY DELETE user '${uname}'?\n\nThis removes their account and ALL their saved areas, parcels, sessions, and tags. Audit history is preserved.\n\nThis cannot be undone.`);
    if (!confirmed) return;
  }

  btn.disabled = true;
  const endpoint = action === "reset"
    ? `/admin/users/${uid}/reset-password`
    : action === "delete"
      ? `/admin/users/${uid}`
      : `/admin/users/${uid}/${action}`;
  const method = action === "delete" ? "DELETE" : "POST";

  try {
    const resp = await fetch(endpoint, {
      method,
      headers: { "Content-Type": "application/json", ...authHeaders() },
      credentials: "same-origin",
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const detail = data?.detail;
      let msg;
      if (typeof detail === "string") {
        msg = detail;
      } else if (Array.isArray(detail) && detail.length > 0) {
        msg = "Validation error: " + detail.map(d => d?.msg || "invalid input").join("; ");
      } else {
        msg = "Action failed.";
      }
      alert(msg);
      btn.disabled = false;
      return;
    }
    if (action === "reset" && data.temp_password) {
      alert(`Temporary password for ${uname}:\n\n${data.temp_password}\n\nShare this with the user; they must change it on first login.`);
    }
    await _adminRefreshTable();
  } catch {
    alert("Network error. Please try again.");
    btn.disabled = false;
  }
}

async function _adminAddUser() {
  const email    = document.getElementById("admin-new-email")?.value.trim() || "";
  const username = document.getElementById("admin-new-username")?.value.trim();
  const tempPassword = document.getElementById("admin-new-temp-password")?.value;
  const role     = document.getElementById("admin-new-role")?.value;
  const msgEl    = document.getElementById("admin-add-msg");

  if (!username || !tempPassword) {
    if (msgEl) { msgEl.textContent = "Username and temporary password are required."; msgEl.className = "admin-msg err"; }
    return;
  }

  if (tempPassword.length < 10) {
    if (msgEl) { msgEl.textContent = "Temporary password must be at least 10 characters."; msgEl.className = "admin-msg err"; }
    return;
  }

  if (msgEl) { msgEl.textContent = ""; msgEl.className = "admin-msg"; }
  const btn = document.getElementById("admin-add-btn");
  if (btn) btn.disabled = true;

  try {
    const body = { username, temp_password: tempPassword, role };
    if (email) body.email = email;
    const resp = await fetch("/admin/users", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(body),
      credentials: "same-origin",
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      if (msgEl) { msgEl.textContent = data?.detail || "Failed to add user."; msgEl.className = "admin-msg err"; }
      if (btn) btn.disabled = false;
      return;
    }
    if (msgEl) { msgEl.textContent = `User ${username} created. They must change the password on first login.`; msgEl.className = "admin-msg ok"; }
    document.getElementById("admin-new-email").value = "";
    document.getElementById("admin-new-username").value = "";
    document.getElementById("admin-new-temp-password").value = "";
    await _adminRefreshTable();
  } catch {
    if (msgEl) { msgEl.textContent = "Network error. Please try again."; msgEl.className = "admin-msg err"; }
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// User bar in sidebar header
// ---------------------------------------------------------------------------
function _renderUserBar(user) {
  const bar = document.getElementById("auth-user-bar");
  if (!bar) return;
  if (!user) {
    bar.innerHTML = `<button class="auth-signin-btn" id="auth-open-login">Sign In</button>`;
    bar.querySelector("#auth-open-login").addEventListener("click", () => _showLoginForm());
    return;
  }
  // Gate CSV download button visibility by role
  const dlBtn = document.getElementById("btn-download");
  if (dlBtn) dlBtn.classList.toggle("hidden", !_canDownloadCsv());
  // Gate "Import from CRM" (outreach upload) — stricter gate than CSV
  // download. Only power_user / owner / developer (Mailer + Phone Tracking,
  // 2026-06-03). Members can download CSVs but can't upload outreach data.
  //
  // Toggle the WHOLE container (outreach-tools section), not just the
  // button — the button no longer lives inside active-item-actions so
  // we can't rely on the workspace-loaded gate to hide it.
  const outreachTools = document.getElementById("outreach-tools");
  if (outreachTools) outreachTools.classList.toggle("hidden", !_isPowerUserOrAbove());

  bar.innerHTML = `
    <div style="position:relative;">
      <button class="auth-username-btn" id="auth-user-menu-btn" title="Account menu">${_esc(user.username)}</button>
      <div id="auth-dropdown" class="auth-dropdown hidden">
        <button class="auth-dropdown-item" id="auth-menu-chpw">Change Password</button>
        ${_isAdmin() ? `<button class="auth-dropdown-item" id="auth-menu-admin">Manage Users</button>` : ""}
        <div class="auth-dropdown-divider"></div>
        <button class="auth-dropdown-item danger" id="auth-menu-signout">Sign Out</button>
      </div>
    </div>`;

  const menuBtn = bar.querySelector("#auth-user-menu-btn");
  const dropdown = bar.querySelector("#auth-dropdown");

  menuBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    _authDropdownOpen = !_authDropdownOpen;
    dropdown.classList.toggle("hidden", !_authDropdownOpen);
  });

  document.addEventListener("click", function _closeDropdown() {
    if (!document.getElementById("auth-dropdown")) {
      // bar was re-rendered; remove stale listener
      document.removeEventListener("click", _closeDropdown);
      return;
    }
    dropdown.classList.add("hidden");
    _authDropdownOpen = false;
  }, { once: false });

  bar.querySelector("#auth-menu-chpw")?.addEventListener("click", () => {
    dropdown.classList.add("hidden");
    _showChangePasswordForm(false);
  });
  bar.querySelector("#auth-menu-admin")?.addEventListener("click", () => {
    dropdown.classList.add("hidden");
    _showAdminPanel();
  });
  bar.querySelector("#auth-menu-signout")?.addEventListener("click", () => {
    dropdown.classList.add("hidden");
    _doSignOut();
  });
}

async function _doSignOut() {
  try {
    await fetch("/auth/logout", {
      method: "POST",
      headers: { ...authHeaders() },
      credentials: "same-origin",
    });
  } catch { /* ignore */ }
  _currentUser = null;
  _renderUserBar(null);
  _applyRoleVisibility();
  _showLoginForm();
}

// ---------------------------------------------------------------------------
// Handle 401 / FORCE_PASSWORD_CHANGE from any API response
// Call this from any catch block where you receive a 401 resp.
// ---------------------------------------------------------------------------
function _handle401() {
  _currentUser = null;
  _renderUserBar(null);
  _applyRoleVisibility();
  _showLoginForm("Your session expired. Please sign in again.");
}

function _handleForcePasswordChange() {
  _showChangePasswordForm(true);
}

async function _loadAreaFromShareId(shareId) {
  try {
    const resp = await fetch(`/api/area/by-share-id/${encodeURIComponent(shareId)}`, {
      credentials: "same-origin",
    });
    if (!resp.ok) {
      if (resp.status === 404) {
        _showToast("Shared link not found - it may have been deleted", "error");
      } else {
        _showToast("Could not open shared workspace", "error");
      }
      return;
    }
    const area = await resp.json();
    const seedParcels = Array.isArray(area.seed_parcels) ? area.seed_parcels : [];
    await restoreSavedArea(_normalizeSavedAreaRow(area));
    // Render the workspace's bonded seed targets as gold-glow polygons.
    // Skipped automatically by _renderSavedParcelOutline if the same account_num
    // is already on the map (e.g., user opened their own workspace and the
    // seed is also one of their standalones).
    seedParcels.forEach((sp) => {
      const normalized = _normalizeSavedParcelRow(sp);
      if (normalized.geometry) {
        _renderSavedParcelOutline(normalized);
      }
      _renderSavedTargetStar(normalized);
    });

    // Sprint 1 multi-user collab (spec §4.2): auto-JOIN replaces auto-FORK.
    // Opening a share link makes the calling user an 'editor' on the owner's
    // row instead of cloning a divergent copy. The explicit "Make my copy"
    // sidebar button remains available for users who DO want a fork.
    //
    // Best-effort owner skip: if this share_id is already in our cache, we
    // already have a membership for it (owner or backfilled).
    const isOwner = _savedAreasCache.some((a) => String(a.share_id || "") === String(shareId));
    if (_currentUser && !isOwner) {
      try {
        const joined = await _apiJson(`/api/areas/by-share-id/${encodeURIComponent(shareId)}/join`, {
          method: "POST",
          headers: { "Content-Type": "application/json", ...authHeaders() },
          body: "{}",
        });
        // Cache hydration (Copilot S-2): the joined area is NOT in
        // _savedAreasCache yet because the initial GET /api/areas list
        // ran before membership was granted. Build the normalized row
        // from the by-share-id payload + the role from the join response.
        const normalized = _normalizeSavedAreaRow({ ...area, role: joined.role });
        _savedAreasCache.unshift(normalized);
        _clearOriginatorStar();
        _setCurrentTargetParcel(null);
        _currentLoadedAreaId = joined.area_id;
        _renderViewToggle();  // area id set → reveal the ARV/NBV/Export toggle
    _updateActiveItemRenameVisibility();  // area id set → reveal rename pencil + share button (idempotent)
        // Reload stored values for the shared area (membership-gated GET
        // succeeds now that the editor row exists). Force a clean reload by
        // resetting the cache guard first — restoreSavedArea already fired a
        // pre-join load that failed (403, not a member yet), and without this
        // reset the _storedValueOnAreaChange guard could short-circuit on the
        // stale/blank cached state and leave the panel empty until refresh.
        _storedValueAreaId = null;
        _storedValueState = null;
        _storedValueOnAreaChange(_currentLoadedAreaId);
        // G7 fence (per-view ratings spec §9): unlike stored values, comp +
        // parcel RATINGS are NOT membership-gated — comps load via the
        // un-authed /api/propelio/by-saved-area endpoint, and parcel ratings
        // via /api/analyze which scopes by workspace_id (not membership; only
        // _saved_area_exists). So the pre-join restoreSavedArea() hydrate above
        // already returned the correct ratings (incl. ratings_by_view) and there
        // is NO 403-before-join race to repair here. Intentionally no rating
        // re-fetch. (Verified 2026-06-29; see spec §9 G7.)
        _syncTabTitle();
        _selectedSavedItemId = joined.area_id;
        // Carry the originator TARGET star through to the editor's view.
        // Per spec §5: editors see the OWNER's originator (subject is
        // workspace-scoped, not per-user).
        if (normalized.originator_parcel_county && normalized.originator_parcel_account_num) {
          _setCurrentTargetParcel({
            county: normalized.originator_parcel_county,
            account: normalized.originator_parcel_account_num,
          });
          void _renderOriginatorTargetStar(
            normalized.originator_parcel_county,
            normalized.originator_parcel_account_num,
          );
        }
        renderSavedAreasList();
        // Refetch subject_properties so the shared area's originator shows
        // the gold outline + star (subject-property redesign carry-over).
        await _reloadSavedResources().catch((err) =>
          console.warn("[auto-join] post-join resource reload failed:", err)
        );
        const toastMsg = joined.already_member
          ? `Already shared with you: ${area.name}`
          : `Shared with you - "${area.name}"`;
        _showToast(toastMsg);
      } catch (joinErr) {
        console.warn("auto-join failed; keeping read-only view", joinErr);
        _showToast("Could not access shared workspace", "error");
      }
    }
  } catch (err) {
    console.error("Deep-link load failed", err);
    _showToast("Could not open shared workspace", "error");
  }
}

// ---------------------------------------------------------------------------
// Startup — check if already logged in
// ---------------------------------------------------------------------------
(async function initAuth() {
  // Render the "Sign In" button immediately while we check.
  _renderUserBar(null);
  _applyRoleVisibility();

  try {
    const resp = await fetch("/auth/me", { credentials: "same-origin" });
    if (resp.status === 401) {
      _showLoginForm();
      return;
    }
    if (resp.status === 403) {
      const data = await resp.json().catch(() => ({}));
      if (data?.code === "FORCE_PASSWORD_CHANGE_REQUIRED") {
        _showLoginForm("Your session is active but requires a password change.");
      } else {
        _showLoginForm();
      }
      return;
    }
    if (resp.ok) {
      const data = await resp.json().catch(() => ({}));
      _currentUser = data.user || data;
      _renderUserBar(_currentUser);
      _applyRoleVisibility();
      _appShellReady = true;
      _maybeShowUpdateBanner();
      await _reloadSavedResources().catch((err) => console.error("load saved resources failed", err));
      _maybeShowImportBanner();
      const pendingShareId = _pendingAreaShareId;
      _pendingAreaShareId = null;
      if (pendingShareId) {
        await _loadAreaFromShareId(pendingShareId);
      }
      if (_currentUser?.force_password_change) {
        _showChangePasswordForm(true);
      }
    } else {
      _showLoginForm();
    }
  } catch {
    // Offline/network error — show login form so user knows something is wrong.
    _showLoginForm("Could not reach server. Check your connection.");
  }
})();

// ---------------------------------------------------------------------------
// Tiny XSS-safe string escape for dynamic HTML above
// ---------------------------------------------------------------------------
function _esc(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// =============================================================================
// EXPERIMENTAL: Propelio Deep Pull (dev-only, throwaway scaffold)
// Removed once the permanent comps DB + production deep-pull is built.
// =============================================================================

let _deepPullPollTimer = null;
let _activeDeepPullJobId = null;

function _showDeepPullBanner(text) {
  const banner = document.getElementById("deep-pull-banner");
  const textEl = document.getElementById("deep-pull-banner-text");
  if (banner) banner.classList.remove("hidden");
  if (textEl) textEl.textContent = text;
}

function _updateDeepPullBanner(status) {
  const textEl = document.getElementById("deep-pull-banner-text");
  if (!textEl) return;
  // Infer total passes from job_id prefix: rr_* = Refresh Recent (3),
  // dp_* (default) = Get Comps deep pull (6).
  const jobId = String(_activeDeepPullJobId || "");
  // Contract: 3 must match api/propelio/deep_pull.py:PASSES_RECENT_COUNT.
  const totalPasses = jobId.startsWith("rr_") ? 3 : 6;
  const passCount = `${status.passes_completed}/${totalPasses}`;
  const captured = Number(status?.total_unique_comps || 0);
  const netNew = Number(status?.net_new_comps || 0);
  if (jobId.startsWith("rr_")) {
    textEl.textContent = `Pass ${passCount} done · ${captured} captured`;
    return;
  }
  textEl.textContent = `${status.status} - Pass ${passCount}, ${captured} captured (${netNew} net-new). Don't refresh.`;
}

function _hideDeepPullBanner() {
  const banner = document.getElementById("deep-pull-banner");
  if (banner) banner.classList.add("hidden");
}

async function startDeepPull() {
  const address = _lastSearchedAddress;
  if (!address) {
    console.warn("[deep-pull] no target address. Search for an address first.");
    _showDeepPullBanner("No target address - search for an address first");
    setTimeout(_hideDeepPullBanner, 4000);
    return;
  }

  if (_activeDeepPullJobId) {
    console.log("[deep-pull] already running, ignoring duplicate start");
    return;
  }

  console.log("[deep-pull] starting for address:", address);
  try {
    const resp = await _apiJson("/api/propelio/deep-pull/start", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({
        target_address: address,
        saved_area_id: _currentLoadedAreaId || null,
      }),
    });
    _activeDeepPullJobId = resp.job_id;
    console.log("[deep-pull] job started:", resp);
    _setPropelioPolygonButtonState({ text: "Deep Pull", disabled: true });
    _showDeepPullBanner("Pass 0/6, queued - warming up... Don't refresh.");
    if (_deepPullPollTimer) clearInterval(_deepPullPollTimer);
    _deepPullPollTimer = setInterval(_pollDeepPullStatus, 5000);
  } catch (err) {
    console.error("[deep-pull] start failed:", err);
    _showDeepPullBanner("Deep pull failed to start (see console)");
    setTimeout(_hideDeepPullBanner, 4000);
  }
}

async function _pollDeepPullStatus() {
  if (!_activeDeepPullJobId) return;
  try {
    const resp = await _apiJson(`/api/propelio/deep-pull/status/${_activeDeepPullJobId}`);
    console.log("[deep-pull] status tick:", resp);
    _updateDeepPullBanner(resp);
    if (["completed", "saturated", "stopped", "error", "blocked"].includes(resp.status)) {
      if (_deepPullPollTimer) {
        clearInterval(_deepPullPollTimer);
        _deepPullPollTimer = null;
      }
      const finishedJobId = _activeDeepPullJobId;
      _activeDeepPullJobId = null;
      console.log("[deep-pull] FINAL summary:", resp);
      const captured = Number(resp?.total_unique_comps || 0);
      const netNew = Number(resp?.net_new_comps || 0);
      // Contract: 3 must match api/propelio/deep_pull.py:PASSES_RECENT_COUNT.
      const totalPasses = String(finishedJobId).startsWith("rr_") ? 3 : 6;
      if (String(finishedJobId).startsWith("rr_")) {
        _showDeepPullBanner(
          `Quick sweep · ${resp.passes_completed}/${totalPasses} passes · ${captured} captured · ${netNew} net-new`
        );
      } else {
        _showDeepPullBanner(
          `Job ${resp.status} - ${resp.passes_completed}/${totalPasses} passes, ${captured} captured (${netNew} net-new). Job ID: ${finishedJobId}`
        );
      }
      try {
        await _fetchPolygonCacheOnly();
      } catch (err) {
        console.warn("[deep-pull] post-complete cache refresh failed:", err);
      }
      _setPropelioPolygonButtonState({ text: "Deep Pull", disabled: false });
      setTimeout(_hideDeepPullBanner, 6000);
    }
  } catch (err) {
    console.error("[deep-pull] poll failed:", err);
  }
}

async function stopDeepPull() {
  if (!_activeDeepPullJobId) return;
  console.log("[deep-pull] stop requested for", _activeDeepPullJobId);
  try {
    await _apiJson(`/api/propelio/deep-pull/stop/${_activeDeepPullJobId}`, {
      method: "POST",
      headers: authHeaders(),
    });
  } catch (err) {
    console.error("[deep-pull] stop failed:", err);
  }
}

document.getElementById("btn-deep-pull-stop")?.addEventListener("click", stopDeepPull);

// ─── Stored Values sidebar block (Phase 3 wiring) ────────────────────────
// Workspace-scoped per-saved-area value tracking. Backed by
// stored_value_entries. Manual fields: arv, nbv, tdpp, rehab_needed.
// Calc fields (computed locally + server-side):
//   mao_arv             = arv * 0.75 - rehab_needed
//   tdpp_minus_mao_arv  = tdpp - mao_arv
//
// K4 (2026-05-27 roadmap, Option B product call): NBV is a NEW manual
// field. Typing in NBV auto-fills TDPP with NBV × 0.2 (see
// _storedValueOnNbvInput below). TDPP stays user-editable so the
// operator can override the auto-filled value when needed (e.g.,
// negotiated price differs from the .2 multiplier). NBV itself is just
// stored — no backend calc reads it.

const _STORED_VALUE_FIELDS = ["arv", "nbv", "tdpp", "rehab_needed", "mao_arv", "tdpp_minus_mao_arv"];
const _STORED_VALUE_MANUAL_FIELDS = ["arv", "nbv", "tdpp", "rehab_needed"];
const _STORED_VALUE_CALC_FIELDS = ["mao_arv", "tdpp_minus_mao_arv"];
const _STORED_VALUE_MULTIPLIER = 0.75;
const _STORED_VALUE_NBV_TO_TDPP_MULTIPLIER = 0.2;  // K4: NBV × 0.2 auto-fills TDPP
const _STORED_VALUE_NUMERIC_MAX = 999_999_999;
const _STORED_VALUE_DEBOUNCE_MS = 600;

let _storedValueAreaId = null;
let _storedValueState = null;
let _storedValueClientSeq = 1;
let _storedValueAbortController = null;
let _storedValueDebounceTimer = null;
let _storedValuePendingFields = new Set();
let _storedValueInflightField = null;

function _storedValueBlankState() {
  const state = {};
  for (const key of _STORED_VALUE_FIELDS) {
    state[key] = {
      numeric_value: null,
      comment_text: "",
      value_source: _STORED_VALUE_CALC_FIELDS.includes(key) ? "calculated" : "manual",
      client_seq: 0,
    };
  }
  return state;
}

function _storedValueParseNumber(raw) {
  if (raw == null) return null;
  // Defensive: explicit lowercase here in addition to parseShorthand's internal
  // .toLowerCase(). Some browser/extension combos appear to behave differently
  // for "1m" vs "1M" in numeric inputmode fields; normalizing at the boundary
  // removes any path-dependency.
  const str = String(raw).trim().toLowerCase();
  if (str === "") return null;
  // Support k/m shorthand: "300k" → 300000, "1.5m" → 1500000.
  const parsed = parseShorthand(str);
  if (parsed == null || !Number.isFinite(parsed)) return null;
  const n = Math.round(parsed);
  if (n < 0) return 0;
  if (n > _STORED_VALUE_NUMERIC_MAX) return _STORED_VALUE_NUMERIC_MAX;
  return n;
}

function _storedValueFormatDisplay(value) {
  if (value == null) return "";
  return formatNumberWithCommas(value);
}

function _storedValueComputeCalc(state) {
  const arv = state.arv.numeric_value;
  const tdpp = state.tdpp.numeric_value;
  const rehab = state.rehab_needed.numeric_value;
  const mao = (arv != null && rehab != null)
    ? Math.round(arv * _STORED_VALUE_MULTIPLIER - rehab)
    : null;
  const tdppMinusMao = (tdpp != null && mao != null)
    ? Math.round(tdpp - mao)
    : null;
  return { mao_arv: mao, tdpp_minus_mao_arv: tdppMinusMao };
}

function _storedValueApplyState(state) {
  const active = document.activeElement;
  for (const key of _STORED_VALUE_FIELDS) {
    const input = document.getElementById(`sv-input-${key}`);
    const comment = document.getElementById(`sv-comment-${key}`);
    if (input && input !== active) {
      input.value = _storedValueFormatDisplay(state[key].numeric_value);
    }
    if (comment && comment !== active) {
      comment.value = state[key].comment_text || "";
    }
  }
}

let _storedValueFlashTimer = null;
function _storedValueSetStatus(state, label) {
  const chip = document.getElementById("stored-value-status");
  if (!chip) return;
  if (_storedValueFlashTimer) {
    clearTimeout(_storedValueFlashTimer);
    _storedValueFlashTimer = null;
  }
  chip.setAttribute("data-state", state);
  const defaults = { idle: "", saving: "Saving…", flash: "Saved ✓", error: "Retry" };
  chip.textContent = label || defaults[state] || state;
  if (state === "flash") {
    _storedValueFlashTimer = setTimeout(() => {
      _storedValueFlashTimer = null;
      const c = document.getElementById("stored-value-status");
      if (c && c.getAttribute("data-state") === "flash") {
        c.setAttribute("data-state", "idle");
        c.textContent = "";
      }
    }, 1200);
  }
}

function _storedValueRecalcAndRender() {
  if (!_storedValueState) return;
  const calc = _storedValueComputeCalc(_storedValueState);
  _storedValueState.mao_arv.numeric_value = calc.mao_arv;
  _storedValueState.tdpp_minus_mao_arv.numeric_value = calc.tdpp_minus_mao_arv;
  _storedValueApplyState(_storedValueState);
}

async function _storedValueLoadFromServer(areaId, signal) {
  try {
    const resp = await fetch(`/api/areas/${encodeURIComponent(areaId)}/stored-value`, {
      credentials: "same-origin",
      signal,
    });
    if (!resp.ok) {
      if (resp.status === 404) return _storedValueBlankState();
      // 403 = not a member YET (e.g. stored values loaded during share-open,
      // before the auto-join grants editor membership). Return null so it is
      // NOT cached as a blank state — otherwise the post-join reload would
      // short-circuit on the _storedValueOnAreaChange guard and the panel would
      // stay empty until a manual refresh.
      if (resp.status === 403) return null;
      throw new Error(`stored-value load failed: ${resp.status}`);
    }
    const data = await resp.json();
    const state = _storedValueBlankState();
    for (const key of _STORED_VALUE_FIELDS) {
      if (data[key] && typeof data[key] === "object") {
        state[key].numeric_value = data[key].numeric_value ?? null;
        state[key].comment_text = String(data[key].comment_text || "");
        state[key].value_source = data[key].value_source || state[key].value_source;
        state[key].client_seq = Number(data[key].client_seq || 0);
      }
    }
    return state;
  } catch (err) {
    if (err.name === "AbortError") return null;
    console.error("[stored-value] load failed:", err);
    return _storedValueBlankState();
  }
}

async function _storedValueOnAreaChange(newAreaId) {
  const targetAreaId = newAreaId || null;
  if (_storedValueAreaId === targetAreaId && _storedValueState) return;

  if (_storedValueAbortController) {
    try { _storedValueAbortController.abort(); } catch (_) {}
    _storedValueAbortController = null;
  }
  if (_storedValuePendingFields.size && _storedValueAreaId) {
    try { await _storedValueFlushPending(); } catch (_) {}
  }

  if (_storedValueDebounceTimer) {
    clearTimeout(_storedValueDebounceTimer);
    _storedValueDebounceTimer = null;
  }
  _storedValuePendingFields = new Set();
  _storedValueInflightField = null;
  _storedValueAreaId = targetAreaId;

  const block = document.getElementById("stored-value-block");
  if (!targetAreaId) {
    _storedValueState = null;
    if (block) block.classList.add("hidden");
    _storedValueSetStatus("idle");
    return;
  }

  _storedValueAbortController = new AbortController();
  if (block) block.classList.remove("hidden");
  _storedValueSetStatus("saving", "Loading…");

  const state = await _storedValueLoadFromServer(targetAreaId, _storedValueAbortController.signal);
  if (_storedValueAreaId !== targetAreaId) return;
  if (state == null) return;

  _storedValueState = state;
  _storedValueClientSeq = Math.max(
    _storedValueClientSeq,
    ..._STORED_VALUE_FIELDS.map((k) => state[k].client_seq),
  );
  _storedValueRecalcAndRender();
  _storedValueSetStatus("idle");
}

async function _storedValueSaveField(fieldKey) {
  if (!_storedValueState || !_storedValueAreaId) return;
  const areaIdAtCall = _storedValueAreaId;
  const fieldData = _storedValueState[fieldKey];
  if (!fieldData) return;

  const isCalc = _STORED_VALUE_CALC_FIELDS.includes(fieldKey);
  const seq = ++_storedValueClientSeq;
  fieldData.client_seq = seq;

  const body = {
    field_key: fieldKey,
    numeric_value: isCalc ? null : fieldData.numeric_value,
    comment_text: fieldData.comment_text || "",
    client_seq: seq,
  };

  _storedValueSetStatus("saving", "Saving…");

  // Mike report 2026-06-06: "stored values still not saving... fresh
  // browser fixes." Root cause: this fetch had no timeout, so a hung
  // network kept the outer await pending indefinitely. _storedValueProcessQueue
  // has a try/finally that clears _storedValueInflightField — but only
  // when this function returns. A never-resolving fetch never returns,
  // so the inflight gate stays locked forever and every subsequent
  // field save is silently skipped at the `if (_storedValueInflightField)
  // return;` check. New browser = fresh JS state = no lock.
  //
  // 15s timeout via AbortController bounds the wait. On abort, the
  // fetch rejects with AbortError, the existing catch (below) maps it
  // to a Retry status, the inflight gate releases, the next field can
  // save. Bound is generous enough that real Cloud SQL latency
  // (typical <500ms) never triggers it.
  const saveAbortController = new AbortController();
  const saveAbortTimer = setTimeout(() => saveAbortController.abort(), 15_000);

  try {
    const resp = await fetch(`/api/areas/${encodeURIComponent(areaIdAtCall)}/stored-value`, {
      method: "PUT",
      credentials: "same-origin",
      signal: saveAbortController.signal,
      headers: {
        "Content-Type": "application/json",
        "X-Session-Id": _sseSessionUuid,  // Sprint 3 hotfix: echo for self-echo filter
        ...authHeaders(),
      },
      body: JSON.stringify(body),
    });

    if (_storedValueAreaId !== areaIdAtCall) return;

    if (resp.status === 409) {
      const data = await resp.json().catch(() => ({}));
      const current = data?.detail?.current;
      if (current && current.field_key === fieldKey && _storedValueState) {
        fieldData.client_seq = Math.max(fieldData.client_seq, Number(current.client_seq || 0));
        fieldData.numeric_value = current.numeric_value ?? fieldData.numeric_value;
        fieldData.comment_text = String(current.comment_text || "");
        _storedValueClientSeq = Math.max(_storedValueClientSeq, fieldData.client_seq);
        _storedValueRecalcAndRender();
      }
      _storedValueSetStatus("flash");
      return;
    }

    if (!resp.ok) throw new Error(`stored-value save failed: ${resp.status}`);

    const data = await resp.json();
    if (_storedValueAreaId === areaIdAtCall && _storedValueState) {
      for (const key of _STORED_VALUE_FIELDS) {
        if (data[key] && typeof data[key] === "object") {
          // K4 NBV-wipe fix (2026-05-29): only refresh numeric_value +
          // comment_text for the field that was just saved. The PUT response
          // returns the full DB snapshot for all 6 fields, but for fields
          // OTHER than the one we PUT, that snapshot can be stale relative
          // to local state with pending edits. Specifically: typing NBV
          // queues 'tdpp' first then 'nbv' (the K4 auto-fill cascade); if
          // we clobber state.nbv here from the 'tdpp' PUT's stale snapshot,
          // the subsequent 'nbv' save reads the clobbered value and writes
          // it back, wiping the user's typed NBV in the DB. Tracking
          // client_seq for all fields is fine (and useful for avoiding 409
          // thrash); the data wipe is from cross-field numeric_value/comment
          // overwrite.
          if (key === fieldKey) {
            _storedValueState[key].numeric_value = data[key].numeric_value ?? null;
            _storedValueState[key].comment_text = String(data[key].comment_text || "");
          }
          _storedValueState[key].client_seq = Math.max(
            _storedValueState[key].client_seq,
            Number(data[key].client_seq || 0),
          );
        }
      }
      _storedValueClientSeq = Math.max(
        _storedValueClientSeq,
        ..._STORED_VALUE_FIELDS.map((k) => _storedValueState[k].client_seq),
      );
      _storedValueRecalcAndRender();
    }
    _storedValueSetStatus("flash");
  } catch (err) {
    if (err.name === "AbortError") {
      // Distinguish timeout (saveAbortController.abort fired) from a
      // navigation-time cancel (existing _storedValueAbortController.abort
      // path). Either way the gate releases via the outer finally and the
      // user can retry; the timeout case warns so we can see the symptom
      // in production logs.
      console.warn("[stored-value] save aborted (timeout or navigation) for", fieldKey);
      _storedValueSetStatus("error", "Retry");
      return;
    }
    console.error("[stored-value] save failed:", err);
    _storedValueSetStatus("error", "Retry");
  } finally {
    clearTimeout(saveAbortTimer);
  }
}

async function _storedValueProcessQueue() {
  if (_storedValueInflightField) return;
  while (_storedValuePendingFields.size > 0) {
    const fieldKey = _storedValuePendingFields.values().next().value;
    _storedValuePendingFields.delete(fieldKey);
    _storedValueInflightField = fieldKey;
    try {
      await _storedValueSaveField(fieldKey);
    } finally {
      _storedValueInflightField = null;
    }
  }
}

function _storedValueQueueSave(fieldKey) {
  _storedValuePendingFields.add(fieldKey);
  if (_storedValueDebounceTimer) clearTimeout(_storedValueDebounceTimer);
  _storedValueDebounceTimer = setTimeout(() => {
    _storedValueDebounceTimer = null;
    void _storedValueProcessQueue();
  }, _STORED_VALUE_DEBOUNCE_MS);
}

async function _storedValueFlushPending() {
  if (_storedValueDebounceTimer) {
    clearTimeout(_storedValueDebounceTimer);
    _storedValueDebounceTimer = null;
  }
  await _storedValueProcessQueue();
}

function _storedValueOnNumericInput(fieldKey, raw) {
  if (!_storedValueState) return;
  const parsed = _storedValueParseNumber(raw);
  _storedValueState[fieldKey].numeric_value = parsed;

  // K4 (2026-05-27 — Option B): typing in NBV auto-fills TDPP with
  // NBV × 0.2. TDPP stays user-editable, so the operator can override
  // the auto-filled value afterward by typing in the TDPP input
  // directly (which fires this same handler with fieldKey === "tdpp"
  // and updates only TDPP, not NBV).
  if (fieldKey === "nbv") {
    const newTdpp = (parsed != null)
      ? Math.round(parsed * _STORED_VALUE_NBV_TO_TDPP_MULTIPLIER)
      : null;
    _storedValueState.tdpp.numeric_value = newTdpp;
    const tdppInput = document.getElementById("sv-input-tdpp");
    if (tdppInput && tdppInput !== document.activeElement) {
      tdppInput.value = _storedValueFormatDisplay(newTdpp);
    }
    _storedValueQueueSave("tdpp");
  }

  const calc = _storedValueComputeCalc(_storedValueState);
  _storedValueState.mao_arv.numeric_value = calc.mao_arv;
  _storedValueState.tdpp_minus_mao_arv.numeric_value = calc.tdpp_minus_mao_arv;
  for (const calcKey of _STORED_VALUE_CALC_FIELDS) {
    const input = document.getElementById(`sv-input-${calcKey}`);
    if (input) input.value = _storedValueFormatDisplay(_storedValueState[calcKey].numeric_value);
  }
  _storedValueQueueSave(fieldKey);
}

function _storedValueOnCommentInput(fieldKey, raw) {
  if (!_storedValueState) return;
  _storedValueState[fieldKey].comment_text = String(raw || "");
  _storedValueQueueSave(fieldKey);
}

function _storedValueOnNumericBlur(fieldKey, el) {
  if (!_storedValueState) return;
  el.value = _storedValueFormatDisplay(_storedValueState[fieldKey].numeric_value);
  _storedValuePendingFields.add(fieldKey);
  if (_storedValueDebounceTimer) {
    clearTimeout(_storedValueDebounceTimer);
    _storedValueDebounceTimer = null;
  }
  void _storedValueProcessQueue();
}

function _storedValueOnCommentBlur(fieldKey) {
  _storedValuePendingFields.add(fieldKey);
  if (_storedValueDebounceTimer) {
    clearTimeout(_storedValueDebounceTimer);
    _storedValueDebounceTimer = null;
  }
  void _storedValueProcessQueue();
}

function _storedValueOnNumericFocus(fieldKey, el) {
  if (!_storedValueState) return;
  const v = _storedValueState[fieldKey].numeric_value;
  el.value = v == null ? "" : String(v);
}

function _storedValueWireListeners() {
  for (const key of _STORED_VALUE_MANUAL_FIELDS) {
    const input = document.getElementById(`sv-input-${key}`);
    if (input) {
      input.addEventListener("input", (e) => _storedValueOnNumericInput(key, e.target.value));
      input.addEventListener("focus", (e) => _storedValueOnNumericFocus(key, e.target));
      input.addEventListener("blur", (e) => _storedValueOnNumericBlur(key, e.target));
    }
  }
  for (const key of _STORED_VALUE_FIELDS) {
    const comment = document.getElementById(`sv-comment-${key}`);
    if (comment) {
      comment.addEventListener("input", (e) => _storedValueOnCommentInput(key, e.target.value));
      comment.addEventListener("blur", () => _storedValueOnCommentBlur(key));
    }
  }
  const status = document.getElementById("stored-value-status");
  if (status) {
    status.addEventListener("click", () => {
      if (status.getAttribute("data-state") !== "error") return;
      _storedValueSetStatus("saving", "Saving…");
      if (_storedValuePendingFields.size === 0) {
        for (const key of _STORED_VALUE_MANUAL_FIELDS) {
          _storedValuePendingFields.add(key);
        }
      }
      void _storedValueProcessQueue();
    });
  }
  window.addEventListener("beforeunload", () => {
    if (_storedValuePendingFields.size > 0) void _storedValueProcessQueue();
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", _storedValueWireListeners, { once: true });
} else {
  _storedValueWireListeners();
}

// ─── ARV · NBV · Export filter-view toggle (Feature #3, Chunk C) ───
// _setActiveView is the real switch, used by BOTH the toggle buttons and the
// window._llSetActiveView console hook. _renderViewToggle shows/hides the
// in-slot toggle box (under the Clear button) + syncs the active segment.
// All flag-gated: flag OFF ⇒ the box stays hidden and none of this runs against
// a visible element, so today's layout/behavior is byte-for-byte unchanged.
function _renderViewToggle() {
  const block = document.getElementById("view-toggle-block");
  _renderAiBar();   // AI bar mirrors this function's area-loaded gate exactly (Task 3) -- every call site covers both
  if (!block) return;
  const show = ARV_NBV_EXPORT_ENABLED && Boolean(_currentLoadedAreaId);
  block.classList.toggle("hidden", !show);
  if (!show) return;
  block.querySelectorAll(".view-toggle-seg").forEach((seg) => {
    const isActive = seg.getAttribute("data-view") === _activeView;
    seg.classList.toggle("active", isActive);
    seg.setAttribute("aria-selected", isActive ? "true" : "false");
  });
}

// AI bar visibility + active-state (Task 3). Gates: admin only AND
// area-loaded, mirroring _renderViewToggle's own gate exactly -- folded
// into that function above so every one of its ~9 call sites (draw, save,
// load, clear, rename flows, SSE) keeps this in sync with zero new call
// sites to miss. ai-card.js's OWN content (mix/brief) is unaffected --
// it polls and re-renders itself; this only manages the wrapper + the
// AI-mode-on visual treatment.
function _renderAiBar() {
  const block = document.getElementById("ai-bar-block");
  if (!block) return;
  const show = Boolean(window.LL_CONFIG && window.LL_CONFIG.aiEnabled) && _isAdmin() && Boolean(_currentLoadedAreaId);
  block.classList.toggle("hidden", !show);
  const toggle = document.getElementById("ai-mode-toggle");
  if (toggle) {
    toggle.setAttribute("aria-pressed", _aiModeOn ? "true" : "false");
    toggle.classList.toggle("active", _aiModeOn);
    // The lot + sqft bands are +/-20% around THE SUBJECT'S dims. With no subject
    // selected there is nothing to compute from, and AI mode would write the year
    // alone and silently leave lot/sqft blank -- looking broken, and claiming to
    // show "what our system picked for this house" when it does not know which
    // house. auto-match disabled its own checkbox for exactly this reason
    // (_refreshAutoMatchAvailability); that gate was lost when the mode was
    // retired. Restored here: no subject, no claim.
    const _dims = _autoMatchSubjectDims();
    const _ready = Boolean(_dims);
    toggle.disabled = !_ready && !_aiModeOn;   // never trap the user INSIDE the mode
    toggle.title = _ready
      ? "Show what our automation would have picked instead of the current filters"
      : (_lastSubjectProps ? "Target has no lot/sqft data" : "Select a target property first");
  }
  // AI-mode-on must be UNMISTAKABLE (§Task 3): the comp-filter panel also
  // gets a gold treatment while it's showing AI's picks instead of the
  // user's real filters.
  const filterCard = document.getElementById("propelio-filters");
  if (filterCard) filterCard.classList.toggle("ai-mode-active", show && _aiModeOn);
}

function _setActiveView(view) {
  if (!ARV_NBV_EXPORT_ENABLED) return;
  if (view !== "arv" && view !== "nbv" && view !== "export") {
    console.warn("[views] unknown view:", view, "— must be 'arv', 'nbv', or 'export'");
    return;
  }
  if (view === _activeView) return; // already active — no-op
  // Save the departing view's live UI back to its cache. AI MODE FIX
  // (docs/AI/CODER_SPEC_AIMODE_FIX_2026-07-14.md §2.3): unconditionally
  // _viewFilterCache now -- there is no separate AI cache to route around.
  // captureFilterState() already returns the user's real values (AI's six
  // fields are substituted out before this line ever sees them), so this
  // cannot pick up AI's picks even while AI mode is on.
  _viewFilterCache[_activeView] = captureFilterState();
  _activeView = view;
  // Per-view ratings (Chunk E): re-project ratings to the new active view up
  // front, independent of whether the filter restore below actually re-renders
  // (the parcel render is gated on lastAnalysisGeojson). This keeps the comp +
  // parcel `user_rating` projections correct for any subsequent read even on
  // the edge where a render is skipped. The renders below then repaint from
  // this already-projected state.
  _projectCompRatingsForActiveView();
  _projectParcelRatingsForActiveView();
  // Seed-from-ARV on first visit to NBV/Export (spec §5.4): copy the current
  // ARV filters so the view starts from a baseline, not blank, and persist each
  // seeded field via the per-field PATCH (as _views.<view>.*) so it survives a
  // reload.
  let _cached = _viewFilterCache[view];
  let _seeded = false;
  if ((view === "nbv" || view === "export") && !(_cached && _cached.v)) {
    // Seed source is ALWAYS the user's real ARV, never AI's (§2.3 asymmetry)
    // -- Export in particular must seed from the truth even while AI mode is
    // on, since AI does not touch Final. This falls out for free now:
    // _viewFilterCache is always the real cache, so this branch cannot see
    // AI's picks regardless of which view AI mode is currently painting.
    const _arv = _viewFilterCache.arv;
    _cached = (_arv && _arv.v)
      ? JSON.parse(JSON.stringify(_arv))
      : { v: 1, checkboxes: { ...DEFAULT_FILTERS }, numeric: {}, sold: {}, comp: {}, propelio: {} };
    // NBV must NOT inherit ARV's year gate. The year is the one filter whose
    // MEANING INVERTS between the views: on ARV, yearMax = old houses (what
    // you'd flip); on NBV, yearMin = new builds (what you'd construct). Seeding
    // ARV's yearMax onto NBV and then letting auto-match add yearMin leaves BOTH
    // bounds at 2008 — a pool of houses built in exactly one year — and the NBV
    // draft is a PICK, so it still returns a confident, wrong number. NBV starts
    // clean on year and fills its own. (Export is a review of the ARV set and
    // legitimately inherits it.)
    if (view === "nbv" && _cached.propelio) {
      delete _cached.propelio.yearMin;
      delete _cached.propelio.yearMax;
    }
    _viewFilterCache[view] = _cached;
    _seeded = true;
  }
  const _toRestore = (_cached && _cached.v)
    ? _cached
    : { v: 1, checkboxes: { ...DEFAULT_FILTERS }, numeric: {}, sold: {}, comp: {}, propelio: {} };
  restoreFilterState(_toRestore);
  // AI MODE FIX §2.1 — paint the overlay AFTER restoring the user's real
  // cached state, never before. Export has no overlay (AI does not touch
  // Final); off-mode is a no-op since _aiOverlay[view] is null.
  if (_aiModeOn && (view === "arv" || view === "nbv")) {
    _applyAiOverlayToDom(view);
  }
  if (_seeded) {
    // Persist the seed via the per-field PATCH path, but diff against the app
    // DEFAULTS (not empty) so we only PATCH fields that actually DIFFER from
    // defaults. Missing fields fall back to defaults on restore, so a near-
    // default ARV seeds with ~zero PATCHes — which keeps the SSE echo burst
    // tiny and makes rapid view-switching ("wild ape" hammering) smooth.
    // _diffFilterState uses _activeView to build the _views.<view>.* keys.
    _filterSaveLastSnapshot = {
      v: 1,
      checkboxes: { ...DEFAULT_FILTERS },
      numeric: {},
      sold: {},
      comp: {},
      propelio: { ...DEFAULT_PROPELIO_FILTERS },
    };
    _filterSaveQueueSave();
  }
  // Baseline for subsequent user edits on this view.
  _filterSaveLastSnapshot = captureFilterState();
  _renderViewToggle();
  console.info("[views] active view →", _activeView, _seeded ? "(seeded from ARV)" : "");
}

// Wire the visible toggle buttons + keep the console hook pointed at the real fn.
//   window._llSetActiveView('nbv' | 'arv' | 'export')  — dev/power-user tool.
if (ARV_NBV_EXPORT_ENABLED) {
  document.querySelectorAll("#view-toggle-block .view-toggle-seg").forEach((seg) => {
    seg.addEventListener("click", () => _setActiveView(seg.getAttribute("data-view")));
  });
  window._llSetActiveView = _setActiveView;
}

// v1 §2.1 — flush pending filter save on tab close. Mirror of
// stored-values beforeunload inside _storedValueWireListeners above.
window.addEventListener("beforeunload", () => {
  if (_filterSavePending) void _filterSaveProcessQueue();
});