// Caches the app so it opens offline; data files are network-first with a saved copy as backup.
const V = "la-fishing-v1", ASSETS = ["./", "index.html", "manifest.webmanifest", "icon-192.png", "icon-512.png"];
self.addEventListener("install", e => { e.waitUntil(caches.open(V).then(c => c.addAll(ASSETS))); self.skipWaiting(); });
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(k => Promise.all(k.filter(x => x !== V).map(x => caches.delete(x)))));
  self.clients.claim();
});
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin) return;
  const key = /\/(latest|forecast)\.json$/.test(u.pathname) ? u.pathname : e.request;
  e.respondWith(fetch(e.request).then(r => {
    const c = r.clone(); caches.open(V).then(x => x.put(key, c)); return r;
  }).catch(() => caches.match(key).then(m => m || caches.match("index.html"))));
});
