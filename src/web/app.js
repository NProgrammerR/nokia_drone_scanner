'use strict';

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const num = (v, digits = 0, unit = '') => (v == null || Number.isNaN(+v) ? '—' : `${(+v).toFixed(digits)}${unit}`);
const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString();
const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' }) : '∞');
const RANK = { green: 0, yellow: 1, red: 2 };
const SITE_ICON = { port: '⚓', airport: '✈', nuclear: '☢', energy: '⚡', vip: '★', other: '◆' };

const state = {
  sites: [], sensors: [], permits: [], drones: new Map(), alerts: [],
  selected: null, activeSite: null, sound: true, siteDraft: null,
};

/* ------------------------------------------------------------------ map */
const map = L.map('map').setView([60.4330, 22.1030], 14);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19, attribution: '&copy; OpenStreetMap contributors',
}).addTo(map);
const layers = {
  sites: L.layerGroup().addTo(map),
  sensors: L.layerGroup().addTo(map),
  drones: L.layerGroup().addTo(map),
};
const droneLayers = new Map();
new ResizeObserver(() => map.invalidateSize()).observe($('#map'));

function flyToSite(id) {
  const s = state.sites.find((x) => x.id === id);
  if (!s) return;
  state.activeSite = id;
  map.flyToBounds(L.latLng(s.lat, s.lon).toBounds(s.perimeter_m * 2.6), { duration: 0.8 });
  renderSiteTabs();
}

function nearestSiteId(lat, lon) {
  let best = null; let bd = Infinity;
  for (const s of state.sites) {
    const d = map.distance([lat, lon], [s.lat, s.lon]) / s.perimeter_m;
    if (d < bd) { bd = d; best = s.id; }
  }
  return best;
}

function renderConfig() {
  layers.sites.clearLayers();
  for (const s of state.sites) {
    const popup = `<b>${esc(s.name)}</b><br>${esc(s.type)} · perimeter ${s.perimeter_m} m · close ${s.close_m} m
      <br>Alerts to: ${esc(s.authority)}<br>Network: ${esc(s.network)}<br><br>
      <button class="btn small danger" data-del-site="${esc(s.id)}">Delete site</button>`;
    L.circle([s.lat, s.lon], { radius: s.perimeter_m, color: '#3fa7ff', weight: 2, dashArray: '8 6', fillOpacity: 0.04 })
      .bindPopup(popup).addTo(layers.sites);
    L.circle([s.lat, s.lon], { radius: s.close_m, color: '#ff4d5e', weight: 2, fillOpacity: 0.12 })
      .bindPopup(popup).addTo(layers.sites);
    L.marker([s.lat, s.lon], { icon: L.divIcon({ className: '', html: `<div class="site-marker">${SITE_ICON[s.type] || '◆'}</div>`, iconSize: [24, 24], iconAnchor: [12, 12] }) })
      .bindTooltip(`Protection target: ${esc(s.name)}`).addTo(layers.sites);
  }
  layers.sensors.clearLayers();
  for (const n of state.sensors) {
    L.circle([n.lat, n.lon], { radius: n.range_m, color: '#3fa7ff', weight: 1, opacity: 0.2, fillOpacity: 0.02, interactive: false })
      .addTo(layers.sensors);
    L.marker([n.lat, n.lon], { icon: L.divIcon({ className: '', html: '<div class="sensor"></div>', iconSize: [14, 14], iconAnchor: [7, 7] }) })
      .bindTooltip(`<b>${esc(n.name)}</b><br>${esc(n.hw)}<br>Backhaul: ${esc(n.backhaul)}<br>range ≈ ${n.range_m} m`)
      .addTo(layers.sensors);
  }
  $('#site-list').innerHTML = state.sites.map((s) => `
    <li class="site-item"><span>${SITE_ICON[s.type] || '◆'}</span>
      <div class="grow" data-site="${esc(s.id)}"><div>${esc(s.name)}</div>
        <div class="muted small">${s.perimeter_m} / ${s.close_m} m · → ${esc(s.authority)} · ${esc(s.network)}</div></div>
      <button class="icon-btn" data-del-site="${esc(s.id)}" title="Delete site">✕</button></li>`).join('')
    || '<li class="muted small">No protected sites</li>';
  const siteName = (id) => state.sites.find((s) => s.id === id)?.name || 'any site';
  const now = Date.now();
  $('#permit-list').innerHTML = state.permits.map((p) => {
    const expired = (p.valid_to && new Date(p.valid_to) < now) || (p.valid_from && new Date(p.valid_from) > now);
    return `<li class="permit-item"><span class="dot ${expired ? 'expired' : ''}" style="margin-top:5px"></span>
      <div class="grow"><div>${esc(p.purpose)} <span class="muted small">${esc(p.id)}</span></div>
        <div class="muted small">${esc(p.remote_id)} · ${esc(siteName(p.site_id))}<br>${esc(p.holder)} · ${fmtDate(p.valid_from)} → ${fmtDate(p.valid_to)}${expired ? ' · <b>not valid now</b>' : ''}</div></div>
      <button class="icon-btn" data-del-permit="${esc(p.id)}" title="Revoke permit">✕</button></li>`;
  }).join('') || '<li class="muted small">No permits</li>';
  const sel = $('#permit-site');
  const keep = sel.value;
  sel.innerHTML = state.sites.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('');
  if (keep) sel.value = keep;
  renderSiteTabs();
}

