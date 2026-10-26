// Port and Coastal Operations Twin: Live, Replay and Flood panels on one Cesium viewer.
// Served by twin_app.py, which also gives /api/config, /api/replay and the websocket feeds.
"use strict";

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const color = (name) => Cesium.Color.fromCssColorString(css(name));
const $ = (id) => document.getElementById(id);

// --- Vessel types --------------------------------------------------------------------------

// Three validated categorical colours for the groups seen most in the harbour, gray for the rest
const TYPE_GROUPS = [
  { key: "cargo", label: "Cargo & tanker", css: "--type-cargo" },
  { key: "passenger", label: "Passenger & ferry", css: "--type-passenger" },
  { key: "service", label: "Tug, pilot & service", css: "--type-service" },
  { key: "other", label: "Other or not yet known", css: "--type-other" },
];
// Towing, dredging, diving, military, pilot, SAR, tug, port tender, anti-pollution,
// law enforcement, medical, non-combatant
const SERVICE_TYPES = new Set([31, 32, 33, 34, 35, 50, 51, 52, 53, 54, 55, 58, 59]);
const TYPE_NAMES = {
  30: "Fishing", 31: "Towing", 32: "Towing (large)", 33: "Dredging or underwater ops",
  34: "Diving ops", 35: "Military ops", 36: "Sailing", 37: "Pleasure craft", 50: "Pilot vessel",
  51: "Search and rescue", 52: "Tug", 53: "Port tender", 54: "Anti-pollution",
  55: "Law enforcement", 58: "Medical transport", 59: "Non-combatant",
};
const TYPE_DECADES = { 2: "Wing in ground", 4: "High-speed craft", 6: "Passenger", 7: "Cargo",
  8: "Tanker", 9: "Other type" };
const NAV_STATUS = ["Under way using engine", "At anchor", "Not under command",
  "Restricted manoeuvrability", "Constrained by draught", "Moored", "Aground",
  "Engaged in fishing", "Under way sailing"];
const MOVING_SOG = 1.0;  // kn, matches MOORED_SOG in ais_consumer.py

function typeGroup(code) {
  if (code == null) return "other";
  if (code >= 70 && code <= 89) return "cargo";
  if (code >= 60 && code <= 69) return "passenger";
  if (SERVICE_TYPES.has(code)) return "service";
  return "other";
}

function typeName(code) {
  if (code == null) return "Type not yet reported";
  return TYPE_NAMES[code] ?? TYPE_DECADES[Math.floor(code / 10)] ?? `Type ${code}`;
}

// --- Formatting ----------------------------------------------------------------------------

const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const timeZoneFmt = new Intl.DateTimeFormat(undefined,
  { hour: "2-digit", minute: "2-digit", timeZoneName: "short" });
const dayTimeFmt = new Intl.DateTimeFormat(undefined,
  { weekday: "short", hour: "2-digit", minute: "2-digit" });
const dayFmt = new Intl.DateTimeFormat(undefined,
  { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });
const weekdayFmt = new Intl.DateTimeFormat(undefined, { weekday: "narrow", timeZone: "UTC" });

const fmt = (value, unit) => (value === null || value === undefined ? "–" : `${value}${unit}`);
const signed = (m) => `${m >= 0 ? "+" : "−"}${Math.abs(m).toFixed(1)} m`;
const escapeHtml = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function ago(iso) {
  const minutes = Math.round((Date.now() - Date.parse(iso)) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 90) return `${minutes} min ago`;
  return `${Math.round(minutes / 60)} h ago`;
}

// --- Viewer --------------------------------------------------------------------------------

const viewer = new Cesium.Viewer("viewer", {
  baseLayer: new Cesium.ImageryLayer(new Cesium.OpenStreetMapImageryProvider({
    url: "https://tile.openstreetmap.org/" })),
  sceneMode: Cesium.SceneMode.SCENE2D,
  mapProjection: new Cesium.WebMercatorProjection(),
  baseLayerPicker: false, geocoder: false, homeButton: false, sceneModePicker: false,
  navigationHelpButton: false, animation: false, timeline: false, fullscreenButton: false,
  infoBox: false, selectionIndicator: false,
});
const scene = viewer.scene;
scene.globe.depthTestAgainstTerrain = true;
viewer.cesiumWidget.screenSpaceEventHandler.removeInputAction(
  Cesium.ScreenSpaceEventType.LEFT_DOUBLE_CLICK);  // no entity tracking

