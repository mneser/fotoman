const BASE = /^\/fotomob(\/|$)/.test(location.pathname) ? "/fotomob" : "";
const pathBar = document.getElementById("path-bar");
const pathBanner = document.getElementById("path-banner");
const treeBody = document.getElementById("tree-body");
const treeLabel = document.getElementById("tree-label");
const sheetEl = document.getElementById("sheet");
const sheetScrim = document.getElementById("sheet-scrim");
const sheetDone = document.getElementById("sheet-done");
const sheetSub = document.getElementById("sheet-sub");
const histToggleAll = document.getElementById("hist-toggle-all");
const galleryPeopleEl = document.getElementById("gallery-people");
const shareDialog = document.getElementById("share-dialog");
const shareOutTitle = document.getElementById("share-out-title");
const shareOutQr = document.getElementById("share-out-qr");
const shareOutWa = document.getElementById("share-out-wa");
const shareOutMore = document.getElementById("share-out-more");
const gallery = document.getElementById("gallery");
const photoLoading = document.getElementById("photo-loading");
const statusEl = document.getElementById("status");
const emptyScan = document.getElementById("empty-scan");
const lightbox = document.getElementById("lightbox");
const lightboxImg = document.getElementById("lightbox-img");
const splash = document.getElementById("splash");
const form = document.getElementById("search-form");
const qEl = document.getElementById("q");
const installBtn = document.getElementById("btn-install");
const searchBtn = document.getElementById("btn-search");
const mapBtn = document.getElementById("btn-map");
const mapView = document.getElementById("map-view");
const mapStatus = document.getElementById("map-status");
const lightboxStage = document.getElementById("lightbox-stage");
const lightboxMapBtn = document.getElementById("lightbox-map");
const lightboxBackBtn = document.getElementById("lightbox-back");
const lightboxCount = document.getElementById("lightbox-count");
const lightboxInfo = document.getElementById("lightbox-info");
const lightboxBusy = document.getElementById("lightbox-busy");
const mapBackBtn = document.getElementById("btn-map-back");
const splashStarted = Date.now();
const SPLASH_MIN_MS = 700;
const SESSION_KEY = "fotomob.session";
const SHARES_KEY = "fotomob.shares";

let liveProviders = [];
let currentFolder = null;
let currentTrees = [];
let albumPhotos = [];
let currentPhotos = [];
let selectedPhotoKey = null;
let selectedPersonKeys = new Set();
let peopleByKey = new Map();
let lastCardTap = { key: "", at: 0 };
let treeSummary = { path: "", count: "", cached: false, provider: "" };
let lightboxPhotos = [];
let installPrompt = null;
let lightboxIndex = 0;
let photoMap = null;
let photoMapCluster = null;
let histOpenKeys = null;
let treeSeq = 0;
let albumSeq = 0;
let splashGone = false;

function dismissSplash() {
  if (!splash || splashGone) return;
  splashGone = true;
  const wait = Math.max(0, SPLASH_MIN_MS - (Date.now() - splashStarted));
  setTimeout(() => {
    splash.classList.add("hidden");
    splash.addEventListener("transitionend", () => splash.remove(), { once: true });
  }, wait);
}

function setStatus(msg) {
  statusEl.hidden = !msg;
  statusEl.textContent = msg || "";
  if (msg) setEmptyScan(false);
}

function setEmptyScan(on) {
  if (!emptyScan) return;
  emptyScan.hidden = !on;
  if (on) {
    statusEl.hidden = true;
    statusEl.textContent = "";
  }
}

function setPhotoLoading(on, label = "Loading") {
  if (!photoLoading) return;
  photoLoading.hidden = !on;
  const iris = photoLoading.querySelector(".busy-iris");
  if (iris) iris.setAttribute("aria-label", label);
}

function setDeviceBusy(on, label = "Uploading") {
  const el = document.getElementById("device-busy");
  if (!el) return;
  el.hidden = !on;
  el.setAttribute("aria-label", label);
}

function isProviderLive(provider) {
  return Boolean(provider && liveProviders.includes(String(provider)));
}

function providerHue(name) {
  const s = String(name || "");
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 33 + s.charCodeAt(i)) >>> 0;
  // Live pills stay in green/teal so they never look like an error (red).
  return 118 + (h % 50);
}

function paintProviderPill(el, name, live) {
  if (!el) return;
  const label = String(name || "").trim();
  if (!label) {
    el.hidden = true;
    el.textContent = "";
    el.removeAttribute("title");
    return;
  }
  el.hidden = false;
  el.className = "provider-pill " + (live ? "live" : "stale");
  if (live) el.style.setProperty("--provider-hue", String(providerHue(label)));
  else el.style.removeProperty("--provider-hue");
  el.title = live ? `${label} is online` : `${label} is offline`;
  el.textContent = label;
}

function splitFolderPath(path) {
  const parts = String(path || "").split(" / ").filter(Boolean);
  return { provider: parts[0] || "", rest: parts.slice(1).join(" / ") };
}

function saveSession(partial) {
  try {
    const prev = JSON.parse(localStorage.getItem(SESSION_KEY) || "{}");
    localStorage.setItem(SESSION_KEY, JSON.stringify({ ...prev, ...partial }));
  } catch {
    /* ignore */
  }
}

function loadSession() {
  try {
    return JSON.parse(localStorage.getItem(SESSION_KEY) || "{}");
  } catch {
    return {};
  }
}

/* —— IndexedDB: latest tree / album / people / photo index per source —— */

const DB_NAME = "fotomob";
const DB_VER = 5;

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VER);
    req.onupgradeneeded = () => {
      const db = req.result;
      for (const name of ["trees", "albums", "people", "photos"]) {
        if (!db.objectStoreNames.contains(name)) db.createObjectStore(name);
        else req.transaction.objectStore(name).clear();
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function idbPut(store, key, value) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(store, "readwrite");
    tx.objectStore(store).put(value, key);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

async function idbGet(store, key) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(store, "readonly");
    const req = tx.objectStore(store).get(key);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function idbGetAll(store) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(store, "readonly");
    const req = tx.objectStore(store).getAll();
    req.onsuccess = () => resolve(req.result || []);
    req.onerror = () => reject(req.error);
  });
}

async function hashJson(value) {
  const data = new TextEncoder().encode(JSON.stringify(value));
  const buf = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 16);
}

async function saveTree(node) {
  const hash = await hashJson({
    name: node.name,
    count: node.count,
    children: node.children || [],
  });
  const prev = await idbGet("trees", node.provider);
  if (prev?.hash === hash) return hash;
  await idbPut("trees", node.provider, {
    hash,
    updatedAt: Date.now(),
    tree: node,
  });
  return hash;
}

async function loadCachedTrees() {
  const rows = await idbGetAll("trees");
  return rows
    .filter((r) => r?.tree)
    .sort((a, b) => String(a.tree.name).localeCompare(String(b.tree.name)))
    .map((r) => ({ ...r.tree, stale: true, hash: r.hash, updatedAt: r.updatedAt }));
}

async function saveAlbum(provider, folderId, name, photos) {
  const hash = await hashJson(
    photos.map((p) => [p.id, p.taken_at, p.place_label, p.lat, p.lon, p.width, p.height])
  );
  await idbPut("albums", `${provider}:${folderId}`, {
    hash,
    updatedAt: Date.now(),
    provider,
    folderId,
    name,
    photos,
  });
  await Promise.all(photos.map((p) => upsertPhoto({ ...p, collection_id: folderId })));
}

async function loadCachedAlbum(provider, folderId) {
  return idbGet("albums", `${provider}:${folderId}`);
}

async function savePeople(provider, people) {
  await idbPut("people", provider, { updatedAt: Date.now(), provider, people });
}

async function loadCachedPeople() {
  return idbGetAll("people");
}

async function upsertPhoto(photo) {
  const key = `${photo.provider}:${photo.id}`;
  const prev = await idbGet("photos", key);
  await idbPut("photos", key, {
    ...(prev || {}),
    ...photo,
    person_ids: photo.person_ids?.length ? photo.person_ids : prev?.person_ids || [],
    collection_id: photo.collection_id ?? prev?.collection_id ?? null,
  });
}

async function loadCachedPhotos() {
  return idbGetAll("photos");
}

/* —— share codes —— */

function appPathParts(url) {
  const u = url instanceof URL ? url : new URL(String(url || location.href), location.origin);
  const p = u.pathname.replace(/\/+$/, "") || "/";
  const rel = BASE && (p === BASE || p.startsWith(`${BASE}/`)) ? p.slice(BASE.length) : p;
  return rel.split("/").filter(Boolean);
}

function tokenFromAppPath(kind, url) {
  const parts = appPathParts(url);
  if (parts[0] === kind && parts[1]) return parts[1];
  return "";
}

function cleanAppUrl(url) {
  const next = new URL(url.href);
  next.searchParams.delete("share");
  next.searchParams.delete("device");
  const parts = appPathParts(next);
  if ((parts[0] === "s" || parts[0] === "d") && parts[1]) {
    next.pathname = BASE ? `${BASE}/` : "/";
  }
  history.replaceState(null, "", next.pathname + next.search + next.hash);
}

function normalizeShareToken(raw) {
  const text = String(raw || "").trim();
  if (!text) return "";
  try {
    const url = new URL(text, location.origin);
    const fromQuery = url.searchParams.get("share");
    if (fromQuery) return fromQuery.split(/[,\s]+/).filter(Boolean)[0] || "";
    const fromPath = tokenFromAppPath("s", url);
    if (fromPath) return fromPath;
  } catch {
    /* not a URL */
  }
  return text.split(/[,\s]+/).filter(Boolean)[0] || "";
}

function shareRecords() {
  try {
    const raw = JSON.parse(localStorage.getItem(SHARES_KEY) || "[]");
    if (!Array.isArray(raw)) return [];
    const out = [];
    const seen = new Set();
    for (const row of raw) {
      const code = normalizeShareToken(typeof row === "string" ? row : row?.code || row?.token || "");
      if (!code || seen.has(code)) continue;
      seen.add(code);
      const name = typeof row === "object" && row ? String(row.name || "").trim() : "";
      out.push({ code, name });
    }
    return out;
  } catch {
    return [];
  }
}

function saveShareRecords(rows) {
  const next = [];
  const seen = new Set();
  for (const row of rows || []) {
    const code = normalizeShareToken(row?.code);
    if (!code || seen.has(code)) continue;
    seen.add(code);
    next.push({ code, name: String(row?.name || "").trim() });
  }
  localStorage.setItem(SHARES_KEY, JSON.stringify(next));
  return next;
}

function shareCodes() {
  return shareRecords().map((r) => r.code);
}

function saveShareCodes(codes) {
  const names = new Map(shareRecords().map((r) => [r.code, r.name]));
  return saveShareRecords(
    codes.map((c) => ({ code: c, name: names.get(normalizeShareToken(c)) || "" }))
  );
}

function addShareCode(raw, name) {
  const token = normalizeShareToken(raw);
  if (!token) return shareCodes();
  const recs = shareRecords();
  const i = recs.findIndex((r) => r.code === token);
  const next = { code: token, name: String(name || (i >= 0 ? recs[i].name : "") || "").trim() };
  if (i >= 0) recs[i] = { ...recs[i], ...next };
  else recs.push(next);
  saveShareRecords(recs);
  return shareCodes();
}

function removeShareCode(token) {
  return saveShareCodes(shareCodes().filter((c) => c !== token));
}

function rememberShareNamesFromTree() {
  const recs = shareRecords();
  let changed = false;
  const next = recs.map((rec) => {
    const name = shareDisplayName(rec);
    if (name && name !== rec.name) {
      changed = true;
      return { ...rec, name };
    }
    return rec;
  });
  if (changed) saveShareRecords(next);
}

