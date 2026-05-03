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
  active: "#e74c3c",
};

const BORDER_COLORS = {
  single_family: "#1a6a9a",
  off_market: "#1a6a9a",
  vacant: "#1e8449",
  multifamily: "#6c3483",
  commercial: "#d35400",
  exempt: "#7f8c8d",
  active: "#c0392b",
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

// Analysis state is initialized early because zoom-nudge logic references it
// during startup before the rest of the module wiring runs.
let lastAnalysisGeojson = null;

const map = L.map("map", { zoomControl: true }).setView(DALLAS_CENTER, DEFAULT_ZOOM);

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
  paintRules: [..._browseRules("dcad"), ..._browseRules("tad")],
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
map.on("moveend zoomend", () => { if (viewportRenderMode) _scheduleViewportRender(); });
_updateZoomNudge();

let drawLayer = L.layerGroup().addTo(map);
let maskLayer = L.layerGroup().addTo(map);
let markerLayer = L.layerGroup().addTo(map);
let redfinLayer = L.layerGroup().addTo(map);
const redfinToggleInput = document.getElementById("toggle-redfin");
let redfinLayerVisible = Boolean(redfinToggleInput?.checked);
if (!redfinLayerVisible) {
  map.removeLayer(redfinLayer);
}
let verificationBadgeLayer = L.layerGroup().addTo(map);
let targetBadgeLayer = L.layerGroup().addTo(map);
let hoaLayer = null;
let hoaVisible = false;
let currentJobId = null;
let lastPolygon = null;
let lastDrawnLatLngs = null;
let lastAnalysisCounts = null;
let lastIncludedRedfin = false;
const verificationByAccount = new Map();
const potentialTargetByAccount = new Map();
const verificationBadgeMarkers = new Map();
const targetBadgeMarkers = new Map();
let activeBrush = null;
let allAnalysisFeatures = null;   // full feature set from last analysis
let viewportRenderMode = false;   // true when feature count exceeds render threshold
let _vpRenderTimeout = null;      // debounce handle for viewport re-render
const LARGE_DRAW_THRESHOLD = 2500;  // viewport-only rendering above this count
const BROWSE_ONLY_THRESHOLD = 30000; // skip all polygon rendering above this; use browse layer

const HOA_COLOR = "#b8860b";

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

async function restoreSavedArea(area) {
  // Location pins (from address search) — just fly there and show the ring.
  if (area.type === "location") {
    const latlng = [area.lat, area.lng];
    map.flyTo(latlng, 17);
    window._clearSearchHighlight?.();
    if (window._searchMoveEndHandler) map.off("moveend", window._searchMoveEndHandler);
    window._clearSearchHighlight = () => {
      if (window._searchHighlight) { window._searchHighlight.remove(); window._searchHighlight = null; }
      if (window._searchPopup) { window._searchPopup.remove(); window._searchPopup = null; }
    };
    window._searchMoveEndHandler = () => {
      window._searchMoveEndHandler = null;
      setTimeout(async () => {
        const [slat, slng] = latlng;
        let highlightLayer = null;
        try {
          const result = browseLayer.queryTileFeaturesDebug(slng, slat);
          const allFeatures = result instanceof Map
            ? [...result.values()].flat()
            : (Array.isArray(result) ? result : []);
          const parcel = allFeatures.find(f => {
            const props = (f.feature && f.feature.props) || f.props || {};
            return props.source_county === "dcad" || props.source_county === "tad";
          });
          if (parcel) {
            const pProps = (parcel.feature && parcel.feature.props) || parcel.props || {};
            const county = pProps.source_county;
            const accountNum = pProps.account_num;
            if (county && accountNum) {
              const resp = await fetch(`/api/parcel/${county}/${accountNum}`);
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
      }, 800);
    };
    map.once("moveend", window._searchMoveEndHandler);
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

  const includeRedfin = Boolean(document.getElementById("toggle-redfin")?.checked);
  document.getElementById("sidebar-instructions")?.classList.add("hidden");
  document.getElementById("sidebar-results")?.classList.add("hidden");
  document.getElementById("sidebar-loading")?.classList.remove("hidden");
  document.getElementById("redfin-status").textContent = "Loading saved area...";

  try {
    const data = await runAnalysis(polygon, includeRedfin);
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
    lastAnalysisGeojson = data;
    lastAnalysisCounts = data.counts;
    allAnalysisFeatures = data.features;
    redfinLayerVisible = includeRedfin;
    if (redfinLayerVisible) redfinLayer.addTo(map); else map.removeLayer(redfinLayer);

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
    console.error("[restoreSavedArea] Analysis failed:", err);
    document.getElementById("sidebar-loading")?.classList.add("hidden");
    document.getElementById("sidebar-instructions")?.classList.remove("hidden");
  }
}

function renderSavedAreasList() {
  const areas = _loadSavedAreas();
  const section = document.getElementById("saved-areas");
  const list = document.getElementById("saved-areas-list");
  if (!section || !list) return;

  section.classList.toggle("hidden", areas.length === 0);

  list.innerHTML = areas.map(area => {
    const date = new Date(area.savedAt).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    const icon = area.type === "location" ? "📍" : "▭";
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

    const input = L.DomUtil.create("input", "address-search-input", container);
    input.type = "text";
    input.placeholder = "Search address or place...";
    input.setAttribute("autocomplete", "off");

    const btn = L.DomUtil.create("button", "address-search-btn", container);
    btn.textContent = "Go";

    const doSearch = async () => {
      const q = input.value.trim();
      if (!q) return;
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
        const { lat, lon, display_name } = results[0];
        const latlng = [parseFloat(lat), parseFloat(lon)];
        const shortName = display_name.split(",")[0].trim();
        map.flyTo(latlng, 17);
        input.value = "";

        // Clear any previous highlight and cancel any pending placement.
        window._clearSearchHighlight?.();
        if (window._searchMoveEndHandler) map.off("moveend", window._searchMoveEndHandler);

        window._clearSearchHighlight = () => {
          if (window._searchHighlight) { window._searchHighlight.remove(); window._searchHighlight = null; }
          if (window._searchPopup) { window._searchPopup.remove(); window._searchPopup = null; }
        };

        // Wait for map to stop flying, then wait 800ms for PMTiles to load tiles
        // at the new viewport before querying the browse layer for the parcel footprint.
        window._searchMoveEndHandler = () => {
          window._searchMoveEndHandler = null;
          setTimeout(async () => {
            const [slat, slng] = latlng;
            let highlightLayer = null;

            // Hit-test the browse layer to find the parcel at this point.
            try {
              const result = browseLayer.queryTileFeaturesDebug(slng, slat);
              const allFeatures = result instanceof Map
                ? [...result.values()].flat()
                : (Array.isArray(result) ? result : []);
              const parcel = allFeatures.find(f => {
                const props = (f.feature && f.feature.props) || f.props || {};
                return props.source_county === "dcad" || props.source_county === "tad";
              });
              if (parcel) {
                const pProps = (parcel.feature && parcel.feature.props) || parcel.props || {};
                const county = pProps.source_county;
                const accountNum = pProps.account_num;
                if (county && accountNum) {
                  const resp = await fetch(`/api/parcel/${county}/${accountNum}`);
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
                }
              }
            } catch (e) {
              console.warn("Search footprint lookup failed", e);
            }

            // Fall back to a ring marker if no polygon was found.
            if (!highlightLayer) {
              highlightLayer = L.circleMarker(latlng, {
                radius: 14, color: "#f1c40f", weight: 3,
                fillColor: "#f1c40f", fillOpacity: 0.12,
                interactive: false,
              }).addTo(map);
            }
            window._searchHighlight = highlightLayer;

            // Separate popup (not bound to marker so it works with geoJSON layers too).
            const popupHtml = `<div style="font-size:12px;min-width:160px;">
              <div style="font-weight:600;margin-bottom:6px;">${shortName}</div>
              <a href="#" id="_search-save" style="color:#c9a24f;text-decoration:none;font-size:11px;margin-right:10px;">+ Save location</a>
              <a href="#" id="_search-clear" style="color:#888;text-decoration:none;font-size:11px;">✕ Clear</a>
            </div>`;
            window._searchPopup = L.popup({ closeButton: false, maxWidth: 240 })
              .setLatLng(latlng)
              .setContent(popupHtml)
              .openOn(map);

            setTimeout(() => {
              document.getElementById("_search-save")?.addEventListener("click", e => {
                e.preventDefault();
                const name = window.prompt("Name this location:", shortName);
                if (!name?.trim()) return;
                saveSearchLocation(name.trim(), slat, slng);
                window._searchPopup?.remove();
                window._searchPopup = null;
              });
              document.getElementById("_search-clear")?.addEventListener("click", e => {
                e.preventDefault();
                window._clearSearchHighlight?.();
              });
            }, 50);
          }, 800);
        };
        map.once("moveend", window._searchMoveEndHandler);
      } catch {
        btn.textContent = "Error";
        setTimeout(() => { btn.textContent = "Go"; btn.disabled = false; }, 2000);
        return;
      }
      btn.textContent = "Go";
      btn.disabled = false;
    };

    L.DomEvent.on(btn, "click", doSearch);
    L.DomEvent.on(input, "keydown", e => { if (e.key === "Enter") doSearch(); });
    return container;
  },
});
new AddressSearch().addTo(map);
const appShell = document.querySelector(".app-shell");
const sidebarToggleBtn = document.getElementById("sidebar-toggle");
const drawHelper = document.getElementById("draw-helper");
const brushStatus = document.getElementById("brush-status");

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

function isRedfinVisualActive(feature) {
  return Boolean(feature?.properties?.on_redfin && redfinLayerVisible);
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
          ${row("Lot Size", p.lot_acres)}
          ${row("Frontage", p.frontage)}
          ${row("Depth", p.depth)}
          ${row("State Code", p.state_code)}
          ${row("Zoning", p.zoning)}
          ${row("School District", p.school)}
          ${row("Year Built", p.yr_built)}
          ${row("Sq Ft", p.sqft && p.sqft !== "N/A" ? p.sqft : "N/A")}
          ${row("Acres", p.lot_acres)}
          ${row("Verified Vacant", verificationDisplay(verifiedVacant))}
          ${row("Potential Target", potentialTarget || "No")}
        </table>
      </div>`;
}

function renderFeatures(geojson) {
  markerLayer.clearLayers();
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
    const color = getColor(feature);
    const borderColor = getBorderColor(feature);
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

    // Polygon outlines always stay in markerLayer. Redfin centroid markers are
    // shown on redfinLayer only when the source toggle is enabled.
    const circleLayer = p.on_redfin && redfinLayerVisible ? redfinLayer : markerLayer;

    let layer;
    if (renderCondoOutline) {
      // Interactive so clicking the building outline shows the first unit's popup.
      L.geoJSON(feature, {
        style: {
          color: borderColor,
          fill: false,
          weight: 1.2,
          opacity: 0.75,
        },
      })
        .bindPopup(() => makePopupHtml(p), { maxWidth: 280 })
        .on("click", applyBrush)
        .addTo(markerLayer);
    }

    if (renderPolygon) {
      layer = L.geoJSON(feature, {
        style: {
          color: borderColor,
          fillColor: color,
          fillOpacity: 0.12,
          weight: 1.5,
          opacity: 0.85,
        },
      }).bindPopup(() => makePopupHtml(p), { maxWidth: 280 });
      layer.on("click", applyBrush);
      layer.addTo(markerLayer);
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
        radius: p.on_redfin ? 7 : 5,
        fillColor: color,
        color: borderColor,
        weight: 1.5,
        opacity: 1,
        fillOpacity: 0.9,
      }).bindPopup(() => makePopupHtml(p), { maxWidth: 280 });
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

  // In viewport render mode, build shortlist from the full feature set so the
  // list doesn't change as the user pans. Clicking flies to the parcel;
  // the viewport re-render will show it and the user can click for popup.
  const shortlistFeatures = viewportRenderMode
    ? (allAnalysisFeatures || [])
        .filter(f => !f.properties.on_redfin && f.properties.prop_type === "single_family")
        .sort((a, b) => (parseFloat(b.properties.land_pct) || 0) - (parseFloat(a.properties.land_pct) || 0))
    : Object.values(markers)
        .map(m => m.feature)
        .filter(f => !f.properties.on_redfin && f.properties.prop_type === "single_family")
        .sort((a, b) => (parseFloat(b.properties.land_pct) || 0) - (parseFloat(a.properties.land_pct) || 0));

  const list = document.getElementById("parcel-list");
  list.innerHTML =
    shortlistFeatures.length === 0
      ? "<p class='sidebar-note'>No off-market SFR parcels found.</p>"
      : "<p class='sidebar-label'>Off-Market SFR Sorted by Land %</p>" +
        shortlistFeatures
          .map(
            (f) => `
          <div class="parcel-row" data-addr="${f.properties.addr}" data-lat="${f.properties.lat}" data-lng="${f.properties.lng}">
            <span class="parcel-addr">${f.properties.addr}</span>
            <span class="parcel-pct">${f.properties.land_pct}</span>
          </div>`
          )
          .join("");

  list.querySelectorAll(".parcel-row").forEach((el) => {
    el.addEventListener("click", () => {
      const entry = markers[el.dataset.addr];
      if (entry && entry.layer) {
        map.flyTo([entry.feature.properties.lat, entry.feature.properties.lng], 18);
        entry.layer.openPopup();
      } else {
        // viewport mode — fly to parcel; viewport re-render fires on moveend
        const lat = parseFloat(el.dataset.lat);
        const lng = parseFloat(el.dataset.lng);
        if (!isNaN(lat) && !isNaN(lng)) map.flyTo([lat, lng], 18);
      }
    });
  });
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
const TILE_MAX_SPLIT_DEPTH = 3; // max adaptive refinement depth per failing tile

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
  if (area > 0.04) return splitBboxIntoNxN(bbox, 5); // ~county-wide → 25 tiles
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

async function fetchTileDataRecursive(tilePolygon, includeRedfin, depth, tileLabel) {
  const resp = await fetch("/api/analyze", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ polygon: tilePolygon, include_redfin: includeRedfin }),
  });

  if (resp.ok) {
    const data = await resp.json();
    if (data.source_status && (!data.source_status.dcad_ok || !data.source_status.tad_ok)) {
      throw new Error(`Incomplete county result on tile ${tileLabel}`);
    }
    return [data];
  }

  if (resp.status === 503 || resp.status === 502 || resp.status === 504) {
    if (depth < TILE_MAX_SPLIT_DEPTH) {
      // For 503 (server overloaded), wait before splitting to let Cloud Run recover.
      const waitMs = resp.status === 503 ? 3000 : 1000;
      document.getElementById("redfin-status").textContent =
        `Tile ${tileLabel} overloaded (${resp.status}) — retrying in ${waitMs / 1000}s...`;
      await new Promise(r => setTimeout(r, waitMs));

      // Try the same tile once more before splitting.
      const retry = await fetch("/api/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ polygon: tilePolygon, include_redfin: includeRedfin }),
      });
      if (retry.ok) {
        const retryData = await retry.json();
        return [retryData];
      }

      // Still failing — split into subtiles.
      document.getElementById("redfin-status").textContent =
        `Tile ${tileLabel} still failing — splitting into subtiles...`;
      const subTiles = splitBboxIntoTiles(getPolygonBbox(tilePolygon));
      const nestedResults = [];
      for (let i = 0; i < subTiles.length; i++) {
        await new Promise(r => setTimeout(r, 500)); // breathe between subtiles
        const subLabel = `${tileLabel}.${i + 1}`;
        const subPolygon = tileToPolygon(subTiles[i]);
        const subData = await fetchTileDataRecursive(subPolygon, includeRedfin, depth + 1, subLabel);
        nestedResults.push(...subData);
      }
      return nestedResults;
    }
  }

  throw new Error(`Tile ${tileLabel} failed: ${resp.status}`);
}

async function runTiledAnalysis(polygon, includeRedfin) {
  const bbox = getPolygonBbox(polygon);
  const tiles = getInitialTileGrid(bbox);
  const tileJobIds = [];
  const allFeatures = [];
  const seenParcelKeys = new Set();
  let anyRedfinOk = false;
  let anyRedfinSkipped = false;
  let lastSourceStatus = null;

  for (let i = 0; i < tiles.length; i++) {
    if (i > 0) await new Promise(r => setTimeout(r, 300)); // breathe between tiles
    document.getElementById("redfin-status").textContent =
      `Loading tile ${i + 1} of ${tiles.length}...`;
    const tilePolygon = tileToPolygon(tiles[i]);
    const tileDataList = await fetchTileDataRecursive(tilePolygon, includeRedfin, 0, `${i + 1}`);
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
    }
  }

  // Strip parcels outside the drawn polygon — tile bboxes are rectangles that
  // extend beyond irregular drawn shapes; server filters to the tile rect, not the draw.
  const filteredFeatures = allFeatures.filter(f => {
    const pt = featureCentroidLngLat(f);
    return pt ? pointInPolygonLngLat(pt, polygon) : true;
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
  const mergeResp = await fetch("/api/merge-jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ job_ids: tileJobIds }),
  });
  if (!mergeResp.ok) throw new Error("Failed to merge tile results for export");
  const mergeData = await mergeResp.json();

  return {
    type: "FeatureCollection",
    features: filteredFeatures,
    counts: mergedCounts,
    job_id: mergeData.job_id,
    redfin_requested: includeRedfin,
    redfin_ok: anyRedfinOk,
    redfin_skipped: anyRedfinSkipped,
    source_status: lastSourceStatus,
    tiled: true,
  };
}

async function runAnalysis(polygon, includeRedfin) {
  const bbox = getPolygonBbox(polygon);
  if (bboxArea(bbox) > TILE_AREA_THRESHOLD) {
    return runTiledAnalysis(polygon, includeRedfin);
  }
  const resp = await fetch("/api/analyze", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ polygon, include_redfin: includeRedfin }),
  });
  if (!resp.ok) throw new Error(`Server error: ${resp.status}`);
  return resp.json();
}
// ---------------------------------------------------------------------------

async function refreshExpiredJob() {
  if (!lastPolygon || lastPolygon.length < 3) return false;
  const includeRedfin = Boolean(document.getElementById("toggle-redfin")?.checked);
  try {
    const data = await runAnalysis(lastPolygon, includeRedfin);
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
}

map.on("draw:created", async (e) => {
  drawLayer.clearLayers();
  markerLayer.clearLayers();
  redfinLayer.clearLayers();
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
  const includeRedfin = Boolean(document.getElementById("toggle-redfin")?.checked);
  document.getElementById("redfin-status").textContent = includeRedfin
    ? "Pulling Redfin listings..."
    : "Skipping Redfin pull (DCAD-only mode)...";
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

  try {
    const data = await runAnalysis(polygon, includeRedfin);
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
    document.getElementById("redfin-status").textContent = includeRedfin
      ? (data.redfin_skipped
          ? `Redfin auto-disabled — area too large (${data.counts.total.toLocaleString()} parcels)`
          : data.redfin_ok
            ? `${data.counts.active} active listing${data.counts.active !== 1 ? "s" : ""} found`
            : "Redfin pull unavailable; DCAD results shown")
      : "DCAD-only mode (Redfin disabled)";
    redfinLayerVisible = Boolean(document.getElementById("toggle-redfin")?.checked);
    if (redfinLayerVisible) {
      redfinLayer.addTo(map);
    } else {
      map.removeLayer(redfinLayer);
    }
    lastIncludedRedfin = includeRedfin;
    lastAnalysisGeojson = data;
    lastAnalysisCounts = data.counts;
    allAnalysisFeatures = data.features;
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
    // Update the always-visible toggle status line.
    const toggleStatus = document.getElementById("redfin-toggle-status");
    if (toggleStatus) {
      if (!includeRedfin) {
        toggleStatus.textContent = "";
      } else if (data.redfin_skipped) {
        toggleStatus.textContent = `Redfin skipped — ${data.counts.total.toLocaleString()} parcels`;
      } else if (!data.redfin_ok) {
        toggleStatus.textContent = "Redfin unavailable";
      } else if (data.counts.active === 0) {
        toggleStatus.textContent = "No active listings found";
      } else {
        toggleStatus.textContent = `${data.counts.active} active listing${data.counts.active !== 1 ? "s" : ""} found`;
      }
    }
  } catch (err) {
    console.error("[draw:created] Analysis failed:", err);
    document.getElementById("sidebar-loading").classList.add("hidden");
    document.getElementById("sidebar-instructions").classList.remove("hidden");
    document.getElementById("btn-draw-clear")?.classList.remove("hidden");
    alert("Analysis failed: " + err.message);
  }
});

function clearDrawResults() {
  viewportRenderMode = false;
  allAnalysisFeatures = null;
  clearTimeout(_vpRenderTimeout);
  drawLayer.clearLayers();
  maskLayer.clearLayers();
  if (!map.hasLayer(browseLayer)) browseLayer.addTo(map);
  markerLayer.clearLayers();
  redfinLayer.clearLayers();
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
  document.getElementById("redfin-toggle-status").textContent = "";
  lastIncludedRedfin = false;
});

// Redfin toggle: if Redfin wasn't included in the last fetch, re-run analysis
// with include_redfin: true, preserving any in-session verification tags.
async function rerunWithRedfin() {
  if (!lastPolygon || lastPolygon.length < 3) return;
  const statusEl = document.getElementById("redfin-toggle-status");
  const toggleEl = document.getElementById("toggle-redfin");
  if (toggleEl) toggleEl.disabled = true;
  if (statusEl) statusEl.textContent = "Fetching Redfin\u2026";
  try {
    const data = await runAnalysis(lastPolygon, true);
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
    lastAnalysisGeojson = data;
    lastAnalysisCounts = data.counts;
    redfinLayerVisible = true;
    redfinLayer.addTo(map);
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

  const result = browseLayer.queryTileFeaturesDebug(ev.latlng.lng, ev.latlng.lat);
  // v3 returns Map<string, PickedFeature[]> — flatten all values regardless of key name
  const allFeatures = result instanceof Map
    ? [...result.values()].flat()
    : (Array.isArray(result) ? result : []);
  if (allFeatures.length === 0) return;

  const parcel = allFeatures.find(f => {
    const props = (f.feature && f.feature.props) || f.props || {};
    return props.source_county === "dcad" || props.source_county === "tad";
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

renderSavedAreasList();