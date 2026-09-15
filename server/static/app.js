/* =========================================================================
   illustro — frontend logic (zero build, plain script)
   Sections:
     0. Utilities                4. Image viewer
     1. State + hash router      5. Analytics (Collection/Activity/Duplicates)
     2. Search + autocomplete    6. Cluster map ("style map")
     3. Gallery (masonry/scroll) 7. Worker status pill
                                 8. Upload / mobile sync (ported)
                                 9. Keyboard · 10. Boot
   ========================================================================= */
'use strict';

/* ---------- 0. Utilities ---------- */
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const on = (sel, ev, fn) => $(sel).addEventListener(ev, fn);
const esc = (s) => String(s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const fmtNum = (n) => (n ?? 0).toLocaleString('en-US');
const fmtMB = (b) => !b ? '—' : b >= (1 << 30) ? (b / (1 << 30)).toFixed(2) + ' GB' : (b / (1 << 20)).toFixed(1) + ' MB';
const fmtDate = (sec) => !sec ? '—' : new Date(sec * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
const basename = (p) => String(p || '').split(/[\\/]/).pop() || '(unknown)';

function fmtDur(sec) {
  if (!sec || sec <= 0) return '-';
  if (sec < 60) return sec.toFixed(1) + 's';
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return m + 'm ' + s + 's';
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

function toast(msg, type = '') {
  const box = $('#toasts');
  const t = document.createElement('div');
  t.className = 'toast ' + type;
  t.textContent = msg;
  box.appendChild(t);
  setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 300); }, 4200);
  while (box.children.length > 4) box.firstChild.remove();
}

/* Theme: html[data-theme] is set pre-paint in index.html; this syncs icon/meta/charts. */
const ICON_SUN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4.5"/><path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.6 4.6l1.8 1.8M17.6 17.6l1.8 1.8M4.6 19.4l1.8-1.8M17.6 6.4l1.8-1.8"/></svg>';
const ICON_MOON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a8.5 8.5 0 1 0 11 11z"/></svg>';

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem('illustro_theme', theme);
  const btn = $('#themeBtn');
  btn.innerHTML = theme === 'light' ? ICON_SUN : ICON_MOON;
  btn.title = theme === 'light' ? 'Switch to dark theme' : 'Switch to light theme';
  $('#themeMeta').setAttribute('content', theme === 'light' ? '#f4f5f8' : '#0a0b0f');
  // Charts read colors per next render cycle; discarding them forces a rebuild.
  if (roundChart) { roundChart.destroy(); roundChart = null; }
  if (latencyChart) { latencyChart.destroy(); latencyChart = null; }
  if (typeof MAP !== 'undefined' && MAP.data) { MAP.colors = makeClusterColors(); drawMap(); }
}
on('#themeBtn', 'click', () =>
  applyTheme(document.documentElement.dataset.theme === 'light' ? 'dark' : 'light'));

/* ---------- 1. State + hash router ---------- */
const state = {
  q: '', include: [], exclude: [], rating: [], sort: 'new', seed: 0,
  page: 1, total: 0, pageSize: 60,
  view: 'gallery',          // 'gallery' | 'similar'
  similarOf: null,
  items: [],                // current result list (gallery pages or similar results)
  loading: false, end: false, loadedOnce: false,
  reqSeq: 0,
};
const viewer = { list: null, idx: 0, pushed: false, info: null };
const scrollMem = new Map(); // hash -> scrollY, restored on Back
let pendingScroll = null;  // scrollY to apply after a popstate-driven reload

const parseHash = () => {
  const h = location.hash.slice(1) || '/';
  const qi = h.indexOf('?');
  return {
    path: qi < 0 ? h : h.slice(0, qi),
    params: new URLSearchParams(qi < 0 ? '' : h.slice(qi + 1)),
  };
};

const nav = (hash, { replace = false } = {}) => {
  if (location.hash === hash) return;
  if (!replace) scrollMem.set(location.hash || '#/', window.scrollY);
  history[replace ? 'replaceState' : 'pushState'](null, '', hash);
};

function galleryHash(st = state) {
  const p = new URLSearchParams();
  if (st.q) p.set('q', st.q);
  if (st.rating.length) p.set('rating', st.rating.join(','));
  st.include.forEach((t) => p.append('in', t));
  st.exclude.forEach((t) => p.append('ex', t));
  if (st.sort !== 'new') {
    p.set('sort', st.sort);
    if (st.sort === 'random') p.set('seed', String(st.seed));
  }
  const s = p.toString();
  return '#/' + (s ? '?' + s : '');
}

function paramsToState(params) {
  const sort = params.get('sort');
  return {
    q: params.get('q') || '',
    include: params.getAll('in').filter(Boolean),
    exclude: params.getAll('ex').filter(Boolean),
    rating: (params.get('rating') || '').split(',').filter(Boolean),
    sort: ['new', 'old', 'random'].includes(sort) ? sort : 'new',
    seed: Math.max(0, Math.min(parseInt(params.get('seed') || '0', 10) || 0, 2 ** 31 - 1)),
  };
}
const galleryParamsEqual = (a) =>
  a.q === state.q && a.sort === state.sort && a.seed === state.seed &&
  JSON.stringify(a.include) === JSON.stringify(state.include) &&
  JSON.stringify(a.exclude) === JSON.stringify(state.exclude) &&
  JSON.stringify([...a.rating].sort()) === JSON.stringify([...state.rating].sort());

function syncFilterUI() {
  $('#q').value = state.q;
  $('#qClear').hidden = !state.q;
  $$('#ratingSeg .rpill').forEach((b) => b.classList.toggle('on', state.rating.includes(b.dataset.r)));
  $('#sortSel').value = state.sort;
  renderActiveTags();
  const sim = state.view === 'similar';
  $('#filterBar').classList.toggle('sim', sim);
  $('#sortSel').disabled = sim;
}

function renderActiveTags() {
  const box = $('#activeTags');
  box.innerHTML = '';
  if (!state.include.length && !state.exclude.length) return;
  const mk = (label, cls, list) => {
    const s = document.createElement('span');
    s.className = 'chip ' + cls;
    s.innerHTML = `${esc(label)}<span class="x">✕</span>`;
    s.title = 'Remove filter';
    s.onclick = () => {
      state[list] = state[list].filter((x) => x !== label);
      applyGalleryChange({ push: false });
    };
    box.appendChild(s);
  };
  state.include.forEach((t) => mk(t, '', 'include'));
  state.exclude.forEach((t) => mk('−' + t, 'exc', 'exclude'));
}

/* Central route renderer: drives overlays + gallery/similar views from the URL. */
function route() {
  const { path, params } = parseHash();

  const mViewer = path.match(/^\/i\/(\d+)/);
  const mSimilar = path.match(/^\/similar\/(\d+)/);
  const mStats = path.match(/^\/stats(?:\/(activity|dupes))?/);
  const mUpload = path === '/upload';
  const mMap = path === '/map';

  // Overlays
  if (mViewer) {
    openViewerById(parseInt(mViewer[1], 10), { push: false });
    hideStats(); hideUpload();
    return;
  }
  hideViewer();
  if (mStats) { hideUpload(); showStats(mStats[2] || 'collection'); return; }
  hideStats();
  if (mUpload) { openUpload(); return; }
  hideUpload();

  // Content views
  if (mMap) { showMap(); return; }
  hideMap();
  if (mSimilar) {
    enterSimilar(parseInt(mSimilar[1], 10), { push: false });
    return;
  }
  // Gallery
  const next = paramsToState(params);
  if (state.view === 'similar' || !galleryParamsEqual(next) || !state.loadedOnce) {
    Object.assign(state, next, { view: 'gallery', similarOf: null });
    if (state.sort === 'random' && !state.seed) state.seed = (Math.random() * 2 ** 31) | 0;
    pendingScroll = scrollMem.get(location.hash || '#/') ?? null;
    syncFilterUI();
    loadFirstPage();
  } else {
    syncFilterUI();
    const y = scrollMem.get(location.hash || '#/');
    if (y != null) requestAnimationFrame(() => window.scrollTo(0, y));
  }
}

/* ---------- 2. Search + autocomplete ---------- */
const qInput = $('#q');
let acItems = [], acIdx = -1, acOpen = false;

qInput.addEventListener('keydown', (e) => {
  if (acOpen) {
    if (e.key === 'ArrowDown') { e.preventDefault(); acMove(1); return; }
    if (e.key === 'ArrowUp') { e.preventDefault(); acMove(-1); return; }
    if ((e.key === 'Enter' || e.key === 'Tab') && acIdx >= 0) { e.preventDefault(); acAccept(acItems[acIdx]); return; }
    if (e.key === 'Escape') { hideAC(); return; }
  }
  if (e.key === 'Enter') { e.preventDefault(); hideAC(); submitSearch(); }
});
qInput.addEventListener('input', () => { $('#qClear').hidden = !qInput.value; });
qInput.addEventListener('input', debounce(acFetch, 130));
qInput.addEventListener('blur', () => setTimeout(hideAC, 150));
on('#qClear', 'click', () => { qInput.value = ''; submitSearch(); qInput.focus(); });

function tokenUnderCaret() {
  const pos = qInput.selectionStart ?? qInput.value.length;
  const before = qInput.value.slice(0, pos);
  const m = before.match(/(\S*)$/);
  return { token: m[1], start: pos - m[1].length };
}

async function acFetch() {
  const { token } = tokenUnderCaret();
  if (!token || token.length < 1) { hideAC(); return; }
  try {
    const d = await api('/api/autocomplete?q=' + encodeURIComponent(token));
    const cur = tokenUnderCaret().token;
    if (cur !== token) return; // caret moved on
    acItems = d.items || [];
    acIdx = -1;
    if (!acItems.length) { hideAC(); return; }
    renderAC(token);
  } catch (e) { hideAC(); }
}

function renderAC(token) {
  const box = $('#acBox');
  const tl = token.toLowerCase();
  box.innerHTML = '';
  acItems.forEach((it, i) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'acitem' + (i === acIdx ? ' on' : '');
    b.setAttribute('role', 'option');
    const ni = it.name.toLowerCase().indexOf(tl);
    const nameHtml = ni >= 0
      ? esc(it.name.slice(0, ni)) + '<mark>' + esc(it.name.slice(ni, ni + token.length)) + '</mark>' + esc(it.name.slice(ni + token.length))
      : esc(it.name);
    b.innerHTML = `<span>${nameHtml}</span>` +
      (it.zh ? `<span class="zh">${esc(it.zh)}</span>` : '') +
      `<span class="n">${fmtNum(it.count)}</span>`;
    b.addEventListener('mousedown', (e) => { e.preventDefault(); acAccept(it); });
    box.appendChild(b);
  });
  box.hidden = false;
  acOpen = true;
}