function unlockedRootFolders() {
  const out = [];
  for (const t of currentTrees || []) {
    const kids = t.children || [];
    if (kids.length) out.push(...kids);
    else if (t.kind !== "provider") out.push(t);
  }
  return out;
}

function shareDisplayName(rec) {
  const folder = folderForShareCode(rec.code);
  if (folder?.name && folder.kind !== "provider") return folder.name;
  const stored = String(rec.name || "").trim();
  const placeholder =
    !stored ||
    stored === "Collection" ||
    stored === "Unlocked collection" ||
    stored === "Shared collection";
  if (!placeholder) return stored;
  const roots = unlockedRootFolders().filter((f) => f?.name);
  const match = roots.find(
    (f) => String(f.share_code || "").trim() === String(rec.code || "").trim()
  );
  if (match?.name) return match.name;
  const recs = shareRecords();
  if (recs.length === 1 && roots.length === 1) return roots[0].name;
  return "";
}

function consumeShareFromUrl() {
  const url = new URL(location.href);
  const incoming = [];
  for (const value of url.searchParams.getAll("share")) {
    incoming.push(...String(value).split(/[,\s]+/));
  }
  const fromPath = tokenFromAppPath("s", url);
  if (fromPath) incoming.push(fromPath);
  const added = incoming.map(normalizeShareToken).filter(Boolean);
  if (!added.length) return shareCodes();
  return saveShareCodes([...shareCodes(), ...added]);
}

function apiPath(path) {
  const p = String(path || "");
  if (!p || /^https?:\/\//i.test(p)) return p;
  return `${BASE}${p.startsWith("/") ? p : `/${p}`}`;
}

function mediaUrl(path) {
  return withShare(path);
}

function withShare(url) {
  const codes = shareCodes();
  const abs = apiPath(url);
  if (!codes.length) return abs;
  const u = new URL(abs, location.origin);
  u.searchParams.set("share", codes.join(","));
  return u.pathname + u.search;
}

async function clearShareCache() {
  try {
    const db = await openDb();
    await new Promise((resolve, reject) => {
      const tx = db.transaction(["trees", "albums", "people", "photos"], "readwrite");
      for (const name of ["trees", "albums", "people", "photos"]) {
        tx.objectStore(name).clear();
      }
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  } catch {
    /* ignore */
  }
}

function folderForShareCode(code) {
  const want = String(code || "").trim();
  if (!want) return null;
  let found = null;
  const walk = (nodes) => {
    for (const n of nodes || []) {
      if (String(n.share_code || "").trim() === want) {
        found = n;
        return true;
      }
      if (walk(n.children)) return true;
    }
    return false;
  };
  walk(currentTrees);
  return found;
}

function renderShareList() {
  const list = document.getElementById("share-list");
  if (!list) return;
  const recs = shareRecords();
  list.replaceChildren();
  if (!recs.length) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = "No collections on this phone yet.";
    list.append(empty);
    return;
  }
  for (const rec of recs) {
    const shown = shareDisplayName(rec) || "collection offline";
    const li = document.createElement("li");
    const label = document.createElement("span");
    label.className = "share-list-name";
    label.textContent = shown;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "share-list-remove";
    btn.textContent = "\u00d7";
    btn.title = "Remove this share from the phone";
    btn.setAttribute("aria-label", `Remove ${shown}`);
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      if (!confirm(`Remove “${shown}” from this phone?`)) return;
      forgetShare(rec.code);
    });
    li.append(label, btn);
    list.append(li);
  }
}

function classifyScannedText(raw) {
  const text = String(raw || "").trim();
  if (!text) return null;
  try {
    const url = new URL(text);
    if (!/^https?:$/i.test(url.protocol)) return null;
    if (url.searchParams.get("device") || tokenFromAppPath("d", url)) {
      const token = normalizeDeviceToken(text);
      return token ? { kind: "device", token } : null;
    }
    if (url.searchParams.get("share") || tokenFromAppPath("s", url)) {
      const token = normalizeShareToken(text);
      return token ? { kind: "share", token } : null;
    }
    return null;
  } catch {
    /* not an absolute URL */
  }
  if (!/^[A-Za-z0-9_-]{6,}$/.test(text)) return null;
  return { kind: "share", token: text };
}

let qrStream = null;
let qrTickId = 0;
let qrDetector = null;
let qrJsQR = null;
const qrCanvas = document.createElement("canvas");

function setQrScanStatus(msg) {
  const el = document.getElementById("qr-scan-status");
  if (el) el.textContent = msg || "";
}

function stopQrCamera() {
  qrTickId += 1;
  const stream = qrStream;
  qrStream = null;
  const video = document.getElementById("qr-scan-video");
  if (video) {
    video.pause();
    video.srcObject = null;
  }
  for (const track of stream?.getTracks() || []) track.stop();
}

function closeQrScan() {
  stopQrCamera();
  const dialog = document.getElementById("qr-scan");
  if (dialog?.open) dialog.close();
}

function loadJsQR() {
  if (qrJsQR) return Promise.resolve(qrJsQR);
  if (typeof window.jsQR === "function") {
    qrJsQR = window.jsQR;
    return Promise.resolve(qrJsQR);
  }
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "https://cdn.jsdelivr.net/npm/jsqr@1.4.0/dist/jsQR.min.js";
    script.onload = () => {
      qrJsQR = typeof window.jsQR === "function" ? window.jsQR : null;
      if (qrJsQR) resolve(qrJsQR);
      else reject(new Error("QR decoder missing"));
    };
    script.onerror = () => reject(new Error("Could not load QR decoder"));
    document.head.append(script);
  });
}

async function detectQrFrame(video) {
  if (qrDetector) {
    try {
      const codes = await qrDetector.detect(video);
      if (codes[0]?.rawValue) return codes[0].rawValue;
    } catch {
      /* try canvas decoder */
    }
  }
  if (!qrJsQR) return "";
  const w = video.videoWidth;
  const h = video.videoHeight;
  if (!w || !h) return "";
  qrCanvas.width = w;
  qrCanvas.height = h;
  const ctx = qrCanvas.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(video, 0, 0, w, h);
  const img = ctx.getImageData(0, 0, w, h);
  return qrJsQR(img.data, w, h)?.data || "";
}

async function ingestQrHit(hit) {
  stopQrCamera();
  setQrScanStatus(hit.kind === "device" ? "Adding server…" : "Unlocking…");
  if (hit.kind === "device") {
    addServer(hit.token);
    selectServer(hit.token);
    closeQrScan();
    renderServerList();
    void refreshDeviceStatus();
    return;
  }
  addShareCode(hit.token);
  await clearShareCache();
  await loadTree();
  void loadPeople();
  closeQrScan();
  document.getElementById("about-dialog")?.close();
  await openFolderForShareCode(hit.token);
}

async function qrLoop(video, gen) {
  if (gen !== qrTickId) return;
  if (video.readyState >= 2) {
    const text = await detectQrFrame(video);
    if (gen !== qrTickId) return;
    if (text) {
      const hit = classifyScannedText(text);
      if (hit) {
        await ingestQrHit(hit);
        return;
      }
      setQrScanStatus("Not a Fotoman QR");
    }
  }
  setTimeout(() => qrLoop(video, gen), qrJsQR && !qrDetector ? 120 : 50);
}

async function openQrScan() {
  const dialog = document.getElementById("qr-scan");
  const video = document.getElementById("qr-scan-video");
  if (!dialog || !video) return;
  setQrScanStatus("Point at a Fotoman QR");
  if (!dialog.open) dialog.showModal();
  stopQrCamera();
  const gen = qrTickId;
  try {
    if (window.BarcodeDetector) {
      qrDetector = qrDetector || new BarcodeDetector({ formats: ["qr_code"] });
    }
  } catch {
    qrDetector = null;
  }
  if (!qrDetector) {
    try {
      await loadJsQR();
    } catch {
      setQrScanStatus("QR scanning is not supported in this browser");
      return;
    }
  } else {
    loadJsQR().catch(() => {});
  }
  if (!navigator.mediaDevices?.getUserMedia) {
    setQrScanStatus("Camera is not available");
    return;
  }
  try {
    qrStream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: { facingMode: { ideal: "environment" } },
    });
  } catch {
    setQrScanStatus("Allow camera access to scan");
    return;
  }
  if (gen !== qrTickId) {
    for (const track of qrStream.getTracks()) track.stop();
    qrStream = null;
    return;
  }
  video.srcObject = qrStream;
  try {
    await video.play();
  } catch {
    /* muted autoplay should work */
  }
  qrLoop(video, gen);
}

function shareableFor(provider, folderId) {
  const want = String(folderId ?? "");
  if (!provider || !want) return null;
  let found = null;
  const stack = [];
  const walk = (nodes) => {
    for (const n of nodes || []) {
      const code = String(n.share_code || "").trim();
      if (code) stack.push({ name: n.name, code, url: n.share_url || "" });
      if ((n.provider || "") === String(provider) && String(n.id) === want) {
        found = stack[stack.length - 1] || null;
        return true;
      }
      if (walk(n.children)) return true;
      if (code) stack.pop();
    }
    return false;
  };
  walk(currentTrees);
  return found;
}

function collectionShareUrl(info) {
  const given = String(info?.url || "").trim();
  if (/^https?:\/\//i.test(given)) return given;
  const code = String(info?.code || given || "").trim();
  if (!code) return "";
  return `${location.origin}${BASE || ""}/s/${code}`;
}

function sharePayload(info) {
  const url = collectionShareUrl(info);
  const name = String(info?.name || "this collection").trim() || "this collection";
  const text = `Look at “${name}” on Fotomob`;
  return { url, text, full: `${text}\n${url}` };
}

function drawShareQr(url) {
  if (!shareOutQr) return;
  shareOutQr.replaceChildren();
  shareOutQr.hidden = !url;
  if (!url) return;
  if (typeof QRCode === "function") {
    try {
      new QRCode(shareOutQr, {
        text: url,
        width: 220,
        height: 220,
        colorDark: "#111814",
        colorLight: "#ffffff",
        correctLevel: QRCode.CorrectLevel.M,
      });
      return;
    } catch {
      /* fall through */
    }
  }
  const img = document.createElement("img");
  img.alt = "";
  img.width = 220;
  img.height = 220;
  img.src = `https://api.qrserver.com/v1/create-qr-code/?size=240x240&ecc=M&margin=8&data=${encodeURIComponent(url)}`;
  shareOutQr.append(img);
}

function openShareOut(info) {
  const { url, full } = sharePayload(info);
  if (!url || !shareDialog) return;
  closeSheet();
  closeSearchView();
  if (shareOutTitle) {
    const name = String(info?.name || "").trim();
    shareOutTitle.textContent = name ? `Share “${name}”` : "Share collection";
  }
  drawShareQr(url);
  if (shareOutWa) {
    shareOutWa.href = `https://wa.me/?text=${encodeURIComponent(full)}`;
  }
  shareDialog.dataset.full = full;
  shareDialog.showModal();
}

async function shareOutNative() {
  const full = shareDialog?.dataset.full || "";
  if (!full) return;
  if (navigator.share) {
    try {
      await navigator.share({ title: "Fotomob", text: full });
      return;
    } catch (err) {
      if (err?.name === "AbortError") return;
    }
  }
  try {
    await navigator.clipboard.writeText(full);
    setStatus("Link copied.");
  } catch {
    setStatus("Could not copy link.");
  }
}

/* —— fetch —— */

async function fetchJson(url, timeoutMs = 12000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(withShare(url), { cache: "no-store", signal: ctrl.signal });
    let data = null;
    try {
      data = await r.json();
    } catch {
      data = null;
    }
    return { ok: r.ok, status: r.status, data };
  } catch {
    return { ok: false, status: 0, data: null };
  } finally {
    clearTimeout(timer);
  }
}