function renderSiteTabs() {
  const counts = {};
  for (const d of state.drones.values()) {
    const sid = d.site?.id;
    if (!sid || d.site.region === 'outside') continue;
    counts[sid] ??= { red: 0, yellow: 0 };
    if (d.level === 'red' || d.level === 'yellow') counts[sid][d.level]++;
  }
  $('#site-tabs').innerHTML = state.sites.map((s) => {
    const c = counts[s.id] || { red: 0, yellow: 0 };
    return `<button class="site-tab ${s.id === state.activeSite ? 'active' : ''}" data-site="${esc(s.id)}">
      ${SITE_ICON[s.type] || '◆'} ${esc(s.name.replace(/ \(demo\)$/, ''))}
      ${c.red ? `<span class="n red">${c.red}</span>` : ''}${c.yellow ? `<span class="n yellow">${c.yellow}</span>` : ''}</button>`;
  }).join('');
}

/* --------------------------------------------------------------- drones */
function droneHtml() {
  return '<div class="drone"><svg class="arrow" viewBox="0 0 24 24"><path d="M12 2 L20 21 L12 16 L4 21 Z"/></svg><span class="lbl"></span></div>';
}

function updateDroneLayer(d) {
  if (d.lat == null || d.lon == null) return;
  const pos = [d.lat, d.lon];
  let l = droneLayers.get(d.id);
  if (!l) {
    l = {
      trail: L.polyline([], { color: '#9ad1ff', weight: 2, opacity: 0.55, interactive: false }).addTo(layers.drones),
      acc: L.circle(pos, { radius: 1, color: '#c58cff', weight: 1, dashArray: '4 4', fillOpacity: 0.08, interactive: false }).addTo(layers.drones),
      opLine: L.polyline([], { color: '#7cf', weight: 1, dashArray: '3 6', opacity: 0.7, interactive: false }),
      op: L.circleMarker(pos, { radius: 5, color: '#fff', fillColor: '#7cf', fillOpacity: 1, weight: 1 }).bindTooltip('Pilot / operator (from Remote ID)'),
      marker: L.marker(pos, { icon: L.divIcon({ className: '', html: droneHtml(), iconSize: [28, 28], iconAnchor: [14, 14] }), zIndexOffset: 1000 })
        .on('click', () => select(d.id, false)).addTo(layers.drones),
    };
    droneLayers.set(d.id, l);
  }
  l.marker.setLatLng(pos);
  l.trail.setLatLngs(d.trail || []);
  l.acc.setLatLng(pos).setRadius(d.accuracy_m || 1).setStyle({ opacity: d.accuracy_m ? 1 : 0, fillOpacity: d.accuracy_m ? 0.08 : 0 });
  if (d.operator) {
    l.op.setLatLng([d.operator.lat, d.operator.lon]).addTo(layers.drones);
    l.opLine.setLatLngs([[d.operator.lat, d.operator.lon], pos]).addTo(layers.drones);
  }
  const el = l.marker.getElement()?.querySelector('.drone');
  if (el) {
    el.className = `drone ${d.level || 'green'}${d.id === state.selected ? ' sel' : ''}`;
    el.querySelector('.arrow').style.transform = `rotate(${d.heading || 0}deg)`;
    el.querySelector('.lbl').textContent = d.authorized ? `${d.label} ✓` : d.label;
  }
}