const sources = {
  live: new Cesium.CustomDataSource("live"),
  replay: new Cesium.CustomDataSource("replay"),
  flood: new Cesium.CustomDataSource("flood"),
};
Object.values(sources).forEach((s) => viewer.dataSources.add(s));

const tooltips = new WeakMap();  // entity -> () => html

// --- Feeds ---------------------------------------------------------------------------------

// All feeds share one port and are told apart by path, e.g. ws://host:8765/tide
function feed(port, path, onMessage, onState = () => {}) {
  let retryMs = 1000;
  const open = () => {
    // wss:// when the page itself came over HTTPS (browsers block ws:// from https pages)
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${scheme}://${location.hostname || "localhost"}:${port}${path}`);
    ws.onopen = () => { retryMs = 1000; onState("live"); };
    ws.onmessage = (e) => onMessage(JSON.parse(e.data));
    ws.onclose = () => {
      onState("down", retryMs);
      setTimeout(open, retryMs);
      retryMs = Math.min(retryMs * 2, 30000);
    };
  };
  open();
}

// The ship feed sends what it has as soon as a browser connects, then each new file the
// consumer writes (about once a minute). No message soon after connecting means nothing has
// been collected; positions that stop getting newer mean ais_consumer.py has stopped.
const FIRST_DATA_MS = 10000;
const VESSELS_STALE_MS = 5 * 60000;
const vesselFeed = { state: "connecting", retryMs: 0, connectedAt: 0, received: false, newest: null };

function vesselFeedState(state, retryMs) {
  Object.assign(vesselFeed, { state, retryMs });
  if (state === "live") {
    vesselFeed.connectedAt = Date.now();
    setTimeout(renderVesselStatus, FIRST_DATA_MS);
  }
  renderVesselStatus();
}

function renderVesselStatus() {
  const f = vesselFeed;
  let state = "live", text = "live", hint = "";
  if (f.state === "down") {
    state = "down";
    text = "Ship feed not reachable";
    hint = `Retrying in ${f.retryMs / 1000} s. twin_app.py serves it on port ${config.feed_port}, `
      + `path ${config.feeds.vessels}.`;
  } else if (f.state === "connecting") {
    state = "connecting";
    text = "connecting…";
  } else if (!f.received) {
    const waiting = Date.now() - f.connectedAt < FIRST_DATA_MS;
    state = waiting ? "connecting" : "down";
    text = waiting ? "waiting for positions…" : "No ship positions collected yet";
    hint = waiting ? "" : "Start ais_consumer.py to collect them.";
  } else if (f.newest !== null && Date.now() - f.newest > VESSELS_STALE_MS) {
    state = "stale";
    text = `Newest position ${ago(new Date(f.newest).toISOString())}`;
    hint = "Showing the last known positions. Is ais_consumer.py running?";
  }
  const el = $("vessels-status");
  el.dataset.state = state;
  el.textContent = text;
  $("vessels-hint").textContent = hint;
  $("vessels-hint").hidden = !hint;
}

// --- Live: vessels -------------------------------------------------------------------------

const iconCache = new Map();
function vesselIcon(shape, fill) {
  const key = `${shape}${fill}`;
  if (!iconCache.has(key)) {
    const size = 56;  // drawn at 2x for sharp edges, shown at 28 px
    const canvas = Object.assign(document.createElement("canvas"), { width: size, height: size });
    const ctx = canvas.getContext("2d");
    ctx.translate(size / 2, size / 2);
    ctx.beginPath();
    if (shape === "arrow") {
      ctx.moveTo(0, -23); ctx.lineTo(16, 20); ctx.lineTo(0, 11); ctx.lineTo(-16, 20);
      ctx.closePath();
    } else {
      ctx.arc(0, 0, 10, 0, 2 * Math.PI);
    }
    ctx.fillStyle = fill;
    ctx.fill();
    ctx.lineJoin = "round";
    ctx.lineWidth = 3.5;
    ctx.strokeStyle = "#ffffff";  // keeps the lighter type colours visible on the map
    ctx.stroke();
    iconCache.set(key, canvas.toDataURL());
  }
  return iconCache.get(key);
}