function hasSearchFilters() {
  return Boolean(qEl.value.trim());
}

function searchParams({ provider, folderId } = {}) {
  const q = new URLSearchParams();
  if (provider) q.set("provider", provider);
  const text = qEl.value.trim();
  if (text) q.set("q", text);
  if (folderId) {
    if (/^\d+$/.test(String(folderId))) q.set("collection_id", String(folderId));
    else q.set("collection", String(folderId));
  }
  q.set("mode", text ? "visual" : "meta");
  q.set("top_k", "120");
  return q;
}

/* —— tree / gallery —— */

function renderNode(folder, depth) {
  const kids = folder.children || [];
  const wrap = document.createElement("div");
  wrap.className = "tree-node";
  if (folder.kind === "provider") wrap.classList.add("is-provider");
  if (folder.stale) wrap.classList.add("is-stale");
  if (kids.length) wrap.classList.add("open");

  const row = document.createElement("button");
  row.type = "button";
  row.className = "browse-item";
  const linked = !!folder.linked;
  const sourceSubdir = !!folder.source_subdir;
  if (linked) row.classList.add("is-linked");
  if (linked && !sourceSubdir) row.classList.add("is-linked-root");
  row.style.paddingLeft = `${1 + depth * 0.85}rem`;
  row.dataset.id = String(folder.id);
  row.dataset.provider = folder.provider || "";
  row.dataset.kind = folder.kind || "folder";
  row.dataset.name = folder.name || "";

  const tw = document.createElement("span");
  tw.className = "browse-tw";
  tw.setAttribute("aria-hidden", "true");
  tw.textContent = kids.length ? "▾" : " ";

  const name = document.createElement("span");
  name.className = "browse-item-name";
  name.textContent = folder.name;

  const count = document.createElement("span");
  count.className = "browse-item-count";
  count.textContent = String(folder.count ?? 0);

  if (folder.kind === "provider") {
    paintProviderPill(name, folder.name, !folder.stale);
    name.classList.add("browse-item-name");
  }
  row.append(tw, name, count);

  const rowWrap = document.createElement("div");
  rowWrap.className = "browse-row";
  rowWrap.append(row);
  const shareCode = String(folder.share_code || "").trim();
  if (shareCode) {
    const shareBtn = document.createElement("button");
    shareBtn.type = "button";
    shareBtn.className = "browse-item-share";
    shareBtn.title = "Share collection";
    shareBtn.setAttribute("aria-label", `Share ${folder.name}`);
    shareBtn.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="18" cy="5" r="2.5" fill="none" stroke="currentColor" stroke-width="1.75"/><circle cx="6" cy="12" r="2.5" fill="none" stroke="currentColor" stroke-width="1.75"/><circle cx="18" cy="19" r="2.5" fill="none" stroke="currentColor" stroke-width="1.75"/><path d="M8.4 10.9 15.6 6.9M8.4 13.1 15.6 17.1" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round"/></svg>`;
    shareBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      openShareOut({
        name: folder.name,
        code: shareCode,
        url: folder.share_url,
      });
    });
    rowWrap.append(shareBtn);
  }
  wrap.append(rowWrap);

  let childBox = null;
  if (kids.length) {
    childBox = document.createElement("div");
    childBox.className = "tree-children";
    for (const child of kids) childBox.append(renderNode(child, depth + 1));
    wrap.append(childBox);
    tw.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      wrap.classList.toggle("open");
      childBox.hidden = !wrap.classList.contains("open");
      tw.textContent = wrap.classList.contains("open") ? "▾" : "▸";
    });
  }

  row.addEventListener("click", () => {
    if (folder.kind === "provider") {
      if (!childBox) return;
      wrap.classList.toggle("open");
      childBox.hidden = !wrap.classList.contains("open");
      tw.textContent = wrap.classList.contains("open") ? "▾" : "▸";
      return;
    }
    closeSheet();
    loadAlbum(folder.provider, folder.id, folder.name);
  });
  return wrap;
}

function markSelected(provider, folderId) {
  const host = treeBody;
  if (!host) return;
  host.querySelectorAll(".browse-item").forEach((el) => {
    el.classList.toggle(
      "selected",
      el.dataset.kind !== "provider" &&
        el.dataset.provider === String(provider) &&
        el.dataset.id === String(folderId)
    );
  });
}

function selectedFolderPath() {
  const selected = treeBody?.querySelector(".browse-item.selected");
  if (!selected) return currentFolder?.name || "";
  const parts = [];
  let node = selected.closest(".tree-node");
  while (node && (!treeBody || treeBody.contains(node))) {
    const item = node.querySelector(":scope > .browse-row > .browse-item, :scope > .browse-item");
    if (item?.dataset.kind === "provider") break;
    if (item?.dataset.name) parts.unshift(item.dataset.name);
    node = node.parentElement?.closest(".tree-node") || null;
  }
  return parts.join(" / ");
}

function providerLabel(provider) {
  const name = String(provider || "");
  const tree = currentTrees.find((t) => (t.provider || t.name) === name);
  return tree?.name || name;
}

function providerTotalCount(provider) {
  const tree = currentTrees.find((t) => (t.provider || t.name) === String(provider || ""));
  if (!tree) return "";
  return tree.count ?? "";
}

function headerProvider() {
  return currentFolder?.provider || treeSummary.provider || currentTrees[0]?.provider || currentTrees[0]?.name || "";
}

function updatePathBanner() {
  if (!treeLabel) return;
  treeLabel.replaceChildren();
  const provider = headerProvider();
  const label = providerLabel(provider);
  const rest = treeSummary.path || "";
  if (label) {
    const pill = document.createElement("span");
    paintProviderPill(pill, label, isProviderLive(provider));
    treeLabel.append(pill);
    if (rest) {
      const tail = document.createElement("span");
      tail.className = "tree-label-rest";
      const parts = rest.split(" / ").filter(Boolean);
      const last = parts.pop();
      let text = " / ";
      if (parts.length) text += `${parts.join(" / ")} / `;
      tail.append(document.createTextNode(text));
      if (last) {
        const b = document.createElement("b");
        b.textContent = last;
        tail.append(b);
      }
      treeLabel.append(tail);
    }
  } else {
    const fallback = document.createElement("span");
    fallback.className = "tree-label-rest";
    fallback.textContent = "Collections";
    treeLabel.append(fallback);
  }
  const count = treeSummary.count === "" || treeSummary.count == null
    ? providerTotalCount(provider)
    : treeSummary.count;
  const bits = [
    [label, rest].filter(Boolean).join(" / ") || "Collections",
    treeSummary.cached ? "cached" : "",
    count === "" || count == null ? "" : String(count),
  ].filter(Boolean);
  if (pathBanner) pathBanner.title = bits.join(" · ");
  if (sheetSub) {
    sheetSub.textContent = count === "" || count == null ? "Choose a folder" : `${count} photos`;
  }
}

function setTreeSummary(path, count, cached = false) {
  const provider = currentFolder?.provider || treeSummary.provider || currentTrees[0]?.provider || "";
  treeSummary = {
    path: path || "",
    count: count ?? "",
    cached,
    provider,
  };
  updatePathBanner();
}

function isSheetOpen() {
  return Boolean(sheetEl?.classList.contains("on"));
}

function setTreeNodeOpen(node, open) {
  const childBox = node?.querySelector(":scope > .tree-children");
  if (!childBox) return;
  node.classList.toggle("open", open);
  childBox.hidden = !open;
  const tw = node.querySelector(":scope > .browse-row .browse-tw");
  if (tw && tw.textContent.trim()) tw.textContent = open ? "▾" : "▸";
}

function expandSheetTree() {
  if (!treeBody) return;
  treeBody.querySelectorAll(".tree-node").forEach((node) => setTreeNodeOpen(node, false));
  const provider = String(headerProvider() || "");
  treeBody.querySelectorAll(".tree-node.is-provider").forEach((node) => {
    const item = node.querySelector(":scope > .browse-row > .browse-item");
    const mine = Boolean(provider) && (item?.dataset.provider || "") === provider;
    setTreeNodeOpen(node, mine || !provider);
  });
  let node = treeBody.querySelector(".browse-item.selected")?.closest(".tree-node");
  while (node && treeBody.contains(node)) {
    setTreeNodeOpen(node, true);
    node = node.parentElement?.closest(".tree-node") || null;
  }
}

function openSheet() {
  if (!sheetEl || !sheetScrim) return;
  closeSearchView();
  expandSheetTree();
  sheetScrim.hidden = false;
  requestAnimationFrame(() => {
    sheetScrim.classList.add("on");
    sheetEl.classList.add("on");
    sheetEl.setAttribute("aria-hidden", "false");
    pathBanner?.setAttribute("aria-expanded", "true");
  });
}

function closeSheet() {
  if (!sheetEl) return;
  sheetScrim?.classList.remove("on");
  sheetEl.classList.remove("on");
  sheetEl.setAttribute("aria-hidden", "true");
  pathBanner?.setAttribute("aria-expanded", "false");
  setTimeout(() => {
    if (sheetScrim && !isSheetOpen()) sheetScrim.hidden = true;
  }, 220);
}

function toggleSheet() {
  if (isSheetOpen()) closeSheet();
  else openSheet();
}

function renderTree(trees) {
  currentTrees = Array.isArray(trees) ? trees : [];
  rememberShareNamesFromTree();
  renderShareList();
  if (!treeBody) return;
  treeBody.replaceChildren();
  const frag = document.createDocumentFragment();
  const active = headerProvider();
  const ordered = [...currentTrees].sort((a, b) => {
    const ap = (a.provider || a.name) === active ? 0 : 1;
    const bp = (b.provider || b.name) === active ? 0 : 1;
    return ap - bp || String(a.name).localeCompare(String(b.name));
  });
  for (const folder of ordered) frag.append(renderNode(folder, 0));
  treeBody.append(frag);
  if (currentFolder) markSelected(currentFolder.provider, currentFolder.folderId);
  if (isSheetOpen()) expandSheetTree();
}

async function forgetShare(code) {
  removeShareCode(code);
  await clearShareCache();
  const trees = await loadTree();
  void loadPeople();
  renderShareList();
  const still = currentFolder && folderInTrees(trees, currentFolder.provider, currentFolder.folderId);
  if (still) {
    await loadAlbum(currentFolder.provider, currentFolder.folderId, currentFolder.name);
    return;
  }
  currentFolder = null;
  albumPhotos = [];
  selectedPersonKeys.clear();
  const next = trees[0];
  treeSummary = {
    path: "",
    count: next?.count ?? "",
    cached: Boolean(next?.stale),
    provider: next?.provider || next?.name || "",
  };
  renderTree(trees);
  updatePathBanner();
  renderPhotos([]);
}

function folderInTrees(trees, provider, folderId) {
  const want = String(folderId);
  const walk = (nodes) => {
    for (const n of nodes || []) {
      if ((n.provider || "") === provider && String(n.id) === want) return true;
      if (walk(n.children)) return true;
    }
    return false;
  };
  return walk(trees);
}

function photoKey(p) {
  return `${p.provider}:${p.id}`;
}

const MONTH_SHORT = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

function formatTaken(taken) {
  const s = String(taken || "");
  const d = s.slice(0, 10);
  const m = d.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return s || "Undated";
  const month = MONTH_SHORT[Number(m[2]) - 1] || m[2];
  return `${month} ${Number(m[3])}, ${m[1]}`;
}

