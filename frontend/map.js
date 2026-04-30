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
let drawLayer = L.layerGroup().addTo(map);
let hoaLayer = null;
let hoaVisible = false;
let currentJobId = null;
const verificationByAccount = new Map();
let verificationMode = false;
let verificationSelection = "Yes";

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
    verifyBtn.title = "Toggle verification mode";
    verifyBtn.textContent = "V";
    L.DomEvent.on(verifyBtn, "click", (e) => {
      L.DomEvent.preventDefault(e);
      setVerificationMode(!verificationMode);
    });

    return container;
  },
});
new MapToolbar().addTo(map);

const VerificationTool = L.Control.extend({
  options: { position: "topleft" },
  onAdd() {
    const container = L.DomUtil.create("div", "leaflet-bar verify-tool hidden");
    container.id = "verify-tool";
    L.DomEvent.disableClickPropagation(container);

    const title = L.DomUtil.create("div", "verify-tool-title", container);
    title.textContent = "Verify Vacant";

    const yesBtn = L.DomUtil.create("button", "verify-tool-btn active", container);
    yesBtn.type = "button";
    yesBtn.dataset.value = "Yes";
    yesBtn.textContent = "Yes";

    const noBtn = L.DomUtil.create("button", "verify-tool-btn", container);
    noBtn.type = "button";
    noBtn.dataset.value = "No";
    noBtn.textContent = "No";

    const clearBtn = L.DomUtil.create("button", "verify-tool-btn", container);
    clearBtn.type = "button";
    clearBtn.dataset.value = "";
    clearBtn.textContent = "Clear";

    [yesBtn, noBtn, clearBtn].forEach((btn) => {
      L.DomEvent.on(btn, "click", () => {
        setVerificationSelection(btn.dataset.value || "");
      });
    });

    return container;
  },
});
new VerificationTool().addTo(map);

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

    L.DomEvent.on(mapBtn, "click", () => {
      if (activeBasemap === "street") return;
      map.removeLayer(satelliteLayer);
      map.removeLayer(labelsLayer);
      streetLayer.addTo(map);
      activeBasemap = "street";
      mapBtn.classList.add("active");
      satBtn.classList.remove("active");
    });

    L.DomEvent.on(satBtn, "click", () => {
      if (activeBasemap === "satellite") return;
      map.removeLayer(streetLayer);
      satelliteLayer.addTo(map);
      labelsLayer.addTo(map);
      activeBasemap = "satellite";
      satBtn.classList.add("active");
      mapBtn.classList.remove("active");
    });

    return container;
  },
});
new BasemapSwitcher().addTo(map);
const appShell = document.querySelector(".app-shell");
const sidebarToggleBtn = document.getElementById("sidebar-toggle");
const drawHelper = document.getElementById("draw-helper");

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

sidebarToggleBtn.addEventListener("click", () => {
  const collapsed = appShell.classList.contains("sidebar-collapsed");
  setSidebarCollapsed(!collapsed);
});

function getColor(feature) {
  if (feature.properties.on_redfin) return COLORS.active;
  return COLORS[feature.properties.prop_type] || COLORS.exempt;
}

function getBorderColor(feature) {
  if (feature.properties.on_redfin) return BORDER_COLORS.active;
  return BORDER_COLORS[feature.properties.prop_type] || BORDER_COLORS.exempt;
}