function vesselTooltip(p) {
  const name = p.ship_name ? escapeHtml(p.ship_name) : "Unknown vessel";
  return `<b>${name}</b><br>${escapeHtml(typeName(p.ship_type))} · MMSI ${p.mmsi}<br>
    ${NAV_STATUS[p.nav_status] ?? "Status not reported"}<br>
    SOG ${fmt(p.sog, " kn")} · COG ${fmt(p.cog, "°")} · HDG ${fmt(p.heading, "°")}<br>
    <span class="muted">reported ${ago(p.ts)}</span>`;
}

function renderTypeLegend(counts = {}) {
  $("type-legend").innerHTML = TYPE_GROUPS.map((g) => `<li>
      <span class="dot" style="background: var(${g.css})"></span>${g.label}
      <span class="count">${counts[g.key] ?? 0}</span></li>`).join("");
}

function updateVessels(collection) {
  const entities = sources.live.entities;
  const seen = new Set();
  const counts = {};
  entities.suspendEvents();
  for (const f of collection.features) {
    const p = f.properties;
    const [lon, lat] = f.geometry.coordinates;
    const id = `vessel-${p.mmsi}`;
    const group = typeGroup(p.ship_type);
    counts[group] = (counts[group] ?? 0) + 1;
    // Heading if reported, else course over ground while moving (COG is noise at rest)
    const moving = p.sog !== null && p.sog >= MOVING_SOG;
    const bearing = p.heading ?? (moving ? p.cog : null);
    const shape = bearing === null ? "circle" : "arrow";
    seen.add(id);
    const entity = entities.getById(id) ?? entities.add({ id, billboard: {
      alignedAxis: Cesium.Cartesian3.UNIT_Z, width: 28, height: 28 } });
    entity.position = Cesium.Cartesian3.fromDegrees(lon, lat);
    entity.billboard.image = vesselIcon(shape, css(TYPE_GROUPS.find((g) => g.key === group).css));
    entity.billboard.rotation = -Cesium.Math.toRadians(bearing ?? 0);
    tooltips.set(entity, () => vesselTooltip(p));
  }
  for (const entity of [...entities.values]) {
    if (!seen.has(entity.id)) entities.remove(entity);
  }
  entities.resumeEvents();
  renderTypeLegend(counts);
  $("vessel-count").textContent = collection.features.length;
  // The newest position report, not when it arrived: the feed replays old files on connect
  const times = collection.features.map((f) => Date.parse(f.properties.ts)).filter(Number.isFinite);
  vesselFeed.received = true;
  if (times.length) vesselFeed.newest = Math.max(...times);
  $("vessels-updated").textContent = vesselFeed.newest === null ? "–" : timeFmt.format(new Date(vesselFeed.newest));
  renderVesselStatus();
}

// --- Live: tide ----------------------------------------------------------------------------

let tide = null;  // latest tide message

const TREND_ARROWS = { rising: "↑", falling: "↓", steady: "→" };

function updateTide(message) {
  tide = message;
  $("tide-level").textContent = tide.tide_level.toFixed(2);
  const arrow = $("tide-trend");
  arrow.textContent = TREND_ARROWS[tide.trend] ?? "";
  arrow.setAttribute("aria-label", tide.trend ?? "trend unknown");
  const kind = tide.series === "wlo" ? "observed" : "predicted";
  const when = timeZoneFmt.format(new Date(tide.ts));
  const trend = tide.trend
    ? `${tide.trend[0].toUpperCase()}${tide.trend.slice(1)} ${Math.abs(tide.change).toFixed(3)} m `
      + `since ${timeFmt.format(new Date(tide.previous_ts))}`
    : "Trend not known yet";
  const stale = Date.now() - Date.parse(tide.ts) > 30 * 60000 ? ` · last reading ${ago(tide.ts)}` : "";
  $("tide-station").textContent = config.station;
  $("tide-detail").innerHTML = `${trend}<br>Above chart datum, ${kind} for ${when}${stale}`;
  updateWater();
}

