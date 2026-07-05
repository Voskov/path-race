/* Path Race — offline-first tap logger.
 * Local state is authoritative and lives in localStorage; the network is a
 * background sync that never blocks the UI. Cold reload restores instantly.
 */
'use strict';

const PREFIX = location.pathname.replace(/\/(stats)?$/, '');   // e.g. /race-xxxx
const API = (p) => `${PREFIX}/api${p}`;
const LS_STATE = 'pr_state_v1';
const LS_CONFIG = 'pr_config_v1';

let CFG = null;          // {config, graph}
let S = null;            // {trip, taps, syncedCount, lastFix}
let syncing = false, syncQueued = false, online = navigator.onLine;
let frozenOptions = null; // options list frozen between reorder moments
let undoTimer = null;

const $ = (id) => document.getElementById(id);
const uuid = () => (crypto.randomUUID ? crypto.randomUUID()
  : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
      const r = Math.random() * 16 | 0; return (c === 'x' ? r : (r & 3 | 8)).toString(16);
    }));

/* ---------- persistence ---------- */
function load() {
  try { CFG = JSON.parse(localStorage.getItem(LS_CONFIG)); } catch (e) { CFG = null; }
  try { S = JSON.parse(localStorage.getItem(LS_STATE)); } catch (e) { S = null; }
  if (!S) S = { trip: null, taps: [], syncedCount: 0, lastFix: null };
}
function save() { localStorage.setItem(LS_STATE, JSON.stringify(S)); }

/* ---------- graph helpers (mirror of backend graph.py) ---------- */
function dirNodes(direction) { return CFG.graph[direction].nodes; }
function terminalOf(direction) { return CFG.graph[direction].terminal; }
function entryOptions() {
  return CFG.graph.entry_options.map(o => {
    const n = dirNodes(o.direction)[o.key];
    return optDict(o.key, n, o.direction);
  });
}
function optDict(key, n, direction) {
  return { key, direction, display: n.display, optional: n.optional,
           hinge: n.hinge, lat: n.lat, lng: n.lng };
}
function currentKey() { return S.taps.length ? S.taps[S.taps.length - 1].checkpoint_key : null; }
function currentOptions() {
  if (!S.trip) return entryOptions();
  const dir = S.trip.direction, ck = currentKey();
  const n = dirNodes(dir)[ck];
  if (!n) return [];
  return n.next.map(k => optDict(k, dirNodes(dir)[k], dir));
}
function isTerminal() {
  return S.trip && currentKey() === terminalOf(S.trip.direction);
}

/* ---------- location ranking (frozen except at defined moments) ---------- */
function locationUsable() {
  const f = S.lastFix, c = CFG.config;
  if (!f) return false;
  if (Date.now() - f.ts > c.locationStaleMs) return false;
  if (f.accuracy != null && f.accuracy > c.locationMaxAccuracyM) return false;
  return true;
}
function dist(aLat, aLng, bLat, bLng) {
  const dLat = aLat - bLat, dLng = (aLng - bLng) * Math.cos(aLat * Math.PI / 180);
  return dLat * dLat + dLng * dLng; // squared, good enough for ordering
}
function rankOptions(opts) {
  // subtractive filter: reorder by plausibility, never remove. Off if no fix.
  if (!locationUsable()) return opts.slice();
  const f = S.lastFix;
  return opts.map((o, i) => ({ o, i }))
    .sort((a, b) => {
      const da = a.o.lat != null ? dist(f.lat, f.lng, a.o.lat, a.o.lng) : Infinity;
      const db = b.o.lat != null ? dist(f.lat, f.lng, b.o.lat, b.o.lng) : Infinity;
      if (da === db) return a.i - b.i;      // stable: keep static order on ties
      return da - db;
    })
    .map(x => x.o);
}
function reorder() {                        // called only at allowed moments
  frozenOptions = rankOptions(currentOptions());
  render();
}
function refreshFix(then) {
  if (!navigator.geolocation) { if (then) then(); return; }
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      S.lastFix = { lat: pos.coords.latitude, lng: pos.coords.longitude,
                    accuracy: pos.coords.accuracy, ts: Date.now() };
      save(); if (then) then();
    },
    () => { if (then) then(); },
    { enableHighAccuracy: true, timeout: 4000, maximumAge: 10000 }
  );
}

