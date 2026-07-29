/* DroneX -- planning page: mission list (left) / mission detail (middle) /
   live map (right), matching the fleet-planning UI reference. */

let planMap, miniMap;
let homeMarker, candidateMarkers = [], routeLine, droneMarker;
let missionsCache = [];
let richDataById = {};      // mission_id -> full /api/plan_mission response, cached for this session
let selectedMissionId = null;
let selectedEntry = null;
let droneHome = { lat: 35.7796, lon: -78.6382 }; // overwritten once a plan response gives the real home
let userRangeOverride = null; // miles, from settings

const SETTINGS_KEY = 'dronex_settings';

function loadSettings() {
  try {
    const s = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}');
    if (s.lat !== undefined && s.lat !== '' && !isNaN(s.lat)) droneHome.lat = parseFloat(s.lat);
    if (s.lon !== undefined && s.lon !== '' && !isNaN(s.lon)) droneHome.lon = parseFloat(s.lon);
    if (s.range !== undefined && s.range !== '' && !isNaN(s.range)) userRangeOverride = parseFloat(s.range);
  } catch (e) { /* ignore */ }
}

function openSettings() {
  const s = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}');
  document.getElementById('setHomeLat').value = s.lat ?? droneHome.lat;
  document.getElementById('setHomeLon').value = s.lon ?? droneHome.lon;
  document.getElementById('setRange').value = s.range ?? (userRangeOverride ?? '');
  document.getElementById('settingsModal').style.display = 'flex';
}

function closeSettings() {
  document.getElementById('settingsModal').style.display = 'none';
}

function saveSettings() {
  const lat = document.getElementById('setHomeLat').value;
  const lon = document.getElementById('setHomeLon').value;
  const range = document.getElementById('setRange').value;
  const s = { lat, lon, range };
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(s));
  if (lat !== '' && !isNaN(parseFloat(lat))) droneHome.lat = parseFloat(lat);
  if (lon !== '' && !isNaN(parseFloat(lon))) droneHome.lon = parseFloat(lon);
  userRangeOverride = (range !== '' && !isNaN(parseFloat(range))) ? parseFloat(range) : null;
  if (planMap) { homeMarker.setLatLng([droneHome.lat, droneHome.lon]); planMap.setView([droneHome.lat, droneHome.lon], 12); }
  closeSettings();
}

function resetSettings() {
  localStorage.removeItem(SETTINGS_KEY);
  droneHome = { lat: 35.7796, lon: -78.6382 };
  userRangeOverride = null;
  document.getElementById('setHomeLat').value = droneHome.lat;
  document.getElementById('setHomeLon').value = droneHome.lon;
  document.getElementById('setRange').value = '';
  if (planMap) { homeMarker.setLatLng([droneHome.lat, droneHome.lon]); planMap.setView([droneHome.lat, droneHome.lon], 12); }
}

// ---------------- helpers ----------------

function aqiColor(aqi) {
  if (aqi === null || aqi === undefined) return '#948da3';
  if (aqi <= 50) return '#57d38c';
  if (aqi <= 100) return '#b45cff';
  if (aqi <= 150) return '#f6a94a';
  if (aqi <= 200) return '#ff6b81';
  if (aqi <= 300) return '#c24fd6';
  return '#8b1a1a';
}

function divIcon(html, size) {
  return L.divIcon({ html, className: '', iconSize: [size, size], iconAnchor: [size / 2, size / 2] });
}

function showError(msg) {
  const box = document.getElementById('errorToast');
  if (!msg) { box.style.display = 'none'; return; }
  box.textContent = msg;
  box.style.display = 'block';
  setTimeout(() => { box.style.display = 'none'; }, 6000);
}

