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