/* ---------- committing a tap ---------- */
function commitOption(opt) {
  const now = Date.now();
  const fix = locationUsable() ? S.lastFix : (S.lastFix && Date.now() - S.lastFix.ts < 60000 ? S.lastFix : null);
  if (!S.trip) {
    // first tap fixes direction
    S.trip = { id: uuid(), direction: opt.direction, status: 'active',
               started_at: now, completed_at: null,
               crowding: null, anomalous: false, anomaly_reason: null };
    S.taps = []; S.syncedCount = 0;
  }
  const tap = { id: uuid(), checkpoint_key: opt.key, client_ts: now,
                seq: S.taps.length,
                lat: fix ? fix.lat : null, lng: fix ? fix.lng : null,
                accuracy: fix ? fix.accuracy : null };
  S.taps.push(tap);
  if (opt.key === terminalOf(S.trip.direction)) {
    S.trip.status = 'done'; S.trip.completed_at = now;
  }
  save();
  showUndoToast(opt.display);
  reorder();                // advance to the next node's options immediately
  refreshFix(reorder);      // then re-rank once a fresh GPS fix arrives (may lag)
  scheduleSync();
}

function undoLast() {
  if (!S.taps.length) return;
  S.taps.pop();
  if (S.syncedCount > S.taps.length) S.syncedCount = S.taps.length; // will re-prune on server
  if (S.trip && S.trip.status === 'done') { S.trip.status = 'active'; S.trip.completed_at = null; }
  if (!S.taps.length) { /* undid the very first tap: drop the trip */ S.trip = null; }
  save();
  hideUndoToast();
  reorder();
  scheduleSync();
}

/* ---------- trip-level controls ---------- */
function setCrowding(v) { if (!S.trip) return; S.trip.crowding = v; save(); render(); scheduleSync(); }
function setAnomalous(on, reason) {
  if (!S.trip) return;
  S.trip.anomalous = on; S.trip.anomaly_reason = on ? (reason || null) : null;
  save(); scheduleSync();
}
function discardTrip() {
  if (!S.trip) return;
  S.trip.status = 'discarded'; save();
  const id = S.trip.id;
  patchServer(id, { status: 'discarded' });
  S.trip = null; S.taps = []; S.syncedCount = 0; save();
  reorder();
}
function newTripReset() { S.trip = null; S.taps = []; S.syncedCount = 0; save(); reorder(); }

/* ---------- network sync (idempotent, best-effort) ---------- */
function scheduleSync() { if (syncing) { syncQueued = true; return; } sync(); }
async function sync() {
  if (!S.trip) return;
  syncing = true; setNet('syncing');
  try {
    // 1. ensure trip exists (idempotent by id; first_tap fixes direction)
    if (S.syncedCount === 0 && S.taps.length) {
      const r = await fetch(API('/trips'), {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ id: S.trip.id, first_tap: S.taps[0] })
      });
      if (r.status === 409) { /* an old active trip lingers on server; ignore, single user */ }
    }
    // 2. push all taps (idempotent by tap id)
    if (S.taps.length) {
      await fetch(API(`/trips/${S.trip.id}/taps`), {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ taps: S.taps })
      });
    }
    // 3. reconcile deletions: if server has trailing taps we removed, undo them
    const st = await (await fetch(API('/state'))).json();
    if (st.trip && st.trip.id === S.trip.id) {
      let serverCount = st.taps.length;
      while (serverCount > S.taps.length) {
        await fetch(API(`/trips/${S.trip.id}/undo`), { method: 'POST' });
        serverCount--;
      }
    }
    // 4. push trip-level fields
    await patchServer(S.trip.id, {
      crowding: S.trip.crowding, anomalous: S.trip.anomalous,
      anomaly_reason: S.trip.anomaly_reason,
      status: S.trip.status, completed_at: S.trip.completed_at
    });
    S.syncedCount = S.taps.length; save();
    online = true; setNet('online');
  } catch (e) {
    online = false; setNet('offline');
  } finally {
    syncing = false;
    if (syncQueued) { syncQueued = false; sync(); }
  }
}
async function patchServer(id, fields) {
  const body = {};
  for (const k in fields) if (fields[k] !== null && fields[k] !== undefined) body[k] = fields[k];
  if (!Object.keys(body).length) return;
  try {
    await fetch(API(`/trips/${id}`), {
      method: 'PATCH', headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body)
    });
  } catch (e) { /* stays queued via dirty state */ }
}

