/* illustro service worker.
 * Minimal by design: no app-shell caching (the server is on-LAN and always up).
 * Its two jobs:
 *  1. Make the PWA installable (a fetch listener must exist).
 *  2. Handle the Web Share Target: Android "Share -> illustro" POSTs image files
 *     to /share; we upload each to /api/sync/upload and redirect to the UI.
 */
'use strict';

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method === 'POST' && url.pathname === '/share') {
    event.respondWith(handleShare(event.request));
  }
});

// Tiny IndexedDB kv (the page mirrors the API token here; SW cannot read localStorage)
function idbGet(k) {
  return new Promise((resolve) => {
    const req = indexedDB.open('illustro', 1);
    req.onupgradeneeded = () => req.result.createObjectStore('kv');
    req.onsuccess = () => {
      try {
        const t = req.result.transaction('kv').objectStore('kv').get(k);
        t.onsuccess = () => resolve(t.result || '');
        t.onerror = () => resolve('');
      } catch (e) { resolve(''); }
    };
    req.onerror = () => resolve('');
  });
}

async function handleShare(request) {
  let stored = 0, dup = 0, failed = 0;
  try {
    const form = await request.formData();
    const files = form.getAll('files').filter((f) => f instanceof File && f.size > 0);
    const token = await idbGet('token');
    for (const f of files) {
      const fd = new FormData();
      fd.append('file', f, f.name);
      try {
        const r = await fetch('/api/sync/upload', {
          method: 'POST',
          body: fd,
          headers: token ? { 'X-API-Token': token } : {},
        });
        if (r.ok) {
          (await r.json()).status === 'stored' ? stored++ : dup++;
        } else {
          failed++;
        }
      } catch (e) {
        failed++;
      }
    }
    if (stored > 0) fetch('/api/worker/run', { method: 'POST' }).catch(() => {});
  } catch (e) {
    failed++;
  }
  const url = new URL(`/?shared=${stored}&dup=${dup}&failed=${failed}`, self.location.origin).href;
  return Response.redirect(url, 303);
}