function photoInfoLines(p) {
  const lines = [];
  lines.push(p.taken_at ? formatTaken(p.taken_at) : "Undated");
  if (p.place_label) lines.push(p.place_label);
  else if (p.city || p.country) lines.push([p.city, p.region, p.country].filter(Boolean).join(", "));
  if (p.camera) lines.push(p.camera);
  if (p.width && p.height) lines.push(`${p.width}×${p.height}`);
  return lines;
}

function parseDay(key) {
  const m = String(key || "").match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return null;
  return { y: m[1], mo: Number(m[2]), d: Number(m[3]), month: MONTH_SHORT[Number(m[2]) - 1] || m[2] };
}

function formatDateRange(from, to) {
  if (!from && !to) return "Undated";
  const aKey = from <= to ? from : to;
  const bKey = from <= to ? to : from;
  const a = parseDay(aKey);
  const b = parseDay(bKey);
  if (!a || !b) return formatTaken(bKey || aKey);
  if (aKey === bKey) return `${a.month} ${a.d}, ${a.y}`;
  if (a.y === b.y && a.mo === b.mo) return `${a.month} ${a.d}\u2013${b.d}, ${a.y}`;
  if (a.y === b.y) return `${a.month} ${a.d} \u2013 ${b.month} ${b.d}, ${a.y}`;
  return `${a.month} ${a.d}, ${a.y} \u2013 ${b.month} ${b.d}, ${b.y}`;
}

function sortPhotosByDate(photos) {
  return [...photos].sort((a, b) => {
    const ta = a.taken_at ? String(a.taken_at) : "";
    const tb = b.taken_at ? String(b.taken_at) : "";
    if (ta !== tb) return tb.localeCompare(ta);
    return Number(b.id) - Number(a.id);
  });
}

function photosMatchingPeople(photos) {
  if (!selectedPersonKeys.size) return photos;
  const need = [...selectedPersonKeys];
  return photos.filter((p) =>
    need.some((key) => {
      const colon = key.lastIndexOf(":");
      const prov = key.slice(0, colon);
      const id = Number(key.slice(colon + 1));
      return p.provider === prov && (p.person_ids || []).map(Number).includes(id);
    })
  );
}

function dateGroups(photos) {
  const sorted = sortPhotosByDate(photos);
  const days = [];
  for (const p of sorted) {
    const key = p.taken_at ? String(p.taken_at).slice(0, 10) : "";
    const last = days[days.length - 1];
    if (last && last.key === key) last.photos.push(p);
    else days.push({ key, photos: [p] });
  }
  const groups = [];
  let small = null;
  const flush = () => {
    if (!small) return;
    groups.push(small);
    small = null;
  };
  for (const day of days) {
    if (!day.key) {
      flush();
      groups.push({ from: "", to: "", photos: day.photos });
    } else if (day.photos.length > 6) {
      flush();
      groups.push({ from: day.key, to: day.key, photos: day.photos });
    } else if (!small) {
      small = { from: day.key, to: day.key, photos: [...day.photos] };
    } else {
      small.photos.push(...day.photos);
      small.to = day.key;
    }
  }
  flush();
  return groups;
}

function personThumbSrc(person) {
  const thumb = String(person?.thumb || "");
  if (thumb.includes("/face/")) return mediaUrl(thumb);
  const faceId = Number(person?.cover_face_id);
  if (Number.isFinite(faceId) && faceId > 0) {
    const q = new URLSearchParams();
    if (person.provider) q.set("provider", person.provider);
    return mediaUrl(`/face/${faceId}?${q}`);
  }
  return "";
}

function pruneSelectedPeople(photos) {
  const valid = new Set();
  for (const p of photos || []) {
    for (const id of p.person_ids || []) valid.add(`${p.provider}:${id}`);
  }
  for (const key of [...selectedPersonKeys]) {
    if (!valid.has(key)) selectedPersonKeys.delete(key);
  }
}

function visibleGalleryPhotos() {
  return sortPhotosByDate(photosMatchingPeople(albumPhotos));
}

function selectPhoto(p) {
  selectedPhotoKey = p ? photoKey(p) : null;
  gallery.querySelectorAll(".card").forEach((el) => {
    el.classList.toggle("selected", el.dataset.key === selectedPhotoKey);
  });
}

function onGalleryCardActivate(p, e) {
  e.preventDefault();
  const key = photoKey(p);
  const now = Date.now();
  if (lastCardTap.key === key && now - lastCardTap.at < 500) {
    lastCardTap = { key: "", at: 0 };
    openLightbox(p);
    return;
  }
  lastCardTap = { key, at: now };
  selectPhoto(p);
}

function togglePersonFilter(key) {
  if (selectedPersonKeys.has(key)) selectedPersonKeys.delete(key);
  else selectedPersonKeys.add(key);
  applyGalleryView();
}

function applyGalleryView() {
  const photos = visibleGalleryPhotos();
  currentPhotos = photos;
  if (selectedPhotoKey && !photos.some((p) => photoKey(p) === selectedPhotoKey)) {
    selectedPhotoKey = null;
  }
  syncMapBtn();
  renderGalleryPeople(albumPhotos);
  paintPhotoGrid(photos);
  if (mapView && !mapView.hidden) renderMapPhotos(photos);
  restorePhotoHash();
  if (currentFolder) {
    treeSummary = { ...treeSummary, count: photos.length };
    updatePathBanner();
  }
  if (albumPhotos.length && !photos.length && selectedPersonKeys.size) {
    setStatus("No photos of the selected people.");
  } else if (photos.length) {
    setStatus("");
  }
}

function renderGalleryPeople(photos) {
  if (!galleryPeopleEl) return;
  const seen = new Map();
  for (const p of photos || []) {
    for (const id of p.person_ids || []) {
      const key = `${p.provider}:${id}`;
      if (seen.has(key)) continue;
      const person = peopleByKey.get(key);
      if (person) seen.set(key, person);
    }
  }
  const people = [...seen.values()];
  const x = galleryPeopleEl.scrollLeft;
  galleryPeopleEl.replaceChildren();
  galleryPeopleEl.hidden = !people.length;
  for (const person of people) {
    const key = `${person.provider}:${person.id}`;
    const el = document.createElement("button");
    el.type = "button";
    el.className = "gallery-person";
    el.title = person.display_name || "";
    el.setAttribute("aria-label", person.display_name || "Person");
    el.setAttribute("aria-pressed", selectedPersonKeys.has(key) ? "true" : "false");
    el.classList.toggle("selected", selectedPersonKeys.has(key));
    const src = personThumbSrc(person);
    if (src) {
      const img = document.createElement("img");
      img.src = src;
      img.alt = person.display_name || "";
      img.loading = "lazy";
      img.addEventListener("error", () => {
        img.remove();
        const mark = document.createElement("span");
        mark.textContent = String(person.display_name || "?").slice(0, 1).toUpperCase();
        el.append(mark);
      });
      el.append(img);
    } else {
      const mark = document.createElement("span");
      mark.textContent = String(person.display_name || "?").slice(0, 1).toUpperCase();
      el.append(mark);
    }
    el.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      togglePersonFilter(key);
    });
    galleryPeopleEl.append(el);
  }
  galleryPeopleEl.scrollLeft = x;
}

function photoCard(p) {
  const cell = document.createElement("div");
  cell.className = "cell";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "card";
  btn.dataset.key = photoKey(p);
  btn.classList.toggle("selected", selectedPhotoKey === photoKey(p));
  btn.addEventListener("click", (e) => onGalleryCardActivate(p, e));
  const img = document.createElement("img");
  img.src = mediaUrl(p.thumb);
  img.alt = "";
  img.loading = "lazy";
  const info = document.createElement("div");
  info.className = "card-info";
  const lines = photoInfoLines(p);
  const strong = document.createElement("strong");
  strong.textContent = lines[0] || "";
  info.append(strong);
  for (const line of lines.slice(1)) {
    const span = document.createElement("span");
    span.textContent = line;
    info.append(span);
  }
  btn.append(img, info);
  cell.append(btn);
  return cell;
}

function paintPhotoGrid(photos) {
  gallery.replaceChildren();
  if (!photos.length) {
    setHistToggleVisible(false);
    return;
  }
  const buckets = galleryBuckets(photos, "month");
  setHistToggleVisible(buckets.length > 0);
  const allKeys = buckets.map((b) => b.key);
  const peak = Math.max(1, ...buckets.map((b) => b.photos.length));
  const frag = document.createDocumentFragment();
  for (const row of buckets) {
    const open = histOpenKeys == null || histOpenKeys.has(row.key);
    const pct = Math.max(8, Math.round((row.photos.length / peak) * 100));
    const sec = document.createElement("section");
    sec.className = "bucket" + (open ? " open" : "");
    sec.dataset.key = row.key;

    const hist = document.createElement("button");
    hist.type = "button";
    hist.className = "hist";
    hist.setAttribute("aria-expanded", open ? "true" : "false");
    const label = document.createElement("span");
    label.className = "hist-label";
    label.textContent = row.label;
    const track = document.createElement("span");
    track.className = "hist-track";
    const bar = document.createElement("i");
    bar.className = "hist-bar";
    bar.style.width = `${pct}%`;
    track.append(bar);
    const count = document.createElement("span");
    count.className = "hist-count";
    count.textContent = String(row.photos.length);
    hist.append(label, track, count);
    hist.addEventListener("click", () => {
      if (histOpenKeys == null) histOpenKeys = new Set(allKeys);
      if (histOpenKeys.has(row.key)) histOpenKeys.delete(row.key);
      else histOpenKeys.add(row.key);
      if (histOpenKeys.size === allKeys.length) histOpenKeys = null;
      const nowOpen = histOpenKeys == null || histOpenKeys.has(row.key);
      sec.classList.toggle("open", nowOpen);
      hist.setAttribute("aria-expanded", nowOpen ? "true" : "false");
      syncHistToggle();
    });

    const grid = document.createElement("div");
    grid.className = "grid";
    for (const p of row.photos) grid.append(photoCard(p));
    sec.append(hist, grid);
    frag.append(sec);
  }
  gallery.append(frag);
  syncHistToggle();
}

function renderPhotos(photos) {
  albumPhotos = photos || [];
  pruneSelectedPeople(albumPhotos);
  applyGalleryView();
}

/* —— histogram buckets —— */

function photoTimelineBucketKey(p, gran) {
  if (!p?.taken_at) return "";
  const s = String(p.taken_at);
  if (gran === "year") return s.slice(0, 4);
  if (gran === "month") return s.slice(0, 7);
  return s.slice(0, 10);
}

function timelineBucketLabel(key, gran) {
  if (!key) return "Undated";
  if (gran === "year") return key;
  if (gran === "month") {
    const [y, m] = key.split("-");
    return `${MONTH_SHORT[Number(m) - 1] || m} ${y}`;
  }
  const [y, m, d] = key.split("-");
  return `${MONTH_SHORT[Number(m) - 1] || m} ${Number(d)}, ${y}`;
}

function galleryBuckets(photos, gran) {
  if (gran === "day") {
    return dateGroups(photos).map((g) => ({
      key: g.from && g.to ? `${g.from}:${g.to}` : "undated",
      label: formatDateRange(g.from, g.to),
      photos: g.photos,
    }));
  }
  const map = new Map();
  const undated = [];
  for (const p of photos) {
    const k = photoTimelineBucketKey(p, gran);
    if (!k) {
      undated.push(p);
      continue;
    }
    let list = map.get(k);
    if (!list) {
      list = [];
      map.set(k, list);
    }
    list.push(p);
  }
  const rows = [...map.keys()].sort().reverse().map((k) => ({
    key: k,
    label: timelineBucketLabel(k, gran),
    photos: map.get(k),
  }));
  if (undated.length) rows.push({ key: "undated", label: "Undated", photos: undated });
  return rows;
}