function acMove(d) {
  acIdx = (acIdx + d + acItems.length) % acItems.length;
  $$('#acBox .acitem').forEach((el, i) => el.classList.toggle('on', i === acIdx));
  $$('#acBox .acitem')[acIdx]?.scrollIntoView({ block: 'nearest' });
}
function acAccept(it) {
  const { start } = tokenUnderCaret();
  const pos = qInput.selectionStart ?? qInput.value.length;
  qInput.value = qInput.value.slice(0, start) + it.name + ' ' + qInput.value.slice(pos);
  const caret = start + it.name.length + 1;
  qInput.setSelectionRange(caret, caret);
  hideAC();
  qInput.focus();
}
function hideAC() { $('#acBox').hidden = true; acOpen = false; acIdx = -1; }

function submitSearch() {
  state.q = qInput.value.trim();
  state.view = 'gallery';
  state.similarOf = null;
  hideMap();
  if (state.sort === 'random' && !state.seed) state.seed = (Math.random() * 2 ** 31) | 0;
  syncFilterUI();
  nav(galleryHash());
  loadFirstPage();
}

/* Filter/sort/chip changes: replace (not push) to keep history clean. */
function applyGalleryChange({ push = false } = {}) {
  state.view = 'gallery';
  state.similarOf = null;
  hideMap();
  if (state.sort === 'random' && !state.seed) state.seed = (Math.random() * 2 ** 31) | 0;
  syncFilterUI();
  nav(galleryHash(), { replace: !push });
  loadFirstPage();
}

$$('#ratingSeg .rpill').forEach((b) => {
  b.onclick = () => {
    const r = b.dataset.r;
    state.rating = state.rating.includes(r)
      ? state.rating.filter((x) => x !== r)
      : [...state.rating, r];
    applyGalleryChange();
  };
});
on('#sortSel', 'change', () => {
  state.sort = $('#sortSel').value;
  state.seed = state.sort === 'random' ? (Math.random() * 2 ** 31) | 0 : 0; // re-shuffle each pick
  applyGalleryChange();
});

/* ---------- 3. Gallery: masonry + infinite scroll ---------- */
const grid = $('#grid');
const GAP = () => (window.innerWidth <= 640 ? 8 : 14);
const colCount = () => Math.max(2, Math.min(8, Math.floor(grid.clientWidth / (window.innerWidth <= 640 ? 160 : 240)) || 2));
let masonry = { n: 0, cols: [], heights: [] };

function masonryReset(n) {
  masonry.n = n;
  masonry.cols = [];
  masonry.heights = new Array(n).fill(0);
  grid.innerHTML = '';
  for (let i = 0; i < n; i++) {
    const c = document.createElement('div');
    c.className = 'gcol';
    grid.appendChild(c);
    masonry.cols.push(c);
  }
}

function masonryAppend(card, img) {
  const n = masonry.n;
  let shortest = 0;
  for (let i = 1; i < n; i++) if (masonry.heights[i] < masonry.heights[shortest]) shortest = i;
  masonry.cols[shortest].appendChild(card);
  const colW = masonry.cols[shortest].clientWidth || 240;
  const ar = img.width && img.height ? img.height / img.width : 0.75;
  masonry.heights[shortest] += colW * ar + GAP();
}

const SKEL_AR = ['3/4', '4/3', '1/1', '2/3', '3/2', '9/16'];
function showSkeletons() {
  masonryReset(colCount());
  for (let i = 0; i < 12; i++) {
    const d = document.createElement('div');
    d.className = 'skel';
    d.style.setProperty('--ar', SKEL_AR[i % SKEL_AR.length]);
    masonryAppend(d, { width: 3, height: 4 });
  }
}

function renderCard(img, idx) {
  const f = document.createElement('figure');
  f.className = 'card';
  f.tabIndex = 0;
  f.style.setProperty('--ar', img.width && img.height ? `${img.width}/${img.height}` : '3/4');
  if (img.avg_color) f.style.setProperty('--ph', img.avg_color);
  f.dataset.idx = idx;

  const im = document.createElement('img');
  im.loading = 'lazy';
  im.decoding = 'async';
  im.src = '/api/thumb/' + img.id;
  im.alt = (img.tags || []).slice(0, 6).map((t) => t.zh || t.name).join(' ');
  f.appendChild(im);

  if (img.rating && img.rating !== 'general') {
    const r = document.createElement('span');
    r.className = 'rbadge r-' + img.rating[0];
    r.textContent = img.rating[0].toUpperCase();
    r.title = img.rating;
    f.appendChild(r);
  }
  if (img.similarity != null) {
    const s = document.createElement('span');
    s.className = 'simbadge';
    s.textContent = (img.similarity * 100).toFixed(0) + '%';
    s.title = 'cosine similarity';
    f.appendChild(s);
  }

  const cover = document.createElement('figcaption');
  cover.className = 'cover';
  const dims = document.createElement('span');
  dims.className = 'cdims';
  dims.textContent = `${img.width || '?'}×${img.height || '?'}`;
  const tags = document.createElement('span');
  tags.className = 'ctags';
  tags.textContent = (img.tags || []).filter((t) => t.cat !== 4).slice(0, 3).map((t) => t.zh || t.name).join(' ');
  const acts = document.createElement('span');
  acts.className = 'cact';
  acts.innerHTML =
    `<button class="cbtn" data-act="sim" title="Find similar" aria-label="Find similar">
       <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>
     </button>
     <button class="cbtn" data-act="orig" title="Open original" aria-label="Open original">
       <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M14 4h6v6"/><path d="M20 4 10 14"/><path d="M20 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h5"/></svg>
     </button>`;
  acts.querySelector('[data-act="sim"]').onclick = (e) => { e.stopPropagation(); enterSimilar(img.id); };
  acts.querySelector('[data-act="orig"]').onclick = (e) => { e.stopPropagation(); window.open('/api/image/' + img.id); };
  cover.append(dims, tags, acts);
  f.appendChild(cover);

  const open = () => openViewerAt(idx);
  f.onclick = open;
  f.onkeydown = (e) => { if (e.key === 'Enter') open(); };
  return f;
}

function renderItems() {
  masonryReset(colCount());
  state.items.forEach((img, i) => masonryAppend(renderCard(img, i), img));
}

async function fetchPage(page) {
  const p = new URLSearchParams({
    q: state.q,
    include: state.include.join(','),
    exclude: state.exclude.join(','),
    rating: state.rating.join(','),
    page: String(page),
    sort: state.sort,
    seed: String(state.seed),
  });
  return api('/api/search?' + p.toString());
}

function renderMeta(data) {
  const parts = [`<b>${fmtNum(data.total)}</b> images`];
  if (data.mode === 'any') parts.push('OR fallback — no images match all tags');
  if (data.matched_tags?.length) parts.push('matched: ' + esc(data.matched_tags.join(', ')));
  if (data.residual?.length) parts.push(`<span style="color:var(--warn)">unrecognized: ${esc(data.residual.join(', '))}</span>`);
  $('#meta').innerHTML = parts.join(' &nbsp;·&nbsp; ');
}

async function loadFirstPage() {
  const seq = ++state.reqSeq;
  state.loading = true;
  state.page = 1;
  state.end = false;
  state.items = [];
  $('#banner').hidden = true;
  $('#endmark').hidden = true;
  showSkeletons();
  $('#meta').textContent = '';
  try {
    const data = await fetchPage(1);
    if (seq !== state.reqSeq) return; // superseded by a newer search
    state.loading = false;
    state.loadedOnce = true;
    state.total = data.total;
    state.pageSize = data.page_size || 60;
    state.items = data.images;
    renderMeta(data);
    if (!data.images.length) {
      grid.innerHTML = '';
      masonry.n = 0;
      grid.appendChild(emptyState());
      state.end = true;
    } else {
      renderItems();
      updateEnd(data);
    }
    window.scrollTo(0, pendingScroll ?? 0);
    pendingScroll = null;
  } catch (e) {
    if (seq !== state.reqSeq) return;
    state.loading = false;
    grid.innerHTML = '';
    const d = document.createElement('div');
    d.className = 'gstate';
    d.innerHTML = `<b>Search failed.</b><br>${esc(e.message)} — is the server up?`;
    grid.appendChild(d);
    toast('Search failed: ' + e.message, 'err');
    pendingScroll = null;
  }
}

function emptyState() {
  const d = document.createElement('div');
  d.className = 'gstate';
  d.style.flex = '1';
  const hasFilter = state.q || state.include.length || state.exclude.length || state.rating.length;
  d.innerHTML = hasFilter
    ? `<b>No images match.</b><br>Try fewer tags, remove exclusions, or loosen the rating filter.`
    : `<b>Library is empty.</b><br>Add images to your configured directories, then run a build — or upload from a device.`;
  return d;
}

function updateEnd(data) {
  state.end = state.items.length >= data.total;
  const showEnd = state.end && data.total > 0;
  const em = $('#endmark');
  em.hidden = !showEnd;
  if (showEnd) em.textContent = `— All ${fmtNum(data.total)} images loaded —`;
}

async function maybeLoadMore() {
  if (state.loading || state.end || state.view !== 'gallery' || !state.loadedOnce) return;
  state.loading = true;
  $('#loader').hidden = false;
  const seq = state.reqSeq;
  try {
    const data = await fetchPage(state.page + 1);
    if (seq !== state.reqSeq) return;
    state.page += 1;
    const base = state.items.length;
    state.items.push(...data.images); // mutate: an open viewer keeps referencing this list
    data.images.forEach((img, i) => masonryAppend(renderCard(img, base + i), img));
    updateEnd(data);
  } catch (e) {
    toast('Failed to load more: ' + e.message, 'err');
  } finally {
    if (seq === state.reqSeq) state.loading = false;
    $('#loader').hidden = true;
  }
}

const io = new IntersectionObserver(
  (es) => { if (es.some((e) => e.isIntersecting)) maybeLoadMore(); },
  { rootMargin: '1400px' }
);
io.observe($('#sentinel'));

let resizeT;
window.addEventListener('resize', debounce(() => {
  const n = colCount();
  if (n !== masonry.n && state.loadedOnce && state.items.length) renderItems();
}, 180));

