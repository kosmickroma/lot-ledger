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

const COLORS = {
  single_family: "#2980b9",
  off_market: "#2980b9",
  vacant: "#27ae60",
  multifamily: "#8e44ad",
  commercial: "#e67e22",
  exempt: "#95a5a6",
  active: "#D92228",
};

const BORDER_COLORS = {
  single_family: "#1a6a9a",
  off_market: "#1a6a9a",
  vacant: "#1e8449",
  multifamily: "#6c3483",
  commercial: "#d35400",
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
  off_market: "Off Market",
};

const SOLD_MARKER_COLOR = "#c9a24f";
const SOLD_MARKER_BORDER = "#8e6f2c";
const SOLD_OUTLINE_COLOR = "#FFD700";
const SOLD_FALLBACK_DOT_COLOR = "#4B0082";
const SOLD_FALLBACK_DOT_BORDER = "#312e81";
const FILTER_STORAGE_KEY = "lotledger.map.filters.v1";

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

const PARCEL_LAYER_KEYS = ["active", "off_market", "vacant", "multifamily", "commercial", "exempt"];

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
let soldMarkers = [];
let transientSoldSidebarPopup = null;
let soldCompsSortMode = "price";
let soldCompsCollapsed = true;
let filterState = { ...DEFAULT_FILTERS };
const verificationByAccount = new Map();
const potentialTargetByAccount = new Map();
const verificationBadgeMarkers = new Map();
const targetBadgeMarkers = new Map();
let activeBrush = null;
let allAnalysisFeatures = null;   // full feature set from last analysis
let viewportRenderMode = false;   // true when feature count exceeds render threshold
let _vpRenderTimeout = null;      // debounce handle for viewport re-render
const LARGE_DRAW_THRESHOLD = 500;  // viewport-only rendering above this count
const BROWSE_ONLY_THRESHOLD = 30000; // skip all polygon rendering above this; use browse layer
let _analysisRequestSeq = 0;
let _activeAnalysisRequestId = 0;
let _activeAnalysisAbortController = null;

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
  return Boolean(filterState[bucket]);
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

