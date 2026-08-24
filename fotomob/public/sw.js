/* Fotomob service worker: shell + previously viewed photos, works offline */
const SHELL = "fotomob-shell-v45";
const IMAGES = "fotomob-images-v3";
const BASE = self.registration.scope.replace(/\/$/, "").replace(self.location.origin, "") || "";
const SHELL_URLS = [
  `${BASE}/`,
  `${BASE}/index.html`,
  `${BASE}/styles.css?v=45`,
  `${BASE}/app.js?v=45`,
  `${BASE}/manifest.webmanifest`,
  `${BASE}/icon.png`,
  `${BASE}/icon-192.png`,
  `${BASE}/icon-512.png`,
  `${BASE}/fotomob.png`,
  `${BASE}/fotoman.png`,
  "https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(SHELL);
      await Promise.all(SHELL_URLS.map((u) => cache.add(u).catch(() => {})));
      await self.skipWaiting();
    })()
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keep = new Set([SHELL, IMAGES]);
      const keys = await caches.keys();
      await Promise.all(keys.filter((k) => !keep.has(k)).map((k) => caches.delete(k)));
      await self.clients.claim();
    })()
  );
});

function isImagePath(pathname) {
  return (
    pathname.startsWith("/thumb/") ||
    pathname.startsWith("/original/") ||
    pathname.startsWith("/face/") ||
    pathname.startsWith(`${BASE}/thumb/`) ||
    pathname.startsWith(`${BASE}/original/`) ||
    pathname.startsWith(`${BASE}/face/`)
  );
}

function isLivePath(pathname) {
  return (
    pathname.startsWith(`${BASE}/api/`) ||
    pathname.startsWith(`${BASE}/provider`) ||
    pathname.startsWith(`${BASE}/device`) ||
    pathname.startsWith("/api/") ||
    pathname.startsWith("/provider") ||
    pathname.startsWith("/device")
  );
}

function imageCacheKey(url) {
  const u = new URL(url, self.location.origin);
  const key = new URL(u.pathname, u.origin);
  const provider = u.searchParams.get("provider");
  if (provider) key.searchParams.set("provider", provider);
  return key.href;
}

async function matchAny(cache, urls) {
  for (const u of urls) {
    const hit = await cache.match(u, { ignoreVary: true });
    if (hit) return hit;
  }
  return null;
}

async function cacheFirst(req, cacheName, fallbacks = []) {
  const cache = await caches.open(cacheName);
  const cached =
    (await cache.match(req, { ignoreVary: true })) || (await matchAny(cache, fallbacks));
  const revalidate = fetch(req)
    .then((res) => {
      if (res.ok) cache.put(req, res.clone());
      return res.ok ? res : null;
    })
    .catch(() => null);
  if (cached) {
    revalidate.catch(() => {});
    return cached;
  }
  const res = await revalidate;
  if (res) return res;
  const again = await matchAny(cache, fallbacks);
  if (again) return again;
  throw new Error("offline");
}

async function cacheFirstImage(req, event) {
  const cache = await caches.open(IMAGES);
  const key = imageCacheKey(req.url);
  const hit =
    (await cache.match(key, { ignoreVary: true })) ||
    (await cache.match(req.url, { ignoreVary: true }));
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok) {
    event.waitUntil(cache.put(key, res.clone()).catch(() => {}));
  }
  return res;
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (isLivePath(url.pathname)) return;
  if (url.origin !== self.location.origin) {
    // no-cors images (QR, map tiles) must reach the network; opaque
    // responses look like failures to cacheFirst and break <img>.
    if (req.mode === "no-cors" || req.destination === "image") return;
    event.respondWith(cacheFirst(req, SHELL));
    return;
  }
  if (isImagePath(url.pathname)) {
    event.respondWith(cacheFirstImage(req, event));
    return;
  }
  if (req.mode === "navigate") {
    if (url.pathname.includes("/mocks/")) return;
    const indexUrl = `${self.location.origin}${BASE}/index.html`;
    event.respondWith(
      cacheFirst(new Request(indexUrl), SHELL, [indexUrl, `${self.location.origin}${BASE}/`])
    );
    return;
  }
  event.respondWith(cacheFirst(req, SHELL));
});
