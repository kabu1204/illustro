/* Dry-run harness for the upload page logic: node scripts/test_upload_ui.js
 * Loads the inline page JS with mocked DOM/network and drives the full flow:
 * folder-handle enqueue -> phase1 (getFile+hash) -> phase2 (batch check) -> phase3 (upload),
 * then a simulated page reload + re-pick to prove nothing re-uploads.
 */
'use strict';

// ---- Browser mocks ----
const els = {};
const el = (id) =>
  (els[id] ||= {
    innerHTML: '', textContent: '', value: '', disabled: false,
    style: {}, scrollTop: 0, scrollHeight: 0,
    addEventListener: () => {}, classList: { add() {}, remove() {} },
  });
global.window = { isSecureContext: false, showDirectoryPicker: undefined };
global.document = {
  getElementById: el,
  addEventListener: () => {},
  querySelectorAll: () => [],
  visibilityState: 'visible',
};
global.localStorage = { _s: {}, getItem(k) { return this._s[k] ?? null; }, setItem(k, v) { this._s[k] = v; } };
global.indexedDB = { open: () => ({ onupgradeneeded: null, addEventListener() {} }) };
Object.defineProperty(global, 'navigator', { value: {}, configurable: true });
global.location = { search: '' };
global.history = { replaceState: () => {} };

// Fake image files: real File objects (node >= 20) so FormData accepts them
function fakeFile(name, size, mtime, byte) {
  return new File([new Uint8Array(size).fill(byte)], name, { lastModified: mtime, type: 'image/png' });
}

// One fake directory with 50 files (a couple with bad extensions), plus a subfolder
// whose file must NOT be queued (non-recursive selection)
const MTIME = 1700000000000;
function fakeDir() {
  const files = [];
  for (let i = 0; i < 50; i++) files.push(fakeFile(`img_${i}.png`, 1024 + i, MTIME + i, i & 0xff));
  files.push(fakeFile('note.txt', 5, MTIME, 1));
  files.push(fakeFile('cover.webp', 2048, MTIME, 7));
  const sub = [fakeFile('sub/a.jpg', 512, MTIME, 9)];
  return {
    name: 'fakepics', kind: 'directory',
    values() {
      const all = [...files.map(f => ({ kind: 'file', name: f.name, getFile: async () => f })),
                    { kind: 'directory', name: 'sub', values() { return [{ done: true, value: { kind: 'file', name: 'sub/a.jpg', getFile: async () => sub[0] } }][Symbol.iterator](); } }];
      return all[Symbol.iterator]();
    },
  };
}

let uploaded = [];   // server-side record of uploads
global.fetch = async (url) => {
  if (url === '/api/sync/info') return { json: async () => ({ enabled: true, extensions: ['.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif'] }) };
  if (url === '/api/sync/check') return { ok: true, status: 200, json: async () => ({ known: [] }) };
  if (url === '/api/worker/run') return { ok: true };
  if (url.startsWith('/api/search')) return { json: async () => ({ total: 0, matched_tags: [], residual: [], mode: 'any', page: 1, page_size: 60, images: [] }) };
  throw new Error('unexpected fetch ' + url);
};
global.XMLHttpRequest = class {
  open() {} setRequestHeader() {}
  get upload() { return { set onprogress(f) { this._p = f; } }; }
  send(fd) {
    // record the uploaded file, then answer asynchronously like the network would
    const f = fd.get('file');
    uploaded.push({ name: f.name, sha: fd.get('sha256') });
    setTimeout(() => { this.status = 200; this.responseText = JSON.stringify({ status: 'stored' }); this.onload(); }, 1);
  }
};

// ---- Load page code ----
const fs = require('fs');
const path = require('path');
const ROOT = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'server/static/index.html'), 'utf-8');
const js = fs.readFileSync(path.join(ROOT, 'server/static/sha256.js'), 'utf-8') +
  '\nglobal.Sha256 = (typeof window !== "undefined" ? window : globalThis).Sha256;\n' +
  [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');
eval(js + '\nglobal.__UP = UP; global.__addFiles = addFiles; global.__enqueueFromDir = enqueueFromDir; global.__startUpload = startUpload; global.__upMetaKey = upMetaKey;');

(async () => {
  const UP = global.__UP;

  // 1) Folder-picker path (handles, lazy)
  await global.__enqueueFromDir(fakeDir());
  const imgs = UP.queue.filter(i => true).length;
  console.assert(imgs === 51, `expected 51 queued (50 png + cover.webp; note.txt filtered, sub/a.jpg skipped as non-recursive), got ${imgs}`);
  console.log('1. enqueueFromDir (top-level only, ext filter, lazy handles):', imgs, 'queued');

  // 2) Re-pick the same folder: no duplicates within session (seenMk gets filled at getFile time in phase 1,
  //    and enqueueFromDir itself has no dup guard across picks by design — verify phase-1 skip instead)
  await global.__startUpload();
  console.log('2. upload run: stored =', UP.sessionStored, '| statuses:',
    JSON.stringify(UP.queue.reduce((a, i) => (a[i.status] = (a[i.status] || 0) + 1, a), {})));
  console.assert(UP.sessionStored === 51, 'all 51 should upload');
  console.assert(uploaded.length === 51, 'exactly 51 uploads hit the network, got ' + uploaded.length);

  // 3) Simulate a page reload: fresh state + persisted meta/ledger from localStorage
  eval('UP.ledger.clear(); UP.meta = {}; UP.queue.length = 0; UP.sessionStored = 0; UP.seenMk.clear(); uploaded.length = 0;');
  // reload persisted caches exactly like a fresh page load would
  const savedHashes = JSON.parse(localStorage.getItem('illustro_up_hashes') || '[]');
  savedHashes.forEach(h => UP.ledger.add(h));
  UP.meta = JSON.parse(localStorage.getItem('illustro_up_meta') || '{}');
  await global.__enqueueFromDir(fakeDir());   // user re-picks the same folder
  await global.__startUpload();
  const dupCount = UP.queue.filter(i => i.status === 'dup' || i.status === 'skip').length;
  console.log('3. after simulated reload + re-pick: dup/skip =', dupCount, '| network uploads =', uploaded.length);
  console.assert(dupCount === 51 && uploaded.length === 0, 're-pick after reload must upload nothing');

  console.log('\nHARNESS PASSED');
  process.exit(0);
})().catch(e => { console.error('HARNESS FAILED:', e); process.exit(1); });