/* ---------- Similar view ---------- */
async function enterSimilar(id, { push = true } = {}) {
  // Back from a viewer opened inside the similar view: keep the loaded results.
  if (!push && state.view === 'similar' && state.similarOf === id && state.items.length) {
    syncFilterUI();
    return;
  }
  const seq = ++state.reqSeq;
  if (push) nav('#/similar/' + id);
  hideMap();
  state.view = 'similar';
  state.similarOf = id;
  state.items = [];
  state.end = true;
  syncFilterUI();
  showSkeletons();
  $('#meta').textContent = '';
  const banner = $('#banner');
  banner.hidden = false;
  banner.innerHTML = `
    <img src="/api/thumb/${id}" alt="" onerror="this.style.visibility='hidden'">
    <div class="btxt"><b>Similar to #${id}</b><br><span>vector nearest neighbors</span></div>
    <button id="bannerBack">← Back to results</button>`;
  $('#bannerBack').onclick = () => history.back();
  try {
    const data = await api('/api/similar?id=' + id + '&k=60');
    if (seq !== state.reqSeq) return;
    state.items = data.images;
    banner.querySelector('.btxt span').textContent =
      `${data.images.length} vector nearest neighbors`;
    if (!data.images.length) {
      grid.innerHTML = '';
      const d = document.createElement('div');
      d.className = 'gstate';
      d.style.flex = '1';
      d.innerHTML = '<b>No similar images.</b><br>This image may not be embedded yet — run the worker.';
      grid.appendChild(d);
    } else {
      renderItems();
    }
    window.scrollTo(0, 0);
  } catch (e) {
    if (seq !== state.reqSeq) return;
    grid.innerHTML = '';
    toast('Similar search failed: ' + e.message, 'err');
  }
}

/* ---------- 4. Image viewer ---------- */
const viewerEl = $('#viewer'), vImg = $('#vImg'), vWrap = $('#vWrap'), vSide = $('#vSide');

async function ensureInfo(img) {
  if (img.tags) return img;
  try {
    const full = await api('/api/image_info/' + img.id);
    Object.assign(img, full);
  } catch (e) { /* sidebar shows what it has */ }
  return img;
}

function openViewerAt(idx) { openViewer(state.items, idx, { push: true }); }

function openViewer(list, idx, { push = true } = {}) {
  if (!list.length) return;
  viewer.list = list;
  viewer.idx = Math.max(0, Math.min(idx, list.length - 1));
  if (push) { nav('#/i/' + list[viewer.idx].id); viewer.pushed = true; }
  showViewer();
}

async function openViewerById(id, { push = false } = {}) {
  const idx = state.items.findIndex((x) => x.id === id);
  if (idx >= 0) { openViewer(state.items, idx, { push }); return; }
  try {
    const info = await api('/api/image_info/' + id);
    openViewer([info], 0, { push });
  } catch (e) {
    toast('Image #' + id + ' not found', 'err');
    nav('#/', { replace: true });
    route();
  }
}

async function showViewer() {
  const img = viewer.list[viewer.idx];
  viewerEl.hidden = false;
  document.body.classList.add('lock');
  vWrap.classList.remove('zoomed');
  $('#vPos').hidden = viewer.list.length < 2;
  $('#vPos').textContent = `${viewer.idx + 1} / ${viewer.list.length}`;
  $('#vPrev').disabled = viewer.idx <= 0;
  $('#vNext').disabled = viewer.idx >= viewer.list.length - 1;

  // Blurred thumb placeholder -> full image crossfade
  $('#vLoad').hidden = false;
  vImg.classList.add('loading');
  vImg.src = '/api/thumb/' + img.id;
  const full = new Image();
  full.onload = () => {
    if (viewer.list[viewer.idx] !== img) return; // navigated away meanwhile
    vImg.src = full.src;
    vImg.classList.remove('loading');
    $('#vLoad').hidden = true;
  };
  full.onerror = () => {
    if (viewer.list[viewer.idx] !== img) return;
    vImg.classList.remove('loading');
    $('#vLoad').hidden = true;
    toast('Failed to load image file', 'err');
  };
  full.src = '/api/image/' + img.id;

  renderViewerSidebar(img);
  ensureInfo(img).then(() => {
    if (viewer.list[viewer.idx] === img && !viewerEl.hidden) renderViewerSidebar(img);
  });
  preloadNeighbors();
  $('#vClose').focus({ preventScroll: true });
}

function renderViewerSidebar(img) {
  const chars = (img.tags || []).filter((t) => t.cat === 4);
  const gens = (img.tags || []).filter((t) => t.cat !== 4);
  const tagHtml = (t) =>
    `<span class="tag ${t.cat === 4 ? 'char' : ''}" data-n="${esc(t.name)}">${esc(t.zh || t.name)}<span class="c">${t.conf}</span></span>`;
  const r = img.rating || 'unknown';
  vSide.innerHTML = `
    <div class="fname">${esc(basename(img.path))}</div>
    <div class="fid">#${img.id}</div>
    <div class="vfacts">
      <span class="vrating r-${(r[0] || 'g')}">${esc(r)}</span>
      <span><b>${img.width || '?'}×${img.height || '?'}</b></span>
      <span>${fmtMB(img.bytes)}</span>
      <span title="Saved (file modification time)">${fmtDate(img.mtime || img.added_at)}</span>
      ${img.mtime && img.added_at && Math.abs(img.mtime - img.added_at) > 86400
        ? `<span title="Added to the library">added ${fmtDate(img.added_at)}</span>` : ''}
      ${img.avg_color ? `<span><span class="swatch" style="background:${esc(img.avg_color)}"></span>${esc(img.avg_color)}</span>` : ''}
    </div>
    <div class="vactions">
      <button class="primary" id="vSim">Find similar</button>
      <button id="vOrig">Original</button>
      <button id="vRand" title="Jump to a random image from the whole library">Random</button>
    </div>
    ${chars.length ? `<h3>Characters</h3><div>${chars.map(tagHtml).join('')}</div>` : ''}
    <h3>Tags</h3>
    <div>${gens.length ? gens.map(tagHtml).join('') : '<span style="color:var(--muted);font-size:13px">Not tagged yet</span>'}</div>
    <p class="uhint">Click a tag to filter · Shift+click to exclude</p>`;
  $('#vSim').onclick = () => {
    const id = img.id;
    hideViewer();
    viewer.pushed = false;
    nav('#/similar/' + id, { replace: true }); // Back from similar returns to the gallery
    enterSimilar(id, { push: false });
  };
  $('#vOrig').onclick = () => window.open('/api/image/' + img.id);
  $('#vRand').onclick = () => surprise();
  vSide.querySelectorAll('.tag').forEach((el) => {
    el.onclick = (e) => {
      const name = el.dataset.n;
      if (e.shiftKey) { if (!state.exclude.includes(name)) state.exclude.push(name); }
      else if (!state.include.includes(name)) state.include.push(name);
      closeViewer();
      applyGalleryChange({ push: true });
    };
  });
}

function preloadNeighbors() {
  [viewer.idx - 1, viewer.idx + 1].forEach((i) => {
    const it = viewer.list[i];
    if (it) { const im = new Image(); im.src = '/api/image/' + it.id; }
  });
}

async function stepViewer(d) {
  const next = viewer.idx + d;
  if (next < 0) return;
  // Auto-extend gallery results when paging past the loaded end
  if (next >= viewer.list.length) {
    if (viewer.list === state.items && state.view === 'gallery' && !state.end) {
      await maybeLoadMore();
      if (next >= viewer.list.length) return;
    } else return;
  }
  viewer.idx = next;
  if (viewer.pushed) history.replaceState(null, '', '#/i/' + viewer.list[next].id);
  showViewer();
  if (slide.playing) restartSlideTimer(); // manual nav during playback: reset the clock
}

/* Slideshow: auto-advance via stepViewer (which auto-fetches the next gallery
   page at the list end, so playback runs over the full result set). At the true
   end of a finite list, loops back to the first image. */
const slide = { timer: null, playing: false, iv: 5000 };

function restartSlideTimer() {
  clearInterval(slide.timer);
  slide.timer = setInterval(slideTick, slide.iv);
}
function startSlide() {
  if (slide.playing) return;
  slide.playing = true;
  restartSlideTimer();
  syncSlideUI();
}
function stopSlide() {
  if (!slide.playing) return;
  slide.playing = false;
  clearInterval(slide.timer);
  slide.timer = null;
  syncSlideUI();
}
const toggleSlide = () => (slide.playing ? stopSlide() : startSlide());
function syncSlideUI() {
  $('#vSlidePlay').hidden = slide.playing;
  $('#vSlidePause').hidden = !slide.playing;
  $('#vSlide').classList.toggle('playing', slide.playing);
}
async function slideTick() {
  if (viewerEl.hidden) { stopSlide(); return; }
  if (viewer.idx < viewer.list.length - 1) { await stepViewer(1); return; }
  // At the end: gallery lists may still have pages to fetch (stepViewer handles
  // it); anything else loops back to the start.
  if (viewer.list === state.items && state.view === 'gallery' && !state.end) await stepViewer(1);
  else if (viewer.list.length > 1) { viewer.idx = -1; await stepViewer(1); }
}
on('#vSlide', 'click', toggleSlide);
on('#vSlideInt', 'change', () => {
  slide.iv = +$('#vSlideInt').value;
  if (slide.playing) restartSlideTimer();
});
on('#vFull', 'click', () => {
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  else viewerEl.requestFullscreen?.().catch(() => {});
});

function hideViewer() {
  if (viewerEl.hidden) return;
  viewerEl.hidden = true;
  vImg.src = '';
  stopSlide();
  document.body.classList.remove('lock');
}
function closeViewer() {
  if (viewer.pushed && location.hash.startsWith('#/i/')) { viewer.pushed = false; history.back(); return; }
  hideViewer();
  if (location.hash.startsWith('#/i/')) { nav('#/', { replace: true }); route(); }
}

on('#vClose', 'click', closeViewer);
on('#vPrev', 'click', () => stepViewer(-1));
on('#vNext', 'click', () => stepViewer(1));
vImg.addEventListener('click', () => vWrap.classList.toggle('zoomed'));
viewerEl.addEventListener('click', (e) => { if (e.target === vWrap) closeViewer(); });