function resetHistogram() {
  histOpenKeys = null;
}

function histAnyOpen() {
  return Boolean(gallery?.querySelector(".bucket.open"));
}

function setHistToggleVisible(on) {
  if (histToggleAll) histToggleAll.hidden = !on;
}

function syncHistToggle() {
  if (!histToggleAll) return;
  const expanded = histAnyOpen();
  histToggleAll.dataset.state = expanded ? "expanded" : "collapsed";
  const label = expanded ? "Collapse all" : "Expand all";
  histToggleAll.title = label;
  histToggleAll.setAttribute("aria-label", label);
}

function setAllBucketsOpen(open) {
  histOpenKeys = open ? null : new Set();
  gallery.querySelectorAll(".bucket").forEach((el) => {
    el.classList.toggle("open", open);
    el.querySelector(".hist")?.setAttribute("aria-expanded", open ? "true" : "false");
  });
  syncHistToggle();
}

function bindHistToolbar() {
  histToggleAll?.addEventListener("click", () => {
    setAllBucketsOpen(!histAnyOpen());
  });
}

function asCoord(v) {
  if (v == null || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

function photosWithGps(photos) {
  return (photos || []).filter((p) => {
    const lat = asCoord(p.lat ?? p.latitude);
    const lon = asCoord(p.lon ?? p.lng ?? p.longitude);
    if (lat == null || lon == null) return false;
    if (Math.abs(lat) < 1e-5 && Math.abs(lon) < 1e-5) return false;
    p.lat = lat;
    p.lon = lon;
    return true;
  });
}

function syncMapBtn() {
  if (!mapBtn) return;
  mapBtn.hidden = !currentPhotos.length || (mapView && !mapView.hidden);
}

function resetLightboxZoom() {
  lb.scale = 1;
  lb.tx = 0;
  lb.ty = 0;
  applyLightboxTransform();
}

function applyLightboxTransform() {
  lightboxImg.style.transform = `translate(${lb.tx}px, ${lb.ty}px) scale(${lb.scale})`;
}

function setLightboxLoading(on) {
  if (!lightboxBusy) return;
  lightboxBusy.hidden = !on;
}

function showLightboxPhoto() {
  resetLightboxZoom();
  const p = lightboxPhotos[lightboxIndex];
  if (!p) return;
  setLightboxLoading(true);
  lightboxImg.dataset.fallback = mediaUrl(p.thumb) || "";
  lightboxImg.src = mediaUrl(p.original);
  lightboxImg.alt = p.place_label || "";
  if (lightboxImg.complete && lightboxImg.naturalWidth) setLightboxLoading(false);
  paintLightboxMeta();
  const next = lightboxPhotos[lightboxIndex + 1];
  const prev = lightboxPhotos[lightboxIndex - 1];
  if (next) {
    const img = new Image();
    img.src = mediaUrl(next.original);
  }
  if (prev) {
    const img = new Image();
    img.src = mediaUrl(prev.original);
  }
}

function paintLightboxMeta() {
  const p = lightboxPhotos[lightboxIndex];
  const n = lightboxPhotos.length;
  if (lightboxCount) {
    lightboxCount.hidden = !n;
    lightboxCount.textContent = n ? `${lightboxIndex + 1} / ${n}` : "";
  }
  if (!lightboxInfo) return;
  lightboxInfo.replaceChildren();
  if (!p) return;
  const lines = photoInfoLines(p);
  const strong = document.createElement("strong");
  strong.textContent = lines[0] || "";
  lightboxInfo.append(strong);
  for (const line of lines.slice(1)) {
    const span = document.createElement("span");
    span.textContent = line;
    lightboxInfo.append(span);
  }
}

function isLightboxOpen() {
  return lightbox && !lightbox.hidden;
}

function galleryUrl() {
  return location.pathname + location.search;
}

function photoHashFor(p) {
  return `#p/${encodeURIComponent(photoKey(p))}`;
}

function parsePhotoHash(hash = location.hash) {
  const m = String(hash || "").match(/^#p\/(.+)$/);
  if (!m) return "";
  try {
    return decodeURIComponent(m[1]);
  } catch {
    return m[1];
  }
}

function findPhotoByKey(key) {
  const want = String(key || "");
  if (!want) return null;
  for (const list of [currentPhotos, albumPhotos, lightboxPhotos]) {
    const hit = (list || []).find((p) => photoKey(p) === want);
    if (hit) return hit;
  }
  return null;
}

let lightboxPushed = false;
let applyingRoute = false;

function pageUrl() {
  return location.pathname + location.search + location.hash;
}

function routeName(state = history.state) {
  const name = state?.fotomob;
  if (name === "connections" || name === "map" || name === "photo" || name === "gallery") return name;
  if (parsePhotoHash()) return "photo";
  return "gallery";
}

function pushView(name) {
  if (history.state?.fotomob === name) return;
  history.pushState({ fotomob: name }, "", pageUrl());
}

function setPhotoHash(p, mode) {
  if (!p) return;
  const url = galleryUrl() + photoHashFor(p);
  if (mode === "push") {
    if (location.hash === photoHashFor(p) && history.state?.fotomob === "photo") {
      lightboxPushed = true;
      return;
    }
    history.pushState({ fotomob: "photo" }, "", url);
    lightboxPushed = true;
    return;
  }
  if (mode === "replace") history.replaceState({ fotomob: "photo" }, "", url);
}

function clearPhotoHash() {
  lightboxPushed = false;
  if (!parsePhotoHash()) return;
  history.replaceState({ fotomob: "gallery" }, "", galleryUrl());
}

function hideLightboxUi() {
  if (!lightbox || lightbox.hidden) return;
  lightbox.hidden = true;
  document.body.classList.remove("lb-open");
  lightboxImg.removeAttribute("src");
  lightboxImg.removeAttribute("data-fallback");
  setLightboxLoading(false);
  if (lightboxCount) {
    lightboxCount.hidden = true;
    lightboxCount.textContent = "";
  }
  resetLightboxZoom();
  lb.fingers = 0;
  lb.pinched = false;
}

function openLightbox(p, { historyMode = "push" } = {}) {
  if (!p) return;
  const already = isLightboxOpen();
  lightboxPhotos = currentPhotos.slice();
  if (!lightboxPhotos.some((x) => photoKey(x) === photoKey(p))) {
    lightboxPhotos = [p, ...lightboxPhotos];
  }
  const idx = lightboxPhotos.findIndex((x) => photoKey(x) === photoKey(p));
  lightboxIndex = idx >= 0 ? idx : 0;
  showLightboxPhoto();
  lightbox.hidden = false;
  document.body.classList.add("lb-open");
  if (historyMode === "none") return;
  setPhotoHash(p, historyMode === "replace" || already ? "replace" : "push");
}

function closeLightbox({ fromPop = false, discard = false } = {}) {
  hideLightboxUi();
  if (fromPop) {
    lightboxPushed = Boolean(parsePhotoHash());
    return;
  }
  if (discard) {
    clearPhotoHash();
    return;
  }
  if (lightboxPushed && parsePhotoHash()) {
    lightboxPushed = false;
    history.back();
    return;
  }
  clearPhotoHash();
}

function restorePhotoHash() {
  if (history.state?.fotomob === "map" || history.state?.fotomob === "connections") return;
  if (document.getElementById("about-dialog")?.open) return;
  if (mapView && !mapView.hidden) return;
  const key = parsePhotoHash();
  if (!key) return;
  const p = findPhotoByKey(key);
  if (!p) return;
  if (isLightboxOpen() && photoKey(lightboxPhotos[lightboxIndex] || {}) === key) return;
  if (!lightboxPushed) {
    history.replaceState({ fotomob: "gallery" }, "", galleryUrl());
    history.pushState({ fotomob: "photo" }, "", galleryUrl() + photoHashFor(p));
    lightboxPushed = true;
  }
  openLightbox(p, { historyMode: "none" });
}

function applyRoute(state = history.state) {
  applyingRoute = true;
  try {
    const view = routeName(state);
    const about = document.getElementById("about-dialog");
    if (view !== "connections") {
      closeQrScan();
      if (about?.open) about.close();
    }
    if (view === "connections") {
      hideMapUi();
      hideLightboxUi();
      if (about && !about.open) {
        renderShareList();
        renderServerList();
        startDevicePoll();
        syncDeviceActions();
        about.showModal();
      }
      return;
    }
    if (view === "map") {
      hideLightboxUi();
      showMapUi();
      return;
    }
    hideMapUi();
    if (view === "photo") {
      const key = parsePhotoHash();
      const p = findPhotoByKey(key);
      if (p) {
        lightboxPushed = true;
        openLightbox(p, { historyMode: "none" });
      } else hideLightboxUi();
      return;
    }
    hideLightboxUi();
    lightboxPushed = false;
  } finally {
    applyingRoute = false;
  }
}

function lightboxNav(dir) {
  if (!lightboxPhotos.length) return;
  lightboxIndex =
    (lightboxIndex + dir + lightboxPhotos.length) % lightboxPhotos.length;
  showLightboxPhoto();
  const p = lightboxPhotos[lightboxIndex];
  if (p) setPhotoHash(p, "replace");
}

function photoPinIcon(photo) {
  return L.divIcon({
    className: "photo-pin",
    html: `<img src="${mediaUrl(photo.thumb)}" alt="" draggable="false" loading="lazy" />`,
    iconSize: [28, 28],
    iconAnchor: [14, 28],
  });
}

function photoClusterIcon(cluster) {
  const kids = cluster.getAllChildMarkers() || [];
  const count = cluster.getChildCount();
  const first = kids[0]?.options?.photo;
  const thumb = first?.thumb
    ? `<img class="photo-cluster-thumb" src="${mediaUrl(first.thumb)}" alt="" loading="lazy" />`
    : "";
  const size = count < 10 ? "small" : count < 100 ? "medium" : "large";
  return L.divIcon({
    className: `photo-cluster photo-cluster-${size}`,
    html: `${thumb}<span class="photo-cluster-count">${count}</span>`,
    iconSize: [32, 32],
    iconAnchor: [16, 32],
  });
}

function ensurePhotoMap() {
  if (photoMap) {
    setTimeout(() => photoMap.invalidateSize(), 80);
    return photoMap;
  }
  photoMap = L.map("photo-map", {
    center: [20, 0],
    zoom: 2,
    worldCopyJump: true,
  });
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    maxZoom: 19,
  }).addTo(photoMap);
  photoMapCluster = L.markerClusterGroup({
    showCoverageOnHover: false,
    maxClusterRadius: (z) => (z <= 6 ? 80 : z <= 12 ? 55 : 40),
    spiderfyOnMaxZoom: true,
    zoomToBoundsOnClick: true,
    iconCreateFunction: photoClusterIcon,
  });
  photoMap.addLayer(photoMapCluster);
  return photoMap;
}

function renderMapPhotos(photos) {
  ensurePhotoMap();
  photoMapCluster.clearLayers();
  const located = photosWithGps(photos);
  const markers = located.map((p) => {
    const marker = L.marker([p.lat, p.lon], {
      icon: photoPinIcon(p),
      photo: p,
    });
    marker.on("click", () => openLightbox(p));
    return marker;
  });
  photoMapCluster.addLayers(markers);
  if (mapStatus) {
    mapStatus.textContent = located.length
      ? `${located.length} with location`
      : "No photos with location";
  }
  setTimeout(() => {
    photoMap.invalidateSize();
    if (markers.length) {
      photoMap.fitBounds(photoMapCluster.getBounds().pad(0.12), { maxZoom: 12 });
    } else {
      photoMap.setView([20, 0], 2);
    }
  }, 80);
}

function mergePhotoCoords(located) {
  const byKey = new Map((located || []).map((p) => [photoKey(p), p]));
  for (const p of currentPhotos) {
    const g = byKey.get(photoKey(p));
    if (!g) continue;
    if (g.lat != null) p.lat = g.lat;
    if (g.lon != null) p.lon = g.lon;
  }
}

function hideMapUi() {
  if (!mapView || mapView.hidden) return;
  mapView.hidden = true;
  document.body.classList.remove("map-open");
  syncMapBtn();
}

function showMapUi() {
  if (typeof L === "undefined") {
    setStatus("Map failed to load.");
    return false;
  }
  const already = Boolean(mapView && !mapView.hidden);
  mapView.hidden = false;
  document.body.classList.add("map-open");
  syncMapBtn();
  if (already) setTimeout(() => photoMap?.invalidateSize(), 80);
  else void loadMapPhotos();
  return true;
}

function openMapView() {
  if (!showMapUi()) return;
  if (isLightboxOpen()) hideLightboxUi();
  pushView("map");
}

async function loadMapPhotos() {
  if (mapStatus) mapStatus.textContent = "Loading locations…";
  setPhotoLoading(true, "Loading locations");
  let photos = currentPhotos;
  if (currentFolder) {
    try {
      const q = new URLSearchParams({
        provider: String(currentFolder.provider || ""),
        collection_id: String(currentFolder.folderId),
      });
      const r = await fetchJson(`/api/map?${q}`);
      if (r.ok && Array.isArray(r.data?.photos)) {
        if (r.data.photos.length) {
          photos = r.data.photos;
          mergePhotoCoords(photos);
        }
      }
    } catch {
      /* keep currentPhotos */
    }
  }
  setPhotoLoading(false);
  const located = photosWithGps(photos);
  if (!located.length) {
    const fallback = photosWithGps(currentPhotos);
    renderMapPhotos(fallback.length ? currentPhotos : photos);
    return;
  }
  renderMapPhotos(photos);
}

function closeMapView() {
  if (applyingRoute) {
    hideMapUi();
    return;
  }
  if (history.state?.fotomob === "map") {
    history.back();
    return;
  }
  hideMapUi();
}

const lb = {
  scale: 1,
  tx: 0,
  ty: 0,
  startScale: 1,
  startTx: 0,
  startTy: 0,
  pinchDist: 1,
  pinchMid: { x: 0, y: 0 },
  startX: 0,
  startY: 0,
  panX: 0,
  panY: 0,
  pinched: false,
  moved: false,
};

function touchPts(e) {
  return [...e.touches].map((t) => ({ x: t.clientX, y: t.clientY }));
}

function pointerDist(a, b) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

function pointerMid(a, b) {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
}

function applyPinch(a, b) {
  const newScale = Math.min(5, Math.max(1, lb.startScale * (pointerDist(a, b) / lb.pinchDist)));
  const mid = pointerMid(a, b);
  const rect = lightboxStage.getBoundingClientRect();
  const cx = rect.left + rect.width / 2;
  const cy = rect.top + rect.height / 2;
  const localX = (lb.pinchMid.x - cx - lb.startTx) / lb.startScale;
  const localY = (lb.pinchMid.y - cy - lb.startTy) / lb.startScale;
  lb.scale = newScale;
  lb.tx = mid.x - cx - localX * newScale;
  lb.ty = mid.y - cy - localY * newScale;
  if (lb.scale <= 1.02) {
    lb.scale = 1;
    lb.tx = 0;
    lb.ty = 0;
  }
  applyLightboxTransform();
}

function lightboxOnTouchStart(e) {
  if (!isLightboxOpen()) return;
  if (e.target.closest("button")) return;
  e.preventDefault();
  const pts = touchPts(e);
  if (pts.length >= 2) {
    lb.pinched = true;
    lb.moved = true;
    lb.startScale = lb.scale;
    lb.startTx = lb.tx;
    lb.startTy = lb.ty;
    lb.pinchDist = pointerDist(pts[0], pts[1]) || 1;
    lb.pinchMid = pointerMid(pts[0], pts[1]);
    return;
  }
  lb.startX = pts[0].x;
  lb.startY = pts[0].y;
  lb.panX = lb.tx;
  lb.panY = lb.ty;
  lb.moved = false;
}

function lightboxOnTouchMove(e) {
  if (!isLightboxOpen()) return;
  if (e.target.closest("button")) return;
  e.preventDefault();
  const pts = touchPts(e);
  if (pts.length >= 2) {
    if (!lb.pinched) {
      lb.pinched = true;
      lb.startScale = lb.scale;
      lb.startTx = lb.tx;
      lb.startTy = lb.ty;
      lb.pinchDist = pointerDist(pts[0], pts[1]) || 1;
      lb.pinchMid = pointerMid(pts[0], pts[1]);
    }
    lb.moved = true;
    applyPinch(pts[0], pts[1]);
    return;
  }
  if (!pts.length) return;
  const dx = pts[0].x - lb.startX;
  const dy = pts[0].y - lb.startY;
  if (Math.hypot(dx, dy) > 8) lb.moved = true;
  if (lb.scale > 1.02) {
    lb.tx = lb.panX + dx;
    lb.ty = lb.panY + dy;
    applyLightboxTransform();
  }
}

function lightboxOnTouchEnd(e) {
  if (!isLightboxOpen()) return;
  if (e.target.closest("button")) return;
  e.preventDefault();
  if (e.touches.length === 1) {
    const t = e.touches[0];
    lb.startX = t.clientX;
    lb.startY = t.clientY;
    lb.panX = lb.tx;
    lb.panY = lb.ty;
    return;
  }
  if (e.touches.length > 0) return;
  if (lb.pinched) {
    lb.pinched = false;
    if (lb.scale <= 1.02) resetLightboxZoom();
    return;
  }
  if (lb.scale > 1.02) return;
  const t = e.changedTouches[0];
  const dx = t.clientX - lb.startX;
  const dy = t.clientY - lb.startY;
  if (Math.abs(dx) > 36 && Math.abs(dx) > Math.abs(dy)) {
    lightboxNav(dx < 0 ? 1 : -1);
  }
}

function mergeTrees(liveTrees, cachedTrees) {
  const byName = new Map();
  for (const t of cachedTrees) byName.set(t.provider || t.name, t);
  for (const t of liveTrees) byName.set(t.provider || t.name, { ...t, stale: false });
  return [...byName.values()].sort((a, b) => String(a.name).localeCompare(String(b.name)));
}

function showTrees(trees, { cached = false, status = "" } = {}) {
  renderTree(trees);
  if (!currentFolder && trees[0]) {
    treeSummary = {
      path: "",
      count: trees[0].count ?? "",
      cached,
      provider: trees[0].provider || trees[0].name || "",
    };
  }
  updatePathBanner();
  if (trees.length) {
    setEmptyScan(false);
    setStatus(status);
    return;
  }
  if (!shareCodes().length) {
    setEmptyScan(true);
    return;
  }
  setEmptyScan(false);
  setStatus(status || "No shared collections for these links.");
}

async function refreshTreeFromNetwork(seq) {
  const r = await fetchJson("/api/tree");
  if (seq !== treeSeq) return currentTrees;
  if (r.ok && Array.isArray(r.data)) {
    liveProviders = r.data.map((t) => t.provider || t.name);
    for (const node of r.data) await saveTree(node);
    if (seq !== treeSeq) return currentTrees;
    const cached = await loadCachedTrees();
    const trees = mergeTrees(r.data, cached);
    showTrees(trees, { cached: false, status: "" });
    return trees;
  }
  liveProviders = [];
  const cached = await loadCachedTrees();
  if (cached.length) {
    showTrees(cached, {
      cached: true,
      status: r.status === 0 ? "Offline — showing last collections." : "No Fotoman connected — showing last collections.",
    });
    return cached;
  }
  if (!shareCodes().length) setEmptyScan(true);
  else setStatus(r.data?.error || "No collections yet.");
  return [];
}

async function loadTree() {
  const seq = ++treeSeq;
  const cached = await loadCachedTrees();
  if (seq !== treeSeq) return currentTrees;
  if (cached.length) {
    showTrees(navigator.onLine ? cached.map((t) => ({ ...t, stale: false })) : cached, {
      cached: true,
      status: navigator.onLine ? "" : "Offline — showing last collections.",
    });
    dismissSplash();
    if (!navigator.onLine) return cached;
    void refreshTreeFromNetwork(seq);
    return cached;
  }
  setStatus("Loading collections…");
  setPhotoLoading(true, "Loading collections");
  try {
    return await refreshTreeFromNetwork(seq);
  } finally {
    if (seq === treeSeq) setPhotoLoading(false);
    dismissSplash();
  }
}

async function refreshAlbumFromNetwork(provider, folderId, name, path, seq) {
  try {
    const q = new URLSearchParams({
      provider: String(provider || ""),
      collection_id: String(folderId),
    });
    const r = await fetchJson(`/api/album?${q}`);
    if (seq !== albumSeq) return;
    if (r.ok) {
      const photos = r.data?.photos || [];
      await saveAlbum(provider, folderId, name, photos);
      if (seq !== albumSeq) return;
      setTreeSummary(path, photos.length);
      if (!photos.length) {
        setStatus("No photos in this collection.");
        gallery.replaceChildren();
        renderGalleryPeople([]);
        setHistToggleVisible(false);
        return;
      }
      setStatus("");
      renderPhotos(photos);
      return;
    }
  } catch {
    /* cache */
  } finally {
    if (seq === albumSeq) setPhotoLoading(false);
  }
  if (seq !== albumSeq) return;
  const cached = await loadCachedAlbum(provider, folderId);
  if (seq !== albumSeq) return;
  if (cached?.photos?.length) {
    setTreeSummary(path, cached.photos.length, true);
    setStatus("Showing last saved collection.");
    if (!albumPhotos.length) renderPhotos(cached.photos);
    return;
  }
  setStatus("Could not load collection.");
}

async function loadAlbum(provider, folderId, name) {
  const seq = ++albumSeq;
  currentFolder = { provider, folderId, name };
  closeSheet();
  renderTree(currentTrees);
  const path = selectedFolderPath() || name || "";
  setTreeSummary(path, "");
  saveSession({ provider, folderId, name, search: false });
  selectedPhotoKey = null;
  resetHistogram();
  syncMapBtn();
  setEmptyScan(false);
  setStatus("");
  const cached = await loadCachedAlbum(provider, folderId);
  if (seq !== albumSeq) return;
  const hasCache = Boolean(cached?.photos?.length);
  if (hasCache) {
    setTreeSummary(path, cached.photos.length, true);
    renderPhotos(cached.photos);
    setPhotoLoading(false);
  } else {
    albumPhotos = [];
    currentPhotos = [];
    gallery.replaceChildren();
    renderGalleryPeople([]);
    setHistToggleVisible(false);
    setPhotoLoading(true);
  }
  const pending = refreshAlbumFromNetwork(provider, folderId, name, path, seq);
  if (hasCache) {
    void pending;
    return;
  }
  await pending;
}

async function runSearch(e) {
  e?.preventDefault();
  if (!hasSearchFilters()) {
    if (currentFolder) {
      closeSearchView();
      await loadAlbum(currentFolder.provider, currentFolder.folderId, currentFolder.name);
    } else {
      setStatus("Find...");
    }
    return;
  }
  closeSearchView();
  const scope = currentFolder;
  const q = searchParams({
    provider: scope?.provider,
    folderId: scope?.folderId,
  });
  currentPhotos = [];
  albumPhotos = [];
  selectedPhotoKey = null;
  resetHistogram();
  syncMapBtn();
  gallery.replaceChildren();
  renderGalleryPeople([]);
  setHistToggleVisible(false);
  const path = scope
    ? selectedFolderPath() || scope.name || ""
    : "Search";
  setTreeSummary(path, "");
  setStatus("");
  setPhotoLoading(true, "Searching");
  saveSession({ search: true, q: qEl.value });
  try {
    const r = await fetchJson(`/api/search?${q}`, 35000);
    if (r.ok) {
      const photos = r.data.photos || [];
      await Promise.all(photos.map((p) => upsertPhoto(p)));
      setTreeSummary(path, photos.length);
      if (!photos.length) {
        setStatus("No matching photos.");
        return;
      }
      setStatus("");
      renderPhotos(photos);
      return;
    }
  } catch {
    /* offline */
  } finally {
    setPhotoLoading(false);
  }
  setStatus("Describe search needs a connected Fotoman.");
}

function fillPeopleCatalog(groups) {
  peopleByKey = new Map();
  for (const g of groups) {
    for (const p of g.people || []) {
      peopleByKey.set(`${g.provider}:${p.id}`, { ...p, provider: g.provider });
    }
  }
  renderGalleryPeople(albumPhotos.length ? albumPhotos : currentPhotos);
}

async function refreshPeople() {
  const cached = await loadCachedPeople();
  try {
    const r = await fetchJson("/api/people");
    if (r.ok && Array.isArray(r.data?.groups)) {
      for (const g of r.data.groups) await savePeople(g.provider, g.people);
      fillPeopleCatalog(r.data.groups);
      return;
    }
  } catch {
    /* cache */
  }
  if (cached.length) {
    fillPeopleCatalog(cached.map((g) => ({ provider: g.provider, people: g.people || [] })));
  }
}

async function loadPeople() {
  const cached = await loadCachedPeople();
  if (cached.length) {
    fillPeopleCatalog(cached.map((g) => ({ provider: g.provider, people: g.people || [] })));
    if (!navigator.onLine) return;
    void refreshPeople();
    return;
  }
  await refreshPeople();
}

lightboxImg.addEventListener("load", () => setLightboxLoading(false));
lightboxImg.addEventListener("error", () => {
  const fallback = lightboxImg.dataset.fallback;
  if (fallback && lightboxImg.src !== fallback) {
    lightboxImg.removeAttribute("data-fallback");
    lightboxImg.src = fallback;
    return;
  }
  setLightboxLoading(false);
});
const touchOpts = { passive: false };
lightbox.addEventListener("touchstart", lightboxOnTouchStart, touchOpts);
lightbox.addEventListener("touchmove", lightboxOnTouchMove, touchOpts);
lightbox.addEventListener("touchend", lightboxOnTouchEnd, touchOpts);
lightbox.addEventListener("touchcancel", lightboxOnTouchEnd, touchOpts);
lightbox.addEventListener("gesturestart", (e) => e.preventDefault());
lightbox.addEventListener("gesturechange", (e) => e.preventDefault());
lightbox.addEventListener("gestureend", (e) => e.preventDefault());

function preventGalleryZoom(e) {
  if (isLightboxOpen()) return;
  if (e.touches && e.touches.length > 1) e.preventDefault();
}
document.addEventListener("touchmove", preventGalleryZoom, { passive: false });
document.addEventListener("gesturestart", (e) => {
  if (!isLightboxOpen()) e.preventDefault();
}, { passive: false });
document.addEventListener("gesturechange", (e) => {
  if (!isLightboxOpen()) e.preventDefault();
}, { passive: false });
document.addEventListener("gestureend", (e) => {
  if (!isLightboxOpen()) e.preventDefault();
}, { passive: false });
lightboxMapBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  openMapView();
});
lightboxBackBtn?.addEventListener("click", (e) => {
  e.stopPropagation();
  closeLightbox();
});
mapBtn.addEventListener("click", openMapView);
mapBackBtn?.addEventListener("click", closeMapView);
bindHistToolbar();