// --- Replay --------------------------------------------------------------------------------

let replay = null;  // {days: [...]} from /api/replay
let playTimer = null;

function dwellColor(hours) {
  const t = Math.min(Math.max(hours / 24, 0), 1);
  return t < 0.5
    ? Cesium.Color.lerp(color("--good"), color("--warning"), t * 2, new Cesium.Color())
    : Cesium.Color.lerp(color("--warning"), color("--critical"), (t - 0.5) * 2, new Cesium.Color());
}

function replayTooltip(v, date) {
  const name = v.ship_name ? escapeHtml(v.ship_name) : "Unknown vessel";
  return `<b>${name}</b><br>${escapeHtml(typeName(v.ship_type))} · MMSI ${v.mmsi}<br>
    ${escapeHtml(v.berth_name ?? "Unnamed berth")}<br>
    Moored ${v.dwell_time} h on ${dayFmt.format(new Date(`${date}T00:00:00Z`))}<br>
    <span class="muted">mean position in the berth</span>`;
}

async function loadReplay() {
  const response = await fetch("/api/replay?days=7");
  replay = await response.json();
  const slider = $("replay-slider");
  slider.max = replay.days.length - 1;
  const latest = replay.days.map((d) => d.analysed).lastIndexOf(true);
  slider.value = latest >= 0 ? latest : replay.days.length - 1;
  $("replay-ticks").innerHTML = replay.days
    .map((d) => `<span>${weekdayFmt.format(new Date(`${d.date}T00:00:00Z`))}</span>`).join("");
  showReplayDay(Number(slider.value));
}

function showReplayDay(index) {
  const day = replay.days[index];
  const entities = sources.replay.entities;
  entities.suspendEvents();
  entities.removeAll();
  // Longest dwell drawn last, so it sits on top where vessels share a berth
  for (const v of [...day.vessels].sort((a, b) => a.dwell_time - b.dwell_time)) {
    const entity = entities.add({
      position: Cesium.Cartesian3.fromDegrees(v.lon, v.lat),
      point: { pixelSize: 13, color: dwellColor(v.dwell_time),
               outlineColor: Cesium.Color.WHITE, outlineWidth: 2 },
    });
    tooltips.set(entity, () => replayTooltip(v, day.date));
  }
  entities.resumeEvents();
  $("replay-date").textContent = dayFmt.format(new Date(`${day.date}T00:00:00Z`));
  if (!day.analysed) {
    $("replay-detail").textContent = "Not analysed (run ais_consumer.py --compact for this day)";
  } else {
    const n = day.vessels.length;
    const longest = Math.max(0, ...day.vessels.map((v) => v.dwell_time));
    $("replay-detail").textContent = n
      ? `${n} vessel berth stays · longest ${longest} h`
      : "No vessels moored in a berth";
  }
}

function togglePlay(play = playTimer === null) {
  clearInterval(playTimer);
  playTimer = null;
  $("replay-play").textContent = play ? "❚❚" : "▶";
  $("replay-play").setAttribute("aria-label", play ? "Pause" : "Play");
  if (!play) return;
  const slider = $("replay-slider");
  if (Number(slider.value) === Number(slider.max)) slider.value = 0;
  showReplayDay(Number(slider.value));
  playTimer = setInterval(() => {
    if (Number(slider.value) >= Number(slider.max)) return togglePlay(false);
    slider.value = Number(slider.value) + 1;
    showReplayDay(Number(slider.value));
  }, 1500);
}

// --- Flood ---------------------------------------------------------------------------------