function removeStaleLayers() {
  for (const [id, l] of droneLayers) {
    if (state.drones.has(id)) continue;
    Object.values(l).forEach((layer) => layers.drones.removeLayer(layer));
    droneLayers.delete(id);
  }
}

function sortedDrones() {
  return [...state.drones.values()].sort((a, b) => (RANK[b.level] - RANK[a.level]) || (b.threat - a.threat));
}

function renderDroneList() {
  const arr = sortedDrones();
  $('#drone-list').innerHTML = arr.map((d) => `
    <li class="drone-item ${d.id === state.selected ? 'active' : ''}" data-drone="${esc(d.id)}">
      <span class="threat ${d.level}">${d.threat}<small>${d.level.toUpperCase()}</small></span>
      <div class="grow">
        <div class="title">${esc(d.label)}
          ${d.authorized ? '<span class="chip">permit ✓</span>' : d.remote_id ? '<span class="chip">Remote ID</span>' : '<span class="chip warn">no ID</span>'}
          ${d.cellular ? '<span class="chip vid">5G</span>' : ''}
          ${d.video ? '<span class="chip vid">video</span>' : ''}</div>
        <div class="muted small">${esc(d.site ? d.site.name.replace(/ \(demo\)$/, '') : '—')} · ${num(d.site?.distance_m, 0, ' m')} · ${num(d.alt, 0, ' m alt')}</div>
      </div>
    </li>`).join('') || '<li class="muted small">No drones detected. Scanning…</li>';
}

function renderDetail() {
  const d = state.drones.get(state.selected);
  if (!d) { $('#detail').innerHTML = '<span class="muted small">Select a drone on the map or in the list to see how the risk agent scored it.</span>'; return; }
  const rows = [
    ['ID', d.label], ['Type', d.model], ['Detected via', d.via || d.sensor_id], ['Signal', d.protocol],
    ['Position', d.lat == null ? '—' : `${num(d.lat, 5)}, ${num(d.lon, 5)}${d.accuracy_m ? ` (±${d.accuracy_m} m)` : ''}`],
    ['Alt / speed', `${num(d.alt, 0, ' m')} · ${num(d.speed, 1, ' m/s')} · ${num(d.heading, 0, '°')}`],
    ['Pilot', d.operator ? `${num(d.operator.lat, 5)}, ${num(d.operator.lon, 5)}` : 'unknown'],
    ['Video', d.video ? (d.video_channel || 'analog') : (d.video_note || '—')],
    ['First seen', fmtTime(d.first_seen)],
  ];
  $('#detail').innerHTML = `
    <div class="threat-bar"><div class="threat-fill ${d.level}" style="width:${Math.max(d.threat, 4)}%"></div>
      <span>${d.level.toUpperCase()} · risk ${d.threat}/100</span></div>
    <p class="summary">${esc(d.summary)}</p>
    <table class="factors">${(d.factors || []).map((f) => `
      <tr><td>${esc(f.signal)}</td><td>${esc(f.detail)}</td><td class="pts ${f.points > 0 ? 'pos' : ''}">${f.points > 0 ? `+${f.points}` : '0'}</td></tr>`).join('')}
    </table>
    <dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join('')}</dl>`;
}

