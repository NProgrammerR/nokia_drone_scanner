'use strict';

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const num = (v, digits = 0, unit = '') => (v == null || Number.isNaN(+v) ? '—' : `${(+v).toFixed(digits)}${unit}`);
const threatClass = (t) => (t >= 70 ? 'crit' : t >= 40 ? 'high' : 'ok');
const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString();

const state = {
  zones: [], sensors: [], drones: new Map(), alerts: [],
  selected: null, sound: true, fitted: false, zoneDraft: null,
};

/* ------------------------------------------------------------------ map */
const map = L.map('map').setView([60.2236, 24.7569], 15);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19, attribution: '&copy; OpenStreetMap contributors',
}).addTo(map);
const layers = {
  zones: L.layerGroup().addTo(map),
  sensors: L.layerGroup().addTo(map),
  drones: L.layerGroup().addTo(map),
};
const droneLayers = new Map();
new ResizeObserver(() => map.invalidateSize()).observe($('#map'));

function renderZones() {
  layers.zones.clearLayers();
  for (const z of state.zones) {
    const critical = z.level === 'critical';
    L.circle([z.lat, z.lon], {
      radius: z.radius_m, color: critical ? '#ff4d5e' : '#ffb020', weight: 2,
      fillOpacity: 0.12, dashArray: critical ? null : '6 6',
    }).bindPopup(`<b>${esc(z.name)}</b><br>${esc(z.level)} · ${z.radius_m} m<br><br>
      <button class="btn small danger" data-del-zone="${esc(z.id)}">Delete zone</button>`)
      .addTo(layers.zones);
  }
  $('#zone-list').innerHTML = state.zones.map((z) => `
    <li class="zone-item"><span class="dot ${esc(z.level)}"></span>
      <span class="grow">${esc(z.name)}</span><span class="muted small">${z.radius_m} m</span>
      <button class="icon-btn" data-del-zone="${esc(z.id)}" title="Delete zone">✕</button></li>`).join('')
    || '<li class="muted small">No zones defined</li>';
}

function renderSensors() {
  layers.sensors.clearLayers();
  for (const s of state.sensors) {
    L.circle([s.lat, s.lon], { radius: s.range_m, color: '#3fa7ff', weight: 1, opacity: 0.35, fillOpacity: 0.03, interactive: false })
      .addTo(layers.sensors);
    L.marker([s.lat, s.lon], { icon: L.divIcon({ className: '', html: '<div class="sensor"></div>', iconSize: [14, 14], iconAnchor: [7, 7] }) })
      .bindTooltip(`<b>${esc(s.name)}</b><br>${esc(s.hw)}<br>range ≈ ${s.range_m} m`)
      .addTo(layers.sensors);
  }
}

function fitOnce() {
  if (state.fitted) return;
  const pts = [...state.sensors, ...state.zones].map((p) => [p.lat, p.lon]);
  if (pts.length) { map.fitBounds(L.latLngBounds(pts).pad(0.25)); state.fitted = true; }
}

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
    el.className = `drone ${threatClass(d.threat)}${d.id === state.selected ? ' sel' : ''}`;
    el.querySelector('.arrow').style.transform = `rotate(${d.heading || 0}deg)`;
    el.querySelector('.lbl').textContent = d.label;
  }
}

function removeStaleLayers() {
  for (const [id, l] of droneLayers) {
    if (state.drones.has(id)) continue;
    Object.values(l).forEach((layer) => layers.drones.removeLayer(layer));
    droneLayers.delete(id);
  }
}

/* --------------------------------------------------------------- panels */
function renderDroneList() {
  const arr = [...state.drones.values()].sort((a, b) => b.threat - a.threat);
  $('#drone-list').innerHTML = arr.map((d) => `
    <li class="drone-item ${d.id === state.selected ? 'active' : ''}" data-drone="${esc(d.id)}">
      <span class="threat ${threatClass(d.threat)}">${d.threat}</span>
      <div class="grow">
        <div class="title">${esc(d.label)}
          ${d.remote_id ? '<span class="chip">Remote ID</span>' : '<span class="chip warn">no ID</span>'}
          ${d.video ? '<span class="chip vid">video</span>' : ''}</div>
        <div class="muted small">${esc(d.model)} · ${num(d.alt, 0, ' m')} · ${num(d.speed, 1, ' m/s')}</div>
      </div>
    </li>`).join('') || '<li class="muted small">No drones detected. Scanning…</li>';
}

