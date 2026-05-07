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
const COUNTY_LABEL_MIN_ZOOM = 9;

const COLORS = {
  single_family: "#2980b9",
  off_market: "#2980b9",
  vacant: "#27ae60",
  multifamily: "#2c2c2c",
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
  commercial: "#6e5c42",
  exempt: "#7f8c8d",
  active: "#a3161a",
};

// Browse layer — renders all county parcels from PMTiles file on GCS.
const PMTILES_URL = "https://storage.googleapis.com/lot-ledger-tiles/parcels.pmtiles";

const TYPE_LABELS = {
  single_family: "Off-Market SFR",
  vacant: "Vacant Lot",
  multifamily: "Multifamily",
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
const CLICK_MODE_STORAGE_KEY = "lot_ledger_click_mode";
const SIDEBAR_SECTION_STATE_STORAGE_KEY = "lot_ledger_sidebar_sections.v1";
const SOLD_COMPS_COLLAPSED_STORAGE_KEY = "lot_ledger_sold_comps_collapsed.v1";

const DEFAULT_FILTERS = {
  active: true,
  sold: true,
  off_market: true,
  vacant: true,
  multifamily: true,
  commercial: true,
  exempt: true,
};

const FILTER_INPUT_IDS = {
  active: "filter-active",
  sold: "filter-sold",
  off_market: "filter-off-market",
  vacant: "filter-vacant",
  multifamily: "filter-multifamily",
  commercial: "filter-commercial",
  exempt: "filter-exempt",
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

const PARCEL_LAYER_KEYS = ["active", "sold", "off_market", "vacant", "multifamily", "commercial", "exempt"];

// -- Click mode helpers (Jump vs Stay) --
let currentClickMode = "jump";

function getClickMode() {
  return currentClickMode;
}

function setClickMode(mode) {
  if (mode !== "stay" && mode !== "jump") mode = "jump";
  currentClickMode = mode;
  localStorage.setItem(CLICK_MODE_STORAGE_KEY, mode);
  updateClickModeButtonState();
}

function updateClickModeButtonState() {
  const jumpBtn = document.querySelector(".click-mode-btn.jump-mode");
  const stayBtn = document.querySelector(".click-mode-btn.stay-mode");
  if (jumpBtn) jumpBtn.classList.toggle("active", currentClickMode === "jump");
  if (stayBtn) stayBtn.classList.toggle("active", currentClickMode === "stay");
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

const map = L.map("map", { zoomControl: true, closePopupOnClick: false }).setView(DALLAS_CENTER, DEFAULT_ZOOM);
const MAP_CANVAS_RENDERER = L.canvas();
const MAP_SVG_RENDERER = L.svg();

const streetLayer = L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", {
  attribution: "&copy; OpenStreetMap contributors &copy; CARTO",
  subdomains: "abcd",
  maxZoom: 20,
});

const contrastLayer = L.tileLayer("https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png", {
  attribution: "&copy; OpenStreetMap contributors &copy; CARTO",
  subdomains: "abcd",
  maxZoom: 20,
});

const satelliteLayer = L.tileLayer(
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  {
    attribution: "Tiles &copy; Esri",
    maxZoom: 20,
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
  { subdomains: "abcd", maxZoom: 20, opacity: 1, pane: "labelsPane" }
);

map.createPane("soldPane");
map.getPane("soldPane").style.zIndex = "640";

map.createPane("countyLabelPane");
map.getPane("countyLabelPane").style.zIndex = "645";
map.getPane("countyLabelPane").style.pointerEvents = "none";

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
// Disable pointer events on the canvas so draw result polygons beneath it
// receive clicks normally. queryTileFeaturesDebug still works via map.on("click").
const _browseContainer = browseLayer.getContainer && browseLayer.getContainer();
if (_browseContainer) _browseContainer.style.pointerEvents = "none";

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
let countyLayer = null;
let countyLabelLayer = null;
let countyVisible = false;
let hoaLayer = null;
let hoaVisible = false;
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
let transientSoldSidebarPopup = null;
let soldCompsSortMode = "price";
let soldCompsCollapsed = (() => {
  try {
    return localStorage.getItem(SOLD_COMPS_COLLAPSED_STORAGE_KEY) === "1";
  } catch (_) {
    return false;
  }
})();
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
let _currentSessionIsNamed = false;
let _savedSessionsCache = [];
let _currentLoadedAreaId = null;
const _initialAreaShareId = (() => {
  try {
    const v = new URLSearchParams(window.location.search).get("area");
    if (v && /^area_[A-Za-z0-9]{10}$/.test(v)) return v;
  } catch {}
  return null;
})();
let _pendingAreaShareId = _initialAreaShareId;

const HOA_COLOR = "#b8860b";

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
  return {
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
  };
}

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
  };
}

function _filterStatesEqual(a, b) {
  const left = _normalizeFilterStateForCompare(a);
  const right = _normalizeFilterStateForCompare(b);
  return JSON.stringify(left) === JSON.stringify(right);
}

function _renderCurrentViewingArea() {
  const host = document.getElementById("saved-area-current");
  const nameEl = document.getElementById("saved-area-current-name");
  if (!host || !nameEl) return;
  if (!_currentLoadedAreaId) {
    host.classList.add("hidden");
    nameEl.textContent = "";
    return;
  }
  const area = _savedAreasCache.find((a) => a.id === _currentLoadedAreaId && a.type === "area");
  if (!area) {
    host.classList.add("hidden");
    nameEl.textContent = "";
    _currentLoadedAreaId = null;
    return;
  }
  nameEl.textContent = area.name || "Saved area";
  host.classList.remove("hidden");
}