function renderSoldCompsPanel() {
  const panel = document.getElementById("sold-comps-panel");
  if (!panel) return;

  if (!Array.isArray(lastSoldPanelPoints) || lastSoldPanelPoints.length === 0) {
    panel.innerHTML = "";
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
        <div class="sold-row" data-sold-idx="${idx}">
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

  panel.innerHTML = `
    <div class="sold-comps-panel">
      <button class="section-toggle" type="button" id="sold-comps-toggle" aria-expanded="${!soldCompsCollapsed}">
        <span class="sidebar-label">Sold Comps</span>
      </button>
      <div id="sold-comps-body" class="collapsible-body${soldCompsCollapsed ? " hidden" : ""}">
        <div class="sold-comps-summary">
          <span class="sold-chip">${lastSoldPanelPoints.length} comps</span>
          <span class="sold-chip">Median ${medianPrice != null ? abbreviatePrice(medianPrice) : "N/A"}</span>
          <span class="sold-chip">Median ${medianPpsf != null ? `$${Math.round(medianPpsf)}/sqft` : "N/A"}</span>
        </div>
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

  panel.querySelectorAll(".sold-sort-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const next = btn.getAttribute("data-sort") === "ppsf" ? "ppsf" : "price";
      if (next === soldCompsSortMode) return;
      soldCompsSortMode = next;
      renderSoldCompsPanel();
    });
  });

  panel.querySelectorAll(".sold-row[data-sold-idx]").forEach((rowEl) => {
    rowEl.addEventListener("click", () => {
      const idx = Number(rowEl.getAttribute("data-sold-idx"));
      const point = sortedForClick[idx];
      if (point) zoomToSoldComp(point);
    });
  });
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

function _loadSavedAreas() {
  try { return JSON.parse(localStorage.getItem("lot_ledger_saved_areas") || "[]"); }
  catch { return []; }
}

function _writeSavedAreas(areas) {
  localStorage.setItem("lot_ledger_saved_areas", JSON.stringify(areas));
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

function saveCurrentArea(name) {
  if (!lastDrawnLatLngs) return;
  const areas = _loadSavedAreas();
  areas.unshift({
    id: Date.now().toString(),
    name: name.trim(),
    latlngs: lastDrawnLatLngs,
    bounds: _savedAreaBoundsFromLatLngs(lastDrawnLatLngs),
    savedAt: new Date().toISOString(),
  });
  _writeSavedAreas(areas);
  renderSavedAreasList();
}

function deleteSavedArea(id) {
  const area = _loadSavedAreas().find(a => a.id === id);
  if (area?.type === "parcel" && area.account_num) {
    const layer = savedParcelLayers[area.account_num];
    if (layer) { savedParcelLayer.removeLayer(layer); delete savedParcelLayers[area.account_num]; }
  }
  _writeSavedAreas(_loadSavedAreas().filter(a => a.id !== id));
  renderSavedAreasList();
}

function saveSearchLocation(name, lat, lng) {
  const areas = _loadSavedAreas();
  areas.unshift({
    id: Date.now().toString(),
    type: "location",
    name,
    lat,
    lng,
    savedAt: new Date().toISOString(),
  });
  _writeSavedAreas(areas);
  renderSavedAreasList();
}

const SAVED_PARCEL_COLOR = "#4fc3f7";

function _renderSavedParcelOutline(area) {
  if (savedParcelLayers[area.account_num]) return; // already on map
  if (!area.geometry || !["Polygon", "MultiPolygon"].includes(area.geometry.type)) return;
  const layer = L.geoJSON({ type: "Feature", geometry: area.geometry, properties: {} }, {
    style: { color: SAVED_PARCEL_COLOR, weight: 3, fill: false, interactive: false },
    interactive: false,
  }).addTo(savedParcelLayer);
  savedParcelLayers[area.account_num] = layer;
}

function saveParcel(account_num, county, addr, lat, lng, geometry) {
  const areas = _loadSavedAreas();
  if (areas.some(a => a.type === "parcel" && a.account_num === account_num)) return; // no dupe
  areas.unshift({
    id: Date.now().toString(),
    type: "parcel",
    account_num,
    county,
    name: addr || account_num,
    lat,
    lng,
    geometry: geometry || null,
    savedAt: new Date().toISOString(),
  });
  _writeSavedAreas(areas);
  renderSavedAreasList();
  _renderSavedParcelOutline(areas[0]);
}

function _restoreAllSavedParcelOutlines() {
  _loadSavedAreas()
    .filter(a => a.type === "parcel")
    .forEach(_renderSavedParcelOutline);
}

async function restoreSavedArea(area) {
  // Location pins (from address search) — just fly there and show the ring.
  if (area.type === "location") {
    const latlng = [area.lat, area.lng];
    map.flyTo(latlng, 17);
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
    map.flyTo([area.lat, area.lng], 18);
    _renderSavedParcelOutline(area);
    return;
  }

  clearDrawResults();
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
  map.fitBounds(area.bounds, { padding: [40, 40] });
  document.getElementById("btn-draw-clear")?.classList.remove("hidden");
  document.getElementById("btn-saved-area-clear")?.classList.remove("hidden");

  // Convert saved [lat, lng] pairs → [lng, lat] for the analysis API.
  const polygon = area.latlngs.map(([lat, lng]) => [lng, lat]);
  lastDrawnLatLngs = area.latlngs;
  lastPolygon = polygon;

  if (map.hasLayer(browseLayer)) browseLayer.remove();

  const includeRedfin = false;
  const includeSold = Boolean(filterState.sold);
  document.getElementById("sidebar-instructions")?.classList.add("hidden");
  document.getElementById("sidebar-results")?.classList.add("hidden");
  document.getElementById("sidebar-loading")?.classList.remove("hidden");
  document.getElementById("redfin-status").textContent = "Loading area analysis...";
  const analysisRequest = beginLatestAnalysisRequest();

  try {
    const data = await runAnalysis(polygon, includeRedfin, includeSold, { signal: analysisRequest.signal });
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
    lastSoldPanelPoints = [...lastSoldPoints];
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
    renderSoldPoints(lastSoldPoints);

    const soldStatus = document.getElementById("sold-toggle-status");
    if (soldStatus) {
      soldStatus.textContent = filterState.sold
        ? `${lastSoldPanelPoints.length} sold comp${lastSoldPanelPoints.length !== 1 ? "s" : ""} found`
        : "";
    }

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
  } catch (err) {
    if (isAbortError(err) || !isActiveAnalysisRequest(analysisRequest.requestId)) return;
    console.error("[restoreSavedArea] Analysis failed:", err);
    document.getElementById("redfin-status").textContent = getAnalysisErrorMessage(err, "Area analysis failed. Please try again.");
    document.getElementById("sidebar-loading")?.classList.add("hidden");
    document.getElementById("sidebar-instructions")?.classList.remove("hidden");
  }
}

function _renderList(sectionId, listId, items) {
  const section = document.getElementById(sectionId);
  const list = document.getElementById(listId);
  if (!section || !list) return;
  section.classList.toggle("hidden", items.length === 0);
  list.innerHTML = items.map(area => {
    const date = new Date(area.savedAt).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    const icon = area.type === "parcel" ? "📌" : area.type === "location" ? "📍" : "▭";
    return `
      <div class="saved-area-row" data-id="${area.id}">
        <span class="saved-area-icon">${icon}</span>
        <span class="saved-area-name">${area.name}</span>
        <span class="saved-area-date">${date}</span>
        <button class="saved-area-delete" data-id="${area.id}" title="Delete">×</button>
      </div>`;
  }).join("");
  list.querySelectorAll(".saved-area-row").forEach(row => {
    row.addEventListener("click", (e) => {
      if (e.target.classList.contains("saved-area-delete")) return;
      const area = _loadSavedAreas().find(a => a.id === row.dataset.id);
      if (area) restoreSavedArea(area);
    });
  });
  list.querySelectorAll(".saved-area-delete").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      deleteSavedArea(btn.dataset.id);
    });
  });
}

function renderSavedAreasList() {
  const all = _loadSavedAreas();
  _renderList("saved-areas",   "saved-areas-list",   all.filter(a => !a.type || a.type === "area"));
  _renderList("saved-parcels", "saved-parcels-list", all.filter(a => a.type === "parcel" || a.type === "location"));
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

    const verifyBtn = L.DomUtil.create("a", "", container);
    verifyBtn.id = "btn-verify-toggle";
    verifyBtn.href = "#";
    verifyBtn.title = "Verification tools: Vacant, Not Vacant, Remove";
    verifyBtn.textContent = "✓";
    L.DomEvent.on(verifyBtn, "click", (e) => {
      L.DomEvent.preventDefault(e);
      toggleVerifyMenu();
    });

    const targetBtn = L.DomUtil.create("a", "", container);
    targetBtn.id = "btn-target-toggle";
    targetBtn.href = "#";
    targetBtn.title = "Target tools: Interested, Unselect";
    targetBtn.textContent = "★";
    L.DomEvent.on(targetBtn, "click", (e) => {
      L.DomEvent.preventDefault(e);
      toggleTargetMenu();
    });

    return container;
  },
});
new MapToolbar().addTo(map);

const VerifyBrushMenu = L.Control.extend({
  options: { position: "topleft" },
  onAdd() {
    const container = L.DomUtil.create("div", "leaflet-bar verify-brush-menu hidden");
    container.id = "verify-brush-menu";
    L.DomEvent.disableClickPropagation(container);

    const vacant = L.DomUtil.create("button", "verify-brush-option", container);
    vacant.type = "button";
    vacant.dataset.brush = "verify_yes";
    vacant.textContent = "✓ Vacant";
    vacant.title = "Mark parcel as verified vacant";
    L.DomEvent.on(vacant, "click", () => selectBrush("verify_yes"));

    const notVacant = L.DomUtil.create("button", "verify-brush-option", container);
    notVacant.type = "button";
    notVacant.dataset.brush = "verify_no";
    notVacant.textContent = "✗ Not Vacant";
    notVacant.title = "Mark parcel as verified not vacant";
    L.DomEvent.on(notVacant, "click", () => selectBrush("verify_no"));

    const clearVerify = L.DomUtil.create("button", "verify-brush-option", container);
    clearVerify.type = "button";
    clearVerify.dataset.brush = "verify_clear";
    clearVerify.textContent = "○ Remove Verify";
    clearVerify.title = "Remove vacant/not-vacant verification";
    L.DomEvent.on(clearVerify, "click", () => selectBrush("verify_clear"));

    return container;
  },
});
new VerifyBrushMenu().addTo(map);

const TargetBrushMenu = L.Control.extend({
  options: { position: "topleft" },
  onAdd() {
    const container = L.DomUtil.create("div", "leaflet-bar target-brush-menu hidden");
    container.id = "target-brush-menu";
    L.DomEvent.disableClickPropagation(container);

    const interested = L.DomUtil.create("button", "verify-brush-option", container);
    interested.type = "button";
    interested.dataset.brush = "target_on";
    interested.textContent = "★ Interested";
    interested.title = "Mark parcel as potential target";
    L.DomEvent.on(interested, "click", () => selectBrush("target_on"));

    const clearTarget = L.DomUtil.create("button", "verify-brush-option", container);
    clearTarget.type = "button";
    clearTarget.dataset.brush = "target_off";
    clearTarget.textContent = "☆ Unselect";
    clearTarget.title = "Remove potential target mark";
    L.DomEvent.on(clearTarget, "click", () => selectBrush("target_off"));

    return container;
  },
});
new TargetBrushMenu().addTo(map);

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

    const clearSuggestList = () => {
      suggestItems = [];
      activeSuggestIndex = -1;
      suggestList.innerHTML = "";
      suggestList.classList.add("hidden");
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

      if (suggestAbort) suggestAbort.abort();
      suggestAbort = new AbortController();

      try {
        const resp = await fetch(`/api/address/suggest?q=${encodeURIComponent(q)}&limit=8`, {
          signal: suggestAbort.signal,
        });
        if (!resp.ok) {
          clearSuggestList();
          return;
        }
        const data = await resp.json();
        suggestItems = Array.isArray(data.items) ? data.items : [];
        activeSuggestIndex = suggestItems.length ? 0 : -1;
        renderSuggestList();
      } catch (e) {
        if (e?.name !== "AbortError") {
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
const brushStatus = document.getElementById("brush-status");

function initSidebarCollapsibles() {
  document.querySelectorAll(".section-toggle[data-target]").forEach((btn) => {
    const targetId = btn.dataset.target;
    const body = targetId ? document.getElementById(targetId) : null;
    if (!body) return;
    btn.addEventListener("click", () => {
      const expanded = btn.getAttribute("aria-expanded") !== "false";
      btn.setAttribute("aria-expanded", String(!expanded));
      body.classList.toggle("hidden", expanded);
    });
  });
}

const BRUSH_LABELS = {
  verify_yes: "Verified Vacant",
  verify_no: "Verified Not Vacant",
  verify_clear: "Remove Verification",
  target_on: "Target: Interested",
  target_off: "Target: Unselect",
};

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

function updateBrushStatus(brush) {
  if (!brushStatus) return;
  if (!brush) {
    brushStatus.classList.add("hidden");
    brushStatus.classList.remove("verify", "target");
    brushStatus.textContent = "Active Tool: None";
    return;
  }

  const label = BRUSH_LABELS[brush] || brush;
  brushStatus.classList.remove("hidden");
  brushStatus.classList.toggle("verify", brush.startsWith("verify_"));
  brushStatus.classList.toggle("target", brush.startsWith("target_"));
  brushStatus.textContent = `Active Tool: ${label}`;
}

sidebarToggleBtn.addEventListener("click", () => {
  const collapsed = appShell.classList.contains("sidebar-collapsed");
  setSidebarCollapsed(!collapsed);
});

initSidebarCollapsibles();

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

  renderSoldPoints(lastSoldPoints);

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

  const soldStatus = document.getElementById("sold-toggle-status");
  if (soldStatus) {
    soldStatus.textContent = filterState.sold
      ? `${lastSoldPanelPoints.length} sold comp${lastSoldPanelPoints.length !== 1 ? "s" : ""} found`
      : "Sold comps hidden";
  }
}

loadFilters();
syncFilterInputs();
Object.entries(FILTER_INPUT_IDS).forEach(([key, id]) => {
  const input = document.getElementById(id);
  if (!input) return;
  input.addEventListener("change", () => {
    filterState[key] = Boolean(input.checked);
    saveFilters();
    applyMapVisibilityFilters();
  });
});

document.getElementById("btn-filters-reset")?.addEventListener("click", () => {
  filterState = { ...DEFAULT_FILTERS };
  saveFilters();
  syncFilterInputs();
  applyMapVisibilityFilters();
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

function toggleVerifyMenu() {
  const menu = document.getElementById("verify-brush-menu");
  if (!menu) return;
  document.getElementById("target-brush-menu")?.classList.add("hidden");
  menu.classList.toggle("hidden");
}

function toggleTargetMenu() {
  const menu = document.getElementById("target-brush-menu");
  if (!menu) return;
  document.getElementById("verify-brush-menu")?.classList.add("hidden");
  menu.classList.toggle("hidden");
}

function selectBrush(brush) {
  activeBrush = brush || null;
  const verifyActive = activeBrush && activeBrush.startsWith("verify_");
  const targetActive = activeBrush && activeBrush.startsWith("target_");
  document.getElementById("btn-verify-toggle")?.classList.toggle("active", Boolean(verifyActive));
  document.getElementById("btn-target-toggle")?.classList.toggle("active", Boolean(targetActive));
  document.getElementById("btn-target-toggle")?.classList.toggle("target-active", Boolean(targetActive));
  map.getContainer().classList.toggle("brush-active", activeBrush !== null);
  updateBrushStatus(activeBrush);
  const verifyMenu = document.getElementById("verify-brush-menu");
  if (verifyMenu) {
    verifyMenu.querySelectorAll(".verify-brush-option").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.brush === activeBrush);
    });
    verifyMenu.classList.add("hidden");
  }
  const targetMenu = document.getElementById("target-brush-menu");
  if (targetMenu) {
    targetMenu.querySelectorAll(".verify-brush-option").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.brush === activeBrush);
    });
    targetMenu.classList.add("hidden");
  }
}

function makePopupHtml(p) {
  const pseudoFeature = { properties: p };
  const statusColor = getColor(pseudoFeature);
  const statusText = getStatusLabel(pseudoFeature);
  const verifiedVacant = normalizeVerificationValue(
    verificationByAccount.get(p.account_num) || p.verified_vacant
  );
  const potentialTarget = String(potentialTargetByAccount.get(p.account_num) || p.potential_target || "").trim();
  const row = (label, val) => `<tr><td class="popup-label">${label}</td><td class="popup-val">${val || "N/A"}</td></tr>`;

  // Redfin price row + delta (active parcels only)
  let redfinPriceRow = "";
  if (p.on_redfin && p.redfin_price) {
    const urlWrap = p.redfin_url
      ? `<a href="${p.redfin_url}" target="_blank" rel="noopener noreferrer">${p.redfin_price}</a>`
      : p.redfin_price;
    redfinPriceRow = row("Redfin List Price", urlWrap);

    // Numeric delta: parse both values
    const rfNum = parseInt(String(p.redfin_price).replace(/[^0-9]/g, ""), 10);
    const dcadRaw = String(p.tot_val || "").replace(/[^0-9]/g, "");
    const dcadNum = dcadRaw ? parseInt(dcadRaw, 10) : NaN;
    if (!isNaN(rfNum) && !isNaN(dcadNum) && dcadNum > 0) {
      const delta = rfNum - dcadNum;
      const pct = ((delta / dcadNum) * 100).toFixed(1);
      const sign = delta >= 0 ? "+" : "";
      const color = delta >= 0 ? "#27ae60" : "#e74c3c";
      redfinPriceRow += `<tr><td class="popup-label">vs DCAD Value</td><td class="popup-val" style="color:${color}">${sign}$${Math.abs(delta).toLocaleString()} (${sign}${pct}%)</td></tr>`;
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
        <div class="popup-status" style="color:${statusColor};">${statusText}</div>
        <table class="popup-table">
          ${row("Owner", p.owner)}
          ${row("Land Value", p.land_val)}
          ${row("Total Value", p.tot_val)}
          ${redfinPriceRow}
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
          ${soldCompRows}
        </table>
        ${p.account_num ? `<div style="margin-top:8px;border-top:1px solid #e2e8f0;padding-top:6px;display:flex;gap:12px;align-items:center;">
          <a href="#" class="parcel-save-link"
            data-account="${p.account_num}"
            data-county="${p.source_county || "dcad"}"
            data-addr="${(p.addr || "").replace(/"/g, "&quot;")}"
            data-lat="${p.lat || ""}"
            data-lng="${p.lng || ""}"
            style="color:#4fc3f7;text-decoration:none;font-size:11px;">📌 Save parcel</a>
          <a href="#" class="parcel-clear-link" style="color:#aaa;text-decoration:none;font-size:11px;">✕ Clear</a>
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
        <div class="popup-status" style="color:${SOLD_MARKER_COLOR};">SOLD COMP</div>
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
      if (!candidate.feature.properties.sold_comp) {
        candidate.feature.properties.sold_comp = {
          sold_price: point.sold_price,
          sold_date: point.sold_date,
          dom: point.dom,
          lot_sqft: point.lot_sqft,
          listing_url: point.listing_url,
        };
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
      lat,
      lng,
      sold_price: p.sold_comp.sold_price,
      sold_date: p.sold_comp.sold_date,
    });
  }

  return { unmatchedSoldPoints, matchedLabelPoints };
}

