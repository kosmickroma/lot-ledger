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

const TYPE_LABELS = {
  single_family: "Off-Market SFR",
  vacant: "Vacant Lot",
  multifamily: "Multifamily",
  commercial: "Commercial",
  exempt: "Exempt",
  active: "Active Listing",
  off_market: "Off Market",
};

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

// Transparent labels overlay — shown on top of satellite tiles
const labelsLayer = L.tileLayer(
  "https://{s}.basemaps.cartocdn.com/rastertiles/voyager_only_labels/{z}/{x}/{y}{r}.png",
  { subdomains: "abcd", maxZoom: 20, opacity: 1, pane: "overlayPane" }
);



const drawControl = new L.Control.Draw({
  draw: {
    polygon: { shapeOptions: { color: "#1a5a42", weight: 2, fill: false } },
    rectangle: false,
    circle: false,
    circlemarker: false,
    marker: false,
    polyline: false,
  },
  edit: false,
});
map.addControl(drawControl);

let markerLayer = L.layerGroup().addTo(map);
let redfinLayer = L.layerGroup().addTo(map);
const redfinToggleInput = document.getElementById("toggle-redfin");
let redfinLayerVisible = Boolean(redfinToggleInput?.checked);
if (!redfinLayerVisible) {
  map.removeLayer(redfinLayer);
}
let drawLayer = L.layerGroup().addTo(map);
let verificationBadgeLayer = L.layerGroup().addTo(map);
let targetBadgeLayer = L.layerGroup().addTo(map);
let hoaLayer = null;
let hoaVisible = false;
let currentJobId = null;
let lastPolygon = null;
let lastAnalysisGeojson = null;
let lastAnalysisCounts = null;
let lastIncludedRedfin = false;
const verificationByAccount = new Map();
const potentialTargetByAccount = new Map();
const verificationBadgeMarkers = new Map();
const targetBadgeMarkers = new Map();
let activeBrush = null;

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

    function activateBasemap(name) {
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

    return container;
  },
});
new BasemapSwitcher().addTo(map);
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

  const sorted = Object.values(markers)
    .map((m) => m.feature)
    .filter((f) => !f.properties.on_redfin && f.properties.prop_type === "single_family")
    .sort((a, b) => (parseFloat(b.properties.land_pct) || 0) - (parseFloat(a.properties.land_pct) || 0));

  const list = document.getElementById("parcel-list");
  list.innerHTML =
    sorted.length === 0
      ? "<p class='sidebar-note'>No off-market SFR parcels found.</p>"
      : "<p class='sidebar-label'>Off-Market SFR Sorted by Land %</p>" +
        sorted
          .map(
            (f) => `
          <div class="parcel-row" data-addr="${f.properties.addr}">
            <span class="parcel-addr">${f.properties.addr}</span>
            <span class="parcel-pct">${f.properties.land_pct}</span>
          </div>`
          )
          .join("");

  list.querySelectorAll(".parcel-row").forEach((el) => {
    el.addEventListener("click", () => {
      const entry = markers[el.dataset.addr];
      if (entry) {
        map.flyTo([entry.feature.properties.lat, entry.feature.properties.lng], 18);
        entry.layer.openPopup();
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
const TILE_MAX_SPLIT_DEPTH = 2; // 2x2 -> 4x4 -> 8x8 worst-case per failing branch

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

function splitBboxIntoTiles(bbox) {
  const midLng = (bbox.minLng + bbox.maxLng) / 2;
  const midLat = (bbox.minLat + bbox.maxLat) / 2;
  return [
    { minLng: bbox.minLng, minLat: bbox.minLat, maxLng: midLng,      maxLat: midLat      }, // SW
    { minLng: midLng,      minLat: bbox.minLat, maxLng: bbox.maxLng, maxLat: midLat      }, // SE
    { minLng: bbox.minLng, minLat: midLat,      maxLng: midLng,      maxLat: bbox.maxLat }, // NW
    { minLng: midLng,      minLat: midLat,      maxLng: bbox.maxLng, maxLat: bbox.maxLat }, // NE
  ];
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

  if (resp.status === 502 && depth < TILE_MAX_SPLIT_DEPTH) {
    document.getElementById("redfin-status").textContent =
      `Refining tile ${tileLabel} due to server load...`;
    const subTiles = splitBboxIntoTiles(getPolygonBbox(tilePolygon));
    const nestedResults = [];
    for (let i = 0; i < subTiles.length; i++) {
      const subLabel = `${tileLabel}.${i + 1}`;
      const subPolygon = tileToPolygon(subTiles[i]);
      const subData = await fetchTileDataRecursive(subPolygon, includeRedfin, depth + 1, subLabel);
      nestedResults.push(...subData);
    }
    return nestedResults;
  }

  throw new Error(`Tile ${tileLabel} failed: ${resp.status}`);
}

async function runTiledAnalysis(polygon, includeRedfin) {
  const bbox = getPolygonBbox(polygon);
  const tiles = splitBboxIntoTiles(bbox);
  const tileJobIds = [];
  const allFeatures = [];
  const seenParcelKeys = new Set();
  let anyRedfinOk = false;
  let anyRedfinSkipped = false;
  let lastSourceStatus = null;

  for (let i = 0; i < tiles.length; i++) {
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

  // Recount from deduplicated features
  const mergedCounts = { active: 0, off_market: 0, multifamily: 0, vacant: 0, commercial: 0, exempt: 0, total: allFeatures.length };
  for (const feature of allFeatures) {
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
    features: allFeatures,
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

  const polygon = e.layer.getLatLngs()[0].map((ll) => [ll.lng, ll.lat]);
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
    document.getElementById("btn-draw-clear")?.classList.remove("hidden");
    const markers = renderFeatures(data);
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
  drawLayer.clearLayers();
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
  lastAnalysisGeojson = null;
  lastAnalysisCounts = null;
  document.getElementById("sidebar-results")?.classList.add("hidden");
  document.getElementById("sidebar-instructions")?.classList.remove("hidden");
  document.getElementById("sidebar-loading")?.classList.add("hidden");
  document.getElementById("btn-draw-clear")?.classList.add("hidden");
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