function _refreshLoadedAreaUi() {
  if (!_currentLoadedAreaId) return;
  renderSavedAreasList();
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

function restoreFilterState(state) {
  if (!state || typeof state !== "object") return;
  if (Number(state.v || 0) > 1) {
    console.info("[saved-area] skipping newer filter_state version", state.v);
    return;
  }
  if (Number(state.v || 0) !== 1) return;
  if (state.checkboxes && typeof state.checkboxes === "object") Object.assign(filterState, state.checkboxes);
  if (state.numeric && typeof state.numeric === "object") Object.assign(numericFilters, state.numeric);
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
  console.debug("[restoreFilterState] restored", {
    checkboxes: { ...filterState },
    numeric: { ...numericFilters },
    comp: { ...compNumericFilters },
    sold: { ...soldCompsFilter },
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
  _refreshLoadedAreaUi();
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

function loadFilters() {
  try {
    const raw = localStorage.getItem(FILTER_STORAGE_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return;
    filterState = { ...DEFAULT_FILTERS, ...parsed };
  } catch (_) {
    filterState = { ...DEFAULT_FILTERS };
  }
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
  if (p.prop_type === "commercial") return "commercial";
  if (p.prop_type === "exempt") return "exempt";
  return "off_market";
}

function isFeatureVisible(feature) {
  const bucket = classifyFeatureForFilter(feature);
  if (!filterState[bucket]) return false;
  if (!passesNumericFilters(feature)) return false;
  const p = feature?.properties || {};
  const isListingOrSold = Boolean(p.on_redfin || p.sold_comp);
  if (isListingOrSold && !passesCompFilters(feature)) return false;
  return true;
}

function getVisibleFeatureCounts(features) {
  const counts = {
    active: 0,
    off_market: 0,
    vacant: 0,
    multifamily: 0,
    commercial: 0,
    exempt: 0,
  };

  const list = Array.isArray(features) ? features : [];
  list.forEach((feature) => {
    const bucket = classifyFeatureForFilter(feature);
    if (!(bucket in counts) || !isFeatureVisible(feature)) return;
    counts[bucket] += 1;
  });

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
  if (!soldLayerVisible || map.getZoom() < 16) return;

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
    soldStatus.textContent = "Sold comps hidden";
    return;
  }

  const filteredCount = lastSoldPanelPoints.length;
  const totalCount = allSoldPointsRef.length;
  if (soldCompsFiltersAreActive() && filteredCount < totalCount) {
    soldStatus.textContent = `${filteredCount} of ${totalCount} sold comps found`;
    return;
  }

  soldStatus.textContent = `${filteredCount} sold comp${filteredCount !== 1 ? "s" : ""} found`;
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
  const resultsVisible = !document.getElementById("sidebar-results")?.classList.contains("hidden");
  if (!resultsVisible) {
    panel.innerHTML = "";
    return;
  }
  const totalSoldCount = Array.isArray(allSoldPointsRef) ? allSoldPointsRef.length : 0;
  const soldCountNote = `<p class="sidebar-note sold-comps-count-note">${totalSoldCount} sold comp${totalSoldCount === 1 ? "" : "s"} in this area</p>`;

  if (!Array.isArray(allSoldPointsRef) || allSoldPointsRef.length === 0) {
    panel.innerHTML = soldCountNote;
    return;
  }

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
      const bedBathText = `${beds != null ? beds : "?"}bd/${baths != null ? baths : "?"}ba`;
      const yearText = yrBuilt != null ? `${Math.round(yrBuilt)}` : "N/A";
      return `
        <div class="sold-row" data-sold-idx="${idx}" data-lat="${point.lat ?? ""}" data-lng="${point.lng ?? ""}">
          <div class="sold-row-top">
            <span class="sold-row-price">${price}</span>
            <span>${ppsfText}</span>
          </div>
          <div class="sold-row-meta">${sizeText} · ${bedBathText} · Built ${yearText}</div>
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
        <span class="numeric-filter-label">Lot Size (acres)</span>
        <div class="numeric-filter-inputs">
          <input type="text" inputmode="decimal" id="nf-comp-lot-min" placeholder="Min acres" class="nf-input" value="${compNumericFilters.lot_sqft_min == null ? "" : (compNumericFilters.lot_sqft_min / 43560).toFixed(2).replace(/\.00$/, "")}">
          <span class="nf-sep">–</span>
          <input type="text" inputmode="decimal" id="nf-comp-lot-max" placeholder="Max acres" class="nf-input" value="${compNumericFilters.lot_sqft_max == null ? "" : (compNumericFilters.lot_sqft_max / 43560).toFixed(2).replace(/\.00$/, "")}">
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
      <div class="numeric-filter-row">
        <span class="numeric-filter-label">Appraised Value</span>
        <div class="numeric-filter-inputs">
          <input type="text" id="nf-comp-val-min" placeholder="Min (500k)" class="nf-input" inputmode="decimal" value="${compNumericFilters.appr_val_min ?? ""}">
          <span class="nf-sep">–</span>
          <input type="text" id="nf-comp-val-max" placeholder="Max (1m)" class="nf-input" inputmode="decimal" value="${compNumericFilters.appr_val_max ?? ""}">
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
        <span class="numeric-filter-label">Sold Year Built</span>
        <div class="numeric-filter-inputs">
          <input type="number" id="sold-yr-min" placeholder="Min" class="nf-input" min="1800" max="2030" value="${soldCompsFilter.minYearBuilt ?? ""}">
          <span class="nf-sep">–</span>
          <input type="number" id="sold-yr-max" placeholder="Max" class="nf-input" min="1800" max="2030" value="${soldCompsFilter.maxYearBuilt ?? ""}">
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
        <span class="sidebar-label">Comp Filters</span>
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
    try {
      localStorage.setItem(SOLD_COMPS_COLLAPSED_STORAGE_KEY, soldCompsCollapsed ? "1" : "0");
    } catch (_) {}
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
  const soldYrMinInput = panel.querySelector("#sold-yr-min");
  const soldYrMaxInput = panel.querySelector("#sold-yr-max");
  const compLotMinInput = panel.querySelector("#nf-comp-lot-min");
  const compLotMaxInput = panel.querySelector("#nf-comp-lot-max");
  const compYrMinInput = panel.querySelector("#nf-comp-yr-min");
  const compYrMaxInput = panel.querySelector("#nf-comp-yr-max");
  const compValMinInput = panel.querySelector("#nf-comp-val-min");
  const compValMaxInput = panel.querySelector("#nf-comp-val-max");
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
    soldCompsFilter.minYearBuilt = parseIntegerInput(soldYrMinInput);
    soldCompsFilter.maxYearBuilt = parseIntegerInput(soldYrMaxInput);
    applyAndRenderSoldFilters();
    _refreshLoadedAreaUi();
  };

  const applyCompNumericInputFilters = () => {
    bumpUndoPillVersion();
    // Read comp numeric filter inputs  and apply
    _applyCompNumericFilters();
  };

  soldDaysMaxInput?.addEventListener("blur", applySoldCompInputFilters);
  soldDaysMaxInput?.addEventListener("change", applySoldCompInputFilters);
  soldYrMinInput?.addEventListener("blur", applySoldCompInputFilters);
  soldYrMaxInput?.addEventListener("blur", applySoldCompInputFilters);
  soldYrMinInput?.addEventListener("change", applySoldCompInputFilters);
  soldYrMaxInput?.addEventListener("change", applySoldCompInputFilters);

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

  [compValMinInput, compValMaxInput].forEach((inputEl) => {
    inputEl?.addEventListener("blur", () => {
      normalizeShorthandInput(inputEl);
      applyCompNumericInputFilters();
    });
    inputEl?.addEventListener("change", () => {
      normalizeShorthandInput(inputEl);
      applyCompNumericInputFilters();
    });
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
  // First load — fetch from API
  btn.textContent = "Loading...";
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
    btn.textContent = "HOA";
    btn.classList.add("active");
  } catch (e) {
    btn.textContent = "HOA";
    console.error("HOA layer load failed", e);
  }
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

  if (btn) btn.textContent = "...";
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
      L.marker(bounds.getCenter(), {
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
    if (btn) {
      btn.textContent = "CNTY";
      btn.classList.add("active");
    }
    _updateCountyLabelVisibility();
  } catch (e) {
    if (btn) btn.textContent = "CNTY";
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
    shared_by_username: area.shared_by_username || null,
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
  _restoreAllSavedParcelOutlines();
  renderSavedAreasList();
  renderSavedSessionsList();
}

function _isLoadedAreaWithFilterDrift(area) {
  if (!area || area.type !== "area") return false;
  if (area.id !== _currentLoadedAreaId) return false;
  return !_filterStatesEqual(captureFilterState(), area.filter_state);
}

async function _updateSavedAreaFilters(area, actionBtn) {
  if (!area || area.type !== "area") return;
  const nextState = captureFilterState();
  if (_filterStatesEqual(nextState, area.filter_state)) {
    renderSavedAreasList();
    return;
  }
  bumpUndoPillVersion();
  if (actionBtn) {
    actionBtn.disabled = true;
    actionBtn.textContent = "Saving...";
  }
  try {
    await _apiJson(`/api/areas/${encodeURIComponent(area.id)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ filter_state: nextState }),
    });
    area.filter_state = _normalizeFilterStateForCompare(nextState);
    if (actionBtn) actionBtn.textContent = "✓ Updated";
    setTimeout(() => {
      if (actionBtn) actionBtn.disabled = false;
      renderSavedAreasList();
      _updateUpdateAreaButtonVisibility();
    }, 700);
  } catch (err) {
    console.error("[updateSavedAreaFilters] failed", err);
    if (actionBtn) {
      actionBtn.disabled = false;
      actionBtn.textContent = "Update";
    }
    await _reloadSavedResources();
  }
}

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
  const created = await _apiJson("/api/areas", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({
      name: trimmed,
      type: "area",
      polygon: lastDrawnLatLngs,
      filter_state: captureFilterState(),
      job_id: currentJobId || null,
    }),
  });
  const normalized = _normalizeSavedAreaRow(created);
  _savedAreasCache.unshift(normalized);
  // Mark the just-saved area as the currently-loaded one so the Update button
  // becomes available the moment the user tweaks any filter after saving.
  _currentLoadedAreaId = normalized.id;
  renderSavedAreasList();
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
  } else {
    await _apiJson(`/api/areas/${encodeURIComponent(item.id)}`, {
      method: "DELETE",
      headers: { ...authHeaders() },
    });
    _savedAreasCache = _savedAreasCache.filter((a) => a.id !== item.id);
    if (_currentLoadedAreaId === item.id) _currentLoadedAreaId = null;
  }
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