function renderSoldPoints(points) {
  soldLayer.clearLayers();
  soldMarkers = [];
  if (!filterState.sold) return;
  const soldPoints = Array.isArray(points) ? points : [];
  soldPoints.forEach((point) => {
    const lat = Number(point.lat);
    const lng = Number(point.lng);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) return;
    const marker = L.circleMarker([lat, lng], {
      pane: "soldPane",
      radius: 4,
      fillColor: SOLD_FALLBACK_DOT_COLOR,
      color: SOLD_FALLBACK_DOT_BORDER,
      weight: 1.2,
      opacity: 1,
      fillOpacity: 0.85,
      bubblingMouseEvents: false,
    }).bindPopup(() => makeSoldPopupHtml(point), { maxWidth: 300 }).addTo(soldLayer);
    soldMarkers.push({
      marker,
      priceLabel: abbreviatePrice(point.sold_price),
      soldDateLabel: formatSoldDateLabel(point.sold_date),
    });
  });

  // Matched sold comps are represented by gold parcel outlines. Create
  // invisible anchor markers so zoomed sold price labels still render.
  matchedSoldLabelPoints.forEach((point) => {
    const marker = L.circleMarker([point.lat, point.lng], {
      pane: "soldPane",
      radius: 0,
      stroke: false,
      fill: false,
      opacity: 0,
      fillOpacity: 0,
      interactive: false,
      bubblingMouseEvents: false,
    }).addTo(soldLayer);
    soldMarkers.push({
      marker,
      priceLabel: abbreviatePrice(point.sold_price),
      soldDateLabel: formatSoldDateLabel(point.sold_date),
    });
  });

  refreshSoldPriceLabels();
}