// Touch swipe for prev/next (mobile)
let touchX = null;
vWrap.addEventListener('touchstart', (e) => { touchX = e.touches[0].clientX; }, { passive: true });
vWrap.addEventListener('touchend', (e) => {
  if (touchX == null) return;
  const dx = e.changedTouches[0].clientX - touchX;
  touchX = null;
  if (Math.abs(dx) > 64) stepViewer(dx < 0 ? 1 : -1);
}, { passive: true });

/* ---------- 5. Analytics (Collection / Activity / Duplicates) ---------- */
const statsEl = $('#stats');
let statsPushed = false, actTimer = null;
let roundChart = null, latencyChart = null;
const dupesCache = { loaded: false };

function openStats(tab = 'collection') {
  nav('#/stats' + (tab === 'collection' ? '' : '/' + tab));
  statsPushed = true;
  showStats(tab);
}

function showStats(tab) {
  statsEl.hidden = false;
  document.body.classList.add('lock');
  $$('#statsTabs .tab').forEach((b) => b.classList.toggle('on', b.dataset.tab === tab));
  $('#tabCollection').hidden = tab !== 'collection';
  $('#tabActivity').hidden = tab !== 'activity';
  $('#tabDupes').hidden = tab !== 'dupes';
  stopActivityPoll();
  if (tab === 'collection') loadCollection();
  if (tab === 'activity') { pollMonitor(); actTimer = setInterval(pollMonitor, 3000); }
  if (tab === 'dupes') loadDupes();
}

function hideStats() {
  if (statsEl.hidden) return;
  statsEl.hidden = true;
  stopActivityPoll();
  document.body.classList.remove('lock');
}
function stopActivityPoll() { if (actTimer) { clearInterval(actTimer); actTimer = null; } }
function closeStats() {
  if (statsPushed && location.hash.startsWith('#/stats')) { statsPushed = false; history.back(); return; }
  hideStats();
  if (location.hash.startsWith('#/stats')) { nav('#/', { replace: true }); route(); }
}

$$('#statsTabs .tab').forEach((b) => {
  b.onclick = () => {
    const tab = b.dataset.tab;
    history.replaceState(null, '', '#/stats' + (tab === 'collection' ? '' : '/' + tab));
    showStats(tab);
  };
});
on('#statsClose', 'click', closeStats);
statsEl.addEventListener('click', (e) => { if (e.target === statsEl) closeStats(); });

async function loadCollection() {
  const box = $('#tabCollection');
  box.innerHTML = '<div class="spinner"></div>';
  try {
    const s = await api('/api/stats');
    const bars = (arr, max) => arr.map((x) => {
      const label = x.zh || x.name || x.color;
      const sw = x.color ? `<span class="swatch" style="background:${esc(x.color)}"></span>` : '';
      return `<div class="brow"><span title="${esc(x.name || x.color)}">${sw}${esc(label)}</span>` +
        `<span class="hbar" style="width:${Math.max(2, (100 * x.count) / max)}%"></span>` +
        `<span class="n">${fmtNum(x.count)}</span></div>`;
    }).join('');
    const gmax = Math.max(1, ...s.top_general.map((x) => x.count));
    const cmax = Math.max(1, ...s.top_characters.map((x) => x.count));
    const pmax = Math.max(1, ...s.palette.map((x) => x.count));
    const omax = Math.max(1, s.orientation.portrait, s.orientation.landscape, s.orientation.square);
    const orow = (label, n) =>
      `<div class="brow"><span>${label}</span><span class="hbar dim" style="width:${Math.max(2, (100 * n) / omax)}%"></span><span class="n">${fmtNum(n)}</span></div>`;
    box.innerHTML = `
      <div class="kpis">
        <div class="kpi"><b>${fmtNum(s.total)}</b><span>Total images</span></div>
        <div class="kpi"><b>${fmtNum(s.tagged)}</b><span>Tagged</span></div>
        <div class="kpi"><b>${fmtNum(s.embedded)}</b><span>Embedded</span></div>
        <div class="kpi"><b>${fmtNum(s.duplicates.images)}</b><span>Duplicates · ${fmtNum(s.duplicates.groups)} groups</span></div>
      </div>
      <div class="grid2">
        <div><h3>Top 40 tags</h3>${bars(s.top_general, gmax)}</div>
        <div>
          <h3>Top 30 characters</h3>
          ${s.top_characters.length ? bars(s.top_characters, cmax) : '<p class="uhint">No character tags yet</p>'}
          <h3>Dominant colors</h3>${bars(s.palette, pmax)}
          <h3>Orientation</h3>
          ${orow('Portrait', s.orientation.portrait)}
          ${orow('Landscape', s.orientation.landscape)}
          ${orow('Square', s.orientation.square)}
        </div>
      </div>`;
  } catch (e) {
    box.innerHTML = `<div class="err-box">Failed to load stats: ${esc(e.message)}</div>`;
  }
}

async function pollMonitor() {
  try {
    const d = await api('/api/monitor');
    renderWorkerKpis(d);
    renderBenchmark(d.benchmark);
    renderLatencyChart(d.query_latency);
    renderLatencyStats(d.query_latency.stats);
    renderRoundChart(d.worker);
  } catch (e) { /* transient */ }
}

function renderWorkerKpis(d) {
  const w = d.worker;
  const el = $('#workerKpis');
  const ctrl = $('#workerControls');
  if (!w) {
    el.innerHTML = `
      <div class="kpi"><b>—</b><span>Worker disabled (serve-only)</span></div>
      <div class="kpi"><b>${fmtNum(d.db_tagged)}/${fmtNum(d.db_total)}</b><span>Tagged / total</span></div>`;
    ctrl.innerHTML = '<span class="uhint">No worker controls in serve-only mode. Use <code>serve-all</code> to enable background processing.</span>';
    $('#workerError').innerHTML = '';
    return;
  }
  const stateLabel = { running: 'Processing', sleeping: 'Idle', paused: 'Paused', idle: 'Idle', stopped: 'Stopped' }[w.state] || w.state;
  const phaseLabel = { tagging: 'Tagging', scanning: 'Scanning', indexing: 'Indexing', applying_zh: 'Applying ZH', running: 'Running' }[w.phase] || w.phase;
  el.innerHTML = `
    <div class="kpi"><b>${esc(stateLabel)}</b><span>State · ${esc(phaseLabel || '')}</span></div>
    <div class="kpi"><b>${fmtNum(w.processed)}/${fmtNum(w.progress_total)}</b><span>Processed this round</span></div>
    <div class="kpi"><b>${fmtNum(w.throughput_img_min)}</b><span>img / min</span></div>
    <div class="kpi"><b>${fmtDur(w.eta_sec)}</b><span>ETA</span></div>
    <div class="kpi"><b>${fmtDur(w.elapsed_sec)}</b><span>Elapsed</span></div>
    <div class="kpi"><b>${fmtNum(d.db_tagged)}/${fmtNum(d.db_total)}</b><span>Tagged / total</span></div>`;
  $('#workerError').innerHTML = w.last_error ? `<div class="err-box">Last error: ${esc(w.last_error)}</div>` : '';
  ctrl.innerHTML = (w.state === 'paused'
    ? '<button class="primary" data-wa="resume">Resume</button>'
    : '<button data-wa="pause">Pause</button>') +
    ' <button data-wa="run">Process now</button>';
  ctrl.querySelectorAll('[data-wa]').forEach((b) => {
    b.onclick = () => workerAction(b.dataset.wa);
  });
}

async function workerAction(a) {
  try {
    await api('/api/worker/' + a, { method: 'POST' });
    toast('Worker: ' + a, 'ok');
  } catch (e) {
    toast('Worker action failed: ' + e.message, 'err');
  }
  pollMonitor();
  pollWorker();
}

function renderBenchmark(b) {
  const el = $('#benchBox');
  if (!b) { el.textContent = 'Benchmark unavailable (failed at startup).'; return; }
  el.innerHTML = `Startup cosine benchmark: <b>${b.n}×${b.dim}</b> vectors, top-${b.k}, ${b.iterations} iterations —
    mean <b>${b.mean_ms.toFixed(2)}ms</b>, p50 <b>${b.p50_ms.toFixed(2)}ms</b>, p95 <b>${b.p95_ms.toFixed(2)}ms</b>`;
}

function chartColors() {
  const cs = getComputedStyle(document.documentElement);
  return {
    grid: cs.getPropertyValue('--hover').trim() || '#222634',
    tick: cs.getPropertyValue('--muted').trim() || '#8b91a3',
    fg: cs.getPropertyValue('--fg').trim() || '#eef0f6',
  };
}