const SAVED_PARCEL_COLOR = "#e67e22";

function _renderSavedParcelOutline(area) {
  if (savedParcelLayers[area.account_num]) return; // already on map
  if (!area.geometry || !["Polygon", "MultiPolygon"].includes(area.geometry.type)) return;
  const layer = L.geoJSON({ type: "Feature", geometry: area.geometry, properties: {} }, {
    style: { color: SAVED_PARCEL_COLOR, weight: 3, fill: false, interactive: false },
    interactive: false,
  }).addTo(savedParcelLayer);
  savedParcelLayers[area.account_num] = layer;
}

async function saveParcel(account_num, county, addr, lat, lng, geometry) {
  if (!account_num) return;
  bumpUndoPillVersion();
  const created = await _apiJson("/api/parcels", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({
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
    }),
  });
  const row = _normalizeSavedParcelRow(created);
  _savedParcelsCache = _savedParcelsCache.filter((p) => !(p.account_num === row.account_num && p.county === row.county));
  _savedParcelsCache.unshift(row);
  renderSavedAreasList();
  _renderSavedParcelOutline(row);
}

function _restoreAllSavedParcelOutlines() {
  Object.values(savedParcelLayers).forEach((layer) => savedParcelLayer.removeLayer(layer));
  Object.keys(savedParcelLayers).forEach((key) => delete savedParcelLayers[key]);
  _savedParcelsCache.forEach(_renderSavedParcelOutline);
}

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
  };
}

function _restoreUndoSnapshot(snapshot) {
  if (!snapshot) return;
  snapshot.abortCtrl?.abort();
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
  if (!_activeParcelPopupState?.accountNum) return;
  const layer = _renderedParcelPopupLayers.get(_activeParcelPopupState.accountNum);
  if (!layer) return;
  _suspendViewportRender();
  setTimeout(() => {
    if (!_activeParcelPopupState?.accountNum) return;
    const nextLayer = _renderedParcelPopupLayers.get(_activeParcelPopupState.accountNum);
    if (!nextLayer || !map.hasLayer(nextLayer)) return;
    nextLayer.openPopup();
  }, 0);
}