function renderFeatures(geojson) {
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
    const targetLayer = parcelTypeLayers[bucket] || markerLayer;
    const color = getColor(feature);
    const borderColor = getBorderColor(feature);
    const hasVisibleSoldComp = Boolean(p.sold_comp) && soldLayerVisible;
    const parcelBorderColor = hasVisibleSoldComp ? SOLD_OUTLINE_COLOR : borderColor;
    const parcelBorderWeight = hasVisibleSoldComp ? (p.on_redfin ? 2.8 : 2.4) : (p.on_redfin ? 2.2 : 1.5);
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

    const applyBrush = () => {
      if (!activeBrush || !p.account_num) return;
      if (activeBrush === "verify_yes") {
        verificationByAccount.set(p.account_num, "Yes");
        p.verified_vacant = "Yes";
        renderVerificationBadge(p.account_num, p.lat, p.lng, "Yes");
      } else if (activeBrush === "verify_no") {
        verificationByAccount.set(p.account_num, "No");
        p.verified_vacant = "No";
        renderVerificationBadge(p.account_num, p.lat, p.lng, "No");
      } else if (activeBrush === "verify_clear") {
        verificationByAccount.set(p.account_num, "");
        p.verified_vacant = "";
        clearVerificationBadge(p.account_num);
      } else if (activeBrush === "target_on") {
        potentialTargetByAccount.set(p.account_num, "Yes");
        p.potential_target = "Yes";
        renderTargetBadge(p.account_num, p.lat, p.lng);
      } else if (activeBrush === "target_off") {
        potentialTargetByAccount.set(p.account_num, "");
        p.potential_target = "";
        clearTargetBadge(p.account_num);
      }
    };

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
    if (renderCondoOutline) {
      // Non-interactive visual outline for condo building footprints.
      L.geoJSON(feature, {
        renderer: MAP_CANVAS_RENDERER,
        interactive: false,
        style: {
          color: parcelBorderColor,
          fill: false,
          weight: hasVisibleSoldComp ? 2.2 : 1.2,
          opacity: 0.75,
        },
      })
        .addTo(targetLayer);
    }

    if (renderPolygon) {
      layer = L.geoJSON(feature, {
        renderer: MAP_SVG_RENDERER,
        bubblingMouseEvents: false,
        style: {
          color: parcelBorderColor,
          fillColor: color,
          fillOpacity: 0.12,
          weight: parcelBorderWeight,
          opacity: 0.85,
        },
      }).bindPopup(() => makePopupHtml(p), { maxWidth: 280, autoPan: false });
      layer.on("click", applyBrush);
      layer.addTo(targetLayer);
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
        fillColor: color,
        color: parcelBorderColor,
        weight: 1.5,
        opacity: 1,
        fillOpacity: 0.9,
        bubblingMouseEvents: false,
      }).bindPopup(() => makePopupHtml(p), { maxWidth: 280, autoPan: false });
      layer.on("click", applyBrush);
      layer.addTo(circleLayer);
    }

    markers[p.addr] = { layer, feature };
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
  const marker = L.marker([lat, lng], { icon: badgeIcon }).addTo(verificationBadgeLayer);
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
  const marker = L.marker([lat, lng], { icon: badgeIcon }).addTo(targetBadgeLayer);
  targetBadgeMarkers.set(accountNum, marker);
}