function renderRoundChart(w) {
  if (!w || !w.rounds || !w.rounds.length || !window.Chart) return;
  const cc = chartColors();
  const cfg = {
    type: 'bar',
    data: {
      labels: w.rounds.map((_, i) => '#' + (i + 1)),
      datasets: [
        { type: 'bar', label: 'Images processed', data: w.rounds.map((r) => r.processed), backgroundColor: 'rgba(138,124,255,0.7)', yAxisID: 'y' },
        { type: 'line', label: 'Duration (s)', data: w.rounds.map((r) => r.duration_sec), borderColor: '#ff7cc3', backgroundColor: 'rgba(255,124,195,0.2)', yAxisID: 'y1', tension: 0.3 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: {
        y: { type: 'linear', position: 'left', title: { display: true, text: 'Images', color: cc.tick }, ticks: { color: cc.tick }, grid: { color: cc.grid } },
        y1: { type: 'linear', position: 'right', title: { display: true, text: 'Seconds', color: cc.tick }, ticks: { color: cc.tick }, grid: { drawOnChartArea: false } },
        x: { ticks: { color: cc.tick }, grid: { color: cc.grid } },
      },
      plugins: { legend: { labels: { color: cc.fg } } },
    },
  };
  if (roundChart) { roundChart.data = cfg.data; roundChart.update('none'); }
  else roundChart = new Chart($('#roundChart'), cfg);
}

function renderLatencyChart(ql) {
  const recent = ql.recent || [];
  if (!recent.length || !window.Chart) return;
  const cc = chartColors();
  const cfg = {
    type: 'line',
    data: {
      labels: recent.map((s) => new Date(s.ts * 1000).toLocaleTimeString()),
      datasets: [
        { label: 'Search (ms)', data: recent.map((s) => (s.endpoint === 'search' ? s.latency_ms : null)), borderColor: '#8a7cff', backgroundColor: 'rgba(138,124,255,0.15)', tension: 0.3, pointRadius: 2, spanGaps: true },
        { label: 'Similar (ms)', data: recent.map((s) => (s.endpoint === 'similar' ? s.latency_ms : null)), borderColor: '#ff7cc3', backgroundColor: 'rgba(255,124,195,0.15)', tension: 0.3, pointRadius: 2, spanGaps: true },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: {
        y: { title: { display: true, text: 'Latency (ms)', color: cc.tick }, ticks: { color: cc.tick }, grid: { color: cc.grid } },
        x: { ticks: { color: cc.tick, maxTicksLimit: 10 }, grid: { color: cc.grid } },
      },
      plugins: { legend: { labels: { color: cc.fg } } },
    },
  };
  if (latencyChart) { latencyChart.data = cfg.data; latencyChart.update('none'); }
  else latencyChart = new Chart($('#latencyChart'), cfg);
}

function renderLatencyStats(stats) {
  const mkTbl = (name, s) => `
    <div><h3>${name}</h3>
    <table class="lat-stats-tbl">
      <tr><th>count</th><th>mean</th><th>p50</th><th>p95</th><th>min</th><th>max</th></tr>
      <tr><td>${s.count}</td><td>${s.mean_ms}ms</td><td>${s.p50_ms}ms</td><td>${s.p95_ms}ms</td><td>${s.min_ms}ms</td><td>${s.max_ms}ms</td></tr>
    </table></div>`;
  $('#latencyStats').innerHTML = mkTbl('Search', stats.search) + mkTbl('Similar', stats.similar);
}

async function loadDupes() {
  const box = $('#tabDupes');
  box.innerHTML = '<div class="spinner"></div>';
  try {
    const d = await api('/api/duplicates');
    const clusters = d.clusters || [];
    if (!clusters.length) {
      box.innerHTML = '<div class="gstate"><b>No near-duplicates found.</b><br>Groups appear here when images share an identical perceptual hash.</div>';
      return;
    }
    box.innerHTML = `<p class="uhint">${clusters.length} near-duplicate groups (identical dHash).
      Read-only preview — click an image to inspect it; clean up files in your file manager.</p>`;
    clusters.forEach((g) => {
      const div = document.createElement('div');
      div.className = 'dupe';
      div.innerHTML = `<div class="dhead"><b>${g.count}× duplicate</b><span class="dhash">dhash ${esc(g.dhash)}</span></div>`;
      const row = document.createElement('div');
      row.className = 'dupe-row';
      g.images.forEach((im, i) => {
        const t = document.createElement('div');
        t.className = 'dthumb';
        t.innerHTML = `<img loading="lazy" decoding="async" src="/api/thumb/${im.id}" alt="#${im.id}">
          <div class="di"><span>#${im.id}</span><span>${fmtMB(im.bytes)}</span></div>`;
        t.title = `${basename(im.path)} · ${im.width}×${im.height}`;
        t.onclick = () => openViewer(g.images, i, { push: true });
        row.appendChild(t);
      });
      div.appendChild(row);
      box.appendChild(div);
    });
  } catch (e) {
    box.innerHTML = `<div class="err-box">Failed to load duplicates: ${esc(e.message)}</div>`;
  }
}

/* ---------- 6. Cluster map ("style map") ----------
   Interactive 2D dot cloud of all image embeddings, colored by spherical-KMeans
   cluster. Custom canvas renderer (Chart.js is too slow at 20k points and can't
   do the hit-testing). Payload from GET /api/clusters; member grids are pure
   client-side filters of that payload — no extra endpoints. */
const MAP = {
  k: 30, data: null, dataK: 0,  // last payload + the k it was requested with
  xs: null, ys: null, ids: null, cs: null, posInCluster: null,
  byCluster: new Map(),        // cluster id -> [{id}, ...] (viewer lists / member grids)
  colors: [],
  view: { s: 1, ox: 0, oy: 0 }, fitS: 1,
  bounds: null, grid: null, gridW: 0, gridH: 0, cell: 1,
  selected: null, hover: -1, seq: 0, drawQueued: false,
};
const mapCanvas = $('#mapCanvas');
const mctx = mapCanvas.getContext('2d');

function makeClusterColors() {
  const light = document.documentElement.dataset.theme === 'light';
  const n = MAP.data ? Math.max(1, MAP.data.k) : 1;
  return Array.from({ length: n }, (_, i) =>
    `hsl(${Math.round((i * 137.508) % 360)} ${light ? 62 : 70}% ${light ? 46 : 62}%)`);
}

function showMap() {
  const el = $('#mapView');
  el.style.top = $('#hdr').offsetHeight + 'px'; // fixed, below the sticky header
  el.hidden = false;
  document.body.classList.add('lock');
  $('#filterBar').hidden = true;
  if (MAP.data && MAP.dataK === MAP.k) { resizeMapCanvas(); renderMapPanel(); queueDraw(); }
  else loadMap(false);
}

function hideMap() {
  const el = $('#mapView');
  if (el.hidden) return;
  el.hidden = true;
  MAP.hover = -1;
  $('#mapTip').hidden = true;
  document.body.classList.remove('lock');
  $('#filterBar').hidden = false;
}

async function loadMap(refresh) {
  const seq = ++MAP.seq;
  $('#mapSpin').hidden = false;
  $('#mapState').hidden = true;
  $('#mapRecompute').disabled = true;
  try {
    const d = await api(`/api/clusters?k=${MAP.k}${refresh ? '&refresh=1' : ''}`);
    if (seq !== MAP.seq) return;
    MAP.data = d;
    MAP.dataK = MAP.k;
    MAP.selected = null;
    MAP.hover = -1;
    if (!d.n) {
      MAP.xs = null;
      $('#mapClusters').innerHTML = '';
      $('#mapInfo').textContent = '';
      $('#mapState').hidden = false;
      $('#mapState').innerHTML = '<b>No vectors yet.</b><br>Tag some images first (run a build / the worker), then reopen the map.';
      return;
    }
    $('#mapState').hidden = true;
    buildMapPoints(d);
    resizeMapCanvas();
    fitMapView();
    renderMapPanel();
    queueDraw();
  } catch (e) {
    if (seq !== MAP.seq) return;
    $('#mapState').hidden = false;
    $('#mapState').innerHTML = `<b>Failed to load clusters.</b><br>${esc(e.message)}`;
    toast('Cluster map failed: ' + e.message, 'err');
  } finally {
    if (seq === MAP.seq) { $('#mapSpin').hidden = true; $('#mapRecompute').disabled = false; }
  }
}

function buildMapPoints(d) {
  const n = d.points.length;
  MAP.xs = new Float32Array(n);
  MAP.ys = new Float32Array(n);
  MAP.ids = new Int32Array(n);
  MAP.cs = new Int32Array(n);
  MAP.posInCluster = new Int32Array(n);
  MAP.byCluster = new Map();
  let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
  d.points.forEach((p, i) => {
    MAP.ids[i] = p[0]; MAP.xs[i] = p[1]; MAP.ys[i] = p[2]; MAP.cs[i] = p[3];
    if (p[1] < minx) minx = p[1]; if (p[1] > maxx) maxx = p[1];
    if (p[2] < miny) miny = p[2]; if (p[2] > maxy) maxy = p[2];
    let list = MAP.byCluster.get(p[3]);
    if (!list) { list = []; MAP.byCluster.set(p[3], list); }
    MAP.posInCluster[i] = list.length;
    list.push({ id: p[0] });
  });
  MAP.bounds = { minx, miny, maxx, maxy };
  MAP.colors = makeClusterColors();
  // Coarse spatial grid for O(1) hover hit-testing
  const bw = Math.max(maxx - minx, 1e-6), bh = Math.max(maxy - miny, 1e-6);
  MAP.cell = Math.max(bw, bh) / 100;
  MAP.gridW = Math.ceil(bw / MAP.cell) + 1;
  MAP.gridH = Math.ceil(bh / MAP.cell) + 1;
  MAP.grid = new Map();
  for (let i = 0; i < n; i++) {
    const gx = Math.floor((MAP.xs[i] - minx) / MAP.cell);
    const gy = Math.floor((MAP.ys[i] - miny) / MAP.cell);
    const key = gx + gy * MAP.gridW;
    let arr = MAP.grid.get(key);
    if (!arr) { arr = []; MAP.grid.set(key, arr); }
    arr.push(i);
  }
}

function resizeMapCanvas() {
  const r = $('#mapMain').getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  mapCanvas.width = Math.max(1, Math.round(r.width * dpr));
  mapCanvas.height = Math.max(1, Math.round(r.height * dpr));
  mctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function fitMapView() {
  const r = $('#mapMain').getBoundingClientRect();
  const b = MAP.bounds;
  const bw = Math.max(b.maxx - b.minx, 1e-6), bh = Math.max(b.maxy - b.miny, 1e-6);
  const s = Math.min(r.width / bw, r.height / bh) * 0.92;
  MAP.view.s = s;
  MAP.fitS = s;
  MAP.view.ox = (r.width - bw * s) / 2 - b.minx * s;
  MAP.view.oy = (r.height - bh * s) / 2 - b.miny * s;
}

function queueDraw() {
  if (MAP.drawQueued) return;
  MAP.drawQueued = true;
  requestAnimationFrame(() => { MAP.drawQueued = false; drawMap(); });
}

function drawMap() {
  if (!MAP.xs || $('#mapView').hidden) return;
  const r = $('#mapMain').getBoundingClientRect();
  const { s, ox, oy } = MAP.view;
  const bg = getComputedStyle(document.documentElement).getPropertyValue('--bg0').trim() || '#0a0b0f';
  mctx.fillStyle = bg;
  mctx.fillRect(0, 0, r.width, r.height);
  const rad = Math.max(1.6, Math.min(5, 1.6 * Math.pow(s / MAP.fitS, 0.25)));
  const sel = MAP.selected;
  for (let pass = 0; pass < (sel == null ? 1 : 2); pass++) {
    // pass 0: dimmed unselected points; pass 1: selected cluster on top
    mctx.globalAlpha = sel == null ? 0.85 : pass === 0 ? 0.07 : 0.95;
    for (let i = 0; i < MAP.xs.length; i++) {
      const c = MAP.cs[i];
      if (sel != null && (pass === 0) === (c === sel)) continue;
      const x = MAP.xs[i] * s + ox, y = MAP.ys[i] * s + oy;
      if (x < -8 || y < -8 || x > r.width + 8 || y > r.height + 8) continue;
      mctx.fillStyle = MAP.colors[c] || '#888';
      mctx.beginPath();
      mctx.arc(x, y, rad, 0, 6.2832);
      mctx.fill();
    }
  }
  mctx.globalAlpha = 1;
  if (MAP.hover >= 0) {
    const i = MAP.hover;
    mctx.strokeStyle = '#fff';
    mctx.lineWidth = 1.5;
    mctx.beginPath();
    mctx.arc(MAP.xs[i] * s + ox, MAP.ys[i] * s + oy, rad + 2.5, 0, 6.2832);
    mctx.stroke();
  }
}

const screenToWorld = (px, py) => ({ x: (px - MAP.view.ox) / MAP.view.s, y: (py - MAP.view.oy) / MAP.view.s });

function nearestPoint(px, py, radiusPx) {
  const w = screenToWorld(px, py);
  const rad = radiusPx / MAP.view.s;
  const b = MAP.bounds;
  const cgx = Math.floor((w.x - b.minx) / MAP.cell), cgy = Math.floor((w.y - b.miny) / MAP.cell);
  const span = Math.ceil(rad / MAP.cell);
  let best = -1, bestD = rad * rad;
  for (let gy = cgy - span; gy <= cgy + span; gy++) {
    for (let gx = cgx - span; gx <= cgx + span; gx++) {
      const arr = MAP.grid.get(gx + gy * MAP.gridW);
      if (!arr) continue;
      for (const i of arr) {
        const dx = MAP.xs[i] - w.x, dy = MAP.ys[i] - w.y;
        const d = dx * dx + dy * dy;
        if (d < bestD) { bestD = d; best = i; }
      }
    }
  }
  return best;
}

function mapEventPos(e) {
  const r = mapCanvas.getBoundingClientRect();
  return { x: e.clientX - r.left, y: e.clientY - r.top };
}

let mapDrag = null;
mapCanvas.addEventListener('pointerdown', (e) => {
  mapCanvas.setPointerCapture(e.pointerId);
  const p = mapEventPos(e);
  mapDrag = { x: p.x, y: p.y, ox: MAP.view.ox, oy: MAP.view.oy, moved: false };
  mapCanvas.classList.add('drag');
});
mapCanvas.addEventListener('pointermove', (e) => {
  const p = mapEventPos(e);
  if (mapDrag) {
    const dx = p.x - mapDrag.x, dy = p.y - mapDrag.y;
    if (Math.abs(dx) + Math.abs(dy) > 4) mapDrag.moved = true;
    MAP.view.ox = mapDrag.ox + dx;
    MAP.view.oy = mapDrag.oy + dy;
    $('#mapTip').hidden = true;
    queueDraw();
    return;
  }
  const i = MAP.xs ? nearestPoint(p.x, p.y, 14) : -1;
  if (i !== MAP.hover) {
    MAP.hover = i;
    queueDraw();
    const tip = $('#mapTip');
    if (i >= 0) {
      tip.innerHTML = `<img src="/api/thumb/${MAP.ids[i]}" alt=""><div class="t">#${MAP.ids[i]}</div>`;
      tip.hidden = false;
    } else tip.hidden = true;
  }
  if (i >= 0) {
    const tip = $('#mapTip');
    const mr = $('#mapMain').getBoundingClientRect();
    tip.style.left = Math.min(p.x + 16, mr.width - 116) + 'px';
    tip.style.top = Math.min(p.y + 16, mr.height - 130) + 'px';
  }
});
mapCanvas.addEventListener('pointerup', (e) => {
  mapCanvas.classList.remove('drag');
  const wasClick = mapDrag && !mapDrag.moved;
  mapDrag = null;
  if (!wasClick || MAP.hover < 0) return;
  const c = MAP.cs[MAP.hover];
  openViewer(MAP.byCluster.get(c), MAP.posInCluster[MAP.hover], { push: true });
});
mapCanvas.addEventListener('pointerleave', () => {
  MAP.hover = -1;
  $('#mapTip').hidden = true;
  queueDraw();
});
mapCanvas.addEventListener('wheel', (e) => {
  if (!MAP.xs) return;
  e.preventDefault();
  const p = mapEventPos(e);
  const w = screenToWorld(p.x, p.y);
  const s = Math.max(MAP.fitS * 0.5, Math.min(MAP.fitS * 100, MAP.view.s * Math.exp(-e.deltaY * 0.0012)));
  MAP.view.s = s;
  MAP.view.ox = p.x - w.x * s;
  MAP.view.oy = p.y - w.y * s;
  queueDraw();
}, { passive: false });
mapCanvas.addEventListener('dblclick', () => { if (MAP.xs) { fitMapView(); queueDraw(); } });

window.addEventListener('resize', debounce(() => {
  if ($('#mapView').hidden) return;
  $('#mapView').style.top = $('#hdr').offsetHeight + 'px';
  resizeMapCanvas();
  queueDraw();
}, 150));

function selectCluster(cid) {
  MAP.selected = MAP.selected === cid ? null : cid;
  queueDraw();
  $$('#mapClusters .mcl').forEach((el) => el.classList.toggle('on', +el.dataset.c === MAP.selected));
}

function renderMapPanel() {
  const d = MAP.data;
  $('#mapInfo').textContent = d
    ? `${fmtNum(d.n)} images · ${d.clusters.length} clusters · computed ${d.computed_at || '—'}`
    : '';
  const box = $('#mapClusters');
  box.innerHTML = '';
  if (!d || !d.n) return;
  d.clusters.forEach((c) => {
    const card = document.createElement('div');
    card.className = 'mcl' + (MAP.selected === c.id ? ' on' : '');
    card.dataset.c = c.id;
    const tags = (c.top_tags || []).slice(0, 5).map((t) =>
      `<b>${esc(t.zh || t.name)}</b> <span class="pc">${Math.round(t.share * 100)}%</span>`).join(' · ');
    card.innerHTML = `
      <div class="mcl-head">
        <span class="cdot" style="background:${MAP.colors[c.id] || '#888'}"></span>
        <b>#${c.id}</b><span class="cnt">${fmtNum(c.count)} images</span>
        ${c.avg_color ? `<span class="sw" style="background:${esc(c.avg_color)}" title="${esc(c.avg_color)}"></span>` : ''}
      </div>
      <div class="mcl-tags">${tags || '<span style="opacity:.6">no distinctive tags</span>'}</div>
      <div class="mcl-reps">${(c.reps || []).slice(0, 4).map((id) =>
        `<img loading="lazy" decoding="async" src="/api/thumb/${id}" alt="#${id}">`).join('')}</div>
      <div class="mcl-detail" hidden></div>`;
    card.querySelector('.mcl-head').onclick = () => {
      selectCluster(c.id);
      const det = card.querySelector('.mcl-detail');
      if (MAP.selected === c.id && det.hidden) { det.hidden = false; fillClusterDetail(det, c); }
      else if (MAP.selected !== c.id) det.hidden = true;
    };
    card.querySelector('.mcl-tags').onclick = card.querySelector('.mcl-reps').onclick =
      () => card.querySelector('.mcl-head').onclick();
    box.appendChild(card);
  });
}

function fillClusterDetail(det, c) {
  const tags = (c.top_tags || []).map((t) =>
    `<b>${esc(t.zh || t.name)}</b> <span class="pc">${Math.round(t.share * 100)}%</span>`).join(' · ');
  const rd = Object.entries(c.rating_dist || {}).map(([r, n]) => `${r[0]}:${fmtNum(n)}`).join('  ');
  det.innerHTML = `
    <div class="mcl-tags">${tags}</div>
    <div class="mcl-rate">${esc(rd)}</div>
    <div class="mcl-grid"></div>`;
  const grid = det.querySelector('.mcl-grid');
  const members = MAP.byCluster.get(c.id) || [];
  let shown = 0;
  const CHUNK = 96;
  const more = document.createElement('button');
  more.className = 'mcl-more';
  const addChunk = () => {
    const end = Math.min(shown + CHUNK, members.length);
    for (let i = shown; i < end; i++) {
      const im = document.createElement('img');
      im.loading = 'lazy';
      im.decoding = 'async';
      im.src = '/api/thumb/' + members[i].id;
      im.alt = '#' + members[i].id;
      im.onclick = (e) => { e.stopPropagation(); openViewer(members, i, { push: true }); };
      grid.appendChild(im);
    }
    shown = end;
    more.textContent = `Load more (${fmtNum(members.length - shown)} remaining)`;
    more.hidden = shown >= members.length;
  };
  more.onclick = (e) => { e.stopPropagation(); addChunk(); };
  det.appendChild(more);
  addChunk();
}

on('#mapK', 'change', () => { MAP.k = +$('#mapK').value; loadMap(false); });
on('#mapRecompute', 'click', () => loadMap(true));

/* ---------- 7. Worker status pill ---------- */
const pill = $('#workerPill');
pill.addEventListener('click', () => openStats('activity'));

async function pollWorker() {
  try { renderPill(await api('/api/worker')); } catch (e) { /* keep previous state */ }
}
function renderPill(w) {
  pill.hidden = false;
  if (!w.enabled) {
    pill.innerHTML = `<span class="wdot stop"></span>Indexed ${fmtNum(w.tagged)}/${fmtNum(w.total)}`;
    return;
  }
  const label = { running: 'Processing', sleeping: 'Idle', paused: 'Paused', idle: 'Idle', stopped: 'Stopped' }[w.state] || w.state;
  const cls = { running: 'run', sleeping: 'sleep', paused: 'pause', stopped: 'stop' }[w.state] || '';
  const ph = { tagging: 'tagging', scanning: 'scan', indexing: 'index', applying_zh: 'zh' }[w.phase] || '';
  const prog = w.state === 'running' && w.progress_total ? ` ${ph} ${w.processed}/${w.progress_total}` : '';
  const warn = w.last_error ? ' ⚠' : '';
  pill.innerHTML = `<span class="wdot ${cls}"></span>${label}${prog} · ${fmtNum(w.remaining)} pending${warn}`;
}

/* ---------- 8. Upload / mobile sync (ported from the legacy single-file UI) ----
   Design notes (unchanged):
   - One file per request; server streams to disk and re-verifies sha256.
   - Dedup at three levels: (1) per-file metadata cache (name|size|mtime -> hash)
     for instant re-picks, (2) localStorage ledger of uploaded hashes,
     (3) batch /api/sync/check against the server before uploading.
   - Folder picking prefers the File System Access API (showDirectoryPicker);
     <input webkitdirectory> is the fallback.
   - crypto.subtle is only available on secure contexts; plain-HTTP LAN falls
     back to the pure-JS streaming Sha256 from sha256.js. */
const UP = {
  queue: [], running: false, stopReq: false, sessionStored: 0,
  info: null, wakeLock: null, ledger: new Set(), meta: {}, renderT: null,
  seenMk: new Set(), lastDir: null, resumeChecked: false,
};
const UP_STATUSES = ['pending', 'hashing', 'ready', 'uploading', 'done', 'dup', 'skip', 'error'];
const UP_LABEL = {
  pending: 'queued', hashing: 'hashing', ready: 'new', uploading: 'uploading',
  done: 'uploaded', dup: 'on server', skip: 'uploaded', error: 'failed',
};

function upLS(key, val) {
  try {
    if (val === undefined) return JSON.parse(localStorage.getItem(key) || 'null');
    localStorage.setItem(key, JSON.stringify(val));
  } catch (e) { return null; }
}
function upLoadPersisted() {
  (upLS('illustro_up_hashes') || []).forEach((h) => UP.ledger.add(h));
  UP.meta = upLS('illustro_up_meta') || {};
}
function upPersist() {
  let arr = [...UP.ledger];
  if (arr.length > 30000) arr = arr.slice(-20000);
  upLS('illustro_up_hashes', arr);
  const keys = Object.keys(UP.meta);
  if (keys.length > 30000) {
    const keep = {};
    keys.slice(-20000).forEach((k) => (keep[k] = UP.meta[k]));
    UP.meta = keep;
  }
  upLS('illustro_up_meta', UP.meta);
}
const upMetaKey = (f) => f.name + '|' + f.size + '|' + f.lastModified;

/* Tiny IndexedDB kv so the service worker (share target) can read the token. */
function upIdb() {
  return new Promise((res) => {
    const o = indexedDB.open('illustro', 1);
    o.onupgradeneeded = () => o.result.createObjectStore('kv');
    o.onsuccess = () => res(o.result);
    o.onerror = () => res(null);
  });
}
async function idbSet(k, v) {
  const db = await upIdb();
  if (!db) return;
  db.transaction('kv', 'readwrite').objectStore('kv').put(v, k);
}
async function idbGet(k) {
  const db = await upIdb();
  if (!db) return null;
  return new Promise((res) => {
    try {
      const t = db.transaction('kv').objectStore('kv').get(k);
      t.onsuccess = () => res(t.result ?? null);
      t.onerror = () => res(null);
    } catch (e) { res(null); }
  });
}
function saveToken(v) {
  localStorage.setItem('illustro_up_token', v);
  idbSet('token', v);
}
function upHeaders(tok, json) {
  const h = {};
  if (tok) h['X-API-Token'] = tok;
  if (json) h['Content-Type'] = 'application/json';
  return h;
}

let uploadPushed = false, shareNote = null;
async function openUpload() {
  const up = $('#upload');
  const firstOpen = up.hidden;
  up.hidden = false;
  document.body.classList.add('lock');
  if (!firstOpen) return;
  $('#upToken').value = localStorage.getItem('illustro_up_token') || '';
  if (!UP.info) {
    try { UP.info = await api('/api/sync/info'); } catch (e) { UP.info = null; }
  }
  const dis = $('#upDisabled');
  if (!UP.info || !UP.info.enabled) {
    dis.hidden = false;
    dis.textContent = 'Sync is disabled on the server. Set sync.enabled=true in config.yaml.';
  } else dis.hidden = true;
  if (!UP.resumeChecked) {
    UP.resumeChecked = true;
    try {
      const h = await idbGet('lastDirHandle');
      if (h) {
        UP.lastDir = h;
        const b = $('#upResumeBtn');
        b.hidden = false;
        b.textContent = '↻ Continue "' + h.name + '"';
      }
    } catch (e) { /* no stored handle */ }
  }
  if (shareNote) { upNote(shareNote); shareNote = null; }
  renderUp();
}
function hideUpload() {
  if ($('#upload').hidden) return;
  $('#upload').hidden = true;
  document.body.classList.remove('lock');
}
function closeUpload() {
  if (uploadPushed && location.hash === '#/upload') { uploadPushed = false; history.back(); return; }
  hideUpload();
  if (location.hash === '#/upload') { nav('#/', { replace: true }); route(); }
}
function upNote(msg) {
  const el = $('#upNote');
  el.textContent = msg;
  el.hidden = false;
}

on('#upPickFolder', 'click', pickFolder);
on('#upPickFiles', 'click', () => $('#upFiles').click());
on('#upResumeBtn', 'click', resumeFolder);
on('#upStartBtn', 'click', startUpload);
on('#upPauseBtn', 'click', pauseUpload);
on('#upRetryBtn', 'click', retryFailed);
on('#upClearBtn', 'click', clearDone);
on('#upToken', 'input', (e) => saveToken(e.target.value));
on('#uploadClose', 'click', closeUpload);
$('#upload').addEventListener('click', (e) => { if (e.target === $('#upload')) closeUpload(); });
$('#upFolder').addEventListener('change', (e) => { addFiles([...e.target.files]); e.target.value = ''; });
$('#upFiles').addEventListener('change', (e) => { addFiles([...e.target.files]); e.target.value = ''; });

function upScanNote(msg) { $('#upSummary').innerHTML = `<span>${esc(msg)}</span>`; }
const upYield = () => new Promise((r) => setTimeout(r, 0));

async function pickFolder() {
  if (window.showDirectoryPicker) {
    try {
      const dir = await window.showDirectoryPicker({ id: 'illustro-uploads', mode: 'read' });
      try { await idbSet('lastDirHandle', dir); } catch (e) {}
      UP.lastDir = dir;
      const b = $('#upResumeBtn');
      b.hidden = false;
      b.textContent = '↻ Continue "' + dir.name + '"';
      await enqueueFromDir(dir);
      return;
    } catch (e) {
      if (e && e.name === 'AbortError') return; // user cancelled the picker
      console.warn('showDirectoryPicker failed, using input fallback', e);
    }
  }
  $('#upFolder').click(); // fallback: webkitdirectory input
}

async function resumeFolder() {
  const h = UP.lastDir;
  if (!h) { upNote('No stored folder — pick one.'); return; }
  let perm = 'granted';
  try {
    if (h.queryPermission && (await h.queryPermission({ mode: 'read' })) !== 'granted') {
      perm = await h.requestPermission({ mode: 'read' }); // button click = user gesture
    }
  } catch (e) { perm = 'denied'; }
  if (perm !== 'granted') { upNote('Folder permission was not granted — pick the folder again.'); return; }
  await enqueueFromDir(h);
}

async function enqueueFromDir(dir) {
  const exts = new Set((UP.info && UP.info.extensions) || ['.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif']);
  const items = [];
  let scanned = 0;
  upScanNote('Scanning folder…');
  // Non-recursive: only files directly inside the picked folder (subfolders are skipped).
  const walk = async (d) => {
    let iter;
    try { iter = d.values(); } catch (e) { return; }
    for (;;) {
      let res;
      try { res = await iter.next(); } catch (e) { return; }
      if (res.done) break;
      const h = res.value;
      scanned++;
      if (h.kind === 'file') {
        const dot = h.name.lastIndexOf('.');
        if (dot > 0 && exts.has(h.name.slice(dot).toLowerCase())) {
          // Lazy: store the handle only — name/size/mtime are fetched at processing time.
          items.push({ f: null, handle: h, name: h.name, size: null, mk: null,
            hash: null, status: 'pending', tries: 0, prog: 0, err: '' });
        }
      }
      if (scanned % 200 === 0) {
        upScanNote(`Scanning folder… ${scanned} entries, ${items.length} images`);
        await upYield();
      }
    }
  };
  await walk(dir);
  for (const it of items) UP.queue.push(it);
  if (scanned) upNote(`Folder scanned: ${items.length} image(s) queued from ${scanned} entries.`);
  renderUp();
}

async function addFiles(files) {
  const exts = new Set((UP.info && UP.info.extensions) || ['.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif']);
  let known = 0;
  const total = files.length;
  for (let idx = 0; idx < total; idx++) {
    const f = files[idx];
    const rel = f.webkitRelativePath || '';
    if (rel && rel.split('/').length > 2) continue; // folder-pick fallback: skip subfolders
    const dot = f.name.lastIndexOf('.');
    if (dot < 0 || !exts.has(f.name.slice(dot).toLowerCase())) continue;
    const mk = upMetaKey(f);
    const rec = UP.meta[mk];
    if (rec && rec.done) { known++; continue; }      // same file seen before: skip without hashing
    if (UP.seenMk.has(mk)) continue;                 // already queued this session
    UP.seenMk.add(mk);
    UP.queue.push({ f, handle: null, name: f.name, size: f.size, mk,
      hash: rec ? rec.h : null, status: 'pending', tries: 0, prog: 0, err: '' });
    // Yield + progress every 200 files so the page stays responsive on huge picks
    if (idx % 200 === 199) {
      upScanNote(`Reading files… ${idx + 1}/${total}` + (known ? ` · ${known} already uploaded` : ''));
      await upYield();
    }
  }
  if (known) upNote(`${known} file(s) already uploaded before — skipped instantly.`);
  renderUp();
}

async function hashFile(f) {
  if (window.isSecureContext && window.crypto && crypto.subtle) {
    const d = await crypto.subtle.digest('SHA-256', await f.arrayBuffer());
    return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, '0')).join('');
  }
  const h = new Sha256();
  const CH = 4 << 20;
  for (let off = 0; off < f.size; off += CH) h.update(new Uint8Array(await f.slice(off, off + CH).arrayBuffer()));
  return h.hex();
}

function scheduleRender() {
  if (UP.renderT) return;
  UP.renderT = setTimeout(() => { UP.renderT = null; renderUp(); }, 250);
}

function renderUp() {
  const n = {};
  UP_STATUSES.forEach((s) => (n[s] = 0));
  let bytesTotal = 0, bytesDone = 0, inflight = 0;
  for (const it of UP.queue) {
    n[it.status]++;
    if (it.status === 'skip') continue;
    bytesTotal += it.size || 0;
    if (it.status === 'done' || it.status === 'dup') bytesDone += it.size || 0;
    if (it.status === 'uploading') inflight += it.prog * (it.size || 0);
  }
  const active = n.pending + n.hashing + n.ready + n.uploading;
  const el = $('#upSummary');
  if (!UP.queue.length) el.textContent = 'No files picked yet — pick a folder to start a bulk backfill.';
  else el.innerHTML = `<span>Picked <b>${UP.queue.length}</b></span>` +
    (n.done ? `<span>uploaded <b>${n.done}</b></span>` : '') +
    (n.dup + n.skip ? `<span>already on server <b>${n.dup + n.skip}</b></span>` : '') +
    (n.error ? `<span style="color:#ffb0b0">failed <b>${n.error}</b></span>` : '') +
    (active ? `<span>to go <b>${active}</b></span>` : '') +
    (bytesTotal ? `<span>${fmtMB(bytesDone + inflight)} / ${fmtMB(bytesTotal)}</span>` : '');
  const pct = bytesTotal ? Math.min(100, Math.round((100 * (bytesDone + inflight)) / bytesTotal)) : 0;
  $('#upBar').style.width = pct + '%';

  const list = $('#upList');
  const rows = UP.queue.filter((i) => i.status !== 'skip').slice(-60);
  list.innerHTML = rows.map((i) => {
    const st = i.status === 'uploading' ? `uploading ${Math.round(i.prog * 100)}%`
      : i.status === 'error' && i.err ? `${UP_LABEL[i.status]}: ${i.err}` : UP_LABEL[i.status];
    return `<div class="ufile"><span class="nm" title="${esc(i.name)}">${esc(i.name)}</span>` +
      `<span class="sz">${fmtMB(i.size)}</span><span class="st ${i.status}">${esc(st)}</span></div>`;
  }).join('');
  if (UP.running) list.scrollTop = list.scrollHeight;

  $('#upStartBtn').hidden = UP.running;
  $('#upStartBtn').disabled = !UP.queue.some((i) => i.status === 'pending' || i.status === 'ready' || i.status === 'error');
  $('#upPauseBtn').hidden = !UP.running;
  $('#upRetryBtn').hidden = UP.running || !n.error;
}

function finishItem(it, status) {
  it.status = status;
  if (status === 'done' || status === 'dup') {
    UP.ledger.add(it.hash);
    UP.meta[it.mk] = { h: it.hash, done: 1 };
    if (status === 'done') UP.sessionStored++;
  }
  scheduleRender();
}

function xhrUpload(it, tok) {
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    fd.append('file', it.f, it.name);
    if (it.hash) fd.append('sha256', it.hash);
    if (it.f && it.f.lastModified) fd.append('mtime_ms', String(it.f.lastModified));
    const x = new XMLHttpRequest();
    x.open('POST', '/api/sync/upload');
    if (tok) x.setRequestHeader('X-API-Token', tok);
    x.timeout = 300000;
    x.upload.onprogress = (e) => { if (e.lengthComputable) { it.prog = e.loaded / e.total; scheduleRender(); } };
    x.onload = () => resolve(x);
    x.onerror = () => reject(new Error('network error'));
    x.ontimeout = () => reject(new Error('timeout'));
    x.send(fd);
  });
}