async function restoreSavedArea(area, options = {}) {
  const rowEl = options.rowEl || null;
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
              highlightLayer = L.geoJSON(detail, {
                style: { color: "#f1c40f", weight: 3, fill: false, interactive: false },
                interactive: false,
              }).addTo(map);
            }
          }
        } catch (e) {
          console.warn("Saved location footprint lookup failed", e);
        }
        if (!highlightLayer) {
          highlightLayer = L.circleMarker(latlng, {
            radius: 14, color: "#f1c40f", weight: 3,
            fillColor: "#f1c40f", fillOpacity: 0.12,
            interactive: false,
          }).addTo(map);
        }
        window._searchHighlight = highlightLayer;
      })();
    };
    map.once("moveend", window._searchMoveEndHandler);
    return;
  }

  if (area.type === "parcel") {
    _renderSavedParcelOutline(area);
    const clickMode = getClickMode();
    if (clickMode === "jump") {
      map.flyTo([area.lat, area.lng], 16);
    } else {
      // Stay mode: only pan if parcel is off-screen
      if (!isPointInViewport([area.lat, area.lng])) {
        map.setView([area.lat, area.lng], map.getZoom());
      }
    }
    return;
  }

  if (rowEl) rowEl.classList.add("row-shimmer");
  _setSessionCacheNote("");
  const savedFilterState = area.filter_state && typeof area.filter_state === "object"
    ? area.filter_state
    : null;
  clearDrawResults();
  if (savedFilterState) {
    restoreFilterState(savedFilterState);
  }
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

  const includeRedfin = true;
  const includeSold = Boolean(filterState.sold);
  document.getElementById("sidebar-instructions")?.classList.add("hidden");
  document.getElementById("sidebar-results")?.classList.add("hidden");
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
    _currentLoadedAreaId = area.id;
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
    document.getElementById("sidebar-instructions")?.classList.remove("hidden");
  } finally {
    if (rowEl) rowEl.classList.remove("row-shimmer");
  }
}