function clearTargetBadge(accountNum) {
  const marker = targetBadgeMarkers.get(accountNum);
  if (!marker) return;
  targetBadgeLayer.removeLayer(marker);
  targetBadgeMarkers.delete(accountNum);
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
  _vpRenderTimeout = setTimeout(renderViewportFeatures, 150);
}

function renderSidebar(counts, markers) {
  document.getElementById("sidebar-loading").classList.add("hidden");
  document.getElementById("sidebar-results").classList.remove("hidden");

  const countsPanel = document.getElementById("counts-panel");
  countsPanel.innerHTML = Object.entries({
    active: counts.active,
    off_market: counts.off_market,
    vacant: counts.vacant,
    multifamily: counts.multifamily,
    commercial: counts.commercial,
    exempt: counts.exempt,
  })
    .filter(([, v]) => v > 0)
    .map(
      ([key, val]) => `
      <div class="count-row">
        <span class="count-dot" style="background:${COLORS[key] || COLORS.exempt}"></span>
        <span class="count-label">${TYPE_LABELS[key] || key}</span>
        <span class="count-val">${val}</span>
      </div>`
    )
    .join("");

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
        headers: { "Content-Type": "application/json" },
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
  const { signal } = options;
  const redfinStatus = document.getElementById("redfin-status");
  let data;
  try {
    data = await postJsonWithRetry(
      "/api/analyze",
      { polygon: tilePolygon, include_redfin: includeRedfin, include_sold: includeSold },
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
    { job_ids: tileJobIds },
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
  const bbox = getPolygonBbox(polygon);
  if (bboxArea(bbox) > TILE_AREA_THRESHOLD) {
    return runTiledAnalysis(polygon, includeRedfin, includeSold, options);
  }
  return postJsonWithRetry(
    "/api/analyze",
    { polygon, include_redfin: includeRedfin, include_sold: includeSold },
    {
      signal: options.signal,
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
  const includeRedfin = false;
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

async function persistTagStateForExport() {
  if (!currentJobId) return false;

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
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ verifications: payload, potential_targets: targetPayload }),
    });
  } catch {
    return false;
  }

  if (resp.ok) return true;
  if (resp.status !== 404) return false;

  const downloadBtn = document.getElementById("btn-download");
  const previousLabel = downloadBtn?.textContent || "Download CSV";
  if (downloadBtn) {
    downloadBtn.disabled = true;
    downloadBtn.textContent = "Re-fetching results, please wait…";
  }

  try {
    const refreshed = await refreshExpiredJob();
    if (!refreshed) return false;

    try {
      const retry = await fetch(`/api/job/${currentJobId}/verification`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ verifications: payload, potential_targets: targetPayload }),
      });
      return retry.ok;
    } catch {
      return false;
    }
  } finally {
    if (downloadBtn) {
      downloadBtn.disabled = false;
      downloadBtn.textContent = previousLabel;
    }
  }
}