function renderDetail() {
  const d = state.drones.get(state.selected);
  if (!d) { $('#detail').innerHTML = '<span class="muted small">Select a drone on the map or in the list.</span>'; return; }
  const cls = threatClass(d.threat);
  const rows = [
    ['ID', d.label], ['Type', d.model], ['Detected via', d.protocol],
    ['Frequency', num(d.freq_mhz, 0, ' MHz')], ['Signal', `${num(d.rssi, 0, ' dBm')} @ ${d.sensor_id || '?'}`],
    ['Position', d.lat == null ? '—' : `${num(d.lat, 5)}, ${num(d.lon, 5)}${d.accuracy_m ? ` (±${d.accuracy_m} m, RF estimate)` : ''}`],
    ['Altitude', num(d.alt, 0, ' m')], ['Speed', num(d.speed, 1, ' m/s')], ['Heading', num(d.heading, 0, '°')],
    ['Operator', d.operator ? `${num(d.operator.lat, 5)}, ${num(d.operator.lon, 5)}` : 'unknown'],
    ['Video', d.video ? (d.video_channel || 'analog') : (d.video_note || '—')],
    ['First seen', fmtTime(d.first_seen)],
  ];
  $('#detail').innerHTML = `
    <div class="threat-bar"><div class="threat-fill ${cls}" style="width:${d.threat}%"></div><span>Threat ${d.threat}/100</span></div>
    <ul class="reasons">${(d.reasons || []).map((r) => `<li>${esc(r)}</li>`).join('')}</ul>
    <dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join('')}</dl>`;
}

function select(id, pan) {
  state.selected = id;
  const d = state.drones.get(id);
  if (pan && d?.lat != null) map.panTo([d.lat, d.lon]);
  renderDroneList();
  renderDetail();
  if (d) updateDroneLayer(d);
  droneLayers.forEach((_, did) => { const x = state.drones.get(did); if (x) updateDroneLayer(x); });
}

function applyDrones(list) {
  state.drones = new Map(list.map((d) => [d.id, d]));
  list.forEach(updateDroneLayer);
  removeStaleLayers();
  renderDroneList();
  renderDetail();
  updateStats();
}

