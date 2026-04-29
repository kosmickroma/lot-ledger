// frontend/map.js
//
// Leaflet map init, draw tool, and all client-side UI logic.
// Sends drawn polygon to the backend, renders the GeoJSON response,
// populates the sidebar, and handles CSV download.
//
// Connects to:
//   frontend/index.html   — loaded as a script tag
//   frontend/style.css    — classes referenced here must exist there
//   POST /api/analyze     — sends polygon, receives GeoJSON + counts  (Phase 5)
//   GET  /api/download/:id — triggers CSV file download               (Phase 5)

const DALLAS_CENTER = [32.78, -96.8];
const DEFAULT_ZOOM = 11;

const map = L.map("map", {
  zoomControl: true,
}).setView(DALLAS_CENTER, DEFAULT_ZOOM);

L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", {
  attribution: "&copy; OpenStreetMap contributors &copy; CARTO",
  subdomains: "abcd",
  maxZoom: 20,
}).addTo(map);