function select(id, fly) {
  state.selected = id;
  const d = state.drones.get(id);
  if (fly && d?.lat != null) {
    const sid = d.site?.id || nearestSiteId(d.lat, d.lon);
    if (sid && sid !== state.activeSite) { state.activeSite = sid; renderSiteTabs(); }
    map.flyTo([d.lat, d.lon], Math.max(map.getZoom(), 14), { duration: 0.8 });
  }
  renderDroneList();
  renderDetail();
  state.drones.forEach(updateDroneLayer);
}

function applyDrones(list) {
  state.drones = new Map(list.map((d) => [d.id, d]));
  list.forEach(updateDroneLayer);
  removeStaleLayers();
  renderDroneList();
  renderDetail();
  renderSiteTabs();
  updateStats();
}

/* --------------------------------------------------------------- alerts */
function renderAlerts() {
  $('#alert-list').innerHTML = state.alerts.map((a) => `
    <li class="alert ${esc(a.level)}${a.acked ? ' acked' : ''}">
      <div class="alert-head"><span class="sev">${esc(a.level)}</span><span class="muted small">${fmtTime(a.ts)}</span></div>
      <div>${esc(a.message)}</div>
      <div class="alert-actions">
        ${a.escalated_to ? `<span class="escalated">→ ${esc(a.escalated_to)} notified</span>` : ''}
        ${a.drone_id ? `<button class="btn small" data-locate="${esc(a.drone_id)}">Locate</button>` : ''}
        ${a.acked ? '<span class="muted small">acknowledged</span>' : `<button class="btn small" data-ack="${a.id}">Acknowledge</button>`}
      </div>
    </li>`).join('') || '<li class="muted small">No alerts</li>';
  updateStats();
}

function addAlert(a) {
  state.alerts.unshift(a);
  state.alerts.length = Math.min(state.alerts.length, 150);
  renderAlerts();
  if (a.level === 'info') return;
  toast(a);
  if (a.level === 'red' || a.level === 'yellow') beep(a.level);
  if (a.level === 'red') notify(a);
}

function updateStats() {
  const drones = [...state.drones.values()];
  const open = state.alerts.filter((a) => !a.acked && (a.level === 'red' || a.level === 'yellow'));
  $('#stat-drones').textContent = drones.length;
  $('#stat-red').textContent = drones.filter((d) => d.level === 'red').length;
  $('#stat-yellow').textContent = drones.filter((d) => d.level === 'yellow').length;
  $('#stat-alerts').textContent = open.length;
  document.body.classList.toggle('alarm', open.some((a) => a.level === 'red'));
}

function toast(a) {
  const el = document.createElement('div');
  el.className = `toast ${a.level}`;
  el.innerHTML = `<div class="sev">${esc(a.level)}${a.escalated_to ? ` · ${esc(a.escalated_to)} notified` : ''}</div><div>${esc(a.message)}</div>`;
  $('#toasts').prepend(el);
  while ($('#toasts').children.length > 4) $('#toasts').lastChild.remove();
  setTimeout(() => el.remove(), 6000);
}

let audio = null;
document.addEventListener('pointerdown', () => {
  audio ??= new (window.AudioContext || window.webkitAudioContext)();
  audio.resume();
});
function beep(level) {
  if (!state.sound || !audio) return;
  const n = level === 'red' ? 3 : 1;
  for (let i = 0; i < n; i++) {
    const t = audio.currentTime + i * 0.22;
    const osc = audio.createOscillator();
    const gain = audio.createGain();
    osc.frequency.value = level === 'red' ? 880 : 620;
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(0.25, t + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.18);
    osc.connect(gain).connect(audio.destination);
    osc.start(t);
    osc.stop(t + 0.2);
  }
}