function fmtDate(iso) {
  if (!iso) return '--';
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

function statusFor(entry) {
  if (entry.aqi_after !== null && entry.aqi_after !== undefined) {
    return { cls: 'complete', label: 'Complete' };
  }
  if (window.__latestTelemetry && window.__latestTelemetry.mission_id === entry.mission_id) {
    return { cls: 'transit', label: 'In Flight' };
  }
  return { cls: 'awaiting', label: 'Awaiting' };
}

// ---------------- header title / toolbar ----------------

function updateHeader() {
  const today = new Date();
  document.getElementById('pageTitle').textContent =
    'Planning for ' + today.toLocaleDateString(undefined, { day: '2-digit', month: '2-digit', year: 'numeric' });

  const lastRich = richDataById[missionsCache[missionsCache.length - 1]?.mission_id];
  const candidateCount = lastRich ? lastRich.candidates.length : (missionsCache.length ? '--' : '--');
  document.getElementById('toolbarStats').textContent =
    `1 drone \u00b7 ${missionsCache.length} mission${missionsCache.length === 1 ? '' : 's'} logged \u00b7 ${candidateCount} candidates compared`;

  const anyInFlight = missionsCache.some(m => statusFor(m).cls === 'transit');
  document.getElementById('phLive').classList.toggle('on', anyInFlight);
}

// ---------------- list column ----------------

function renderList() {
  const col = document.getElementById('listCol');
  const empty = document.getElementById('listEmpty');
  col.innerHTML = '';
  if (missionsCache.length === 0) {
    col.appendChild(empty);
    empty.innerHTML = `
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M12 2L4 9v6l8 7 8-7V9l-8-7z"/></svg>
      No missions yet -- plan one above to get started.
    `;
    empty.style.display = 'flex';
    empty.style.flexDirection = 'column';
    empty.style.alignItems = 'center';
    empty.style.gap = '10px';
    return;
  }
  missionsCache.slice().reverse().forEach(entry => {
    const status = statusFor(entry);
    const card = document.createElement('div');
    card.className = `mission-card status-${status.cls}` + (entry.mission_id === selectedMissionId ? ' selected' : '');
    card.tabIndex = 0;
    card.setAttribute('role', 'button');
    card.setAttribute('aria-pressed', entry.mission_id === selectedMissionId ? 'true' : 'false');
    card.onclick = () => selectMission(entry.mission_id);
    card.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectMission(entry.mission_id); } };
    const locText = (entry.address_resolved || entry.address || 'Unknown address');
    card.innerHTML = `
      <div class="mc-top">
        <span class="mc-id">#${entry.mission_id}</span>
        <span class="status-pill ${status.cls}"><span class="dot"></span>${status.label}</span>
      </div>
      <div class="mc-loc" title="${locText}">${locText}</div>
      <div class="mc-sub">${entry.location_label ? entry.location_label + ' \u00b7 ' : ''}${entry.aqi_param || 'AQI'}</div>
      <div class="mc-bottom">
        <span><b>${entry.aqi_before ?? '--'}</b> AQI</span>
        <span>${fmtDate(entry.created)}</span>
      </div>
    `;
    col.appendChild(card);
  });
}

// ---------------- main map ----------------

function initPlanMap() {
  if (planMap) return;
  planMap = L.map('planMap', { zoomControl: true }).setView([droneHome.lat, droneHome.lon], 12);
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; OpenStreetMap contributors &copy; CARTO',
    subdomains: 'abcd',
    maxZoom: 20,
  }).addTo(planMap);
  homeMarker = L.marker([droneHome.lat, droneHome.lon], {
    icon: divIcon('<div class="home-marker"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/></svg></div>', 30),
  }).addTo(planMap).bindPopup('Home base');
  planMap.on('move zoom', positionFloatCard);
}

function clearMissionLayers() {
  candidateMarkers.forEach(m => planMap.removeLayer(m));
  candidateMarkers = [];
  if (routeLine) { planMap.removeLayer(routeLine); routeLine = null; }
  document.getElementById('targetFloatCard').style.display = 'none';
}