function isSearchOpen() {
  return Boolean(form && !form.hidden);
}

function openSearchView() {
  if (!form) return;
  closeSheet();
  if (mapView && !mapView.hidden) closeMapView();
  form.hidden = false;
  form.setAttribute("aria-hidden", "false");
  document.body.classList.add("search-open");
  searchBtn?.classList.add("on");
  searchBtn?.setAttribute("aria-pressed", "true");
  qEl?.focus();
}

function closeSearchView() {
  if (!form || form.hidden) return;
  form.hidden = true;
  form.setAttribute("aria-hidden", "true");
  document.body.classList.remove("search-open");
  searchBtn?.classList.remove("on");
  searchBtn?.setAttribute("aria-pressed", "false");
}

function toggleSearchView() {
  if (isSearchOpen()) closeSearchView();
  else openSearchView();
}

form?.addEventListener("submit", runSearch);
searchBtn?.addEventListener("click", toggleSearchView);
qEl?.addEventListener("blur", () => {
  setTimeout(() => {
    if (!isSearchOpen()) return;
    if (form.contains(document.activeElement)) return;
    closeSearchView();
  }, 80);
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (isLightboxOpen()) {
    e.preventDefault();
    closeLightbox();
    return;
  }
  if (mapView && !mapView.hidden) {
    e.preventDefault();
    closeMapView();
    return;
  }
  if (isSheetOpen()) {
    e.preventDefault();
    closeSheet();
    return;
  }
  if (isSearchOpen()) {
    e.preventDefault();
    closeSearchView();
  }
});
window.addEventListener("popstate", () => applyRoute());
pathBar?.addEventListener("click", (e) => {
  if (e.target.closest(".gallery-people")) return;
  toggleSheet();
});
shareOutMore?.addEventListener("click", () => {
  shareOutNative();
});
sheetDone?.addEventListener("click", closeSheet);
sheetScrim?.addEventListener("click", closeSheet);