function getStatusLabel(feature) {
  if (feature.properties.on_redfin) return "ACTIVE LISTING";
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

function setVerificationSelection(value) {
  verificationSelection = normalizeVerificationValue(value);
  const tool = document.getElementById("verify-tool");
  if (!tool) return;
  tool.querySelectorAll(".verify-tool-btn").forEach((btn) => {
    const btnValue = normalizeVerificationValue(btn.dataset.value || "");
    btn.classList.toggle("active", btnValue === verificationSelection);
  });
}

function setVerificationMode(enabled) {
  verificationMode = Boolean(enabled);
  document.getElementById("btn-verify-toggle")?.classList.toggle("active", verificationMode);
  document.getElementById("verify-tool")?.classList.toggle("hidden", !verificationMode);
  map.getContainer().classList.toggle("verify-mode", verificationMode);
}

function makePopupHtml(p) {
  const pseudoFeature = { properties: p };
  const statusColor = getColor(pseudoFeature);
  const statusText = getStatusLabel(pseudoFeature);
  const verifiedVacant = normalizeVerificationValue(
    verificationByAccount.get(p.account_num) || p.verified_vacant
  );
  const row = (label, val) => `<tr><td class="popup-label">${label}</td><td class="popup-val">${val || "N/A"}</td></tr>`;
  return `
      <div class="popup">
        <div class="popup-addr">${p.addr || "Unknown address"}</div>
        <div class="popup-status" style="color:${statusColor};">${statusText}</div>
        <table class="popup-table">
          ${row("Owner", p.owner)}
          ${row("Land Value", p.land_val)}
          ${row("Total Value", p.tot_val)}
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
        </table>
      </div>`;
}

function renderFeatures(geojson) {
  markerLayer.clearLayers();
  const markers = {};
  geojson.features.forEach((feature) => {
    const p = feature.properties;
    const color = getColor(feature);
    const borderColor = getBorderColor(feature);
    if (p.lat == null || p.lng == null) return;

    const applyVerification = () => {
      if (!verificationMode || !p.account_num) return;
      verificationByAccount.set(p.account_num, verificationSelection);
      p.verified_vacant = verificationSelection;
    };

    let layer;
    if (feature.geometry?.type === "Polygon") {
      layer = L.geoJSON(feature, {
        style: {
          color: borderColor,
          fillColor: color,
          fillOpacity: 0.12,
          weight: 1.5,
          opacity: 0.85,
        },
      }).bindPopup(() => makePopupHtml(p), { maxWidth: 280 });
      layer.on("click", applyVerification);
      layer.addTo(markerLayer);

      L.circleMarker([p.lat, p.lng], {
        radius: p.on_redfin ? 5 : 3,
        fillColor: color,
        color: borderColor,
        weight: 1,
        opacity: 1,
        fillOpacity: 0.95,
      })
        .bindPopup(() => makePopupHtml(p), { maxWidth: 280 })
        .on("click", applyVerification)
        .addTo(markerLayer);
    } else {
      layer = L.circleMarker([p.lat, p.lng], {
        radius: p.on_redfin ? 7 : 5,
        fillColor: color,
        color: borderColor,
        weight: 1.5,
        opacity: 1,
        fillOpacity: 0.9,
      }).bindPopup(() => makePopupHtml(p), { maxWidth: 280 });
      layer.on("click", applyVerification);
      layer.addTo(markerLayer);
    }

    markers[p.addr] = { layer, feature };
  });
  return markers;
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

map.on("draw:created", async (e) => {
  drawLayer.clearLayers();
  markerLayer.clearLayers();
  verificationByAccount.clear();
  setVerificationMode(false);
  setVerificationSelection("Yes");
  document.getElementById("sidebar-instructions").classList.add("hidden");
  document.getElementById("sidebar-results").classList.add("hidden");
  document.getElementById("sidebar-loading").classList.remove("hidden");
  document.getElementById("redfin-status").textContent = "Pulling Redfin listings...";
  drawLayer.addLayer(e.layer);

  const polygon = e.layer.getLatLngs()[0].map((ll) => [ll.lng, ll.lat]);

  try {
    const resp = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ polygon }),
    });
    if (!resp.ok) throw new Error(`Server error: ${resp.status}`);
    const data = await resp.json();
    currentJobId = data.job_id;
    data.features.forEach((feature) => {
      const p = feature.properties || {};
      if (!p.account_num) return;
      const normalized = normalizeVerificationValue(p.verified_vacant);
      verificationByAccount.set(p.account_num, normalized);
      p.verified_vacant = normalized;
    });
    document.getElementById("redfin-status").textContent = data.redfin_ok
      ? `${data.counts.active} active listing${data.counts.active !== 1 ? "s" : ""} found`
      : "Active listings unavailable";
    const markers = renderFeatures(data);
    renderSidebar(data.counts, markers);
  } catch (err) {
    document.getElementById("sidebar-loading").classList.add("hidden");
    document.getElementById("sidebar-instructions").classList.remove("hidden");
    alert("Analysis failed: " + err.message);
  }
});

map.on("draw:drawstart", () => {
  drawHelper.classList.remove("hidden");
  document.getElementById("btn-draw")?.classList.add("active");
});

map.on("draw:drawstop", () => {
  drawHelper.classList.add("hidden");
  document.getElementById("btn-draw")?.classList.remove("active");
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

    try {
      const payload = {};
      verificationByAccount.forEach((value, accountNum) => {
        payload[accountNum] = String(value || "").toLowerCase();
      });
      await fetch(`/api/job/${currentJobId}/verification`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ verifications: payload }),
      });
    } catch (err) {
      console.error("Unable to persist verification status before export", err);
    }

    const suggested = makeDefaultCsvName();
    const entered = window.prompt("Name this CSV export:", suggested);
    if (entered === null) return;
    const filename = normalizeCsvFilename(entered);
    window.location.href = `/api/download/${currentJobId}?filename=${encodeURIComponent(filename)}`;
  })();
});

document.getElementById("btn-clear").addEventListener("click", () => {
  markerLayer.clearLayers();
  drawLayer.clearLayers();
  currentJobId = null;
  verificationByAccount.clear();
  setVerificationMode(false);
  setVerificationSelection("Yes");
  document.getElementById("sidebar-results").classList.add("hidden");
  document.getElementById("sidebar-loading").classList.add("hidden");
  document.getElementById("sidebar-instructions").classList.remove("hidden");
});