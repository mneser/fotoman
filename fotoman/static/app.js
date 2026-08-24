const $ = (sel) => document.querySelector(sel);

function loadingMarkup(label = "Loading", size = "") {
  // Default matches the regroup/recluster busy-mark; sm/inline are compact.
  if (!size) {
    return `<div class="busy-mark" role="status" aria-label="${label}">
    <img class="busy-mark-img" src="/fotoman.png" alt="" draggable="false" />
    <div class="busy-spinner" aria-hidden="true">
      <img src="/fotoman.png" alt="" draggable="false" />
    </div>
  </div>`;
  }
  const sizeClass = ` loading-mark-${size}`;
  return `<div class="loading-mark${sizeClass}" role="status" aria-label="${label}">
    <img class="loading-mark-img" src="/fotoman.png" alt="" draggable="false" />
    <div class="busy-spinner" aria-hidden="true">
      <img src="/fotoman.png" alt="" draggable="false" />
    </div>
  </div>`;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (!res.ok) {
    const text = await res.text();
    let msg = text || res.statusText;
    try {
      const j = JSON.parse(text);
      if (typeof j?.detail === "string") msg = j.detail;
      else if (Array.isArray(j?.detail)) {
        msg = j.detail.map((d) => d.msg || JSON.stringify(d)).join("; ");
      } else if (j?.detail?.code === "name_conflict") {
        msg = "A folder with that name already exists";
      }
    } catch (_) { /* plain text */ }
    throw new Error(msg);
  }
  if (res.status === 204) return null;
  return res.json();
}

function basename(p) {
  return (p || "").split("/").pop();
}

function orientationDeg(p) {
  const d = Number(p?.orientation ?? p);
  return d === 90 || d === 180 || d === 270 ? d : 0;
}

function isMirrored(p) {
  if (p == null || typeof p !== "object") return Boolean(p);
  return Boolean(p.mirrored ?? p.mirror);
}

function orientationStyle(p) {
  const d = orientationDeg(p);
  const mirror = isMirrored(p);
  const parts = [];
  if (d) parts.push(`rotate(${d}deg)`);
  if (mirror) parts.push("scaleX(-1)");
  return parts.length ? `transform:${parts.join(" ")}` : "";
}

function setLightboxTransform(deg, mirrored = false) {
  const img = $("#lightbox-img");
  const stage = img?.closest(".lightbox-stage") || img;
  if (!stage) return;
  const d = orientationDeg(deg);
  const parts = [];
  if (d) parts.push(`rotate(${d}deg)`);
  if (mirrored) parts.push("scaleX(-1)");
  stage.style.transform = parts.join(" ") || "";
}

const LIGHTBOX_ZOOM_MIN = 1;
const LIGHTBOX_ZOOM_MAX = 8;
const VQA_KEY_MASK = "••••••••";
let lightboxZoom = 1;
let lightboxPanX = 0;
let lightboxPanY = 0;
let lightboxPanDrag = null;

function lightboxZoomLayout() {
  const img = $("#lightbox-img");
  const stage = img?.closest(".lightbox-stage");
  const wrap = stage?.closest(".lightbox-stage-wrap");
  if (!img || !stage || !wrap) return null;
  const nw = img.naturalWidth;
  const nh = img.naturalHeight;
  const ww = wrap.clientWidth;
  const wh = wrap.clientHeight;
  if (!nw || !nh || !ww || !wh) return null;
  const contain = Math.min(ww / nw, wh / nh);
  const cover = Math.max(ww / nw, wh / nh);
  const zoomed = lightboxZoom > 1.001;
  // Continuous zoom from contain; past cover/contain the long axis crops.
  const fit = contain * lightboxZoom;
  return {
    img,
    stage,
    wrap,
    nw,
    nh,
    ww,
    wh,
    contain,
    cover,
    fit,
    zoomed,
    iw: nw * fit,
    ih: nh * fit,
  };
}

function clampLightboxPan(layout = lightboxZoomLayout()) {
  if (!layout?.zoomed) {
    lightboxPanX = 0;
    lightboxPanY = 0;
    return;
  }
  const maxX = Math.max(0, (layout.iw - layout.ww) / 2);
  const maxY = Math.max(0, (layout.ih - layout.wh) / 2);
  lightboxPanX = Math.max(-maxX, Math.min(maxX, lightboxPanX));
  lightboxPanY = Math.max(-maxY, Math.min(maxY, lightboxPanY));
}

function applyLightboxZoomPan() {
  const layout = lightboxZoomLayout();
  const img = layout?.img || $("#lightbox-img");
  const faces = $("#lightbox-faces");
  const stage = layout?.stage || img?.closest(".lightbox-stage");
  const zoomed = !!layout?.zoomed;
  if (layout) clampLightboxPan(layout);
  if (img) {
    if (zoomed && layout) {
      img.style.width = `${layout.iw}px`;
      img.style.height = `${layout.ih}px`;
      img.style.maxWidth = "none";
      img.style.maxHeight = "none";
      img.style.transform = `translate(${lightboxPanX}px, ${lightboxPanY}px)`;
    } else {
      img.style.width = "";
      img.style.height = "";
      img.style.maxWidth = "";
      img.style.maxHeight = "";
      img.style.transform = "";
    }
  }
  if (faces) {
    if (zoomed && layout && img) {
      faces.style.width = `${layout.iw}px`;
      faces.style.height = `${layout.ih}px`;
      faces.style.left = `${img.offsetLeft}px`;
      faces.style.top = `${img.offsetTop}px`;
      faces.style.right = "auto";
      faces.style.bottom = "auto";
      faces.style.marginLeft = "";
      faces.style.marginTop = "";
      faces.style.transform = `translate(${lightboxPanX}px, ${lightboxPanY}px)`;
    } else {
      faces.style.transform = "";
      faces.style.right = "";
      faces.style.bottom = "";
      faces.style.marginLeft = "";
      faces.style.marginTop = "";
      if (img && !faces.hidden) syncLightboxFaceLayer(faces, img);
      else {
        faces.style.width = "";
        faces.style.height = "";
        faces.style.left = "";
        faces.style.top = "";
      }
    }
  }
  if (stage) {
    stage.classList.toggle("is-zoomed", zoomed);
    stage.classList.toggle("is-panning", !!lightboxPanDrag);
  }
  syncLightboxZoomHud(layout);
}

function formatLightboxZoomScale(z) {
  const rounded = Math.round(z * 10) / 10;
  return Number.isInteger(rounded) ? `${rounded}×` : `${rounded.toFixed(1)}×`;
}

function syncLightboxZoomHud(layout = lightboxZoomLayout()) {
  const hud = $("#lightbox-zoom-hud");
  const scaleEl = $("#lightbox-zoom-scale");
  const thumb = $("#lightbox-zoom-thumb");
  const roi = $("#lightbox-zoom-roi");
  const img = layout?.img || $("#lightbox-img");
  const stage = layout?.stage || img?.closest(".lightbox-stage");
  if (!hud || !scaleEl || !thumb || !roi || !img || !stage) return;

  const zoomed = !!layout?.zoomed;
  hud.hidden = !zoomed;
  if (!zoomed || !layout) return;

  const wrap = layout.wrap;
  if (wrap) {
    const sr = stage.getBoundingClientRect();
    const wr = wrap.getBoundingClientRect();
    hud.style.left = `${Math.max(0, sr.left - wr.left) + 8}px`;
    hud.style.top = `${Math.max(0, sr.top - wr.top) + 8}px`;
  }

  scaleEl.textContent = formatLightboxZoomScale(lightboxZoom);
  if (thumb.getAttribute("src") !== img.getAttribute("src")) {
    thumb.src = img.currentSrc || img.src || "";
  }

  const { iw, ih, ww: sw, wh: sh } = layout;
  if (!iw || !ih || !sw || !sh) return;

  const left = (iw / 2 + (-sw / 2 - lightboxPanX)) / iw;
  const top = (ih / 2 + (-sh / 2 - lightboxPanY)) / ih;
  const width = sw / iw;
  const height = sh / ih;
  const x0 = Math.max(0, Math.min(1, left));
  const y0 = Math.max(0, Math.min(1, top));
  const x1 = Math.max(0, Math.min(1, left + width));
  const y1 = Math.max(0, Math.min(1, top + height));
  roi.style.left = `${x0 * 100}%`;
  roi.style.top = `${y0 * 100}%`;
  roi.style.width = `${Math.max(0.02, x1 - x0) * 100}%`;
  roi.style.height = `${Math.max(0.02, y1 - y0) * 100}%`;
}

function resetLightboxZoomPan() {
  lightboxZoom = 1;
  lightboxPanX = 0;
  lightboxPanY = 0;
  lightboxPanDrag = null;
  applyLightboxZoomPan();
}

function lightboxPanToNorm(nx, ny) {
  const layout = lightboxZoomLayout();
  if (!layout?.zoomed) return;
  const x = Math.max(0, Math.min(1, nx));
  const y = Math.max(0, Math.min(1, ny));
  lightboxPanX = -(x - 0.5) * layout.iw;
  lightboxPanY = -(y - 0.5) * layout.ih;
  applyLightboxZoomPan();
}

function lightboxZoomNavNormFromEvent(e, nav) {
  const rect = nav.getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  return {
    x: (e.clientX - rect.left) / rect.width,
    y: (e.clientY - rect.top) / rect.height,
  };
}

function lightboxZoomAt(clientX, clientY, nextZoom) {
  const prev = lightboxZoomLayout();
  if (!prev) return;
  const z = Math.min(LIGHTBOX_ZOOM_MAX, Math.max(LIGHTBOX_ZOOM_MIN, nextZoom));
  if (z <= LIGHTBOX_ZOOM_MIN) {
    resetLightboxZoomPan();
    return;
  }
  const rect = prev.stage.getBoundingClientRect();
  const cx = clientX - rect.left - rect.width / 2;
  const cy = clientY - rect.top - rect.height / 2;
  const prevFit = prev.zoomed ? prev.fit : prev.contain;
  const contentX = (cx - lightboxPanX) / prevFit;
  const contentY = (cy - lightboxPanY) / prevFit;
  lightboxZoom = z;
  const next = lightboxZoomLayout();
  if (!next) return;
  lightboxPanX = cx - contentX * next.fit;
  lightboxPanY = cy - contentY * next.fit;
  applyLightboxZoomPan();
}

async function refreshStats() {
  const s = await api("/api/stats");
  $("#stats").textContent = `${s.photos} photos · ${s.embedded} embedded`;
}

async function removePhotoIds(ids) {
  if (!ids.length) return { removed: 0 };
  return api("/api/duplicates/remove", {
    method: "POST",
    body: JSON.stringify({ photo_ids: ids }),
  });
}

async function restorePhotoIds(ids) {
  if (!ids.length) return { restored: 0 };
  return api("/api/duplicates/restore", {
    method: "POST",
    body: JSON.stringify({ photo_ids: ids }),
  });
}

async function deletePhotoIds(ids) {
  if (!ids.length) return { deleted: 0 };
  return api("/api/duplicates/delete", {
    method: "POST",
    body: JSON.stringify({ photo_ids: ids }),
  });
}

function ensureBrowseGalleryVisible() {
  browseGalleryView = "thumbs";
  activateTab("browse", { workspaceView: "stack" });
}

function restoreBrowseGalleryFromSearch() {
  browseSearchActive = false;
  browseItems = browseItemsFull.slice();
  browseSelected.clear();
  browseAnchorKey = null;
  if (mapRegionActive || mapSelectRect || mapRegionBounds) {
    clearMapRegionSelection();
  }
  renderBrowseResults();
}

async function runSearch(e) {
  e.preventDefault();
  ensureBrowseGalleryVisible();
  const q = $("#q").value.trim();
  // Empty describe restores the open folder/group. Tree scopes place/camera/date.
  if (!q) {
    restoreBrowseGalleryFromSearch();
    return;
  }
  const scope = browseItemsFull.length ? browseItemsFull : browseItems;
  if (!scope.length) {
    $("#browse-results").innerHTML = `<p class="muted">Select items in the tree to search.</p>`;
    return;
  }
  const params = new URLSearchParams();
  params.set("mode", "visual");
  params.set("q", q);
  if (includeCulled()) params.set("include_culled", "true");
  const ids = [
    ...new Set(scope.map((p) => p.content_id).filter((id) => id != null)),
  ];
  if (!ids.length) {
    $("#browse-results").innerHTML = `<p class="muted">No indexed photos in this selection to search.</p>`;
    return;
  }
  params.set("content_ids", ids.join(","));
  params.set("top_k", String(Math.min(200, Math.max(48, ids.length))));
  const results = $("#browse-results");
  results.innerHTML = `<p class="muted">Searching…</p>`;
  try {
    const hits = await api(`/api/search?${params}`);
    const byContent = new Map();
    for (const p of scope) {
      if (p.content_id == null || byContent.has(p.content_id)) continue;
      byContent.set(p.content_id, p);
    }
    browseItems = filterByPhotoVisibility(
      (hits || []).map((hit) => {
        const local = byContent.get(hit.content_id);
        return local ? { ...local, score: hit.score } : hit;
      })
    );
    browseSearchActive = true;
    browseSelected.clear();
    browseAnchorKey = null;
    renderBrowseResults();
  } catch (err) {
    results.innerHTML = `<p class="muted">${err.message}</p>`;
  }
}

async function findSimilarToPhoto(photo) {
  const contentId = photo?.content_id;
  if (contentId == null) {
    throw new Error("Photo is not indexed yet");
  }
  ensureBrowseGalleryVisible();
  const results = $("#browse-results");
  results.innerHTML = `<p class="muted">Finding similar…</p>`;
  const params = new URLSearchParams();
  params.set("content_id", String(contentId));
  params.set("top_k", "48");
  if (includeCulled()) params.set("include_culled", "true");
  try {
    const hits = await api(`/api/search/similar?${params}`);
    browseItems = filterByPhotoVisibility(hits || []);
    browseSearchActive = true;
    browseSelected.clear();
    browseAnchorKey = null;
    if ($("#q")) $("#q").value = "";
    renderBrowseResults();
  } catch (err) {
    results.innerHTML = `<p class="muted">${err.message}</p>`;
    throw err;
  }
}

let lastIndexStatus = {
  running: false,
  phase: "idle",
  current: 0,
  total: 0,
  message: "",
  source_id: null,
  embed_suspended: false,
  pending_embed: 0,
  queued_embed: [],
};
let lastFacesStatus = {
  running: false,
  phase: "idle",
  current: 0,
  total: 0,
  message: "",
  source_id: null,
  queued: [],
};
let lastSources = [];

function archiveDisplayName(sourceId, sources) {
  if (sourceId == null) return "";
  const sid = Number(sourceId);
  const row = (sources || lastSources || []).find((s) => Number(s.id) === sid);
  if (row?.label) return row.label;
  const input = document.querySelector(
    `#source-list input.source-label[data-label="${sid}"]`
  );
  const fromDom = input?.value?.trim();
  if (fromDom) return fromDom;
  return "";
}

function archiveIsBusy(sourceId, st) {
  if (!st?.running) return false;
  if (st.source_id == null) return false;
  return Number(st.source_id) === Number(sourceId);
}

function archiveFacesBusy(sourceId, fst) {
  if (!fst?.running) return false;
  if (fst.source_id == null) return false;
  return Number(fst.source_id) === Number(sourceId);
}

function archiveEmbedQueued(sourceId, st) {
  const q = st?.queued_embed || [];
  return q.some((id) => Number(id) === Number(sourceId));
}

function archiveFacesQueued(sourceId, fst) {
  const q = fst?.queued || [];
  return q.some((id) => Number(id) === Number(sourceId));
}

function archiveMatchesIndex(sourceId, st) {
  if (st?.source_id == null) return false;
  return Number(st.source_id) === Number(sourceId);
}

function archiveMatchesFaces(sourceId, fst) {
  if (fst?.source_id == null) return false;
  return Number(fst.source_id) === Number(sourceId);
}

/** @returns {"pause"|"go"} */
function archiveEmbedAction(sourceId, st, fst, source) {
  const embedBusy = archiveIsBusy(sourceId, st) && st.phase === "embed";
  if (embedBusy || archiveEmbedQueued(sourceId, st)) return "pause";
  return "go";
}

/** Whether Embed Go can start work for this archive. */
function archiveEmbedGoEnabled(sourceId, st, fst, source) {
  if (!!fst?.running) return false;
  if (st?.running && archiveIsBusy(sourceId, st)) return false;
  if (archiveEmbedQueued(sourceId, st)) return false;
  const pending = Number(source?.pending_embed_count ?? 0);
  return pending > 0;
}

/** @returns {"pause"|"go"} */
function archiveFacesAction(sourceId, fst) {
  if (archiveFacesQueued(sourceId, fst)) return "pause";
  if (archiveFacesBusy(sourceId, fst) && fst.phase !== "suspended") return "pause";
  return "go";
}

/** Whether Faces Go can start or resume for this archive. */
function archiveFacesGoEnabled(sourceId, fst, source) {
  if (!source?.exists) return false;
  if (archiveFacesQueued(sourceId, fst)) return false;
  if (fst?.running && fst.phase === "suspended" && archiveMatchesFaces(sourceId, fst)) {
    return true;
  }
  if (fst?.running && archiveFacesBusy(sourceId, fst) && fst.phase !== "suspended") {
    return false;
  }
  return Number(source?.pending_faces_count ?? 0) > 0;
}

function formatSourceCounts(s) {
  const isBackup = String(s.kind || "watched") === "backup";
  if (isBackup) {
    const copied = Number(s.backup_copied_count ?? s.photo_count ?? 0);
    const pending = Number(s.backup_pending_count || 0);
    const extra = Number(s.backup_extra_count || 0);
    const parts = [`${copied} copied`];
    if (pending > 0) {
      parts.push(
        `<span class="source-count-progress">${pending} pending</span>`
      );
    } else if (copied > 0) {
      parts.push(`in sync <span class="source-count-done" title="done">✓</span>`);
    }
    if (extra > 0) parts.push(`${extra} extra`);
    return parts.join(" · ");
  }
  const listed = Number(s.listed_count || 0);
  const indexed = Number(s.photo_count ?? 0);
  const faces = Number(s.faces_ready_count ?? 0);
  const embedded = Number(s.embedded_count ?? 0);
  const pendingFaces = Number(s.pending_faces_count ?? 0);
  const pendingEmbed = Number(s.pending_embed_count ?? 0);
  const parts = [];
  if (listed > 0) parts.push(`${listed} listed`);
  // Prefer listed vs indexed; if listed is missing, still show ✓ when photos exist.
  const indexedDone = indexed > 0 && (listed <= 0 || indexed >= listed);
  parts.push(formatSourceStage("indexed", indexed, indexedDone));
  parts.push(
    formatSourceStage("facematched", faces, indexed > 0 && pendingFaces === 0)
  );
  parts.push(
    formatSourceStage("embedded", embedded, indexed > 0 && pendingEmbed === 0)
  );
  return parts.join(" · ");
}

/** Done → `label ✓`; in progress → orange `N label`. */
function formatSourceStage(label, count, done) {
  if (done) {
    return `${label} <span class="source-count-done" title="done">✓</span>`;
  }
  return `<span class="source-count-progress">${count} ${label}</span>`;
}

function remainingCount(st) {
  if (!st || !(st.total > 0)) return 0;
  return Math.max(0, Number(st.total) - Number(st.current || 0));
}

function toGoLabel(n) {
  const left = Number(n || 0);
  return left > 0 ? `${left} to go` : "";
}

const INDEX_STAGES = [
  { id: "unpack", label: "Unpack", slow: true },
  { id: "index", label: "Index", slow: true },
  { id: "sync", label: "Sync", slow: true },
  { id: "geocode", label: "Geocode", slow: true },
  { id: "grayscale", label: "Fingerprint", slow: true },
  { id: "faces", label: "Facematch", slow: false },
  { id: "embed", label: "Embed", slow: true },
  { id: "clustering", label: "Cluster", slow: true },
];

function stageOrder(phase) {
  if (phase === "unpack") return "unpack";
  if (phase === "starting" || phase === "scan" || phase === "list" || phase === "index") {
    return "index";
  }
  if (phase === "sync") return "sync";
  if (
    phase === "geocode" ||
    phase === "grayscale" ||
    phase === "embed" ||
    phase === "clustering"
  ) {
    return phase;
  }
  return null;
}

function pendingEmbedCount(st) {
  const fromSources = (lastSources || []).reduce(
    (n, s) =>
      n +
      (Number(s.enabled ?? 1) && Number(s.embed_enabled)
        ? Number(s.pending_embed_count || 0)
        : 0),
    0
  );
  if (fromSources > 0) return fromSources;
  // Fallback before sources load / if counts lag status.
  return Number(st?.pending_embed || 0);
}

function pendingFacesCount() {
  return (lastSources || []).reduce(
    (n, s) =>
      n +
      (Number(s.enabled ?? 1) && Number(s.face_enabled)
        ? Number(s.pending_faces_count || 0)
        : 0),
    0
  );
}

function renderIndexStages(st, fst) {
  const root = $("#index-stages");
  if (!root) return;
  const faceSt = fst || lastFacesStatus;
  const phase = st?.phase || "idle";
  const unpacking = !!st?.import?.running;
  const running = !!st?.running || unpacking;
  const suspended = !!st?.embed_suspended || phase === "suspended";
  const active = unpacking ? "unpack" : running ? stageOrder(phase) : null;
  const pipeline = INDEX_STAGES.filter((s) => s.id !== "faces");
  const activeIdx = active ? pipeline.findIndex((s) => s.id === active) : -1;
  const pendingEmbed = pendingEmbedCount(st);
  const pendingFaces = pendingFacesCount();
  const faceRunning = !!faceSt?.running && faceSt.phase !== "suspended";
  const unpackLeft = Math.max(
    0,
    Number(st?.import?.total || 0) - Number(st?.import?.extracted || 0)
  );

  const items = INDEX_STAGES.map((s) => {
    let cls = "";
    let count = "";
    let title = s.label;
    const pipeIdx = pipeline.findIndex((p) => p.id === s.id);

    if (s.id === "unpack") {
      if (unpacking) {
        cls = "is-active";
        title = "Unpack now";
        if (unpackLeft > 0) count = toGoLabel(unpackLeft);
      } else if (running && activeIdx > 0) {
        cls = "is-done";
        title = "Unpack done";
      }
    } else if (s.id === "faces") {
      if (pendingFaces > 0) count = toGoLabel(pendingFaces);
      const facesQueued = Number(faceSt?.queued_count || 0) > 0;
      if (faceRunning) {
        cls = "is-active";
        title = "Facematch running";
        if (faceSt?.total > 0) count = toGoLabel(remainingCount(faceSt));
      } else if (facesQueued && pendingFaces > 0) {
        cls = "is-waiting";
        title = "Facematch scheduled";
      }
    } else if (s.id === "embed") {
      if (pendingEmbed > 0) count = toGoLabel(pendingEmbed);
      if (running && active === "embed") {
        cls = "is-active";
        title = "Embed now";
        count = toGoLabel(remainingCount(st)) || count;
      } else if (pendingEmbed > 0 && running) {
        cls = "is-waiting";
        title = "Embed scheduled next";
      } else if (pendingEmbed > 0) {
        cls = "is-waiting";
        title = "Embed has work left";
      } else if (running && activeIdx >= 0 && pipeIdx < activeIdx) {
        cls = "is-done";
        title = "Embed caught up";
      }
    } else if (running && s.id === active) {
      cls = "is-active";
      count = toGoLabel(remainingCount(st));
      title = `${s.label} now`;
    } else if (running && activeIdx >= 0 && pipeIdx >= 0 && pipeIdx < activeIdx) {
      cls = "is-done";
      title = `${s.label} done`;
    } else if (!running && (phase === "done" || phase === "idle" || suspended) && pipeIdx >= 0) {
      cls = "is-done";
      title = `${s.label} done`;
    }

    return `<li class="${cls}" data-stage="${s.id}" title="${title}"><span class="stage-label">${s.label}</span>${
      count ? `<span class="stage-count">${count}</span>` : ""
    }</li>`;
  });

  root.innerHTML = items.join('<li class="index-stage-sep" aria-hidden="true">→</li>');
}

function sourceCheckCell(attr, id, on, { disabled = false, available = true } = {}) {
  if (!available) return `<td class="source-col-check"></td>`;
  return `<td class="source-col-check"><label class="source-check"><input type="checkbox" data-${attr}="${id}" ${on ? "checked" : ""} ${disabled ? "disabled" : ""} /></label></td>`;
}

const SOURCE_REFRESH_SVG = `<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 6V1L7 6l5 5V7c3.31 0 6 2.69 6 6 0 1.01-.25 1.97-.7 2.8l1.46 1.46A7.93 7.93 0 0 0 20 13c0-4.42-3.58-8-8-8zm-6.7 2.74L3.84 7.28A7.93 7.93 0 0 0 4 13c0 4.42 3.58 8 8 8v5l5-5-5-5v4c-3.31 0-6-2.69-6-6 0-1.01.25-1.97.7-2.8z"/></svg>`;
const SOURCE_DEL_SVG = `<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M6 19a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>`;

function applyIndexStatusToSources(st, sources, fst) {
  const faceSt = fst || lastFacesStatus;
  const byId = new Map((sources || []).map((s) => [Number(s.id), s]));
  $("#source-list").querySelectorAll("tr[data-source-id]").forEach((row) => {
    const id = Number(row.dataset.sourceId);
    const missing = row.dataset.missing === "1";
    const src = byId.get(id) || {
      id,
      exists: !missing,
      pending_embed_count: Number(row.dataset.pendingEmbed || 0),
      pending_faces_count: Number(row.dataset.pendingFaces || 0),
    };
    if (byId.has(id) && row.dataset) {
      row.dataset.pendingEmbed = String(src.pending_embed_count ?? 0);
      row.dataset.pendingFaces = String(src.pending_faces_count ?? 0);
      if (src.shadow_path != null) row.dataset.shadowPath = src.shadow_path || "";
    }
    const progRow = row.querySelector(".source-progress-row");
    const prog = row.querySelector(".source-progress");
    const counts = row.querySelector(".source-counts");
    if (src && counts && byId.has(id)) {
      counts.innerHTML = formatSourceCounts(src);
    }
    const shadowInput = row.querySelector("input[data-shadow-edit]");
    if (shadowInput && byId.has(id) && document.activeElement !== shadowInput) {
      const path = String(src.shadow_path || "").trim();
      shadowInput.value = path;
      shadowInput.defaultValue = path;
      row.dataset.shadowPath = path;
    }
    const reindexBtn = row.querySelector("[data-reindex], [data-sync]");
    if (reindexBtn && byId.has(id)) {
      const enabled = Number(src.enabled ?? 1);
      reindexBtn.disabled = !enabled || missing;
    }
    const unpackBusy =
      !!st?.import?.running && Number(st.import.source_id) === Number(id);
    const indexBusy = archiveIsBusy(id, st) && st.phase !== "suspended";
    const facesBusy = archiveFacesBusy(id, faceSt) && faceSt.phase !== "suspended";
    const busy = unpackBusy || indexBusy || facesBusy;
    row.classList.toggle("is-active", busy);
    if (progRow) progRow.hidden = true;
    if (prog) prog.textContent = "";
  });
  renderIndexStages(st, faceSt);
}

async function loadSources() {
  const sources = await api("/api/sources");
  lastSources = sources;
  const st = lastIndexStatus;
  const fst = lastFacesStatus;
  const esc = (s) =>
    String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/"/g, "&quot;");
  $("#source-list").innerHTML = sources
    .map((s) => {
      const enabled = Number(s.enabled ?? 1);
      const faceOn = Number(s.face_enabled ?? 0);
      const embedOn = Number(s.embed_enabled ?? 0);
      const pending = Number(s.pending_embed_count || 0);
      const pendingFaces = Number(s.pending_faces_count || 0);
      const isBackup = String(s.kind || "watched") === "backup";
      const isDevice = String(s.kind || "watched") === "device";
      const shadowOn = !!(s.shadow_enabled && s.shadow_path);
      const syncOn = Number(s.sync_enabled ?? 0);
      const badge = isBackup
        ? `<span class="badge backup">backup</span>`
        : `<span class="badge ${s.exists ? "ok" : "missing"}">${s.exists ? "mounted" : "missing"}</span>${
            isDevice ? ` <span class="badge device">phone</span>` : ""
          }`;
      const off = !enabled;
      const shadowPath = esc(s.shadow_path || "");
      const actions = `
        ${isDevice ? `<button type="button" class="source-icon-btn" data-pair-qr="${s.id}" title="Pair QR" aria-label="Pair QR">◎</button>` : ""}
        ${isBackup
          ? `<button type="button" class="source-icon-btn source-reindex" data-sync="${s.id}" title="Sync backup" aria-label="Sync backup" ${enabled && s.exists ? "" : "disabled"}>${SOURCE_REFRESH_SVG}</button>`
          : `<button type="button" class="source-icon-btn source-reindex" data-reindex="${s.id}" title="Reindex" aria-label="Reindex" ${enabled && s.exists ? "" : "disabled"}>${SOURCE_REFRESH_SVG}</button>`}
        <button type="button" class="source-icon-btn danger" data-del="${s.id}" title="Remove" aria-label="Remove">${SOURCE_DEL_SVG}</button>`;
      const shadowRow = !isBackup
        ? `<div class="source-shadow-row">
            <span class="path-pick">
              <input type="text" class="source-shadow-input" data-shadow-edit="${s.id}" value="${shadowPath}" placeholder="Shadow folder (default if empty)…" autocomplete="off" spellcheck="false" aria-label="Shadow folder" ${off ? "disabled" : ""} />
              <button type="button" data-shadow-browse="${s.id}" title="Choose shadow folder" ${off ? "disabled" : ""}>…</button>
            </span>
          </div>`
        : "";
      return `
      <tr data-source-id="${s.id}" data-missing="${s.exists ? "0" : "1"}" data-pending-embed="${pending}" data-pending-faces="${pendingFaces}" data-shadow-path="${shadowPath}" class="${enabled ? "" : "source-disabled"}">
        <td class="source-meta">
          <input class="source-label" type="text" value="${esc(s.label)}" data-label="${s.id}" aria-label="Volume label" />
          <div class="path">${esc(isDevice ? (s.pair_url || s.path) : s.path)}</div>
          ${shadowRow}
          <div class="source-status-row">
            <span class="source-badges">${badge}</span>
            <span class="source-counts">${formatSourceCounts(s)}</span>
          </div>
          <div class="source-progress-row" hidden>
            <div class="source-progress"></div>
          </div>
        </td>
        ${sourceCheckCell("include", s.id, enabled)}
        ${sourceCheckCell("face-toggle", s.id, faceOn, { disabled: off, available: !isBackup })}
        ${sourceCheckCell("embed-toggle", s.id, embedOn, { disabled: off, available: !isBackup })}
        ${sourceCheckCell("shadow-toggle", s.id, shadowOn, { disabled: off, available: !isBackup })}
        ${sourceCheckCell("sync-toggle", s.id, syncOn, { disabled: off, available: !isBackup })}
        <td class="source-col-act"><div class="source-actions">${actions}</div></td>
      </tr>`;
    })
    .join("");
  applyIndexStatusToSources(st, sources, fst);
  $("#source-list").querySelectorAll("[data-label]").forEach((input) => {
    const save = async () => {
      const next = input.value.trim();
      if (!next || next === input.defaultValue) {
        input.value = input.defaultValue;
        return;
      }
      try {
        await api(`/api/sources/${input.dataset.label}`, {
          method: "PATCH",
          body: JSON.stringify({ label: next }),
        });
        input.defaultValue = next;
        refreshStats();
      } catch (err) {
        input.value = input.defaultValue;
        alert(err.message);
      }
    };
    input.addEventListener("blur", save);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        input.blur();
      } else if (e.key === "Escape") {
        input.value = input.defaultValue;
        input.blur();
      }
    });
  });
  $("#source-list").querySelectorAll("[data-del]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (
        !confirm(
          "Remove this volume from the index? Files on disk are kept. Photos that also belong under another volume stay indexed."
        )
      )
        return;
      await api(`/api/sources/${btn.dataset.del}`, { method: "DELETE" });
      loadSources();
      refreshStats();
    });
  });
  $("#source-list").querySelectorAll("[data-include]").forEach((input) => {
    input.addEventListener("change", async () => {
      const id = input.dataset.include;
      const next = input.checked;
      const row = input.closest("tr");
      row?.classList.toggle("source-disabled", !next);
      row?.querySelectorAll("[data-face-toggle], [data-embed-toggle], [data-shadow-toggle], [data-sync-toggle], [data-shadow-edit], [data-shadow-browse]").forEach((b) => {
        b.disabled = !next;
      });
      const reindexBtn = row?.querySelector("[data-reindex], [data-sync]");
      if (reindexBtn) reindexBtn.disabled = !next || row?.dataset.missing === "1";
      try {
        await api(`/api/sources/${id}/enable?enabled=${next}`, { method: "POST" });
      } catch (err) {
        input.checked = !next;
        row?.classList.toggle("source-disabled", next);
        row?.querySelectorAll("[data-face-toggle], [data-embed-toggle], [data-shadow-toggle], [data-sync-toggle], [data-shadow-edit], [data-shadow-browse]").forEach((b) => {
          b.disabled = next;
        });
        if (reindexBtn) reindexBtn.disabled = next || row?.dataset.missing === "1";
        alert(err.message);
      }
    });
  });
  $("#source-list").querySelectorAll("[data-face-toggle]").forEach((input) => {
    input.addEventListener("change", async () => {
      const id = input.dataset.faceToggle;
      const next = input.checked;
      try {
        const st = await api(`/api/sources/${id}/face_enabled?enabled=${next}`, { method: "POST" });
        lastFacesStatus = st;
        loadSources();
      } catch (err) {
        input.checked = !next;
        alert(err.message);
      }
    });
  });
  $("#source-list").querySelectorAll("[data-embed-toggle]").forEach((input) => {
    input.addEventListener("change", async () => {
      const id = input.dataset.embedToggle;
      const next = input.checked;
      try {
        const st = await api(`/api/sources/${id}/embed_enabled?enabled=${next}`, { method: "POST" });
        lastIndexStatus = st;
        loadSources();
      } catch (err) {
        input.checked = !next;
        alert(err.message);
      }
    });
  });
  $("#source-list").querySelectorAll("[data-shadow-edit]").forEach((input) => {
    const save = async () => {
      const next = input.value.trim();
      if (next === input.defaultValue.trim()) return;
      try {
        const src = await api(`/api/sources/${input.dataset.shadowEdit}`, {
          method: "PATCH",
          body: JSON.stringify({ shadow_path: next, shadow_enabled: !!next }),
        });
        input.value = src.shadow_path || "";
        input.defaultValue = input.value;
        const row = input.closest("tr");
        if (row) row.dataset.shadowPath = input.value;
        const shadowToggle = row?.querySelector("[data-shadow-toggle]");
        if (shadowToggle) shadowToggle.checked = !!(src.shadow_enabled && src.shadow_path);
      } catch (err) {
        input.value = input.defaultValue;
        alert(err.message);
      }
    };
    input.addEventListener("blur", save);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        input.blur();
      } else if (e.key === "Escape") {
        input.value = input.defaultValue;
        input.blur();
      }
    });
  });
  $("#source-list").querySelectorAll("[data-shadow-browse]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.shadowBrowse;
      const row = btn.closest("tr");
      const input = row?.querySelector("[data-shadow-edit]");
      try {
        const picked = await api(
          `/api/pick-directory?title=${encodeURIComponent("Shadow folder for phone copies")}`,
          { method: "POST" }
        );
        if (picked.cancelled || !picked.path) return;
        const src = await api(`/api/sources/${id}`, {
          method: "PATCH",
          body: JSON.stringify({ shadow_path: picked.path, shadow_enabled: true }),
        });
        if (input) {
          input.value = src.shadow_path || "";
          input.defaultValue = input.value;
        }
        if (row) row.dataset.shadowPath = src.shadow_path || "";
        const shadowToggle = row?.querySelector("[data-shadow-toggle]");
        if (shadowToggle) shadowToggle.checked = true;
      } catch (err) {
        alert(err.message);
      }
    });
  });
  $("#source-list").querySelectorAll("[data-shadow-toggle]").forEach((input) => {
    input.addEventListener("change", async () => {
      const id = input.dataset.shadowToggle;
      const next = input.checked;
      const row = input.closest("tr");
      try {
        if (next && !(row?.dataset.shadowPath || "").trim()) {
          await api(`/api/sources/${id}`, {
            method: "PATCH",
            body: JSON.stringify({ shadow_enabled: true }),
          });
        } else {
          await api(`/api/sources/${id}`, {
            method: "PATCH",
            body: JSON.stringify({ shadow_enabled: next }),
          });
        }
        loadSources();
      } catch (err) {
        input.checked = !next;
        alert(err.message);
      }
    });
  });
  $("#source-list").querySelectorAll("[data-sync-toggle]").forEach((input) => {
    input.addEventListener("change", async () => {
      const id = input.dataset.syncToggle;
      const next = input.checked;
      try {
        await api(`/api/sources/${id}`, {
          method: "PATCH",
          body: JSON.stringify({ sync_enabled: next }),
        });
      } catch (err) {
        input.checked = !next;
        alert(err.message);
      }
    });
  });
  $("#source-list").querySelectorAll("[data-pair-qr]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.dataset.pairQr);
      const src = (lastSources || []).find((s) => Number(s.id) === id);
      if (src) openPairDeviceDialog(src);
    });
  });
  $("#source-list").querySelectorAll("[data-reindex]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.reindex;
      try {
        const res = await api(`/api/sources/${id}/reindex`, { method: "POST" });
        if (res.status) lastIndexStatus = res.status;
        loadSources();
        pollIndex();
        pollFacesStatus();
      } catch (err) {
        alert(err.message);
      }
    });
  });
  $("#source-list").querySelectorAll("[data-sync]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.sync;
      btn.disabled = true;
      try {
        await api(`/api/sources/${id}/sync`, { method: "POST" });
        await loadSources();
      } catch (err) {
        alert(err.message);
      } finally {
        btn.disabled = false;
      }
    });
  });
}

function indexStatusText(st, sources, fst) {
  const bits = [];
  const imp = st?.import;
  if (imp?.running) {
    bits.push("Unpack");
  } else if (imp?.error) {
    bits.push(String(imp.error));
  }
  if (fst && (fst.running || fst.phase === "suspended")) {
    const name = archiveDisplayName(fst.source_id, sources);
    const msg = String(fst.message || "").trim();
    if (msg) bits.push(name ? `${name} — ${msg}` : msg);
    else if (name) bits.push(`Facematch · ${name}`);
  }
  // Only show indexer text while work is in flight — clear when idle/done.
  if (st?.running) {
    const msg = String(st.message || "").trim();
    const name = archiveDisplayName(st.source_id, sources);
    if (msg) bits.push(msg);
    else if (name) bits.push(name);
  }
  return bits.join(" · ");
}

async function pollIndex() {
  const [st, sources] = await Promise.all([
    api("/api/index/status"),
    api("/api/sources").catch(() => lastSources),
  ]);
  const wasRunning = !!lastIndexStatus?.running;
  lastIndexStatus = st;
  const el = $("#index-status");
  el.textContent = indexStatusText(st, lastSources, lastFacesStatus);
  if (sources) await applySourcesLive(sources);
  applyIndexStatusToSources(st, lastSources, lastFacesStatus);
  if (!st.running) {
    refreshStats();
    if (wasRunning) loadSources();
  }
}

let dupeSelected = new Set();
let dupePhotosById = new Map();

function dupeGroupSelectionState(groupEl) {
  const members = [...groupEl.querySelectorAll(".dupe-members [data-id]")];
  const selectedCount = members.filter((el) => dupeSelected.has(Number(el.dataset.id))).length;
  const unmatched = groupEl.dataset.group === "unmatched";
  const selectable = members.filter((el) => {
    if (el.dataset.alt === "1") return false;
    if (photoVisibility === "removed") return el.dataset.culled === "1";
    if (unmatched) return el.dataset.culled !== "1";
    return el.dataset.culled !== "1" && el.dataset.keeper !== "1";
  });
  return { selectedCount, nonPrimaryCount: selectable.length, nonPrimary: selectable };
}

function syncDupeSelectToggleLabel(btn, selectedCount, nonPrimaryCount) {
  if (selectedCount > 0) {
    btn.textContent = "Unselect all";
    btn.disabled = false;
  } else if (btn.dataset.group === "unmatched" && photoVisibility !== "removed") {
    btn.textContent = nonPrimaryCount
      ? `Select all (${nonPrimaryCount})`
      : "Select all";
    btn.disabled = nonPrimaryCount === 0;
  } else if (photoVisibility === "removed") {
    btn.textContent = nonPrimaryCount
      ? `Select rejected (${nonPrimaryCount})`
      : "Select rejected";
    btn.disabled = nonPrimaryCount === 0;
  } else {
    btn.textContent = nonPrimaryCount
      ? `Select non-preferred (${nonPrimaryCount})`
      : "Select non-preferred";
    btn.disabled = nonPrimaryCount === 0;
  }
}

function syncDupeToolbar() {
  const n = dupeSelected.size;
  const selected = selectedDupePhotos();
  const activeIds = selected.filter((p) => !p.culled).map((p) => p.id);
  const removedIds = selected.filter((p) => p.culled).map((p) => p.id);
  const indexedIds = selected.filter((p) => p.id != null).map((p) => p.id);

  let totalNonPrimary = 0;
  document.querySelectorAll("#dupe-list .dupe-group").forEach((group) => {
    const state = dupeGroupSelectionState(group);
    totalNonPrimary += state.nonPrimaryCount;
    const btn = group.querySelector(".dupe-select-toggle");
    if (btn) syncDupeSelectToggleLabel(btn, state.selectedCount, state.nonPrimaryCount);
  });

  if (!isDupesWorkspace()) return;

  const galleryCount = $("#browse-gallery-count");
  if (galleryCount) {
    const total = dupePhotosById.size;
    galleryCount.textContent = total ? `${n} / ${total}` : "";
  }

  const allBtn = $("#btn-browse-select-all");
  if (allBtn) {
    const hasGroups = !!document.querySelector("#dupe-list .dupe-group");
    allBtn.hidden = !hasGroups;
    if (hasGroups) syncDupeSelectToggleLabel(allBtn, n, totalNonPrimary);
  }

  const removeBtn = $("#btn-browse-remove");
  const deleteBtn = $("#btn-browse-delete");
  const editBtn = $("#btn-browse-edit");
  const exportBtn = $("#btn-browse-export");
  const addFolderBtn = $("#btn-browse-add-folder");
  const removeFolderBtn = $("#btn-browse-remove-folder");
  if (removeBtn) {
    const restoreMode = removedIds.length > 0;
    removeBtn.disabled = restoreMode ? false : activeIds.length === 0;
    removeBtn.textContent = restoreMode ? "Restore selected" : "Reject selected";
    removeBtn.classList.toggle("danger", !restoreMode);
    removeBtn.dataset.mode = restoreMode ? "restore" : "remove";
  }
  if (deleteBtn) {
    deleteBtn.disabled = removedIds.length === 0 || activeIds.length > 0;
  }
  if (editBtn) {
    editBtn.disabled = indexedIds.length === 0;
    editBtn.textContent = "Edit selection";
  }
  if (exportBtn) {
    exportBtn.disabled = indexedIds.length === 0;
    exportBtn.textContent = "Export selected";
  }
  if (addFolderBtn) {
    addFolderBtn.disabled = indexedIds.length === 0;
  }
  if (removeFolderBtn) {
    removeFolderBtn.hidden = true;
    removeFolderBtn.disabled = true;
  }
  const similarBtn = $("#btn-browse-similar");
  if (similarBtn) {
    similarBtn.disabled = !(
      selected.length === 1 && selected[0]?.content_id != null
    );
  }
  const actions = $(".browse-selection-actions");
  if (actions) actions.hidden = n === 0;
  syncTimelineSelectionUi();
}

function selectedDupePhotos() {
  return [...dupeSelected].map((id) => dupePhotosById.get(id)).filter(Boolean);
}

function isDupesWorkspace() {
  return document.querySelector(".panel.active")?.id === "tab-dupes";
}

function selectDupeNonPrimary(scopeEl) {
  scopeEl.querySelectorAll(".dupe-members [data-id]").forEach((el) => {
    if (el.dataset.alt === "1" || el.dataset.culled === "1" || el.dataset.keeper === "1") return;
    const id = Number(el.dataset.id);
    dupeSelected.add(id);
    el.classList.add("selected");
  });
}

function selectDupeRemoved(scopeEl) {
  scopeEl.querySelectorAll(".dupe-members [data-id]").forEach((el) => {
    if (el.dataset.culled !== "1" || el.dataset.alt === "1") return;
    const id = Number(el.dataset.id);
    dupeSelected.add(id);
    el.classList.add("selected");
  });
}

function selectDupeAll(scopeEl) {
  scopeEl.querySelectorAll(".dupe-members [data-id]").forEach((el) => {
    if (el.dataset.alt === "1") return;
    const id = Number(el.dataset.id);
    dupeSelected.add(id);
    el.classList.add("selected");
  });
}

function unselectDupeAll(scopeEl) {
  scopeEl.querySelectorAll(".dupe-members [data-id]").forEach((el) => {
    const id = Number(el.dataset.id);
    dupeSelected.delete(id);
    el.classList.remove("selected");
  });
}

function renderDupeMember(m, { role = null } = {}) {
  const removed = !!m.culled;
  const isAlt = role === "active-alt";
  const isKeeper = !!m.is_keeper && !removed && !isAlt;
  const inScope = !!m.in_scope;
  const selected = dupeSelected.has(m.id);
  const path = photoPath(m);
  const classes = [
    "card",
    isKeeper ? "keeper" : "",
    removed ? "removed" : "",
    isAlt ? "active-alt" : "",
    inScope ? "in-scope" : "out-of-scope",
    selected ? "selected" : "",
  ]
    .filter(Boolean)
    .join(" ");

  let badge = "";
  if (isAlt) badge = `<span class="tag kept-tag">KEPT</span>`;
  else if (removed) badge = `<span class="removed-badge">REJECTED</span>`;
  else if (isKeeper) badge = `<span class="tag">PREFERRED</span>`;
  const outsideBadge = !inScope ? `<span class="tree-badge">OUTSIDE</span>` : "";

  return `
    <article class="${classes}" data-id="${m.id}" data-keeper="${isKeeper ? 1 : 0}" data-culled="${removed ? 1 : 0}" data-alt="${isAlt ? 1 : 0}" data-in-scope="${inScope ? 1 : 0}" data-orientation="${orientationDeg(m)}" data-mirrored="${isMirrored(m) ? 1 : 0}">
      ${badge}
      <div class="card-media">
        ${outsideBadge}
        <img loading="lazy" src="/api/thumb/${m.id}" alt="" draggable="false" style="${orientationStyle(m)}" />
        ${browseItemHoverDetail(m)}
      </div>
      <div class="meta">
        <strong title="${path}">${basename(m.path)}</strong>
        <span class="meta-line">${browseItemMetaLine(m)}</span>
      </div>
    </article>`;
}

/** Global photo visibility: active | all | removed | selected */
let photoVisibility = "active";
const PHOTO_VISIBILITY_KEY = "fotoman.photoVisibility";
const VALID_PHOTO_VISIBILITY = new Set(["active", "all", "removed", "selected"]);

function includeCulled() {
  return photoVisibility !== "active";
}

/** Culled/active filter. Selection filter is display-only (see respectSelection). */
function currentAspectFilter() {
  return $("#aspect-filter")?.value || "";
}

function syncAspectFilterStyle() {
  const el = $("#aspect-filter");
  if (el) el.classList.toggle("is-filtered", !!el.value);
}

const SQUARE_MAX_RATIO = 1.05;
const PANORAMA_MIN_RATIO = 2.35;

function photoAspectClass(p) {
  if (p?.aspect_class) return p.aspect_class;
  const w0 = Number(p?.width);
  const h0 = Number(p?.height);
  if (!Number.isFinite(w0) || !Number.isFinite(h0) || w0 <= 0 || h0 <= 0) return "";
  const rot = Number(p?.orientation) || 0;
  const dw = rot % 180 === 90 ? h0 : w0;
  const dh = rot % 180 === 90 ? w0 : h0;
  const ratio = Math.max(dw, dh) / Math.min(dw, dh);
  if (ratio >= PANORAMA_MIN_RATIO) return "panorama";
  if (ratio <= SQUARE_MAX_RATIO) return "square";
  return dw > dh ? "landscape" : "portrait";
}

function filterByAspect(items) {
  const aspect = currentAspectFilter();
  if (!aspect) return items;
  return items.filter((p) => photoAspectClass(p) === aspect);
}

/** Culled/active filter. Selection filter is display-only (see respectSelection). */
function filterByPhotoVisibility(items, { respectSelection = false } = {}) {
  if (!Array.isArray(items)) return [];
  let out;
  if (photoVisibility === "removed") out = items.filter((p) => p?.culled);
  else if (photoVisibility === "active") out = items.filter((p) => p && !p.culled);
  else out = items.filter((p) => p); // all | selected (ingest keeps full set)
  if (respectSelection && photoVisibility === "selected") {
    out = out.filter((p) => browseSelected.has(browseItemKey(p)));
  }
  return out;
}

function syncPhotoVisibilityButtons() {
  document.querySelectorAll(".visibility-modes [data-visibility]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.visibility === photoVisibility);
  });
}

function savePhotoVisibilityPref() {
  try {
    localStorage.setItem(PHOTO_VISIBILITY_KEY, photoVisibility);
  } catch (_) {
    /* ignore */
  }
}

function loadPhotoVisibilityPref() {
  try {
    const saved = localStorage.getItem(PHOTO_VISIBILITY_KEY);
    if (saved && VALID_PHOTO_VISIBILITY.has(saved)) {
      // Selection is not persisted across reloads.
      photoVisibility = saved === "selected" ? "active" : saved;
      return;
    }
    // Migrate old per-surface "show removed" checkboxes → All.
    const legacyKeys = [
      "fotoman.browseShowRemove",
      "fotoman.dupeIncludeRemoved",
      "fotoman.searchShowRemove",
      "fotoman.mapShowRemove",
    ];
    if (legacyKeys.some((k) => localStorage.getItem(k) === "1")) {
      photoVisibility = "all";
    }
  } catch (_) {
    /* ignore */
  }
}

function isPressOn(el) {
  return el?.getAttribute("aria-pressed") === "true";
}

function setPressOn(el, on) {
  if (!el) return;
  el.setAttribute("aria-pressed", on ? "true" : "false");
  el.classList.toggle("active", !!on);
}

function togglePress(el) {
  if (!el) return false;
  const next = !isPressOn(el);
  setPressOn(el, next);
  return next;
}

/** Toggle button ids persisted in localStorage (missing key → keep HTML default). */
const PRESS_PREFS = [
  { id: "browse-show-face", key: "fotoman.browseShowFace" },
  { id: "browse-show-nested", key: "fotoman.browseShowNested" },
  { id: "lightbox-show-frames", key: "fotoman.lightboxShowFrames" },
];

const THUMB_CROP_KEY = "fotoman.browseThumbCrop";
/** Details table: expanded cluster group_ids (default collapsed). */
const detailsExpandedGroups = new Set();
/** Old Expand/dupes workspace should restore as Compare. */
let restoreCompareView = false;

function isThumbCropOn() {
  try {
    return localStorage.getItem(THUMB_CROP_KEY) === "1";
  } catch (_) {
    return false;
  }
}

function syncThumbCropClass() {
  document.body.classList.toggle("thumb-crop", isThumbCropOn());
}

function setThumbCropPref(on) {
  try {
    localStorage.setItem(THUMB_CROP_KEY, on ? "1" : "0");
  } catch (_) {
    /* ignore */
  }
  syncThumbCropClass();
}

function loadGalleryLayoutPref() {
  try {
    const saved = localStorage.getItem("fotoman.galleryLayout");
    const tab = localStorage.getItem(TAB_STORAGE_KEY);
    if (saved === "clusters" || tab === "dupes") restoreCompareView = true;
  } catch (_) {
    /* ignore */
  }
}

function loadPressPrefs() {
  for (const { id, key } of PRESS_PREFS) {
    const el = document.getElementById(id);
    if (!el) continue;
    try {
      const v = localStorage.getItem(key);
      if (v != null) setPressOn(el, v === "1");
      else setPressOn(el, isPressOn(el));
    } catch (_) {
      /* ignore */
    }
  }
  loadGalleryLayoutPref();
  syncThumbCropClass();
}

function savePressPref(id) {
  const pref = PRESS_PREFS.find((p) => p.id === id);
  const el = pref ? document.getElementById(id) : null;
  if (!pref || !el) return;
  try {
    localStorage.setItem(pref.key, isPressOn(el) ? "1" : "0");
  } catch (_) {
    /* ignore */
  }
}

function bindPressPrefs() {
  for (const { id } of PRESS_PREFS) {
    const el = document.getElementById(id);
    // Capture phase so side-effect listeners see the updated pressed state.
    el?.addEventListener(
      "click",
      () => {
        togglePress(el);
        savePressPref(id);
      },
      true
    );
  }
}

async function loadDupes() {
  return regroupClusters({ persist: false });
}

/** Render cluster groups already shaped like /api/duplicates items. */
function renderDupeGroups(rawGroups) {
  const scopeIds = browseScopedPhotoIds();
  const root = $("#dupe-list");
  if (!root) return;
  if (!scopeIds.size) {
    dupePhotosById = new Map();
    dupeSelected.clear();
    root.innerHTML = `<p class="muted">Select items in the tree to show clusters.</p>`;
    syncDupeToolbar();
    renderBrowseTimeline([]);
    return;
  }
  let groups = Array.isArray(rawGroups) ? rawGroups : [];
  let unmatched = [];

  const annotate = (m) => ({
    ...m,
    in_scope: m.in_scope === true || m.in_scope === 1 || scopeIds.has(m.id),
  });

  if (photoVisibility === "removed") {
    const clusteredRemoved = new Set();
    groups = groups
      .map((g) => {
        const members = (g.members || []).map(annotate);
        const removed = members.filter((m) => m.culled && m.in_scope);
        if (!removed.length) return null;
        removed.forEach((m) => clusteredRemoved.add(m.id));
        const activeAlts = members.filter((m) => !m.culled);
        return {
          ...g,
          members: [
            ...removed.map((m) => ({ ...m, _role: "removed" })),
            ...activeAlts.map((m) => ({ ...m, _role: "active-alt" })),
          ],
          activeAltCount: activeAlts.length,
        };
      })
      .filter(Boolean);
    unmatched = browseItems.filter(
      (p) => p?.id != null && p.culled && scopeIds.has(p.id) && !clusteredRemoved.has(p.id)
    );
  } else {
    const clusteredIds = new Set();
    groups = groups
      .map((g) => {
        let members = (g.members || []).map(annotate);
        members = filterByPhotoVisibility(members, { respectSelection: false });
        if (photoVisibility === "selected") {
          const hasSelected = members.some(
            (m) => m.in_scope && browseSelected.has(browseItemKey(m))
          );
          if (!hasSelected) return { ...g, members: [] };
          members = members.filter(
            (m) => !m.in_scope || browseSelected.has(browseItemKey(m))
          );
        }
        if (!members.some((m) => m.in_scope)) return { ...g, members: [] };
        return { ...g, members };
      })
      .filter((g) => (g.members || []).length >= 1);
    groups.forEach((g) =>
      (g.members || []).forEach((m) => {
        if (m.in_scope) clusteredIds.add(m.id);
      })
    );
    unmatched = filterByPhotoVisibility(
      browseItems.filter((p) => p?.id != null && scopeIds.has(p.id) && !clusteredIds.has(p.id)),
      { respectSelection: true }
    );
  }

  const liveIds = new Set();
  dupePhotosById = new Map();
  groups.forEach((g) =>
    (g.members || []).forEach((m) => {
      liveIds.add(m.id);
      dupePhotosById.set(m.id, m);
    })
  );
  unmatched.forEach((m) => {
    liveIds.add(m.id);
    dupePhotosById.set(m.id, m);
  });
  dupeSelected = new Set([...dupeSelected].filter((id) => liveIds.has(id)));
  syncDupeToolbar();

  if (!groups.length && !unmatched.length) {
    root.innerHTML = `<p class="muted">No clusters in the current selection.</p>`;
    syncDupeToolbar();
    renderBrowseTimeline(browseDisplayItems());
    return;
  }

  const groupHtml = groups
    .map((g) => {
      const active = (g.members || []).filter((m) => !m.culled && m._role !== "active-alt");
      const removedMembers = (g.members || []).filter((m) => m.culled);
      const altCount = g.activeAltCount || 0;
      const selectable =
        photoVisibility === "removed"
          ? removedMembers.length
          : active.filter((m) => !m.is_keeper).length;
      const groupSelected = (g.members || []).filter(
        (m) => m._role !== "active-alt" && dupeSelected.has(m.id)
      ).length;
      const toggleLabel =
        photoVisibility === "removed"
          ? groupSelected
            ? "Unselect all"
            : `Select rejected${selectable ? ` (${selectable})` : ""}`
          : groupSelected
            ? "Unselect all"
            : `Select non-preferred${selectable ? ` (${selectable})` : ""}`;
      const altNote = altCount
        ? ` · ${altCount} kept alternative${altCount === 1 ? "" : "s"}`
        : photoVisibility === "removed"
          ? " · no kept alternative"
          : "";
      const outsideN = (g.members || []).filter((m) => !m.in_scope).length;
      const outsideNote = outsideN
        ? ` · ${outsideN} outside tree`
        : "";
      return `
      <section class="dupe-group" data-group="${g.id}">
        <div class="dupe-group-head month-divider" role="separator">
          <span class="dupe-group-title">${g.kind}${altNote}${outsideNote}</span>
          <button type="button" class="dupe-select-toggle" data-group="${g.id}" ${
            groupSelected || selectable ? "" : "disabled"
          }>${toggleLabel}</button>
        </div>
        <div class="dupe-members">
          ${(g.members || [])
            .map((m) => renderDupeMember(m, { role: m._role || null }))
            .join("")}
        </div>
      </section>`;
    })
    .join("");

  const unmatchedHtml = unmatched.length
    ? (() => {
        const removedMode = photoVisibility === "removed";
        const title = removedMode
          ? `Unmatched rejected (${unmatched.length})`
          : `Unmatched (${unmatched.length})`;
        const toggleLabel = removedMode
          ? `Select rejected (${unmatched.length})`
          : `Select all (${unmatched.length})`;
        const note = removedMode
          ? "No cluster partners at the current level — including no kept active alternative."
          : "No cluster partners at the current level.";
        return `
      <section class="dupe-group dupe-unmatched" data-group="unmatched">
        <div class="dupe-group-head month-divider" role="separator">
          <span class="dupe-group-title">${title}</span>
          <button type="button" class="dupe-select-toggle" data-group="unmatched">${toggleLabel}</button>
        </div>
        <p class="muted dupe-unmatched-note">${note}</p>
        <div class="dupe-members">
          ${unmatched.map((m) => renderDupeMember({ ...m, in_scope: true })).join("")}
        </div>
      </section>`;
      })()
    : "";

  root.innerHTML = groupHtml + unmatchedHtml;

  root.querySelectorAll(".dupe-members [data-id]").forEach((el) => {
    let clickTimer = null;
    el.addEventListener("click", (e) => {
      e.preventDefault();
      if (el.dataset.alt === "1") return;
      if (isCardHoverDetailEvent(e) && hoverDetailHasSelection()) return;
      if (clickTimer) clearTimeout(clickTimer);
      clickTimer = setTimeout(() => {
        clickTimer = null;
        const id = Number(el.dataset.id);
        if (dupeSelected.has(id)) {
          dupeSelected.delete(id);
          el.classList.remove("selected");
        } else {
          dupeSelected.add(id);
          el.classList.add("selected");
        }
        syncDupeToolbar();
      }, 220);
    });
    el.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      const id = Number(el.dataset.id);
      if (!dupeSelected.has(id)) {
        root.querySelectorAll(".dupe-members [data-id].selected").forEach((card) => {
          card.classList.remove("selected");
        });
        dupeSelected.clear();
        dupeSelected.add(id);
        el.classList.add("selected");
        syncDupeToolbar();
      }
      openEditPropertiesModal();
    });
    el.addEventListener("dblclick", (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (clickTimer) {
        clearTimeout(clickTimer);
        clickTimer = null;
      }
      const id = Number(el.dataset.id);
      const groupEl = el.closest(".dupe-group");
      const memberEls = groupEl ? [...groupEl.querySelectorAll(".dupe-members [data-id]")] : [];
      const group = memberEls.map((m) => {
        const mid = Number(m.dataset.id);
        return { src: `/api/original/${mid}`, orientation: m.dataset.orientation, mirrored: m.dataset.mirrored === "1", photo: dupePhotosById.get(mid) || null };
      });
      const groupIndex = memberEls.findIndex((m) => Number(m.dataset.id) === id);
      openLightbox({
        src: `/api/original/${id}`,
        orientation: el.dataset.orientation,
        mirrored: el.dataset.mirrored === "1",
        photo: dupePhotosById.get(id) || null,
        group,
        groupIndex,
      });
    });
  });

  root.querySelectorAll(".dupe-select-toggle").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const group = btn.closest(".dupe-group");
      const { selectedCount } = dupeGroupSelectionState(group);
      if (selectedCount > 0) unselectDupeAll(group);
      else if (group?.dataset.group === "unmatched") selectDupeAll(group);
      else if (photoVisibility === "removed") selectDupeRemoved(group);
      else selectDupeNonPrimary(group);
      syncDupeToolbar();
    });
  });

  syncDupeToolbar();
  applyBrowseTileSize();
  renderBrowseTimeline(browseDisplayItems());
}

const TAB_STORAGE_KEY = "fotoman.activeTab";
const VALID_TABS = new Set(["browse", "map", "dupes"]);
const BROWSE_PANEL_VIEWS = new Set(["stack", "details"]);
const GALLERY_ACTIONS_VIEWS = new Set(["stack", "details", "compare"]);
const TILE_SIZE_VIEWS = new Set(["stack", "compare"]);
const TIMELINE_WORKSPACE_VIEWS = new Set(["stack", "details", "map", "compare"]);
const VALID_WORKSPACE_VIEWS = new Set(["stack", "compare", "map", "details"]);

function syncWorkspaceViewButtons(view) {
  document.querySelectorAll(".browse-view-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.workspaceView === view);
  });
  const showClusterRange =
    view === "compare" || view === "details" || view === "stack";
  syncClusterLevelControls(showClusterRange);
  const sizeControl = $("#browse-size-control");
  if (sizeControl) sizeControl.hidden = !TILE_SIZE_VIEWS.has(view);
  const galleryActions = $(".browse-gallery-actions");
  if (galleryActions) galleryActions.hidden = !GALLERY_ACTIONS_VIEWS.has(view);
  const frameOpt = $("#browse-show-face");
  if (frameOpt) {
    frameOpt.hidden = view !== "stack" || browseView !== "people";
  }
}

function currentWorkspaceView() {
  return (
    document.querySelector(".browse-view-btn.active")?.dataset.workspaceView ||
    (browseGalleryView === "details" ? "details" : "stack")
  );
}

function activateTab(tab, opts = {}) {
  if (!VALID_TABS.has(tab)) tab = "browse";
  const panel = $(`#tab-${tab}`);
  if (!panel) return;
  document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
  panel.classList.add("active");

  let workspaceView = opts.workspaceView;
  if (!workspaceView) {
    if (tab === "browse") {
      workspaceView = browseGalleryView === "details" ? "details" : "stack";
    } else if (tab === "map") workspaceView = "map";
    else if (tab === "dupes") workspaceView = "compare";
    else workspaceView = null;
  }
  if (workspaceView && VALID_WORKSPACE_VIEWS.has(workspaceView)) {
    syncWorkspaceViewButtons(workspaceView);
    try {
      localStorage.setItem(TAB_STORAGE_KEY, workspaceView);
    } catch (_) { /* ignore */ }
  } else {
    syncWorkspaceViewButtons(null);
  }

  if (tab === "map") loadMap(true);
}

function photoIdFromBrowseKey(key) {
  if (!key) return null;
  const s = String(key);
  if (!s.startsWith("id:")) return null;
  const n = Number(s.slice(3));
  return Number.isFinite(n) ? n : null;
}

function photoForBrowseKey(key) {
  if (!key) return null;
  const fromBrowse =
    browseItems.find((p) => browseItemKey(p) === key) ||
    browseItemsFull.find((p) => browseItemKey(p) === key);
  if (fromBrowse) return fromBrowse;
  const id = photoIdFromBrowseKey(key);
  if (id == null) return null;
  return dupePhotosById?.get?.(id) || null;
}

function captureWorkspaceSelection() {
  if (isDupesWorkspace()) {
    const ids = [...dupeSelected];
    const keys = ids.map((id) => `id:${id}`);
    const focusId = ids.length ? ids[ids.length - 1] : null;
    return {
      keys,
      focusKey: focusId != null ? `id:${focusId}` : browseAnchorKey,
    };
  }
  const keys = [...browseSelected];
  const focusKey =
    browseAnchorKey && browseSelected.has(browseAnchorKey)
      ? browseAnchorKey
      : keys.length
        ? keys[keys.length - 1]
        : null;
  return { keys, focusKey };
}

function restoreWorkspaceSelection({ keys, focusKey }) {
  browseSelected = new Set(keys || []);
  browseAnchorKey = focusKey || null;
}

function syncDupeSelectionFromBrowse() {
  const ids = new Set();
  for (const key of browseSelected) {
    const id = photoIdFromBrowseKey(key);
    if (id != null && dupePhotosById?.has?.(id)) ids.add(id);
  }
  dupeSelected = ids;
  document.querySelectorAll("#dupe-list .dupe-members [data-id]").forEach((el) => {
    el.classList.toggle("selected", dupeSelected.has(Number(el.dataset.id)));
  });
  syncDupeToolbar();
}

function ensureVisibleSelectionStandIns() {
  if (!browseKeepersOnlyView()) return;
  const items = browseDisplayItems();
  const byGroup = new Map();
  for (const p of items) {
    if (p?.group_id != null && !byGroup.has(p.group_id)) byGroup.set(p.group_id, p);
  }
  for (const key of [...browseSelected]) {
    if (items.some((p) => browseItemKey(p) === key)) continue;
    const photo = photoForBrowseKey(key);
    if (!photo || photo.group_id == null) continue;
    const standIn = byGroup.get(photo.group_id);
    if (standIn) browseSelected.add(browseItemKey(standIn));
  }
}

function expandDetailsForFocus(key) {
  if (browseGalleryView !== "details" || !key) return;
  const photo = photoForBrowseKey(key);
  if (photo?.group_id == null) return;
  if (isClusterKeeper(photo)) return;
  detailsExpandedGroups.add(String(photo.group_id));
}

function scrollWorkspaceToFocus(key) {
  if (!key) return;
  const run = () => {
    const view = currentWorkspaceView();
    if (view === "map") {
      const tryFocus = (photo) => {
        const lat = Number(photo?.lat);
        const lon = Number(photo?.lon);
        if (photo?.id == null || !Number.isFinite(lat) || !Number.isFinite(lon)) {
          return false;
        }
        focusMapOnPhoto(photo.id, lat, lon);
        return true;
      };
      if (tryFocus(photoForBrowseKey(key))) return;
      for (const k of browseSelected) {
        if (tryFocus(photoForBrowseKey(k))) return;
      }
      return;
    }
    if (isDupesWorkspace()) {
      const id = photoIdFromBrowseKey(key);
      if (id == null) return;
      const el = $("#dupe-list")?.querySelector(
        `.dupe-members [data-id="${CSS.escape(String(id))}"]`
      );
      el?.scrollIntoView({ block: "center", behavior: "auto" });
      return;
    }
    let targetKey = key;
    if (browseKeepersOnlyView()) {
      const photo = photoForBrowseKey(key);
      if (photo?.group_id != null) {
        const standIn = browseDisplayItems().find(
          (p) => p.group_id === photo.group_id
        );
        if (standIn) targetKey = browseItemKey(standIn);
      }
    }
    const el = $("#browse-results")?.querySelector(
      `[data-browse-key="${CSS.escape(targetKey)}"]`
    );
    el?.scrollIntoView({ block: "center", behavior: "auto" });
  };
  requestAnimationFrame(() => requestAnimationFrame(run));
}

/** Stack / Details browse panel (not Compare). */
async function showGalleryOrClusters() {
  // Apply preview keepers locally, switch UI immediately, persist in background.
  if (clustersPreviewDirty && lastClusterPreviewItems) {
    applyClusterAssignmentsToBrowseItems(lastClusterPreviewItems);
  }
  const view = browseGalleryView === "details" ? "details" : "stack";
  activateTab("browse", { workspaceView: view });
  renderBrowseResults();
  void persistClustersPreview();
}

async function activateWorkspaceView(view) {
  // Migrate old workspace names.
  if (view === "dupes") view = "compare";
  if (view === "thumbs") view = "stack";
  if (!VALID_WORKSPACE_VIEWS.has(view)) view = "stack";
  const captured = captureWorkspaceSelection();
  restoreWorkspaceSelection(captured);
  if (BROWSE_PANEL_VIEWS.has(view)) {
    browseGalleryView = view === "details" ? "details" : "thumbs";
    saveBrowseGalleryPref();
    if (clustersPreviewDirty && lastClusterPreviewItems) {
      applyClusterAssignmentsToBrowseItems(lastClusterPreviewItems);
    }
    expandDetailsForFocus(captured.focusKey);
    ensureVisibleSelectionStandIns();
    await showGalleryOrClusters();
    expandDetailsForFocus(captured.focusKey);
    ensureVisibleSelectionStandIns();
    if (view === "details" && captured.focusKey) {
      const el = $("#browse-results")?.querySelector(
        `[data-browse-key="${CSS.escape(captured.focusKey)}"]`
      );
      if (el?.hidden) renderBrowseResults();
    }
    syncBrowseSelectionDom();
    scrollWorkspaceToFocus(captured.focusKey);
  } else if (view === "compare") {
    if (clustersPreviewDirty && lastClusterPreviewItems) {
      applyClusterAssignmentsToBrowseItems(lastClusterPreviewItems);
    }
    debouncedRegroup.cancel?.();
    activateTab("dupes", { workspaceView: "compare" });
    renderBrowseTimeline(browseDisplayItems());
    await regroupClusters({ persist: false });
    restoreWorkspaceSelection(captured);
    syncDupeSelectionFromBrowse();
    scrollWorkspaceToFocus(captured.focusKey);
    void persistClustersPreview();
  } else if (view === "map") {
    if (clustersPreviewDirty && lastClusterPreviewItems) {
      applyClusterAssignmentsToBrowseItems(lastClusterPreviewItems);
    }
    activateTab("map", { workspaceView: "map" });
    renderBrowseTimeline(browseDisplayItems());
    if (photoMapLoadInFlight) await photoMapLoadInFlight;
    setTimeout(() => photoMap?.invalidateSize(), 60);
    scrollWorkspaceToFocus(captured.focusKey);
    void persistClustersPreview();
  }
}

function openArchiveDialog() {
  const dlg = $("#archive-dialog");
  if (!dlg) return;
  loadSources();
  pollFacesStatus();
  dlg.showModal();
}

$("#btn-open-archive")?.addEventListener("click", openArchiveDialog);
$("#btn-close-archive")?.addEventListener("click", () => {
  $("#archive-dialog")?.close();
});

/** Cached from /api/settings — stay | follow after Place edits. */
let placeEditNav = "stay";
/** App version from /api/settings. */
let appVersion = "";
/** "newest" or "oldest" — cached from server settings. */
let dateTreeOrder = "newest";
/** Persist Edit-dialog "Write timestamp to EXIF". */
let writeDateToExif = false;
/** True when VLM provider + API key + model are configured. */
let vqaConfigured = false;
/** Current Ask AI mode: where | describe | freeform */
let lightboxVqaTask = "freeform";
/** Pending Place suggestion from Where-is-this: { previous } or null. */
let placeSuggestPending = null;

function applyCachedSettings(s) {
  if (!s || typeof s !== "object") return;
  placeEditNav = s.place_edit_nav === "follow" ? "follow" : "stay";
  dateTreeOrder = s.date_tree_order === "oldest" ? "oldest" : "newest";
  if (typeof s.write_date_to_exif === "boolean") {
    writeDateToExif = s.write_date_to_exif;
  }
  if (typeof s.version === "string" && s.version.trim()) {
    appVersion = s.version.trim();
  }
  if (typeof s.vqa_configured === "boolean") {
    vqaConfigured = s.vqa_configured;
  } else {
    vqaConfigured =
      !!s.vqa_api_key_set && !!String(s.vqa_base_url || "").trim();
  }
}

async function loadAppSettings() {
  try {
    applyCachedSettings(await api("/api/settings"));
  } catch (_) {
    /* keep defaults */
  }
}

async function openPrefsDialog() {
  const dlg = $("#prefs-dialog");
  if (!dlg) return;
  const err = $("#prefs-error");
  if (err) {
    err.hidden = true;
    err.textContent = "";
  }
  try {
    const s = await api("/api/settings");
    applyCachedSettings(s);
    const nearEl = $("#pref-near-dup");
    const faceEl = $("#pref-face-match");
    const placeNavEl = $("#pref-place-edit-nav");
    const writeMtimeEl = $("#pref-write-date-mtime");
    const copiesEl = $("#pref-collection-copies");
    const keyEl = $("#pref-vqa-api-key");
    const baseEl = $("#pref-vqa-base-url");
    const modelVqaEl = $("#pref-vqa-model");
    if (nearEl) nearEl.value = s.near_dup_threshold ?? "";
    if (faceEl) faceEl.value = s.face_match_threshold ?? "";
    if (placeNavEl) placeNavEl.checked = placeEditNav === "follow";
    if (writeMtimeEl) writeMtimeEl.checked = !!s.write_date_to_mtime;
    if (copiesEl) copiesEl.checked = s.collection_copies === "cluster";
    const cropEl = $("#pref-thumb-crop");
    if (cropEl) cropEl.checked = isThumbCropOn();
    if (keyEl) {
      keyEl.value = s.vqa_api_key_set ? VQA_KEY_MASK : "";
      keyEl.placeholder = "sk-…";
    }
    if (baseEl) baseEl.value = s.vqa_base_url || "";
    if (modelVqaEl) modelVqaEl.value = s.vqa_model || "gpt-4o-mini";
  } catch (ex) {
    if (err) {
      err.textContent = ex.message || "Failed to load preferences";
      err.hidden = false;
    }
    const modelVqaEl = $("#pref-vqa-model");
    if (modelVqaEl && !modelVqaEl.value) modelVqaEl.value = "gpt-4o-mini";
    const cropEl = $("#pref-thumb-crop");
    if (cropEl) cropEl.checked = isThumbCropOn();
  }
  dlg.showModal();
}

function drawQr(host, url) {
  if (!host) return;
  host.replaceChildren();
  if (!url || typeof QRCode !== "function") {
    host.hidden = true;
    return;
  }
  host.hidden = false;
  try {
    new QRCode(host, {
      text: url,
      width: 192,
      height: 192,
      colorDark: "#1a1f1c",
      colorLight: "#ffffff",
      correctLevel: QRCode.CorrectLevel.M,
    });
  } catch {
    host.hidden = true;
  }
}

function drawShareQr(url) {
  drawQr($("#share-info-qr"), url);
}

function openShareInfoDialog({ name, url } = {}) {
  const dlg = $("#share-info-dialog");
  if (!dlg) return;
  const title = $("#share-info-title");
  const urlEl = $("#share-info-url");
  if (title) title.textContent = name ? `Share “${name}”` : "Shared on Fotomob";
  if (urlEl) urlEl.value = url || "";
  drawShareQr(url || "");
  dlg.showModal();
}

$("#btn-close-share-info")?.addEventListener("click", () => {
  $("#share-info-dialog")?.close();
});
$("#share-info-dialog")?.addEventListener("click", (e) => {
  if (e.target === e.currentTarget) e.currentTarget.close();
});
$("#btn-share-info-copy")?.addEventListener("click", async () => {
  const url = ($("#share-info-url")?.value || "").trim();
  if (!url) return;
  try {
    await navigator.clipboard.writeText(url);
  } catch {
    $("#share-info-url")?.select();
  }
});

function openPairDeviceDialog(src) {
  const dlg = $("#pair-device-dialog");
  if (!dlg || !src) return;
  const title = $("#pair-device-title");
  const urlEl = $("#pair-device-url");
  const url = src.pair_url || src.pair_id || "";
  if (title) title.textContent = src.label ? `Pair “${src.label}”` : "Pair phone";
  if (urlEl) urlEl.value = url;
  drawQr($("#pair-device-qr"), url);
  dlg.showModal();
}

$("#btn-close-pair-device")?.addEventListener("click", () => {
  $("#pair-device-dialog")?.close();
});
$("#pair-device-dialog")?.addEventListener("click", (e) => {
  if (e.target === e.currentTarget) e.currentTarget.close();
});
$("#btn-pair-device-copy")?.addEventListener("click", async () => {
  const url = ($("#pair-device-url")?.value || "").trim();
  if (!url) return;
  try {
    await navigator.clipboard.writeText(url);
  } catch {
    $("#pair-device-url")?.select();
  }
});

$("#btn-open-prefs")?.addEventListener("click", openPrefsDialog);
$("#btn-close-prefs")?.addEventListener("click", () => {
  $("#prefs-dialog")?.close();
});

function openAboutDialog() {
  const dlg = $("#about-dialog");
  if (!dlg) return;
  const ver = $("#about-version");
  if (ver) ver.textContent = appVersion || "0.1.0";
  dlg.showModal();
}

$("#btn-open-about")?.addEventListener("click", openAboutDialog);
$("#btn-close-about")?.addEventListener("click", () => {
  $("#about-dialog")?.close();
});
$("#about-dialog")?.addEventListener("click", (e) => {
  if (e.target === e.currentTarget) e.currentTarget.close();
});

$("#prefs-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#prefs-error");
  const saveBtn = $("#btn-save-prefs");
  if (err) {
    err.hidden = true;
    err.textContent = "";
  }
  const body = {
    near_dup_threshold: Number($("#pref-near-dup")?.value),
    face_match_threshold: Number($("#pref-face-match")?.value),
    date_tree_order: dateTreeOrder,
    place_edit_nav: $("#pref-place-edit-nav")?.checked ? "follow" : "stay",
    write_date_to_exif: writeDateToExif,
    write_date_to_mtime: !!$("#pref-write-date-mtime")?.checked,
    collection_copies: $("#pref-collection-copies")?.checked ? "cluster" : "primary",
    vqa_base_url: ($("#pref-vqa-base-url")?.value || "").trim(),
    vqa_model: ($("#pref-vqa-model")?.value || "").trim(),
  };
  const keyEl = $("#pref-vqa-api-key");
  const keyVal = keyEl?.value;
  if (keyVal != null && keyVal !== VQA_KEY_MASK) {
    // Empty clears the saved key (turns off AI support); any other value replaces it.
    body.vqa_api_key = keyVal;
  }
  setThumbCropPref(!!$("#pref-thumb-crop")?.checked);
  if (saveBtn) saveBtn.disabled = true;
  try {
    const s = await api("/api/settings", { method: "POST", body: JSON.stringify(body) });
    applyCachedSettings(s);
    $("#prefs-dialog")?.close();
    if ($("#lightbox")?.open) syncLightboxVqa(lightboxCurrentPhoto());
    await refreshExpandedBranches(["dates"]);
    await reloadBrowseGallery();
  } catch (ex) {
    if (err) {
      err.textContent = ex.message || "Save failed";
      err.hidden = false;
    }
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
});

let photoMap = null;
let photoMapCluster = null;
let photoMapLoaded = false;
/** @type {Promise<void> | null} */
let photoMapLoadInFlight = null;
/** Re-run loadMap after the in-flight request (scope often arrives mid-fetch). */
let photoMapReloadQueued = false;
/** @type {Array<[number, number]> | null} */
let photoMapBounds = null;
/** @type {L.Rectangle | null} */
let mapSelectRect = null;
let mapSelectDrawing = false;
/** @type {L.LatLng | null} */
let mapSelectStart = null;
let mapRegionActive = false;
let mapRegionCount = 0;
/** @type {L.LatLngBounds | null} */
let mapRegionBounds = null;
let mapRegionApplyInProgress = false;
let mapPinJustDragged = false;

/** Leaflet zoom for a shared pin cluster (~subcontinent / large region). */
const MAP_SINGLE_LOCATION_ZOOM = 4;

function mapBoundsAreSingleLocation(bounds) {
  if (!bounds?.length) return false;
  const [lat0, lon0] = bounds[0];
  const eps = 1e-5;
  return bounds.every(
    ([lat, lon]) => Math.abs(lat - lat0) < eps && Math.abs(lon - lon0) < eps
  );
}

function fitMapLatLngs(latlngs, { padding = [36, 36], maxZoom = 17 } = {}) {
  if (!photoMap || !latlngs?.length) return;
  if (mapBoundsAreSingleLocation(latlngs.map((ll) => [ll.lat ?? ll[0], ll.lng ?? ll[1]]))) {
    const first = latlngs[0];
    const lat = first.lat ?? first[0];
    const lon = first.lng ?? first[1];
    photoMap.setView([lat, lon], MAP_SINGLE_LOCATION_ZOOM);
    return;
  }
  photoMap.fitBounds(L.latLngBounds(latlngs), { padding, maxZoom });
}

function fitPhotoMapToPins() {
  if (!photoMap) return;
  const run = () => {
    photoMap.invalidateSize();
    if (photoMapBounds?.length) {
      fitMapLatLngs(
        photoMapBounds.map(([lat, lon]) => L.latLng(lat, lon)),
        { padding: [36, 36], maxZoom: 17 }
      );
    }
  };
  requestAnimationFrame(() => setTimeout(run, 50));
}

function clearMapSelectRect() {
  if (mapSelectRect && photoMap) {
    photoMap.removeLayer(mapSelectRect);
  }
  mapSelectRect = null;
}

function drawMapSelectRect(bounds) {
  if (!photoMap || !bounds) return;
  clearMapSelectRect();
  mapSelectRect = L.rectangle(bounds, {
    className: "map-select-rect",
    interactive: false,
  }).addTo(photoMap);
}

function mapStatusIdleText() {
  const n = photoMapCluster?.getLayers()?.length ?? 0;
  if (!n) return null;
  return `${n} photo${n === 1 ? "" : "s"} on map · Drag a pin to set GPS · Shift-drag to select`;
}

function clearMapRegionSelection({ clearGallerySelection = false } = {}) {
  clearMapSelectRect();
  mapRegionActive = false;
  mapRegionCount = 0;
  mapRegionBounds = null;
  if (clearGallerySelection) {
    browseSelected.clear();
    browseAnchorKey = null;
    syncBrowseSelectionDom();
  }
  const status = $("#map-status");
  const idle = mapStatusIdleText();
  if (status && photoMapLoaded && idle) status.textContent = idle;
}

function applyMapRegionBounds(bounds, { additive = false } = {}) {
  if (!bounds || !photoMapCluster) return;
  const ids = new Set();
  for (const marker of photoMapCluster.getLayers()) {
    const id = marker.options?.photoId;
    if (id == null) continue;
    if (bounds.contains(marker.getLatLng())) ids.add(id);
  }
  const pool = browseItemsFull.length ? browseItemsFull : browseItems;
  if (!additive) browseSelected.clear();
  let lastKey = null;
  let matched = 0;
  for (const p of pool) {
    if (p.id == null || !ids.has(p.id)) continue;
    const key = browseItemKey(p);
    browseSelected.add(key);
    lastKey = key;
    matched += 1;
  }
  if (lastKey != null) browseAnchorKey = lastKey;
  mapRegionActive = matched > 0;
  mapRegionCount = matched;
  mapRegionBounds = bounds;
  drawMapSelectRect(bounds);
  mapRegionApplyInProgress = true;
  syncBrowseSelectionDom();
  mapRegionApplyInProgress = false;
  const status = $("#map-status");
  if (status) {
    status.textContent = matched
      ? `${matched} photo${matched === 1 ? "" : "s"} selected · click map to clear`
      : "No photos in that region";
  }
}

function mapPhotosFromMarkers(markers) {
  const photos = (markers || [])
    .map((m) => m.options?.photo)
    .filter((p) => p?.id != null);
  photos.sort((a, b) => {
    const ak = isClusterKeeper(a) ? 0 : 1;
    const bk = isClusterKeeper(b) ? 0 : 1;
    if (ak !== bk) return ak - bk;
    return (Number(b.id) || 0) - (Number(a.id) || 0);
  });
  return photos;
}

function openMapLightbox(photos, startPhoto) {
  if (!photos?.length) return;
  const p = startPhoto || photos[0];
  const idx = Math.max(
    0,
    photos.findIndex((x) => x.id === p.id)
  );
  const cur = photos[idx] || p;
  openLightbox({
    src: `/api/original/${cur.id}`,
    orientation: cur.orientation,
    mirrored: isMirrored(cur),
    photo: cur,
    group: photos.map((x) => ({
      src: `/api/original/${x.id}`,
      orientation: x.orientation,
      mirrored: isMirrored(x),
      photo: x,
    })),
    groupIndex: idx,
  });
}

function photoPinIcon(photo) {
  const removed = photo.culled ? " removed" : "";
  const style = orientationStyle(photo);
  return L.divIcon({
    className: `photo-pin${removed}`,
    html: `<img src="/api/thumb/${photo.id}" alt="" draggable="false" loading="lazy"${style ? ` style="${style}"` : ""} />`,
    iconSize: [28, 28],
    iconAnchor: [14, 28],
    popupAnchor: [0, -26],
  });
}

/** Wider clusters when zoomed out; keep nearby pins stacked at all zooms. */
function photoClusterRadius(zoom) {
  if (zoom <= 3) return 100;
  if (zoom <= 6) return 80;
  if (zoom <= 10) return 60;
  if (zoom <= 14) return 50;
  return 40;
}

function photoClusterIcon(cluster) {
  const kids = cluster.getAllChildMarkers() || [];
  const count = cluster.getChildCount();
  const keeper = kids.find((m) => isClusterKeeper(m.options?.photo)) || kids[0];
  const thumbId = keeper?.options?.photoId;
  const size = count < 10 ? "small" : count < 100 ? "medium" : "large";
  const thumb = thumbId
    ? `<img class="photo-cluster-thumb" src="/api/thumb/${thumbId}" alt="" loading="lazy" />`
    : "";
  return L.divIcon({
    className: `photo-cluster photo-cluster-${size}`,
    html: `${thumb}<span class="photo-cluster-count">${count}</span>`,
    iconSize: [32, 32],
    iconAnchor: [16, 32],
  });
}

function ensurePhotoMap() {
  if (photoMap) {
    setTimeout(() => photoMap.invalidateSize(), 50);
    return photoMap;
  }
  photoMap = L.map("photo-map", {
    center: [20, 0],
    zoom: 2,
    worldCopyJump: true,
    boxZoom: false,
  });
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    maxZoom: 19,
  }).addTo(photoMap);
  photoMapCluster = L.markerClusterGroup({
    showCoverageOnHover: false,
    maxClusterRadius: photoClusterRadius,
    spiderfyOnMaxZoom: false,
    zoomToBoundsOnClick: false,
    iconCreateFunction: photoClusterIcon,
  });
  photoMap.addLayer(photoMapCluster);
  photoMapCluster.on("clustermouseover", (e) => {
    const cluster = e.layer;
    const kids = cluster.getAllChildMarkers?.() || [];
    const first =
      kids.find((m) => isClusterKeeper(m.options?.photo))?.options?.photo ||
      kids[0]?.options?.photo;
    if (!first) return;
    cluster.unbindTooltip();
    cluster.bindTooltip(mapPinHoverHtml(first, { count: kids.length }), {
      direction: "top",
      offset: [0, -32],
      opacity: 1,
      className: "map-pin-tip-wrap",
    }).openTooltip();
  });
  photoMapCluster.on("clusterclick", (e) => {
    L.DomEvent.stop(e.originalEvent);
    mapPinJustDragged = true;
    setTimeout(() => {
      mapPinJustDragged = false;
    }, 0);
    const kids = e.layer.getAllChildMarkers?.() || [];
    const photos = mapPhotosFromMarkers(kids);
    if (!photos.length) return;
    openMapLightbox(photos, photos[0]);
  });
  bindMapRegionSelect(photoMap);
  setTimeout(() => photoMap.invalidateSize(), 50);
  return photoMap;
}

function patchPhotoCoords(photo, lat, lon, coordSource = "manual") {
  if (!photo) return;
  const has = lat != null && lon != null;
  photo.lat = has ? lat : null;
  photo.lon = has ? lon : null;
  photo.coord_source = has ? coordSource : null;
  photo.gps = has && coordSource === "exif" ? 1 : 0;
}

function patchPhotoPlaceFields(photo, place) {
  if (!photo || !place) return;
  photo.place_label = place.place_label || null;
  photo.venue = place.venue || null;
  photo.city = place.city || null;
  photo.region = place.region || null;
  photo.country = place.country || null;
}

function patchLocalCoordsById(id, lat, lon, coordSource = "manual", place = null) {
  if (id == null) return;
  const patch = (p) => {
    if (!p) return;
    patchPhotoCoords(p, lat, lon, coordSource);
    if (place) patchPhotoPlaceFields(p, place);
  };
  for (const p of browseItems) if (p.id === id) patch(p);
  for (const p of browseItemsFull) if (p.id === id) patch(p);
  const dupe = dupePhotosById?.get?.(id);
  if (dupe) patch(dupe);
  const lb = typeof lightboxCurrentPhoto === "function" ? lightboxCurrentPhoto() : null;
  if (lb?.id === id) {
    patch(lb);
    renderLightboxMeta(lb);
    syncLightboxShowMapBtn(lb);
    refreshLightboxFileMeta(lb);
    const placeEl = $("#lightbox-place");
    if (placeEl && place) placeEl.value = (lb.place_label || "").trim();
  }
}

function syncPhotoMapBoundsFromMarkers() {
  if (!photoMapCluster) {
    photoMapBounds = null;
    return;
  }
  const bounds = [];
  for (const m of photoMapCluster.getLayers()) {
    const ll = m.getLatLng();
    bounds.push([ll.lat, ll.lng]);
  }
  photoMapBounds = bounds.length ? bounds : null;
}

function mapPinHoverHtml(photo, { count = 1 } = {}) {
  if (!photo?.id) return "";
  const name = escapeHtml(basename(photo.path || photo.rel_path) || "Photo");
  const style = orientationStyle(photo);
  const bits = [
    count > 1 ? `${count} photos` : "",
    photo.taken_at ? String(photo.taken_at).slice(0, 10) : "undated",
    photo.place_label || "",
    photo.camera || "",
    formatDims(photo) !== "—" ? formatDims(photo) : "",
  ].filter(Boolean);
  if (photo.lat != null && photo.lon != null) {
    bits.push(`${Number(photo.lat).toFixed(4)}, ${Number(photo.lon).toFixed(4)}`);
  }
  return `<div class="map-pin-tip">
    <img src="/api/thumb/${photo.id}" alt="" draggable="false"${style ? ` style="${style}"` : ""} />
    <div class="map-pin-tip-meta">
      <strong>${name}</strong>
      ${bits.map((b) => `<span>${escapeHtml(b)}</span>`).join("")}
    </div>
  </div>`;
}

function bindMapPinTooltip(marker) {
  const photo = marker.options?.photo;
  if (!photo) return;
  marker.unbindTooltip();
  marker.bindTooltip(mapPinHoverHtml(photo), {
    direction: "top",
    offset: [0, -28],
    opacity: 1,
    className: "map-pin-tip-wrap",
  });
}

function mapMarkerByPhotoId(id) {
  if (id == null || !photoMapCluster) return null;
  for (const m of photoMapCluster.getLayers()) {
    if (m.options?.photoId === id) return m;
  }
  return null;
}

function mapMarkersForPinDrag(primary) {
  const primaryId = primary.options?.photoId;
  const selected = typeof selectedBrowsePhotos === "function" ? selectedBrowsePhotos() : [];
  const ids = new Set(selected.map((p) => p.id).filter((id) => id != null));
  if (primaryId == null || !ids.has(primaryId) || ids.size < 2) return [primary];
  const found = [];
  for (const m of photoMapCluster?.getLayers() || []) {
    if (ids.has(m.options?.photoId)) found.push(m);
  }
  return found.length ? found : [primary];
}

let mapPinConfirmResolve = null;

function settleMapPinConfirm(ok) {
  $("#map-pin-confirm")?.close();
  const resolve = mapPinConfirmResolve;
  mapPinConfirmResolve = null;
  if (resolve) resolve(!!ok);
}

function confirmMapPinUpdate(message) {
  const dlg = $("#map-pin-confirm");
  const msg = $("#map-pin-confirm-msg");
  if (!dlg || !msg) return Promise.resolve(window.confirm(message));
  msg.textContent = message;
  return new Promise((resolve) => {
    mapPinConfirmResolve = resolve;
    dlg.showModal();
  });
}

async function previewReversePlace(lat, lon) {
  try {
    const res = await api(
      `/api/geocode/reverse?lat=${encodeURIComponent(lat)}&lon=${encodeURIComponent(lon)}`
    );
    return (res?.place_label || "").trim();
  } catch {
    return "";
  }
}

function mapDropLatLngFromEvent(e) {
  const mapEl = $("#photo-map");
  if (!photoMap || !mapEl) return null;
  if (!$("#tab-map")?.classList.contains("active")) return null;
  const rect = mapEl.getBoundingClientRect();
  if (
    e.clientX < rect.left ||
    e.clientX > rect.right ||
    e.clientY < rect.top ||
    e.clientY > rect.bottom
  ) {
    return null;
  }
  return photoMap.containerPointToLatLng(
    L.point(e.clientX - rect.left, e.clientY - rect.top)
  );
}

async function applyMapPhotoDrop(photos, latlng) {
  const list = (photos || []).filter((p) => p?.id != null);
  if (!list.length || !latlng) return;
  const lat = latlng.lat;
  const lon = latlng.lng;
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
  const status = $("#map-status");
  if (status) status.textContent = "Checking location…";
  const placeLabel = await previewReversePlace(lat, lon);
  const n = list.length;
  const coord = `${Number(lat).toFixed(5)}, ${Number(lon).toFixed(5)}`;
  const who = n === 1 ? "this photo" : `${n} photos`;
  const where = placeLabel ? `${placeLabel} (${coord})` : coord;
  const ok = await confirmMapPinUpdate(`Set location of ${who} to ${where}?`);
  if (!ok) {
    const idle = mapStatusIdleText();
    if (status && idle) status.textContent = idle;
    return;
  }
  if (status) status.textContent = "Saving coordinates…";
  try {
    const updates = list.map((photo) => ({
      id: photo.id,
      lat,
      lon,
      photo,
      marker: mapMarkerByPhotoId(photo.id),
    }));
    await savePhotoCoordsBatch(updates);
    const missingPins = updates.some((u) => !u.marker);
    if (missingPins) {
      photoMapLoaded = false;
      await loadMap(true);
    } else if (typeof photoMapCluster.refreshClusters === "function") {
      photoMapCluster.refreshClusters();
      syncPhotoMapBoundsFromMarkers();
    }
    if (typeof refreshExpandedBranches === "function") {
      refreshExpandedBranches(["places"]).catch(() => {});
    }
    if (status) {
      status.textContent = placeLabel ? `Saved ${placeLabel}` : `Saved ${coord}`;
      setTimeout(() => {
        if (status.textContent?.startsWith("Saved ")) {
          const idle = mapStatusIdleText();
          if (idle) status.textContent = idle;
        }
      }, 2000);
    }
  } catch (err) {
    if (status) status.textContent = err.message || "Could not save coordinates";
  }
}

async function savePhotoCoordsBatch(updates) {
  if (!updates?.length) return { ok: false };
  const first = updates[0];
  const same = updates.every(
    (u) => u.lat === first.lat && u.lon === first.lon
  );
  const groups = same
    ? [updates]
    : updates.map((u) => [u]);
  let lastRes = null;
  for (const group of groups) {
    const res = await api("/api/photos/properties", {
      method: "POST",
      body: JSON.stringify({
        photo_ids: group.map((u) => u.id),
        properties: { lat: group[0].lat, lon: group[0].lon },
      }),
    });
    if (res.errors?.length && !res.updated) {
      throw new Error(res.errors[0].error || "Could not save coordinates");
    }
    lastRes = res;
    const newLat = res.lat ?? group[0].lat;
    const newLon = res.lon ?? group[0].lon;
    const src = res.coord_source || "manual";
    const place = res.place || null;
    for (const u of group) {
      patchPhotoCoords(u.photo, newLat, newLon, src);
      if (place) patchPhotoPlaceFields(u.photo, place);
      patchLocalCoordsById(u.id, newLat, newLon, src, place);
      if (u.marker) {
        u.marker.setLatLng([newLat, newLon]);
        bindMapPinTooltip(u.marker);
      }
    }
  }
  if (typeof renderBrowseResults === "function" && browseItems.length) {
    renderBrowseResults();
  }
  return { ok: true, res: lastRes };
}

async function saveMapPinCoords(markers) {
  const list = (markers || []).filter(Boolean);
  const status = $("#map-status");
  const revert = () => {
    for (const marker of list) {
      if (marker._pinOrigin) marker.setLatLng(marker._pinOrigin);
    }
    if (typeof photoMapCluster.refreshClusters === "function") {
      photoMapCluster.refreshClusters(list);
    }
  };
  const updates = [];
  for (const marker of list) {
    const photo = marker.options?.photo;
    const id = marker.options?.photoId ?? photo?.id;
    const ll = marker.getLatLng();
    if (id == null || !Number.isFinite(ll.lat) || !Number.isFinite(ll.lng)) {
      revert();
      return;
    }
    updates.push({ id, lat: ll.lat, lon: ll.lng, marker, photo });
  }
  if (!updates.length) {
    revert();
    return;
  }
  const primary = updates[0];
  if (status) status.textContent = "Checking location…";
  const placeLabel = await previewReversePlace(primary.lat, primary.lon);
  const n = updates.length;
  const coord = `${Number(primary.lat).toFixed(5)}, ${Number(primary.lon).toFixed(5)}`;
  const who = n === 1 ? "this photo" : `${n} photos`;
  const where = placeLabel ? `${placeLabel} (${coord})` : coord;
  const ok = await confirmMapPinUpdate(`Update location of ${who} to ${where}?`);
  if (!ok) {
    revert();
    const idle = mapStatusIdleText();
    if (status && idle) status.textContent = idle;
    return;
  }
  if (status) status.textContent = "Saving coordinates…";
  try {
    await savePhotoCoordsBatch(updates);
    if (typeof photoMapCluster.refreshClusters === "function") {
      photoMapCluster.refreshClusters(list);
    }
    syncPhotoMapBoundsFromMarkers();
    if (typeof refreshExpandedBranches === "function") {
      refreshExpandedBranches(["places"]).catch(() => {});
    }
    if (status) {
      status.textContent = placeLabel
        ? `Saved ${placeLabel}`
        : `Saved ${coord}`;
      setTimeout(() => {
        if (status.textContent?.startsWith("Saved ")) {
          const idle = mapStatusIdleText();
          if (idle) status.textContent = idle;
        }
      }, 2000);
    }
  } catch (err) {
    revert();
    if (status) status.textContent = err.message || "Could not save coordinates";
  }
}

function bindMapPinDrag(marker) {
  marker.on("mousedown", (e) => {
    if (!marker.dragging) return;
    if (e.originalEvent?.shiftKey) marker.dragging.disable();
    else if (!marker.dragging.enabled()) marker.dragging.enable();
  });
  marker.on("mouseup", () => {
    if (marker.dragging && !marker.dragging.enabled()) marker.dragging.enable();
  });
  marker.on("dragstart", () => {
    marker.closeTooltip?.();
    const group = mapMarkersForPinDrag(marker);
    marker._pinGroup = group;
    for (const m of group) {
      m._pinMoved = false;
      m._pinOrigin = m.getLatLng();
    }
    mapPinJustDragged = true;
    photoMap?.getContainer()?.classList.add("map-pin-dragging");
  });
  marker.on("drag", () => {
    marker._pinMoved = true;
    const origin = marker._pinOrigin;
    if (!origin) return;
    const now = marker.getLatLng();
    const dLat = now.lat - origin.lat;
    const dLng = now.lng - origin.lng;
    for (const m of marker._pinGroup || []) {
      if (m === marker) continue;
      const o = m._pinOrigin;
      if (!o) continue;
      m._pinMoved = true;
      m.setLatLng([o.lat + dLat, o.lng + dLng]);
    }
  });
  marker.on("dragend", () => {
    photoMap?.getContainer()?.classList.remove("map-pin-dragging");
    setTimeout(() => {
      mapPinJustDragged = false;
    }, 0);
    const group = marker._pinGroup || [marker];
    marker._pinGroup = null;
    if (!group.some((m) => m._pinMoved)) return;
    saveMapPinCoords(group);
  });
}

function bindMapRegionSelect(map) {
  const container = map.getContainer();
  let suppressClick = false;

  const onMove = (e) => {
    if (!mapSelectDrawing || !mapSelectStart || !mapSelectRect) return;
    const latlng = map.mouseEventToLatLng(e);
    mapSelectRect.setBounds(L.latLngBounds(mapSelectStart, latlng));
  };

  const onUp = (e) => {
    if (!mapSelectDrawing) return;
    mapSelectDrawing = false;
    container.classList.remove("map-selecting");
    map.dragging.enable();
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
    if (!mapSelectStart || !mapSelectRect) return;
    const latlng = map.mouseEventToLatLng(e);
    const bounds = L.latLngBounds(mapSelectStart, latlng);
    mapSelectStart = null;
    // Ignore tiny accidental drags
    const sw = map.latLngToContainerPoint(bounds.getSouthWest());
    const ne = map.latLngToContainerPoint(bounds.getNorthEast());
    if (Math.abs(sw.x - ne.x) < 8 || Math.abs(sw.y - ne.y) < 8) {
      clearMapSelectRect();
      return;
    }
    mapSelectRect.setBounds(bounds);
    suppressClick = true;
    setTimeout(() => {
      suppressClick = false;
    }, 0);
    applyMapRegionBounds(bounds);
  };

  map.on("mousedown", (e) => {
    if (!e.originalEvent.shiftKey || e.originalEvent.button !== 0) return;
    L.DomEvent.stop(e.originalEvent);
    map.dragging.disable();
    mapSelectDrawing = true;
    mapSelectStart = e.latlng;
    clearMapSelectRect();
    mapSelectRect = L.rectangle(L.latLngBounds(e.latlng, e.latlng), {
      className: "map-select-rect",
      interactive: false,
    }).addTo(map);
    container.classList.add("map-selecting");
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });

  map.on("click", () => {
    if (suppressClick || mapSelectDrawing || mapPinJustDragged) return;
    if (!mapRegionBounds && !mapRegionActive) return;
    clearMapRegionSelection({ clearGallerySelection: true });
  });
}

function browseScopedPhotoIds() {
  return new Set(browseItems.map((p) => p.id).filter((id) => id != null));
}

function refreshScopedWorkspaceViews() {
  const active = document.querySelector(".panel.active")?.id;
  if (active === "tab-map") loadMap(true);
  else if (active === "tab-dupes") regroupClusters();
}

async function loadMap(force = false) {
  ensurePhotoMap();
  const status = $("#map-status");
  if (photoMapLoaded && !force) {
    fitPhotoMapToPins();
    return;
  }
  // Gallery scope often fills in while /api/map/photos is in flight — queue a
  // reload instead of dropping the forced refresh.
  if (photoMapLoadInFlight) {
    if (force) photoMapReloadQueued = true;
    return photoMapLoadInFlight;
  }
  status.innerHTML = loadingMarkup("Loading pins", "inline");
  photoMapReloadQueued = false;
  photoMapLoadInFlight = (async () => {
    try {
      const q = includeCulled() ? "?include_culled=true" : "";
      const all = await api(`/api/map/photos${q}`);
      // Re-read after await: tree/gallery may have loaded during the fetch.
      const scopeIds = browseScopedPhotoIds();
      photoMapCluster.clearLayers();
      photoMapBounds = null;
      clearMapSelectRect();
      if (!scopeIds.size) {
        clearMapRegionSelection();
        status.textContent = "Select items in the tree to show photos on the map.";
        photoMapLoaded = true;
        fitPhotoMapToPins();
        return;
      }
      const photos = filterByAspect(
        filterByPhotoVisibility(
          all.filter((p) => scopeIds.has(p.id)),
          { respectSelection: true }
        )
      );
      if (!photos.length) {
        status.textContent = "No GPS photos in the current selection.";
        photoMapLoaded = true;
        fitPhotoMapToPins();
        return;
      }
      const bounds = [];
      const markers = [];
      for (const p of photos) {
        const lat = Number(p.lat);
        const lon = Number(p.lon);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
        bounds.push([lat, lon]);
        const marker = L.marker([lat, lon], {
          icon: photoPinIcon(p),
          photoId: p.id,
          photo: p,
          draggable: true,
          autoPan: true,
        });
        bindMapPinDrag(marker);
        bindMapPinTooltip(marker);
        marker.on("click", () => {
          if (marker._pinMoved) {
            marker._pinMoved = false;
            return;
          }
          openMapLightbox(photos, p);
        });
        markers.push(marker);
      }
      photoMapCluster.addLayers(markers);
      photoMapBounds = bounds.length ? bounds : null;
      if (mapRegionActive && mapRegionBounds) {
        drawMapSelectRect(mapRegionBounds);
        status.textContent = mapRegionCount
          ? `${mapRegionCount} photo${mapRegionCount === 1 ? "" : "s"} selected · click map to clear`
          : "No photos in that region";
      } else {
        status.textContent = mapStatusIdleText() || `${photos.length} photos on map`;
      }
      photoMapLoaded = true;
      fitPhotoMapToPins();
    } catch (err) {
      status.textContent = err.message;
    } finally {
      photoMapLoadInFlight = null;
      if (photoMapReloadQueued) {
        photoMapReloadQueued = false;
        void loadMap(true);
      }
    }
  })();
  return photoMapLoadInFlight;
}


$("#search-form")?.addEventListener("submit", runSearch);
$("#aspect-filter")?.addEventListener("change", async () => {
  syncAspectFilterStyle();
  saveAspectFilterPref();
  setRegroupBusy(true);
  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  try {
    renderBrowseResults();
    await refreshScopedWorkspaceViews();
  } finally {
    setRegroupBusy(false);
  }
});

$("#btn-pair-device")?.addEventListener("click", async () => {
  const btn = $("#btn-pair-device");
  const status = $("#index-status");
  if (!btn) return;
  btn.disabled = true;
  try {
    const src = await api("/api/sources/pair-device", {
      method: "POST",
      body: JSON.stringify({}),
    });
    loadSources();
    openPairDeviceDialog(src);
  } catch (err) {
    if (status) status.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

$("#btn-add-source")?.addEventListener("click", async () => {
  const btn = $("#btn-add-source");
  if (!btn) return;
  btn.disabled = true;
  try {
    const picked = await api("/api/pick-directory", { method: "POST" });
    if (picked.cancelled || !picked.path) return;
    const added = await api("/api/sources", {
      method: "POST",
      body: JSON.stringify({ path: picked.path }),
    });
    if (added.status) lastIndexStatus = added.status;
    loadSources();
    refreshStats();
    if (added.indexing) pollIndex();
  } catch (err) {
    const status = $("#index-status");
    if (status) status.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

$("#btn-import-zip")?.addEventListener("click", async () => {
  const btn = $("#btn-import-zip");
  if (!btn) return;
  btn.disabled = true;
  const status = $("#index-status");
  try {
    const picked = await api("/api/pick-zip", { method: "POST" });
    if (picked.cancelled || !picked.path) return;
    const started = await api("/api/sources/import-zip", {
      method: "POST",
      body: JSON.stringify({ zip_path: picked.path }),
    });
    if (started.cancelled) return;
    lastIndexStatus = { ...(lastIndexStatus || {}), import: started };
    loadSources();
    pollIndex();
  } catch (err) {
    if (status) status.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

const CLUSTER_METHOD_KEY = "fotoman.clusterMethod";
const CLUSTER_METHODS = new Set(["HASH_VECTOR", "SCENE_MATCH"]);

function clusterMethodFromSelect() {
  const v = String($("#cluster-method")?.value || "HASH_VECTOR").toUpperCase();
  return CLUSTER_METHODS.has(v) ? v : "HASH_VECTOR";
}

function loadClusterMethodPref() {
  const el = $("#cluster-method");
  if (!el) return;
  try {
    const saved = localStorage.getItem(CLUSTER_METHOD_KEY);
    if (saved && CLUSTER_METHODS.has(saved)) el.value = saved;
  } catch (_) { /* ignore */ }
}

function persistClusterMethodPref() {
  try {
    localStorage.setItem(CLUSTER_METHOD_KEY, clusterMethodFromSelect());
  } catch (_) { /* ignore */ }
}

/** Slider is Cluster range (small→large left→right) = internal level 0=strict…7=loose. */
function clusterLevelFromSlider() {
  const v = Number($("#cluster-level")?.value);
  return Math.max(0, Math.min(7, Number.isFinite(v) ? v : 0));
}

function clusterLevelConfig(level) {
  const n = Math.max(0, Math.min(7, Number(level) || 0));
  const near = n >= 3;
  return {
    exact: true,
    formatted: n >= 1, // levels 1–2 (framed uses formatted for now)
    near,
    // Burst→Familiar: 0.98, 0.95, 0.92, 0.89, 0.86 (retakes often land ~0.87)
    threshold: near ? 0.98 - (n - 3) * 0.03 : undefined,
  };
}

function syncClusterLevelControls(show) {
  const control = $(".cluster-level-control");
  if (control) control.hidden = !show;
}

function clusterRangeRegroupsView() {
  return isDupesWorkspace() || clusterRangeAffectsBrowse();
}

function setRegroupBusy(busy, { onWatermark = false } = {}) {
  const overlay = $("#regroup-overlay");
  if (!overlay) return;
  if (busy) {
    const panel = document.querySelector(".panel.active");
    if (panel && overlay.parentElement !== panel) panel.appendChild(overlay);
  }
  overlay.classList.toggle("on-watermark", !!(busy && onWatermark));
  overlay.hidden = !busy;
}

function showBrowseLoading() {
  const results = $("#browse-results");
  if (results) {
    results.className = "";
    results.innerHTML = "";
  }
  setRegroupBusy(true, { onWatermark: true });
}

/** True when Clusters view is showing a preview not yet written to photo_groups. */
let clustersPreviewDirty = false;
/** Last preview/persist items — used to update gallery keepers without waiting on DB. */
let lastClusterPreviewItems = null;
let regroupSeq = 0;

function applyClusterAssignmentsToBrowseItems(items) {
  if (!Array.isArray(items)) return;
  const byId = new Map();
  for (const g of items) {
    for (const m of g.members || []) {
      if (m?.id != null) byId.set(m.id, m);
    }
  }
  const scopeIds = browseScopedPhotoIds();
  for (const p of browseItems) {
    if (p?.id == null || !scopeIds.has(p.id)) continue;
    const m = byId.get(p.id);
    if (m) {
      p.group_id = m.group_id;
      p.is_keeper = !!m.is_keeper;
    } else {
      p.group_id = null;
      p.is_keeper = true;
    }
  }
}

async function persistClustersPreview() {
  debouncedRegroup.cancel?.();
  if (!clustersPreviewDirty) {
    setRegroupBusy(false);
    return;
  }
  await regroupClusters({ persist: true, render: false });
}

function applyRegroupedClustersToGallery() {
  const visible = new Set(browseDisplayItems().map(browseItemKey));
  browseSelected = new Set([...browseSelected].filter((k) => visible.has(k)));
  renderBrowseResults();
}

async function regroupClusters({ persist = false, render = true } = {}) {
  const seq = ++regroupSeq;
  const level = clusterLevelFromSlider();
  const { exact, formatted, near, threshold } = clusterLevelConfig(level);
  const photo_ids = [...browseScopedPhotoIds()];
  const list = $("#dupe-list");
  if (!photo_ids.length) {
    clustersPreviewDirty = false;
    lastClusterPreviewItems = [];
    setRegroupBusy(false);
    if (render && isDupesWorkspace() && list) {
      list.innerHTML = `<p class="muted">Select items in the tree to show clusters.</p>`;
    }
    dupePhotosById = new Map();
    dupeSelected.clear();
    syncDupeToolbar();
    if (render) renderBrowseTimeline([]);
    return;
  }
  // Overlay while the grouping UI is on screen; background persist stays silent.
  if (render && clusterRangeRegroupsView()) setRegroupBusy(true);
  else setRegroupBusy(false);
  if (render && isDupesWorkspace()) {
    dupeSelected.clear();
    syncDupeToolbar();
  }
  try {
    const result = await api("/api/duplicates/rebuild", {
      method: "POST",
      body: JSON.stringify({
        threshold: near ? threshold : undefined,
        exact,
        formatted,
        near,
        clustering: clusterMethodFromSelect(),
        photo_ids,
        include_culled: includeCulled(),
        persist,
      }),
    });
    if (seq !== regroupSeq) return;
    clustersPreviewDirty = !persist;
    lastClusterPreviewItems = result.items || [];
    if (persist) {
      applyClusterAssignmentsToBrowseItems(lastClusterPreviewItems);
      refreshStats();
    } else if (render && clusterRangeAffectsBrowse()) {
      applyClusterAssignmentsToBrowseItems(lastClusterPreviewItems);
    }
    if (render) {
      if (isDupesWorkspace()) renderDupeGroups(lastClusterPreviewItems);
      else if (clusterRangeAffectsBrowse()) applyRegroupedClustersToGallery();
    }
  } catch (err) {
    if (seq === regroupSeq && render) {
      if (isDupesWorkspace() && list) {
        list.innerHTML = `<p class="muted">Regroup failed.</p>`;
      } else if (clusterRangeAffectsBrowse()) {
        const root = $("#browse-results");
        if (root) root.innerHTML = `<p class="muted">Regroup failed.</p>`;
      }
    }
    console.error("regroup failed", err);
  } finally {
    if (seq === regroupSeq) setRegroupBusy(false);
  }
}

function debounce(fn, ms) {
  let t;
  const wrapped = (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
  wrapped.cancel = () => clearTimeout(t);
  return wrapped;
}

const debouncedRegroup = debounce(() => {
  if (isDupesWorkspace()) regroupClusters({ persist: false });
  else if (clusterRangeAffectsBrowse()) regroupClusters({ persist: true });
  else setRegroupBusy(false);
}, 120);

$("#cluster-level")?.addEventListener("input", () => {
  if (!clusterRangeRegroupsView()) return;
  setRegroupBusy(true);
  debouncedRegroup();
});
$("#cluster-method")?.addEventListener("change", () => {
  persistClusterMethodPref();
  if (!clusterRangeRegroupsView()) return;
  setRegroupBusy(true);
  debouncedRegroup();
});
loadClusterMethodPref();

refreshStats();
loadSources();
pollIndex();
pollFacesStatus();
setInterval(() => {
  pollIndex().catch(() => {});
}, 10000);

// ── SSE: live updates from server ───────────────────────────────────────────
/** Apply a fresh sources payload; full rebuild only if the archive set changed. */
async function applySourcesLive(sources) {
  const prev = lastSources || [];
  const prevIds = prev.map((s) => s.id).join(",");
  const nextIds = (sources || []).map((s) => s.id).join(",");
  const prevExists = Object.fromEntries(prev.map((s) => [s.id, !!s.exists]));
  const mountChanged = (sources || []).some((s) => {
    const was = prevExists[s.id];
    return was !== undefined && was !== !!s.exists;
  });
  lastSources = sources;
  if (
    prevIds !== nextIds ||
    mountChanged ||
    !$("#source-list")?.querySelector("tr[data-source-id]")
  ) {
    await loadSources();
    if (prevIds !== nextIds || mountChanged) {
      refreshExpandedBranches(["volumes"]).catch(() => {});
    }
    return;
  }
  applyIndexStatusToSources(lastIndexStatus, sources, lastFacesStatus);
}

(function connectSSE() {
  let refreshPending = false;
  let refreshAgain = false;
  const es = new EventSource("/api/events");
  es.onmessage = () => {
    if (refreshPending) {
      refreshAgain = true;
      return;
    }
    refreshPending = true;
    (async () => {
      try {
        do {
          refreshAgain = false;
          const [st, fst, sources] = await Promise.all([
            api("/api/index/status"),
            api("/api/faces/status").catch(() => lastFacesStatus),
            api("/api/sources"),
          ]);
          lastIndexStatus = st;
          if (fst) lastFacesStatus = fst;
          const bar = $("#index-status");
          if (bar) bar.textContent = indexStatusText(st, sources, lastFacesStatus);
          await applySourcesLive(sources);
          if (!st.running) refreshStats();
        } while (refreshAgain);
      } catch {
        /* ignore transient errors; next event retries */
      } finally {
        refreshPending = false;
      }
    })();
  };
  es.onerror = () => {
    es.close();
    setTimeout(connectSSE, 3000);
  };
})();

// ── Browse tab ──────────────────────────────────────────────────────────────

let browseView = "volumes";
let browseActiveKey = null;
let browseActiveKeys = [];
/** @type {Record<string, string[]>} groupBy → selected keys (OR within, AND across) */
let browseSelectionByGroup = {};
/** Bumps on each gallery load so stale async fetches cannot overwrite newer results. */
let browseLoadGen = 0;
let browseGalleryView = "thumbs";
/** Gallery tile min width in px (Cards/Thumbnails unified via size slider). */
let browseTileSize = 156;
let browseItems = [];
/** Last folder/group load; describe search filters this without leaving browse. */
let browseItemsFull = [];
let browseSearchActive = false;
let browseSelected = new Set();
let browseAnchorKey = null;
let browseTreeAnchor = null;
let browseSort = { key: "taken_at", dir: "desc" };

const BROWSE_VIEW_KEY = "fotoman.browseView";
const BROWSE_GALLERY_KEY = "fotoman.browseGalleryView";
const BROWSE_TILE_SIZE_KEY = "fotoman.browseTileSize";
const BROWSE_SELECTION_KEY = "fotoman.browseSelection";
const ASPECT_FILTER_KEY = "fotoman.aspectFilter";
const BROWSE_TILE_SIZE_MIN = 64;
const BROWSE_TILE_SIZE_MAX = 376;
const BROWSE_TILE_COMPACT_MAX = 120;
const VALID_BROWSE_VIEWS = new Set([
  "volumes",
  "collections",
  "dates",
  "cameras",
  "places",
  "people",
]);
const BROWSE_VIEW_ALIASES = {
  date: "dates",
  camera: "cameras",
  location: "places",
};
const VALID_GALLERY_VIEWS = new Set(["thumbs", "details"]);
const MONTH_LABELS = [
  "",
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];

function clampBrowseTileSize(n) {
  const v = Math.round(Number(n) || 156);
  const stepped = Math.round(v / 4) * 4;
  return Math.max(BROWSE_TILE_SIZE_MIN, Math.min(BROWSE_TILE_SIZE_MAX, stepped));
}

function saveBrowseSelectionPref() {
  try {
    localStorage.setItem(BROWSE_SELECTION_KEY, JSON.stringify(cloneBrowseSelection()));
  } catch (_) {
    /* ignore */
  }
}

function loadBrowseSelectionPref() {
  try {
    const raw = localStorage.getItem(BROWSE_SELECTION_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      const cleaned = {};
      for (const [gb, keys] of Object.entries(parsed)) {
        if (!Array.isArray(keys)) continue;
        cleaned[gb] = keys.map(String);
      }
      browseSelectionByGroup = cleaned;
      syncBrowseActiveFromSelection();
    }
  } catch (_) {
    /* ignore */
  }
}

function saveAspectFilterPref() {
  try {
    localStorage.setItem(ASPECT_FILTER_KEY, currentAspectFilter());
  } catch (_) {
    /* ignore */
  }
}

function loadAspectFilterPref() {
  try {
    const saved = localStorage.getItem(ASPECT_FILTER_KEY);
    const el = $("#aspect-filter");
    if (!el || saved == null) return;
    const ok = [...el.options].some((o) => o.value === saved);
    if (ok) el.value = saved;
  } catch (_) {
    /* ignore */
  }
}

function loadBrowsePrefs() {
  let migratedRemovedBranch = false;
  try {
    const view = localStorage.getItem(BROWSE_VIEW_KEY);
    // Old Removed tree branch → global Removed visibility.
    if (view === "remove") {
      browseView = "volumes";
      migratedRemovedBranch = true;
    } else if (view === "aspect") {
      browseView = "volumes";
    } else if (view && BROWSE_VIEW_ALIASES[view]) {
      browseView = BROWSE_VIEW_ALIASES[view];
    } else if (view && VALID_BROWSE_VIEWS.has(view)) {
      browseView = view;
    }
    const gallery = localStorage.getItem(BROWSE_GALLERY_KEY);
    // Old "cards" (thumbnails) view → gallery + small tile size.
    if (gallery === "cards") {
      browseGalleryView = "thumbs";
      browseTileSize = 80;
    } else if (gallery && VALID_GALLERY_VIEWS.has(gallery)) {
      browseGalleryView = gallery;
    }
    const size = localStorage.getItem(BROWSE_TILE_SIZE_KEY);
    if (size != null && size !== "") browseTileSize = clampBrowseTileSize(size);
  } catch (_) {
    /* ignore */
  }
  loadPhotoVisibilityPref();
  if (migratedRemovedBranch) {
    photoVisibility = "removed";
    savePhotoVisibilityPref();
    saveBrowseViewPref();
  }
  loadBrowseSelectionPref();
  loadAspectFilterPref();
  loadPressPrefs();
  syncBrowseTileSizeControl();
  syncPhotoVisibilityButtons();
  syncBrowseNestedOptLabel();
  syncAspectFilterStyle();
}

function saveBrowseTileSizePref() {
  try {
    localStorage.setItem(BROWSE_TILE_SIZE_KEY, String(browseTileSize));
  } catch (_) {
    /* ignore */
  }
}

function syncBrowseTileSizeControl() {
  const slider = $("#browse-tile-size");
  if (slider) slider.value = String(browseTileSize);
}

function applyBrowseTileSize() {
  const size = `${browseTileSize}px`;
  const compact = browseTileSize <= BROWSE_TILE_COMPACT_MAX;
  for (const root of [$("#browse-results"), $("#dupe-list")]) {
    if (!root) continue;
    root.style.setProperty("--browse-tile", size);
    root.classList.toggle("is-compact", compact);
  }
}

function setBrowseTileSize(n, { persist = true } = {}) {
  browseTileSize = clampBrowseTileSize(n);
  syncBrowseTileSizeControl();
  applyBrowseTileSize();
  if (persist) saveBrowseTileSizePref();
}

function showMonthDividers() {
  // Search rank order wins; month bands only when browsing normally.
  return false;
}

function photoMonthKey(p) {
  return p?.taken_at ? String(p.taken_at).slice(0, 7) : "";
}

function monthDividerLabel(key) {
  if (!key) return "Undated";
  const [year, month] = key.split("-");
  const name = MONTH_LABELS[Number(month)] || month;
  return year ? `${name} ${year}` : name;
}

function monthDividerHtml(key) {
  return `<div class="month-divider" role="separator">${monthDividerLabel(key)}</div>`;
}

function monthDividerRowHtml(key, cols) {
  return `<tr class="month-divider-row"><td colspan="${cols}">${monthDividerLabel(key)}</td></tr>`;
}

function saveBrowseViewPref() {
  try {
    localStorage.setItem(BROWSE_VIEW_KEY, browseView);
  } catch (_) {
    /* ignore */
  }
}

function saveBrowseGalleryPref() {
  try {
    localStorage.setItem(BROWSE_GALLERY_KEY, browseGalleryView);
  } catch (_) {
    /* ignore */
  }
}

function syncBrowseViewButtons() {
  const activePanel = document.querySelector(".panel.active");
  if (activePanel?.id === "tab-dupes") {
    syncWorkspaceViewButtons("compare");
  } else if (activePanel?.id === "tab-browse") {
    syncWorkspaceViewButtons(
      browseGalleryView === "details" ? "details" : "stack"
    );
  }
}

loadBrowsePrefs();
syncBrowseViewButtons();

const BROWSE_DETAIL_COLS = [
  { key: "name", label: "Name" },
  { key: "archive", label: "Volume" },
  { key: "path", label: "Path" },
  { key: "taken_at", label: "Date" },
  { key: "camera", label: "Camera" },
  { key: "place", label: "Place" },
  { key: "size", label: "Size" },
  { key: "dims", label: "Dimensions" },
  { key: "aspect", label: "Aspect" },
];

function browseMediaUrls(p) {
  if (p.id != null) {
    return { thumb: `/api/thumb/${p.id}`, original: `/api/original/${p.id}` };
  }
  const q = `source_id=${p.source_id}&rel=${encodeURIComponent(p.rel_path || "")}`;
  return { thumb: `/api/browse/thumb?${q}`, original: `/api/browse/original?${q}` };
}

function browseItemKey(p) {
  if (p.id != null) return `id:${p.id}`;
  return `fs:${p.source_id}:${p.rel_path || p.path || ""}`;
}

function formatBytes(n) {
  if (n == null || n === "") return "—";
  const v = Number(n);
  if (!Number.isFinite(v) || v < 0) return "—";
  if (v < 1024) return `${v} B`;
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`;
  return `${(v / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDims(p) {
  if (p.width && p.height) return `${p.width}×${p.height}`;
  return "—";
}

function formatAspect(p) {
  const labels = {
    landscape: "Landscape",
    portrait: "Portrait",
    square: "Square",
    panorama: "Panorama",
  };
  return labels[photoAspectClass(p)] || "—";
}

function photoPath(p) {
  return p.path || p.rel_path || "";
}

function photoArchiveName(p) {
  if (!p) return "";
  if (p.source_label) return p.source_label;
  return archiveDisplayName(p.source_id) || "";
}

function browseItemMetaLine(p) {
  const bits = [
    photoArchiveName(p),
    p.taken_at ? p.taken_at.slice(0, 10) : "undated",
    p.place_label || "",
    p.camera || "",
  ].filter(Boolean);
  return bits.join(" · ");
}

function browseItemDetailBits(p) {
  return [
    photoArchiveName(p) || null,
    photoPath(p) || null,
    p.taken_at ? p.taken_at.slice(0, 10) : "undated",
    p.place_label || null,
    p.camera || null,
    formatDims(p) !== "—" ? formatDims(p) : null,
    formatBytes(p.size) !== "—" ? formatBytes(p.size) : null,
  ].filter(Boolean);
}

function browseItemHoverDetail(p) {
  const name = basename(p.path || p.rel_path);
  const path = photoPath(p);
  const rest = browseItemDetailBits(p).filter((b) => b !== path);
  return `<div class="card-hover-detail">
    <strong>${name}</strong>
    ${path ? `<span class="card-hover-path">${path}</span>` : ""}
    <span>${rest.join(" · ")}</span>
  </div>`;
}

function isCardHoverDetailEvent(e) {
  return !!e.target.closest(".card-hover-detail, .browse-thumb-detail");
}

function hoverDetailHasSelection() {
  const sel = window.getSelection();
  return !!(sel && sel.toString());
}

function browseSortValue(p, key) {
  switch (key) {
    case "name":
      return (basename(p.path) || "").toLowerCase();
    case "archive":
      return photoArchiveName(p).toLowerCase();
    case "path":
      return (photoPath(p) || "").toLowerCase();
    case "taken_at":
      return p.taken_at || "";
    case "camera":
      return (p.camera || "").toLowerCase();
    case "place":
      return (p.place_label || "").toLowerCase();
    case "size":
      return Number(p.size) || 0;
    case "dims":
      return (Number(p.width) || 0) * (Number(p.height) || 0);
    case "aspect":
      return photoAspectClass(p);
    default:
      return "";
  }
}

function isClusterKeeper(p) {
  return p == null || p.is_keeper == null || !!p.is_keeper;
}

/** Stack: one card per cluster (preferred/keeper only). */
function browseKeepersOnlyView() {
  return browseGalleryView === "thumbs" && !isDupesWorkspace();
}

function clusterRangeAffectsBrowse() {
  return browseKeepersOnlyView() || browseGalleryView === "details";
}

/** Count live cluster members by group_id for badge display. */
function clusterCountsByGroupId() {
  const counts = new Map();
  const pool = browseItemsFull.length ? browseItemsFull : browseItems;
  for (const p of filterByPhotoVisibility(pool, { respectSelection: false })) {
    if (p?.group_id == null) continue;
    counts.set(p.group_id, (counts.get(p.group_id) || 0) + 1);
  }
  return counts;
}

/** Cluster partners for lightbox up/down — whole library, not just this tree selection. */
async function clusterLightboxEntries(photo) {
  if (photo?.group_id == null) return [];
  if (isDupesWorkspace()) return [];
  let members = [];
  try {
    members =
      (await api(
        `/api/browse/photos?group_by=cluster&key=${encodeURIComponent(photo.group_id)}&limit=200${removeQuery()}`
      )) || [];
  } catch (_) {
    const pool = browseItemsFull.length ? browseItemsFull : browseItems;
    members = filterByPhotoVisibility(
      pool.filter((p) => p?.group_id === photo.group_id),
      { respectSelection: false }
    );
  }
  members = members.filter((p) => p?.id != null);
  members.sort((a, b) => {
    const ak = isClusterKeeper(a) ? 0 : 1;
    const bk = isClusterKeeper(b) ? 0 : 1;
    if (ak !== bk) return ak - bk;
    return (Number(a.id) || 0) - (Number(b.id) || 0);
  });
  return members.map((p) => {
    const m = browseMediaUrls(p);
    return { src: m.original, orientation: orientationDeg(p), mirrored: isMirrored(p), photo: p };
  });
}

/**
 * Thumbs hide same-group replicas; acting on only the visible id leaves a sibling
 * that pops back into the gallery. Expand to matching members in the current load
 * (Expanded layout selects members explicitly — skip there).
 * @param {"live"|"culled"|"all"} mode live = Reject; culled = Delete from Rejected; all = edits
 */
function expandReplicaIdsInGallery(ids, mode = "live") {
  if (!browseKeepersOnlyView() || isDupesWorkspace() || !ids?.length) return ids;
  const idSet = new Set(ids);
  const groups = new Set();
  const pool = browseItemsFull.length ? browseItemsFull : browseItems;
  for (const p of pool) {
    // Expand from visible cluster cards (keeper, or replica stand-in).
    if (p?.id != null && idSet.has(p.id) && p.group_id != null) {
      groups.add(p.group_id);
    }
  }
  if (!groups.size) return ids;
  for (const p of pool) {
    if (p?.id == null || p.group_id == null) continue;
    if (!groups.has(p.group_id)) continue;
    if (mode === "live" && p.culled) continue;
    if (mode === "culled" && !p.culled) continue;
    idSet.add(p.id);
  }
  return [...idSet];
}

function expandRemoveIdsForHiddenReplicas(ids) {
  return expandReplicaIdsInGallery(ids, "live");
}

function expandDeleteIdsForHiddenReplicas(ids) {
  return expandReplicaIdsInGallery(ids, "culled");
}

function expandEditIdsForHiddenReplicas(ids) {
  return expandReplicaIdsInGallery(ids, "all");
}

/** Visible Clustered cards plus hidden same-group members (for date/place edits). */
function selectedBrowsePhotosForEdit() {
  const selected = selectedBrowsePhotos().filter((p) => p.id != null);
  if (!browseKeepersOnlyView() || isDupesWorkspace()) return selected;
  const idSet = new Set(expandEditIdsForHiddenReplicas(selected.map((p) => p.id)));
  const pool = browseItemsFull.length ? browseItemsFull : browseItems;
  return pool.filter((p) => p?.id != null && idSet.has(p.id));
}

function selectedPhotosForProperties() {
  if (isDupesWorkspace()) return selectedDupePhotos().filter((p) => p.id != null);
  return selectedBrowsePhotosForEdit();
}

function dropLocalPhotosById(ids) {
  const gone = new Set(ids);
  if (!gone.size) return;
  browseItems = browseItems.filter((p) => !gone.has(p.id));
  browseItemsFull = browseItemsFull.filter((p) => !gone.has(p.id));
  for (const id of gone) dupePhotosById?.delete?.(id);
}

function multiPersonSelection() {
  const keys = browseSelectionByGroup.person;
  if (!Array.isArray(keys)) return false;
  return keys.filter((k) => k && k !== "all").length > 1;
}

function sortedBrowseItems() {
  const { key, dir } = browseSort;
  const mult = dir === "asc" ? 1 : -1;
  const byPeople = multiPersonSelection();
  return [...browseItems].sort((a, b) => {
    if (byPeople) {
      const ac = Number(a._personMatchCount) || 1;
      const bc = Number(b._personMatchCount) || 1;
      if (ac !== bc) return bc - ac;
    }
    const av = browseSortValue(a, key);
    const bv = browseSortValue(b, key);
    if (av < bv) return -1 * mult;
    if (av > bv) return 1 * mult;
    return (basename(a.path) || "").localeCompare(basename(b.path) || "");
  });
}

function browseDisplayItems() {
  // Keep visual/hybrid rank order while a describe search is active.
  const raw = browseSearchActive ? [...browseItems] : sortedBrowseItems();
  const items = filterByAspect(
    filterByPhotoVisibility(raw, { respectSelection: true })
  );
  if (!browseKeepersOnlyView()) return items;
  // Thumbs hide cluster replicas, but if a place/date filter only hits replicas
  // (keeper is elsewhere), still show one photo per group so the node isn't empty.
  const groupHasKeeper = new Set();
  for (const p of items) {
    if (isClusterKeeper(p) && p.group_id != null) groupHasKeeper.add(p.group_id);
  }
  const seenReplicaGroup = new Set();
  return items.filter((p) => {
    if (isClusterKeeper(p)) return true;
    if (p.group_id == null) return true;
    if (groupHasKeeper.has(p.group_id)) return false;
    if (seenReplicaGroup.has(p.group_id)) return false;
    seenReplicaGroup.add(p.group_id);
    return true;
  });
}

function browseVisibleKeys() {
  if (browseGalleryView === "details") return browseVisibleDetailKeys();
  return browseDisplayItems().map(browseItemKey);
}

function allBrowseVisibleSelected() {
  const keys = browseVisibleKeys();
  return keys.length > 0 && keys.every((k) => browseSelected.has(k));
}

function updateBrowseSelectionCount() {
  if (isDupesWorkspace()) {
    syncDupeToolbar();
    return;
  }
  const n = browseSelected.size;
  const countEl = $("#browse-gallery-count");
  if (countEl) {
    const total = browseDisplayItems().length;
    countEl.textContent = total ? `${n} / ${total}` : "";
  }
  const btn = $("#btn-browse-select-all");
  if (!btn) return;
  const hasItems = browseDisplayItems().length > 0;
  btn.hidden = !hasItems;
  if (!hasItems) {
    syncBrowseRemoveButtons();
    return;
  }
  btn.textContent = allBrowseVisibleSelected() ? "Deselect all" : "Select all";
  syncBrowseRemoveButtons();
}

function selectedBrowsePhotos() {
  return browseItems.filter((p) => browseSelected.has(browseItemKey(p)));
}

function selectedTreePersonIds() {
  const keys = browseSelectionByGroup.person;
  if (!Array.isArray(keys)) return [];
  return keys
    .filter((k) => k && k !== "all")
    .map((k) => Number(k))
    .filter((id) => Number.isFinite(id) && id > 0);
}

/** Single tree person + selected photos that include that person's face. */
function peopleSelectionEditContext() {
  if (isDupesWorkspace()) return null;
  const personIds = selectedTreePersonIds();
  if (personIds.length !== 1) return null;
  const personId = personIds[0];
  const photos = selectedBrowsePhotos().filter(
    (p) => p.id != null && p.face_id != null
  );
  if (!photos.length) return null;
  const treeInput = $("#browse-groups")?.querySelector(
    `.person-label[data-person="${personId}"]`
  );
  const labeled = treeInput?.dataset.labeled === "1";
  const displayName =
    (treeInput?.dataset.displayName || treeInput?.value || treeInput?.textContent || "").trim() ||
    `Person ${personId}`;
  return {
    personId,
    labeled,
    displayName,
    photos,
    faceIds: photos.map((p) => Number(p.face_id)).filter((id) => id > 0),
  };
}

function isPersonNameLabeled(name) {
  const label = (name || "").trim();
  if (!label) return false;
  return !/^unknown\s+\d+$/i.test(label);
}

function syncBrowseRemoveButtons() {
  const selected = selectedBrowsePhotos();
  const activeIds = selected.filter((p) => p.id != null && !p.culled).map((p) => p.id);
  const removedIds = selected.filter((p) => p.id != null && p.culled).map((p) => p.id);
  const indexedIds = selected.filter((p) => p.id != null).map((p) => p.id);
  const removeBtn = $("#btn-browse-remove");
  const deleteBtn = $("#btn-browse-delete");
  const editBtn = $("#btn-browse-edit");
  const exportBtn = $("#btn-browse-export");
  const similarBtn = $("#btn-browse-similar");
  const addFolderBtn = $("#btn-browse-add-folder");
  const removeFolderBtn = $("#btn-browse-remove-folder");
  const actions = $(".browse-selection-actions");
  if (actions) actions.hidden = selected.length === 0;
  if (removeBtn) {
    const restoreMode = removedIds.length > 0;
    removeBtn.disabled = restoreMode ? false : activeIds.length === 0;
    removeBtn.textContent = restoreMode ? "Restore selected" : "Reject selected";
    removeBtn.classList.toggle("danger", !restoreMode);
    removeBtn.dataset.mode = restoreMode ? "restore" : "remove";
  }
  if (deleteBtn) {
    deleteBtn.disabled = removedIds.length === 0 || activeIds.length > 0;
  }
  if (editBtn) {
    editBtn.disabled = indexedIds.length === 0;
    editBtn.textContent = "Edit selection";
  }
  if (exportBtn) {
    exportBtn.disabled = indexedIds.length === 0;
    exportBtn.textContent = "Export selected";
  }
  if (similarBtn) {
    similarBtn.disabled = !(
      selected.length === 1 && selected[0]?.content_id != null
    );
  }
  if (addFolderBtn) {
    addFolderBtn.disabled = indexedIds.length === 0;
  }
  if (removeFolderBtn) {
    const inCollection = activeCollectionId() != null && !activeCollectionFlags().linked;
    removeFolderBtn.hidden = !inCollection;
    removeFolderBtn.disabled = !inCollection || indexedIds.length === 0;
  }
}

function toggleBrowseSelectAll() {
  if (isDupesWorkspace()) {
    const root = $("#dupe-list");
    if (!root) return;
    if (dupeSelected.size > 0) unselectDupeAll(root);
    else if (photoVisibility === "removed") selectDupeRemoved(root);
    else {
      selectDupeNonPrimary(root);
      const unmatched = root.querySelector('.dupe-group[data-group="unmatched"]');
      if (unmatched) selectDupeAll(unmatched);
    }
    syncDupeToolbar();
    return;
  }
  const keys = browseVisibleKeys();
  if (!keys.length) return;
  if (allBrowseVisibleSelected()) {
    browseSelected.clear();
    browseAnchorKey = null;
  } else {
    keys.forEach((k) => browseSelected.add(k));
    browseAnchorKey = keys[keys.length - 1];
  }
  renderBrowseResults();
}

const FACE_COLOR_CACHE = new Map();

let lightboxFaces = [];
let lightboxFallbackFace = null;
let lightboxFaceLoadGen = 0;
/** Unlabeled person ids (+ resulting named ids) to recluster on lightbox close. */
const lightboxReclusterPersonIds = new Set();

/** Stable saturated color from a person/face id (not a short cycling palette). */
function faceColor(key) {
  const n = Number(key);
  const id = Number.isFinite(n) ? Math.abs(Math.trunc(n)) : 0;
  const cached = FACE_COLOR_CACHE.get(id);
  if (cached) return cached;
  // Multiplicative hash → hue; keep sat/light in a readable range on dark UI.
  let h = (id * 2654435761) >>> 0;
  const hue = h % 360;
  h = (h / 360) | 0;
  const sat = 58 + (h % 22); // 58–79%
  h = (h / 22) | 0;
  const light = 48 + (h % 14); // 48–61%
  const color = `hsl(${hue} ${sat}% ${light}%)`;
  FACE_COLOR_CACHE.set(id, color);
  return color;
}

/** Unlabeled cluster or ``Unknown N`` — same gray treatment. */
function faceBoxIsUnlabeled(f) {
  if (f == null) return true;
  if (f.labeled === true) return false;
  if (f.labeled === false) return true;
  if (f.person_id == null || f.person_id === "") return true;
  const name = String(f.display_name || f.person_name || "").trim();
  if (!name || /^person\s+\d+$/i.test(name)) return true;
  return /^unknown\s+\d+$/i.test(name);
}

function faceBoxStyle(f) {
  if (faceBoxIsUnlabeled(f)) {
    return "border-color:#9aa3a0;border-style:dashed;opacity:0.85";
  }
  return `border-color:${faceColor(f.person_id)}`;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function clearLightboxFaces() {
  const layer = $("#lightbox-faces");
  if (!layer) return;
  layer.innerHTML = "";
  layer.hidden = true;
  layer.style.left = "";
  layer.style.top = "";
  layer.style.width = "";
  layer.style.height = "";
  applyLightboxZoomPan();
}

function clearLightboxInfo() {
  lightboxFaceLoadGen += 1;
  cancelPlaceSuggestion();
  const info = $("#lightbox-info");
  const meta = $("#lightbox-meta");
  const list = $("#lightbox-face-list");
  const framesOpt = $("#lightbox-show-frames");
  const edit = $("#lightbox-edit");
  const err = $("#lightbox-edit-error");
  const vqa = $("#lightbox-vqa");
  const removedBadge = $("#lightbox-removed-badge");
  if (meta) meta.innerHTML = "";
  if (list) {
    list.innerHTML = "";
    list.hidden = true;
  }
  if (framesOpt) framesOpt.hidden = true;
  if (edit) edit.hidden = true;
  if (err) {
    err.hidden = true;
    err.textContent = "";
  }
  if (removedBadge) removedBadge.hidden = true;
  resetLightboxVqa();
  if (vqa) vqa.hidden = true;
  if (info) info.hidden = true;
  lightboxFaces = [];
  lightboxFallbackFace = null;
}

function resetLightboxVqa() {
  const err = $("#lightbox-vqa-error");
  const answer = $("#lightbox-vqa-answer");
  if (err) {
    err.hidden = true;
    err.textContent = "";
  }
  if (answer) {
    answer.hidden = true;
    answer.textContent = "";
  }
}

function setLightboxVqaTask(task) {
  const next = ["where", "describe", "freeform"].includes(task)
    ? task
    : "freeform";
  lightboxVqaTask = next;
  document.querySelectorAll(".lightbox-vqa-preset").forEach((btn) => {
    const on = btn.dataset.vqaTask === next;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  });
  const freeWrap = $("#lightbox-vqa-freeform-wrap");
  if (freeWrap) freeWrap.hidden = next !== "freeform";
}

function cancelPlaceSuggestion() {
  const input = $("#lightbox-place");
  if (!placeSuggestPending || !input) {
    placeSuggestPending = null;
    input?.classList.remove("place-suggest");
    return;
  }
  input.value = placeSuggestPending.previous;
  placeSuggestPending = null;
  input.classList.remove("place-suggest");
}

function beginPlaceSuggestion(label) {
  const input = $("#lightbox-place");
  if (!input || !label) return;
  if (!placeSuggestPending) {
    placeSuggestPending = { previous: input.value };
  }
  input.value = label;
  input.classList.add("place-suggest");
  input.focus();
  input.select();
}

async function acceptPlaceSuggestion() {
  const input = $("#lightbox-place");
  if (!placeSuggestPending || !input) return;
  placeSuggestPending = null;
  input.classList.remove("place-suggest");
  await saveLightboxEdits({ showErrors: true });
}

function syncLightboxVqa(photo) {
  const vqa = $("#lightbox-vqa");
  if (!vqa) return;
  const hasPhoto = photo?.id != null;
  vqa.hidden = !hasPhoto;
  if (!hasPhoto) return;

  const disabled = $("#lightbox-vqa-disabled");
  const controls = $("#lightbox-vqa-controls");
  const ready = vqaConfigured;
  if (disabled) disabled.hidden = ready;
  if (controls) {
    controls.hidden = !ready;
    controls.querySelectorAll("button, textarea").forEach((el) => {
      el.disabled = !ready;
    });
  }
  if (!ready) return;
  setLightboxVqaTask(lightboxVqaTask || "freeform");
  resetLightboxVqa();
}

async function runLightboxVqa(taskOverride = null) {
  const photo = lightboxCurrentPhoto();
  const id = photo?.id;
  const task = taskOverride || lightboxVqaTask || "freeform";
  const prompt = ($("#lightbox-vqa-prompt")?.value || "").trim();
  const err = $("#lightbox-vqa-error");
  const answer = $("#lightbox-vqa-answer");
  if (err) {
    err.hidden = true;
    err.textContent = "";
  }
  if (answer) {
    answer.hidden = true;
    answer.textContent = "";
  }
  if (!vqaConfigured) {
    if (err) {
      err.textContent = "Configure a VLM in Preferences first";
      err.hidden = false;
    }
    return;
  }
  if (id == null) return;
  if (task === "freeform" && !prompt) {
    if (err) {
      err.textContent = "Enter a question first";
      err.hidden = false;
    }
    return;
  }
  document.querySelectorAll(".lightbox-vqa-preset").forEach((btn) => {
    btn.disabled = true;
  });
  try {
    const body = {
      photo_id: id,
      task,
      // Always include prompt so older validators accepting only prompt still work.
      prompt: task === "freeform" ? prompt : task,
    };
    const res = await api("/api/vqa", {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (task === "where") {
      const place = (res.place_label || "").trim();
      if (place) {
        if (answer) {
          answer.textContent = place;
          answer.hidden = false;
        }
        beginPlaceSuggestion(place);
      } else {
        if (answer) {
          answer.textContent = res.answer || "Could not determine a place";
          answer.hidden = false;
        }
        if (err) {
          err.textContent = "No structured place found — edit Place manually or try again";
          err.hidden = false;
        }
      }
    } else if (answer) {
      answer.textContent = res.answer || "(no answer)";
      answer.hidden = false;
    }
  } catch (ex) {
    if (err) {
      err.textContent = ex.message || "VQA failed";
      err.hidden = false;
    }
  } finally {
    document.querySelectorAll(".lightbox-vqa-preset").forEach((btn) => {
      btn.disabled = !vqaConfigured;
    });
  }
}

function applyLightboxPersonUpdate(personId, faceId, updated) {
  for (const f of lightboxFaces) {
    if (f.id !== faceId) continue;
    f.person_id = updated.id;
    f.person_name = updated.name;
    f.labeled = !!updated.name;
    f.display_name = updated.display_name;
  }
  renderLightboxFaceList(lightboxFaces);
  drawLightboxFaceBoxes();
}

function noteLightboxReclusterIds(wasLabeled, oldPersonId, updated, named) {
  if (!named) return;
  const nextId = Number(updated?.id);
  if (!wasLabeled) {
    const oldId = Number(oldPersonId);
    if (Number.isFinite(oldId) && oldId > 0) lightboxReclusterPersonIds.add(oldId);
    if (Number.isFinite(nextId) && nextId > 0) lightboxReclusterPersonIds.add(nextId);
    return;
  }
  if (lightboxReclusterPersonIds.size && Number.isFinite(nextId) && nextId > 0) {
    lightboxReclusterPersonIds.add(nextId);
  }
}

async function flushLightboxRecluster() {
  const ids = [...lightboxReclusterPersonIds];
  lightboxReclusterPersonIds.clear();
  if (!ids.length) return;
  try {
    await api("/api/people/recluster", {
      method: "POST",
      body: JSON.stringify({ person_ids: ids }),
    });
    await refreshPeopleBranchIfLoaded();
  } catch (err) {
    console.error("recluster failed", err);
  }
}

async function patchLightboxFaceName(faceId, name, merge = false) {
  const res = await fetch(`/api/faces/${faceId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name || null, merge }),
  });
  if (res.status === 409) {
    const body = await res.json();
    const err = new Error("name_conflict");
    err.conflict = true;
    err.existing = body.detail?.existing;
    throw err;
  }
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  return res.json();
}

function bindLightboxFaceLabel(input) {
  const save = async () => {
    const next = input.value.trim();
    const prev = input.defaultValue;
    if (next === prev) return;
    const personId = input.dataset.person;
    const faceId = Number(input.dataset.face);
    if (!faceId) return;
    const wasLabeled = input.dataset.labeled === "1";

    try {
      const updated = await patchLightboxFaceName(faceId, next || null, false);
      noteLightboxReclusterIds(wasLabeled, personId, updated, !!next);
      applyLightboxPersonUpdate(personId, faceId, updated);
    } catch (err) {
      if (err.conflict && err.existing) {
        const ex = err.existing;
        const label = ex.display_name || next;
        const ok = confirm(
          `"${label}" already exists (${ex.photo_count} photos). Merge this face into them?`
        );
        if (!ok) {
          input.value = prev;
          return;
        }
        try {
          const updated = await patchLightboxFaceName(faceId, next || null, true);
          noteLightboxReclusterIds(wasLabeled, personId, updated, !!next);
          applyLightboxPersonUpdate(personId, faceId, updated);
        } catch (mergeErr) {
          input.value = prev;
          alert(mergeErr.message);
        }
        return;
      }
      input.value = prev;
      alert(err.message);
    }
  };
  input.addEventListener("click", (e) => e.stopPropagation());
  input.addEventListener("blur", save);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      input.blur();
    } else if (e.key === "Escape") {
      input.value = input.defaultValue;
      input.blur();
    }
  });
}

function bindLightboxFaceClear(btn) {
  btn.addEventListener("click", async (e) => {
    e.stopPropagation();
    const personId = btn.dataset.person;
    const faceId = Number(btn.dataset.face);
    if (!faceId) return;
    btn.disabled = true;
    try {
      const updated = await patchLightboxFaceName(faceId, null, false);
      applyLightboxPersonUpdate(personId, faceId, updated);
    } catch (err) {
      btn.disabled = false;
      alert(err.message);
    }
  });
}

function bindLightboxFaceDismiss(btn) {
  btn.addEventListener("click", async (e) => {
    e.stopPropagation();
    const faceId = Number(btn.dataset.face);
    const personId = Number(btn.dataset.person);
    if (!faceId) return;
    if (!confirm("Remove this detection? Use for dogs, statues, cartoons, etc.")) return;
    btn.disabled = true;
    try {
      await api(`/api/faces/${faceId}`, { method: "DELETE" });
      lightboxFaces = lightboxFaces.filter((f) => f.id !== faceId);
      if (Number.isFinite(personId) && personId > 0) {
        lightboxReclusterPersonIds.add(personId);
      }
      renderLightboxFaceList(lightboxFaces);
      drawLightboxFaceBoxes();
      syncLightboxFramesOpt();
      refreshPeopleBranchIfLoaded().catch(() => {});
      // Drop dismissed face from gallery overlays for this photo.
      const photo = lightboxCurrentPhoto();
      if (photo?.faces) {
        photo.faces = photo.faces.filter((f) => f.id !== faceId);
      }
      const local = browseItems.find((p) => p.id === photo?.id);
      if (local?.faces) {
        local.faces = local.faces.filter((f) => f.id !== faceId);
        if (typeof renderBrowseResults === "function") renderBrowseResults();
      }
    } catch (err) {
      btn.disabled = false;
      alert(err.message);
    }
  });
}

function bindLightboxFaceUnknown(btn) {
  btn.addEventListener("click", async (e) => {
    e.stopPropagation();
    const personId = btn.dataset.person;
    const faceId = Number(btn.dataset.face);
    if (!faceId) return;
    const wasLabeled = btn.dataset.labeled === "1";
    btn.disabled = true;
    try {
      const updated = await api(`/api/faces/${faceId}/unknown`, { method: "POST" });
      noteLightboxReclusterIds(wasLabeled, personId, updated, true);
      applyLightboxPersonUpdate(personId, faceId, updated);
    } catch (err) {
      btn.disabled = false;
      alert(err.message);
    }
  });
}

function renderLightboxFaceList(faces) {
  const list = $("#lightbox-face-list");
  if (!list) return;
  if (!(faces || []).length) {
    list.innerHTML = "";
    list.hidden = true;
    return;
  }
  list.innerHTML = faces
    .map((f) => {
      const unlabeled = faceBoxIsUnlabeled(f);
      const color = unlabeled ? "#9aa3a0" : faceColor(f.person_id);
      const value = f.labeled ? f.display_name : "";
      const placeholder = f.display_name || "Name…";
      const clear = f.labeled
        ? `<button type="button" class="lightbox-face-clear" data-person="${f.person_id ?? ""}" data-face="${f.id ?? ""}" aria-label="Remove label" title="Remove label">✕</button>`
        : "";
      const unknown = !f.labeled
        ? `<button type="button" class="lightbox-face-unknown" data-person="${f.person_id ?? ""}" data-face="${f.id ?? ""}" data-labeled="0" aria-label="Assign next Unknown label" title="Unknown N">?</button>`
        : "";
      const dismiss = f.id
        ? `<button type="button" class="lightbox-face-dismiss" data-person="${f.person_id ?? ""}" data-face="${f.id}" aria-label="Not a human" title="Not a human (dog, statue, …)">⊘</button>`
        : "";
      return `<li${unlabeled ? ' class="is-unlabeled"' : ""}>
        <span class="face-dot" style="background:${color}"></span>
        <input class="lightbox-face-label" type="text"
          value="${escapeHtml(value)}"
          placeholder="${escapeHtml(placeholder)}"
          data-person="${f.person_id ?? ""}"
          data-face="${f.id ?? ""}"
          data-labeled="${f.labeled ? "1" : "0"}"
          aria-label="Face name" />
        ${unknown}
        ${clear}
        ${dismiss}
      </li>`;
    })
    .join("");
  list.hidden = false;
  list.querySelectorAll(".lightbox-face-label").forEach(bindLightboxFaceLabel);
  list.querySelectorAll(".lightbox-face-unknown").forEach(bindLightboxFaceUnknown);
  list.querySelectorAll(".lightbox-face-clear").forEach(bindLightboxFaceClear);
  list.querySelectorAll(".lightbox-face-dismiss").forEach(bindLightboxFaceDismiss);
}

function lightboxImageReady(img = $("#lightbox-img")) {
  return !!(img && img.complete && img.naturalWidth);
}

function syncLightboxFaceLayer(layer, img) {
  if (!layer || !img) return;
  const w = img.clientWidth;
  const h = img.clientHeight;
  if (!w || !h) return;
  layer.style.left = `${img.offsetLeft}px`;
  layer.style.top = `${img.offsetTop}px`;
  layer.style.width = `${w}px`;
  layer.style.height = `${h}px`;
}

function drawLightboxFaceBoxes() {
  const layer = $("#lightbox-faces");
  const img = $("#lightbox-img");
  if (!layer) return;
  const showFrames = isPressOn($("#lightbox-show-frames"));
  if (!showFrames) {
    clearLightboxFaces();
    return;
  }
  // Wait until the image has real layout so % boxes align to pixels.
  if (!lightboxImageReady(img)) {
    clearLightboxFaces();
    return;
  }
  const faces = lightboxFaces.length
    ? lightboxFaces
    : lightboxFallbackFace && lightboxFallbackFace.bbox_w != null
      ? [lightboxFallbackFace]
      : [];
  if (!faces.length) {
    clearLightboxFaces();
    return;
  }
  syncLightboxFaceLayer(layer, img);
  layer.hidden = false;
  layer.innerHTML = faces
    .map((f) => {
      if (f.bbox_w == null) return "";
      const unlabeled = faceBoxIsUnlabeled(f);
      const label = (f.display_name || "").trim();
      const labelHtml = label
        ? `<span class="face-box-label">${escapeHtml(label)}</span>`
        : "";
      return `<span class="face-box${unlabeled ? " is-unlabeled" : ""}" style="left:${f.bbox_x * 100}%;top:${f.bbox_y * 100}%;width:${f.bbox_w * 100}%;height:${f.bbox_h * 100}%;${faceBoxStyle(f)}">${labelHtml}</span>`;
    })
    .join("");
  applyLightboxZoomPan();
}

function syncLightboxFramesOpt() {
  const opt = $("#lightbox-show-frames");
  if (!opt) return;
  const has =
    lightboxFaces.length > 0 ||
    (lightboxFallbackFace && lightboxFallbackFace.bbox_w != null);
  opt.hidden = !has;
}

async function loadLightboxFaces(photo) {
  const gen = ++lightboxFaceLoadGen;
  lightboxFaces = [];
  const id = photo?.id;
  if (id == null) {
    syncLightboxFramesOpt();
    renderLightboxFaceList([]);
    drawLightboxFaceBoxes();
    return;
  }
  let faces = [];
  try {
    faces = await api(`/api/photos/${id}/faces`);
  } catch {
    faces = [];
  }
  if (gen !== lightboxFaceLoadGen) return;
  lightboxFaces = faces;
  if (faces.length) {
    const info = $("#lightbox-info");
    if (info) info.hidden = false;
  }
  syncLightboxFramesOpt();
  renderLightboxFaceList(faces);
  drawLightboxFaceBoxes();
}

function lightboxMetaRows(p) {
  if (!p) return [];
  const rows = [];
  const archive = photoArchiveName(p);
  if (archive) rows.push(["Volume", archive]);
  const path = photoPath(p);
  if (path) rows.push(["Path", path]);
  if (p.camera) rows.push(["Camera", p.camera]);
  const dims = formatDims(p);
  const bytes = formatBytes(p.size);
  if (dims !== "—" || bytes !== "—") {
    const sizeVal =
      dims !== "—" && bytes !== "—" ? `${dims} (${bytes})` : dims !== "—" ? dims : bytes;
    rows.push(["Size", sizeVal]);
  }
  const hasCoords = p.lat != null && p.lon != null;
  const coords = hasCoords
    ? `${Number(p.lat).toFixed(5)}, ${Number(p.lon).toFixed(5)}`
    : "—";
  const src = coordSourceLabel(p.coord_source) || (p.gps ? "EXIF" : "");
  const text = hasCoords && src ? `${coords} (${src})` : coords;
  const refreshBtn = `<button type="button" class="lightbox-coords-refresh" title="Read from EXIF" aria-label="Read coordinates from EXIF"><svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path fill="currentColor" d="M17.65 6.35A7.95 7.95 0 0 0 12 4V1L7 6l5 5V7c2.76 0 5 2.24 5 5a5 5 0 0 1-5 5 5 5 0 0 1-4.9-4H5.08A7 7 0 0 0 12 19a7 7 0 0 0 7-7c0-1.93-.78-3.68-2.05-4.95z"/></svg></button>`;
  const mapBtn = hasCoords
    ? `<button type="button" class="lightbox-coords-map" title="Show on map" aria-label="Show on map"><svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path fill="currentColor" d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7zm0 9.5c-1.38 0-2.5-1.12-2.5-2.5S10.62 6.5 12 6.5s2.5 1.12 2.5 2.5S13.38 11.5 12 11.5z"/></svg></button>`
    : "";
  rows.push(["Coordinates", text, refreshBtn + mapBtn, lightboxCoordsDiffer(p)]);
  if (p.score != null) rows.push(["Score", Number(p.score).toFixed(3)]);
  if (p.distance_km != null) rows.push(["Distance", `${p.distance_km} km`]);
  return rows;
}

function renderLightboxMeta(p) {
  const info = $("#lightbox-info");
  const meta = $("#lightbox-meta");
  if (!meta || !info) return;
  const rows = lightboxMetaRows(p);
  if (!rows.length) {
    meta.innerHTML = "";
    return;
  }
  info.hidden = false;
  meta.innerHTML = rows
    .map(
      ([k, v, extra, differs]) =>
        `<dt>${escapeHtml(k)}</dt><dd><span${differs ? ' class="is-diff"' : ""}>${escapeHtml(v)}</span>${extra ? " " + extra : ""}</dd>`
    )
    .join("");
}

let lightboxGroup = [];
let lightboxIndex = -1;
/** Same-group stack for up/down nav (Default gallery clusters). */
let lightboxCluster = [];
let lightboxClusterIndex = -1;

function lightboxCurrentPhoto() {
  if (lightboxClusterIndex >= 0 && lightboxClusterIndex < lightboxCluster.length) {
    return lightboxCluster[lightboxClusterIndex]?.photo || null;
  }
  if (lightboxIndex >= 0 && lightboxIndex < lightboxGroup.length) {
    return lightboxGroup[lightboxIndex]?.photo || null;
  }
  return null;
}

function lightboxCurrentEntry() {
  if (lightboxClusterIndex >= 0 && lightboxClusterIndex < lightboxCluster.length) {
    return lightboxCluster[lightboxClusterIndex] || null;
  }
  if (lightboxIndex >= 0 && lightboxIndex < lightboxGroup.length) {
    return lightboxGroup[lightboxIndex] || null;
  }
  return null;
}

async function setLightboxClusterFromPhoto(photo) {
  lightboxCluster = await clusterLightboxEntries(photo);
  if (!lightboxCluster.length) {
    lightboxClusterIndex = -1;
    return;
  }
  const id = photo?.id;
  const idx = lightboxCluster.findIndex((e) => e.photo?.id === id);
  lightboxClusterIndex = idx >= 0 ? idx : 0;
}

function syncLightboxShowMapBtn(_photo) {}

function coordSourceLabel(src) {
  if (src === "exif") return "EXIF";
  if (src === "manual") return "manual";
  if (src === "geocode") return "place";
  return "";
}

function lightboxCoordsDiffer(p) {
  if (!p || p.lat == null || p.lon == null) return false;
  const meta = lightboxFileMeta;
  if (meta && !meta.error && meta.photo_id === p.id) return !meta.gps_matches_exif;
  return p.coord_source === "geocode";
}

function dateFileMetaDiffers(meta) {
  return !!(meta?.taken_at && !meta.date_matches_exif);
}

function gpsFileMetaDiffers(meta) {
  return !!(meta && meta.lat != null && meta.lon != null && !meta.gps_matches_exif);
}

function fileMetaCanWrite(meta) {
  return !!(meta?.can_write_exif && (dateFileMetaDiffers(meta) || gpsFileMetaDiffers(meta)));
}

let lightboxFileMeta = null;
let lightboxFileMetaGen = 0;

function renderLightboxFileMeta(meta) {
  lightboxFileMeta = meta || null;
  const takenEl = $("#lightbox-taken-at");
  if (takenEl) {
    takenEl.classList.toggle("is-diff", dateFileMetaDiffers(meta));
    takenEl.removeAttribute("title");
  }
  const btn = $("#lightbox-write-exif");
  if (btn) {
    const can = fileMetaCanWrite(meta);
    btn.hidden = !can;
    btn.disabled = !can;
  }
  const photo = lightboxCurrentPhoto();
  if (photo) renderLightboxMeta(photo);
}

async function refreshLightboxFileMeta(photo) {
  const gen = ++lightboxFileMetaGen;
  renderLightboxFileMeta(null);
  if (photo?.id == null) return;
  try {
    const res = await api("/api/photos/file-meta", {
      method: "POST",
      body: JSON.stringify({ photo_ids: [photo.id] }),
    });
    if (gen !== lightboxFileMetaGen) return;
    renderLightboxFileMeta(res.photos?.[0] || null);
  } catch {
    if (gen !== lightboxFileMetaGen) return;
    renderLightboxFileMeta(null);
  }
}

async function writeLightboxExif() {
  const photo = lightboxCurrentPhoto();
  const btn = $("#lightbox-write-exif");
  const err = $("#lightbox-edit-error");
  if (photo?.id == null) return;
  if (lightboxSaveInFlight) {
    const saved = await lightboxSaveInFlight;
    if (!saved) return;
  } else if (lightboxEditsDirty(photo)) {
    const saved = await saveLightboxEdits({ showErrors: true });
    if (!saved) return;
  }
  await refreshLightboxFileMeta(photo);
  const dateDiff = dateFileMetaDiffers(lightboxFileMeta);
  const gpsDiff = gpsFileMetaDiffers(lightboxFileMeta);
  if (!dateDiff && !gpsDiff) return;
  const msg =
    dateDiff && gpsDiff
      ? "Write date and coordinates to EXIF?"
      : dateDiff
        ? "Write date to EXIF?"
        : "Write coordinates to EXIF?";
  if (!confirm(msg)) return;
  if (btn) btn.disabled = true;
  if (err) {
    err.hidden = true;
    err.textContent = "";
  }
  try {
    const res = await api("/api/photos/write-exif", {
      method: "POST",
      body: JSON.stringify({
        photo_ids: [photo.id],
        taken_at: dateDiff,
        gps: gpsDiff,
      }),
    });
    if (res.errors?.length && !res.updated) {
      throw new Error(res.errors[0].error || "Write failed");
    }
    if (gpsDiff && photo.lat != null && photo.lon != null) {
      photoMapLoaded = false;
      photoMapBounds = null;
      patchLocalCoordsById(photo.id, Number(photo.lat), Number(photo.lon), "exif");
    } else {
      await refreshLightboxFileMeta(photo);
    }
  } catch (ex) {
    if (err) {
      err.textContent = ex.message || "Write failed";
      err.hidden = false;
    }
    if (btn) btn.disabled = false;
  }
}

async function reloadLightboxExifCoords() {
  const photo = lightboxCurrentPhoto();
  const err = $("#lightbox-edit-error");
  if (photo?.id == null) return;
  if (err) {
    err.hidden = true;
    err.textContent = "";
  }
  const btn = $(".lightbox-meta")?.querySelector(".lightbox-coords-refresh");
  if (btn) btn.disabled = true;
  try {
    const res = await api("/api/photos/reload-exif-gps", {
      method: "POST",
      body: JSON.stringify({ photo_ids: [photo.id] }),
    });
    if (res.errors?.length && !res.updated) {
      throw new Error(res.errors[0].error || "Could not read EXIF");
    }
    const next = res.photos?.[0];
    photoMapLoaded = false;
    photoMapBounds = null;
    patchLocalCoordsById(
      photo.id,
      next?.lat ?? null,
      next?.lon ?? null,
      next?.coord_source || null,
      next || { place_label: null }
    );
  } catch (ex) {
    if (err) {
      err.textContent = ex.message || "Could not read EXIF";
      err.hidden = false;
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

function setLightboxOrientationControl(deg) {
  const d = orientationDeg(deg);
  const input = $("#lightbox-orientation");
  if (input) input.value = String(d);
}

function syncLightboxEditControls(photo, orientation, mirrored) {
  const edit = $("#lightbox-edit");
  const placeEl = $("#lightbox-place");
  const takenEl = $("#lightbox-taken-at");
  const mirrorEl = $("#lightbox-mirror");
  const similarBtn = $("#lightbox-similar");
  const removeBtn = $("#lightbox-remove");
  const restoreBtn = $("#lightbox-restore");
  const deleteBtn = $("#lightbox-delete");
  const removedBadge = $("#lightbox-removed-badge");
  const err = $("#lightbox-edit-error");
  const canEdit = photo?.id != null;
  if (edit) edit.hidden = !canEdit;
  if (!canEdit) {
    if (removedBadge) removedBadge.hidden = true;
    renderLightboxFileMeta(null);
    return;
  }
  if (err) {
    err.hidden = true;
    err.textContent = "";
  }
  if (placeEl) placeEl.value = (photo?.place_label || "").trim();
  if (takenEl) takenEl.value = takenAtToInputValue(photo?.taken_at);
  const deg = orientationDeg(orientation ?? photo?.orientation);
  const mirror = mirrored ?? isMirrored(photo);
  setLightboxOrientationControl(deg);
  const rotateEl = $("#lightbox-rotate");
  if (rotateEl) rotateEl.setAttribute("aria-pressed", deg ? "true" : "false");
  if (mirrorEl) setPressOn(mirrorEl, !!mirror);
  setLightboxTransform(deg, mirror);
  const culled = !!photo?.culled;
  if (removedBadge) removedBadge.hidden = !culled;
  syncLightboxShowMapBtn(photo);
  if (similarBtn) {
    similarBtn.disabled = photo?.content_id == null;
  }
  if (removeBtn) {
    removeBtn.hidden = culled;
    removeBtn.disabled = false;
  }
  if (restoreBtn) {
    restoreBtn.hidden = !culled;
    restoreBtn.disabled = false;
  }
  if (deleteBtn) {
    deleteBtn.hidden = false;
    deleteBtn.disabled = !culled;
    deleteBtn.title = culled ? "Delete" : "Reject the photo first";
  }
  refreshLightboxFileMeta(photo);
}

function coordsAreProtected(photo) {
  const src = photo?.coord_source;
  return src === "exif" || src === "manual";
}

function applyPlaceFieldsToPhoto(photo, place, { overwriteCoords = false } = {}) {
  if (!photo) return;
  if (!place) {
    photo.place_label = null;
    photo.venue = null;
    photo.city = null;
    photo.region = null;
    photo.country = null;
    if (!coordsAreProtected(photo)) {
      photo.lat = null;
      photo.lon = null;
      photo.coord_source = null;
      photo.gps = 0;
    }
    return;
  }
  photo.place_label = place.place_label || null;
  photo.venue = place.venue || null;
  photo.city = place.city || null;
  photo.region = place.region || null;
  photo.country = place.country || null;
  const canApplyCoords = overwriteCoords || !coordsAreProtected(photo);
  if (place.lat != null && place.lon != null && canApplyCoords) {
    photo.lat = place.lat;
    photo.lon = place.lon;
    photo.gps = 0;
    photo.coord_source = place.coord_source || "geocode";
  } else if (!coordsAreProtected(photo) && (place.lat == null || place.lon == null)) {
    photo.lat = null;
    photo.lon = null;
    photo.coord_source = null;
    photo.gps = 0;
  }
}

function applyLightboxEditPreview() {
  const deg = orientationDeg($("#lightbox-orientation")?.value);
  const mirrored = isPressOn($("#lightbox-mirror"));
  const rotateEl = $("#lightbox-rotate");
  if (rotateEl) rotateEl.setAttribute("aria-pressed", deg ? "true" : "false");
  setLightboxTransform(deg, mirrored);
}

function lightboxEditsDirty(photo = lightboxCurrentPhoto()) {
  if (photo?.id == null) return false;
  const orientation = orientationDeg($("#lightbox-orientation")?.value);
  const mirrored = isPressOn($("#lightbox-mirror"));
  const placeValue = ($("#lightbox-place")?.value || "").trim();
  const prevPlace = (photo.place_label || "").trim();
  const takenValue = normalizeTakenAtInput($("#lightbox-taken-at")?.value || "");
  const prevTaken = normalizeTakenAtInput(takenAtToInputValue(photo.taken_at));
  return (
    orientation !== orientationDeg(photo.orientation) ||
    mirrored !== isMirrored(photo) ||
    placeValue !== prevPlace ||
    takenValue !== prevTaken
  );
}

let lightboxSaveInFlight = null;

async function saveLightboxEdits({ showErrors = false } = {}) {
  const photo = lightboxCurrentPhoto();
  const id = photo?.id;
  if (id == null || !lightboxEditsDirty(photo)) return true;
  if (lightboxSaveInFlight) return lightboxSaveInFlight;

  const orientation = orientationDeg($("#lightbox-orientation")?.value);
  const mirrored = isPressOn($("#lightbox-mirror"));
  const placeValue = ($("#lightbox-place")?.value || "").trim();
  const prevPlace = (photo.place_label || "").trim();
  const placeEdited = placeValue !== prevPlace;
  const takenValue = normalizeTakenAtInput($("#lightbox-taken-at")?.value || "");
  const prevTaken = normalizeTakenAtInput(takenAtToInputValue(photo.taken_at));
  const takenEdited = takenValue !== prevTaken;
  const properties = { orientation, mirrored };
  if (placeEdited) properties.place_label = placeValue;
  if (takenEdited && takenValue) properties.taken_at = takenValue;
  const err = $("#lightbox-edit-error");
  if (err) {
    err.hidden = true;
    err.textContent = "";
  }

  lightboxSaveInFlight = (async () => {
    try {
      const res = await api("/api/photos/properties", {
        method: "POST",
        body: JSON.stringify({
          photo_ids: [id],
          properties,
        }),
      });
      if (res.errors?.length && !res.updated) {
        if (showErrors && err) {
          err.textContent = res.errors[0].error || "Update failed";
          err.hidden = false;
        }
        return false;
      }
      photo.orientation = orientation;
      photo.mirrored = mirrored;
      if (takenEdited && takenValue) {
        photo.taken_at = takenValue.replace("T", " ");
        photo.taken_at_source = "manual";
      }
      if (placeEdited) applyPlaceFieldsToPhoto(photo, res.place ?? null);
      const syncEntry = (entry) => {
        if (!entry || entry.photo?.id !== id) return;
        entry.orientation = orientation;
        entry.mirrored = mirrored;
        entry.photo = photo;
      };
      if (lightboxIndex >= 0 && lightboxIndex < lightboxGroup.length) {
        syncEntry(lightboxGroup[lightboxIndex]);
      }
      if (lightboxClusterIndex >= 0 && lightboxClusterIndex < lightboxCluster.length) {
        syncEntry(lightboxCluster[lightboxClusterIndex]);
      }
      if (placeEdited) {
        const placeEl = $("#lightbox-place");
        if (placeEl) placeEl.value = (photo.place_label || "").trim();
        syncLightboxShowMapBtn(photo);
      }
      if (takenEdited) {
        const takenEl = $("#lightbox-taken-at");
        if (takenEl) takenEl.value = takenAtToInputValue(photo.taken_at);
      }
      renderLightboxMeta(photo);
      const patchLocal = (local) => {
        if (!local) return;
        local.orientation = orientation;
        local.mirrored = mirrored;
        if (takenEdited && takenValue) {
          local.taken_at = photo.taken_at;
          local.taken_at_source = "manual";
        }
        if (placeEdited) applyPlaceFieldsToPhoto(local, res.place ?? null);
      };
      patchLocal(browseItems.find((p) => p.id === id));
      patchLocal(dupePhotosById?.get?.(id));
      if (typeof renderBrowseResults === "function" && browseItems.length) {
        renderBrowseResults();
      }
      if (placeEdited || takenEdited) {
        photoMapLoaded = false;
        photoMapBounds = null;
        if (typeof refreshExpandedBranches === "function") {
          const branches = [];
          if (placeEdited) branches.push("places");
          if (takenEdited) branches.push("dates");
          if (placeEdited || takenEdited) branches.push("collections");
          refreshExpandedBranches(branches).catch(() => {});
        }
      }
      await refreshLightboxFileMeta(photo);
      return true;
    } catch (ex) {
      if (showErrors && err) {
        err.textContent = ex.message || "Update failed";
        err.hidden = false;
      }
      return false;
    } finally {
      lightboxSaveInFlight = null;
    }
  })();

  return lightboxSaveInFlight;
}

async function showLightboxPhotoOnMap() {
  const photo = lightboxCurrentPhoto();
  const lat = Number(photo?.lat);
  const lon = Number(photo?.lon);
  if (!photo?.id || !Number.isFinite(lat) || !Number.isFinite(lon)) return;
  const saved = await saveLightboxEdits({ showErrors: true });
  if (!saved) return;
  $("#lightbox")?.close();
  await activateWorkspaceView("map");
  if (photoMapLoadInFlight) await photoMapLoadInFlight;
  else if (!photoMapLoaded) await loadMap(true);
  focusMapOnPhoto(photo.id, lat, lon);
}

function focusMapOnPhoto(photoId, lat, lon) {
  if (!photoMap || !photoMapCluster) return;
  const run = () => {
    photoMap.invalidateSize();
    photoMap.setView([lat, lon], Math.max(photoMap.getZoom(), 15));
  };
  requestAnimationFrame(() => setTimeout(run, 60));
}

function patchLocalPhotoCulled(id, culled) {
  const patch = (p) => {
    if (p && p.id === id) p.culled = culled;
  };
  browseItems.forEach(patch);
  browseItemsFull.forEach(patch);
  const dupe = dupePhotosById?.get?.(id);
  if (dupe) dupe.culled = culled;
  // Active scope must not keep removed photos (else they reappear as Unmatched).
  if (culled && photoVisibility === "active") {
    browseItems = browseItems.filter((p) => p?.id !== id);
    browseItemsFull = browseItemsFull.filter((p) => p?.id !== id);
  }
}

async function removeLightboxPhoto() {
  const photo = lightboxCurrentPhoto();
  const id = photo?.id;
  if (id == null || photo.culled) return;
  const btn = $("#lightbox-remove");
  if (btn) btn.disabled = true;
  try {
    await saveLightboxEdits({ showErrors: true });
    const ids = expandRemoveIdsForHiddenReplicas([id]);
    await removePhotoIds(ids);
    photo.culled = true;
    ids.forEach((rid) => patchLocalPhotoCulled(rid, true));
    browseSelected.delete(browseItemKey(photo));
    if (
      lightboxIndex >= 0 &&
      lightboxIndex < lightboxGroup.length &&
      lightboxGroup[lightboxIndex].photo?.id === id
    ) {
      lightboxGroup[lightboxIndex].photo = photo;
    }
    if (photoVisibility === "active") {
      const gone = new Set(ids);
      lightboxGroup = lightboxGroup.filter((entry) => !gone.has(entry.photo?.id));
      if (!lightboxGroup.length) {
        $("#lightbox")?.close();
        if (typeof renderBrowseResults === "function") renderBrowseResults();
        refreshScopedWorkspaceViews();
        return;
      }
      const next = Math.min(lightboxIndex, lightboxGroup.length - 1);
      lightboxIndex = -1;
      await lightboxNavigateTo(next);
    } else {
      syncLightboxEditControls(photo, photo.orientation, isMirrored(photo));
      renderLightboxMeta(photo);
      syncLightboxNav();
    }
    if (typeof renderBrowseResults === "function" && browseItems.length) {
      renderBrowseResults();
    }
    refreshScopedWorkspaceViews();
  } catch (ex) {
    const err = $("#lightbox-edit-error");
    if (err) {
      err.textContent = ex.message || "Reject failed";
      err.hidden = false;
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function restoreLightboxPhoto() {
  const photo = lightboxCurrentPhoto();
  const id = photo?.id;
  if (id == null || !photo.culled) return;
  const btn = $("#lightbox-restore");
  if (btn) btn.disabled = true;
  try {
    await restorePhotoIds([id]);
    photo.culled = false;
    patchLocalPhotoCulled(id, false);
    if (lightboxIndex >= 0 && lightboxIndex < lightboxGroup.length) {
      lightboxGroup[lightboxIndex].photo = photo;
    }
    if (photoVisibility === "removed") {
      lightboxGroup.splice(lightboxIndex, 1);
      if (!lightboxGroup.length) {
        $("#lightbox")?.close();
        if (typeof renderBrowseResults === "function") renderBrowseResults();
        refreshScopedWorkspaceViews();
        return;
      }
      const next = Math.min(lightboxIndex, lightboxGroup.length - 1);
      lightboxIndex = -1;
      await lightboxNavigateTo(next);
    } else {
      syncLightboxEditControls(photo, photo.orientation, isMirrored(photo));
      renderLightboxMeta(photo);
    }
    if (typeof renderBrowseResults === "function" && browseItems.length) {
      renderBrowseResults();
    }
    refreshScopedWorkspaceViews();
  } catch (ex) {
    const err = $("#lightbox-edit-error");
    if (err) {
      err.textContent = ex.message || "Restore failed";
      err.hidden = false;
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function deleteLightboxPhoto() {
  const photo = lightboxCurrentPhoto();
  const id = photo?.id;
  if (id == null || !photo.culled) return;
  if (
    !confirm(
      "Delete this file?\n\nThe file is moved to the system trash (not permanently erased). It will be removed from the index."
    )
  ) {
    return;
  }
  const btn = $("#lightbox-delete");
  const restoreBtn = $("#lightbox-restore");
  if (btn) btn.disabled = true;
  if (restoreBtn) restoreBtn.disabled = true;
  try {
    const ids = expandDeleteIdsForHiddenReplicas([id]);
    const res = await deletePhotoIds(ids);
    const deletedIds = res?.deleted_ids?.length ? res.deleted_ids : ids;
    const failed = res?.failed || 0;
    dropLocalPhotosById(deletedIds);
    browseSelected.delete(browseItemKey(photo));
    const gone = new Set(deletedIds);
    lightboxGroup = lightboxGroup.filter((entry) => !gone.has(entry.photo?.id));
    lightboxCluster = lightboxCluster.filter((entry) => !gone.has(entry.photo?.id));
    if (failed) {
      alert(
        `Deleted from the index. ${failed} file${failed === 1 ? "" : "s"} could not be moved to trash (kept in Rejected).`
      );
    }
    if (!lightboxGroup.length) {
      $("#lightbox")?.close();
    } else {
      const next = Math.min(Math.max(lightboxIndex, 0), lightboxGroup.length - 1);
      lightboxIndex = -1;
      lightboxCluster = [];
      lightboxClusterIndex = -1;
      await lightboxNavigateTo(next);
    }
    if (typeof renderBrowseResults === "function") renderBrowseResults();
    if (isDupesWorkspace() && typeof loadDupes === "function") {
      await loadDupes();
    }
    refreshScopedWorkspaceViews();
    refreshStats();
  } catch (ex) {
    const err = $("#lightbox-edit-error");
    if (err) {
      err.textContent = ex.message || "Delete failed";
      err.hidden = false;
    }
  } finally {
    if (btn) btn.disabled = false;
    if (restoreBtn) restoreBtn.disabled = false;
  }
}

function syncLightboxLayoutMode() {
  requestAnimationFrame(() => drawLightboxFaceBoxes());
}

const LIGHTBOX_FULLSCREEN_KEY = "fotoman.lightboxFullscreen";

function lightboxFullscreenPref() {
  try {
    return localStorage.getItem(LIGHTBOX_FULLSCREEN_KEY) === "1";
  } catch (_) {
    return false;
  }
}

function setLightboxFullscreen(on, { persist = true } = {}) {
  const dlg = $("#lightbox");
  const btn = $("#lightbox-fullscreen");
  dlg?.classList.toggle("is-fullscreen", on);
  if (btn) {
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    btn.title = on ? "Exit full screen" : "Full screen";
  }
  if (persist) {
    try {
      localStorage.setItem(LIGHTBOX_FULLSCREEN_KEY, on ? "1" : "0");
    } catch (_) {
      /* ignore */
    }
  }
  syncLightboxLayoutMode();
  requestAnimationFrame(() => applyLightboxZoomPan());
}

async function lightboxNavigateTo(idx) {
  if (idx < 0 || idx >= lightboxGroup.length) return;
  if (idx === lightboxIndex) return;
  const saved = await saveLightboxEdits({ showErrors: true });
  if (!saved) return;
  lightboxIndex = idx;
  const entry = lightboxGroup[idx];
  lightboxCluster = [];
  lightboxClusterIndex = -1;
  await showLightboxEntry(entry);
  await setLightboxClusterFromPhoto(entry.photo || null);
  syncLightboxNav();
}

async function lightboxNavigateCluster(delta) {
  if (lightboxCluster.length < 2) return;
  const idx = lightboxClusterIndex + delta;
  if (idx < 0 || idx >= lightboxCluster.length) return;
  const saved = await saveLightboxEdits({ showErrors: true });
  if (!saved) return;
  lightboxClusterIndex = idx;
  await showLightboxEntry(lightboxCluster[idx]);
}

async function showLightboxEntry(entry) {
  if (!entry) return;
  const photo = entry.photo || null;
  const src = entry.src || "";
  resetLightboxZoomPan();
  clearLightboxFaces();
  clearLightboxInfo();
  lightboxFallbackFace = entry.face || null;
  const orient = entry.orientation ?? photo?.orientation;
  const mirrored = entry.mirrored ?? isMirrored(photo);
  syncLightboxEditControls(photo, orient, mirrored);
  renderLightboxMeta(photo);
  if (photo) $("#lightbox-info").hidden = false;
  syncLightboxFramesOpt();
  const img = $("#lightbox-img");
  img.onload = null;
  img.src = src;
  const afterLoad = () => {
    syncLightboxLayoutMode();
    drawLightboxFaceBoxes();
  };
  if (img.complete && img.naturalWidth) afterLoad();
  else img.onload = afterLoad;
  loadLightboxFaces(photo);
  syncLightboxVqa(photo);
  syncLightboxNav();
}

function syncLightboxNav() {
  const prev = $("#lightbox-prev");
  const next = $("#lightbox-next");
  const up = $("#lightbox-up");
  const down = $("#lightbox-down");
  const pos = $("#lightbox-cluster-pos");
  const hasGroup = lightboxGroup.length >= 2;
  if (prev) prev.disabled = !hasGroup || lightboxIndex <= 0;
  if (next) next.disabled = !hasGroup || lightboxIndex >= lightboxGroup.length - 1;
  const clusterNav = lightboxCluster.length >= 2;
  if (up) up.disabled = !clusterNav || lightboxClusterIndex <= 0;
  if (down) down.disabled = !clusterNav || lightboxClusterIndex >= lightboxCluster.length - 1;
  if (pos) {
    pos.textContent = clusterNav
      ? `${lightboxClusterIndex + 1}/${lightboxCluster.length}`
      : "";
  }
}

function openLightbox({ src, face = null, orientation = 0, mirrored = null, photo = null, group = [], groupIndex = -1 } = {}) {
  if (!group.length && (src || photo)) {
    group = [{
      src: src || (photo?.id != null ? `/api/original/${photo.id}` : ""),
      face,
      orientation: orientation ?? photo?.orientation,
      mirrored: mirrored != null ? !!mirrored : isMirrored(photo),
      photo,
    }];
    groupIndex = 0;
  }
  lightboxGroup = group;
  lightboxIndex = groupIndex;
  lightboxCluster = [];
  lightboxClusterIndex = -1;
  const entry = {
    src: src || (photo?.id != null ? `/api/original/${photo.id}` : ""),
    face,
    orientation: orientation ?? photo?.orientation,
    mirrored: mirrored != null ? !!mirrored : isMirrored(photo),
    photo,
  };
  void showLightboxEntry(entry);
  $("#lightbox").showModal();
  syncLightboxLayoutMode();
  void setLightboxClusterFromPhoto(photo).then(() => syncLightboxNav());
}

function openBrowseLightbox(original, face, orientation, photo, group = [], groupIndex = -1, mirrored = false) {
  openLightbox({ src: original, face, orientation, mirrored, photo, group, groupIndex });
}

function syncBrowseFrameVisibility() {
  const root = $("#browse-results");
  if (!root) return;
  const show = !$("#browse-show-face") || isPressOn($("#browse-show-face"));
  root.classList.toggle("hide-face-frames", browseView === "people" && !show);
}

function syncBrowseSelectionDom(root = $("#browse-results")) {
  if (photoVisibility === "selected") {
    if (mapRegionActive && !mapRegionApplyInProgress) {
      clearMapRegionSelection();
    }
    renderBrowseResults();
    refreshScopedWorkspaceViews();
    return;
  }
  root?.querySelectorAll("[data-browse-key]").forEach((el) => {
    el.classList.toggle("selected", browseSelected.has(el.dataset.browseKey));
  });
  if (mapRegionActive && !mapRegionApplyInProgress) {
    clearMapRegionSelection();
  }
  updateBrowseSelectionCount();
  syncTimelineSelectionUi();
}

function applyBrowseItemSelection(key, e) {
  const keys = browseVisibleKeys();
  const idx = keys.indexOf(key);
  if (idx < 0) return;
  const range = e.shiftKey;

  if (range && browseAnchorKey != null) {
    const anchor = keys.indexOf(browseAnchorKey);
    if (anchor >= 0) {
      const lo = Math.min(anchor, idx);
      const hi = Math.max(anchor, idx);
      for (let i = lo; i <= hi; i++) browseSelected.add(keys[i]);
      return;
    }
  }

  if (browseSelected.has(key)) browseSelected.delete(key);
  else browseSelected.add(key);
  browseAnchorKey = key;
}

let photoTreeDrag = null;
let galleryDragSuppressClick = false;

function photosForCardDrag(startKey) {
  const keys =
    browseSelected.has(startKey) && browseSelected.size
      ? [...browseSelected]
      : [startKey];
  const byKey = new Map(browseItems.map((p) => [browseItemKey(p), p]));
  return keys.map((k) => byKey.get(k)).filter((p) => p && p.id != null);
}

function photosForPropertyDrop(photos) {
  const indexed = (photos || []).filter((p) => p.id != null);
  if (!browseKeepersOnlyView()) return indexed;
  const idSet = new Set(expandEditIdsForHiddenReplicas(indexed.map((p) => p.id)));
  const pool = browseItemsFull.length ? browseItemsFull : browseItems;
  return pool.filter((p) => p?.id != null && idSet.has(p.id));
}

/** Face ids for the currently selected tree person only (not every bbox on the photo). */
function selectedPersonFaceIds(photos) {
  const personIds = selectedTreePersonIds();
  if (personIds.length !== 1) return [];
  const pid = personIds[0];
  const ids = [];
  for (const p of photos || []) {
    const faces = browseFaceBoxes(p);
    const match = faces.find((f) => Number(f.person_id) === pid);
    let fid = Number(match?.id);
    if (!(fid > 0) && Number(p.person_id) === pid) fid = Number(p.face_id);
    if (fid > 0) ids.push(fid);
  }
  return [...new Set(ids)];
}

function takenAtFromDatePath(path) {
  const raw = String(path || "");
  if (/^\d{4}$/.test(raw)) return `${raw}-01-01T00:00:00`;
  if (/^\d{4}-\d{2}$/.test(raw)) return `${raw}-01T00:00:00`;
  if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return `${raw}T00:00:00`;
  return null;
}

function placeLabelFromLocationPath(path, kind) {
  const raw = String(path || "");
  if (raw.startsWith("place-suffix::")) return raw.slice("place-suffix::".length);
  if (kind === "place" && raw.startsWith("place::")) return raw.slice("place::".length);
  if (kind === "country" && raw.startsWith("country::")) return raw.slice("country::".length);
  if (kind === "city" && raw.startsWith("city::")) {
    const rest = raw.slice("city::".length);
    const i = rest.indexOf("::");
    if (i < 0) return rest;
    return `${rest.slice(i + 2)}, ${rest.slice(0, i)}`;
  }
  if (kind === "venue" && raw.startsWith("venue::")) {
    const parts = raw.slice("venue::".length).split("::");
    if (parts.length >= 3) return `${parts.slice(2).join("::")}, ${parts[1]}, ${parts[0]}`;
  }
  return null;
}

function placeParentSuffix(label) {
  const s = String(label || "").trim();
  const i = s.indexOf(", ");
  return i >= 0 ? s.slice(i + 2) : "";
}

function placeLabelMatchesSource(label, sourceLabel) {
  const photo = String(label || "").trim();
  const source = String(sourceLabel || "").trim();
  if (!photo || !source) return false;
  return photo === source || photo.endsWith(`, ${source}`);
}

/** Folder-move Place rewrite: keep [*, source-node], replace source parent with target. */
function relocatePlaceLabel(photoLabel, sourceLabel, targetLabel) {
  const photo = String(photoLabel || "").trim();
  const source = String(sourceLabel || "").trim();
  const target = String(targetLabel || "").trim();
  if (!target || !placeLabelMatchesSource(photo, source)) return photo;
  const parent = placeParentSuffix(source);
  if (!parent) return `${photo}, ${target}`;
  const suffix = `, ${parent}`;
  if (photo === parent) return target;
  if (photo.endsWith(suffix)) return `${photo.slice(0, -suffix.length)}, ${target}`;
  return photo;
}

function isPlaceMoveIntoSelf(sourceLabel, targetLabel) {
  return placeLabelMatchesSource(targetLabel, sourceLabel);
}

function treeDropActionAtEvent(e) {
  return treeDropActionFromEl(
    document.elementFromPoint(e.clientX, e.clientY) || e.target
  );
}

function isDraggableTreeBranch(item) {
  if (!item || item.classList.contains("browse-branch")) return false;
  const gb = item.dataset.groupBy;
  const path = item.dataset.path;
  if (!path) return false;
  if (gb === "date") return !!takenAtFromDatePath(path);
  if (gb === "location") return !!placeLabelFromLocationPath(path, item.dataset.kind || "");
  if (gb === "folder" || gb === "collection" || gb === "camera") return true;
  if (gb === "person" && item.classList.contains("browse-person")) {
    return Number(path) > 0;
  }
  return false;
}

function treeItemLabel(item) {
  if (!item) return "";
  const person = item.querySelector(".person-label");
  if (person) {
    return (person.dataset.displayName || person.value || person.textContent || "").trim();
  }
  const editing = item.querySelector(".tree-label-edit");
  if (editing) return (editing.value || "").trim();
  return (item.querySelector(".browse-item-name")?.textContent || "").trim();
}

function treeItemCount(item) {
  const raw = (item?.querySelector(".browse-item-count")?.textContent || "").trim();
  if (!raw) return 0;
  const parts = raw.split("/").map((s) => parseInt(s, 10)).filter(Number.isFinite);
  return parts.length ? parts[parts.length - 1] : 0;
}

function friendlyTakenAt(action) {
  const path = String(action.path || "");
  const m = String(action.taken_at || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return action.taken_at || "";
  const monthName = MONTH_LABELS[Number(m[2])] || m[2];
  const day = Number(m[3]);
  if (/^\d{4}$/.test(path) || /^\d{4}-\d{2}$/.test(path)) return `1 ${monthName} ${m[1]}`;
  return `${day} ${monthName} ${m[1]}`;
}

function branchDropConfirmMessage(action, payload) {
  const src = payload.sourceLabel || treeItemLabel(payload.sourceItem) || payload.sourcePath || "this branch";
  const n = payload.sourceCount || treeItemCount(payload.sourceItem);
  const photos = n > 0 ? `${n} photo${n === 1 ? "" : "s"}` : "photos";
  if (action.type === "date") {
    return `Set the date of ${photos} in “${src}” to ${friendlyTakenAt(action)}?`;
  }
  if (action.type === "place") {
    if (payload.sourceGroupBy === "location" && payload.sourcePlaceLabel) {
      const moved = relocatePlaceLabel(
        payload.sourcePlaceLabel,
        payload.sourcePlaceLabel,
        action.place_label
      );
      return `Move “${src}” under “${action.place_label}”? Place [*,] ${payload.sourcePlaceLabel} becomes [*,] ${moved}.`;
    }
    return `Set the place of ${photos} in “${src}” to “${action.place_label}”?`;
  }
  if (action.type === "person") {
    const dest = action.label || treeItemLabel(action.item) || "this person";
    return `Merge “${src}” into “${dest}”? All faces in the dragged person will be assigned to ${dest}.`;
  }
  if (action.type === "collection-move") {
    const dest = treeItemLabel(action.item) || "this collection";
    return `Move collection “${src}” under “${dest}”?`;
  }
  if (action.type === "collection-relocate") {
    const dest = treeItemLabel(action.item) || "this collection";
    return `Move ${photos} to “${dest}”?`;
  }
  if (action.type === "collection-add") {
    const dest =
      action.folderId == null
        ? "Collections"
        : treeItemLabel(action.item) || "this collection";
    if (payload.sourceGroupBy && payload.sourceGroupBy !== "collection") {
      return `Add “${src}” (and its branches) to “${dest}”?`;
    }
    return `Add ${photos} from “${src}” to collection “${dest}”?`;
  }
  return "Apply this change to the dragged branch?";
}

function treeDropActionFromEl(el) {
  if (!photoTreeDrag) return null;
  const hasCards = !!photoTreeDrag.photos?.length;
  const hasBranch = !!photoTreeDrag.sourceGroupBy;
  if (!hasCards && !hasBranch) return null;
  const item = el?.closest?.(".browse-item");
  if (!item) return null;
  if (item.classList.contains("browse-branch")) {
    if (
      item.dataset.branch === "collections" &&
      photoTreeDrag.sourceGroupBy &&
      photoTreeDrag.sourceGroupBy !== "collection"
    ) {
      return { type: "collection-add", folderId: null, item };
    }
    return null;
  }
  if (item === photoTreeDrag.sourceItem) return null;
  const gb = item.dataset.groupBy;
  const path = item.dataset.path;
  if (gb === "date") {
    const taken_at = takenAtFromDatePath(path);
    if (taken_at) return { type: "date", taken_at, path, item };
  } else if (gb === "location") {
    const place_label = placeLabelFromLocationPath(path, item.dataset.kind || "");
    if (!place_label) return null;
    if (
      photoTreeDrag.sourceGroupBy === "location" &&
      photoTreeDrag.sourcePlaceLabel &&
      isPlaceMoveIntoSelf(photoTreeDrag.sourcePlaceLabel, place_label)
    ) {
      return null;
    }
    return { type: "place", place_label, item };
  } else if (gb === "person" && item.classList.contains("browse-person")) {
    const targetId = Number(item.dataset.path);
    const sourceId = photoTreeDrag.sourcePersonId;
    if (!Number.isFinite(targetId) || targetId <= 0) return null;
    if (sourceId == null || targetId === sourceId) return null;
    const personBranch = photoTreeDrag.sourceGroupBy === "person";
    if (!personBranch && !photoTreeDrag.faceIds?.length) return null;
    const input = item.querySelector(".person-label");
    return {
      type: "person",
      personId: targetId,
      labeled: input?.dataset.labeled === "1",
      label: (input?.dataset.displayName || input?.value || input?.textContent || "").trim(),
      item,
    };
  } else if (gb === "collection") {
    const targetId = Number(path);
    if (!Number.isFinite(targetId) || targetId <= 0) return null;
    if (item.dataset.linked === "1") return null;
    if (photoTreeDrag.sourceGroupBy === "collection") {
      const sourceId = Number(photoTreeDrag.sourcePath);
      if (!Number.isFinite(sourceId) || sourceId === targetId) return null;
      if (photoTreeDrag.sourceItem?.dataset.sourceSubdir === "1") return null;
      if (
        item.closest(".browse-subtree") &&
        photoTreeDrag.sourceItem?.nextElementSibling?.contains(item)
      ) {
        return null;
      }
      return { type: "collection-move", folderId: targetId, item };
    }
    const fromId = photoTreeDrag.sourceCollectionId;
    if (
      fromId != null &&
      fromId !== targetId &&
      !photoTreeDrag.sourceGroupBy &&
      sameCollectionTree(fromId, targetId)
    ) {
      return { type: "collection-relocate", folderId: targetId, fromId, item };
    }
    return { type: "collection-add", folderId: targetId, item };
  }
  return null;
}

function highlightTreeDropItem(item) {
  $("#browse-groups")?.querySelectorAll(".browse-item.drop-ok").forEach((el) => {
    if (el !== item) el.classList.remove("drop-ok");
  });
  if (!item) return;
  item.classList.add("drop-ok");
}

function clearPhotoTreeDrag() {
  photoTreeDrag = null;
  document.body.classList.remove("is-photo-dragging", "drop-allowed");
  highlightTreeDropItem(null);
  $("#photo-map")?.classList.remove("map-drop-target");
}

async function fetchBranchPhotos(groupBy, key) {
  let q = `group_by=${encodeURIComponent(groupBy)}&key=${encodeURIComponent(key)}${removeQuery()}&limit=10000`;
  if (groupBy === "location" || groupBy === "date" || groupBy === "folder" || groupBy === "collection") {
    q += `&include_subfolders=true`;
  }
  return api(`/api/browse/photos?${q}`);
}

async function resolveTreeDragPayload(payload, action) {
  if (payload.photos?.length) return payload;
  if (!payload.sourceGroupBy || payload.sourcePath == null) return payload;
  const fetched = (await fetchBranchPhotos(payload.sourceGroupBy, payload.sourcePath)) || [];
  let photos = fetched.filter((p) => p.id != null);
  if (
    action.type === "place" &&
    payload.sourceGroupBy === "location" &&
    payload.sourcePlaceLabel
  ) {
    const suffixFetched =
      (await fetchBranchPhotos(
        "location",
        `place-suffix::${payload.sourcePlaceLabel}`
      )) || [];
    const byId = new Map(photos.map((p) => [p.id, p]));
    for (const p of suffixFetched) {
      if (p.id != null && !byId.has(p.id)) {
        byId.set(p.id, p);
        photos.push(p);
      }
    }
  }
  const next = { ...payload, photos };
  if (action.type === "person" && payload.sourceGroupBy === "person") {
    next.faceIds = [
      ...new Set(photos.map((p) => Number(p.face_id)).filter((id) => id > 0)),
    ];
  }
  return next;
}

async function applyPlaceRelocate(photos, sourceLabel, targetLabel) {
  const byNew = new Map();
  for (const p of photos) {
    if (p.id == null) continue;
    const cur = (p.place_label || "").trim();
    const next = placeLabelMatchesSource(cur, sourceLabel)
      ? relocatePlaceLabel(cur, sourceLabel, targetLabel)
      : relocatePlaceLabel(sourceLabel, sourceLabel, targetLabel);
    if (!next || next === cur) continue;
    const ids = byNew.get(next);
    if (ids) ids.push(p.id);
    else byNew.set(next, [p.id]);
  }
  if (!byNew.size) return;
  let lastPlace = null;
  for (const [label, ids] of byNew) {
    const res = await api("/api/photos/properties", {
      method: "POST",
      body: JSON.stringify({ photo_ids: ids, properties: { place_label: label } }),
    });
    if (res.errors?.length && !res.updated) {
      throw new Error(res.errors[0].error || "Update failed");
    }
    lastPlace = res.place ?? lastPlace;
  }
  return lastPlace;
}

async function applyTreeDrop(action, payload) {
  if (action.type === "collection-move") {
    await api(`/api/collections/${payload.sourcePath}`, {
      method: "PATCH",
      body: JSON.stringify({ parent_id: action.folderId }),
    });
    await refreshExpandedBranches(["collections"]);
    return;
  }
  if (action.type === "collection-relocate") {
    payload = await resolveTreeDragPayload(payload, action);
    const ids = (payload.photos || []).map((p) => p.id).filter((id) => id != null);
    if (!ids.length) return;
    await api(`/api/collections/${action.folderId}/members/move`, {
      method: "POST",
      body: JSON.stringify({
        photo_ids: ids,
        from_collection_id: action.fromId,
      }),
    });
    await refreshExpandedBranches(["collections"]);
    await reloadBrowseGallery();
    return;
  }
  if (action.type === "collection-add") {
    if (payload.sourceGroupBy && payload.sourcePath != null && payload.sourceGroupBy !== "collection") {
      const culledQ = removeQuery();
      const url =
        action.folderId == null
          ? `/api/collections/import-tree${culledQ ? `?${culledQ.slice(1)}` : ""}`
          : `/api/collections/${action.folderId}/import-tree${culledQ ? `?${culledQ.slice(1)}` : ""}`;
      await api(url, {
        method: "POST",
        body: JSON.stringify({
          group_by: payload.sourceGroupBy,
          key: String(payload.sourcePath),
        }),
      });
      await refreshExpandedBranches(["collections"]);
      const foldersRoot = $("#browse-groups")?.querySelector(
        '.browse-branch[data-branch="collections"]'
      );
      if (foldersRoot && action.folderId == null) {
        foldersRoot.dataset.loaded = "0";
        await toggleBrowseBranch(foldersRoot, true);
      }
      return;
    }
    payload = await resolveTreeDragPayload(payload, action);
    const ids = (payload.photos || []).map((p) => p.id).filter((id) => id != null);
    if (!ids.length) return;
    await api(`/api/collections/${action.folderId}/members`, {
      method: "POST",
      body: JSON.stringify({ photo_ids: ids }),
    });
    await refreshExpandedBranches(["collections"]);
    return;
  }
  if (
    action.type === "person" &&
    payload.sourceGroupBy === "person" &&
    payload.sourcePersonId
  ) {
    await api(`/api/people/${payload.sourcePersonId}`, {
      method: "PATCH",
      body: JSON.stringify({ merge_into: action.personId }),
    });
    const named = action.labeled || isPersonNameLabeled(action.label);
    await reloadAfterPeopleFaceEdit(
      named && !payload.sourceLabeled
        ? [payload.sourcePersonId, action.personId]
        : []
    );
    return;
  }
  payload = await resolveTreeDragPayload(payload, action);
  if (action.type === "date" || action.type === "place") {
    const photos = payload.sourceGroupBy
      ? (payload.photos || []).filter((p) => p.id != null)
      : photosForPropertyDrop(payload.photos);
    if (
      action.type === "place" &&
      payload.sourceGroupBy === "location" &&
      payload.sourcePlaceLabel
    ) {
      const place = await applyPlaceRelocate(
        photos,
        payload.sourcePlaceLabel,
        action.place_label
      );
      if (place !== undefined) {
        await reloadBrowseAfterPropertyEdit({ place_label: action.place_label }, place);
      }
      return;
    }
    const ids = photos.map((p) => p.id);
    if (!ids.length) return;
    const properties =
      action.type === "date"
        ? { taken_at: action.taken_at }
        : { place_label: action.place_label };
    const res = await api("/api/photos/properties", {
      method: "POST",
      body: JSON.stringify({ photo_ids: ids, properties }),
    });
    if (res.errors?.length && !res.updated) {
      throw new Error(res.errors[0].error || "Update failed");
    }
    await reloadBrowseAfterPropertyEdit(properties, res.place ?? null);
    return;
  }
  if (action.type === "person") {
    const faceIds = payload.faceIds || [];
    if (!faceIds.length) return;
    const recluster = new Set(
      [payload.sourcePersonId, action.personId].filter((id) => id > 0)
    );
    for (const faceId of faceIds) {
      const updated = await api(`/api/faces/${faceId}`, {
        method: "PATCH",
        body: JSON.stringify({ person_id: action.personId }),
      });
      if (updated.id) recluster.add(Number(updated.id));
    }
    const named = action.labeled || isPersonNameLabeled(action.label);
    await reloadAfterPeopleFaceEdit(
      named && !payload.sourceLabeled ? [...recluster] : []
    );
  }
}

let pendingBranchDrop = null;

function closeBranchDropConfirm() {
  pendingBranchDrop = null;
  const err = $("#branch-drop-confirm-error");
  if (err) {
    err.hidden = true;
    err.textContent = "";
  }
  const btn = $("#btn-branch-drop-confirm");
  if (btn) btn.disabled = false;
  $("#branch-drop-confirm")?.close();
}

function openBranchDropConfirm(action, payload) {
  pendingBranchDrop = { action, payload };
  const dlg = $("#branch-drop-confirm");
  const msg = $("#branch-drop-confirm-msg");
  const err = $("#branch-drop-confirm-error");
  const btn = $("#btn-branch-drop-confirm");
  if (msg) msg.textContent = branchDropConfirmMessage(action, payload);
  if (err) {
    err.hidden = true;
    err.textContent = "";
  }
  if (btn) btn.disabled = false;
  dlg?.showModal();
}

$("#btn-branch-drop-cancel")?.addEventListener("click", () => closeBranchDropConfirm());
$("#btn-branch-drop-no")?.addEventListener("click", () => closeBranchDropConfirm());
$("#branch-drop-confirm")?.addEventListener("cancel", () => {
  pendingBranchDrop = null;
});
$("#branch-drop-confirm-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const pending = pendingBranchDrop;
  if (!pending) {
    closeBranchDropConfirm();
    return;
  }
  const err = $("#branch-drop-confirm-error");
  const btn = $("#btn-branch-drop-confirm");
  if (err) {
    err.hidden = true;
    err.textContent = "";
  }
  if (btn) btn.disabled = true;
  try {
    await applyTreeDrop(pending.action, pending.payload);
    closeBranchDropConfirm();
  } catch (ex) {
    if (err) {
      err.textContent = ex.message || "Update failed";
      err.hidden = false;
    } else {
      alert(ex.message || "Update failed");
    }
    if (btn) btn.disabled = false;
  }
});

function bindBrowseItemInteractions(root) {
  root.querySelectorAll("[data-browse-key]").forEach((el) => {
    el.addEventListener("mousedown", (e) => {
      if (isCardHoverDetailEvent(e)) {
        el.draggable = false;
        window.addEventListener(
          "mouseup",
          () => {
            el.draggable = true;
          },
          { once: true }
        );
        return;
      }
      if (e.shiftKey) e.preventDefault();
    });
    el.addEventListener("mouseenter", () => {
      setTimelineHoverKey(el.dataset.browseKey);
    });
    el.addEventListener("mouseleave", () => {
      if (timelineHoverKey === el.dataset.browseKey) setTimelineHoverKey(null);
    });
    el.addEventListener("dragstart", (e) => {
      if (isCardHoverDetailEvent(e)) {
        e.preventDefault();
        return;
      }
      const photos = photosForCardDrag(el.dataset.browseKey);
      if (!photos.length) {
        e.preventDefault();
        return;
      }
      const sourcePersonId = selectedTreePersonIds()[0] ?? null;
      const srcInput =
        sourcePersonId != null
          ? $("#browse-groups")?.querySelector(
              `.person-label[data-person="${sourcePersonId}"]`
            )
          : null;
      const sourceCollectionId = (() => {
        const id = activeCollectionId();
        if (id == null) return null;
        return activeCollectionFlags().linked ? null : id;
      })();
      photoTreeDrag = {
        photos,
        faceIds: selectedPersonFaceIds(photos),
        sourcePersonId,
        sourceLabeled: srcInput?.dataset.labeled === "1",
        sourceCollectionId,
      };
      e.dataTransfer.effectAllowed = sourceCollectionId != null ? "move" : "copy";
      e.dataTransfer.setData("text/plain", "fotoman-photos");
      document.body.classList.add("is-photo-dragging");
      galleryDragSuppressClick = false;
    });
    el.addEventListener("dragend", () => {
      clearPhotoTreeDrag();
      galleryDragSuppressClick = true;
      setTimeout(() => {
        galleryDragSuppressClick = false;
      }, 0);
    });
    el.addEventListener("click", (e) => {
      if (galleryDragSuppressClick) {
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      if (e.target.closest("th, .detail-cluster-toggle")) return;
      if (e.detail > 1) return;
      if (isCardHoverDetailEvent(e) && hoverDetailHasSelection()) return;
      applyBrowseItemSelection(el.dataset.browseKey, e);
      syncBrowseSelectionDom(root);
    });
    el.addEventListener("contextmenu", (e) => {
      if (e.target.closest("th")) return;
      e.preventDefault();
      const key = el.dataset.browseKey;
      if (key && !browseSelected.has(key)) {
        browseSelected.clear();
        browseSelected.add(key);
        browseAnchorKey = key;
        syncBrowseSelectionDom(root);
      }
      openEditPropertiesModal();
    });
    el.addEventListener("dblclick", (e) => {
      e.preventDefault();
      const face =
        el.dataset.bboxW != null
          ? {
              bbox_x: Number(el.dataset.bboxX),
              bbox_y: Number(el.dataset.bboxY),
              bbox_w: Number(el.dataset.bboxW),
              bbox_h: Number(el.dataset.bboxH),
            }
          : null;
      const key = el.dataset.browseKey;
      // Left/right follow visible gallery cards; up/down walks the cluster.
      const sequence = browseKeepersOnlyView() ? browseDisplayItems() : sortedBrowseItems();
      const idx = sequence.findIndex((p) => browseItemKey(p) === key);
      const photo = idx >= 0 ? sequence[idx] : null;
      const group = sequence.map((p) => {
        const m = browseMediaUrls(p);
        return { src: m.original, orientation: orientationDeg(p), mirrored: isMirrored(p), photo: p };
      });
      openBrowseLightbox(
        el.dataset.original,
        face,
        el.dataset.orientation,
        photo,
        group,
        idx,
        el.dataset.mirrored === "1"
      );
    });
  });
}

document.addEventListener("dragover", (e) => {
  if (!photoTreeDrag) return;
  const mapLl =
    !photoTreeDrag.sourceGroupBy && photoTreeDrag.photos?.length
      ? mapDropLatLngFromEvent(e)
      : null;
  const action = treeDropActionAtEvent(e);
  e.preventDefault();
  const allow = !!action || !!mapLl;
  const moving =
    action?.type === "collection-move" || action?.type === "collection-relocate";
  e.dataTransfer.dropEffect = allow ? (moving ? "move" : "copy") : "none";
  document.body.classList.toggle("drop-allowed", allow);
  highlightTreeDropItem(action?.item || null);
  $("#photo-map")?.classList.toggle("map-drop-target", !!mapLl);
});

document.addEventListener("dragenter", (e) => {
  if (!photoTreeDrag) return;
  e.preventDefault();
});

document.addEventListener("drop", (e) => {
  if (!photoTreeDrag) return;
  e.preventDefault();
  const mapLl =
    !photoTreeDrag.sourceGroupBy && photoTreeDrag.photos?.length
      ? mapDropLatLngFromEvent(e)
      : null;
  const action = treeDropActionAtEvent(e);
  const payload = photoTreeDrag;
  clearPhotoTreeDrag();
  if (mapLl) {
    swallowNextClick();
    applyMapPhotoDrop(payload.photos, mapLl).catch((err) => {
      alert(err.message || "Update failed");
    });
    return;
  }
  if (!action) return;
  swallowNextClick();
  if (payload.sourceGroupBy) {
    if (
      action.type === "collection-add" ||
      action.type === "collection-move" ||
      action.type === "collection-relocate"
    ) {
      applyTreeDrop(action, payload).catch((err) => {
        alert(err.message || "Update failed");
      });
      return;
    }
    openBranchDropConfirm(action, payload);
    return;
  }
  applyTreeDrop(action, payload).catch((err) => {
    alert(err.message || "Update failed");
  });
});

function removeQuery() {
  return includeCulled() ? "&include_culled=true" : "";
}

function faceBoxAttrs(p) {
  const faces = browseFaceBoxes(p);
  if (!faces.length) return "";
  // Lightbox fallback uses the first face until /faces loads.
  const f = faces[0];
  return ` data-bbox-x="${f.bbox_x}" data-bbox-y="${f.bbox_y}" data-bbox-w="${f.bbox_w}" data-bbox-h="${f.bbox_h}"`;
}

/** Face bboxes for gallery overlays (all faces when present, else single bbox). */
function browseFaceBoxes(p) {
  if (Array.isArray(p?.faces) && p.faces.length) {
    return p.faces.filter((f) => f && f.bbox_w != null && f.bbox_h != null);
  }
  if (p?.bbox_w != null && p?.bbox_h != null) {
    return [
      {
        id: p.face_id,
        person_id: p.person_id,
        labeled: p.labeled,
        display_name: p.display_name || p.person_name,
        person_name: p.person_name,
        bbox_x: p.bbox_x,
        bbox_y: p.bbox_y,
        bbox_w: p.bbox_w,
        bbox_h: p.bbox_h,
      },
    ];
  }
  return [];
}

function mergeBrowseFaceLists(...lists) {
  const map = new Map();
  for (const list of lists) {
    for (const f of list || []) {
      if (!f || f.bbox_w == null || f.bbox_h == null) continue;
      const key =
        f.id != null
          ? `id:${f.id}`
          : `b:${f.bbox_x},${f.bbox_y},${f.bbox_w},${f.bbox_h}`;
      if (!map.has(key)) map.set(key, f);
    }
  }
  return [...map.values()];
}

function faceBoxOverlay(p) {
  const faces = browseFaceBoxes(p);
  if (!faces.length) return "";
  return faces
    .map((f) => {
      const unlabeled = faceBoxIsUnlabeled(f);
      return `<span class="face-box${unlabeled ? " is-unlabeled" : ""}" style="left:${f.bbox_x * 100}%;top:${f.bbox_y * 100}%;width:${f.bbox_w * 100}%;height:${f.bbox_h * 100}%;${faceBoxStyle(f)}"></span>`;
    })
    .join("");
}

function browseMediaInner(p, media) {
  const box = faceBoxOverlay(p);
  const style = orientationStyle(p);
  const styleAttr = style ? ` style="${style}"` : "";
  if (!box) {
    return `<img loading="lazy" src="${media.thumb}" alt="" draggable="false"${styleAttr} />`;
  }
  return `<div class="face-stage"${styleAttr}>
    <img loading="lazy" src="${media.thumb}" alt="" draggable="false" />
    ${box}
  </div>`;
}

function renderBrowseThumbCard(p, clusterCounts = null) {
  const media = browseMediaUrls(p);
  const key = browseItemKey(p);
  const removed = !!p.culled;
  const sel = browseSelected.has(key) ? " selected" : "";
  const hasFace = browseFaceBoxes(p).length ? " has-face" : "";
  const path = photoPath(p);
  const clusterN =
    clusterCounts && p.group_id != null ? clusterCounts.get(p.group_id) || 0 : 0;
  const clusterBadge =
    clusterN > 1
      ? `<span class="cluster-badge" title="${clusterN} photos in cluster">${clusterN}</span>`
      : "";
  return `
      <article class="card${removed ? " removed" : ""}${sel}" draggable="true" data-browse-key="${key}" data-original="${media.original}" data-orientation="${orientationDeg(p)}" data-mirrored="${isMirrored(p) ? 1 : 0}"${faceBoxAttrs(p)}>
        ${removed ? `<span class="removed-badge">REJECTED</span>` : ""}
        ${clusterBadge}
        <div class="card-media${hasFace}">
          ${browseMediaInner(p, media)}
          ${browseItemHoverDetail(p)}
        </div>
        <div class="meta">
          <strong title="${path}">${basename(p.path)}</strong>
          <span class="meta-line">${browseItemMetaLine(p)}</span>
        </div>
      </article>`;
}

function renderBrowseDetailRow(p, { member = false, clusterCount = 0, expanded = false } = {}) {
  const media = browseMediaUrls(p);
  const key = browseItemKey(p);
  const removed = !!p.culled;
  const sel = browseSelected.has(key) ? " selected" : "";
  const path = photoPath(p);
  const memberClass = member ? " detail-cluster-member" : "";
  const toggle =
    !member && clusterCount > 1
      ? `<button type="button" class="detail-cluster-toggle" aria-expanded="${expanded ? "true" : "false"}" aria-label="${expanded ? "Collapse" : "Expand"} cluster">${expanded ? "▼" : "▶"}</button>`
      : `<span class="detail-cluster-spacer" aria-hidden="true"></span>`;
  const countBadge =
    !member && clusterCount > 1
      ? `<span class="detail-cluster-count" title="${clusterCount} photos in cluster">${clusterCount}</span>`
      : "";
  return `
      <tr class="${removed ? "removed " : ""}${sel.trim()}${memberClass}" draggable="true" data-browse-key="${key}" data-original="${media.original}" data-orientation="${orientationDeg(p)}" data-mirrored="${isMirrored(p) ? 1 : 0}"${faceBoxAttrs(p)}${member && !expanded ? " hidden" : ""}>
        <td class="browse-detail-name">
          ${toggle}
          <img loading="lazy" src="${media.thumb}" alt="" draggable="false" style="${orientationStyle(p)}" />
          <span title="${path}">${basename(p.path)}${removed ? " · REJECTED" : ""}</span>
          ${countBadge}
        </td>
        <td>${photoArchiveName(p) || "—"}</td>
        <td class="browse-detail-path" title="${path}">${path || "—"}</td>
        <td>${p.taken_at ? p.taken_at.slice(0, 10) : "—"}</td>
        <td>${p.camera || "—"}</td>
        <td>${p.place_label || "—"}</td>
        <td class="num">${formatBytes(p.size)}</td>
        <td class="num">${formatDims(p)}</td>
        <td>${formatAspect(p)}</td>
      </tr>`;
}

function clusterDetailGroups(items) {
  const groups = [];
  const seen = new Map();
  for (const p of items) {
    const gid = p?.group_id;
    if (gid == null) {
      groups.push({ groupId: null, photos: [p] });
      continue;
    }
    let g = seen.get(gid);
    if (!g) {
      g = { groupId: gid, photos: [] };
      seen.set(gid, g);
      groups.push(g);
    }
    g.photos.push(p);
  }
  for (const g of groups) {
    if (g.photos.length < 2) continue;
    g.photos.sort((a, b) => {
      const ak = isClusterKeeper(a) ? 0 : 1;
      const bk = isClusterKeeper(b) ? 0 : 1;
      if (ak !== bk) return ak - bk;
      return (Number(a.id) || 0) - (Number(b.id) || 0);
    });
  }
  const { key, dir } = browseSort;
  const mult = dir === "asc" ? 1 : -1;
  groups.sort((a, b) => {
    const av = browseSortValue(a.photos[0], key);
    const bv = browseSortValue(b.photos[0], key);
    if (av < bv) return -1 * mult;
    if (av > bv) return 1 * mult;
    return (basename(a.photos[0].path) || "").localeCompare(basename(b.photos[0].path) || "");
  });
  return groups;
}

function browseVisibleDetailKeys() {
  const keys = [];
  for (const g of clusterDetailGroups(browseDisplayItems())) {
    if (!g.photos.length) continue;
    keys.push(browseItemKey(g.photos[0]));
    const expanded = g.groupId != null && detailsExpandedGroups.has(String(g.groupId));
    if (expanded) {
      for (let i = 1; i < g.photos.length; i++) keys.push(browseItemKey(g.photos[i]));
    }
  }
  return keys;
}

function bindDetailClusterToggles(root) {
  root.querySelectorAll(".detail-cluster-toggle").forEach((btn) => {
    btn.addEventListener("mousedown", (e) => e.stopPropagation());
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const tbody = btn.closest("tbody.detail-cluster");
      const gid = tbody?.dataset.groupId;
      if (gid == null || gid === "") return;
      const expand = !detailsExpandedGroups.has(gid);
      if (expand) detailsExpandedGroups.add(gid);
      else detailsExpandedGroups.delete(gid);
      btn.setAttribute("aria-expanded", expand ? "true" : "false");
      btn.setAttribute("aria-label", expand ? "Collapse cluster" : "Expand cluster");
      btn.textContent = expand ? "▼" : "▶";
      tbody.querySelectorAll("tr.detail-cluster-member").forEach((tr) => {
        tr.hidden = !expand;
      });
    });
  });
}

function renderItemsWithMonthDividers(items, renderItem, dividerForKey) {
  if (!showMonthDividers()) return items.map(renderItem).join("");
  let last = null;
  const parts = [];
  for (const p of items) {
    const mk = photoMonthKey(p);
    if (mk !== last) {
      parts.push(dividerForKey(mk));
      last = mk;
    }
    parts.push(renderItem(p));
  }
  return parts.join("");
}

function renderBrowseThumbs(items) {
  const clusterCounts = browseKeepersOnlyView() ? clusterCountsByGroupId() : null;
  return renderItemsWithMonthDividers(
    items,
    (p) => renderBrowseThumbCard(p, clusterCounts),
    monthDividerHtml
  );
}

function renderBrowseDetails(items) {
  const sortIcon = (key) => {
    if (browseSort.key !== key) return "";
    return browseSort.dir === "asc" ? " ▲" : " ▼";
  };
  const head = BROWSE_DETAIL_COLS.map(
    (c) =>
      `<th data-sort="${c.key}" class="${browseSort.key === c.key ? "sorted" : ""}">${c.label}${sortIcon(c.key)}</th>`
  ).join("");
  const bodies = clusterDetailGroups(items)
    .map((g) => {
      const expanded =
        g.groupId != null && detailsExpandedGroups.has(String(g.groupId));
      const count = g.photos.length;
      const headRow = renderBrowseDetailRow(g.photos[0], {
        clusterCount: count,
        expanded,
      });
      const members = g.photos
        .slice(1)
        .map((p) => renderBrowseDetailRow(p, { member: true, expanded }))
        .join("");
      const gid = g.groupId != null ? String(g.groupId) : "";
      return `<tbody class="detail-cluster" data-group-id="${gid}">${headRow}${members}</tbody>`;
    })
    .join("");
  return `<div class="browse-details-wrap"><table class="browse-details">
    <thead><tr>${head}</tr></thead>
    ${bodies}
  </table></div>`;
}

/** @type {{
 *   granularity: "year"|"month"|"day",
 *   buckets: { key: string, label: string, keys: string[], times: number[], t0: number, t1: number, leftPct: number, widthPct: number }[],
 *   itemTimes: Map<string, number|null>,
 *   tMin: number,
 *   tMax: number,
 *   datedWidthPct: number,
 * }} */
let timelineModel = {
  granularity: "month",
  buckets: [],
  itemTimes: new Map(),
  tMin: 0,
  tMax: 0,
  datedWidthPct: 100,
};
let timelineDrag = null;

function photoTimelineBucketKey(p, gran) {
  if (!p?.taken_at) return "";
  const s = String(p.taken_at);
  if (gran === "year") return s.slice(0, 4);
  if (gran === "month") return s.slice(0, 7);
  return s.slice(0, 10);
}

function photoTakenMs(p) {
  if (!p?.taken_at) return null;
  const t = Date.parse(String(p.taken_at).replace(" ", "T"));
  return Number.isFinite(t) ? t : null;
}

function pickTimelineGranularity(items) {
  const days = new Set();
  const months = new Set();
  for (const p of items) {
    if (!p?.taken_at) continue;
    const s = String(p.taken_at);
    days.add(s.slice(0, 10));
    months.add(s.slice(0, 7));
  }
  if (!days.size) return "year";
  if (days.size <= 90) return "day";
  if (months.size <= 72) return "month";
  return "year";
}

function timelineBucketLabel(key, gran) {
  if (!key) return "Undated";
  if (gran === "year") return key;
  if (gran === "month") {
    const [y, m] = key.split("-");
    const name = (MONTH_LABELS[Number(m)] || m).slice(0, 3);
    return `${name} ${y}`;
  }
  const [y, m, d] = key.split("-");
  const name = (MONTH_LABELS[Number(m)] || m).slice(0, 3);
  return `${name} ${Number(d)}, ${y}`;
}

function timelineGranLabel(gran) {
  if (gran === "day") return "by day";
  if (gran === "month") return "by month";
  return "by year";
}

function timelineBucketBoundsMs(key, gran) {
  if (!key) return null;
  if (gran === "year") {
    const y = Number(key);
    return [Date.UTC(y, 0, 1), Date.UTC(y + 1, 0, 1)];
  }
  if (gran === "month") {
    const [y, m] = key.split("-").map(Number);
    return [Date.UTC(y, m - 1, 1), Date.UTC(y, m, 1)];
  }
  const [y, m, d] = key.split("-").map(Number);
  return [Date.UTC(y, m - 1, d), Date.UTC(y, m - 1, d + 1)];
}

function nextTimelineBucketKey(key, gran) {
  if (gran === "year") return String(Number(key) + 1);
  if (gran === "month") {
    let y = Number(key.slice(0, 4));
    let m = Number(key.slice(5, 7)) + 1;
    if (m > 12) {
      m = 1;
      y += 1;
    }
    return `${y}-${String(m).padStart(2, "0")}`;
  }
  const [y, m, d] = key.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d + 1));
  return `${dt.getUTCFullYear()}-${String(dt.getUTCMonth() + 1).padStart(2, "0")}-${String(dt.getUTCDate()).padStart(2, "0")}`;
}

function enumerateTimelineKeys(fromKey, toKey, gran) {
  const out = [];
  let cur = fromKey;
  let guard = 0;
  while (cur && cur <= toKey && guard < 20000) {
    out.push(cur);
    if (cur === toKey) break;
    cur = nextTimelineBucketKey(cur, gran);
    guard += 1;
  }
  return out;
}

function buildTimelineModel(items) {
  const gran = pickTimelineGranularity(items);
  const map = new Map();
  const undatedKeys = [];
  const itemTimes = new Map();
  for (const p of items) {
    const itemKey = browseItemKey(p);
    const ms = photoTakenMs(p);
    itemTimes.set(itemKey, ms);
    const bk = photoTimelineBucketKey(p, gran);
    if (!bk) {
      undatedKeys.push(itemKey);
      continue;
    }
    let b = map.get(bk);
    if (!b) {
      b = { key: bk, keys: [], times: [] };
      map.set(bk, b);
    }
    b.keys.push(itemKey);
    b.times.push(ms != null ? ms : 0);
  }

  const datedKeys = [...map.keys()].sort();
  let filledKeys = datedKeys;
  if (datedKeys.length >= 2) {
    const enumerated = enumerateTimelineKeys(
      datedKeys[0],
      datedKeys[datedKeys.length - 1],
      gran
    );
    // Fill calendar gaps when the span stays readable; otherwise keep occupied buckets only.
    if (enumerated.length <= 180) filledKeys = enumerated;
  }

  let tMin = 0;
  let tMax = 0;
  if (filledKeys.length) {
    const first = timelineBucketBoundsMs(filledKeys[0], gran);
    const last = timelineBucketBoundsMs(filledKeys[filledKeys.length - 1], gran);
    tMin = first[0];
    tMax = last[1];
  }

  const hasUndated = undatedKeys.length > 0;
  const datedWidthPct = hasUndated && filledKeys.length ? 96 : 100;
  const span = Math.max(1, tMax - tMin);

  const buckets = filledKeys.map((k) => {
    const src = map.get(k) || { keys: [], times: [] };
    const [t0, t1] = timelineBucketBoundsMs(k, gran);
    const leftPct = ((t0 - tMin) / span) * datedWidthPct;
    const widthPct = Math.max(0.15, ((t1 - t0) / span) * datedWidthPct);
    return {
      key: k,
      label: timelineBucketLabel(k, gran),
      keys: src.keys,
      times: src.times,
      t0,
      t1,
      leftPct,
      widthPct,
    };
  });

  if (hasUndated) {
    buckets.push({
      key: "",
      label: "Undated",
      keys: undatedKeys,
      times: undatedKeys.map(() => 0),
      t0: tMax,
      t1: tMax,
      leftPct: datedWidthPct,
      widthPct: 100 - datedWidthPct,
    });
  }

  return {
    granularity: gran,
    buckets,
    itemTimes,
    tMin,
    tMax,
    datedWidthPct,
  };
}

function timelineNeedlePct(ms) {
  const { tMin, tMax, datedWidthPct, buckets } = timelineModel;
  if (ms == null) {
    const undated = buckets.find((b) => !b.key);
    if (!undated) return null;
    return undated.leftPct + undated.widthPct / 2;
  }
  if (!(tMax > tMin)) return datedWidthPct / 2;
  const pct = ((ms - tMin) / (tMax - tMin)) * datedWidthPct;
  return Math.min(datedWidthPct, Math.max(0, pct));
}

function timelineBucketIndexAtClientX(clientX) {
  const track = $("#browse-timeline-track");
  const n = timelineModel.buckets.length;
  if (!track || n <= 0) return -1;
  const rect = track.getBoundingClientRect();
  if (rect.width <= 0) return -1;
  const pct = ((clientX - rect.left) / rect.width) * 100;
  for (let i = 0; i < n; i++) {
    const b = timelineModel.buckets[i];
    if (pct >= b.leftPct && pct < b.leftPct + b.widthPct) return i;
  }
  // Gaps (sparse timelines) / float edges: nearest bucket by edge distance.
  // Never default to the last bucket — that snaps the brush to the right margin.
  let best = 0;
  let bestDist = Infinity;
  for (let i = 0; i < n; i++) {
    const b = timelineModel.buckets[i];
    const lo = b.leftPct;
    const hi = b.leftPct + b.widthPct;
    const dist = pct < lo ? lo - pct : pct > hi ? pct - hi : 0;
    if (dist < bestDist) {
      bestDist = dist;
      best = i;
    }
  }
  return best;
}

function timelineSelectBuckets(lo, hi, { additive = false } = {}) {
  const n = timelineModel.buckets.length;
  if (n <= 0) return;
  const a = Math.max(0, Math.min(lo, hi));
  const b = Math.min(n - 1, Math.max(lo, hi));
  if (!additive) browseSelected.clear();
  const rangeKeys = [];
  let lastKey = null;
  for (let i = a; i <= b; i++) {
    const bucket = timelineModel.buckets[i];
    for (const key of bucket.keys) {
      browseSelected.add(key);
      rangeKeys.push(key);
      lastKey = key;
    }
  }
  if (lastKey != null) browseAnchorKey = lastKey;
  if (isDupesWorkspace()) {
    if (!additive) dupeSelected.clear();
    for (const key of rangeKeys) {
      const id = Number(String(key).startsWith("id:") ? key.slice(3) : NaN);
      if (Number.isFinite(id) && dupePhotosById.has(id)) dupeSelected.add(id);
    }
    document.querySelectorAll("#dupe-list .dupe-members [data-id]").forEach((el) => {
      el.classList.toggle("selected", dupeSelected.has(Number(el.dataset.id)));
    });
    syncDupeToolbar();
  } else {
    syncBrowseSelectionDom();
  }
  if (currentWorkspaceView() === "map") {
    fitMapToTimelineSelection(rangeKeys);
  } else {
    scrollBrowseToFirstTimelineKey(rangeKeys);
  }
}

function fitMapToTimelineSelection(keys) {
  if (!photoMap || !photoMapCluster || !keys?.length) return;
  const want = new Set();
  for (const key of keys) {
    const p = browseItems.find((x) => browseItemKey(x) === key);
    if (p?.id != null) want.add(p.id);
  }
  if (!want.size) return;
  const latlngs = [];
  for (const marker of photoMapCluster.getLayers()) {
    if (want.has(marker.options?.photoId) && marker.getLatLng) {
      latlngs.push(marker.getLatLng());
    }
  }
  if (!latlngs.length) return;
  fitMapLatLngs(latlngs, { padding: [48, 48], maxZoom: 15 });
}

function scrollBrowseToFirstTimelineKey(keys) {
  if (!keys?.length) return;
  const want = new Set(keys);
  if (isDupesWorkspace()) {
    for (const key of keys) {
      const id = Number(String(key).startsWith("id:") ? key.slice(3) : NaN);
      if (!Number.isFinite(id)) continue;
      const el = $("#dupe-list")?.querySelector(
        `.dupe-members [data-id="${CSS.escape(String(id))}"]`
      );
      if (el) {
        el.scrollIntoView({ block: "nearest", behavior: "smooth" });
        return;
      }
    }
    return;
  }
  const first = browseDisplayItems().find((p) => want.has(browseItemKey(p)));
  const key = first ? browseItemKey(first) : keys[0];
  const root = $("#browse-results");
  const el = root?.querySelector(`[data-browse-key="${CSS.escape(key)}"]`);
  el?.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function syncTimelineSelectionUi() {
  renderTimelineNeedles();
}

let timelineHoverKey = null;

function setTimelineHoverKey(key) {
  if (timelineHoverKey === key) return;
  timelineHoverKey = key || null;
  renderTimelineNeedles();
}

function renderTimelineNeedles() {
  const root = $("#browse-timeline-needles");
  if (!root) return;
  if (!timelineModel.buckets.length) {
    root.innerHTML = "";
    return;
  }
  const parts = [];
  let selectedKeys;
  if (isDupesWorkspace()) {
    selectedKeys = [...dupeSelected]
      .map((id) => {
        const p = dupePhotosById.get(id);
        return p ? browseItemKey(p) : `id:${id}`;
      })
      .filter(Boolean);
  } else {
    selectedKeys = [...browseSelected];
  }
  const selectedOk = selectedKeys.length > 0 && selectedKeys.length <= 500;
  if (selectedOk) {
    for (const key of selectedKeys) {
      if (!timelineModel.itemTimes.has(key)) continue;
      const pct = timelineNeedlePct(timelineModel.itemTimes.get(key));
      if (pct == null) continue;
      parts.push(`<span class="browse-timeline-needle" style="left:${pct}%"></span>`);
    }
  }
  if (timelineHoverKey && timelineModel.itemTimes.has(timelineHoverKey)) {
    const pct = timelineNeedlePct(timelineModel.itemTimes.get(timelineHoverKey));
    if (pct != null) {
      parts.push(
        `<span class="browse-timeline-needle is-hover" style="left:${pct}%"></span>`
      );
    }
  }
  root.innerHTML = parts.join("");
}

function timelineEdgeLabels(buckets) {
  const dated = buckets.filter((b) => b.key);
  if (!dated.length) return buckets[0] ? [buckets[0].label] : [];
  if (dated.length === 1) return [dated[0].label];
  const mid = dated[Math.floor(dated.length / 2)];
  return [dated[0].label, mid.label, dated[dated.length - 1].label];
}

function setTimelineBrush(lo, hi) {
  const brush = $("#browse-timeline-brush");
  const n = timelineModel.buckets.length;
  if (!brush || n <= 0) return;
  const a = Math.max(0, Math.min(lo, hi));
  const b = Math.min(n - 1, Math.max(lo, hi));
  const left = timelineModel.buckets[a].leftPct;
  const right =
    timelineModel.buckets[b].leftPct + timelineModel.buckets[b].widthPct;
  brush.hidden = false;
  brush.style.left = `${left}%`;
  brush.style.width = `${Math.max(0.2, right - left)}%`;
  $("#browse-timeline-bars")
    ?.querySelectorAll(".browse-timeline-bar")
    .forEach((el, i) => {
      el.classList.toggle("is-hot", i >= a && i <= b);
    });
}

function clearTimelineBrush() {
  const brush = $("#browse-timeline-brush");
  if (brush) brush.hidden = true;
  $("#browse-timeline-bars")
    ?.querySelectorAll(".browse-timeline-bar")
    .forEach((el) => el.classList.remove("is-hot"));
}

function renderBrowseTimeline(items) {
  const root = $("#browse-timeline");
  const split = $("#split-timeline");
  if (!root) return;
  if (!TIMELINE_WORKSPACE_VIEWS.has(currentWorkspaceView()) || !items.length) {
    root.hidden = true;
    if (split) split.hidden = true;
    timelineModel = {
      granularity: "month",
      buckets: [],
      itemTimes: new Map(),
      tMin: 0,
      tMax: 0,
      datedWidthPct: 100,
    };
    return;
  }
  timelineModel = buildTimelineModel(items);
  const { granularity, buckets } = timelineModel;
  if (!buckets.length) {
    root.hidden = true;
    if (split) split.hidden = true;
    return;
  }
  root.hidden = false;
  if (split) split.hidden = false;
  const granEl = $("#browse-timeline-gran");
  if (granEl) granEl.textContent = timelineGranLabel(granularity);

  const maxCount = Math.max(1, ...buckets.map((b) => b.keys.length));
  const bars = $("#browse-timeline-bars");
  if (bars) {
    bars.innerHTML = buckets
      .map((b, i) => {
        const h =
          b.keys.length > 0
            ? Math.max(10, Math.round((b.keys.length / maxCount) * 100))
            : 0;
        const empty = b.keys.length ? "" : " is-empty";
        return `<div class="browse-timeline-bar${empty}" data-idx="${i}" title="${b.label} · ${b.keys.length}" role="option" style="left:${b.leftPct}%;width:${b.widthPct}%"><i style="height:${h}%"></i></div>`;
      })
      .join("");
  }
  const labels = $("#browse-timeline-labels");
  if (labels) {
    labels.innerHTML = timelineEdgeLabels(buckets)
      .map((t) => `<span>${t}</span>`)
      .join("");
  }
  clearTimelineBrush();
  syncTimelineSelectionUi();
  if (currentWorkspaceView() === "map") {
    setTimeout(() => photoMap?.invalidateSize(), 60);
  }
}

function onTimelinePointerDown(e) {
  if (e.button != null && e.button !== 0) return;
  const idx = timelineBucketIndexAtClientX(e.clientX);
  if (idx < 0) return;
  e.preventDefault();
  timelineDrag = {
    start: idx,
    last: idx,
    additive: e.shiftKey || e.metaKey || e.ctrlKey,
  };
  setTimelineBrush(idx, idx);
}

function onTimelinePointerMove(e) {
  if (!timelineDrag) return;
  const idx = timelineBucketIndexAtClientX(e.clientX);
  if (idx < 0) return;
  timelineDrag.last = idx;
  setTimelineBrush(timelineDrag.start, idx);
}

function onTimelinePointerUp(e) {
  if (!timelineDrag) return;
  const idx = timelineBucketIndexAtClientX(e.clientX);
  const end = idx >= 0 ? idx : timelineDrag.last;
  const { start, additive } = timelineDrag;
  timelineDrag = null;
  clearTimelineBrush();
  timelineSelectBuckets(start, end, { additive });
}

function onTimelineContextMenu(e) {
  e.preventDefault();
  if (isDupesWorkspace()) {
    if (!dupeSelected.size) return;
    dupeSelected.clear();
    document.querySelectorAll("#dupe-list .dupe-members [data-id]").forEach((el) => {
      el.classList.remove("selected");
    });
    syncDupeToolbar();
    return;
  }
  if (!browseSelected.size) return;
  browseSelected.clear();
  browseAnchorKey = null;
  syncBrowseSelectionDom();
}

function bindBrowseTimeline() {
  const track = $("#browse-timeline-track");
  if (!track || track.dataset.bound) return;
  track.dataset.bound = "1";
  track.addEventListener("mousedown", onTimelinePointerDown);
  track.addEventListener("contextmenu", onTimelineContextMenu);
  window.addEventListener("mousemove", onTimelinePointerMove);
  window.addEventListener("mouseup", onTimelinePointerUp);
}

const SIDEBAR_WIDTH_KEY = "fotoman.sidebarWidth";
const TIMELINE_HEIGHT_KEY = "fotoman.timelineHeight";
const SIDEBAR_WIDTH_MIN = 160;
const SIDEBAR_WIDTH_MAX = 520;
const TIMELINE_HEIGHT_MIN = 64;
const TIMELINE_HEIGHT_MAX = 560;

function clamp(n, lo, hi) {
  return Math.max(lo, Math.min(hi, n));
}

function applySidebarWidth(px) {
  const w = clamp(Math.round(px), SIDEBAR_WIDTH_MIN, SIDEBAR_WIDTH_MAX);
  document.documentElement.style.setProperty("--sidebar-width", `${w}px`);
  return w;
}

function applyTimelineHeight(px) {
  const h = clamp(Math.round(px), TIMELINE_HEIGHT_MIN, TIMELINE_HEIGHT_MAX);
  document.documentElement.style.setProperty("--timeline-height", `${h}px`);
  return h;
}

function loadLayoutSplitterPrefs() {
  try {
    const sw = localStorage.getItem(SIDEBAR_WIDTH_KEY);
    if (sw != null && sw !== "") applySidebarWidth(Number(sw));
    const th = localStorage.getItem(TIMELINE_HEIGHT_KEY);
    if (th != null && th !== "") applyTimelineHeight(Number(th));
  } catch (_) {
    /* ignore */
  }
}

function saveSidebarWidth(px) {
  try {
    localStorage.setItem(SIDEBAR_WIDTH_KEY, String(px));
  } catch (_) {
    /* ignore */
  }
}

function saveTimelineHeight(px) {
  try {
    localStorage.setItem(TIMELINE_HEIGHT_KEY, String(px));
  } catch (_) {
    /* ignore */
  }
}

function bindLayoutSplitter(el, { onMove, onEnd }) {
  if (!el || el.dataset.bound) return;
  el.dataset.bound = "1";
  el.addEventListener("mousedown", (e) => {
    if (e.button != null && e.button !== 0) return;
    e.preventDefault();
    el.classList.add("is-dragging");
    document.body.style.cursor = getComputedStyle(el).cursor;
    document.body.style.userSelect = "none";
    const onPointerMove = (ev) => onMove(ev);
    const onPointerUp = (ev) => {
      el.classList.remove("is-dragging");
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      window.removeEventListener("mousemove", onPointerMove);
      window.removeEventListener("mouseup", onPointerUp);
      onEnd?.(ev);
    };
    window.addEventListener("mousemove", onPointerMove);
    window.addEventListener("mouseup", onPointerUp);
  });
}

function bindLayoutSplitters() {
  loadLayoutSplitterPrefs();
  const layout = $(".app-layout");
  const treeSplit = $("#split-tree");
  bindLayoutSplitter(treeSplit, {
    onMove: (ev) => {
      if (!layout) return;
      const rect = layout.getBoundingClientRect();
      const narrow = window.matchMedia("(max-width: 720px)").matches;
      if (narrow) {
        const h = clamp(ev.clientY - rect.top, 120, Math.max(120, rect.height - 160));
        document.documentElement.style.setProperty("--sidebar-height", `${Math.round(h)}px`);
        return;
      }
      applySidebarWidth(ev.clientX - rect.left);
    },
    onEnd: () => {
      const narrow = window.matchMedia("(max-width: 720px)").matches;
      if (!narrow) {
        const raw = getComputedStyle(document.documentElement).getPropertyValue("--sidebar-width");
        const px = Number.parseFloat(raw);
        if (Number.isFinite(px)) saveSidebarWidth(Math.round(px));
      }
      if (typeof photoMap !== "undefined" && photoMap) {
        try {
          photoMap.invalidateSize();
        } catch (_) {
          /* ignore */
        }
      }
    },
  });

  const content = $(".app-content");
  const timeSplit = $("#split-timeline");
  bindLayoutSplitter(timeSplit, {
    onMove: (ev) => {
      if (!content) return;
      const rect = content.getBoundingClientRect();
      // Splitter sits above the timeline; height is distance from content bottom.
      const fromBottom = rect.bottom - ev.clientY;
      applyTimelineHeight(fromBottom);
    },
    onEnd: () => {
      const raw = getComputedStyle(document.documentElement).getPropertyValue("--timeline-height");
      const px = Number.parseFloat(raw);
      if (Number.isFinite(px)) saveTimelineHeight(Math.round(px));
    },
  });
}

function renderBrowseResults() {
  const root = $("#browse-results");
  root.className =
    browseGalleryView === "thumbs"
      ? "browse-results-gallery"
      : "browse-results-details";
  if (!browseItems.length) {
    root.innerHTML =
      photoVisibility === "removed"
        ? `<p class="muted">Rejected is empty.</p>`
        : photoVisibility === "selected"
          ? `<p class="muted">No photos selected.</p>`
          : `<p class="muted">No photos in this group.</p>`;
    updateBrowseSelectionCount();
    renderBrowseTimeline([]);
    applyBrowseTileSize();
    return;
  }
  const items = browseDisplayItems();
  if (!items.length) {
    root.innerHTML =
      photoVisibility === "selected"
        ? `<p class="muted">No photos selected.</p>`
        : currentAspectFilter()
          ? `<p class="muted">No photos match this aspect.</p>`
          : `<p class="muted">No photos to show (cluster replicas are hidden in this view).</p>`;
    updateBrowseSelectionCount();
    renderBrowseTimeline([]);
    applyBrowseTileSize();
    return;
  }
  if (browseGalleryView === "details") root.innerHTML = renderBrowseDetails(items);
  else root.innerHTML = renderBrowseThumbs(items);
  applyBrowseTileSize();

  bindBrowseItemInteractions(root);
  if (browseGalleryView === "details") {
    bindDetailClusterToggles(root);
    root.querySelectorAll("th[data-sort]").forEach((th) => {
      th.addEventListener("click", (e) => {
        e.stopPropagation();
        const key = th.dataset.sort;
        if (browseSort.key === key) {
          browseSort.dir = browseSort.dir === "asc" ? "desc" : "asc";
        } else {
          browseSort.key = key;
          browseSort.dir = key === "name" || key === "camera" || key === "place" ? "asc" : "desc";
        }
        renderBrowseResults();
      });
    });
  }
  syncBrowseFrameVisibility();
  updateBrowseSelectionCount();
  renderBrowseTimeline(items);
}

function clearBrowseTreeHighlights() {
  $("#browse-groups")?.querySelectorAll(".browse-item.selected").forEach((i) => {
    i.classList.remove("selected");
  });
}

function clearBrowseSelection() {
  clearBrowseTreeHighlights();
  browseTreeAnchor = null;
}

function selectBrowseItem(item) {
  clearBrowseTreeHighlights();
  if (item) item.classList.add("selected");
  browseTreeAnchor = item || null;
}

function visibleBrowseTreeItems() {
  const sidebar = $("#browse-groups");
  if (!sidebar) return [];
  return [...sidebar.querySelectorAll(".browse-item")].filter((el) => {
    let p = el.parentElement;
    while (p && p !== sidebar) {
      if (p.classList.contains("browse-subtree") && p.hidden) return false;
      p = p.parentElement;
    }
    return true;
  });
}

function applyBrowseTreeSelection(item, e) {
  const items = visibleBrowseTreeItems();
  const idx = items.indexOf(item);
  if (idx < 0) return;
  const additive = e.ctrlKey || e.metaKey;
  const range = e.shiftKey;

  if (range && browseTreeAnchor) {
    const anchor = items.indexOf(browseTreeAnchor);
    if (anchor >= 0) {
      const lo = Math.min(anchor, idx);
      const hi = Math.max(anchor, idx);
      if (!additive) clearBrowseTreeHighlights();
      for (let i = lo; i <= hi; i++) items[i].classList.add("selected");
      return;
    }
  }

  if (additive) {
    item.classList.toggle("selected");
    browseTreeAnchor = item;
  } else {
    clearBrowseTreeHighlights();
    item.classList.add("selected");
    browseTreeAnchor = item;
  }
}

function browseViewForGroupBy(groupBy) {
  if (groupBy === "folder") return "volumes";
  if (groupBy === "collection") return "collections";
  if (groupBy === "person") return "people";
  if (groupBy === "date") return "dates";
  if (groupBy === "location") return "places";
  if (groupBy === "camera") return "cameras";
  return groupBy;
}

function browseGroupByForBranch(branchId) {
  if (branchId === "volumes") return "folder";
  if (branchId === "collections") return "collection";
  if (branchId === "people") return "person";
  if (branchId === "dates") return "date";
  if (branchId === "places") return "location";
  if (branchId === "cameras") return "camera";
  return branchId;
}

function clearBrowseGallery() {
  browseActiveKey = null;
  browseActiveKeys = [];
  browseSelectionByGroup = {};
  saveBrowseSelectionPref();
  browseItems = [];
  browseItemsFull = [];
  browseSearchActive = false;
  browseSelected.clear();
  browseAnchorKey = null;
  const results = $("#browse-results");
  if (results) results.innerHTML = "";
  updateBrowseSelectionCount();
}

function showLabeledImagesChecked() {
  return isPressOn($("#browse-show-nested"));
}

/** Nested-folder / date gallery scope (same checkbox as labeled people/places). */
function showSubfoldersChecked() {
  return showLabeledImagesChecked();
}

/** Cardinal toggle label: labeled (People/Places) vs nested (folders/dates). */
function syncBrowseNestedOptLabel() {
  const btn = $("#browse-show-nested");
  if (!btn) return;
  if (browseView === "people") {
    btn.textContent = "Show labeled people";
    btn.title = "Show identified people or unlabeled faces";
  } else {
    btn.textContent = "Show nested images";
    btn.title = "Include photos from sub-classified branches";
  }
}

function cloneBrowseSelection(sel = browseSelectionByGroup) {
  const out = {};
  for (const [gb, keys] of Object.entries(sel || {})) {
    if (Array.isArray(keys)) out[gb] = [...keys];
  }
  return out;
}

function browseSelectionEquals(a, b) {
  const na = cloneBrowseSelection(a);
  const nb = cloneBrowseSelection(b);
  const keys = new Set([...Object.keys(na), ...Object.keys(nb)]);
  for (const k of keys) {
    const va = [...(na[k] || [])].map(String).sort();
    const vb = [...(nb[k] || [])].map(String).sort();
    if (va.length !== vb.length) return false;
    if (va.some((x, i) => x !== vb[i])) return false;
  }
  return true;
}

function browseSelectionParticipates(sel = browseSelectionByGroup) {
  return Object.values(sel || {}).some((keys) => Array.isArray(keys));
}

function syncBrowseActiveFromSelection() {
  const gb = browseGroupBy();
  const keys = browseSelectionByGroup[gb] || [];
  browseActiveKey = keys[0] ?? null;
  browseActiveKeys = keys.length > 1 ? [...keys] : [];
}

/** Collect tree selection: OR keys within each groupBy; empty array = ineligible branch. */
function collectBrowseSelectionFromDom() {
  const selected = [...($("#browse-groups")?.querySelectorAll(".browse-item.selected") || [])];
  const showSub = showSubfoldersChecked();
  /** @type {Record<string, string[]>} */
  const selectionByGroup = {};
  /** @type {Record<string, boolean>} */
  const hadSelection = {};

  const pathItems = selected.filter((el) => el.dataset.path != null && el.dataset.path !== "");
  for (const el of pathItems) {
    const gb = el.dataset.groupBy;
    if (!gb) continue;
    hadSelection[gb] = true;
    // Always include the selected node; include_subfolders controls whether
    // child-folder / nested photos are mixed into the gallery.
    if (!selectionByGroup[gb]) selectionByGroup[gb] = [];
    if (!selectionByGroup[gb].includes(el.dataset.path)) {
      selectionByGroup[gb].push(el.dataset.path);
    }
  }

  for (const el of selected) {
    if (!el.classList.contains("browse-branch")) continue;
    const branchId = el.dataset.branch;
    if (!branchId) continue;
    const gb = browseGroupByForBranch(branchId);
    hadSelection[gb] = true;
    if (selectionByGroup[gb]?.length) continue;
    // People / Places: checkbox filters labeled vs unlabeled, not nested scope —
    // always load the filtered “all” set when the branch root is selected.
    if (branchId === "people" || branchId === "places" || showSub) {
      selectionByGroup[gb] = ["all"];
    }
  }

  for (const gb of Object.keys(selectionByGroup)) {
    if (selectionByGroup[gb].includes("all")) selectionByGroup[gb] = ["all"];
  }
  for (const gb of Object.keys(hadSelection)) {
    if (!selectionByGroup[gb]) selectionByGroup[gb] = [];
  }

  return { selected, pathItems, selectionByGroup };
}

async function activateSelectedBrowseTree(clickedItem) {
  const { selected, selectionByGroup } = collectBrowseSelectionFromDom();
  if (!selected.length) {
    clearBrowseGallery();
    refreshScopedWorkspaceViews();
    return;
  }

  for (const el of selected) {
    if (!el.classList.contains("browse-branch")) continue;
    const subtree = el.nextElementSibling;
    if (subtree?.classList.contains("browse-subtree") && subtree.hidden) {
      await toggleBrowseBranch(el, true);
    }
  }

  const focusGroupBy =
    clickedItem?.dataset.groupBy ||
    (clickedItem?.dataset.branch
      ? browseGroupByForBranch(clickedItem.dataset.branch)
      : null) ||
    Object.keys(selectionByGroup)[0] ||
    null;
  if (focusGroupBy) {
    const keys = selectionByGroup[focusGroupBy] || [];
    setBrowseContext(browseViewForGroupBy(focusGroupBy), keys[0] || null);
  }

  if (!browseSelectionParticipates(selectionByGroup)) {
    clearBrowseGallery();
    if (focusGroupBy) setBrowseContext(browseViewForGroupBy(focusGroupBy));
    refreshScopedWorkspaceViews();
    return;
  }

  const emptyBranch = Object.entries(selectionByGroup).find(([, keys]) => Array.isArray(keys) && !keys.length);
  if (emptyBranch) {
    browseSelectionByGroup = cloneBrowseSelection(selectionByGroup);
    syncBrowseActiveFromSelection();
    saveBrowseSelectionPref();
    browseItems = [];
    browseItemsFull = [];
    browseSearchActive = false;
    browseSelected.clear();
    browseAnchorKey = null;
    const results = $("#browse-results");
    if (results) {
      results.innerHTML = `<p class="muted">Turn on “Show labeled images” to include nested items.</p>`;
    }
    updateBrowseSelectionCount();
    refreshScopedWorkspaceViews();
    return;
  }

  if (browseSelectionEquals(browseSelectionByGroup, selectionByGroup)) return;

  await loadBrowsePhotosFromSelection(selectionByGroup);
  refreshScopedWorkspaceViews();
}

function swallowNextClick() {
  const swallowClick = (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
  };
  document.addEventListener("click", swallowClick, true);
  setTimeout(() => document.removeEventListener("click", swallowClick, true), 50);
}

function bindBrowseTreeRowClick(item) {
  item.addEventListener("mousedown", (e) => {
    if (e.shiftKey) e.preventDefault();
  });
  if (isDraggableTreeBranch(item)) {
    item.draggable = true;
    item.addEventListener("dragstart", (e) => {
      if (e.target.closest(".tree-label-edit, .browse-twistie:not(.spacer), button")) {
        e.preventDefault();
        return;
      }
      const gb = item.dataset.groupBy;
      const path = item.dataset.path;
      const personId = gb === "person" ? Number(path) : null;
      photoTreeDrag = {
        photos: [],
        faceIds: [],
        sourceItem: item,
        sourceGroupBy: gb,
        sourcePath: path,
        sourceLabel: treeItemLabel(item),
        sourcePlaceLabel:
          gb === "location"
            ? placeLabelFromLocationPath(path, item.dataset.kind || "")
            : null,
        sourceCount: treeItemCount(item),
        sourcePersonId: Number.isFinite(personId) && personId > 0 ? personId : null,
        sourceLabeled: item.querySelector(".person-label")?.dataset.labeled === "1",
      };
      e.dataTransfer.effectAllowed = gb === "collection" ? "move" : "copy";
      e.dataTransfer.setData("text/plain", "fotoman-branch");
      document.body.classList.add("is-photo-dragging");
    });
    item.addEventListener("dragend", () => {
      clearPhotoTreeDrag();
      swallowNextClick();
    });
  }
  item.addEventListener("click", async (e) => {
    if (e.target.closest(".browse-twistie:not(.spacer)")) return;
    if (e.target.closest(".tree-label-edit")) return;
    if (e.target.closest("button")) return;
    e.stopPropagation();
    applyBrowseTreeSelection(item, e);
    await activateSelectedBrowseTree(item);
  });
}

function browseTwistieHtml(hasChildren, open = false) {
  if (!hasChildren) {
    return `<span class="browse-twistie spacer" aria-hidden="true"></span>`;
  }
  return `<button type="button" class="browse-twistie" aria-label="${open ? "Collapse" : "Expand"}" aria-expanded="${open ? "true" : "false"}">${open ? "▾" : "▸"}</button>`;
}

function setBrowseTwistie(item, open) {
  const tw = item?.querySelector?.(".browse-twistie:not(.spacer)");
  if (!tw) return;
  tw.textContent = open ? "▾" : "▸";
  tw.setAttribute("aria-expanded", open ? "true" : "false");
  tw.setAttribute("aria-label", open ? "Collapse" : "Expand");
}

function toggleBrowseSubtree(item, forceOpen = null) {
  const sub = item.nextElementSibling;
  if (!sub || !sub.classList.contains("browse-subtree")) return false;
  const willOpen = forceOpen == null ? sub.hidden : forceOpen;
  sub.hidden = !willOpen;
  setBrowseTwistie(item, willOpen);
  return willOpen;
}

function bindBrowseTwistie(item, onToggle) {
  const tw = item.querySelector(".browse-twistie:not(.spacer)");
  if (!tw) return;
  tw.addEventListener("click", (e) => {
    e.stopPropagation();
    onToggle(e);
  });
}

function setBrowseContext(view, key = null) {
  browseView = view;
  browseActiveKey = key;
  const frameOpt = $("#browse-show-face");
  if (frameOpt) frameOpt.hidden = view !== "people";
  syncBrowseFrameVisibility();
  syncBrowseNestedOptLabel();
  saveBrowseViewPref();
}

function browseCountLabel(node) {
  const total = Number(node?.count) || 0;
  if (node?.direct_count == null) return String(total);
  const direct = Number(node.direct_count) || 0;
  // Always show direct/total when the node nests children (or counts differ).
  if (node.children?.length || direct !== total) return `${direct}/${total}`;
  return String(total);
}

function browseTreeOpenKey(item) {
  if (!item) return null;
  if (item.dataset.branch) return `branch:${item.dataset.branch}`;
  const gb = item.dataset.groupBy || "";
  const path = item.dataset.path;
  if (path == null || path === "") return null;
  return `${gb}:${path}`;
}

/** Reverse year/month/day rows in place (each item + its subtree stay paired). */
function reverseBrowseTreeOrder(container) {
  if (!container) return;
  const nodes = [...container.children];
  const groups = [];
  for (let i = 0; i < nodes.length; ) {
    const el = nodes[i];
    if (!el.classList.contains("browse-item")) {
      i += 1;
      continue;
    }
    const next = nodes[i + 1];
    if (next?.classList.contains("browse-subtree")) {
      reverseBrowseTreeOrder(next);
      groups.push([el, next]);
      i += 2;
    } else {
      groups.push([el]);
      i += 1;
    }
  }
  for (let i = groups.length - 1; i >= 0; i--) {
    for (const el of groups[i]) container.appendChild(el);
  }
}

function collectOpenBrowseKeys(root) {
  const keys = new Set();
  if (!root) return keys;
  root.querySelectorAll(".browse-item.has-children").forEach((item) => {
    const sub = item.nextElementSibling;
    if (!sub?.classList.contains("browse-subtree") || sub.hidden) return;
    const key = browseTreeOpenKey(item);
    if (key) keys.add(key);
  });
  return keys;
}

function renderBrowseTree(nodes, container, groupBy, depth = 0, openKeys = null) {
  nodes.forEach((node) => {
    const isFile = !!node.is_file;
    const hasChildren = !!node.children;
    const kind = node.kind || (String(node.path || "").startsWith("region::") ? "region" : "");
    const open = !!(hasChildren && openKeys?.has(`${groupBy}:${node.path}`));
    const item = document.createElement("div");
    item.className =
      "browse-item" +
      (hasChildren ? " has-children" : "") +
      (isFile ? " is-file" : "") +
      (kind === "region" ? " is-region" : "") +
      (node.offline ? " is-offline" : "");
    item.dataset.path = node.path;
    item.dataset.groupBy = groupBy;
    if (kind) item.dataset.kind = kind;
    if (node.offline) item.dataset.offline = "1";
    item.style.paddingLeft = `${depth * 0.8 + 0.4}rem`;
    const countHtml = isFile ? "" : `<span class="browse-item-count">${browseCountLabel(node)}</span>`;
    const isArchiveRoot = /:\s*$/.test(String(node.path || ""));
    const offlineHtml =
      node.offline && isArchiveRoot
        ? `<span class="browse-offline-flag" title="Volume is unmounted">offline</span>`
        : "";
    item.innerHTML = `${browseTwistieHtml(hasChildren, open)}<span class="browse-item-name">${node.name}</span>${offlineHtml}${countHtml}`;
    bindBrowseTwistie(item, () => toggleBrowseSubtree(item));
    bindBrowseTreeRowClick(item);
    container.appendChild(item);
    if (hasChildren) {
      const subtree = document.createElement("div");
      subtree.className = "browse-subtree";
      subtree.hidden = !open;
      renderBrowseTree(node.children, subtree, groupBy, depth + 1, openKeys);
      container.appendChild(subtree);
    }
  });
}

function selectBrowseTreePath(path) {
  const sidebar = $("#browse-groups");
  if (!sidebar || path == null || path === "") return null;
  const item = sidebar.querySelector(`.browse-item[data-path="${CSS.escape(String(path))}"]`);
  if (!item) return null;
  selectBrowseItem(item);
  let el = item.parentElement;
  while (el && el !== sidebar) {
    if (el.classList.contains("browse-subtree")) {
      el.hidden = false;
      const parentItem = el.previousElementSibling;
      if (parentItem) {
        setBrowseTwistie(parentItem, true);
        if (parentItem.classList.contains("browse-branch")) {
          parentItem.dataset.loaded = "1";
        }
      }
    }
    el = el.parentElement;
  }
  return item;
}

function beginInlineTreeRename(span, onCommit, { allowEmpty = false } = {}) {
  if (!span || span.tagName === "INPUT") return;
  const prev = (span.dataset.displayName || span.textContent || "").trim();
  const input = document.createElement("input");
  input.type = "text";
  input.className = "tree-label-edit";
  input.value = prev;
  input.defaultValue = prev;
  input.setAttribute("aria-label", span.getAttribute("aria-label") || "Rename");
  for (const attr of span.attributes) {
    if (attr.name.startsWith("data-")) input.setAttribute(attr.name, attr.value);
  }
  span.replaceWith(input);
  input.focus();
  input.select();

  let done = false;
  const restore = (label) => {
    if (!input.isConnected) return;
    const text = label ?? prev;
    span.textContent = text;
    if (span.dataset.displayName != null) span.dataset.displayName = text;
    input.replaceWith(span);
  };
  const finish = async (commit) => {
    if (done) return;
    done = true;
    const next = input.value.trim();
    if (!commit || next === prev || (!allowEmpty && !next)) {
      restore(prev);
      return;
    }
    try {
      const result = await onCommit(next, { input, prev });
      if (result?.replaced || !input.isConnected) return;
      const label = result?.label ?? next;
      if (result?.labeled != null) span.dataset.labeled = result.labeled ? "1" : "0";
      restore(label);
    } catch (err) {
      restore(prev);
      alert(err.message || "Rename failed");
    }
  };
  input.addEventListener("click", (e) => e.stopPropagation());
  input.addEventListener("mousedown", (e) => e.stopPropagation());
  input.addEventListener("dblclick", (e) => e.stopPropagation());
  input.addEventListener("blur", () => finish(true));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      input.blur();
    } else if (e.key === "Escape") {
      e.preventDefault();
      finish(false);
    }
  });
}

let treeContextMenu = null;
let treeContextMenuCloser = null;

function closeTreeContextMenu() {
  treeContextMenu?.remove();
  treeContextMenu = null;
  if (treeContextMenuCloser) {
    document.removeEventListener("click", treeContextMenuCloser, true);
    document.removeEventListener("contextmenu", treeContextMenuCloser, true);
    document.removeEventListener("keydown", treeContextMenuCloser, true);
    treeContextMenuCloser = null;
  }
}

function openTreeContextMenu(e, items) {
  e.preventDefault();
  e.stopPropagation();
  closeTreeContextMenu();
  const menu = document.createElement("div");
  menu.className = "tree-context-menu";
  menu.setAttribute("role", "menu");
  for (const it of items) {
    if (it === "sep") {
      menu.appendChild(document.createElement("hr"));
      continue;
    }
    const btn = document.createElement("button");
    btn.type = "button";
    btn.setAttribute("role", "menuitem");
    btn.textContent = it.label;
    if (it.danger) btn.classList.add("is-danger");
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      closeTreeContextMenu();
      it.action();
    });
    menu.appendChild(btn);
  }
  document.body.appendChild(menu);
  const pad = 8;
  let x = e.clientX;
  let y = e.clientY;
  const rect = menu.getBoundingClientRect();
  if (x + rect.width > window.innerWidth - pad) x = Math.max(pad, window.innerWidth - rect.width - pad);
  if (y + rect.height > window.innerHeight - pad) y = Math.max(pad, window.innerHeight - rect.height - pad);
  menu.style.left = `${Math.max(pad, x)}px`;
  menu.style.top = `${Math.max(pad, y)}px`;
  treeContextMenu = menu;
  treeContextMenuCloser = (ev) => {
    if (ev.type === "keydown" && ev.key !== "Escape") return;
    if (ev.type !== "keydown" && menu.contains(ev.target)) return;
    closeTreeContextMenu();
  };
  setTimeout(() => {
    document.addEventListener("click", treeContextMenuCloser, true);
    document.addEventListener("contextmenu", treeContextMenuCloser, true);
    document.addEventListener("keydown", treeContextMenuCloser, true);
  }, 0);
}

function bindPersonLabel(span) {
  span.addEventListener("dblclick", (e) => {
    e.preventDefault();
    e.stopPropagation();
    beginInlineTreeRename(
      span,
      async (next, { input, prev }) => {
        const personId = input.dataset.person;
        const wasLabeled = input.dataset.labeled === "1";
        const nextLabeled = isPersonNameLabeled(next);
        const photoCount = Number(input.dataset.photoCount) || 0;

        if (!wasLabeled && nextLabeled && photoCount > 1) {
          const ok = confirm(
            `Assign “${next}” to all ${photoCount} photos in this group?\n\nTo label only some photos, select them and use Edit selection.`
          );
          if (!ok) return { label: prev };
        }

        const patch = async (merge) => {
          const res = await fetch(`/api/people/${personId}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: next || null, merge }),
          });
          if (res.status === 409) {
            const body = await res.json();
            const err = new Error("name_conflict");
            err.conflict = true;
            err.existing = body.detail?.existing;
            throw err;
          }
          if (!res.ok) {
            const text = await res.text();
            throw new Error(text || res.statusText);
          }
          return res.json();
        };

        try {
          const updated = await patch(false);
          span.dataset.displayName = updated.display_name;
          span.dataset.labeled = updated.labeled ? "1" : "0";
          if (wasLabeled !== !!updated.labeled) {
            await fillPeopleBranch();
            if (browseSelectionByGroup.person) {
              const surviving = await restoreBrowseTreeSelection(cloneBrowseSelection());
              if (Object.values(surviving).some((k) => k?.length)) {
                await loadBrowsePhotosFromSelection(surviving);
              } else {
                clearBrowseGallery();
              }
            }
            return { replaced: true };
          }
          return { label: updated.display_name, labeled: updated.labeled };
        } catch (err) {
          if (err.conflict && err.existing) {
            const ex = err.existing;
            const label = ex.display_name || next;
            const ok = confirm(
              `"${label}" already exists (${ex.photo_count} photos). Merge this person into them?`
            );
            if (!ok) return { label: prev };
            const updated = await patch(true);
            await fillPeopleBranch();
            const kept = $("#browse-groups")?.querySelector(
              `.person-label[data-person="${updated.id}"]`
            );
            const item = kept?.closest(".browse-item");
            if (item) selectBrowseItem(item);
            setBrowseContext("people", String(updated.id));
            await loadBrowsePhotos("person", String(updated.id));
            return { replaced: true };
          }
          throw err;
        }
      },
      { allowEmpty: true }
    );
  });
}

function peopleBranchItem() {
  return $("#browse-groups")?.querySelector('.browse-branch[data-branch="people"]');
}

function peopleBranchContainer() {
  const item = peopleBranchItem();
  const next = item?.nextElementSibling;
  if (next?.classList.contains("browse-subtree")) return next;
  return $("#browse-groups")?.querySelector('.browse-subtree[data-branch="people"]');
}

async function refreshPeopleBranchIfLoaded() {
  const item = peopleBranchItem();
  if (item?.dataset.loaded !== "1") return;
  await fillPeopleBranch();
}

function locationBranchContainer() {
  return $("#browse-groups")?.querySelector('.browse-subtree[data-branch="places"]');
}

function escHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/"/g, "&quot;");
}

function activeCollectionId() {
  const keys = browseSelectionByGroup.collection;
  if (!keys?.length) return null;
  const id = Number(keys[0]);
  return Number.isFinite(id) ? id : null;
}

function collectionTreeRootId(collectionId) {
  const id = Number(collectionId);
  if (!Number.isFinite(id) || id <= 0) return null;
  let item = document.querySelector(`.browse-collection[data-path="${id}"]`);
  if (!item) return id;
  while (true) {
    const sub = item.parentElement;
    if (!sub?.classList.contains("browse-subtree")) break;
    const parent = sub.previousElementSibling;
    if (!parent?.classList.contains("browse-collection")) break;
    item = parent;
  }
  const root = Number(item.dataset.path);
  return Number.isFinite(root) ? root : id;
}

function sameCollectionTree(aId, bId) {
  const a = collectionTreeRootId(aId);
  const b = collectionTreeRootId(bId);
  return a != null && b != null && a === b;
}

function activeCollectionFlags() {
  const id = activeCollectionId();
  if (id == null) return { linked: false, sourceSubdir: false };
  const item = document.querySelector(`.browse-collection[data-path="${id}"]`);
  return {
    linked: item?.dataset.linked === "1",
    sourceSubdir: item?.dataset.sourceSubdir === "1",
  };
}

function flattenCollections(folders, prefix = "") {
  const out = [];
  for (const f of folders || []) {
    const label = prefix ? `${prefix} / ${f.name}` : f.name;
    out.push({ ...f, pathLabel: label });
    if (f.children?.length) out.push(...flattenCollections(f.children, label));
  }
  return out;
}

async function exportCollectionAsArchive(folder) {
  if (!folder?.id) return;
  const countEl = $("#browse-gallery-count");
  const prevCount = countEl?.textContent || "";
  try {
    const picked = await api(
      `/api/pick-directory?title=${encodeURIComponent("Export collection")}`,
      { method: "POST" }
    );
    if (picked.cancelled || !picked.path) return;
    if (countEl) countEl.textContent = `Exporting “${folder.name}”…`;
    const culledQ = removeQuery();
    const res = await api(
      `/api/collections/${folder.id}/export${culledQ ? `?${culledQ.slice(1)}` : ""}`,
      {
        method: "POST",
        body: JSON.stringify({ dest: picked.path, write_mtime: true }),
      }
    );
    const parts = [`Exported ${res.exported || res.copied || 0}`];
    if (res.skipped) parts.push(`${res.skipped} already there`);
    if (res.failed) parts.push(`${res.failed} failed`);
    if (res.pending) parts.push(`${res.pending} still pending`);
    if (countEl) countEl.textContent = `${parts.join("; ")} → ${res.dest || picked.path}`;
    await loadSources();
    await refreshExpandedBranches(["collections", "volumes"]);
  } catch (err) {
    alert(err.message || "Export failed");
    if (countEl) countEl.textContent = prevCount;
  }
}

async function deleteCollection(folder) {
  const kind = folder.parent_id != null ? "subfolder" : "collection";
  if (!confirm(`Delete ${kind} “${folder.name}” and its subfolders? Photos stay in the library.`)) return;
  try {
    await api(`/api/collections/${folder.id}`, { method: "DELETE" });
    const vkeys = browseSelectionByGroup.collection || [];
    if (vkeys.map(String).includes(String(folder.id))) {
      clearBrowseGallery();
    }
    await refreshExpandedBranches(["collections"]);
  } catch (err) {
    alert(err.message || "Delete failed");
  }
}

function bindCollectionItemMenu(item, folder) {
  item.addEventListener("contextmenu", (e) => {
    if (e.target.closest(".tree-label-edit")) return;
    if (!item.classList.contains("selected")) {
      applyBrowseTreeSelection(item, { ctrlKey: false, metaKey: false, shiftKey: false });
      activateSelectedBrowseTree(item);
    }
    const kind = folder.parent_id != null ? "subfolder" : "collection";
    const linked = !!folder.linked;
    const sourceSubdir = !!folder.source_subdir;
    const items = [];
    if (!linked) {
      items.push({ label: "Add subfolder", action: () => promptNewCollection(folder.id) });
    }
    items.push({ label: "Export", action: () => exportCollectionAsArchive(folder) });
    if (folder.share_url || folder.share_code) {
      items.push({
        label: "Fotomob link",
        action: () =>
          openShareInfoDialog({
            name: folder.name,
            url: folder.share_url || folder.share_code || "",
          }),
      });
    }
    if (!sourceSubdir) {
      items.push("sep");
      items.push({
        label: kind === "subfolder" ? "Delete subfolder" : "Delete collection",
        danger: true,
        action: () => deleteCollection(folder),
      });
    }
    openTreeContextMenu(e, items);
  });
}

function renderCollections(folders, container, depth = 1, openKeys = null) {
  if (!folders?.length) return;
  folders.forEach((f) => {
    const kids = f.children || [];
    const hasChildren = kids.length > 0;
    const open = !!(hasChildren && openKeys?.has(`collection:${f.id}`));
    const linked = !!f.linked;
    const sourceSubdir = !!f.source_subdir;
    const linkedRoot = linked && !sourceSubdir;
    const item = document.createElement("div");
    item.className =
      "browse-item browse-collection" +
      (hasChildren ? " has-children" : "") +
      (linked ? " is-linked" : "") +
      (linkedRoot ? " is-linked-root" : "");
    item.dataset.path = String(f.id);
    item.dataset.groupBy = "collection";
    item.dataset.linked = linked ? "1" : "0";
    item.dataset.sourceSubdir = sourceSubdir ? "1" : "0";
    item.style.paddingLeft = `${depth * 0.8 + 0.4}rem`;
    item.innerHTML = `${browseTwistieHtml(hasChildren, open)}<span class="browse-item-name"${
      sourceSubdir
        ? ' title="Linked child — edit in the source tree"'
        : ' title="Double-click to rename"'
    }>${escHtml(f.name)}</span>
      <span class="browse-item-count">${browseCountLabel(f)}</span>`;
    const nameEl = item.querySelector(".browse-item-name");
    if (!sourceSubdir) {
      nameEl?.addEventListener("dblclick", (e) => {
        e.preventDefault();
        e.stopPropagation();
        beginInlineTreeRename(nameEl, async (label) => {
          await api(`/api/collections/${f.id}`, {
            method: "PATCH",
            body: JSON.stringify({ name: label }),
          });
          await refreshExpandedBranches(["collections"]);
          return { replaced: true };
        });
      });
    }
    bindCollectionItemMenu(item, f);
    bindBrowseTwistie(item, () => toggleBrowseSubtree(item));
    bindBrowseTreeRowClick(item);
    container.appendChild(item);
    if (hasChildren) {
      const subtree = document.createElement("div");
      subtree.className = "browse-subtree";
      subtree.hidden = !open;
      renderCollections(kids, subtree, depth + 1, openKeys);
      container.appendChild(subtree);
    }
  });
}

async function fillPeopleBranch(container = peopleBranchContainer()) {
  if (!container) return;
  // Branch header is a sibling of the subtree — never replace "People" with person rows.
  if (container.classList.contains("browse-branch")) {
    const sub = container.nextElementSibling;
    if (!sub?.classList.contains("browse-subtree")) return;
    container = sub;
  }
  const labeled = showLabeledImagesChecked();
  const people = await api(`/api/browse/people?labeled=${labeled ? "true" : "false"}`);
  container.innerHTML = "";
  if (!people.length) {
    container.innerHTML = `<p class="muted" style="padding:0.35rem 0.6rem">${
      labeled
        ? "No labeled people yet."
        : "No unlabeled or Unknown people."
    }</p>`;
    return;
  }
  const esc = (s) =>
    String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/"/g, "&quot;");
  people.forEach((p) => {
    const item = document.createElement("div");
    item.className = "browse-item browse-person";
    item.dataset.path = String(p.id);
    item.dataset.groupBy = "person";
    item.style.paddingLeft = "1.2rem";
    const faceSrc = p.cover_face_id ? `/api/faces/${p.cover_face_id}/thumb` : "";
    item.innerHTML = `
      ${browseTwistieHtml(false)}
      ${faceSrc ? `<img class="person-face" src="${faceSrc}" alt="" loading="lazy" draggable="false" />` : `<span class="person-face person-face-empty"></span>`}
      <span class="person-label browse-item-name" data-person="${p.id}" data-labeled="${p.labeled ? "1" : "0"}"
        data-display-name="${esc(p.display_name)}" data-photo-count="${p.photo_count}"
        title="Double-click to rename" aria-label="Person name">${esc(p.display_name)}</span>
      <span class="browse-item-count">${p.photo_count}</span>`;
    bindBrowseTreeRowClick(item);
    bindPersonLabel(item.querySelector(".person-label"));
    container.appendChild(item);
  });
}

async function fillPlacesBranch(container = locationBranchContainer(), openKeys = null) {
  if (!container) return;
  const preserved = openKeys || collectOpenBrowseKeys(container);
  const culledQ = removeQuery();
  const tree = await api(`/api/browse/location-tree${culledQ ? `?${culledQ.slice(1)}` : ""}`);
  container.innerHTML = "";
  if (!tree.length) {
    container.innerHTML = `<p class="muted" style="padding:0.35rem 0.6rem">No places. Index GPS or folder-named places first.</p>`;
    return;
  }
  renderBrowseTree(tree, container, "location", 1, preserved);
}

async function fillBranchChildren(branchId, container, openKeys = null) {
  const preserved = openKeys || collectOpenBrowseKeys(container);
  if (!container.querySelector(".browse-item")) {
    container.innerHTML = loadingMarkup("Loading", "sm");
  }
  try {
    if (branchId === "collections") {
      const culledQ = removeQuery();
      const folders = await api(`/api/collections${culledQ ? `?${culledQ.slice(1)}` : ""}`);
      container.innerHTML = "";
      if (!(folders || []).length) {
        container.innerHTML = `<p class="muted" style="padding:0.35rem 0.6rem">No collections yet. Right-click Collections to create one.</p>`;
        return;
      }
      renderCollections(folders || [], container, 1, preserved);
    } else if (
      branchId === "volumes"
    ) {
      const culledQ = removeQuery();
      const tree = await api(`/api/browse/tree?include_files=false${culledQ}`);
      container.innerHTML = "";
      if (!(tree || []).length) {
        container.innerHTML = `<p class="muted" style="padding:0.35rem 0.6rem">No volumes or photos found.</p>`;
        return;
      }
      renderBrowseTree(tree, container, "folder", 1, preserved);
    } else if (branchId === "dates") {
      const tree = await api(`/api/browse/date-tree?${removeQuery().slice(1)}`);
      container.innerHTML = "";
      if (!tree.length) {
        container.innerHTML = `<p class="muted" style="padding:0.35rem 0.6rem">No dated photos found.</p>`;
        return;
      }
      renderBrowseTree(tree, container, "date", 1, preserved);
    } else if (branchId === "places") {
      await fillPlacesBranch(container, preserved);
    } else if (branchId === "people") {
      await fillPeopleBranch(container);
    } else if (branchId === "cameras") {
      const groups = await api(`/api/browse/groups?group_by=camera${removeQuery()}`);
      container.innerHTML = "";
      if (!groups.length) {
        container.innerHTML = `<p class="muted" style="padding:0.35rem 0.6rem">No camera data.</p>`;
        return;
      }
      groups.forEach((g) => {
        const item = document.createElement("div");
        item.className = "browse-item";
        item.dataset.path = g.key;
        item.dataset.groupBy = "camera";
        item.style.paddingLeft = "1.2rem";
        item.innerHTML = `${browseTwistieHtml(false)}<span class="browse-item-name">${g.key}</span>
          <span class="browse-item-count">${g.count}</span>`;
        bindBrowseTreeRowClick(item);
        container.appendChild(item);
      });
    }
  } catch (err) {
    container.innerHTML = `<p class="muted" style="padding:0.35rem 0.6rem">${err.message}</p>`;
  }
}

async function toggleBrowseBranch(branchEl, forceOpen = null, openKeys = null) {
  const subtree = branchEl.nextElementSibling;
  const branchId = branchEl.dataset.branch;
  const open = forceOpen == null ? subtree.hidden : forceOpen;
  if (!open) {
    subtree.hidden = true;
    setBrowseTwistie(branchEl, false);
    return;
  }
  subtree.hidden = false;
  setBrowseTwistie(branchEl, true);
  if (branchEl.dataset.loaded !== "1") {
    await fillBranchChildren(branchId, subtree, openKeys);
    branchEl.dataset.loaded = "1";
  }
}

async function refreshExpandedBranches(branchIds = null) {
  const sidebar = $("#browse-groups");
  if (!sidebar) return;
  const roots = [...sidebar.querySelectorAll(".browse-branch")];
  for (const branchEl of roots) {
    const id = branchEl.dataset.branch;
    if (branchIds && !branchIds.includes(id)) continue;
    if (branchEl.dataset.loaded !== "1") continue;
    const subtree = branchEl.nextElementSibling;
    if (!subtree || subtree.hidden) {
      // Stale cache: refetch on next expand.
      branchEl.dataset.loaded = "0";
      continue;
    }
    await fillBranchChildren(id, subtree);
  }
}

async function promptNewCollection(parentId = null) {
  const next = prompt(parentId == null ? "New collection name" : "New subfolder name", "");
  if (next == null) return;
  const label = next.trim();
  if (!label) return;
  try {
    await api("/api/collections", {
      method: "POST",
      body: JSON.stringify({ name: label, parent_id: parentId }),
    });
    const foldersRoot = $("#browse-groups")?.querySelector(
      '.browse-branch[data-branch="collections"]'
    );
    if (parentId == null && foldersRoot) {
      foldersRoot.dataset.loaded = "0";
      await toggleBrowseBranch(foldersRoot, true);
    } else {
      await refreshExpandedBranches(["collections"]);
    }
  } catch (err) {
    alert(err.message || "Create failed");
  }
}

function createBrowseBranch(branch) {
  const item = document.createElement("div");
  item.className = "browse-item browse-branch has-children";
  item.dataset.branch = branch.id;
  item.dataset.label = branch.label;
  item.dataset.loaded = "0";
  item.innerHTML = `${browseTwistieHtml(true)}<span class="browse-item-name">${branch.label}</span>`;
  const subtree = document.createElement("div");
  subtree.className = "browse-subtree";
  subtree.dataset.branch = branch.id;
  subtree.hidden = true;

  bindBrowseTwistie(item, async () => {
    await toggleBrowseBranch(item);
  });
  bindBrowseTreeRowClick(item);
  if (branch.id === "dates") {
    item.addEventListener("dblclick", (e) => {
      e.preventDefault();
      e.stopPropagation();
      dateTreeOrder = dateTreeOrder === "newest" ? "oldest" : "newest";
      const subtree = item.nextElementSibling;
      if (subtree?.classList.contains("browse-subtree")) reverseBrowseTreeOrder(subtree);
      api("/api/settings", {
        method: "POST",
        body: JSON.stringify({ date_tree_order: dateTreeOrder }),
      }).catch(() => {});
    });
  }
  if (branch.id === "collections") {
    item.addEventListener("contextmenu", (e) => {
      openTreeContextMenu(e, [
        { label: "Add collection", action: () => promptNewCollection(null) },
      ]);
    });
  }

  return { item, subtree };
}

const BROWSE_TREE_BRANCHES = [
  { id: "volumes", label: "Volumes" },
  { id: "collections", label: "Collections" },
  { id: "dates", label: "Dates" },
  { id: "places", label: "Places" },
  { id: "people", label: "People" },
  { id: "cameras", label: "Cameras" },
];

async function pollFacesStatus() {
  try {
    const st = await api("/api/faces/status");
    lastFacesStatus = st;
    const bar = $("#index-status");
    if (bar) bar.textContent = indexStatusText(lastIndexStatus, lastSources, st);
    applyIndexStatusToSources(lastIndexStatus, lastSources, st);
    if (!st.running) await refreshPeopleBranchIfLoaded();
  } catch {
    /* ignore */
  }
}

async function loadBrowseView(opts = {}) {
  const sidebar = $("#browse-groups");
  const results = $("#browse-results");
  const frameOpt = $("#browse-show-face");
  sidebar.innerHTML = "";
  browseItems = [];
  browseItemsFull = [];
  browseSearchActive = false;
  browseSelected.clear();
  browseAnchorKey = null;
  results.innerHTML = "";
  updateBrowseSelectionCount();
  if (frameOpt) frameOpt.hidden = browseView !== "people";
  syncBrowseFrameVisibility();

  BROWSE_TREE_BRANCHES.forEach((branch) => {
    const { item, subtree } = createBrowseBranch(branch);
    sidebar.appendChild(item);
    sidebar.appendChild(subtree);
  });

  if (opts.skipRestore) return;

  const saved = cloneBrowseSelection();
  if (browseSelectionParticipates(saved)) {
    const surviving = await restoreBrowseTreeSelection(saved);
    if (browseSelectionParticipates(surviving)) {
      const emptyBranch = Object.entries(surviving).find(
        ([, keys]) => Array.isArray(keys) && !keys.length
      );
      if (emptyBranch) {
        browseSelectionByGroup = surviving;
        syncBrowseActiveFromSelection();
        saveBrowseSelectionPref();
        return;
      }
      const hasKeys = Object.values(surviving).some((k) => k?.length);
      if (hasKeys) await loadBrowsePhotosFromSelection(surviving);
      return;
    }
  }

  if (photoVisibility === "removed") {
    await loadAllRemovedPhotos();
    return;
  }

  const restoreId =
    browseView === "volumes"
      ? "volumes"
      : browseView === "people"
        ? "people"
        : browseView;
  const root = sidebar.querySelector(`.browse-branch[data-branch="${restoreId}"]`);
  if (root) {
    await toggleBrowseBranch(root, true);
    if (browseActiveKey === "all") {
      selectBrowseItem(root);
      if (restoreId === "people" || restoreId === "places" || showSubfoldersChecked()) {
        await loadBrowsePhotos(browseGroupBy(), "all");
      } else clearBrowseGallery();
    } else if (browseActiveKey) {
      selectBrowseTreePath(browseActiveKey);
      await loadBrowsePhotos(browseGroupBy(), browseActiveKey);
    }
  }
}

function browseIntersectKey(p) {
  if (p?.id != null) return `id:${p.id}`;
  if (p?.path) return `path:${p.path}`;
  return null;
}

async function fetchBrowsePhotosForKey(groupBy, key) {
  let q = `group_by=${encodeURIComponent(groupBy)}&key=${encodeURIComponent(key)}${removeQuery()}`;
  if (
    groupBy === "person" ||
    groupBy === "location" ||
    groupBy === "date" ||
    groupBy === "camera" ||
    groupBy === "aspect" ||
    groupBy === "folder" ||
    groupBy === "collection"
  ) {
    // Large groups exceed the default 200; truncation breaks cross-branch AND
    // (e.g. China ∩ 2025) and drops older bursts from Clusters/Regroup scope.
    q += `&limit=10000`;
    if (groupBy === "folder" || groupBy === "location" || groupBy === "date" || groupBy === "collection") {
      q += `&include_subfolders=${showSubfoldersChecked() ? "true" : "false"}`;
    }
    if ((groupBy === "person" || groupBy === "location") && key === "all") {
      q += `&labeled=${showLabeledImagesChecked() ? "true" : "false"}`;
    }
  }
  return api(`/api/browse/photos?${q}`);
}

async function loadAllRemovedPhotos() {
  const gen = ++browseLoadGen;
  browseSelectionByGroup = {};
  saveBrowseSelectionPref();
  syncBrowseActiveFromSelection();
  browseSelected.clear();
  browseAnchorKey = null;
  showBrowseLoading();
  updateBrowseSelectionCount();
  try {
    const items = await api(`/api/browse/photos?group_by=remove&key=all&limit=500`);
    if (gen !== browseLoadGen) return;
    browseItems = filterByPhotoVisibility(items || []);
    browseItemsFull = browseItems.slice();
    browseSearchActive = false;
    renderBrowseResults();
    refreshScopedWorkspaceViews();
  } finally {
    if (gen === browseLoadGen) setRegroupBusy(false);
  }
}

/** OR within each groupBy, AND across groupBy branches. */
async function loadBrowsePhotosFromSelection(selectionByGroup) {
  const gen = ++browseLoadGen;
  browseSelectionByGroup = cloneBrowseSelection(selectionByGroup);
  saveBrowseSelectionPref();
  syncBrowseActiveFromSelection();
  browseSelected.clear();
  browseAnchorKey = null;
  const results = $("#browse-results");
  showBrowseLoading();
  updateBrowseSelectionCount();

  const participating = Object.entries(browseSelectionByGroup).filter(([, keys]) =>
    Array.isArray(keys)
  );
  if (!participating.length) {
    if (gen !== browseLoadGen) return;
    browseItems = [];
    browseItemsFull = [];
    browseSearchActive = false;
    results.innerHTML = "";
    updateBrowseSelectionCount();
    setRegroupBusy(false);
    return;
  }
  for (const [, keys] of participating) {
    if (!keys.length) {
      if (gen !== browseLoadGen) return;
      browseItems = [];
      browseItemsFull = [];
      browseSearchActive = false;
      results.innerHTML = `<p class="muted">Turn on “Show labeled images” to include nested items.</p>`;
      updateBrowseSelectionCount();
      setRegroupBusy(false);
      return;
    }
  }

  try {
  const branchMaps = await Promise.all(
    participating.map(async ([groupBy, keys]) => {
      const batches = await Promise.all(
        keys.map((key) => fetchBrowsePhotosForKey(groupBy, key))
      );
      const countPeople =
        groupBy === "person" && keys.filter((k) => k && k !== "all").length > 1;
      const byKey = new Map();
      batches.forEach((batch, i) => {
        const personKey = keys[i];
        for (const p of batch || []) {
          const k = browseIntersectKey(p);
          if (k == null) continue;
          const faces = browseFaceBoxes(p);
          if (!byKey.has(k)) {
            byKey.set(
              k,
              countPeople
                ? {
                    ...p,
                    faces,
                    _personMatchCount: 1,
                    _matchedPeople: new Set([personKey]),
                  }
                : faces.length && !p.faces
                  ? { ...p, faces }
                  : p
            );
            continue;
          }
          if (!countPeople) continue;
          const existing = byKey.get(k);
          if (!existing._matchedPeople) existing._matchedPeople = new Set();
          if (!existing._matchedPeople.has(personKey)) {
            existing._matchedPeople.add(personKey);
            existing._personMatchCount = existing._matchedPeople.size;
          }
          existing.faces = mergeBrowseFaceLists(browseFaceBoxes(existing), faces);
        }
      });
      return byKey;
    })
  );
  if (gen !== browseLoadGen) return;

  let resultMap = branchMaps[0];
  for (let i = 1; i < branchMaps.length; i++) {
    const next = branchMaps[i];
    const intersected = new Map();
    for (const [k, p] of resultMap) {
      if (!next.has(k)) continue;
      const other = next.get(k);
      const ac = Number(p._personMatchCount) || 0;
      const bc = Number(other._personMatchCount) || 0;
      const merged = { ...p, ...other };
      // Keep person face ids when another branch's photo dict omits them.
      if (merged.face_id == null) {
        merged.face_id = p.face_id ?? other.face_id ?? null;
      }
      merged.faces = mergeBrowseFaceLists(browseFaceBoxes(p), browseFaceBoxes(other));
      if (merged.bbox_w == null && (p.bbox_w != null || other.bbox_w != null)) {
        const src = p.bbox_w != null ? p : other;
        merged.bbox_x = src.bbox_x;
        merged.bbox_y = src.bbox_y;
        merged.bbox_w = src.bbox_w;
        merged.bbox_h = src.bbox_h;
      }
      if (ac || bc) merged._personMatchCount = Math.max(ac, bc);
      intersected.set(k, merged);
    }
    resultMap = intersected;
  }
  browseItems = filterByPhotoVisibility([...resultMap.values()]);
  browseItemsFull = browseItems.slice();
  browseSearchActive = false;
  renderBrowseResults();
  } finally {
    if (gen === browseLoadGen) setRegroupBusy(false);
  }
}

async function loadBrowsePhotos(groupBy, key) {
  setBrowseContext(browseViewForGroupBy(groupBy), key);
  await loadBrowsePhotosFromSelection({ [groupBy]: [key] });
}

async function loadBrowsePhotosMulti(groupBy, keys) {
  setBrowseContext(browseViewForGroupBy(groupBy), keys[0] || null);
  await loadBrowsePhotosFromSelection({ [groupBy]: [...keys] });
}

document.querySelectorAll(".browse-view-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    activateWorkspaceView(btn.dataset.workspaceView);
  });
});

$("#browse-tile-size")?.addEventListener("input", (e) => {
  setBrowseTileSize(e.target.value);
});

$("#btn-browse-select-all")?.addEventListener("click", () => {
  toggleBrowseSelectAll();
});

function takenAtToInputValue(takenAt) {
  if (!takenAt) return "";
  const s = String(takenAt).trim().replace(" ", "T");
  // datetime-local wants YYYY-MM-DDTHH:MM or with seconds
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s)) return s.slice(0, 19);
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return `${s}T00:00:00`;
  return "";
}

/** Pad missing time parts to 00 so date-only / HH:MM values save cleanly. */
function normalizeTakenAtInput(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return `${raw}T00:00:00`;
  const m = raw.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?/);
  if (m) return `${m[1]}T${m[2]}:${m[3]}:${m[4] ?? "00"}`;
  return raw.replace(" ", "T");
}

function collectEditProperties(form) {
  /** Build extendible properties payload from named form fields. */
  const props = {};
  const taken = form.elements.namedItem("taken_at");
  if (taken && taken.value) props.taken_at = normalizeTakenAtInput(taken.value);
  const place = form.elements.namedItem("place_label");
  if (place && place.value.trim()) props.place_label = place.value.trim();
  return props;
}

function syncEditPersonControls(ctx) {
  const wrap = $("#edit-prop-person");
  const nameEl = $("#edit-prop-person-name");
  const unknownBtn = $("#edit-prop-person-unknown");
  const clearBtn = $("#edit-prop-person-clear");
  if (!wrap || !nameEl) return;
  if (!ctx?.faceIds?.length) {
    wrap.hidden = true;
    nameEl.value = "";
    nameEl.defaultValue = "";
    nameEl.dataset.person = "";
    nameEl.dataset.labeled = "0";
    if (unknownBtn) unknownBtn.hidden = true;
    if (clearBtn) clearBtn.hidden = true;
    return;
  }
  wrap.hidden = false;
  nameEl.value = ctx.labeled ? ctx.displayName : "";
  nameEl.defaultValue = nameEl.value;
  nameEl.placeholder = ctx.displayName || "Name…";
  nameEl.dataset.person = String(ctx.personId);
  nameEl.dataset.labeled = ctx.labeled ? "1" : "0";
  if (unknownBtn) unknownBtn.hidden = !!ctx.labeled;
  if (clearBtn) clearBtn.hidden = !ctx.labeled;
}

let editFileMetaList = [];
let editFileMetaGen = 0;

function renderEditFileMeta(list) {
  editFileMetaList = Array.isArray(list) ? list.filter((m) => m && !m.error) : [];
  const takenEl = $("#edit-prop-taken-at");
  const dateDiff = editFileMetaList.some(dateFileMetaDiffers);
  if (takenEl) takenEl.classList.toggle("is-diff", dateDiff);
}

async function refreshEditFileMeta(photos) {
  const gen = ++editFileMetaGen;
  const ids = (photos || []).map((p) => p.id).filter((id) => id != null);
  renderEditFileMeta([]);
  if (!ids.length) return;
  try {
    const res = await api("/api/photos/file-meta", {
      method: "POST",
      body: JSON.stringify({ photo_ids: ids }),
    });
    if (gen !== editFileMetaGen) return;
    renderEditFileMeta(res.photos || []);
  } catch {
    if (gen !== editFileMetaGen) return;
    renderEditFileMeta([]);
  }
}

function openEditPropertiesModal() {
  const photos = selectedPhotosForProperties();
  if (!photos.length) return;
  const dlg = $("#edit-properties");
  const form = $("#edit-properties-form");
  const err = $("#edit-properties-error");
  const count = $("#edit-properties-count");
  const title = $("#edit-properties-title");
  err.hidden = true;
  err.textContent = "";
  const n = photos.length;
  if (title) title.textContent = n === 1 ? "Edit 1 photo" : `Edit ${n} photos`;
  if (count) count.textContent = n === 1 ? "Editing 1 photo" : `Editing ${n} photos`;
  const dates = [...new Set(photos.map((p) => takenAtToInputValue(p.taken_at)).filter(Boolean))];
  form.elements.namedItem("taken_at").value = dates.length === 1 ? dates[0] : "";
  const places = [...new Set(photos.map((p) => (p.place_label || "").trim()).filter(Boolean))];
  form.elements.namedItem("place_label").value = places.length === 1 ? places[0] : "";
  const writeEl = $("#edit-write-exif");
  if (writeEl) writeEl.checked = writeDateToExif;
  syncEditPersonControls(peopleSelectionEditContext());
  renderEditFileMeta([]);
  dlg.showModal();
  refreshEditFileMeta(photos);
}

async function reloadAfterPeopleFaceEdit(personIds = []) {
  $("#edit-properties")?.close();
  const ids = [...new Set(personIds.map(Number).filter((id) => id > 0))];
  if (ids.length) {
    try {
      await api("/api/people/recluster", {
        method: "POST",
        body: JSON.stringify({ person_ids: ids }),
      });
    } catch (err) {
      console.error("recluster failed", err);
    }
  }
  await refreshPeopleBranchIfLoaded();
  await reloadBrowseGallery();
  refreshStats();
}

async function applySelectedFacesName(name, { merge = false, unknown = false } = {}) {
  const ctx = peopleSelectionEditContext();
  const err = $("#edit-properties-error");
  if (!ctx?.faceIds?.length) return;
  if (err) {
    err.hidden = true;
    err.textContent = "";
  }
  const faceIds = ctx.faceIds;
  const recluster = new Set([ctx.personId]);
  const wasLabeled = ctx.labeled;
  try {
    let label = name;
    if (unknown) {
      const first = await api(`/api/faces/${faceIds[0]}/unknown`, { method: "POST" });
      label = first.name;
      if (first.id) recluster.add(Number(first.id));
      for (const faceId of faceIds.slice(1)) {
        const updated = await patchLightboxFaceName(faceId, label, true);
        if (updated.id) recluster.add(Number(updated.id));
      }
    } else {
      let useMerge = merge;
      for (const faceId of faceIds) {
        try {
          const updated = await patchLightboxFaceName(faceId, label, useMerge);
          if (updated.id) recluster.add(Number(updated.id));
          useMerge = true;
        } catch (ex) {
          if (ex.conflict && ex.existing && !useMerge) {
            const exPerson = ex.existing;
            const existingLabel = exPerson.display_name || label;
            const ok = confirm(
              `"${existingLabel}" already exists (${exPerson.photo_count} photos). Merge selected faces into them?`
            );
            if (!ok) throw ex;
            const updated = await patchLightboxFaceName(faceId, label, true);
            if (updated.id) recluster.add(Number(updated.id));
            useMerge = true;
            continue;
          }
          throw ex;
        }
      }
    }
    // Recluster when assigning a name onto previously unlabeled faces.
    const named = unknown || isPersonNameLabeled(label);
    await reloadAfterPeopleFaceEdit(named && !wasLabeled ? [...recluster] : []);
  } catch (ex) {
    if (err) {
      err.textContent = ex.message || "Update failed";
      err.hidden = false;
    } else {
      alert(ex.message || "Update failed");
    }
  }
}

function browseGroupBy() {
  return browseGroupByForBranch(browseView);
}

async function reloadBrowseGallery() {
  if (!browseSelectionParticipates()) {
    renderBrowseResults();
    refreshScopedWorkspaceViews();
    return;
  }
  const emptyBranch = Object.entries(browseSelectionByGroup).find(
    ([, keys]) => Array.isArray(keys) && !keys.length
  );
  if (emptyBranch) {
    browseItems = [];
    browseItemsFull = [];
    browseSearchActive = false;
    const results = $("#browse-results");
    if (results) {
      results.innerHTML = `<p class="muted">Turn on “Show labeled images” to include nested items.</p>`;
    }
    updateBrowseSelectionCount();
    refreshScopedWorkspaceViews();
    return;
  }
  const hasKeys = Object.values(browseSelectionByGroup).some((k) => k?.length);
  if (hasKeys) await loadBrowsePhotosFromSelection(browseSelectionByGroup);
  else renderBrowseResults();
  refreshScopedWorkspaceViews();
}

function locationKeysForResolvedPlace(place) {
  if (!place) return [];
  const country = (place.country || "").trim();
  const city = (place.city || "").trim();
  const region = (place.region || "").trim();
  const venue = (place.venue || "").trim();
  const label = (place.place_label || "").trim();
  const keys = [];
  if (label) {
    const parts = label.split(",").map((s) => s.trim()).filter(Boolean);
    for (let i = 0; i < parts.length; i++) {
      keys.push(`place-suffix::${parts.slice(i).join(", ")}`);
    }
  }
  const parent = city || region;
  if (venue && country && parent) keys.push(`venue::${country}::${parent}::${venue}`);
  if (parent && country) keys.push(`city::${country}::${parent}`);
  if (country) keys.push(`country::${country}`);
  if (label && !country) keys.push(`place::${label}`);
  return keys;
}

/** Optionally jump to the resolved Place node after edit (pref: place_edit_nav). */
async function selectionAfterPlaceEdit(saved, resolvedPlace) {
  const next = cloneBrowseSelection(saved);
  const hadLocation =
    Object.prototype.hasOwnProperty.call(next, "location") || browseView === "places";
  if (!resolvedPlace || !hadLocation) return next;
  const keys = locationKeysForResolvedPlace(resolvedPlace);
  if (!keys.length) return next;
  await ensureBrowseBranchLoaded("location");
  const sidebar = $("#browse-groups");
  for (const k of keys) {
    const item = sidebar?.querySelector(
      `.browse-item[data-group-by="location"][data-path="${CSS.escape(k)}"]`
    );
    if (item) {
      next.location = [k];
      return next;
    }
  }
  return next;
}

async function reloadBrowseAfterPropertyEdit(props, resolvedPlace = null) {
  const placeEdited = "place_label" in props;
  const dateEdited = "taken_at" in props;
  const treeAffecting =
    (Object.prototype.hasOwnProperty.call(browseSelectionByGroup, "date") && dateEdited) ||
    (Object.prototype.hasOwnProperty.call(browseSelectionByGroup, "location") &&
      placeEdited) ||
    (browseView === "dates" && dateEdited) ||
    (browseView === "places" && placeEdited);
  if (!treeAffecting) {
    await reloadBrowseGallery();
    // Places/Dates trees are lazy-cached; refresh or invalidate after edits
    // even when browsing Folders (or another view).
    const ids = [];
    if (dateEdited) ids.push("dates");
    if (placeEdited) ids.push("places");
    if (dateEdited || placeEdited) ids.push("collections");
    if (ids.length) await refreshExpandedBranches(ids);
    photoMapLoaded = false;
    photoMapBounds = null;
    return;
  }
  const saved = cloneBrowseSelection();
  const openKeys = collectOpenBrowseKeys($("#browse-groups"));
  await loadBrowseView({ skipRestore: true });
  const target =
    placeEdited && placeEditNav === "follow"
      ? await selectionAfterPlaceEdit(saved, resolvedPlace)
      : saved;
  if (!browseSelectionParticipates(target)) {
    await restoreBrowseTreeExpansion(openKeys);
    refreshScopedWorkspaceViews();
    return;
  }
  const surviving = await restoreBrowseTreeSelection(target, openKeys);
  const hasKeys = Object.values(surviving).some((k) => k?.length);
  if (!hasKeys) {
    browseSelectionByGroup = {};
    saveBrowseSelectionPref();
    browseActiveKey = null;
    browseActiveKeys = [];
    browseItems = [];
    browseItemsFull = [];
    browseSearchActive = false;
    browseSelected.clear();
    browseAnchorKey = null;
    const results = $("#browse-results");
    if (results) results.innerHTML = `<p class="muted">This group no longer has photos.</p>`;
    updateBrowseSelectionCount();
    await restoreBrowseTreeExpansion(openKeys);
    refreshScopedWorkspaceViews();
    return;
  }
  await restoreBrowseTreeExpansion(openKeys);
  await loadBrowsePhotosFromSelection(surviving);
  photoMapLoaded = false;
  photoMapBounds = null;
  refreshScopedWorkspaceViews();
}

$("#btn-browse-edit")?.addEventListener("click", () => {
  openEditPropertiesModal();
});

let addToFolderPhotoIds = [];

async function openAddToFolderModal(photoIds) {
  const ids = (photoIds || []).map(Number).filter((id) => Number.isFinite(id));
  if (!ids.length) return;
  addToFolderPhotoIds = ids;
  const dlg = $("#add-to-folder");
  const count = $("#add-to-folder-count");
  const err = $("#add-to-folder-error");
  const select = $("#add-to-folder-select");
  const neu = $("#add-to-folder-new");
  if (err) {
    err.hidden = true;
    err.textContent = "";
  }
  if (neu) neu.value = "";
  if (count) {
    count.textContent = `${ids.length} photo${ids.length === 1 ? "" : "s"} selected`;
  }
  const nestedWrap = $("#add-to-folder-nested-wrap");
  const nested = $("#add-to-folder-nested");
  const parentId = activeCollectionId();
  const parentLinked = activeCollectionFlags().linked;
  if (nestedWrap) nestedWrap.hidden = parentId == null || parentLinked;
  if (nested) nested.checked = parentId != null && !parentLinked;
  if (select) {
    select.innerHTML = `<option value="">Choose…</option>`;
    try {
      const folders = await api(`/api/collections${removeQuery() ? `?${removeQuery().slice(1)}` : ""}`);
      for (const f of flattenCollections(folders || [])) {
        if (f.linked) continue;
        const opt = document.createElement("option");
        opt.value = String(f.id);
        opt.textContent = `${f.pathLabel} (${f.count ?? 0})`;
        select.appendChild(opt);
      }
    } catch (ex) {
      if (err) {
        err.textContent = ex.message || "Failed to load collections";
        err.hidden = false;
      }
    }
  }
  dlg?.showModal();
}

$("#btn-browse-add-folder")?.addEventListener("click", () => {
  const ids = isDupesWorkspace()
    ? [...dupeSelected]
    : selectedBrowsePhotos()
        .filter((p) => p.id != null)
        .map((p) => p.id);
  openAddToFolderModal(ids);
});

$("#btn-browse-remove-folder")?.addEventListener("click", async () => {
  const folderId = activeCollectionId();
  if (folderId == null) return;
  const ids = selectedBrowsePhotos()
    .filter((p) => p.id != null)
    .map((p) => p.id);
  if (!ids.length) return;
  try {
    await api(`/api/collections/${folderId}/members`, {
      method: "DELETE",
      body: JSON.stringify({ photo_ids: ids }),
    });
    browseSelected.clear();
    browseAnchorKey = null;
    await reloadBrowseGallery();
    await refreshExpandedBranches(["collections"]);
  } catch (ex) {
    alert(ex.message || "Remove from folder failed");
  }
});

$("#btn-add-to-folder-cancel")?.addEventListener("click", () => {
  $("#add-to-folder")?.close();
});

$("#add-to-folder-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#add-to-folder-error");
  const saveBtn = $("#btn-add-to-folder-save");
  const select = $("#add-to-folder-select");
  const neu = $("#add-to-folder-new");
  if (err) {
    err.hidden = true;
    err.textContent = "";
  }
  const newName = (neu?.value || "").trim();
  let folderId = select?.value ? Number(select.value) : null;
  if (saveBtn) saveBtn.disabled = true;
  try {
    if (newName) {
      const created = await api("/api/collections", {
        method: "POST",
        body: JSON.stringify({
          name: newName,
          parent_id:
            $("#add-to-folder-nested")?.checked && activeCollectionId() != null
              ? activeCollectionId()
              : null,
        }),
      });
      folderId = created.id;
    }
    if (!folderId) {
      throw new Error("Choose an existing folder or enter a new name");
    }
    await api(`/api/collections/${folderId}/members`, {
      method: "POST",
      body: JSON.stringify({ photo_ids: addToFolderPhotoIds }),
    });
    $("#add-to-folder")?.close();
    await refreshExpandedBranches(["collections"]);
  } catch (ex) {
    let msg = ex.message || "Add failed";
    if (msg.includes("name_conflict") || /already exists/i.test(msg)) {
      msg = "A folder with that name already exists";
    }
    if (err) {
      err.textContent = msg;
      err.hidden = false;
    } else {
      alert(msg);
    }
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
});

$("#edit-prop-person-unknown")?.addEventListener("click", async (e) => {
  e.preventDefault();
  const btn = e.currentTarget;
  btn.disabled = true;
  try {
    await applySelectedFacesName(null, { unknown: true });
  } finally {
    btn.disabled = false;
  }
});

$("#edit-prop-person-clear")?.addEventListener("click", async (e) => {
  e.preventDefault();
  const btn = e.currentTarget;
  btn.disabled = true;
  try {
    await applySelectedFacesName(null);
  } finally {
    btn.disabled = false;
  }
});

$("#edit-prop-person-name")?.addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  e.preventDefault();
  const input = e.currentTarget;
  const next = input.value.trim();
  if (next === input.defaultValue) return;
  applySelectedFacesName(next || null);
});

$("#btn-edit-properties-cancel")?.addEventListener("click", () => {
  $("#edit-properties")?.close();
});

$("#edit-properties-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  const ids = selectedPhotosForProperties().map((p) => p.id);
  if (!ids.length) return;
  const props = collectEditProperties(form);
  const err = $("#edit-properties-error");
  const saveBtn = $("#btn-edit-properties-save");
  const personWrap = $("#edit-prop-person");
  const personNameEl = $("#edit-prop-person-name");
  const personDirty =
    !!personWrap &&
    !personWrap.hidden &&
    !!personNameEl &&
    personNameEl.value.trim() !== personNameEl.defaultValue;
  const takenEmpty = !(form.elements.namedItem("taken_at")?.value || "");
  const placeEmpty = !(form.elements.namedItem("place_label")?.value || "").trim();
  // Person-only save: apply face labels without clearing Place.
  if (personDirty && !Object.keys(props).length && takenEmpty && placeEmpty) {
    saveBtn.disabled = true;
    err.hidden = true;
    try {
      await applySelectedFacesName(personNameEl.value.trim() || null);
    } finally {
      saveBtn.disabled = false;
    }
    return;
  }
  // Empty form → clear Place (city/region/country/label).
  if (!Object.keys(props).length) {
    props.place_label = "";
  }
  saveBtn.disabled = true;
  err.hidden = true;
  const writeExif = !!$("#edit-write-exif")?.checked;
  if (writeExif !== writeDateToExif) {
    writeDateToExif = writeExif;
    api("/api/settings", {
      method: "POST",
      body: JSON.stringify({ write_date_to_exif: writeExif }),
    }).catch(() => {});
  }
  try {
    const res = await api("/api/photos/properties", {
      method: "POST",
      body: JSON.stringify({ photo_ids: ids, properties: props, write_exif: writeExif }),
    });
    if (res.errors?.length) {
      const n = res.errors.length;
      err.textContent =
        res.updated > 0
          ? `Updated ${res.updated}; ${n} failed (${res.errors[0].error})`
          : res.errors[0].error || "Update failed";
      err.hidden = false;
      if (!res.updated) return;
    }
    if (personDirty) {
      await applySelectedFacesName(personNameEl.value.trim() || null);
      return;
    }
    $("#edit-properties")?.close();
    if (isDupesWorkspace()) {
      await loadDupes();
      refreshStats();
    } else {
      await reloadBrowseAfterPropertyEdit(props, res.place);
    }
  } catch (ex) {
    err.textContent = ex.message || "Update failed";
    err.hidden = false;
  } finally {
    saveBtn.disabled = false;
  }
});

$("#btn-browse-export")?.addEventListener("click", async () => {
  const ids = (isDupesWorkspace() ? selectedDupePhotos() : selectedBrowsePhotos())
    .filter((p) => p.id != null)
    .map((p) => p.id);
  if (!ids.length) return;
  const exportBtn = $("#btn-browse-export");
  const countEl = $("#browse-gallery-count");
  const prevLabel = exportBtn?.textContent || "Export selected";
  const prevCount = countEl?.textContent || "";
  if (exportBtn) {
    exportBtn.disabled = true;
    exportBtn.textContent = "Choosing folder…";
  }
  try {
    const picked = await api(
      `/api/pick-directory?title=${encodeURIComponent("Export photos to")}`,
      { method: "POST" }
    );
    if (picked.cancelled || !picked.path) return;
    if (exportBtn) exportBtn.textContent = "Exporting…";
    if (countEl) countEl.textContent = `Exporting ${ids.length}…`;
    const res = await api("/api/photos/export", {
      method: "POST",
      body: JSON.stringify({ photo_ids: ids, dest: picked.path }),
    });
    const parts = [`Exported ${res.exported || 0}`];
    if (res.skipped) parts.push(`${res.skipped} missing`);
    if (res.failed) parts.push(`${res.failed} failed`);
    if (countEl) countEl.textContent = `${parts.join("; ")} → ${res.dest || picked.path}`;
  } catch (ex) {
    alert(ex.message || "Export failed");
    if (countEl) countEl.textContent = prevCount;
  } finally {
    if (exportBtn) {
      exportBtn.textContent = prevLabel;
      if (isDupesWorkspace()) syncDupeToolbar();
      else syncBrowseRemoveButtons();
    }
  }
});

$("#btn-browse-remove")?.addEventListener("click", async () => {
  const removeBtn = $("#btn-browse-remove");
  const restoreMode = removeBtn?.dataset.mode === "restore";
  if (restoreMode) {
    if (isDupesWorkspace()) {
      const ids = selectedDupePhotos()
        .filter((p) => p.id != null && p.culled)
        .map((p) => p.id);
      if (!ids.length) return;
      await restorePhotoIds(ids);
      ids.forEach((id) => patchLocalPhotoCulled(id, false));
      dupeSelected.clear();
      loadDupes();
      refreshStats();
      return;
    }
    const ids = selectedBrowsePhotos()
      .filter((p) => p.id != null && p.culled)
      .map((p) => p.id);
    if (!ids.length) return;
    await restorePhotoIds(ids);
    ids.forEach((id) => patchLocalPhotoCulled(id, false));
    browseSelected.clear();
    browseAnchorKey = null;
    refreshStats();
    if (photoVisibility === "all") {
      renderBrowseResults();
      refreshScopedWorkspaceViews();
    } else {
      await reloadBrowseGallery();
    }
    return;
  }
  if (isDupesWorkspace()) {
    const ids = selectedDupePhotos()
      .filter((p) => p.id != null && !p.culled)
      .map((p) => p.id);
    if (!ids.length) return;
    const n = ids.length;
    const removingAll =
      n > 0 &&
      [...document.querySelectorAll("#dupe-list .dupe-members [data-id]")].every((el) => {
        if (el.dataset.culled === "1") return true;
        return dupeSelected.has(Number(el.dataset.id));
      });
    if (removingAll && !confirm(`Reject all ${n} photo${n === 1 ? "" : "s"}?`)) return;
    await removePhotoIds(ids);
    ids.forEach((id) => patchLocalPhotoCulled(id, true));
    dupeSelected.clear();
    loadDupes();
    refreshStats();
    return;
  }
  const selectedIds = selectedBrowsePhotos()
    .filter((p) => p.id != null && !p.culled)
    .map((p) => p.id);
  if (!selectedIds.length) return;
  const n = selectedIds.length;
  const removableVisible = browseDisplayItems().filter((p) => p.id != null && !p.culled);
  const removingAll =
    removableVisible.length > 0 &&
    removableVisible.every((p) => browseSelected.has(browseItemKey(p)));
  if (removingAll && !confirm(`Reject all ${n} photo${n === 1 ? "" : "s"}?`)) return;
  const ids = expandRemoveIdsForHiddenReplicas(selectedIds);
  await removePhotoIds(ids);
  ids.forEach((id) => patchLocalPhotoCulled(id, true));
  browseSelected.clear();
  browseAnchorKey = null;
  refreshStats();
  if (photoVisibility === "all") {
    renderBrowseResults();
    refreshScopedWorkspaceViews();
  } else {
    await reloadBrowseGallery();
  }
});

$("#btn-browse-delete")?.addEventListener("click", async () => {
  const selected = isDupesWorkspace() ? selectedDupePhotos() : selectedBrowsePhotos();
  const selectedIds = selected.filter((p) => p.id != null && p.culled).map((p) => p.id);
  if (!selectedIds.length) return;
  const n = selectedIds.length;
  if (
    !confirm(
      `Delete ${n} selected file${n === 1 ? "" : "s"}?\n\nFiles are moved to the system trash (not permanently erased). They will be removed from the index.`
    )
  ) {
    return;
  }
  const ids = expandDeleteIdsForHiddenReplicas(selectedIds);
  const deleteBtn = $("#btn-browse-delete");
  if (deleteBtn) {
    deleteBtn.disabled = true;
    deleteBtn.setAttribute("aria-busy", "true");
  }
  const countEl = $("#browse-gallery-count");
  const prevCount = countEl?.textContent || "";
  if (countEl) countEl.textContent = `Deleting ${ids.length}…`;
  try {
    const res = await deletePhotoIds(ids);
    const deletedIds = res?.deleted_ids?.length ? res.deleted_ids : ids;
    const failed = res?.failed || 0;
    const deleted = res?.deleted || deletedIds.length;
    dropLocalPhotosById(deletedIds);
    browseSelected.clear();
    browseAnchorKey = null;
    dupeSelected.clear();
    if (isDupesWorkspace()) {
      const gone = new Set(deletedIds);
      lastClusterPreviewItems = (lastClusterPreviewItems || [])
        .map((g) => ({
          ...g,
          members: (g.members || []).filter((m) => !gone.has(m.id)),
        }))
        .filter((g) => (g.members || []).length);
      renderDupeGroups(lastClusterPreviewItems);
    } else if (typeof renderBrowseResults === "function") {
      renderBrowseResults();
    }
    updateBrowseSelectionCount();
    if (countEl) {
      countEl.textContent =
        failed > 0
          ? `Deleted ${deleted}; ${failed} failed`
          : `Deleted ${deleted}`;
    }
    if (failed) {
      alert(
        `Deleted ${deleted} from the index. ${failed} file${failed === 1 ? "" : "s"} could not be moved to trash (kept in Rejected).`
      );
    }
    refreshStats();
    if (isDupesWorkspace()) {
      await loadDupes();
    } else {
      await reloadBrowseGallery();
    }
  } catch (ex) {
    alert(ex.message || "Delete failed");
    if (countEl) countEl.textContent = prevCount;
  } finally {
    if (deleteBtn) {
      deleteBtn.removeAttribute("aria-busy");
      syncBrowseRemoveButtons();
      syncDupeToolbar();
    }
  }
});

async function ensureBrowseBranchLoaded(groupBy, openKeys = null) {
  const branchId = browseViewForGroupBy(groupBy);
  const root = $("#browse-groups")?.querySelector(
    `.browse-branch[data-branch="${CSS.escape(branchId)}"]`
  );
  if (!root) return null;
  if (root.dataset.loaded !== "1") await toggleBrowseBranch(root, true, openKeys);
  else {
    const sub = root.nextElementSibling;
    if (sub?.classList.contains("browse-subtree") && sub.hidden) {
      await toggleBrowseBranch(root, true, openKeys);
    }
  }
  return root;
}

async function restoreBrowseTreeExpansion(openKeys) {
  if (!openKeys?.size) return;
  for (const key of openKeys) {
    if (!key.startsWith("branch:")) continue;
    const id = key.slice("branch:".length);
    await ensureBrowseBranchLoaded(browseGroupByForBranch(id), openKeys);
  }
}

function revealBrowseTreeAncestors(item) {
  const sidebar = $("#browse-groups");
  if (!sidebar || !item) return;
  let el = item.parentElement;
  while (el && el !== sidebar) {
    if (el.classList.contains("browse-subtree")) {
      el.hidden = false;
      const parentItem = el.previousElementSibling;
      if (parentItem) {
        setBrowseTwistie(parentItem, true);
        if (parentItem.classList.contains("browse-branch")) parentItem.dataset.loaded = "1";
      }
    }
    el = el.parentElement;
  }
}

function restoreBrowseTreeKeys(keys) {
  const sidebar = $("#browse-groups");
  if (!sidebar || !keys?.length) return [];
  clearBrowseTreeHighlights();
  const selectedEls = [];
  for (const k of keys) {
    const item = sidebar.querySelector(`.browse-item[data-path="${CSS.escape(String(k))}"]`);
    if (!item) continue;
    revealBrowseTreeAncestors(item);
    item.classList.add("selected");
    selectedEls.push(item);
  }
  browseTreeAnchor = selectedEls[selectedEls.length - 1] || null;
  return selectedEls.map((el) => el.dataset.path);
}

/** Restore multi-branch selection; returns surviving selectionByGroup. */
async function restoreBrowseTreeSelection(selectionByGroup, openKeys = null) {
  const sidebar = $("#browse-groups");
  const surviving = {};
  if (!sidebar) return surviving;
  clearBrowseTreeHighlights();
  const selectedEls = [];
  for (const [groupBy, keys] of Object.entries(selectionByGroup || {})) {
    if (!Array.isArray(keys)) continue;
    if (!keys.length) {
      surviving[groupBy] = [];
      continue;
    }
    if (keys.length === 1 && keys[0] === "all") {
      const root = await ensureBrowseBranchLoaded(groupBy, openKeys);
      if (root) {
        root.classList.add("selected");
        selectedEls.push(root);
        surviving[groupBy] = ["all"];
      }
      continue;
    }
    await ensureBrowseBranchLoaded(groupBy, openKeys);
    const kept = [];
    for (const k of keys) {
      const item = sidebar.querySelector(
        `.browse-item[data-group-by="${CSS.escape(groupBy)}"][data-path="${CSS.escape(String(k))}"]`
      );
      if (!item) continue;
      revealBrowseTreeAncestors(item);
      item.classList.add("selected");
      selectedEls.push(item);
      kept.push(k);
    }
    if (kept.length) surviving[groupBy] = kept;
  }
  browseTreeAnchor = selectedEls[selectedEls.length - 1] || null;
  return surviving;
}

$("#browse-show-nested")?.addEventListener("click", async () => {
  // Archives/collections/dates: nested gallery scope. People/Places: labeled filter.
  const saved = cloneBrowseSelection();
  const hadPerson = Array.isArray(saved.person);
  const hadLocation = Array.isArray(saved.location);
  const locationRoot = $("#browse-groups")?.querySelector('.browse-branch[data-branch="places"]');
  await refreshPeopleBranchIfLoaded();
  if (locationRoot?.dataset.loaded === "1") await fillPlacesBranch();
  if (hadPerson || hadLocation) {
    const surviving = await restoreBrowseTreeSelection(saved);
    if (hadLocation && !surviving.location?.length && locationRoot) {
      locationRoot.classList.add("selected");
      surviving.location = ["all"];
    }
    if (Object.values(surviving).some((k) => k?.length)) {
      await loadBrowsePhotosFromSelection(surviving);
    } else {
      clearBrowseGallery();
    }
    refreshScopedWorkspaceViews();
    return;
  }
  // Selection keys are often unchanged; clear so activate reloads with the new
  // include_subfolders (Library / Collections / Dates).
  const selected = [...($("#browse-groups")?.querySelectorAll(".browse-item.selected") || [])];
  if (selected.length) {
    browseSelectionByGroup = {};
    await activateSelectedBrowseTree(selected[0]);
    return;
  }
  if (browseSelectionParticipates()) await reloadBrowseGallery();
});

async function applyPhotoVisibility(mode) {
  if (mode === "selected") mode = "active";
  if (!VALID_PHOTO_VISIBILITY.has(mode) || mode === photoVisibility) return;
  photoVisibility = mode;
  syncPhotoVisibilityButtons();
  savePhotoVisibilityPref();
  // Selected is a client-side filter over the current gallery — keep photo picks.
  if (mode === "selected") {
    renderBrowseResults();
    refreshScopedWorkspaceViews();
    return;
  }
  const saved = cloneBrowseSelection();
  await refreshExpandedBranches();
  if (browseSelectionParticipates(saved)) {
    const surviving = await restoreBrowseTreeSelection(saved);
    if (Object.values(surviving).some((k) => k?.length)) {
      await loadBrowsePhotosFromSelection(surviving);
      refreshScopedWorkspaceViews();
      return;
    }
  }
  if (photoVisibility === "removed") {
    await loadAllRemovedPhotos();
    return;
  }
  clearBrowseGallery();
  refreshScopedWorkspaceViews();
}

document.querySelectorAll(".visibility-modes [data-visibility]").forEach((btn) => {
  btn.addEventListener("click", () => {
    applyPhotoVisibility(btn.dataset.visibility);
  });
});

$("#browse-show-face")?.addEventListener("click", () => {
  syncBrowseFrameVisibility();
});

$("#lightbox-show-frames")?.addEventListener("click", () => {
  drawLightboxFaceBoxes();
});

$("#lightbox-rotate")?.addEventListener("click", () => {
  const next = (orientationDeg($("#lightbox-orientation")?.value) + 90) % 360;
  setLightboxOrientationControl(next);
  applyLightboxEditPreview();
  syncLightboxLayoutMode();
  requestAnimationFrame(() => drawLightboxFaceBoxes());
  saveLightboxEdits({ showErrors: true });
});

$("#lightbox-mirror")?.addEventListener("click", () => {
  togglePress($("#lightbox-mirror"));
  applyLightboxEditPreview();
  requestAnimationFrame(() => drawLightboxFaceBoxes());
  saveLightboxEdits({ showErrors: true });
});

function commitLightboxPlaceEdit() {
  if (placeSuggestPending) return;
  saveLightboxEdits({ showErrors: true });
}
$("#lightbox-place")?.addEventListener("change", commitLightboxPlaceEdit);
$("#lightbox-place")?.addEventListener("blur", commitLightboxPlaceEdit);
$("#lightbox-taken-at")?.addEventListener("change", () => {
  saveLightboxEdits({ showErrors: true });
});
$("#lightbox-place")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    if (placeSuggestPending) {
      acceptPlaceSuggestion();
      return;
    }
    e.target.blur();
    commitLightboxPlaceEdit();
  } else if (e.key === "Escape" && placeSuggestPending) {
    e.preventDefault();
    e.stopPropagation();
    cancelPlaceSuggestion();
  }
});

$("#lightbox-meta")?.addEventListener("click", (e) => {
  if (e.target.closest(".lightbox-coords-map")) showLightboxPhotoOnMap();
  if (e.target.closest(".lightbox-coords-refresh")) reloadLightboxExifCoords();
});
$("#lightbox-write-exif")?.addEventListener("click", () => {
  writeLightboxExif();
});

$("#lightbox-remove")?.addEventListener("click", () => {
  removeLightboxPhoto();
});
$("#lightbox-restore")?.addEventListener("click", () => {
  restoreLightboxPhoto();
});
$("#lightbox-delete")?.addEventListener("click", () => {
  deleteLightboxPhoto();
});
$("#lightbox-similar")?.addEventListener("click", async () => {
  const photo = lightboxCurrentPhoto();
  const btn = $("#lightbox-similar");
  const err = $("#lightbox-edit-error");
  if (!photo || btn?.disabled) return;
  if (btn) btn.disabled = true;
  if (err) {
    err.hidden = true;
    err.textContent = "";
  }
  try {
    await findSimilarToPhoto(photo);
    $("#lightbox")?.close();
  } catch (e) {
    if (err) {
      err.textContent = e.message || "Could not find similar photos";
      err.hidden = false;
    }
    if (btn) btn.disabled = !(photo?.content_id != null);
  }
});

$("#btn-browse-similar")?.addEventListener("click", async () => {
  const selected = isDupesWorkspace() ? selectedDupePhotos() : selectedBrowsePhotos();
  if (selected.length !== 1) return;
  const btn = $("#btn-browse-similar");
  if (btn) btn.disabled = true;
  try {
    await findSimilarToPhoto(selected[0]);
  } catch {
    /* error already shown in gallery */
  } finally {
    syncBrowseRemoveButtons();
  }
});

document.querySelectorAll(".lightbox-vqa-preset").forEach((btn) => {
  btn.addEventListener("click", () => {
    const task = btn.dataset.vqaTask || "freeform";
    setLightboxVqaTask(task);
    if (task === "freeform") {
      const prompt = ($("#lightbox-vqa-prompt")?.value || "").trim();
      if (prompt) runLightboxVqa("freeform");
      else $("#lightbox-vqa-prompt")?.focus();
      return;
    }
    runLightboxVqa(task);
  });
});

$("#lightbox-vqa-prompt")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    runLightboxVqa("freeform");
  }
});

$("#lightbox-prev")?.addEventListener("click", () => lightboxNavigateTo(lightboxIndex - 1));
$("#lightbox-next")?.addEventListener("click", () => lightboxNavigateTo(lightboxIndex + 1));
$("#lightbox-up")?.addEventListener("click", () => lightboxNavigateCluster(-1));
$("#lightbox-down")?.addEventListener("click", () => lightboxNavigateCluster(1));
$("#lightbox-fullscreen")?.addEventListener("click", () => {
  const dlg = $("#lightbox");
  setLightboxFullscreen(!dlg?.classList.contains("is-fullscreen"));
});
$("#lightbox")?.addEventListener("cancel", (e) => {
  if (placeSuggestPending) {
    e.preventDefault();
    cancelPlaceSuggestion();
    return;
  }
  const dlg = $("#lightbox");
  if (dlg?.classList.contains("is-fullscreen")) {
    e.preventDefault();
    setLightboxFullscreen(false);
  }
});
$("#lightbox")?.addEventListener("keydown", (e) => {
  const tag = (e.target?.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select") return;
  if (e.key === "ArrowLeft") { e.preventDefault(); lightboxNavigateTo(lightboxIndex - 1); }
  else if (e.key === "ArrowRight") { e.preventDefault(); lightboxNavigateTo(lightboxIndex + 1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); lightboxNavigateCluster(-1); }
  else if (e.key === "ArrowDown") { e.preventDefault(); lightboxNavigateCluster(1); }
});

$("#lightbox")?.addEventListener("close", () => {
  document.querySelector(".lightbox-stage-wrap")?.classList.remove("is-over-image");
  cancelPlaceSuggestion();
  saveLightboxEdits();
  flushLightboxRecluster();
  resetLightboxZoomPan();
});

(() => {
  const wrap = document.querySelector(".lightbox-stage-wrap");
  const stage = $("#lightbox-img")?.closest(".lightbox-stage");
  const nav = wrap?.querySelector(".lightbox-nav-bar");
  if (!wrap || !stage || !nav) return;
  const setOver = (on) => wrap.classList.toggle("is-over-image", on);
  stage.addEventListener("mouseenter", () => setOver(true));
  stage.addEventListener("mouseleave", (e) => {
    if (nav.contains(e.relatedTarget)) return;
    setOver(false);
  });
  nav.addEventListener("mouseout", (e) => {
    if (nav.contains(e.relatedTarget) || stage.contains(e.relatedTarget)) return;
    setOver(false);
  });
})();

$("#btn-map-pin-cancel")?.addEventListener("click", () => settleMapPinConfirm(false));
$("#btn-map-pin-no")?.addEventListener("click", () => settleMapPinConfirm(false));
$("#map-pin-confirm")?.addEventListener("cancel", (e) => {
  e.preventDefault();
  settleMapPinConfirm(false);
});
$("#map-pin-confirm-form")?.addEventListener("submit", (e) => {
  e.preventDefault();
  settleMapPinConfirm(true);
});

(() => {
  const stage = $("#lightbox-img")?.closest(".lightbox-stage");
  if (!stage) return;

  stage.addEventListener(
    "wheel",
    (e) => {
      if (!$("#lightbox")?.open) return;
      e.preventDefault();
      const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
      lightboxZoomAt(e.clientX, e.clientY, lightboxZoom * factor);
    },
    { passive: false }
  );

  stage.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    if (e.target.closest(".lightbox-nav")) return;
    if (lightboxZoom <= 1.001) return;
    e.preventDefault();
    lightboxPanDrag = {
      x: e.clientX,
      y: e.clientY,
      panX: lightboxPanX,
      panY: lightboxPanY,
    };
    applyLightboxZoomPan();
  });

  window.addEventListener("mousemove", (e) => {
    if (!lightboxPanDrag) return;
    lightboxPanX = lightboxPanDrag.panX + (e.clientX - lightboxPanDrag.x);
    lightboxPanY = lightboxPanDrag.panY + (e.clientY - lightboxPanDrag.y);
    applyLightboxZoomPan();
  });

  window.addEventListener("mouseup", () => {
    if (!lightboxPanDrag) return;
    lightboxPanDrag = null;
    applyLightboxZoomPan();
  });

  const nav = document.querySelector(".lightbox-zoom-nav");
  if (nav) {
    let navDragging = false;
    const jumpToEvent = (e) => {
      const n = lightboxZoomNavNormFromEvent(e, nav);
      if (!n) return;
      lightboxPanToNorm(n.x, n.y);
    };
    nav.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      e.stopPropagation();
      navDragging = true;
      jumpToEvent(e);
    });
    window.addEventListener("mousemove", (e) => {
      if (!navDragging) return;
      jumpToEvent(e);
    });
    window.addEventListener("mouseup", () => {
      navDragging = false;
    });
  }
})();

/* Draggable & resizable dialogs with localStorage persistence */
function initDraggableDialog(dialog) {
  const key = "dlg-" + dialog.id;
  const handle = dialog.querySelector(".dialog-head");
  if (!handle) return;
  let restoring = false;

  function applyBox(x, y, w, h, clamp = true) {
    dialog.style.margin = "0";
    dialog.style.position = "fixed";
    dialog.style.inset = "auto";
    const left = clamp ? Math.max(0, Math.min(x, innerWidth - 80)) : x;
    const top = clamp ? Math.max(0, Math.min(y, innerHeight - 60)) : y;
    dialog.style.left = left + "px";
    dialog.style.top = top + "px";
    if (w) dialog.style.width = w + "px";
    if (h) dialog.style.height = h + "px";
  }

  function save() {
    if (restoring || !dialog.open) return;
    try {
      const r = dialog.getBoundingClientRect();
      if (r.width < 40 || r.height < 40) return;
      localStorage.setItem(key, JSON.stringify({
        x: Math.round(r.left), y: Math.round(r.top),
        w: Math.round(r.width), h: Math.round(r.height),
      }));
    } catch (_) {}
  }

  function restore() {
    try {
      const s = JSON.parse(localStorage.getItem(key));
      if (!s || !Number.isFinite(s.x) || !Number.isFinite(s.y)) return;
      restoring = true;
      applyBox(s.x, s.y, s.w, s.h);
      requestAnimationFrame(() => {
        applyBox(s.x, s.y, s.w, s.h);
        restoring = false;
      });
    } catch (_) {
      restoring = false;
    }
  }

  handle.addEventListener("mousedown", (e) => {
    if (e.target.closest("button, input, select, a")) return;
    e.preventDefault();
    const rect = dialog.getBoundingClientRect();
    const sx = e.clientX, sy = e.clientY;
    const sl = rect.left, st = rect.top;
    const onMove = (ev) => {
      applyBox(sl + ev.clientX - sx, st + ev.clientY - sy, null, null, false);
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      save();
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });

  new ResizeObserver(() => { if (dialog.open && !restoring) save(); }).observe(dialog);

  const origShow = dialog.showModal.bind(dialog);
  dialog.showModal = function () { origShow(); restore(); };
}

document.querySelectorAll(".draggable-dialog").forEach(initDraggableDialog);

loadBrowsePrefs();
bindPressPrefs();
bindLayoutSplitters();
bindBrowseTimeline();
setLightboxFullscreen(lightboxFullscreenPref(), { persist: false });
loadBrowseView();
loadAppSettings();
try {
  const saved = localStorage.getItem(TAB_STORAGE_KEY);
  if (restoreCompareView || saved === "compare" || saved === "dupes") {
    activateWorkspaceView("compare");
  } else if (saved === "thumbs" || saved === "stack") {
    activateWorkspaceView("stack");
  } else if (saved === "sources" || saved === "browse" || saved === "cards" || !saved) {
    activateWorkspaceView(
      browseGalleryView === "details" ? "details" : "stack"
    );
  } else if (VALID_WORKSPACE_VIEWS.has(saved)) {
    activateWorkspaceView(saved);
  } else if (saved === "search") {
    activateWorkspaceView(
      browseGalleryView === "details" ? "details" : "stack"
    );
  } else {
    activateWorkspaceView("stack");
  }
} catch (_) {
  activateWorkspaceView("stack");
}