function notify(a) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  new Notification(`RED drone alert${a.site_name ? `: ${a.site_name}` : ''}`, { body: a.message, tag: `alert-${a.id}` });
}

/* ------------------------------------------------------------ live data */
function setConn(on) {
  const el = $('#conn');
  el.textContent = on ? '● live' : '● reconnecting…';
  el.className = `conn ${on ? 'on' : 'off'}`;
}

function applyConfig(m) {
  state.sites = m.sites;
  state.sensors = m.sensors;
  state.permits = m.permits;
  if (!state.sites.some((s) => s.id === state.activeSite)) {
    state.activeSite = null;
    if (state.sites[0]) flyToSite(state.sites[0].id);
  }
  renderConfig();
}

function handle(m) {
  switch (m.type) {
    case 'snapshot': {
      const mode = $('#mode');
      mode.textContent = m.simulation ? 'SIMULATION' : 'LIVE SENSORS';
      mode.className = `tag ${m.simulation ? 'sim' : 'live'}`;
      applyConfig(m);
      state.alerts = m.alerts.slice().reverse();
      applyDrones(m.drones);
      renderAlerts();
      break;
    }
    case 'drones': applyDrones(m.drones); break;
    case 'config': applyConfig(m); break;
    case 'alert': addAlert(m.alert); break;
    case 'alert_update': {
      const i = state.alerts.findIndex((a) => a.id === m.alert.id);
      if (i >= 0) state.alerts[i] = m.alert;
      renderAlerts();
      break;
    }
    default: break;
  }
}

const es = new EventSource('/api/stream');
es.onopen = () => setConn(true);
es.onerror = () => setConn(false);
es.onmessage = (ev) => handle(JSON.parse(ev.data));

/* --------------------------------------------------------------- events */
async function api(method, url, body) {
  const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: body && JSON.stringify(body) });
  if (!r.ok) toast({ level: 'yellow', message: `Request failed: ${(await r.json().catch(() => ({}))).error || r.status}` });
  return r.ok;
}

document.addEventListener('click', (e) => {
  const t = e.target.closest('[data-del-site],[data-del-permit],[data-ack],[data-locate],[data-drone],[data-site]');
  if (!t) return;
  const ds = t.dataset;
  if (ds.delSite) {
    if (confirm('Delete this protected site?')) api('DELETE', `/api/sites/${encodeURIComponent(ds.delSite)}`);
    map.closePopup();
  } else if (ds.delPermit) {
    api('DELETE', `/api/permits/${encodeURIComponent(ds.delPermit)}`);
  } else if (ds.ack) {
    api('POST', `/api/alerts/${ds.ack}/ack`);
  } else if (ds.locate) {
    if (state.drones.has(ds.locate)) select(ds.locate, true);
    else toast({ level: 'info', message: 'That drone is no longer detected.' });
  } else if (ds.drone) {
    select(ds.drone, true);
  } else if (ds.site) {
    flyToSite(ds.site);
  }
});

$('#btn-ack-all').addEventListener('click', () => api('POST', '/api/alerts/ack-all'));

$('#btn-sound').addEventListener('click', (e) => {
  state.sound = !state.sound;
  e.currentTarget.textContent = state.sound ? '🔊 Sound on' : '🔇 Sound off';
});

$('#btn-notify').addEventListener('click', async () => {
  if (!('Notification' in window)) return toast({ level: 'info', message: 'Notifications not supported in this browser.' });
  const p = await Notification.requestPermission();
  toast({ level: 'info', message: p === 'granted' ? 'Desktop alerts enabled for RED threats.' : 'Desktop alerts were blocked.' });
});

// permits (stand-in for the port's permit system)
$('#permit-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = new FormData(e.currentTarget);
  const ok = await api('POST', '/api/permits', {
    remote_id: f.get('remote_id'), purpose: f.get('purpose') || 'Authorized flight', site_id: f.get('site_id'),
    holder: 'Added from console', hours: +f.get('hours'),
  });
  if (ok) e.currentTarget.reset();
});