async function restoreNamedSession(session, options = {}) {
  const rowEl = options.rowEl || null;
  if (!session.latlngs || session.latlngs.length < 3) {
    console.warn("[restoreNamedSession] session has no polygon", session);
    return;
  }
  if (rowEl) rowEl.classList.add("row-shimmer");
  _currentLoadedAreaId = null;
  renderSavedAreasList();
  const savedFilterState = session.filter_state && typeof session.filter_state === "object"
    ? session.filter_state
    : null;
  clearDrawResults();
  if (savedFilterState) {
    restoreFilterState(savedFilterState);
  }
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
  const polygon = session.latlngs.map(([lat, lng]) => [lng, lat]);
  lastDrawnLatLngs = session.latlngs;
  lastPolygon = polygon;
  if (map.hasLayer(browseLayer)) browseLayer.remove();
  const includeSold = Boolean(filterState.sold);
  document.getElementById("sidebar-instructions")?.classList.add("hidden");
  document.getElementById("sidebar-results")?.classList.add("hidden");
  document.getElementById("sidebar-loading")?.classList.remove("hidden");
  document.getElementById("redfin-status").textContent = "Loading session…";
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
    document.getElementById("sidebar-instructions")?.classList.remove("hidden");
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

function _renderList(sectionId, listId, items) {
  const section = document.getElementById(sectionId);
  const list = document.getElementById(listId);
  if (!section || !list) return;
  section.classList.toggle("hidden", items.length === 0);
  _renderCurrentViewingArea();
  list.innerHTML = items.map((area) => {
    const date = new Date(area.savedAt).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    const icon = area.type === "parcel" ? "📌" : area.type === "location" ? "📍" : "▭";
    const chip = _formatFilterDiffChip(area);
    const canRename = area.type !== "parcel";
    const canShare = area.type === "area" && Boolean(String(area.share_id || "").trim());
    const activeClass = area.id === _currentLoadedAreaId ? " saved-area-row-active" : "";
    const secondaryLine = [chip, `saved ${date}`].filter(Boolean).join(" · ");

    // Ownership + role gating for Rename / Delete / Fork
    const isOwn = area.user_id != null
      ? String(area.user_id) === String(_currentUser?.id || "")
      : true; // no user_id on row → treat as own (legacy safe)
    const showFullControls = isOwn || _canEditAnyArea();

    return `
      <div class="saved-area-row${activeClass}" tabindex="0" data-id="${area.id}" data-type="${area.type}">
        <div class="saved-area-main">
          <span class="saved-area-icon">${icon}</span>
          <span class="saved-area-name">${area.name}</span>
          ${showFullControls ? `<button type="button" class="saved-area-quick-delete-btn" data-action="delete" title="Delete">🗑</button>` : ""}
          ${canShare ? `<button type="button" class="saved-area-action-btn saved-area-share-btn" data-action="share" data-share-id="${_esc(area.share_id)}" title="Share">🔗 Share</button>` : ""}
        </div>
        <div class="saved-area-secondary-line">${secondaryLine}</div>
        <div class="saved-area-row-secondary-actions">
          <hr class="saved-area-actions-divider">
          <div class="saved-area-secondary-btns">
            ${!showFullControls && canShare ? `<button type="button" class="saved-area-action-btn" data-action="fork" data-share-id="${_esc(area.share_id)}" title="Make my own copy">📋 Make my copy</button>` : ""}
            ${showFullControls && canRename ? `<button type="button" class="saved-area-action-btn rename" data-action="rename" title="Rename">✎ Rename</button>` : ""}
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
          _savedAreasCache.unshift(_normalizeSavedAreaRow(cloned));
          _currentLoadedAreaId = cloned.area_id;
          renderSavedAreasList();
          _showToast(`Forked → "${cloned.name}"`);
        } catch {
          _showToast("Could not fork area", "error");
        }
        return;
      }
      if (actionEl?.dataset.action === "delete") {
        e.stopPropagation();
        await deleteSavedArea(area);
        return;
      }
      if (actionEl?.dataset.action === "rename") {
        e.stopPropagation();
        await _renameSavedItemInline(area, row);
        return;
      }
      bumpUndoPillVersion();
      const snapshot = _createUndoSnapshot();
      await restoreSavedArea(area, { rowEl: row, undoSnapshot: snapshot });
      const restoredCount = _countRestoredFilterKeys(area.filter_state);
      _showUndoPill(snapshot, restoredCount);
    });
  });
}

function renderSavedAreasList() {
  _renderCurrentViewingArea();
  _renderList("saved-areas", "saved-areas-list", _savedAreasCache.filter((a) => a.type === "area"));
  _renderList("saved-parcels", "saved-parcels-list", [..._savedAreasCache.filter((a) => a.type === "location"), ..._savedParcelsCache]);
  _updateUpdateAreaButtonVisibility();
}

function _updateUpdateAreaButtonVisibility() {
  const btn = document.getElementById("btn-update-saved-area");
  if (!btn) return;
  // Keep the in-flight Saving... state until request completion.
  if (btn.disabled) return;
  if (!_currentLoadedAreaId) {
    btn.classList.add("hidden");
    btn.textContent = "Update";
    return;
  }
  const area = _savedAreasCache.find((a) => a.id === _currentLoadedAreaId && a.type === "area");
  if (!area || !_isLoadedAreaWithFilterDrift(area)) {
    btn.classList.add("hidden");
    btn.textContent = "Update";
    return;
  }
  btn.classList.remove("hidden");
  btn.textContent = "Update";
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
          <span class="saved-area-name">${session.name}</span>
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
    clearBtn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14H6L5 6"></path><path d="M10 11v6"></path><path d="M14 11v6"></path><path d="M9 6V4h6v2"></path></svg>';
    L.DomEvent.on(clearBtn, "click", (e) => {
      L.DomEvent.preventDefault(e);
      clearDrawResults();
    });

    const hoaBtn = L.DomUtil.create("a", "", container);
    hoaBtn.id = "btn-hoa-toggle";
    hoaBtn.href = "#";
    hoaBtn.title = "Toggle HOA zone boundaries";
    hoaBtn.textContent = "HOA";
    L.DomEvent.on(hoaBtn, "click", (e) => {
      L.DomEvent.preventDefault(e);
      toggleHoaLayer();
    });

    const countyBtn = L.DomUtil.create("a", "", container);
    countyBtn.id = "btn-county-toggle";
    countyBtn.href = "#";
    countyBtn.title = "Toggle county boundary lines";
    countyBtn.textContent = "CNTY";
    L.DomEvent.on(countyBtn, "click", (e) => {
      L.DomEvent.preventDefault(e);
      toggleCountyLayer();
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
          try {
            const resp = await fetch(`/api/parcel/near?lat=${slat}&lng=${slng}`);
            if (resp.ok) {
              const detail = await resp.json();
              const geom = detail.geometry;
              if (geom && (geom.type === "Polygon" || geom.type === "MultiPolygon")) {
                highlightLayer = L.geoJSON(detail, {
                  style: { color: "#f1c40f", weight: 3, fill: false, interactive: false },
                  interactive: false,
                }).addTo(map);
              }
            }
          } catch (e) {
            console.warn("Search footprint lookup failed", e);
          }

          if (!highlightLayer) {
            highlightLayer = L.circleMarker(latlng, {
              radius: 14,
              color: "#f1c40f",
              weight: 3,
              fillColor: "#f1c40f",
              fillOpacity: 0.12,
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
  // Restore saved mode from localStorage
  const saved = localStorage.getItem(CLICK_MODE_STORAGE_KEY);
  currentClickMode = (saved === "stay" || saved === "jump") ? saved : "jump";
  
  // Set up button click handlers
  const jumpBtn = document.querySelector(".click-mode-btn.jump-mode");
  const stayBtn = document.querySelector(".click-mode-btn.stay-mode");
  
  if (jumpBtn) {
    jumpBtn.addEventListener("click", () => setClickMode("jump"));
  }
  if (stayBtn) {
    stayBtn.addEventListener("click", () => setClickMode("stay"));
  }
  
  // Apply active state to current mode
  updateClickModeButtonState();
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

  updateSoldStatusText();
}

loadFilters();
syncFilterInputs();

function _applyNumericFilters() {
  _readNumericInputs();
  if (!lastAnalysisGeojson) return;
  const markers = viewportRenderMode
    ? renderViewportFeatures()
    : renderFeatures(lastAnalysisGeojson);
  const counts = getVisibleFeatureCounts(lastAnalysisGeojson.features || []);
  if (lastAnalysisCounts) renderSidebar(counts, markers || {});
  _refreshLoadedAreaUi();
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
    _refreshLoadedAreaUi();
  });
});

document.getElementById("btn-filters-reset")?.addEventListener("click", () => {
  bumpUndoPillVersion();
  filterState = { ...DEFAULT_FILTERS };
  saveFilters();
  syncFilterInputs();
  applyMapVisibilityFilters();
  _refreshLoadedAreaUi();
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
  const markers = viewportRenderMode
    ? renderViewportFeatures()
    : renderFeatures(lastAnalysisGeojson);
  const counts = getVisibleFeatureCounts(lastAnalysisGeojson.features || []);
  if (lastAnalysisCounts) renderSidebar(counts, markers || {});
  _refreshLoadedAreaUi();
}

// Set up event listeners for comp numeric filters
const COMP_NUMERIC_FILTER_INPUTS = [
  { id: "nf-comp-lot-min", key: "lot_sqft_min" },
  { id: "nf-comp-lot-max", key: "lot_sqft_max" },
  { id: "nf-comp-val-min", key: "appr_val_min" },
  { id: "nf-comp-val-max", key: "appr_val_max" },
  { id: "nf-comp-yr-min", key: "yr_built_min" },
  { id: "nf-comp-yr-max", key: "yr_built_max" },
  { id: "nf-comp-sqft-min", key: "sqft_min" },
  { id: "nf-comp-sqft-max", key: "sqft_max" },
];

COMP_NUMERIC_FILTER_INPUTS.forEach(({ id }) => {
  document.getElementById(id)?.addEventListener("input", () => {
    bumpUndoPillVersion();
    _applyCompNumericFilters();
  });
});

["nf-comp-val-min", "nf-comp-val-max"].forEach((id) => {
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
    _applyCompNumericFilters();
  });
  inputEl.addEventListener("change", () => {
    bumpUndoPillVersion();
    normalize();
    _applyCompNumericFilters();
  });
});

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

function makePopupHtml(p) {
  const pseudoFeature = { properties: p };
  const hasVisibleSoldComp = Boolean(p?.sold_comp);
  const statusColor = hasVisibleSoldComp ? SOLD_OUTLINE_COLOR : getColor(pseudoFeature);
  const statusText = hasVisibleSoldComp ? "SOLD" : getStatusLabel(pseudoFeature);
  const soldHeaderPrice = hasVisibleSoldComp
    ? (typeof p.sold_comp?.sold_price === "number"
      ? `$${p.sold_comp.sold_price.toLocaleString()}`
      : String(p.sold_comp?.sold_price || ""))
    : "";
  const verifiedVacant = normalizeVerificationValue(
    verificationByAccount.get(p.account_num) || p.verified_vacant
  );
  const potentialTarget = String(potentialTargetByAccount.get(p.account_num) || p.potential_target || "").trim();
  const row = (label, val) => `<tr><td class="popup-label">${label}</td><td class="popup-val">${val || "N/A"}</td></tr>`;

  // Active listing price in header + delta row in table.
  let activeListingPrice = "";
  let listingDeltaRow = "";
  let redfinListingRow = "";
  if (p.on_redfin && p.redfin_price) {
    activeListingPrice = p.redfin_url
      ? `<a href="${p.redfin_url}" target="_blank" rel="noopener noreferrer">${p.redfin_price}</a>`
      : p.redfin_price;

    // Numeric delta: parse both values
    const rfNum = parseInt(String(p.redfin_price).replace(/[^0-9]/g, ""), 10);
    const dcadRaw = String(p.tot_val || "").replace(/[^0-9]/g, "");
    const dcadNum = dcadRaw ? parseInt(dcadRaw, 10) : NaN;
    if (!isNaN(rfNum) && !isNaN(dcadNum) && dcadNum > 0) {
      const delta = rfNum - dcadNum;
      const pct = ((delta / dcadNum) * 100).toFixed(1);
      const sign = delta >= 0 ? "+" : "";
      const color = delta >= 0 ? "#27ae60" : "#e74c3c";
      listingDeltaRow = `<tr><td class="popup-label">LP vs DCAD</td><td class="popup-val" style="color:${color}">${sign}$${Math.abs(delta).toLocaleString()} (${sign}${pct}%)</td></tr>`;
    }

    // Separate "Listing | View listing" row goes immediately under Potential Target
    if (p.redfin_url) {
      redfinListingRow = row("Listing", `<a href="${p.redfin_url}" target="_blank" rel="noopener noreferrer">View listing</a>`);
    }
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

  return `
      <div class="popup">
        <div class="popup-addr">${p.addr || "Unknown address"}</div>
        <div class="popup-status-row">
          <div class="popup-status" style="color:${statusColor};">${statusText}</div>
          ${activeListingPrice
            ? `<div class="popup-list-price">${activeListingPrice}</div>`
            : soldHeaderPrice
              ? `<div class="popup-sold-price">${soldHeaderPrice}</div>`
              : ""}
        </div>
        <table class="popup-table">
          ${row("Owner", p.owner)}
          ${row("Land Value", p.land_val)}
          ${row("Total Value", p.tot_val)}
          ${listingDeltaRow}
          ${row("Land % of Total", p.land_pct)}
          ${row("Lot Size", p.lot_sqft)}
          ${row("Acres", p.lot_acres)}
          ${row("Frontage", p.frontage)}
          ${row("Depth", p.depth)}
          ${row("State Code", p.state_code)}
          ${row("Zoning", p.zoning)}
          ${row("School District", p.school)}
          ${row("Year Built", p.yr_built)}
          ${row("Living Area", p.sqft && p.sqft !== "N/A" ? p.sqft + " sf" : "N/A")}
          ${row("Verified Vacant", verificationDisplay(verifiedVacant))}
          ${row("Potential Target", potentialTarget || "No")}
          ${redfinListingRow}
          ${soldCompRows}
        </table>
        ${p.account_num ? `<div style="margin-top:8px;border-top:1px solid #e2e8f0;padding-top:6px;display:flex;gap:12px;align-items:center;">
          <a href="#" class="parcel-save-link"
            data-account="${p.account_num}"
            data-county="${p.source_county || "dcad"}"
            data-addr="${(p.addr || "").replace(/"/g, "&quot;")}"
            data-lat="${p.lat || ""}"
            data-lng="${p.lng || ""}"
            style="color:#e67e22;text-decoration:none;font-size:11px;">📌 Save parcel</a>
          <a href="#" class="parcel-clear-link" style="color:#aaa;text-decoration:none;font-size:11px;">✕ Clear</a>
        </div>
        <div style="margin-top:8px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding-top:6px;border-top:1px solid #e2e8f0;">
          <div style="flex:1;display:flex;gap:6px;font-size:11px;">
            <a href="#" class="parcel-verify-yes" data-account="${p.account_num}" data-lat="${p.lat || ""}" data-lng="${p.lng || ""}" style="color:#27ae60;text-decoration:none;">✓ Vacant</a>
            <a href="#" class="parcel-verify-no" data-account="${p.account_num}" data-lat="${p.lat || ""}" data-lng="${p.lng || ""}" style="color:#e74c3c;text-decoration:none;">✗ Not vacant</a>
            <a href="#" class="parcel-verify-clear" data-account="${p.account_num}" style="color:#aaa;text-decoration:none;">· Clear</a>
          </div>
          <div style="flex:1;display:flex;gap:6px;font-size:11px;justify-content:flex-end;">
            <a href="#" class="parcel-target-on" data-account="${p.account_num}" data-lat="${p.lat || ""}" data-lng="${p.lng || ""}" style="color:#e67e22;text-decoration:none;">★ Interested</a>
            <a href="#" class="parcel-target-off" data-account="${p.account_num}" style="color:#aaa;text-decoration:none;">· Clear</a>
          </div>
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

function renderFeatures(geojson) {
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
    const parcelBorderColor = hasVisibleSoldComp ? SOLD_OUTLINE_COLOR : borderColor;
    const parcelBorderWeight = hasVisibleSoldComp ? 3.2 : (p.on_redfin ? 2.8 : 1.5);
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
          fillColor: hasVisibleSoldComp ? SOLD_OUTLINE_COLOR : (p.on_redfin ? COLORS.active : color),
          fillOpacity: (hasVisibleSoldComp || p.on_redfin) ? 0.65 : 0.12,
          weight: parcelBorderWeight,
          opacity: 0.85,
        },
      }).bindPopup(() => makePopupHtml(p), {
        maxWidth: 280,
        autoPan: true,
        autoPanPadding: [10, 50],
        keepInView: true,
      });
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
        fillColor: hasVisibleSoldComp ? SOLD_OUTLINE_COLOR : (p.on_redfin ? COLORS.active : color),
        color: parcelBorderColor,
        weight: 1.5,
        opacity: 1,
        fillOpacity: 0.9,
        bubblingMouseEvents: false,
      }).bindPopup(() => makePopupHtml(p), {
        maxWidth: 280,
        autoPan: true,
        autoPanPadding: [10, 50],
        keepInView: true,
      });
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
  const visibleCounts = Array.isArray(allAnalysisFeatures) && allAnalysisFeatures.length
    ? getVisibleFeatureCounts(allAnalysisFeatures)
    : {
      active: counts.active,
      off_market: counts.off_market,
      vacant: counts.vacant,
      multifamily: counts.multifamily,
      commercial: counts.commercial,
      exempt: counts.exempt,
    };
  const soldCount = Array.isArray(lastSoldPanelPoints) && lastSoldPanelPoints.length
    ? lastSoldPanelPoints.length
    : (Array.isArray(allSoldPointsRef) ? allSoldPointsRef.length : 0);
  const orderedCountRows = [
    ["active", visibleCounts.active],
    ["sold", soldCount],
    ["off_market", visibleCounts.off_market],
    ["vacant", visibleCounts.vacant],
    ["multifamily", visibleCounts.multifamily],
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
  const stamp = [
    now.getFullYear(),
    pad(now.getMonth() + 1),
    pad(now.getDate()),
    "_",
    pad(now.getHours()),
    pad(now.getMinutes()),
  ].join("");
  return `lotledger_${stamp}.csv`;
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
  const mergedCounts = { active: 0, off_market: 0, multifamily: 0, vacant: 0, commercial: 0, exempt: 0, total: filteredFeatures.length };
  for (const feature of filteredFeatures) {
    const p = feature.properties || {};
    if (p.on_redfin) mergedCounts.active++;
    else if (p.prop_type === "multifamily") mergedCounts.multifamily++;
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
  const includeRedfin = true;
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
  _currentSessionIsNamed = false;
  _currentLoadedAreaId = null;
  _setSessionCacheNote("");
  renderSavedAreasList();
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
  document.getElementById("sidebar-instructions").classList.add("hidden");
  document.getElementById("sidebar-results").classList.add("hidden");
  document.getElementById("sidebar-loading").classList.remove("hidden");
  const includeRedfin = true;
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
    document.getElementById("sidebar-instructions").classList.remove("hidden");
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
  currentJobId = null;
  lastPolygon = null;
  lastDrawnLatLngs = null;
  _currentSessionIsNamed = false;
  _currentLoadedAreaId = null;
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
  const soldCompsPanel = document.getElementById("sold-comps-panel");
  if (soldCompsPanel) soldCompsPanel.innerHTML = "";
  document.getElementById("redfin-toggle-status").textContent = "";
  document.getElementById("sold-toggle-status").textContent = "";
  document.getElementById("sidebar-results")?.classList.add("hidden");
  document.getElementById("sidebar-instructions")?.classList.remove("hidden");
  document.getElementById("sidebar-loading")?.classList.add("hidden");
  document.getElementById("btn-draw-clear")?.classList.add("hidden");
  document.getElementById("btn-saved-area-clear")?.classList.add("hidden");
  renderSavedAreasList();
}

map.on("draw:drawstart", () => {
  bumpUndoPillVersion();
  drawHelper.classList.remove("hidden");
  document.getElementById("btn-draw")?.classList.add("active");
  document.getElementById("btn-draw-cancel")?.classList.remove("hidden");
  document.getElementById("btn-draw-clear")?.classList.add("hidden");
  // CSS pointer-events:none (drawing-active class) blocks parcel layer clicks
  // so vertices never get swallowed by underlying markers.
  map.getContainer().classList.add("drawing-active");
  _currentSessionIsNamed = false;
  _currentLoadedAreaId = null;
  _setSessionCacheNote("");
  renderSavedAreasList();
  _updateSaveSessionButtonState();
});

map.on("draw:drawstop", () => {
  drawHelper.classList.add("hidden");
  document.getElementById("btn-draw")?.classList.remove("active");
  document.getElementById("btn-draw-cancel")?.classList.add("hidden");
  map.getContainer().classList.remove("drawing-active");
});

map.on("contextmenu", () => {
  const handler = getPolygonDrawHandler();
  if (handler && handler.enabled()) {
    handler.completeShape();
  }
});

// Sidebar-triggered sold popups should dismiss once the map is moved away.
map.on("movestart", () => {
  closeTransientSoldSidebarPopup();
});

document.addEventListener("keydown", (event) => {
  if (isDrawInputTarget(event.target)) return;
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

document.getElementById("btn-download").addEventListener("click", async () => {
  if (_downloadInFlight) return;
  if (!currentJobId) return;

  _downloadInFlight = true;
  let downloadTriggered = false;
  try {
    _setDownloadButtonState("Preparing CSV…", true);

    const persisted = await persistTagStateForExport((statusText) => {
      _setDownloadButtonState(statusText, true);
    });
    if (!persisted) {
      _resetDownloadButtonState();
      alert("Your analysis session expired. Please re-run the draw/analyze step, then export again.");
      return;
    }

    _setDownloadButtonState("Name export…", true);
    const suggested = makeDefaultCsvName();
    const entered = window.prompt("Name this CSV export:", suggested);
    if (entered === null) {
      _resetDownloadButtonState();
      return;
    }

    const filename = normalizeCsvFilename(entered);
    _setDownloadButtonState("Starting download…", true);
    window.location.href = `/api/download/${currentJobId}?filename=${encodeURIComponent(filename)}`;
    downloadTriggered = true;
    setTimeout(() => {
      _downloadInFlight = false;
      _resetDownloadButtonState();
    }, 4000);
  } finally {
    if (!downloadTriggered) {
      _downloadInFlight = false;
      _resetDownloadButtonState();
    }
  }
});

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
  requestAnimationFrame(() => input.focus());
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

document.getElementById("btn-update-saved-area")?.addEventListener("click", async (e) => {
  if (!_currentLoadedAreaId) return;
  const area = _savedAreasCache.find((a) => a.id === _currentLoadedAreaId && a.type === "area");
  if (!area) return;
  await _updateSavedAreaFilters(area, e.currentTarget);
});

document.getElementById("btn-saved-area-current-clear")?.addEventListener("click", () => {
  bumpUndoPillVersion();
  _currentLoadedAreaId = null;
  renderSavedAreasList();
});

document.getElementById("btn-clear").addEventListener("click", () => {
  clearDrawResults();
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
  // Don't fire browse popup when draw results are visible — let polygon clicks handle it.
  if (lastAnalysisGeojson) return;

  // Any map click clears an orphaned search highlight (search popup was replaced by this click).
  window._clearSearchHighlight?.();

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
    L.popup()
      .setLatLng(ev.latlng)
      .setContent(makePopupHtml(detail.properties || detail))
      .openOn(map);
  } catch (e) {
    console.error("Browse popup failed", e);
  }
});

// Wire up "Save parcel" link in any popup — uses data attributes from makePopupHtml.
map.on("popupopen", (e) => {
  const popupMeta = e.popup?._source?._lotLedgerPopupMeta;
  if (popupMeta?.type === "parcel") {
    _captureParcelPopupState(popupMeta);
    _suspendViewportRender();
  }
  const el = e.popup.getElement();
  if (!el) return;

  const saveLink = el.querySelector(".parcel-save-link");
  if (saveLink) {
    saveLink.addEventListener("click", async (ev) => {
      ev.preventDefault();
      const { account, county, addr, lat, lng } = saveLink.dataset;
      let geometry = null;
      try {
        const resp = await fetch(`/api/parcel/${county}/${account}`);
        if (resp.ok) {
          const detail = await resp.json();
          if (detail.geometry?.type === "Polygon" || detail.geometry?.type === "MultiPolygon") {
            geometry = detail.geometry;
          }
        }
      } catch { /* geometry stays null — outline skipped */ }
      try {
        await saveParcel(account, county, addr, parseFloat(lat), parseFloat(lng), geometry);
        saveLink.textContent = "✓ Saved";
        saveLink.style.color = "#888";
        saveLink.style.pointerEvents = "none";
      } catch (err) {
        console.error("save parcel failed", err);
        saveLink.textContent = "Save failed";
      }
    });
  }

  const clearLink = el.querySelector(".parcel-clear-link");
  if (clearLink) {
    // Hide the clear link if there's no active search highlight.
    if (!window._searchHighlight) clearLink.style.display = "none";
    clearLink.addEventListener("click", (ev) => {
      ev.preventDefault();
      window._clearSearchHighlight?.();
      clearLink.style.display = "none";
    });
  }

  // Verify buttons
  const verifyYes = el.querySelector(".parcel-verify-yes");
  if (verifyYes) {
    verifyYes.addEventListener("click", (ev) => {
      ev.preventDefault();
      const { account, lat, lng } = verifyYes.dataset;
      setVerification(account, "yes", parseFloat(lat), parseFloat(lng));
      persistSingleTag(account, "verified_vacant", "yes");
      e.popup.close();
    });
  }

  const verifyNo = el.querySelector(".parcel-verify-no");
  if (verifyNo) {
    verifyNo.addEventListener("click", (ev) => {
      ev.preventDefault();
      const { account, lat, lng } = verifyNo.dataset;
      setVerification(account, "no", parseFloat(lat), parseFloat(lng));
      persistSingleTag(account, "verified_vacant", "no");
      e.popup.close();
    });
  }

  const verifyClear = el.querySelector(".parcel-verify-clear");
  if (verifyClear) {
    verifyClear.addEventListener("click", (ev) => {
      ev.preventDefault();
      const { account } = verifyClear.dataset;
      setVerification(account, null);
      persistSingleTag(account, "verified_vacant", null);
      e.popup.close();
    });
  }

  // Target buttons
  const targetOn = el.querySelector(".parcel-target-on");
  if (targetOn) {
    targetOn.addEventListener("click", (ev) => {
      ev.preventDefault();
      const { account, lat, lng } = targetOn.dataset;
      setTarget(account, true, parseFloat(lat), parseFloat(lng));
      persistSingleTag(account, "potential_target", "yes");
      e.popup.close();
    });
  }

  const targetOff = el.querySelector(".parcel-target-off");
  if (targetOff) {
    targetOff.addEventListener("click", (ev) => {
      ev.preventDefault();
      const { account } = targetOff.dataset;
      setTarget(account, false);
      persistSingleTag(account, "potential_target", null);
      e.popup.close();
    });
  }
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
const _isAdmin        = () => ["owner", "developer"].includes(_currentRole());
const _canDownloadCsv = () => _currentRole() !== "user";
const _canEditAnyArea = () => _currentRole() === "developer";

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
        <label for="auth-email">Email</label>
        <input type="email" id="auth-email" name="email" autocomplete="off" autocapitalize="off" spellcheck="false" required placeholder="you@example.com">
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
    const email = document.getElementById("auth-email").value.trim();
    const password = document.getElementById("auth-password").value;
    _setAuthError("auth-login-error", "");
    _setAuthBusy("auth-login-btn", true, "Sign In");

    try {
      const resp = await fetch("/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ email, password }),
        credentials: "same-origin",
      });
      const data = await resp.json().catch(() => ({}));
      if (resp.ok) {
        _hideAuthModal();
        _currentUser = data.user || data;
        _renderUserBar(_currentUser);
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

  // Autofocus email field
  requestAnimationFrame(() => document.getElementById("auth-email")?.focus());
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

  ["auth-chpw-current", "auth-chpw-new", "auth-chpw-confirm"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.value = "";
  });

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
          _showLoginForm("Password updated. Sign in with your new password.");
          return;
        }
        _currentUser = data.user || _currentUser;
        if (_currentUser) _currentUser.force_password_change = false;
        _renderUserBar(_currentUser);
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
      <input type="email" id="admin-new-email" placeholder="Email" autocomplete="off">
      <input type="text"  id="admin-new-username" placeholder="Username" autocomplete="off">
      <input type="password" id="admin-new-temp-password" placeholder="Temp Password (10+ chars)" autocomplete="new-password">
      <select id="admin-new-role">
        <option value="user">User</option>
        <option value="power_user">Power User</option>
        ${_currentRole() === "developer" ? `<option value="owner">Owner</option>` : ""}
      </select>
      <button id="admin-add-btn" class="admin-add-btn">+ Add User</button>
      <p id="admin-add-msg" class="admin-msg"></p>
    </div>`;
  _showAuthModal();

  document.getElementById("auth-close-admin").addEventListener("click", _hideAuthModal);
  document.getElementById("admin-add-btn").addEventListener("click", _adminAddUser);

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
        <td>${_esc(u.email)}</td>
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
  const email    = document.getElementById("admin-new-email")?.value.trim();
  const username = document.getElementById("admin-new-username")?.value.trim();
  const tempPassword = document.getElementById("admin-new-temp-password")?.value;
  const role     = document.getElementById("admin-new-role")?.value;
  const msgEl    = document.getElementById("admin-add-msg");

  if (!email || !username || !tempPassword) {
    if (msgEl) { msgEl.textContent = "Email, username, and temporary password are required."; msgEl.className = "admin-msg err"; }
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
    const resp = await fetch("/admin/users", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ email, username, temp_password: tempPassword, role }),
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
  _showLoginForm();
}

// ---------------------------------------------------------------------------
// Handle 401 / FORCE_PASSWORD_CHANGE from any API response
// Call this from any catch block where you receive a 401 resp.
// ---------------------------------------------------------------------------
function _handle401() {
  _currentUser = null;
  _renderUserBar(null);
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
    await restoreSavedArea(_normalizeSavedAreaRow(area));
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