function plotMissionOnMap(entry, rich) {
  initPlanMap();
  clearMissionLayers();

  const targetLat = entry.target.lat, targetLon = entry.target.lon;

  if (rich) {
    rich.candidates.forEach(c => {
      const isChosen = c.lat === rich.chosen.lat && c.lon === rich.chosen.lon;
      const html = isChosen
        ? '<div class="package-marker chosen"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 8a2 2 0 0 0-1-1.7l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.7l7 4a2 2 0 0 0 2 0l7-4a2 2 0 0 0 1-1.7V8z"/></svg></div>'
        : '<div class="package-marker"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 8a2 2 0 0 0-1-1.7l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.7l7 4a2 2 0 0 0 2 0l7-4a2 2 0 0 0 1-1.7V8z"/></svg></div>';
      const marker = L.marker([c.lat, c.lon], { icon: divIcon(html, 30) })
        .addTo(planMap)
        .bindPopup(`AQI ${c.worst_aqi} (${c.worst_param})<br>${c.distance_miles} mi from address`);
      candidateMarkers.push(marker);
    });
  }

  routeLine = L.polyline([[droneHome.lat, droneHome.lon], [targetLat, targetLon]], {
    color: '#c265ff', weight: 3, opacity: 0.85, dashArray: '2 8',
  }).addTo(planMap);

  const bounds = L.latLngBounds([[droneHome.lat, droneHome.lon], [targetLat, targetLon]]);
  planMap.fitBounds(bounds.pad(0.35));

  const card = document.getElementById('targetFloatCard');
  card.style.display = 'flex';
  document.getElementById('tfcTitle').textContent = '#' + entry.mission_id;
  document.getElementById('tfcSub').textContent = `${entry.aqi_param || 'AQI'} \u00b7 ${entry.aqi_before ?? '--'}`;
  // position the float card near the target point on screen
  const pt = planMap.latLngToContainerPoint([targetLat, targetLon]);
  card.style.left = Math.min(Math.max(pt.x - 90, 10), planMap.getSize().x - 210) + 'px';
  card.style.top = Math.max(pt.y - 70, 10) + 'px';

  placeDroneMarker();
}

function placeDroneMarker() {
  const t = window.__latestTelemetry;
  if (!planMap) return;
  if (droneMarker) { planMap.removeLayer(droneMarker); droneMarker = null; }
  if (t && t.lat && t.lon && t.mission_id === selectedMissionId) {
    droneMarker = L.marker([t.lat, t.lon], {
      icon: divIcon('<div class="drone-marker"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M12 2L4 9v6l8 7 8-7V9l-8-7z"/></svg></div>', 34),
    }).addTo(planMap).bindPopup('Live position');
  }
}

// ---------------- detail column ----------------

function selectMission(missionId) {
  selectedMissionId = missionId;
  const entry = missionsCache.find(m => m.mission_id === missionId);
  if (!entry) return;
  renderList();
  renderDetail(entry, richDataById[missionId]);
  plotMissionOnMap(entry, richDataById[missionId]);
  document.getElementById('exportBtn').disabled = false;
}

function deselectMission() {
  selectedMissionId = null;
  renderList();
  document.getElementById('detailContent').style.display = 'none';
  document.getElementById('detailEmpty').style.display = 'flex';
  document.getElementById('exportBtn').disabled = true;
  if (planMap) clearMissionLayers();
}

function renderDetail(entry, rich) {
  document.getElementById('detailEmpty').style.display = 'none';
  document.getElementById('detailContent').style.display = 'block';

  const status = statusFor(entry);
  document.getElementById('d-id').textContent = entry.mission_id;
  const statusPill = document.getElementById('d-status');
  statusPill.className = 'status-pill ' + status.cls;
  document.getElementById('d-status-label').textContent = status.label;

  document.getElementById('d-address').textContent = (entry.address_resolved || entry.address || '--').slice(0, 60);
  document.getElementById('d-pollutant').textContent = entry.aqi_param || '--';
  document.getElementById('d-created').textContent = fmtDate(entry.created);
  document.getElementById('d-aqi').textContent = entry.aqi_before !== null && entry.aqi_before !== undefined ? entry.aqi_before : '--';

  document.getElementById('d-drone').textContent = `DRN-01 \u00b7 home base`;

  const r = entry.range_info;
  document.getElementById('d-range-badge').innerHTML = r
    ? `${r.round_trip_miles} mi round trip \u00b7 <span style="color:${r.in_range ? 'var(--green)' : 'var(--red)'}">${r.in_range ? 'in range' : 'out of range'}</span>`
    : 'not calculated';

  const a = rich ? rich.airspace : null;
  document.getElementById('d-airspace-badge').innerHTML = a
    ? (a.warning ? `<span style="color:var(--amber)">${a.checked ? 'Airspace flagged' : 'Airspace unverified'}</span>` : `<span style="color:var(--green)">Airspace clear</span>`)
    : 'Only checked for missions planned this session';

  document.getElementById('d-home-coords').textContent = `Home base \u00b7 ${droneHome.lat.toFixed(4)}, ${droneHome.lon.toFixed(4)}`;
  document.getElementById('d-target-coords').textContent = `Target \u00b7 ${entry.target.lat.toFixed(4)}, ${entry.target.lon.toFixed(4)}`;
  document.getElementById('miniMapLabel').textContent = (entry.address_resolved || entry.address || 'Target').slice(0, 28);

  document.getElementById('downloadLink').href = `/api/plan/${entry.mission_id}`;

  renderMiniMap(entry.target.lat, entry.target.lon);
}