let config = null;           // /api/config
let tileset = null;
let terrainLoaded = false;
let buildingsAvailable = true;  // false once the tiles turn out to be missing or broken
let waterEllipsoidal = null; // water plane height on the WGS84 ellipsoid, as the terrain is
const groundByBuilding = new Map();  // feature_id -> z_bottom (m CGVD2013), loaded tiles only

const waterLevels = () => {
  const offset = Number($("water-slider").value);
  const cd = tide.tide_level + offset;
  const cgvd = cd + config.datums.chart_datum_cgvd2013;
  return { offset, cd, cgvd, ellipsoidal: cgvd + config.datums.cgvd2013_to_wgs84 };
};

function updateWater() {
  const offset = Number($("water-slider").value);
  $("water-offset").textContent = signed(offset);
  if (!tide || !config) return;
  const w = waterLevels();
  waterEllipsoidal = w.ellipsoidal;
  $("water-level").textContent = w.cd.toFixed(2);
  const flooded = [...groundByBuilding.values()].filter((z) => z <= w.cgvd).length;
  $("water-detail").innerHTML = `Tide ${tide.tide_level.toFixed(2)} m ${signed(offset)}`
    + ` · ${w.cgvd.toFixed(2)} m CGVD2013<br>`
    + (groundByBuilding.size
      ? `${flooded.toLocaleString()} of ${groundByBuilding.size.toLocaleString()} loaded buildings at or below the water`
      : buildingsAvailable ? "Buildings loading…" : "No buildings to check");
  if (tileset) {
    tileset.style = new Cesium.Cesium3DTileStyle({ color: { conditions: [
      [`\${z_bottom} <= ${w.cgvd.toFixed(3)}`, `color('${css("--flooded")}')`],
      ["true", "color('#000000')"]] } });
  }
}

function addWaterPlane() {
  const [w, s, e, n] = config.extent;
  sources.flood.entities.add({
    id: "water",
    rectangle: {
      coordinates: Cesium.Rectangle.fromDegrees(w, s, e, n),
      height: new Cesium.CallbackProperty(() => waterEllipsoidal ?? -1000, false),
      material: Cesium.Color.fromCssColorString("#2a78d6").withAlpha(0.55),
    },
  });
}

const LAYER_HELP = {
  terrain: "<b>Terrain not built.</b> The globe has no ground heights, so the water plane is not "
    + "placed against real ground. Run <code>python export_terrain.py</code>, then reload.",
  buildings: "<b>Buildings not built.</b> Run <code>python fetch_buildings.py</code>, "
    + "<code>python extrude_buildings.py</code> and <code>python export_buildings_3dtiles.py</code>, "
    + "then reload.",
};

function showLayerProblems(problems) {
  const notice = $("layers-notice");
  notice.innerHTML = problems.map((p) => `<p>${p}</p>`).join("");
  notice.hidden = problems.length === 0;
}

// Terrain and buildings load independently, so one missing doesn't stop the other
async function loadFloodLayers() {
  if (terrainLoaded) return;
  terrainLoaded = true;
  const problems = [];
  if (!config.layers.terrain) {
    problems.push(LAYER_HELP.terrain);
  } else {
    try {
      viewer.terrainProvider = await Cesium.CesiumTerrainProvider.fromUrl("/terrain/",
        { requestVertexNormals: true });
    } catch (e) {
      problems.push(`<b>Terrain could not be loaded:</b> ${escapeHtml(e.message ?? e)}`);
    }
  }
  if (!config.layers.buildings) {
    problems.push(LAYER_HELP.buildings);
  } else {
    try {
      await loadBuildings();
    } catch (e) {
      problems.push(`<b>Buildings could not be loaded:</b> ${escapeHtml(e.message ?? e)}`);
    }
  }
  buildingsAvailable = tileset !== null;
  showLayerProblems(problems);
  updateWater();
}