async function uploadItem(it, tok) {
  it.status = 'uploading';
  it.prog = 0;
  scheduleRender();
  for (;;) {
    try {
      const x = await xhrUpload(it, tok);
      if (x.status === 401) { it.status = 'error'; it.err = 'unauthorized (check token)'; UP.stopReq = true; renderUp(); return; }
      if (x.status === 413) { it.status = 'error'; it.err = 'too large for server limit'; renderUp(); return; }
      if (x.status === 415) { it.status = 'error'; it.err = 'unsupported type'; renderUp(); return; }
      if (x.status === 422) { it.status = 'error'; it.err = 'hash mismatch (retry from scratch)'; renderUp(); return; }
      if (x.status >= 500) throw new Error('server error ' + x.status);
      if (x.status !== 200) throw new Error('HTTP ' + x.status);
      const j = JSON.parse(x.responseText);
      finishItem(it, j.status === 'stored' ? 'done' : 'dup');
      return;
    } catch (e) {
      it.tries++;
      if (it.tries >= 3) { it.status = 'error'; it.err = e.message || String(e); renderUp(); return; }
      await new Promise((r) => setTimeout(r, 1000 * Math.pow(2, it.tries)));
    }
  }
}

async function acquireWake() {
  try { if ('wakeLock' in navigator) UP.wakeLock = await navigator.wakeLock.request('screen'); } catch (e) {}
}
function releaseWake() { if (UP.wakeLock) { UP.wakeLock.release().catch(() => {}); UP.wakeLock = null; } }
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && UP.running) acquireWake();
});