function isAppInstalled() {
  return (
    window.matchMedia("(display-mode: standalone)").matches ||
    window.matchMedia("(display-mode: fullscreen)").matches ||
    window.matchMedia("(display-mode: minimal-ui)").matches ||
    window.matchMedia("(display-mode: window-controls-overlay)").matches ||
    navigator.standalone === true
  );
}

function hideInstallBtn() {
  installPrompt = null;
  installBtn.hidden = true;
}

window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  if (isAppInstalled()) {
    hideInstallBtn();
    return;
  }
  installPrompt = e;
  installBtn.hidden = false;
});

window.addEventListener("appinstalled", hideInstallBtn);
window.matchMedia("(display-mode: standalone)").addEventListener("change", (e) => {
  if (e.matches) hideInstallBtn();
});

installBtn.addEventListener("click", async () => {
  if (!installPrompt) {
    hideInstallBtn();
    return;
  }
  try {
    await installPrompt.prompt();
    await installPrompt.userChoice.catch(() => {});
  } catch {
    /* already used or not available */
  }
  hideInstallBtn();
});

if (isAppInstalled()) hideInstallBtn();

if ("serviceWorker" in navigator) {
  let swReloadArmed = Boolean(navigator.serviceWorker.controller);
  navigator.serviceWorker
    .register(`${BASE}/sw.js`, { updateViaCache: "none" })
    .then((reg) => {
      reg.update().catch(() => {});
    })
    .catch(() => {});
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (!swReloadArmed) {
      swReloadArmed = true;
      return;
    }
    const reload = () => {
      document.removeEventListener("visibilitychange", onHide);
      location.reload();
    };
    const onHide = () => {
      if (document.visibilityState === "hidden") reload();
    };
    if (document.visibilityState === "hidden") reload();
    else document.addEventListener("visibilitychange", onHide);
  });
}

const aboutBtn = document.getElementById("btn-about");
const aboutDialog = document.getElementById("about-dialog");

aboutBtn?.addEventListener("click", () => {
  closeSheet();
  closeSearchView();
  renderShareList();
  renderServerList();
  startDevicePoll();
  syncDeviceActions();
  aboutDialog?.showModal();
  pushView("connections");
});
document.getElementById("about-back")?.addEventListener("click", () => {
  if (history.state?.fotomob === "connections") history.back();
  else aboutDialog?.close();
});
aboutDialog?.addEventListener("close", () => {
  stopDevicePoll();
  if (applyingRoute) return;
  if (history.state?.fotomob === "connections") history.back();
});
aboutDialog?.querySelector("form")?.addEventListener("submit", (e) => {
  e.preventDefault();
});

document.querySelectorAll("[data-qr-scan]").forEach((btn) => {
  btn.addEventListener("click", (e) => {
    e.preventDefault();
    openQrScan();
  });
});
document.getElementById("qr-scan-close")?.addEventListener("click", (e) => {
  e.preventDefault();
  closeQrScan();
});
document.getElementById("qr-scan")?.addEventListener("close", stopQrCamera);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden" && document.getElementById("qr-scan")?.open) {
    closeQrScan();
  }
});

const DEVICE_KEY = "fotomob.device";
const SERVERS_KEY = "fotomob.servers";
const SERVER_SEL_KEY = "fotomob.server";
const devicePickBtn = document.getElementById("device-pick-btn");
const deviceUploadBtn = document.getElementById("device-upload-btn");
const deviceFolderEl = document.getElementById("device-folder");
const deviceFilesEl = document.getElementById("device-files");
const deviceStatusEl = document.getElementById("device-status");

const IMAGE_EXTS = /\.(jpe?g|png|webp|tiff?|bmp|heic|heif)$/i;
const deviceFiles = new Map();
let deviceBusy = false;
let livePairStatus = new Map();
let devicePollId = 0;
let holdDeviceStatus = false;

function setDeviceStatus(msg, opts = {}) {
  if (deviceStatusEl) deviceStatusEl.textContent = msg || "";
  if ("hold" in opts) holdDeviceStatus = Boolean(opts.hold);
}

function normalizeServer(row) {
  const pair = normalizeDeviceToken(typeof row === "string" ? row : row?.pair);
  if (!pair) return null;
  return {
    pair,
    provider: String(row?.provider || "").trim(),
    label: String(row?.label || "").trim(),
  };
}

function loadServers() {
  try {
    const raw = localStorage.getItem(SERVERS_KEY);
    if (raw != null) {
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed.map(normalizeServer).filter(Boolean) : [];
    }
  } catch {
    /* ignore */
  }
  try {
    const legacy = String(localStorage.getItem(DEVICE_KEY) || "").trim();
    const pair = normalizeDeviceToken(legacy);
    if (pair) {
      const servers = [{ pair, provider: "", label: "" }];
      saveServers(servers);
      saveSelectedServer(pair);
      return servers;
    }
  } catch {
    /* ignore */
  }
  return [];
}

function saveServers(servers) {
  const next = [];
  const seen = new Set();
  for (const row of servers || []) {
    const server = normalizeServer(row);
    if (!server || seen.has(server.pair)) continue;
    seen.add(server.pair);
    next.push(server);
  }
  try {
    localStorage.setItem(SERVERS_KEY, JSON.stringify(next));
  } catch {
    /* ignore */
  }
  return next;
}

function loadSelectedServer() {
  try {
    const token = normalizeDeviceToken(localStorage.getItem(SERVER_SEL_KEY) || "");
    if (token && loadServers().some((s) => s.pair === token)) return token;
  } catch {
    /* ignore */
  }
  return loadServers()[0]?.pair || "";
}

function saveSelectedServer(pair) {
  const token = normalizeDeviceToken(pair);
  try {
    if (token) localStorage.setItem(SERVER_SEL_KEY, token);
    else localStorage.removeItem(SERVER_SEL_KEY);
  } catch {
    /* ignore */
  }
  return token;
}

function addServer(pair, extra = {}) {
  const token = normalizeDeviceToken(pair);
  if (!token) return loadServers();
  const servers = loadServers();
  const i = servers.findIndex((s) => s.pair === token);
  const next = { pair: token, provider: "", label: "", ...(i >= 0 ? servers[i] : {}), ...extra };
  if (i >= 0) servers[i] = next;
  else servers.push(next);
  return saveServers(servers);
}

