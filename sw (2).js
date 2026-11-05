// Caches the app so it opens offline; data files are network-first with a saved copy as backup.
const V = "la-fishing-v2";
const ASSETS = ["./", "index.html", "manifest.webmanifest", "icon-192.png", "icon-512.png"];
const DATA_RE = /\/(latest|forecast|status)\.json$/;

self.addEventListener("install", e => {
  e.waitUntil(caches.open(V).then(c => c.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== V).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin) return;

  // Data JSON: network-first, cache as backup (keyed by pathname so ?t= bust works)
  if (DATA_RE.test(u.pathname)) {
    const key = u.pathname;
    e.respondWith(
      fetch(e.request)
        .then(r => {
          if (r.ok) {
            const c = r.clone();
            caches.open(V).then(x => x.put(key, c));
          }
          return r;
        })
        .catch(() =>
          caches.match(key).then(m => m || caches.match("index.html"))
        )
    );
    return;
  }

  // App shell: cache-first, then network
  e.respondWith(
    caches.match(e.request).then(cached => {
      if (cached) return cached;
      return fetch(e.request).then(r => {
        if (r.ok && (u.pathname === "/" || u.pathname.endsWith(".html") ||
            u.pathname.endsWith(".webmanifest") || u.pathname.endsWith(".png") ||
            u.pathname.endsWith(".js"))) {
          const c = r.clone();
          caches.open(V).then(x => x.put(e.request, c));
        }
        return r;
      }).catch(() => caches.match("index.html"));
    })
  );
});