async function startUpload() {
  if (UP.running) return;
  if (!UP.queue.some((i) => i.status === 'pending' || i.status === 'ready')) { renderUp(); return; }
  UP.running = true;
  UP.stopReq = false;
  renderUp();
  await acquireWake();
  const tok = localStorage.getItem('illustro_up_token') || '';
  try {
    // Phase 1: hash everything pending (sequential: CPU/IO bound).
    for (const it of UP.queue) {
      if (UP.stopReq) break;
      if (it.status !== 'pending') continue;
      if (!it.f && it.handle) {
        try { it.f = await it.handle.getFile(); }
        catch (e) { it.status = 'error'; it.err = 'file unavailable'; continue; }
        it.size = it.f.size;
        it.mk = upMetaKey(it.f);
        UP.seenMk.add(it.mk);
        const rec = UP.meta[it.mk];
        if (rec && rec.done) { it.status = 'skip'; scheduleRender(); continue; } // uploaded in a previous session
        if (rec && rec.h) it.hash = rec.h;
      }
      if (!it.hash) {
        it.status = 'hashing';
        scheduleRender();
        try { it.hash = await hashFile(it.f); }
        catch (e) { it.status = 'error'; it.err = 'hash failed'; continue; }
      }
      if (UP.ledger.has(it.hash)) { finishItem(it, 'dup'); continue; }
      it.status = 'ready';
      scheduleRender();
    }
    renderUp();
    // Phase 2: batch server check -> mark everything the server already has
    const ready = UP.queue.filter((i) => i.status === 'ready' && i.hash);
    for (let i = 0; i < ready.length && !UP.stopReq; i += 500) {
      const batch = ready.slice(i, i + 500);
      try {
        const r = await fetch('/api/sync/check', {
          method: 'POST', headers: upHeaders(tok, true),
          body: JSON.stringify({ hashes: batch.map((b) => b.hash) }),
        });
        if (r.status === 401) {
          batch.forEach((b) => { b.status = 'error'; b.err = 'unauthorized (check token)'; });
          UP.stopReq = true;
          break;
        }
        if (r.ok) {
          const known = new Set((await r.json()).known || []);
          batch.forEach((b) => { if (known.has(b.hash)) finishItem(b, 'dup'); });
        }
      } catch (e) { /* transient: the upload phase will surface real errors */ }
    }
    // Phase 3: upload with N parallel workers
    const list = UP.queue.filter((i) => i.status === 'ready');
    let idx = 0;
    const next = () => (!UP.stopReq && idx < list.length ? list[idx++] : null);
    await Promise.all([0, 1, 2].map(async () => {
      for (let it; (it = next());) await uploadItem(it, tok);
    }));
  } finally {
    UP.running = false;
    releaseWake();
    upPersist();
    if (UP.sessionStored > 0) {
      upNote(`Done — ${UP.sessionStored} file(s) stored. Start processing from Analytics → Activity when you're ready.`);
    }
    UP.sessionStored = 0;
    renderUp();
  }
}