/* Background reconcile on startup: adopt server state only if we have nothing local. */
async function reconcileOnStart() {
  try {
    const st = await (await fetch(API('/state'))).json();
    online = true; setNet('online');
    if ((!S.trip || !S.taps.length) && st.trip && st.trip.status === 'active') {
      S.trip = { id: st.trip.id, direction: st.trip.direction, status: st.trip.status,
                 started_at: st.trip.started_at, completed_at: st.trip.completed_at,
                 crowding: st.trip.crowding, anomalous: st.trip.anomalous,
                 anomaly_reason: st.trip.anomaly_reason };
      S.taps = st.taps.map(t => ({ id: t.id, checkpoint_key: t.checkpoint_key,
        client_ts: t.client_ts, seq: t.seq, lat: t.lat, lng: t.lng, accuracy: t.accuracy }));
      S.syncedCount = S.taps.length; save(); reorder();
    } else if (S.trip && S.taps.length) {
      scheduleSync();   // push our local truth
    }
  } catch (e) { online = false; setNet('offline'); }
}

/* ---------- rendering ---------- */
function setNet(cls) {
  const b = $('net-badge'); b.className = 'badge net ' + cls;
  b.textContent = cls === 'online' ? '● synced' : cls === 'syncing' ? '… sync' : '○ offline';
}
function render() {
  const opts = frozenOptions || currentOptions();
  const board = $('options'), moreWrap = $('more-fold'), more = $('more-options');
  board.innerHTML = ''; more.innerHTML = '';

  // direction badge / current label
  const badge = $('direction-badge');
  if (S.trip) { badge.textContent = S.trip.direction; badge.className = 'badge ' + S.trip.direction; }
  else { badge.textContent = 'ready'; badge.className = 'badge'; }
  $('current-label').textContent = S.trip
    ? (isTerminal() ? 'trip complete' : (dirNodes(S.trip.direction)[currentKey()] || {}).display || S.trip.direction)
    : 'choose start';

  if (isTerminal()) { renderDone(); toggleControls(); return; }

  const fold = CFG.config.locationFoldSize;
  const main = opts.slice(0, Math.max(fold, opts.length <= fold ? opts.length : fold));
  const rest = opts.slice(main.length);
  main.forEach(o => board.appendChild(makeSlider(o, () => commitOption(o))));
  if (rest.length) {
    moreWrap.hidden = false;
    rest.forEach(o => more.appendChild(makeSlider(o, () => commitOption(o))));
  } else { moreWrap.hidden = true; }

  toggleControls();
}
function renderDone() {
  const board = $('options'); board.innerHTML = '';
  const div = document.createElement('div');
  div.className = 'done-screen';
  div.innerHTML = `<h2>✓ ${S.trip.direction} trip logged</h2>
    <p>${S.taps.length} checkpoints · total ${fmt(S.trip.completed_at - S.trip.started_at)}</p>`;
  const btn = document.createElement('button');
  btn.className = 'primary-btn'; btn.textContent = 'New trip';
  btn.onclick = newTripReset;
  div.appendChild(btn);
  board.appendChild(div);
  $('more-fold').hidden = true;
}
function fmt(ms) {
  if (ms == null) return '—';
  const s = Math.round(ms / 1000); return `${(s / 60 | 0)}m ${s % 60}s`;
}
function toggleControls() {
  const c = $('trip-controls'), slot = $('discard-slot');
  const activeTrip = S.trip && S.trip.status === 'active';
  const afterBoarding = activeTrip && S.taps.some(t => t.checkpoint_key.includes('doors'));
  c.hidden = !activeTrip;
  if (!activeTrip) return;
  // crowding buttons
  document.querySelectorAll('.crowd-btn').forEach(b => {
    b.classList.toggle('active', String(S.trip.crowding) === b.dataset.crowding);
    b.disabled = !afterBoarding; b.style.opacity = afterBoarding ? 1 : .4;
  });
  $('anomaly-toggle').checked = !!S.trip.anomalous;
  $('anomaly-reason').hidden = !S.trip.anomalous;
  $('anomaly-reason').value = S.trip.anomaly_reason || '';
  // discard slider (slider-guarded, no extra confirm)
  if (!slot.dataset.built) {
    const s = makeSlider({ key: '__discard', display: 'Slide to discard trip',
                           optional: false, hinge: null }, discardTrip, 'danger');
    slot.appendChild(s); slot.dataset.built = '1';
  }
}