async function loadBuildings() {
  tileset = await Cesium.Cesium3DTileset.fromUrl("/buildings/tileset.json");
  tileset.outlineColor = Cesium.Color.fromCssColorString("#898781");
  tileset.show = currentTab === "flood";
  tileset.tileLoad.addEventListener((tile) => {
    const content = tile.content;
    for (let i = 0; i < content.featuresLength; i++) {
      const feature = content.getFeature(i);
      groundByBuilding.set(feature.getProperty("feature_id"), feature.getProperty("z_bottom"));
    }
    scheduleWaterUpdate();
  });
  scene.primitives.add(tileset);
}

let waterUpdatePending = false;
function scheduleWaterUpdate() {  // tiles load in bursts; recount once per burst
  if (waterUpdatePending) return;
  waterUpdatePending = true;
  setTimeout(() => { waterUpdatePending = false; updateWater(); }, 250);
}

function buildingTooltip(feature) {
  const ground = feature.getProperty("z_bottom");
  const height = feature.getProperty("heightmax");
  const depth = tide ? waterLevels().cgvd - ground : null;
  return `<b>Building</b><br>Ground ${ground.toFixed(2)} m CGVD2013 · ${height.toFixed(1)} m tall<br>`
    + (depth === null ? "" : depth >= 0
      ? `<span class="muted">${depth.toFixed(2)} m below the water plane</span>`
      : `<span class="muted">${(-depth).toFixed(2)} m above the water plane</span>`);
}

function updateStormSurge(collection) {
  const entities = sources.flood.entities;
  for (const entity of [...entities.values]) {
    if (entity.id !== "water") entities.remove(entity);
  }
  const checked = `checked ${timeFmt.format(new Date(collection.fetched_at))}`;
  const stale = Date.now() - Date.parse(collection.fetched_at) > 2 * 3600000
    ? ` · feed last updated ${ago(collection.fetched_at)}` : "";
  const features = collection.features;
  if (!features.length) {
    $("surge-headline").textContent = "No storm surge risk";
    $("surge-detail").textContent = `None forecast in the extent · ${checked}${stale}`;
    return;
  }
  for (const f of features) {
    const rings = f.geometry.type === "Polygon" ? [f.geometry.coordinates]
      : f.geometry.coordinates;  // MultiPolygon
    for (const [outer] of rings) {
      const entity = entities.add({ polygon: {
        hierarchy: Cesium.Cartesian3.fromDegreesArray(outer.flat()),
        material: color("--serious").withAlpha(0.45),
        classificationType: Cesium.ClassificationType.TERRAIN } });
      tooltips.set(entity, () => surgeTooltip(f.properties));
    }
  }
  const worst = features.reduce((a, b) => (b.properties.risk > a.properties.risk ? b : a)).properties;
  $("surge-headline").textContent = `Maximum risk level ${worst.risk}`;
  $("surge-detail").innerHTML = `Impact ${worst.impact} · likelihood ${worst.likelihood}`
    + ` · ${features.length} area${features.length === 1 ? "" : "s"}<br>`
    + `${surgePeriod(worst)} · ${checked}${stale}`;
}

const surgePeriod = (p) => `${dayTimeFmt.format(new Date(p.validity_datetime))} – `
  + `${dayTimeFmt.format(new Date(p.expiration_datetime))}`;

function surgeTooltip(p) {
  const drivers = ["storm surge", p.tide && "tide", p.waves && "waves"].filter(Boolean).join(", ");
  return `<b>Coastal flooding risk ${p.risk}</b><br>Impact ${p.impact} · likelihood ${p.likelihood}<br>
    Driven by ${drivers}<br><span class="muted">${surgePeriod(p)}</span>`;
}

// --- Tabs and camera -----------------------------------------------------------------------

let currentTab = "live";

function viewExtent2D() {
  const [w, s, e, n] = config.extent;
  viewer.camera.setView({ destination: Cesium.Rectangle.fromDegrees(w, s, e, n) });
}

function viewHarbour3D() {
  // From the harbour mouth, looking north up the Narrows towards Bedford Basin
  viewer.camera.setView({
    destination: Cesium.Cartesian3.fromDegrees(-63.585, 44.585, 2300),
    orientation: { heading: Cesium.Math.toRadians(20), pitch: Cesium.Math.toRadians(-30) },
  });
}