function pauseUpload() { UP.stopReq = true; }
function retryFailed() {
  for (const it of UP.queue) if (it.status === 'error') {
    it.status = it.hash ? 'ready' : 'pending';
    it.tries = 0;
    it.prog = 0;
    it.err = '';
  }
  if (!UP.running) startUpload();
}
function clearDone() {
  UP.queue = UP.queue.filter((i) => ['done', 'dup', 'skip', 'error'].indexOf(i.status) < 0);
  renderUp();
}
upLoadPersisted();

/* ---------- 9. Keyboard ---------- */
document.addEventListener('keydown', (e) => {
  const tag = (document.activeElement && document.activeElement.tagName) || '';
  const inField = /^(INPUT|TEXTAREA|SELECT)$/.test(tag);
  if (e.key === '/' && !inField) {
    e.preventDefault();
    qInput.focus();
    qInput.select();
    return;
  }
  if (e.key === 'Escape') {
    if (acOpen) { hideAC(); return; }
    if (!viewerEl.hidden) { closeViewer(); return; }
    if (!statsEl.hidden) { closeStats(); return; }
    if (!$('#upload').hidden) { closeUpload(); return; }
    if (!$('#mapView').hidden) { nav('#/'); route(); return; }
    if (inField) document.activeElement.blur();
    return;
  }
  if (e.key === ' ' && !viewerEl.hidden && !inField && tag !== 'BUTTON') {
    e.preventDefault();
    toggleSlide();
    return;
  }
  if (!viewerEl.hidden && !inField) {
    if (e.key === 'ArrowLeft') { e.preventDefault(); stepViewer(-1); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); stepViewer(1); }
  }
});

/* ---------- 10. Boot ---------- */
applyTheme(document.documentElement.dataset.theme); // sync icon/meta (theme set pre-paint inline)
on('#btnStats', 'click', () => openStats('collection'));
on('#btnUpload', 'click', () => { nav('#/upload'); uploadPushed = true; openUpload(); });
on('#btnMap', 'click', () => { nav('#/map'); showMap(); });
on('#btnLucky', 'click', () => surprise());

/* Surprise me: reset to a fresh shuffle of the whole library and open the first
   image full-screen; arrows / slideshow then walk the shuffled order. */
async function surprise() {
  hideMap();
  Object.assign(state, {
    q: '', include: [], exclude: [], rating: [],
    sort: 'random', seed: (Math.random() * 2 ** 31) | 0,
    view: 'gallery', similarOf: null,
  });
  syncFilterUI();
  nav(galleryHash());
  await loadFirstPage();
  if (state.items.length) openViewerAt(0);
}

// PWA share target: the service worker uploads shared files, then redirects to /?shared=...
(function handleShareLanding() {
  const sp = new URLSearchParams(location.search);
  if (!sp.has('shared')) return;
  shareNote = `Shared: ${sp.get('shared')} stored · ${sp.get('dup') || 0} already on server · ${sp.get('failed') || 0} failed`;
  history.replaceState(null, '', '#/upload');
})();

window.addEventListener('popstate', route);
route(); // initial view from URL (defaults to gallery)
pollWorker();
setInterval(pollWorker, 5000);

if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