/* --------------------------------------------------------------- alerts */
function renderAlerts() {
  $('#alert-list').innerHTML = state.alerts.map((a) => `
    <li class="alert ${esc(a.severity)}${a.acked ? ' acked' : ''}">
      <div class="alert-head"><span class="sev">${esc(a.severity)}</span><span class="muted small">${fmtTime(a.ts)}</span></div>
      <div>${esc(a.message)}</div>
      <div class="alert-actions">
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
  if (a.severity === 'info') return;
  toast(a);
  if (a.severity === 'critical' || a.severity === 'high') { beep(a.severity); notify(a); }
}

function updateStats() {
  const drones = [...state.drones.values()];
  const open = state.alerts.filter((a) => !a.acked && a.severity !== 'info');
  $('#stat-drones').textContent = drones.length;
  $('#stat-inzone').textContent = drones.filter((d) => d.zones?.length).length;
  $('#stat-alerts').textContent = open.length;
  document.body.classList.toggle('alarm', open.some((a) => a.severity === 'critical'));
}

function toast(a) {
  const el = document.createElement('div');
  el.className = `toast ${a.severity}`;
  el.innerHTML = `<div class="sev">${esc(a.severity)}</div><div>${esc(a.message)}</div>`;
  $('#toasts').prepend(el);
  setTimeout(() => el.remove(), 6000);
}

let audio = null;
document.addEventListener('pointerdown', () => {
  audio ??= new (window.AudioContext || window.webkitAudioContext)();
  audio.resume();
});
function beep(severity) {
  if (!state.sound || !audio) return;
  const n = severity === 'critical' ? 3 : 1;
  for (let i = 0; i < n; i++) {
    const t = audio.currentTime + i * 0.22;
    const osc = audio.createOscillator();
    const gain = audio.createGain();
    osc.frequency.value = severity === 'critical' ? 880 : 660;
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
  new Notification(`Drone alert: ${a.severity}`, { body: a.message, tag: `alert-${a.id}` });
}

/* ------------------------------------------------------------ live data */
function setConn(on) {
  const el = $('#conn');
  el.textContent = on ? '● live' : '● reconnecting…';
  el.className = `conn ${on ? 'on' : 'off'}`;
}

function handle(m) {
  switch (m.type) {
    case 'snapshot': {
      const mode = $('#mode');
      mode.textContent = m.simulation ? 'SIMULATION' : 'LIVE SENSORS';
      mode.className = `tag ${m.simulation ? 'sim' : 'live'}`;
      state.zones = m.zones;
      state.sensors = m.sensors;
      state.alerts = m.alerts.slice().reverse();
      renderZones();
      renderSensors();
      fitOnce();
      applyDrones(m.drones);
      renderAlerts();
      break;
    }
    case 'drones': applyDrones(m.drones); break;
    case 'zones': state.zones = m.zones; renderZones(); break;
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
document.addEventListener('click', (e) => {
  const t = e.target.closest('[data-del-zone],[data-ack],[data-locate],[data-drone]');
  if (!t) return;
  if (t.dataset.delZone) {
    fetch(`/api/zones/${encodeURIComponent(t.dataset.delZone)}`, { method: 'DELETE' });
    map.closePopup();
  } else if (t.dataset.ack) {
    fetch(`/api/alerts/${t.dataset.ack}/ack`, { method: 'POST' });
  } else if (t.dataset.locate) {
    if (state.drones.has(t.dataset.locate)) select(t.dataset.locate, true);
    else toast({ severity: 'info', message: 'That drone is no longer detected.' });
  } else if (t.dataset.drone) {
    select(t.dataset.drone, true);
  }
});

$('#btn-ack-all').addEventListener('click', () => fetch('/api/alerts/ack-all', { method: 'POST' }));

$('#btn-sound').addEventListener('click', (e) => {
  state.sound = !state.sound;
  e.currentTarget.textContent = state.sound ? '🔊 Sound on' : '🔇 Sound off';
});

$('#btn-notify').addEventListener('click', async () => {
  if (!('Notification' in window)) return toast({ severity: 'info', message: 'Notifications not supported in this browser.' });
  const p = await Notification.requestPermission();
  toast({ severity: 'info', message: p === 'granted' ? 'Desktop alerts enabled.' : 'Desktop alerts were blocked.' });
});

// zone placement
const zoneForm = $('#zone-form');
function stopPlacing() {
  state.zoneDraft = null;
  $('#place-hint').hidden = true;
  $('.map-wrap').classList.remove('placing');
}
zoneForm.addEventListener('submit', (e) => {
  e.preventDefault();
  const f = new FormData(zoneForm);
  state.zoneDraft = { name: f.get('name'), radius_m: +f.get('radius'), level: f.get('level') };
  $('#place-hint').hidden = false;
  $('.map-wrap').classList.add('placing');
});
map.on('click', async (e) => {
  if (!state.zoneDraft) return;
  const body = { ...state.zoneDraft, lat: e.latlng.lat, lon: e.latlng.lng };
  stopPlacing();
  const r = await fetch('/api/zones', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (r.ok) zoneForm.reset();
  else toast({ severity: 'warning', message: `Could not add zone: ${(await r.json()).error}` });
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

    // signal quality → static and tearing
    const q = Math.max(0, Math.min(1, (-(d.rssi ?? -90) - 55) / 40));
    drawStatic(0.06 + q * 0.55);
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
    osd('INTERCEPTED · SIMULATED', W / 2, 24, 'center');
  }

  function frame(now) {
    requestAnimationFrame(frame);
    if (source !== 'sim') return;
    const t = target();
    if (!t) {
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
      toast({ severity: 'warning', message: `Could not open capture device: ${err.message}` });
      sel.value = 'sim'; useDevice('sim');
    }
  }

  async function listDevices() {
    if (!navigator.mediaDevices?.getUserMedia) {
      toast({ severity: 'warning', message: 'Camera access needs localhost or HTTPS.' });
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
      toast({ severity: 'warning', message: `No capture device: ${err.message}` });
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
