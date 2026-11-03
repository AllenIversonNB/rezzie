// Minimal service worker: caches the app shell so the Home Screen app opens
// instantly; API calls always go to the network (never cached).
const CACHE = "rezzie-v1";
const SHELL = ["/", "/index.html", "/styles.css", "/app.js", "/icon.svg", "/manifest.webmanifest"];
self.addEventListener("install", e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL))); self.skipWaiting(); });
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.pathname.startsWith("/api/") || url.origin !== location.origin) return;
  // Network first, fall back to cache when offline.
  e.respondWith(fetch(e.request).then(r => {
    const copy = r.clone();
    if (r.ok) caches.open(CACHE).then(c => c.put(e.request, copy));
    return r;
  }).catch(() => caches.match(e.request).then(r => r || caches.match("/"))));
});
