// Caches the app so it opens offline. Data files and the page are network-first with the
// saved copy as backup; on a weak connection the saved copy is used after a few seconds.
// The map library and fonts are cached the first time they load.
const V = "la-fishing-v18";
const CDN_CACHE = "la-fishing-cdn-v1";          // kept across app versions
const TILE_CACHE = "la-fishing-tiles";          // map areas saved for offline (kept too)
const TILE_RE = /^https:\/\/(gis\.charttools\.noaa\.gov|gis\.ngdc\.noaa\.gov)\/.*(\/tile\/|exportImage)/;
const ASSETS = ["./", "index.html", "config.js", "manifest.webmanifest", "icon-192.png", "icon-512.png", "favicon.svg", "apple-touch-icon.png"];
const DATA_RE = /\/(latest|forecast|status|history|ocean|rigs|reefs|tropical|deadzone)\.json$/;
const CDN_RE = /^https:\/\/(cdnjs\.cloudflare\.com|fonts\.googleapis\.com|fonts\.gstatic\.com|cdn\.jsdelivr\.net\/npm\/@supabase)\//;
const SLOW_MS = 4000;                            // give up waiting on the network after this

self.addEventListener("install", e => {
  // Cache each file on its own so one missing file (say, an icon) doesn't break offline mode
  e.waitUntil(caches.open(V).then(c => Promise.all(ASSETS.map(a => c.add(a).catch(() => null)))));
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== V && k !== CDN_CACHE && k !== TILE_CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// Network first. If the network fails, or is slower than SLOW_MS and a saved copy exists,
// use the saved copy. The network response still updates the cache when it arrives.
function networkFirst(req, key, fallback) {
  const net = fetch(req).then(r => {
    if (r.ok) { const c = r.clone(); caches.open(V).then(x => x.put(key, c)); }
    return r;
  });
  const saved = () => caches.match(key).then(m => m || (fallback ? fallback() : undefined));
  const slow = new Promise(res => setTimeout(res, SLOW_MS)).then(() => caches.match(key));
  return Promise.race([net.catch(saved), slow.then(m => m || net.catch(saved))])
    .then(r => r || saved());
}

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const u = new URL(req.url);

  // Map library and fonts: serve the saved copy, refresh it in the background
  if (CDN_RE.test(req.url)) {
    e.respondWith(caches.open(CDN_CACHE).then(c => c.match(req).then(m => {
      const net = fetch(req).then(r => { if (r.ok || r.type === "opaque") c.put(req, r.clone()); return r; });
      return m || net;
    })));
    return;
  }
  // Chart and depth tiles: a saved copy (from "Save this area for offline") first, else the network
  if (TILE_RE.test(req.url)) {
    e.respondWith(caches.open(TILE_CACHE).then(c => c.match(req.url)).then(m => m || fetch(req)));
    return;
  }
  if (u.origin !== location.origin) return;

  // Data JSON: keyed by pathname so the ?t= cache-buster still finds the saved copy.
  // With nothing saved, answer 503 so the page shows its own "could not load" message.
  // Site settings (config.js): network first so a change shows up on the next visit
  if (u.pathname.endsWith("/config.js")) {
    e.respondWith(networkFirst(req, u.pathname));
    return;
  }
  if (DATA_RE.test(u.pathname)) {
    e.respondWith(networkFirst(req, u.pathname, () =>
      new Response('{"error":"offline"}', { status: 503, headers: { "Content-Type": "application/json" } })));
    return;
  }

  // App shell
  e.respondWith(networkFirst(req, req,
    req.mode === "navigate" ? () => caches.match("index.html") : null));
});