/* ---------- slide-to-commit control ---------- */
function makeSlider(opt, onCommit, extraClass) {
  const el = document.createElement('div');
  el.className = 'slider' + (opt.optional ? ' optional' : '')
    + (opt.hinge ? ' hinge-' + opt.hinge : '') + (extraClass ? ' ' + extraClass : '');
  el.innerHTML = `<div class="fill"></div>
    <div class="label">${opt.display}</div>
    ${opt.hinge ? `<div class="tag">${opt.hinge === 'main' ? 'hinge' : 'hinge·2'}</div>` : ''}
    <div class="knob">›</div>`;
  const knob = el.querySelector('.knob'), fill = el.querySelector('.fill');
  let dragging = false, startX = 0, max = 0;
  const COMMIT = 0.82;

  const down = (x) => { dragging = true; startX = x; max = el.clientWidth - knob.clientWidth - 10; };
  const move = (x) => {
    if (!dragging) return;
    let dx = Math.max(0, Math.min(max, x - startX));
    knob.style.left = (5 + dx) + 'px';
    fill.style.width = (dx + knob.clientWidth) + 'px';
    el.classList.toggle('commit-ready', dx / max >= COMMIT);
  };
  const up = () => {
    if (!dragging) return; dragging = false;
    const dx = parseFloat(knob.style.left || 5) - 5;
    if (max > 0 && dx / max >= COMMIT) { reset(); onCommit(); }
    else reset();
  };
  const reset = () => { knob.style.left = '5px'; fill.style.width = '0'; el.classList.remove('commit-ready'); };

  knob.addEventListener('pointerdown', e => { down(e.clientX); knob.setPointerCapture(e.pointerId); });
  knob.addEventListener('pointermove', e => move(e.clientX));
  knob.addEventListener('pointerup', up);
  knob.addEventListener('pointercancel', () => { dragging = false; reset(); });
  return el;
}

/* ---------- undo toast ---------- */
function showUndoToast(text) {
  const t = $('undo-toast'); $('undo-text').textContent = `Logged: ${text}`;
  t.hidden = false;
  clearTimeout(undoTimer);
  undoTimer = setTimeout(hideUndoToast, CFG.config.undoToastMs);
}
function hideUndoToast() { clearTimeout(undoTimer); $('undo-toast').hidden = true; }

/* ---------- wiring ---------- */
function wire() {
  $('undo-btn').onclick = undoLast;
  $('more-toggle').onclick = () => {
    const m = $('more-options'); m.style.display = m.style.display === 'none' ? '' : 'none';
  };
  document.querySelectorAll('.crowd-btn').forEach(b =>
    b.onclick = () => setCrowding(parseInt(b.dataset.crowding, 10)));
  $('anomaly-toggle').onchange = (e) => {
    setAnomalous(e.target.checked, $('anomaly-reason').value);
    $('anomaly-reason').hidden = !e.target.checked;
  };
  $('anomaly-reason').onchange = (e) => setAnomalous(true, e.target.value);

  window.addEventListener('online', () => { online = true; setNet('online'); scheduleSync(); });
  window.addEventListener('offline', () => { online = false; setNet('offline'); });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') { refreshFix(reorder); reconcileOnStart(); }
  });

  // pull-to-refresh reorder moment. #board is column-reverse: scrollTop is 0
  // at its bottom rest position and goes negative when scrolled up.
  const atRest = () => $('board').scrollTop >= 0;
  let touchStartY = 0, pulling = false;
  window.addEventListener('touchstart', e => { if (atRest()) touchStartY = e.touches[0].clientY; });
  window.addEventListener('touchmove', e => {
    const dy = e.touches[0].clientY - touchStartY;
    if (atRest() && dy > 60 && !pulling) {
      pulling = true; $('pull-refresh').style.top = '6px';
      refreshFix(() => { reorder(); $('pull-refresh').style.top = '-40px'; pulling = false; });
    }
  });
}

/* ---------- boot ---------- */
async function boot() {
  load();
  // config: use cached first (instant offline), refresh in background
  if (CFG) { start(); }
  try {
    const c = await (await fetch(API('/config'))).json();
    CFG = c; localStorage.setItem(LS_CONFIG, JSON.stringify(c));
    if (!frozenOptions) start(); else render();
  } catch (e) {
    if (!CFG) { document.body.innerHTML = '<p style="padding:20px">Offline and no cached config yet. Open once online.</p>'; return; }
  }
}
function start() {
  wire();
  setNet(navigator.onLine ? 'online' : 'offline');
  frozenOptions = rankOptions(currentOptions());   // trip start = reorder moment
  render();
  refreshFix(reorder);
  reconcileOnStart();
  if ('serviceWorker' in navigator) navigator.serviceWorker.register(`${PREFIX}/sw.js`).catch(() => {});
}
boot();