map.on("draw:created", async (e) => {
  closeTransientSoldSidebarPopup();
  map.getContainer().classList.remove("drawing-active");
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
  activeBrush = null;
  document.getElementById("btn-verify-toggle")?.classList.remove("active");
  document.getElementById("btn-target-toggle")?.classList.remove("active", "target-active");
  map.getContainer().classList.remove("brush-active");
  updateBrushStatus(null);
  document.getElementById("verify-brush-menu")?.classList.add("hidden");
  document.getElementById("target-brush-menu")?.classList.add("hidden");
  document.getElementById("sidebar-instructions").classList.add("hidden");
  document.getElementById("sidebar-results").classList.add("hidden");
  document.getElementById("sidebar-loading").classList.remove("hidden");
  const includeRedfin = false;
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
    redfinLayerVisible = false;
    soldLayerVisible = Boolean(filterState.sold);
    map.removeLayer(redfinLayer);
    if (soldLayerVisible) {
      soldLayer.addTo(map);
    } else {
      map.removeLayer(soldLayer);
    }
    lastIncludedRedfin = false;
    lastIncludedSold = includeSold;
    lastSoldPoints = Array.isArray(data.sold_points) ? data.sold_points : [];
    lastSoldPanelPoints = [...lastSoldPoints];
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
    renderSoldPoints(lastSoldPoints);
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
    const soldStatus = document.getElementById("sold-toggle-status");
    if (soldStatus) {
      soldStatus.textContent = filterState.sold
        ? `${lastSoldPanelPoints.length} sold comp${lastSoldPanelPoints.length !== 1 ? "s" : ""} found`
        : "";
    }
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
  lastAnalysisGeojson = null;
  lastAnalysisCounts = null;
  lastIncludedRedfin = false;
  lastIncludedSold = false;
  lastSoldPoints = [];
  lastSoldPanelPoints = [];
  matchedSoldLabelPoints = [];
  const soldCompsPanel = document.getElementById("sold-comps-panel");
  if (soldCompsPanel) soldCompsPanel.innerHTML = "";
  document.getElementById("redfin-toggle-status").textContent = "";
  document.getElementById("sold-toggle-status").textContent = "";
  document.getElementById("sidebar-results")?.classList.add("hidden");
  document.getElementById("sidebar-instructions")?.classList.remove("hidden");
  document.getElementById("sidebar-loading")?.classList.add("hidden");
  document.getElementById("btn-draw-clear")?.classList.add("hidden");
  document.getElementById("btn-saved-area-clear")?.classList.add("hidden");
}

map.on("draw:drawstart", () => {
  drawHelper.classList.remove("hidden");
  document.getElementById("btn-draw")?.classList.add("active");
  document.getElementById("btn-draw-cancel")?.classList.remove("hidden");
  document.getElementById("btn-draw-clear")?.classList.add("hidden");
  // CSS pointer-events:none (drawing-active class) blocks parcel layer clicks
  // so vertices never get swallowed by underlying markers.
  map.getContainer().classList.add("drawing-active");
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

document.getElementById("btn-download").addEventListener("click", () => {
  (async () => {
    if (!currentJobId) return;

    const persisted = await persistTagStateForExport();
    if (!persisted) {
      alert("Your analysis session expired. Please re-run the draw/analyze step, then export again.");
      return;
    }

    const suggested = makeDefaultCsvName();
    const entered = window.prompt("Name this CSV export:", suggested);
    if (entered === null) return;
    const filename = normalizeCsvFilename(entered);
    window.location.href = `/api/download/${currentJobId}?filename=${encodeURIComponent(filename)}`;
  })();
});

document.getElementById("btn-save-area")?.addEventListener("click", () => {
  const name = window.prompt("Name this area:", "");
  if (!name || !name.trim()) return;
  saveCurrentArea(name);
});

document.getElementById("btn-clear").addEventListener("click", () => {
  clearDrawResults();
  activeBrush = null;
  document.getElementById("btn-verify-toggle")?.classList.remove("active");
  document.getElementById("btn-target-toggle")?.classList.remove("active", "target-active");
  map.getContainer().classList.remove("brush-active");
  updateBrushStatus(null);
  document.getElementById("verify-brush-menu")?.classList.add("hidden");
  document.getElementById("target-brush-menu")?.classList.add("hidden");
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
    lastSoldPanelPoints = [...lastSoldPoints];
    lastAnalysisGeojson = data;
    lastAnalysisCounts = data.counts;
    redfinLayerVisible = true;
    redfinLayer.addTo(map);
    soldLayerVisible = includeSold;
    if (soldLayerVisible) soldLayer.addTo(map); else map.removeLayer(soldLayer);
    renderSoldPoints(lastSoldPoints);
    const markers = renderFeatures(data);
    renderSidebar(data.counts, markers);
    if (statusEl) {
      if (!data.redfin_ok) {
        statusEl.textContent = "Redfin unavailable";
      } else if (data.counts.active === 0) {
        statusEl.textContent = "No active listings found";
      } else {
        statusEl.textContent = `${data.counts.active} active listing${data.counts.active !== 1 ? "s" : ""} found`;
      }
    }
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
    lastSoldPanelPoints = [...lastSoldPoints];
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
    renderSoldPoints(lastSoldPoints);
    if (lastAnalysisGeojson && Array.isArray(lastAnalysisGeojson.features) && lastAnalysisGeojson.features.length <= BROWSE_ONLY_THRESHOLD) {
      const markers = renderFeatures(lastAnalysisGeojson);
      if (lastAnalysisCounts) renderSidebar(lastAnalysisCounts, markers);
    }

    if (statusEl) {
      statusEl.textContent = `${lastSoldPanelPoints.length} sold comp${lastSoldPanelPoints.length !== 1 ? "s" : ""} found`;
    }
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
      saveParcel(account, county, addr, parseFloat(lat), parseFloat(lng), geometry);
      saveLink.textContent = "✓ Saved";
      saveLink.style.color = "#888";
      saveLink.style.pointerEvents = "none";
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
});

renderSavedAreasList();
_restoreAllSavedParcelOutlines();