function morph(mode, then) {
  if (scene.mode === mode) return then();
  const remove = scene.morphComplete.addEventListener(() => { remove(); then(); });
  if (mode === Cesium.SceneMode.SCENE2D) scene.morphTo2D(0);
  else scene.morphTo3D(0);
}

function showTab(name) {
  const changed = name !== currentTab;
  currentTab = name;
  for (const tab of ["live", "replay", "flood"]) {
    $(`tab-${tab}`).setAttribute("aria-selected", String(tab === name));
    $(`tab-${tab}`).tabIndex = tab === name ? 0 : -1;
    $(`panel-${tab}`).hidden = tab !== name;
    sources[tab].show = tab === name;
  }
  if (tileset) tileset.show = name === "flood";
  hideTooltip();
  if (name !== "replay") togglePlay(false);
  if (name === "flood") {
    loadFloodLayers();
    morph(Cesium.SceneMode.SCENE3D, () => changed && viewHarbour3D());
  } else {
    if (name === "replay") loadReplay();
    morph(Cesium.SceneMode.SCENE2D, () => changed && scene.mode === Cesium.SceneMode.SCENE2D
      && viewExtent2D());
  }
}

// --- Hover tooltip -------------------------------------------------------------------------

const tooltipEl = $("tooltip");
function hideTooltip() { tooltipEl.hidden = true; }

let hoverPosition = null;
new Cesium.ScreenSpaceEventHandler(scene.canvas).setInputAction((movement) => {
  if (!hoverPosition) requestAnimationFrame(pickHover);  // at most one pick per frame
  hoverPosition = movement.endPosition;
}, Cesium.ScreenSpaceEventType.MOUSE_MOVE);

function pickHover() {
  const position = hoverPosition;
  hoverPosition = null;
  const picked = scene.pick(position);
  let html = null;
  if (picked?.id instanceof Cesium.Entity && tooltips.has(picked.id)) {
    html = tooltips.get(picked.id)();
  } else if (picked instanceof Cesium.Cesium3DTileFeature && currentTab === "flood") {
    html = buildingTooltip(picked);
  }
  scene.canvas.style.cursor = html ? "pointer" : "";
  if (!html) return hideTooltip();
  tooltipEl.innerHTML = html;
  tooltipEl.hidden = false;
  const { width, height } = tooltipEl.getBoundingClientRect();
  const x = Math.min(position.x + 14, window.innerWidth - width - 8);
  const y = position.y + 14 + height > window.innerHeight ? position.y - height - 10 : position.y + 14;
  tooltipEl.style.left = `${x}px`;
  tooltipEl.style.top = `${y}px`;
}

// --- Start ---------------------------------------------------------------------------------

async function start() {
  config = await (await fetch("/api/config")).json();
  renderTypeLegend();
  addWaterPlane();

  const tabs = ["live", "replay", "flood"];
  tabs.forEach((tab, i) => {
    $(`tab-${tab}`).addEventListener("click", () => showTab(tab));
    $(`tab-${tab}`).addEventListener("keydown", (e) => {
      const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
      if (!step) return;
      const next = tabs[(i + step + tabs.length) % tabs.length];
      showTab(next);
      $(`tab-${next}`).focus();
    });
  });
  $("replay-slider").addEventListener("input", (e) => {
    togglePlay(false);
    showReplayDay(Number(e.target.value));
  });
  $("replay-play").addEventListener("click", () => togglePlay());
  $("water-slider").addEventListener("input", updateWater);
  $("water-reset").addEventListener("click", () => { $("water-slider").value = 0; updateWater(); });

  const initial = new URLSearchParams(location.search).get("tab");
  currentTab = null;
  showTab(tabs.includes(initial) ? initial : "live");

  const port = config.feed_port;
  feed(port, config.feeds.vessels, updateVessels, vesselFeedState);
  setInterval(renderVesselStatus, 15000);  // positions go stale without any new message
  feed(port, config.feeds.tide, updateTide);
  feed(port, config.feeds.storm_surge, updateStormSurge);
}

start();