function renderMiniMap(lat, lon) {
  if (miniMap) { miniMap.remove(); miniMap = null; }
  miniMap = L.map('miniMap', { zoomControl: false, attributionControl: false, dragging: false, scrollWheelZoom: false }).setView([lat, lon], 14);
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', { subdomains: 'abcd', maxZoom: 20 }).addTo(miniMap);
  L.marker([lat, lon], { icon: divIcon('<div class="package-marker chosen"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 21s7-6.5 7-11.5A7 7 0 0 0 5 9.5C5 14.5 12 21 12 21z"/></svg></div>', 28) }).addTo(miniMap);
}

// ---------------- plan a new mission ----------------

async function planMission() {
  const location = document.getElementById('address').value;
  const rangeInput = document.getElementById('range').value;
  const btn = document.getElementById('planBtn');
  showError(null);

  if (!location) { showError('Choose Virginia or California first.'); return; }

  btn.disabled = true;
  const originalHtml = btn.innerHTML;
  btn.innerHTML = '<span class="spinner"></span>Planning...';

  try {
    const res = await fetch('/api/plan_mission', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ location, max_round_trip_miles: userRangeOverride !== null ? userRangeOverride : (rangeInput ? parseFloat(rangeInput) : null) }),
    });
    const data = await res.json();
    if (!res.ok) { showError(data.error || 'Something went wrong.'); return; }

    droneHome = data.home;
    richDataById[data.mission_id] = data;
    await refreshHistory();
    selectMission(data.mission_id);
  } catch (e) {
    showError('Request failed: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = originalHtml;
  }
}

function exportSelected() {
  if (!selectedMissionId) return;
  window.open(`/api/plan/${selectedMissionId}`, '_blank');
}

// ---------------- history / telemetry polling ----------------

async function refreshHistory() {
  try {
    const res = await fetch('/api/history');
    missionsCache = await res.json();
    updateHeader();
    renderList();
  } catch (e) { /* ignore */ }
}

async function pollTelemetry() {
  try {
    const res = await fetch('/api/telemetry/latest');
    const data = await res.json();
    window.__latestTelemetry = data && data.received_at ? data : null;
    renderList();
    placeDroneMarker();
  } catch (e) { /* ignore */ }
}

// ---------------- map toolbar buttons ----------------

document.getElementById('recenterBtn')?.addEventListener('click', () => {
  if (!planMap) return;
  if (selectedMissionId) {
    const entry = missionsCache.find(m => m.mission_id === selectedMissionId);
    if (entry) {
      const bounds = L.latLngBounds([[droneHome.lat, droneHome.lon], [entry.target.lat, entry.target.lon]]);
      planMap.fitBounds(bounds.pad(0.35));
      return;
    }
  }
  planMap.setView([droneHome.lat, droneHome.lon], 12);
});

document.getElementById('expandBtn')?.addEventListener('click', () => {
  document.getElementById('planBody').classList.toggle('map-focus');
  setTimeout(() => planMap && planMap.invalidateSize(), 250);
});

// ---------------- init ----------------

updateHeader();
loadSettings();
initPlanMap();
if (homeMarker) homeMarker.setLatLng([droneHome.lat, droneHome.lon]);
refreshHistory();
pollTelemetry();
setInterval(pollTelemetry, 4000);