function removeServer(pair) {
  const token = normalizeDeviceToken(pair);
  const next = saveServers(loadServers().filter((s) => s.pair !== token));
  let selected = "";
  try {
    selected = normalizeDeviceToken(localStorage.getItem(SERVER_SEL_KEY) || "");
  } catch {
    /* ignore */
  }
  if (!next.some((s) => s.pair === selected)) saveSelectedServer(next[0]?.pair || "");
  return next;
}

function selectServer(pair) {
  const token = normalizeDeviceToken(pair);
  if (token) addServer(token);
  return saveSelectedServer(token);
}

function serverDisplayName(server, servers) {
  const name = String(server?.provider || "").trim() || "Fotoman";
  const same = (servers || []).filter(
    (s) => (String(s.provider || "").trim() || "Fotoman") === name
  );
  if (same.length > 1) {
    const extra = String(server?.label || "").trim() || String(server?.pair || "").slice(0, 6);
    return extra ? `${name} · ${extra}` : name;
  }
  return name;
}

function renderServerList() {
  const list = document.getElementById("server-list");
  if (!list) return;
  const servers = loadServers();
  const selected = loadSelectedServer();
  list.replaceChildren();
  if (!servers.length) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = "No paired servers yet. Scan a QR.";
    list.append(empty);
    return;
  }
  for (const server of servers) {
    const li = document.createElement("li");
    const isSelected = server.pair === selected;
    const live = livePairStatus.get(server.pair);
    const online = Boolean(live?.online);
    li.classList.toggle("online", online);
    li.classList.toggle("offline", !online);
    if (isSelected) li.classList.add("selected");
    const pick = document.createElement("button");
    pick.type = "button";
    pick.className = "server-pick";
    pick.setAttribute("aria-pressed", isSelected ? "true" : "false");
    const name = document.createElement("span");
    name.className = "server-pick-name";
    name.textContent = serverDisplayName(server, servers);
    const meta = document.createElement("span");
    meta.className = "server-pick-meta";
    if (online) meta.textContent = isSelected ? "Online · selected" : "Online";
    else meta.textContent = "Offline";
    pick.append(name, meta);
    pick.addEventListener("click", (e) => {
      e.preventDefault();
      selectProvider(server.pair);
    });
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "share-list-remove";
    btn.textContent = "\u00d7";
    btn.title = "Remove server";
    btn.setAttribute("aria-label", `Remove ${serverDisplayName(server, servers)}`);
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      const name = serverDisplayName(server, servers);
      if (!confirm(`Remove “${name}” from this phone?`)) return;
      forgetServer(server.pair);
    });
    li.append(pick, btn);
    list.append(li);
  }
}

function selectProvider(pair) {
  selectServer(pair);
  holdDeviceStatus = false;
  renderServerList();
  syncDeviceActions();
  void refreshDeviceStatus();
}

function forgetServer(pair) {
  removeServer(pair);
  renderServerList();
  syncDeviceActions();
  if (!loadSelectedServer()) setDeviceStatus("", { hold: false });
}

function pairIsOnline(pair) {
  const token = normalizeDeviceToken(pair);
  return Boolean(token && livePairStatus.get(token)?.online);
}

function syncDeviceActions() {
  const selected = loadSelectedServer();
  const online = pairIsOnline(selected);
  if (devicePickBtn) devicePickBtn.disabled = !selected || deviceBusy;
  if (deviceUploadBtn) deviceUploadBtn.disabled = !online || deviceBusy || !deviceFiles.size;
}

function stopDevicePoll() {
  if (devicePollId) {
    clearInterval(devicePollId);
    devicePollId = 0;
  }
}

function startDevicePoll() {
  stopDevicePoll();
  void refreshDeviceStatus();
  devicePollId = setInterval(() => void refreshDeviceStatus(), 5000);
}

async function refreshDeviceStatus() {
  const servers = loadServers();
  if (!servers.length) {
    livePairStatus.clear();
    renderServerList();
    syncDeviceActions();
    if (!deviceBusy && !holdDeviceStatus) setDeviceStatus("");
    return;
  }
  const q = new URLSearchParams();
  for (const s of servers) q.append("pair", s.pair);
  try {
    const r = await fetch(apiPath(`/api/device/status?${q}`), { cache: "no-store" });
    const data = await r.json();
    livePairStatus.clear();
    for (const row of data.pairs || []) {
      livePairStatus.set(row.pair, row);
      if (row.provider) addServer(row.pair, { provider: row.provider, label: row.label || "" });
    }
  } catch {
    /* keep last status */
  }
  renderServerList();
  syncDeviceActions();
  if (deviceBusy || holdDeviceStatus) return;
  const selected = loadSelectedServer();
  if (!selected) setDeviceStatus("");
  else if (pairIsOnline(selected)) {
    const n = deviceFiles.size;
    setDeviceStatus(n ? `${n} selected — tap Upload` : "Select photos, then upload");
  } else {
    setDeviceStatus("Fotoman is offline");
  }
}

function folderName() {
  return String(deviceFolderEl?.value || "").trim();
}

function relFor(file) {
  const folder = folderName();
  if (folder) return `${folder}/${file.name}`;
  return file.webkitRelativePath || file.name;
}

function normalizeDeviceToken(raw) {
  const text = String(raw || "").trim();
  if (!text) return "";
  try {
    const u = new URL(text, location.origin);
    const fromQuery = u.searchParams.get("device");
    if (fromQuery) return fromQuery.trim();
    const fromPath = tokenFromAppPath("d", u);
    if (fromPath) return fromPath;
  } catch {
    /* not a URL */
  }
  return text;
}

function consumeDeviceFromUrl() {
  const url = new URL(location.href);
  const incoming = url.searchParams.get("device") || tokenFromAppPath("d", url);
  if (!incoming) return loadSelectedServer();
  const token = normalizeDeviceToken(incoming);
  if (token) selectServer(token);
  return token || loadSelectedServer();
}

function assetIdFor(file, i) {
  const raw = `${file.name}__${file.size}__${file.lastModified}__${i}`;
  return raw.replace(/[^A-Za-z0-9._-]+/g, "_");
}

function bufToB64(buf) {
  const bytes = new Uint8Array(buf);
  let s = "";
  const chunk = 0x2000;
  for (let i = 0; i < bytes.length; i += chunk) {
    s += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(s);
}

async function postDeviceFile(pair, file, i, finalize) {
  const buf = await file.arrayBuffer();
  const r = await fetch(apiPath("/api/device/put"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      pair,
      folder: folderName(),
      finalize,
      file: {
        id: file._assetId || assetIdFor(file, i),
        name: file.name,
        rel: relFor(file),
        size: file.size,
        mtime: file.lastModified / 1000,
        type: file.type || "",
        body: bufToB64(buf),
      },
    }),
  });
  let data = null;
  try {
    data = await r.json();
  } catch {
    data = null;
  }
  if (!r.ok) throw new Error(data?.error || `Upload failed (${r.status})`);
  if (data?.provider) addServer(pair, { provider: data.provider });
  const code = String(data?.share_code || "").trim();
  if (code) addShareCode(code, data?.collection_name || folderName());
  return data;
}

async function uploadSelected() {
  const pair = loadSelectedServer();
  if (!pair) {
    setDeviceStatus("Select a Fotoman first");
    return;
  }
  if (!pairIsOnline(pair)) {
    setDeviceStatus("Fotoman is offline");
    return;
  }
  const files = [...deviceFiles.values()];
  if (!files.length) {
    setDeviceStatus("Select photos first");
    return;
  }
  deviceBusy = true;
  syncDeviceActions();
  setDeviceBusy(true, "Uploading");
  setPhotoLoading(true, "Uploading");
  try {
    let shareCode = "";
    for (let i = 0; i < files.length; i++) {
      setDeviceStatus(`Uploading ${i + 1}/${files.length}…`);
      const data = await postDeviceFile(pair, files[i], i, i === files.length - 1);
      if (data?.share_code) shareCode = String(data.share_code).trim();
    }
    deviceFiles.clear();
    if (deviceFilesEl) deviceFilesEl.value = "";
    const folder = folderName();
    setDeviceStatus(
      `Uploaded ${files.length}${folder ? ` to ${folder}` : ""}. You can close this page.`,
      { hold: true }
    );
    if (shareCode) {
      renderShareList();
      await clearShareCache();
      await loadTree();
      document.getElementById("about-dialog")?.close();
      await openFolderForShareCode(shareCode);
    }
  } catch (err) {
    setDeviceStatus(err.message || "Upload failed", { hold: false });
  } finally {
    deviceBusy = false;
    setDeviceBusy(false);
    setPhotoLoading(false);
    syncDeviceActions();
    void refreshDeviceStatus();
  }
}

devicePickBtn?.addEventListener("click", (e) => {
  e.preventDefault();
  deviceFilesEl?.click();
});

deviceUploadBtn?.addEventListener("click", (e) => {
  e.preventDefault();
  void uploadSelected();
});

deviceFilesEl?.addEventListener("change", () => {
  const picked = [...(deviceFilesEl.files || [])].filter((f) => IMAGE_EXTS.test(f.name));
  deviceFiles.clear();
  picked.forEach((file, i) => {
    file._assetId = assetIdFor(file, i);
    deviceFiles.set(file._assetId, file);
  });
  syncDeviceActions();
  if (!picked.length) {
    setDeviceStatus("No photos selected", { hold: false });
    return;
  }
  setDeviceStatus(`${picked.length} selected — tap Upload`, { hold: false });
});

function launchShareTokens(url) {
  const u = url instanceof URL ? url : new URL(String(url || location.href), location.origin);
  const incoming = [];
  for (const value of u.searchParams.getAll("share")) {
    incoming.push(...String(value).split(/[,\s]+/));
  }
  const fromPath = tokenFromAppPath("s", u);
  if (fromPath) incoming.push(fromPath);
  return incoming.map(normalizeShareToken).filter(Boolean);
}

async function openFolderForShareCode(code) {
  const folder = folderForShareCode(code);
  if (!folder || folder.kind === "provider") return false;
  if (folder.name) addShareCode(code, folder.name);
  renderShareList();
  await loadAlbum(folder.provider, folder.id, folder.name);
  return true;
}

window.addEventListener("online", () => {
  void loadTree();
  void loadPeople();
  if (currentFolder) {
    void loadAlbum(currentFolder.provider, currentFolder.folderId, currentFolder.name);
  }
});

(async () => {
  updatePathBanner();
  const launch = new URL(location.href);
  const launchShares = launchShareTokens(launch);
  const hadLaunch = Boolean(
    launchShares.length ||
      launch.searchParams.get("device") ||
      tokenFromAppPath("d", launch)
  );
  const launchPair = consumeDeviceFromUrl();
  const before = shareCodes().join("\0");
  consumeShareFromUrl();
  if (hadLaunch) cleanAppUrl(launch);
  renderServerList();
  syncDeviceActions();
  if (launchPair) void refreshDeviceStatus();
  if (shareCodes().join("\0") !== before) await clearShareCache();
  const session = loadSession();
  if (session.q) qEl.value = session.q;
  await loadTree();
  void loadPeople();
  for (const code of launchShares) {
    if (await openFolderForShareCode(code)) return;
  }
  if (session.search && hasSearchFilters()) {
    await runSearch();
    return;
  }
  if (session.provider && session.folderId) {
    const exists = folderInTrees(currentTrees, session.provider, session.folderId);
    if (exists) await loadAlbum(session.provider, session.folderId, session.name || "");
  }
})();