// site placement
const siteForm = $('#site-form');
function stopPlacing() {
  state.siteDraft = null;
  $('#place-hint').hidden = true;
  $('.map-wrap').classList.remove('placing');
}
siteForm.addEventListener('submit', (e) => {
  e.preventDefault();
  const f = new FormData(siteForm);
  if (+f.get('close') >= +f.get('perimeter')) {
    toast({ level: 'yellow', message: '"Close to target" must be smaller than the perimeter.' });
    return;
  }
  state.siteDraft = {
    name: f.get('name'), type: f.get('type'), authority: f.get('authority'),
    perimeter_m: +f.get('perimeter'), close_m: +f.get('close'), network: 'Private 5G',
  };
  $('#place-hint').hidden = false;
  $('.map-wrap').classList.add('placing');
});
map.on('click', async (e) => {
  if (!state.siteDraft) return;
  const body = { ...state.siteDraft, lat: e.latlng.lat, lon: e.latlng.lng };
  stopPlacing();
  if (await api('POST', '/api/sites', body)) siteForm.reset();
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') stopPlacing(); });

/* ---------------------------------------------------------------- video */
const video = (() => {
  const canvas = $('#video-canvas');
  const ctx = canvas.getContext('2d');
  const vid = $('#video-el');
  const sel = $('#video-source');
  const status = $('#video-status');
  const W = canvas.width; const H = canvas.height;
  const noise = document.createElement('canvas');
  noise.width = 160; noise.height = 90;
  const nctx = noise.getContext('2d');
  const nimg = nctx.createImageData(noise.width, noise.height);
  const scenery = Array.from({ length: 70 }, () => ({
    ang: Math.random() * 360, w: 2 + Math.random() * 7, h: 8 + Math.random() * 55,
    shade: 30 + Math.random() * 40, tree: Math.random() < 0.5,
  }));
  let source = 'sim';
  let stream = null;
  let yaw = null; let roll = 0; let lastId = null;

  // Recorded drone footage from src/web/media/ (listed by /api/media). When clips exist,
  // they replace the drawn scene; static + OSD are still overlaid so it reads as an intercept.
  const clip = document.createElement('video');
  clip.muted = true; clip.loop = true; clip.playsInline = true; clip.preload = 'auto';
  let clips = []; let clipFor = null;
  fetch('/api/media').then((r) => (r.ok ? r.json() : { clips: [] }))
    .then((j) => { clips = j.clips || []; }).catch(() => {});

  function useClip(d) {
    if (!clips.length) return false;
    if (clipFor !== d.id) { // same drone always gets the same clip
      clipFor = d.id;
      let h = 0;
      for (const c of d.id) h = (h * 31 + c.charCodeAt(0)) >>> 0;
      clip.src = clips[h % clips.length];
    }
    if (clip.paused) clip.play().catch(() => {});
    return clip.readyState >= 2;
  }

  function drawClip() {
    const vw = clip.videoWidth || W; const vh = clip.videoHeight || H;
    const scale = Math.max(W / vw, H / vh) * 1.12; // cover-fit, slight zoom hides rotated corners
    ctx.save();
    ctx.fillStyle = '#000'; ctx.fillRect(0, 0, W, H);
    ctx.translate(W / 2, H / 2);
    ctx.rotate((roll * 0.25 * Math.PI) / 180); // small wobble from the drone's turns
    ctx.filter = 'saturate(.75) contrast(1.1)'; // analog look
    ctx.drawImage(clip, (-vw * scale) / 2, (-vh * scale) / 2, vw * scale, vh * scale);
    ctx.restore();
  }

  function target() {
    const sel = state.drones.get(state.selected);
    if (sel?.video) return { d: sel, auto: false };
    const any = [...state.drones.values()].find((d) => d.video);
    return any ? { d: any, auto: true } : null;
  }

  function drawStatic(alpha) {
    const px = nimg.data;
    for (let i = 0; i < px.length; i += 4) {
      const v = Math.random() * 255;
      px[i] = px[i + 1] = px[i + 2] = v; px[i + 3] = 255;
    }
    nctx.putImageData(nimg, 0, 0);
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(noise, 0, 0, W, H);
    ctx.restore();
  }

  function osd(text, x, y, align = 'left') {
    ctx.font = 'bold 15px ui-monospace, monospace';
    ctx.textAlign = align;
    ctx.fillStyle = '#000';
    ctx.fillText(text, x + 1, y + 1);
    ctx.fillStyle = '#fff';
    ctx.fillText(text, x, y);
  }

  function drawScene(d, now) {
    const hdg = d.heading || 0;
    if (yaw == null || lastId !== d.id) { yaw = hdg; roll = 0; lastId = d.id; }
    const dh = ((hdg - yaw + 540) % 360) - 180;
    yaw = (yaw + dh * 0.06 + 360) % 360;
    roll += (Math.max(-35, Math.min(35, dh * 1.5)) - roll) * 0.06;
    const recorded = useClip(d);
    if (recorded) drawClip();
    else drawSynthetic(d, now);

    // signal quality → static and tearing
    const q = Math.max(0, Math.min(1, (-(d.rssi ?? -90) - 55) / 40));
    drawStatic(0.06 + q * (recorded ? 0.4 : 0.55));
    if (Math.random() < q * 0.4) {
      const y = Math.random() * H;
      ctx.drawImage(canvas, 0, y, W, 12, (Math.random() - 0.5) * 40, y, W, 12);
    }

    // OSD (what the pilot sees, intercepted)
    osd(`CH ${d.video_channel || '?'}`, 14, 24);
    osd(`RSSI ${num(d.rssi, 0)} dBm`, W - 14, 24, 'right');
    osd(`ALT ${num(d.alt, 0)}m  SPD ${num(d.speed, 1)}m/s`, 14, H - 16);
    osd(new Date().toLocaleTimeString(), W - 14, H - 16, 'right');
    if (Math.floor(now / 500) % 2) { ctx.fillStyle = '#ff3b3b'; ctx.beginPath(); ctx.arc(W - 24, 44, 5, 0, 7); ctx.fill(); osd('REC', W - 34, 49, 'right'); }
    ctx.strokeStyle = 'rgba(255,255,255,.8)'; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(W / 2 - 18, H / 2); ctx.lineTo(W / 2 - 6, H / 2); ctx.moveTo(W / 2 + 6, H / 2); ctx.lineTo(W / 2 + 18, H / 2);
    ctx.moveTo(W / 2, H / 2 - 12); ctx.lineTo(W / 2, H / 2 - 4); ctx.stroke();
    osd(recorded ? 'INTERCEPTED · RECORDED CLIP' : 'INTERCEPTED · SIMULATED', W / 2, 24, 'center');
  }

  function drawSynthetic(d, now) {
    const horizon = -50 + Math.sin(now / 1700) * 10;
    const pxPerDeg = W / 100;

    ctx.save();
    ctx.translate(W / 2, H / 2);
    ctx.rotate((roll * Math.PI) / 180);
    const sky = ctx.createLinearGradient(0, -H, 0, horizon);
    sky.addColorStop(0, '#5d86b3'); sky.addColorStop(1, '#b9cbdc');
    ctx.fillStyle = sky; ctx.fillRect(-W, -H, W * 2, H + horizon);
    const ground = ctx.createLinearGradient(0, horizon, 0, H);
    ground.addColorStop(0, '#6c7a5e'); ground.addColorStop(1, '#3d4a33');
    ctx.fillStyle = ground; ctx.fillRect(-W, horizon, W * 2, H * 1.5);
    for (const s of scenery) {
      const rel = ((s.ang - yaw + 540) % 360) - 180;
      if (Math.abs(rel) > 70) continue;
      const x = rel * pxPerDeg;
      ctx.fillStyle = `rgb(${s.shade},${s.shade + (s.tree ? 18 : 4)},${s.shade})`;
      if (s.tree) {
        ctx.beginPath(); ctx.moveTo(x, horizon - s.h); ctx.lineTo(x - s.w * 2, horizon); ctx.lineTo(x + s.w * 2, horizon); ctx.fill();
      } else {
        ctx.fillRect(x, horizon - s.h, s.w * pxPerDeg, s.h);
      }
    }
    const phase = ((now / 1000) * (d.speed || 5) * 0.08) % 1;
    ctx.strokeStyle = 'rgba(255,255,255,.08)';
    for (let i = 1; i < 16; i++) {
      const y = horizon + 140 / (i - phase + 0.4);
      ctx.beginPath(); ctx.moveTo(-W, y); ctx.lineTo(W, y); ctx.stroke();
    }
    ctx.restore();
  }

  function frame(now) {
    requestAnimationFrame(frame);
    if (source !== 'sim') { if (!clip.paused) clip.pause(); return; }
    const t = target();
    if (!t) {
      if (!clip.paused) clip.pause();
      clipFor = null;
      ctx.fillStyle = '#000'; ctx.fillRect(0, 0, W, H);
      drawStatic(0.85);
      osd('NO SIGNAL · SCANNING 5.8 GHz', W / 2, H / 2, 'center');
      status.textContent = 'Scanning 5.8 GHz band. No analog video found.';
      return;
    }
    drawScene(t.d, now);
    status.textContent = `${t.auto ? 'Auto-locked' : 'Tuned'}: ${t.d.label} · ${t.d.video_channel}`;
  }
  requestAnimationFrame(frame);

  async function useDevice(id) {
    stream?.getTracks().forEach((tr) => tr.stop());
    stream = null;
    if (id === 'sim') {
      source = 'sim'; vid.hidden = true; canvas.hidden = false;
      return;
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video: { deviceId: { exact: id } } });
      vid.srcObject = stream;
      source = id; vid.hidden = false; canvas.hidden = true;
      status.textContent = `Live capture: ${sel.selectedOptions[0]?.textContent}`;
    } catch (err) {
      toast({ level: 'yellow', message: `Could not open capture device: ${err.message}` });
      sel.value = 'sim'; useDevice('sim');
    }
  }

  async function listDevices() {
    if (!navigator.mediaDevices?.getUserMedia) {
      toast({ level: 'yellow', message: 'Camera access needs localhost or HTTPS.' });
      return;
    }
    try {
      const tmp = await navigator.mediaDevices.getUserMedia({ video: true }); // unlocks device labels
      tmp.getTracks().forEach((tr) => tr.stop());
      const cams = (await navigator.mediaDevices.enumerateDevices()).filter((x) => x.kind === 'videoinput');
      sel.innerHTML = '<option value="sim">Intercepted (simulated)</option>'
        + cams.map((c, i) => `<option value="${esc(c.deviceId)}">${esc(c.label || `Camera ${i + 1}`)}</option>`).join('');
      if (cams.length) { sel.value = cams[0].deviceId; useDevice(cams[0].deviceId); }
    } catch (err) {
      toast({ level: 'yellow', message: `No capture device: ${err.message}` });
    }
  }

  function snapshot() {
    let src = canvas;
    if (source !== 'sim') {
      src = document.createElement('canvas');
      src.width = vid.videoWidth || W; src.height = vid.videoHeight || H;
      src.getContext('2d').drawImage(vid, 0, 0, src.width, src.height);
    }
    const a = document.createElement('a');
    a.href = src.toDataURL('image/png');
    a.download = `drone-snapshot-${new Date().toISOString().replace(/[:.]/g, '-')}.png`;
    a.click();
  }

  sel.addEventListener('change', () => useDevice(sel.value));
  $('#btn-capture').addEventListener('click', listDevices);
  $('#btn-snap').addEventListener('click', snapshot);
})();
