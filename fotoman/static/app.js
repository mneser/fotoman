const $ = (sel) => document.querySelector(sel);

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
  const view = GALLERY_WORKSPACE_VIEWS.has(browseGalleryView)
    ? browseGalleryView
    : "thumbs";
  browseGalleryView = view;
  activateTab("browse", { workspaceView: view });
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
    formatSourceStage("face-scanned", faces, indexed > 0 && pendingFaces === 0)
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
  { id: "index", label: "Indexing", slow: true },
  { id: "geocode", label: "Geocoding", slow: true },
  { id: "grayscale", label: "Fingerprinting", slow: true },
  { id: "faces", label: "Facematch", slow: false },
  { id: "embed", label: "Embedding", slow: true },
  { id: "clustering", label: "Clustering", slow: true },
];

function stageOrder(phase) {
  if (phase === "starting" || phase === "scan" || phase === "list" || phase === "index") {
    return "index";
  }
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
  const running = !!st?.running;
  const suspended = !!st?.embed_suspended || phase === "suspended";
  const active = running ? stageOrder(phase) : null;
  const pipeline = INDEX_STAGES.filter((s) => s.id !== "faces");
  const activeIdx = active ? pipeline.findIndex((s) => s.id === active) : -1;
  const pendingEmbed = pendingEmbedCount(st);
  const pendingFaces = pendingFacesCount();
  const faceRunning = !!faceSt?.running && faceSt.phase !== "suspended";

  const items = INDEX_STAGES.map((s) => {
    let cls = "";
    let count = "";
    let title = s.label;
    const pipeIdx = pipeline.findIndex((p) => p.id === s.id);

    if (s.id === "faces") {
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
      // Facematch disabled → pendingFaces is 0 → grey
    } else if (s.id === "embed") {
      if (pendingEmbed > 0) count = toGoLabel(pendingEmbed);
      if (running && active === "embed") {
        cls = "is-active";
        title = "Embedding now";
        count = toGoLabel(remainingCount(st)) || count;
      } else if (pendingEmbed > 0 && running) {
        cls = "is-waiting";
        title = "Embedding scheduled next";
      } else if (pendingEmbed > 0) {
        // Embed still enabled with leftover work, but indexer idle.
        cls = "is-waiting";
        title = "Embedding has work left";
      } else if (running && activeIdx >= 0 && pipeIdx < activeIdx) {
        cls = "is-done";
        title = "Embedding caught up";
      }
      // Embed disabled → pendingEmbed is 0 → grey idle (no pill highlight)
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
    // Do not mark later stages orange just because they are "next" — that lit
    // Fingerprinting while it had nothing left to do.

    return `<li class="${cls}" data-stage="${s.id}" title="${title}"><span class="stage-label">${s.label}</span>${
      count ? `<span class="stage-count">${count}</span>` : ""
    }</li>`;
  });

  root.innerHTML = items.join('<li class="index-stage-sep" aria-hidden="true">→</li>');
}

function applyIndexStatusToSources(st, sources, fst) {
  const faceSt = fst || lastFacesStatus;
  const byId = new Map((sources || []).map((s) => [Number(s.id), s]));
  $("#source-list").querySelectorAll("li[data-source-id]").forEach((li) => {
    const id = Number(li.dataset.sourceId);
    const missing = li.dataset.missing === "1";
    const src = byId.get(id) || {
      id,
      exists: !missing,
      pending_embed_count: Number(li.dataset.pendingEmbed || 0),
      pending_faces_count: Number(li.dataset.pendingFaces || 0),
    };
    if (byId.has(id) && li.dataset) {
      li.dataset.pendingEmbed = String(src.pending_embed_count ?? 0);
      li.dataset.pendingFaces = String(src.pending_faces_count ?? 0);
    }
    const progRow = li.querySelector(".source-progress-row");
    const prog = li.querySelector(".source-progress");
    const counts = li.querySelector(".source-counts");
    if (src && counts && byId.has(id)) {
      counts.innerHTML = formatSourceCounts(src);
    }
    const reindexBtn = li.querySelector("[data-reindex]");
    if (reindexBtn && byId.has(id)) {
      const enabled = Number(src.enabled ?? 1);
      reindexBtn.disabled = !enabled || missing;
    }
    const indexBusy = archiveIsBusy(id, st) && st.phase !== "suspended";
    const facesBusy = archiveFacesBusy(id, faceSt) && faceSt.phase !== "suspended";
    const busy = indexBusy || facesBusy;
    li.classList.toggle("is-active", busy);
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
      return `
      <li data-source-id="${s.id}" data-missing="${s.exists ? "0" : "1"}" data-pending-embed="${pending}" data-pending-faces="${pendingFaces}" class="${enabled ? "" : "source-disabled"}">
        <span class="badge ${s.exists ? "ok" : "missing"}">${s.exists ? "mounted" : "missing"}</span>
        <div class="source-meta">
          <input class="source-label" type="text" value="${esc(s.label)}" data-label="${s.id}" aria-label="Archive label" />
          <div class="path">${esc(s.path)}</div>
          <div class="source-counts">${formatSourceCounts(s)}</div>
          <div class="source-progress-row" hidden>
            <div class="source-progress"></div>
          </div>
        </div>
        <div class="source-actions">
          <button type="button" class="source-toggle ${enabled ? "is-on" : ""}" data-include="${s.id}">Include</button>
          <button type="button" class="source-toggle ${faceOn ? "is-on" : ""}" data-face-toggle="${s.id}" ${enabled ? "" : "disabled"}>Facematch</button>
          <button type="button" class="source-toggle ${embedOn ? "is-on" : ""}" data-embed-toggle="${s.id}" ${enabled ? "" : "disabled"}>Embed</button>
          <button type="button" class="source-icon-btn source-reindex" data-reindex="${s.id}" title="Reindex" aria-label="Reindex" ${enabled && s.exists ? "" : "disabled"}><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 6V1L7 6l5 5V7c3.31 0 6 2.69 6 6 0 1.01-.25 1.97-.7 2.8l1.46 1.46A7.93 7.93 0 0 0 20 13c0-4.42-3.58-8-8-8zm-6.7 2.74L3.84 7.28A7.93 7.93 0 0 0 4 13c0 4.42 3.58 8 8 8v5l5-5-5-5v4c-3.31 0-6-2.69-6-6 0-1.01.25-1.97.7-2.8z"/></svg></button>
          <button type="button" class="source-icon-btn danger" data-del="${s.id}" title="Remove" aria-label="Remove"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M6 19a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg></button>
        </div>
      </li>`;
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
          "Remove this archive from the index? Files on disk are kept. Photos that also belong under another archive stay indexed."
        )
      )
        return;
      await api(`/api/sources/${btn.dataset.del}`, { method: "DELETE" });
      loadSources();
      refreshStats();
    });
  });
  $("#source-list").querySelectorAll("[data-include]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.include;
      const next = !btn.classList.contains("is-on");
      btn.classList.toggle("is-on", next);
      const li = btn.closest("li");
      li?.classList.toggle("source-disabled", !next);
      li?.querySelectorAll("[data-face-toggle], [data-embed-toggle]").forEach((b) => {
        b.disabled = !next;
      });
      const reindexBtn = li?.querySelector("[data-reindex]");
      if (reindexBtn) reindexBtn.disabled = !next || li?.dataset.missing === "1";
      try {
        await api(`/api/sources/${id}/enable?enabled=${next}`, { method: "POST" });
      } catch (err) {
        btn.classList.toggle("is-on", !next);
        li?.classList.toggle("source-disabled", next);
        li?.querySelectorAll("[data-face-toggle], [data-embed-toggle]").forEach((b) => {
          b.disabled = next;
        });
        if (reindexBtn) reindexBtn.disabled = next || li?.dataset.missing === "1";
        alert(err.message);
      }
    });
  });
  $("#source-list").querySelectorAll("[data-face-toggle]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.faceToggle;
      const next = !btn.classList.contains("is-on");
      btn.classList.toggle("is-on", next);
      try {
        const st = await api(`/api/sources/${id}/face_enabled?enabled=${next}`, { method: "POST" });
        lastFacesStatus = st;
        loadSources();
      } catch (err) {
        btn.classList.toggle("is-on", !next);
        alert(err.message);
      }
    });
  });
  $("#source-list").querySelectorAll("[data-embed-toggle]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.embedToggle;
      const next = !btn.classList.contains("is-on");
      btn.classList.toggle("is-on", next);
      try {
        const st = await api(`/api/sources/${id}/embed_enabled?enabled=${next}`, { method: "POST" });
        lastIndexStatus = st;
        loadSources();
      } catch (err) {
        btn.classList.toggle("is-on", !next);
        alert(err.message);
      }
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
}

function indexStatusText(st, sources, fst) {
  const bits = [];
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
  const st = await api("/api/index/status");
  const wasRunning = !!lastIndexStatus?.running;
  lastIndexStatus = st;
  const el = $("#index-status");
  el.textContent = indexStatusText(st, lastSources, lastFacesStatus);
  applyIndexStatusToSources(st, lastSources, lastFacesStatus);
  if (!st.running) {
    refreshStats();
    // Refresh archive count lines after a job finishes.
    if (wasRunning) loadSources();
  }
}

let dupeSelected = new Set();
let dupePhotosById = new Map();

function dupeGroupSelectionState(groupEl) {
  const members = [...groupEl.querySelectorAll(".dupe-members [data-id]")];
  const selectedCount = members.filter((el) => dupeSelected.has(Number(el.dataset.id))).length;
  const selectable = members.filter((el) => {
    if (el.dataset.alt === "1") return false;
    if (photoVisibility === "removed") return el.dataset.culled === "1";
    return el.dataset.culled !== "1" && el.dataset.keeper !== "1";
  });
  return { selectedCount, nonPrimaryCount: selectable.length, nonPrimary: selectable };
}

function syncDupeSelectToggleLabel(btn, selectedCount, nonPrimaryCount) {
  if (selectedCount > 0) {
    btn.textContent = "Unselect all";
    btn.disabled = false;
  } else if (photoVisibility === "removed") {
    btn.textContent = nonPrimaryCount
      ? `Select removed (${nonPrimaryCount})`
      : "Select removed";
    btn.disabled = nonPrimaryCount === 0;
  } else {
    btn.textContent = nonPrimaryCount
      ? `Select non-primary (${nonPrimaryCount})`
      : "Select non-primary";
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
    const parts = [];
    if (n) parts.push(`${n} selected`);
    if (total) parts.push(`${total} photo${total === 1 ? "" : "s"}`);
    galleryCount.textContent = parts.join(" · ");
  }

  const allBtn = $("#btn-browse-select-all");
  if (allBtn) {
    const hasGroups = !!document.querySelector("#dupe-list .dupe-group");
    allBtn.hidden = !hasGroups;
    if (hasGroups) syncDupeSelectToggleLabel(allBtn, n, totalNonPrimary);
  }

  const removeBtn = $("#btn-browse-remove");
  const restoreBtn = $("#btn-browse-restore");
  const deleteBtn = $("#btn-browse-delete");
  const editBtn = $("#btn-browse-edit");
  const addFolderBtn = $("#btn-browse-add-folder");
  const removeFolderBtn = $("#btn-browse-remove-folder");
  if (removeBtn) {
    removeBtn.disabled = activeIds.length === 0;
    removeBtn.textContent = "Remove selected";
  }
  if (restoreBtn) {
    restoreBtn.disabled = removedIds.length === 0;
    restoreBtn.textContent = "Restore selected";
  }
  if (deleteBtn) {
    deleteBtn.disabled = removedIds.length === 0;
    deleteBtn.textContent = "Delete selected";
  }
  if (editBtn) {
    editBtn.disabled = indexedIds.length === 0;
    editBtn.textContent = "Edit selection";
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
  const selected = dupeSelected.has(m.id);
  const path = photoPath(m);
  const classes = [
    "card",
    isKeeper ? "keeper" : "",
    removed ? "removed" : "",
    isAlt ? "active-alt" : "",
    selected ? "selected" : "",
  ]
    .filter(Boolean)
    .join(" ");

  let badge = "";
  if (isAlt) badge = `<span class="tag kept-tag">KEPT</span>`;
  else if (removed) badge = `<span class="removed-badge">REMOVED</span>`;
  else if (isKeeper) badge = `<span class="tag">PRIMARY</span>`;

  return `
    <article class="${classes}" data-id="${m.id}" data-keeper="${isKeeper ? 1 : 0}" data-culled="${removed ? 1 : 0}" data-alt="${isAlt ? 1 : 0}" data-orientation="${orientationDeg(m)}" data-mirrored="${isMirrored(m) ? 1 : 0}">
      ${badge}
      <div class="card-media">
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
  document.querySelectorAll(".visibility-btn").forEach((btn) => {
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
  { id: "browse-show-months", key: "fotoman.browseShowMonths" },
  { id: "browse-show-nested", key: "fotoman.browseShowNested" },
  { id: "lightbox-show-frames", key: "fotoman.lightboxShowFrames" },
];

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
  const q = includeCulled() ? "?include_culled=true" : "";
  const scopeIds = browseScopedPhotoIds();
  const root = $("#dupe-list");
  if (!scopeIds.size) {
    dupePhotosById = new Map();
    dupeSelected.clear();
    root.innerHTML = `<p class="muted">Select items in the tree to show clusters.</p>`;
    syncDupeToolbar();
    return;
  }
  let groups = await api(`/api/duplicates${q}`);
  let unmatched = [];

  if (photoVisibility === "removed") {
    const clusteredRemoved = new Set();
    groups = groups
      .map((g) => {
        const members = g.members || [];
        const removed = members.filter((m) => m.culled && scopeIds.has(m.id));
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
    groups = groups
      .map((g) => ({
        ...g,
        members: filterByPhotoVisibility(
          (g.members || []).filter((m) => scopeIds.has(m.id)),
          { respectSelection: true }
        ),
      }))
      .filter((g) => (g.members || []).length >= 2);
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
            : `Select removed${selectable ? ` (${selectable})` : ""}`
          : groupSelected
            ? "Unselect all"
            : `Select non-primary${selectable ? ` (${selectable})` : ""}`;
      const altNote = altCount
        ? ` · ${altCount} kept alternative${altCount === 1 ? "" : "s"}`
        : photoVisibility === "removed"
          ? " · no kept alternative"
          : "";
      return `
      <section class="dupe-group" data-group="${g.id}">
        <div class="dupe-group-head month-divider" role="separator">
          <span class="dupe-group-title">${g.kind}${altNote}</span>
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
    ? `
      <section class="dupe-group dupe-unmatched" data-group="unmatched">
        <div class="dupe-group-head month-divider" role="separator">
          <span class="dupe-group-title">Unmatched removed (${unmatched.length})</span>
          <button type="button" class="dupe-select-toggle" data-group="unmatched">Select removed (${unmatched.length})</button>
        </div>
        <p class="muted dupe-unmatched-note">No cluster partners at the current level — including no kept active alternative.</p>
        <div class="dupe-members">
          ${unmatched.map((m) => renderDupeMember(m)).join("")}
        </div>
      </section>`
    : "";

  root.innerHTML = groupHtml + unmatchedHtml;

  root.querySelectorAll(".dupe-members [data-id]").forEach((el) => {
    let clickTimer = null;
    el.addEventListener("click", (e) => {
      e.preventDefault();
      if (el.dataset.alt === "1") return;
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
      else if (photoVisibility === "removed") selectDupeRemoved(group);
      else selectDupeNonPrimary(group);
      syncDupeToolbar();
    });
  });

  syncDupeToolbar();
}

const TAB_STORAGE_KEY = "fotoman.activeTab";
const VALID_TABS = new Set(["browse", "map", "dupes"]);
const GALLERY_WORKSPACE_VIEWS = new Set(["thumbs", "details"]);
const TIMELINE_WORKSPACE_VIEWS = new Set(["thumbs", "details", "map"]);
const VALID_WORKSPACE_VIEWS = new Set(["thumbs", "details", "map", "dupes"]);

function syncWorkspaceViewButtons(view) {
  document.querySelectorAll(".browse-view-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.workspaceView === view);
  });
  syncClusterLevelControls(view);
  const sizeControl = $("#browse-size-control");
  if (sizeControl) sizeControl.hidden = view !== "thumbs";
  const galleryActions = $(".browse-gallery-actions");
  if (galleryActions) {
    galleryActions.hidden = !(GALLERY_WORKSPACE_VIEWS.has(view) || view === "dupes");
  }
  const frameOpt = $("#browse-show-face");
  if (frameOpt) {
    frameOpt.hidden =
      !GALLERY_WORKSPACE_VIEWS.has(view) || browseView !== "people";
  }
  const monthsOpt = $("#browse-show-months");
  if (monthsOpt) monthsOpt.hidden = !GALLERY_WORKSPACE_VIEWS.has(view);
}

function currentWorkspaceView() {
  return (
    document.querySelector(".browse-view-btn.active")?.dataset.workspaceView ||
    browseGalleryView
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
    if (tab === "browse") workspaceView = browseGalleryView;
    else if (tab === "map" || tab === "dupes") workspaceView = tab;
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

  if (tab === "dupes") regroupClusters();
  if (tab === "map") loadMap(true);
}

function activateWorkspaceView(view) {
  if (!VALID_WORKSPACE_VIEWS.has(view)) view = "thumbs";
  if (GALLERY_WORKSPACE_VIEWS.has(view)) {
    browseGalleryView = view;
    saveBrowseGalleryPref();
    activateTab("browse", { workspaceView: view });
    renderBrowseResults();
  } else if (view === "map") {
    activateTab("map", { workspaceView: "map" });
    renderBrowseTimeline(browseDisplayItems());
    setTimeout(() => photoMap?.invalidateSize(), 60);
  } else if (view === "dupes") {
    activateTab("dupes", { workspaceView: "dupes" });
    renderBrowseTimeline([]);
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
/** True when VLM provider + API key + model are configured. */
let vqaConfigured = false;
/** Current Ask AI mode: where | describe | freeform */
let lightboxVqaTask = "freeform";
/** Pending Place suggestion from Where-is-this: { previous } or null. */
let placeSuggestPending = null;

function applyCachedSettings(s) {
  if (!s || typeof s !== "object") return;
  placeEditNav = s.place_edit_nav === "follow" ? "follow" : "stay";
  if (typeof s.vqa_configured === "boolean") {
    vqaConfigured = s.vqa_configured;
  } else {
    const provider = String(s.vqa_provider || "").trim().toLowerCase();
    vqaConfigured =
      provider !== "" &&
      provider !== "none" &&
      provider !== "off" &&
      !!s.vqa_api_key_set &&
      !!String(s.vqa_model || "").trim();
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
    const modelEl = $("#pref-model-id");
    const nearEl = $("#pref-near-dup");
    const faceEl = $("#pref-face-match");
    const dateOrderEl = $("#pref-date-tree-order");
    const placeNavEl = $("#pref-place-edit-nav");
    const writeExifEl = $("#pref-write-date-exif");
    const writeMtimeEl = $("#pref-write-date-mtime");
    const providerEl = $("#pref-vqa-provider");
    const keyEl = $("#pref-vqa-api-key");
    const keyStatus = $("#pref-vqa-key-status");
    const baseEl = $("#pref-vqa-base-url");
    const modelVqaEl = $("#pref-vqa-model");
    if (modelEl) modelEl.value = s.model_id || "";
    if (nearEl) nearEl.value = s.near_dup_threshold ?? "";
    if (faceEl) faceEl.value = s.face_match_threshold ?? "";
    if (dateOrderEl) {
      dateOrderEl.value = s.date_tree_order === "oldest" ? "oldest" : "newest";
    }
    if (placeNavEl) {
      placeNavEl.value = placeEditNav === "follow" ? "follow" : "stay";
    }
    if (writeExifEl) writeExifEl.checked = !!s.write_date_to_exif;
    if (writeMtimeEl) writeMtimeEl.checked = !!s.write_date_to_mtime;
    if (providerEl) providerEl.value = s.vqa_provider || "openai";
    if (keyEl) {
      keyEl.value = "";
      keyEl.placeholder = s.vqa_api_key_set ? "•••••••• (leave blank to keep)" : "sk-…";
    }
    if (keyStatus) {
      keyStatus.hidden = !s.vqa_api_key_set;
      keyStatus.textContent = s.vqa_api_key_set ? "API key is saved." : "";
    }
    if (baseEl) baseEl.value = s.vqa_base_url || "https://api.openai.com/v1";
    if (modelVqaEl) modelVqaEl.value = s.vqa_model || "gpt-4o-mini";
  } catch (ex) {
    if (err) {
      err.textContent = ex.message || "Failed to load preferences";
      err.hidden = false;
    }
    const baseEl = $("#pref-vqa-base-url");
    const modelVqaEl = $("#pref-vqa-model");
    if (baseEl && !baseEl.value) baseEl.value = "https://api.openai.com/v1";
    if (modelVqaEl && !modelVqaEl.value) modelVqaEl.value = "gpt-4o-mini";
  }
  dlg.showModal();
}

$("#btn-open-prefs")?.addEventListener("click", openPrefsDialog);
$("#btn-close-prefs")?.addEventListener("click", () => {
  $("#prefs-dialog")?.close();
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
    model_id: ($("#pref-model-id")?.value || "").trim(),
    near_dup_threshold: Number($("#pref-near-dup")?.value),
    face_match_threshold: Number($("#pref-face-match")?.value),
    date_tree_order:
      $("#pref-date-tree-order")?.value === "oldest" ? "oldest" : "newest",
    place_edit_nav:
      $("#pref-place-edit-nav")?.value === "follow" ? "follow" : "stay",
    write_date_to_exif: !!$("#pref-write-date-exif")?.checked,
    write_date_to_mtime: !!$("#pref-write-date-mtime")?.checked,
    vqa_provider: $("#pref-vqa-provider")?.value || "openai",
    vqa_base_url: ($("#pref-vqa-base-url")?.value || "").trim(),
    vqa_model: ($("#pref-vqa-model")?.value || "").trim(),
  };
  const keyVal = $("#pref-vqa-api-key")?.value;
  if (keyVal != null && keyVal !== "") body.vqa_api_key = keyVal;
  if (saveBtn) saveBtn.disabled = true;
  try {
    const s = await api("/api/settings", { method: "POST", body: JSON.stringify(body) });
    applyCachedSettings(s);
    $("#prefs-dialog")?.close();
    if ($("#lightbox")?.open) syncLightboxVqa(lightboxCurrentPhoto());
    await refreshExpandedBranches(["date"]);
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

function fitPhotoMapToPins() {
  if (!photoMap) return;
  const run = () => {
    photoMap.invalidateSize();
    if (photoMapBounds?.length) {
      photoMap.fitBounds(photoMapBounds, { padding: [36, 36], maxZoom: 17 });
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
  return `${n} photo${n === 1 ? "" : "s"} on map · Shift-drag to select a region`;
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

function photoPinIcon(photo) {
  const removed = photo.culled ? " removed" : "";
  const style = orientationStyle(photo);
  return L.divIcon({
    className: `photo-pin${removed}`,
    html: `<img src="/api/thumb/${photo.id}" alt="" loading="lazy"${style ? ` style="${style}"` : ""} />`,
    iconSize: [44, 44],
    iconAnchor: [22, 44],
    popupAnchor: [0, -40],
  });
}

/** Wider clusters when zoomed out; tighter (then off) when zoomed in. */
function photoClusterRadius(zoom) {
  if (zoom <= 3) return 100;
  if (zoom <= 6) return 80;
  if (zoom <= 10) return 60;
  if (zoom <= 14) return 45;
  return 30;
}

function photoClusterIcon(cluster) {
  const count = cluster.getChildCount();
  const first = cluster.getAllChildMarkers()[0];
  const thumbId = first?.options?.photoId;
  const size = count < 10 ? "small" : count < 100 ? "medium" : "large";
  const thumb = thumbId
    ? `<img class="photo-cluster-thumb" src="/api/thumb/${thumbId}" alt="" loading="lazy" />`
    : "";
  return L.divIcon({
    className: `photo-cluster photo-cluster-${size}`,
    html: `${thumb}<span class="photo-cluster-count">${count}</span>`,
    iconSize: [48, 48],
    iconAnchor: [24, 48],
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
    disableClusteringAtZoom: 17,
    spiderfyOnMaxZoom: true,
    zoomToBoundsOnClick: true,
    iconCreateFunction: photoClusterIcon,
  });
  photoMap.addLayer(photoMapCluster);
  bindMapRegionSelect(photoMap);
  setTimeout(() => photoMap.invalidateSize(), 50);
  return photoMap;
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
    if (suppressClick || mapSelectDrawing) return;
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
  status.textContent = "Loading pins…";
  try {
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
    const q = includeCulled() ? "?include_culled=true" : "";
    const all = await api(`/api/map/photos${q}`);
    const photos = filterByPhotoVisibility(
      all.filter((p) => scopeIds.has(p.id) && isClusterKeeper(p)),
      { respectSelection: true }
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
        title: [p.place_label, p.taken_at?.slice(0, 10)].filter(Boolean).join(" · "),
      });
      marker.on("click", () => {
        openLightbox({
          src: `/api/original/${p.id}`,
          orientation: p.orientation,
          mirrored: isMirrored(p),
          photo: p,
          group: photos.map((x) => ({
            src: `/api/original/${x.id}`,
            orientation: x.orientation,
            mirrored: isMirrored(x),
            photo: x,
          })),
          groupIndex: photos.indexOf(p),
        });
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
      status.textContent = `${photos.length} photo${photos.length === 1 ? "" : "s"} on map · Shift-drag to select a region`;
    }
    photoMapLoaded = true;
    fitPhotoMapToPins();
  } catch (err) {
    status.textContent = err.message;
  }
}


$("#search-form")?.addEventListener("submit", runSearch);

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

const CLUSTER_LEVEL_LABELS = [
  "Duplicates",
  "File formats",
  "Framing",
  "Burst",
  "Same",
  "Similar",
  "Related",
  "Familiar",
];

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

function syncClusterLevelLabel() {
  const slider = $("#cluster-level");
  const label = $("#cluster-level-label");
  if (!slider || !label) return;
  const n = Math.max(0, Math.min(7, Number(slider.value) || 0));
  label.textContent = CLUSTER_LEVEL_LABELS[n] || String(n);
}

function syncClusterLevelControls(workspaceView) {
  const control = $(".cluster-level-control");
  if (control) control.hidden = workspaceView !== "dupes";
}

function setRegroupBusy(busy) {
  const overlay = $("#regroup-overlay");
  if (overlay) overlay.hidden = !busy;
}

let regroupSeq = 0;

async function regroupClusters() {
  const seq = ++regroupSeq;
  const level = Number($("#cluster-level")?.value);
  const { exact, formatted, near, threshold } = clusterLevelConfig(level);
  const photo_ids = [...browseScopedPhotoIds()];
  const list = $("#dupe-list");
  if (!photo_ids.length) {
    setRegroupBusy(false);
    if (list) list.innerHTML = `<p class="muted">Select items in the tree to show clusters.</p>`;
    dupePhotosById = new Map();
    dupeSelected.clear();
    syncDupeToolbar();
    return;
  }
  setRegroupBusy(true);
  dupeSelected.clear();
  syncDupeToolbar();
  try {
    await api("/api/duplicates/rebuild", {
      method: "POST",
      body: JSON.stringify({
        threshold: near ? threshold : undefined,
        exact,
        formatted,
        near,
        photo_ids,
        include_culled: includeCulled(),
      }),
    });
    if (seq !== regroupSeq) return;
    await loadDupes();
    refreshStats();
  } catch (err) {
    if (seq === regroupSeq && list) {
      list.innerHTML = `<p class="muted">Regroup failed.</p>`;
    }
    console.error("regroup failed", err);
  } finally {
    if (seq === regroupSeq) setRegroupBusy(false);
  }
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

const debouncedRegroup = debounce(regroupClusters, 280);

$("#cluster-level")?.addEventListener("input", () => {
  syncClusterLevelLabel();
  debouncedRegroup();
});
syncClusterLevelLabel();

refreshStats();
loadSources();
pollIndex();
pollFacesStatus();

// ── SSE: live updates from server ───────────────────────────────────────────
/** Apply a fresh sources payload; full rebuild only if the archive set changed. */
async function applySourcesLive(sources) {
  const prevIds = (lastSources || []).map((s) => s.id).join(",");
  const nextIds = (sources || []).map((s) => s.id).join(",");
  lastSources = sources;
  if (prevIds !== nextIds || !$("#source-list")?.querySelector("li[data-source-id]")) {
    await loadSources();
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

let browseView = "folders";
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
const BROWSE_TILE_SIZE_MIN = 64;
const BROWSE_TILE_SIZE_MAX = 376;
const BROWSE_TILE_COMPACT_MAX = 120;
const VALID_BROWSE_VIEWS = new Set([
  "folders",
  "date",
  "camera",
  "location",
  "people",
]);
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

function loadBrowsePrefs() {
  let migratedRemovedBranch = false;
  try {
    const view = localStorage.getItem(BROWSE_VIEW_KEY);
    // Old Removed tree branch → global Removed visibility.
    if (view === "remove") {
      browseView = "folders";
      migratedRemovedBranch = true;
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
  loadPressPrefs();
  syncBrowseTileSizeControl();
  syncPhotoVisibilityButtons();
  syncBrowseNestedOptLabel();
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
  const root = $("#browse-results");
  if (!root) return;
  root.style.setProperty("--browse-tile", `${browseTileSize}px`);
  root.classList.toggle("is-compact", browseTileSize <= BROWSE_TILE_COMPACT_MAX);
}

function setBrowseTileSize(n, { persist = true } = {}) {
  browseTileSize = clampBrowseTileSize(n);
  syncBrowseTileSizeControl();
  applyBrowseTileSize();
  if (persist) saveBrowseTileSizePref();
}

function showMonthDividers() {
  // Search rank order wins; month bands only when browsing normally.
  return isPressOn($("#browse-show-months")) && !browseSearchActive;
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
  if (activePanel?.id === "tab-browse") {
    syncWorkspaceViewButtons(browseGalleryView);
  }
}

loadBrowsePrefs();
syncBrowseViewButtons();

const BROWSE_DETAIL_COLS = [
  { key: "name", label: "Name" },
  { key: "archive", label: "Archive" },
  { key: "path", label: "Path" },
  { key: "taken_at", label: "Date" },
  { key: "camera", label: "Camera" },
  { key: "place", label: "Place" },
  { key: "size", label: "Size" },
  { key: "dims", label: "Dimensions" },
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
    default:
      return "";
  }
}

function isClusterKeeper(p) {
  return p == null || p.is_keeper == null || !!p.is_keeper;
}

function browseKeepersOnlyView() {
  return browseGalleryView === "thumbs";
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
  const items = filterByPhotoVisibility(raw, { respectSelection: true });
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
    const parts = [];
    if (n) parts.push(`${n} selected`);
    if (total) parts.push(`${total} photo${total === 1 ? "" : "s"}`);
    countEl.textContent = parts.join(" · ");
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
    (treeInput?.dataset.displayName || treeInput?.defaultValue || "").trim() ||
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
  const restoreBtn = $("#btn-browse-restore");
  const deleteBtn = $("#btn-browse-delete");
  const editBtn = $("#btn-browse-edit");
  const similarBtn = $("#btn-browse-similar");
  const addFolderBtn = $("#btn-browse-add-folder");
  const removeFolderBtn = $("#btn-browse-remove-folder");
  const actions = $(".browse-selection-actions");
  if (actions) actions.hidden = selected.length === 0;
  if (removeBtn) {
    removeBtn.disabled = activeIds.length === 0;
    removeBtn.textContent = "Remove selected";
  }
  if (restoreBtn) {
    restoreBtn.disabled = removedIds.length === 0;
    restoreBtn.textContent = "Restore selected";
  }
  if (deleteBtn) {
    deleteBtn.disabled = removedIds.length === 0;
    deleteBtn.textContent = "Delete selected";
  }
  if (editBtn) {
    editBtn.disabled = indexedIds.length === 0;
    editBtn.textContent = "Edit selection";
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
    const inVfolder = activeVirtualFolderId() != null;
    removeFolderBtn.hidden = !inVfolder;
    removeFolderBtn.disabled = !inVfolder || indexedIds.length === 0;
  }
}

function toggleBrowseSelectAll() {
  if (isDupesWorkspace()) {
    const root = $("#dupe-list");
    if (!root) return;
    if (dupeSelected.size > 0) unselectDupeAll(root);
    else if (photoVisibility === "removed") selectDupeRemoved(root);
    else selectDupeNonPrimary(root);
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
  resetLightboxVqa();
  if (vqa) vqa.hidden = true;
  if (info) info.hidden = true;
  lightboxFaces = [];
  lightboxFallbackFace = null;
}

function resetLightboxVqa() {
  const err = $("#lightbox-vqa-error");
  const answer = $("#lightbox-vqa-answer");
  const askBtn = $("#lightbox-vqa-ask");
  if (err) {
    err.hidden = true;
    err.textContent = "";
  }
  if (answer) {
    answer.hidden = true;
    answer.textContent = "";
  }
  if (askBtn) askBtn.disabled = false;
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
  const askBtn = $("#lightbox-vqa-ask");
  if (freeWrap) freeWrap.hidden = next !== "freeform";
  if (askBtn) askBtn.hidden = next !== "freeform";
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
  const askBtn = $("#lightbox-vqa-ask");
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
  if (askBtn) askBtn.disabled = true;
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
    if (askBtn) askBtn.disabled = false;
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
    const peopleRoot = peopleBranchContainer();
    if (peopleRoot?.dataset.loaded === "1") await fillPeopleBranch(peopleRoot);
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
      const peopleRoot = $("#browse-groups")?.querySelector('.browse-branch[data-branch="people"]');
      if (peopleRoot?.dataset.loaded === "1") {
        fillPeopleBranch(peopleRoot).catch(() => {});
      }
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
  const name = basename(p.path || p.rel_path);
  if (name) rows.push(["Name", name]);
  const archive = photoArchiveName(p);
  if (archive) rows.push(["Archive", archive]);
  const path = photoPath(p);
  if (path) rows.push(["Path", path]);
  rows.push(["Date", p.taken_at ? p.taken_at.slice(0, 19) : "undated"]);
  if (p.place_label) rows.push(["Place", p.place_label]);
  if (p.camera) rows.push(["Camera", p.camera]);
  if (formatDims(p) !== "—") rows.push(["Dimensions", formatDims(p)]);
  if (formatBytes(p.size) !== "—") rows.push(["Size", formatBytes(p.size)]);
  const orient = orientationDeg(p);
  if (orient) rows.push(["Orientation", `${orient}°`]);
  if (isMirrored(p)) rows.push(["Mirror", "yes"]);
  if (p.lat != null && p.lon != null) {
    const coords = `${Number(p.lat).toFixed(5)}, ${Number(p.lon).toFixed(5)}`;
    rows.push(["Coordinates", p.gps ? `${coords} (GPS)` : coords]);
  }
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
    .map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`)
    .join("");
}

let lightboxGroup = [];
let lightboxIndex = -1;

function lightboxCurrentPhoto() {
  if (lightboxIndex >= 0 && lightboxIndex < lightboxGroup.length) {
    return lightboxGroup[lightboxIndex]?.photo || null;
  }
  return null;
}

function syncLightboxEditControls(photo, orientation, mirrored) {
  const edit = $("#lightbox-edit");
  const placeEl = $("#lightbox-place");
  const takenEl = $("#lightbox-taken-at");
  const orientEl = $("#lightbox-orientation");
  const mirrorEl = $("#lightbox-mirror");
  const similarBtn = $("#lightbox-similar");
  const removeBtn = $("#lightbox-remove");
  const restoreBtn = $("#lightbox-restore");
  const err = $("#lightbox-edit-error");
  const canEdit = photo?.id != null;
  if (edit) edit.hidden = !canEdit;
  if (!canEdit) return;
  if (err) {
    err.hidden = true;
    err.textContent = "";
  }
  if (placeEl) placeEl.value = (photo?.place_label || "").trim();
  if (takenEl) takenEl.value = takenAtToInputValue(photo?.taken_at);
  const deg = orientationDeg(orientation ?? photo?.orientation);
  const mirror = mirrored ?? isMirrored(photo);
  if (orientEl) orientEl.value = String(deg);
  if (mirrorEl) setPressOn(mirrorEl, !!mirror);
  setLightboxTransform(deg, mirror);
  const culled = !!photo?.culled;
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
}

function applyPlaceFieldsToPhoto(photo, place) {
  if (!photo) return;
  if (!place) {
    photo.place_label = null;
    photo.venue = null;
    photo.city = null;
    photo.region = null;
    photo.country = null;
    if (!photo.gps) {
      photo.lat = null;
      photo.lon = null;
    }
    return;
  }
  photo.place_label = place.place_label || null;
  photo.venue = place.venue || null;
  photo.city = place.city || null;
  photo.region = place.region || null;
  photo.country = place.country || null;
  if (place.lat != null && place.lon != null) {
    photo.lat = place.lat;
    photo.lon = place.lon;
    photo.gps = 0;
  } else if (!photo.gps) {
    photo.lat = null;
    photo.lon = null;
  }
}

function applyLightboxEditPreview() {
  const deg = orientationDeg($("#lightbox-orientation")?.value);
  const mirrored = isPressOn($("#lightbox-mirror"));
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
      }
      if (placeEdited) applyPlaceFieldsToPhoto(photo, res.place ?? null);
      if (lightboxIndex >= 0 && lightboxIndex < lightboxGroup.length) {
        const entry = lightboxGroup[lightboxIndex];
        entry.orientation = orientation;
        entry.mirrored = mirrored;
        entry.photo = photo;
      }
      if (placeEdited) {
        const placeEl = $("#lightbox-place");
        if (placeEl) placeEl.value = (photo.place_label || "").trim();
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
        if (takenEdited && takenValue) local.taken_at = photo.taken_at;
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
          if (placeEdited) branches.push("location");
          if (takenEdited) branches.push("date");
          refreshExpandedBranches(branches).catch(() => {});
        }
      }
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

function patchLocalPhotoCulled(id, culled) {
  const patch = (p) => {
    if (p && p.id === id) p.culled = culled;
  };
  browseItems.forEach(patch);
  browseItemsFull.forEach(patch);
  const dupe = dupePhotosById?.get?.(id);
  if (dupe) dupe.culled = culled;
}

async function removeLightboxPhoto() {
  const photo = lightboxCurrentPhoto();
  const id = photo?.id;
  if (id == null || photo.culled) return;
  const btn = $("#lightbox-remove");
  if (btn) btn.disabled = true;
  try {
    await saveLightboxEdits({ showErrors: true });
    await removePhotoIds([id]);
    photo.culled = true;
    patchLocalPhotoCulled(id, true);
    browseSelected.delete(browseItemKey(photo));
    if (lightboxIndex >= 0 && lightboxIndex < lightboxGroup.length) {
      lightboxGroup[lightboxIndex].photo = photo;
    }
    if (photoVisibility === "active") {
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
      err.textContent = ex.message || "Remove failed";
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

function syncLightboxLayoutMode() {
  const dlg = $("#lightbox");
  if (!dlg) return;
  const img = $("#lightbox-img");
  let w = img?.naturalWidth || 0;
  let h = img?.naturalHeight || 0;
  const deg = orientationDeg($("#lightbox-orientation")?.value);
  if (deg === 90 || deg === 270) {
    const t = w;
    w = h;
    h = t;
  }
  // Landscape photo: image on top, props|Ask side-by-side below.
  // Portrait photo: image beside stacked props + Ask.
  const landscape = w > 0 && h > 0 ? w >= h : true;
  dlg.classList.toggle("is-landscape", landscape);
  dlg.classList.toggle("is-portrait", !landscape);
  requestAnimationFrame(() => drawLightboxFaceBoxes());
}

async function lightboxNavigateTo(idx) {
  if (idx < 0 || idx >= lightboxGroup.length) return;
  if (idx === lightboxIndex) return;
  const saved = await saveLightboxEdits({ showErrors: true });
  if (!saved) return;
  lightboxIndex = idx;
  const entry = lightboxGroup[idx];
  const photo = entry.photo || null;
  const src = entry.src || "";
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
  if (prev) prev.hidden = lightboxGroup.length < 2 || lightboxIndex <= 0;
  if (next) next.hidden = lightboxGroup.length < 2 || lightboxIndex >= lightboxGroup.length - 1;
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
  const img = $("#lightbox-img");
  clearLightboxFaces();
  clearLightboxInfo();
  lightboxFallbackFace = face;
  const orient = orientation ?? photo?.orientation;
  const mirror = mirrored != null ? !!mirrored : isMirrored(photo);
  syncLightboxEditControls(photo, orient, mirror);
  renderLightboxMeta(photo);
  if (photo) $("#lightbox-info").hidden = false;
  syncLightboxFramesOpt();
  img.onload = null;
  img.src = src || "";
  const afterLoad = () => {
    syncLightboxLayoutMode();
    drawLightboxFaceBoxes();
  };
  if (img.complete && img.naturalWidth) afterLoad();
  else img.onload = afterLoad;
  loadLightboxFaces(photo);
  syncLightboxVqa(photo);
  syncLightboxNav();
  $("#lightbox").showModal();
  syncLightboxLayoutMode();
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
  const keys = sortedBrowseItems().map(browseItemKey);
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

function bindBrowseItemInteractions(root) {
  root.querySelectorAll("[data-browse-key]").forEach((el) => {
    el.addEventListener("mousedown", (e) => {
      if (e.shiftKey) e.preventDefault();
    });
    el.addEventListener("mouseenter", () => {
      setTimelineHoverKey(el.dataset.browseKey);
    });
    el.addEventListener("mouseleave", () => {
      if (timelineHoverKey === el.dataset.browseKey) setTimelineHoverKey(null);
    });
    el.addEventListener("click", (e) => {
      if (e.target.closest("th")) return;
      if (e.detail > 1) return;
      applyBrowseItemSelection(el.dataset.browseKey, e);
      syncBrowseSelectionDom(root);
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
      const sorted = sortedBrowseItems();
      const idx = sorted.findIndex((p) => browseItemKey(p) === key);
      const photo = idx >= 0 ? sorted[idx] : null;
      const group = sorted.map((p) => {
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

function renderBrowseThumbCard(p) {
  const media = browseMediaUrls(p);
  const key = browseItemKey(p);
  const removed = !!p.culled;
  const sel = browseSelected.has(key) ? " selected" : "";
  const hasFace = browseFaceBoxes(p).length ? " has-face" : "";
  const path = photoPath(p);
  return `
      <article class="card${removed ? " removed" : ""}${sel}" data-browse-key="${key}" data-original="${media.original}" data-orientation="${orientationDeg(p)}" data-mirrored="${isMirrored(p) ? 1 : 0}"${faceBoxAttrs(p)}>
        ${removed ? `<span class="removed-badge">REMOVE</span>` : ""}
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

function renderBrowseDetailRow(p) {
  const media = browseMediaUrls(p);
  const key = browseItemKey(p);
  const removed = !!p.culled;
  const sel = browseSelected.has(key) ? " selected" : "";
  const path = photoPath(p);
  return `
      <tr class="${removed ? "removed " : ""}${sel}" data-browse-key="${key}" data-original="${media.original}" data-orientation="${orientationDeg(p)}" data-mirrored="${isMirrored(p) ? 1 : 0}"${faceBoxAttrs(p)}>
        <td class="browse-detail-name">
          <img loading="lazy" src="${media.thumb}" alt="" draggable="false" style="${orientationStyle(p)}" />
          <span title="${path}">${basename(p.path)}${removed ? " · REMOVE" : ""}</span>
        </td>
        <td>${photoArchiveName(p) || "—"}</td>
        <td class="browse-detail-path" title="${path}">${path || "—"}</td>
        <td>${p.taken_at ? p.taken_at.slice(0, 10) : "—"}</td>
        <td>${p.camera || "—"}</td>
        <td>${p.place_label || "—"}</td>
        <td class="num">${formatBytes(p.size)}</td>
        <td class="num">${formatDims(p)}</td>
      </tr>`;
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
  return renderItemsWithMonthDividers(items, renderBrowseThumbCard, monthDividerHtml);
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
  const rows = renderItemsWithMonthDividers(items, renderBrowseDetailRow, (mk) =>
    monthDividerRowHtml(mk, BROWSE_DETAIL_COLS.length)
  );
  return `<div class="browse-details-wrap"><table class="browse-details">
    <thead><tr>${head}</tr></thead>
    <tbody>${rows}</tbody>
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
  syncBrowseSelectionDom();
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
  photoMap.fitBounds(L.latLngBounds(latlngs), { padding: [48, 48], maxZoom: 15 });
}

function scrollBrowseToFirstTimelineKey(keys) {
  if (!keys?.length) return;
  const want = new Set(keys);
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
  const selectedOk = browseSelected.size > 0 && browseSelected.size <= 500;
  if (selectedOk) {
    for (const key of browseSelected) {
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
        ? `<p class="muted">Removed is empty.</p>`
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
  if (groupBy === "folder" || groupBy === "vfolder") return "folders";
  if (groupBy === "person") return "people";
  return groupBy;
}

function browseGroupByForBranch(branchId) {
  if (branchId === "folders") return "folder";
  if (branchId === "people") return "person";
  return branchId;
}

function clearBrowseGallery() {
  browseActiveKey = null;
  browseActiveKeys = [];
  browseSelectionByGroup = {};
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

/** Nested-folder / date / place gallery scope (same checkbox as labeled people). */
function showSubfoldersChecked() {
  return showLabeledImagesChecked();
}

/** Cardinal toggle label: labeled (People) vs nested (folders/dates/places). */
function syncBrowseNestedOptLabel() {
  const btn = $("#browse-show-nested");
  if (!btn) return;
  if (browseView === "people") {
    btn.textContent = "Show labeled images";
    btn.title = "Show labeled people instead of unlabeled / Unknown";
  } else {
    btn.textContent = "Show nested";
    btn.title = "Include photos from nested folders, dates, or places";
  }
}

function cloneBrowseSelection(sel = browseSelectionByGroup) {
  const out = {};
  for (const [gb, keys] of Object.entries(sel || {})) {
    if (Array.isArray(keys)) out[gb] = [...keys];
  }
  return out;
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
    // People: checkbox filters labeled vs unlabeled, not nested scope — always
    // load the filtered “all” set when the People root is selected.
    if (branchId === "people" || showSub) selectionByGroup[gb] = ["all"];
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
    browseItems = [];
    browseItemsFull = [];
    browseSearchActive = false;
    browseSelected.clear();
    browseAnchorKey = null;
    const results = $("#browse-results");
    if (results) {
      results.innerHTML = `<p class="muted">Turn on “Show nested” to include nested items.</p>`;
    }
    updateBrowseSelectionCount();
    refreshScopedWorkspaceViews();
    return;
  }

  await loadBrowsePhotosFromSelection(selectionByGroup);
  refreshScopedWorkspaceViews();
}

function bindBrowseTreeRowClick(item) {
  item.addEventListener("mousedown", (e) => {
    if (e.shiftKey) e.preventDefault();
  });
  item.addEventListener("click", async (e) => {
    if (e.target.closest(".browse-twistie:not(.spacer)")) return;
    if (e.target.closest(".person-label")) return;
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

function renderBrowseTree(nodes, container, groupBy, depth = 0) {
  nodes.forEach((node) => {
    const isFile = !!node.is_file;
    const hasChildren = !!node.children;
    const kind = node.kind || (String(node.path || "").startsWith("region::") ? "region" : "");
    const item = document.createElement("div");
    item.className =
      "browse-item" +
      (hasChildren ? " has-children" : "") +
      (isFile ? " is-file" : "") +
      (kind === "region" ? " is-region" : "");
    item.dataset.path = node.path;
    item.dataset.groupBy = groupBy;
    if (kind) item.dataset.kind = kind;
    item.style.paddingLeft = `${depth * 0.8 + 0.4}rem`;
    const countHtml = isFile ? "" : `<span class="browse-item-count">${browseCountLabel(node)}</span>`;
    item.innerHTML = `${browseTwistieHtml(hasChildren)}<span class="browse-item-name">${node.name}</span>${countHtml}`;
    bindBrowseTwistie(item, () => toggleBrowseSubtree(item));
    bindBrowseTreeRowClick(item);
    container.appendChild(item);
    if (hasChildren) {
      const subtree = document.createElement("div");
      subtree.className = "browse-subtree";
      subtree.hidden = true;
      renderBrowseTree(node.children, subtree, groupBy, depth + 1);
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

function bindPersonLabel(input) {
  const save = async () => {
    const next = input.value.trim();
    const prev = input.defaultValue;
    if (next === prev) return;
    const personId = input.dataset.person;
    const wasLabeled = input.dataset.labeled === "1";
    const nextLabeled = isPersonNameLabeled(next);
    const photoCount = Number(input.dataset.photoCount) || 0;

    // Whole-group rename of an unlabeled/Unknown cluster — confirm when multi-photo.
    if (!wasLabeled && nextLabeled && photoCount > 1) {
      const ok = confirm(
        `Assign “${next}” to all ${photoCount} photos in this group?\n\nTo label only some photos, select them and use Edit selection.`
      );
      if (!ok) {
        input.value = prev;
        return;
      }
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
      input.value = updated.display_name;
      input.defaultValue = updated.display_name;
      input.dataset.displayName = updated.display_name;
      input.dataset.labeled = updated.labeled ? "1" : "0";
      input.title = `Person ${updated.id}`;
      // Labeled filter may hide/show this row after status change.
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
      }
    } catch (err) {
      if (err.conflict && err.existing) {
        const ex = err.existing;
        const label = ex.display_name || next;
        const ok = confirm(
          `"${label}" already exists (${ex.photo_count} photos). Merge this person into them?`
        );
        if (!ok) {
          input.value = prev;
          return;
        }
        try {
          const updated = await patch(true);
          await fillPeopleBranch();
          const kept = $("#browse-groups")?.querySelector(
            `.person-label[data-person="${updated.id}"]`
          );
          const item = kept?.closest(".browse-item");
          if (item) selectBrowseItem(item);
          setBrowseContext("people", String(updated.id));
          await loadBrowsePhotos("person", String(updated.id));
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

function peopleBranchContainer() {
  return $("#browse-groups")?.querySelector('.browse-subtree[data-branch="people"]');
}

function escHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/"/g, "&quot;");
}

function activeVirtualFolderId() {
  const keys = browseSelectionByGroup.vfolder;
  if (!keys?.length) return null;
  const id = Number(keys[0]);
  return Number.isFinite(id) ? id : null;
}

function renderVirtualFolders(folders, container) {
  if (!folders?.length) return;
  folders.forEach((f) => {
    const item = document.createElement("div");
    item.className = "browse-item browse-vfolder";
    item.dataset.path = String(f.id);
    item.dataset.groupBy = "vfolder";
    item.style.paddingLeft = "1.2rem";
    item.innerHTML = `${browseTwistieHtml(false)}<span class="browse-item-name" title="Double-click to rename">${escHtml(f.name)}</span>
      <span class="browse-item-count">${f.count ?? 0}</span>
      <button type="button" class="vfolder-delete" title="Delete folder" aria-label="Delete folder">✕</button>`;
    const nameEl = item.querySelector(".browse-item-name");
    nameEl?.addEventListener("dblclick", async (e) => {
      e.stopPropagation();
      const next = prompt("Rename folder", f.name);
      if (next == null) return;
      const label = next.trim();
      if (!label || label === f.name) return;
      try {
        await api(`/api/virtual-folders/${f.id}`, {
          method: "PATCH",
          body: JSON.stringify({ name: label }),
        });
        await refreshExpandedBranches(["folders"]);
      } catch (err) {
        alert(err.message || "Rename failed");
      }
    });
    item.querySelector(".vfolder-delete")?.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm(`Delete virtual folder “${f.name}”? Photos stay in the library.`)) return;
      try {
        await api(`/api/virtual-folders/${f.id}`, { method: "DELETE" });
        const vkeys = browseSelectionByGroup.vfolder || [];
        if (vkeys.map(String).includes(String(f.id))) {
          clearBrowseGallery();
        }
        await refreshExpandedBranches(["folders"]);
      } catch (err) {
        alert(err.message || "Delete failed");
      }
    });
    bindBrowseTreeRowClick(item);
    container.appendChild(item);
  });
}

async function fillPeopleBranch(container = peopleBranchContainer()) {
  if (!container) return;
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
      <input class="person-label" type="text" value="${esc(p.display_name)}"
        data-person="${p.id}" data-labeled="${p.labeled ? "1" : "0"}"
        data-display-name="${esc(p.display_name)}" data-photo-count="${p.photo_count}"
        title="Person ${p.id}" aria-label="Person name" />
      <span class="browse-item-count">${p.photo_count}</span>`;
    bindBrowseTreeRowClick(item);
    bindPersonLabel(item.querySelector(".person-label"));
    container.appendChild(item);
  });
}

async function fillBranchChildren(branchId, container) {
  container.innerHTML = `<p class="muted" style="padding:0.35rem 0.6rem">Loading…</p>`;
  try {
    if (branchId === "folders") {
      // Never list individual images in the tree — only folders.
      const culledQ = removeQuery();
      const [folders, tree] = await Promise.all([
        api(`/api/virtual-folders${culledQ ? `?${culledQ.slice(1)}` : ""}`),
        api(`/api/browse/tree?include_files=false${culledQ}`),
      ]);
      container.innerHTML = "";
      if (!(folders || []).length && !(tree || []).length) {
        container.innerHTML = `<p class="muted" style="padding:0.35rem 0.6rem">No archives or photos found.</p>`;
        return;
      }
      renderVirtualFolders(folders || [], container);
      if (tree?.length) renderBrowseTree(tree, container, "folder", 1);
    } else if (branchId === "date") {
      const tree = await api(`/api/browse/date-tree?${removeQuery().slice(1)}`);
      container.innerHTML = "";
      if (!tree.length) {
        container.innerHTML = `<p class="muted" style="padding:0.35rem 0.6rem">No dated photos found.</p>`;
        return;
      }
      renderBrowseTree(tree, container, "date", 1);
    } else if (branchId === "location") {
      const tree = await api(`/api/browse/location-tree?${removeQuery().slice(1)}`);
      container.innerHTML = "";
      if (!tree.length) {
        container.innerHTML = `<p class="muted" style="padding:0.35rem 0.6rem">No places. Index GPS or folder-named places first.</p>`;
        return;
      }
      renderBrowseTree(tree, container, "location", 1);
    } else if (branchId === "people") {
      await fillPeopleBranch(container);
    } else if (branchId === "camera") {
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

async function toggleBrowseBranch(branchEl, forceOpen = null) {
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
    await fillBranchChildren(branchId, subtree);
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

  return { item, subtree };
}

const BROWSE_TREE_BRANCHES = [
  { id: "folders", label: "Folders" },
  { id: "date", label: "Dates" },
  { id: "location", label: "Places" },
  { id: "people", label: "People" },
  { id: "camera", label: "Cameras" },
];

async function pollFacesStatus() {
  try {
    const st = await api("/api/faces/status");
    lastFacesStatus = st;
    const bar = $("#index-status");
    if (bar) bar.textContent = indexStatusText(lastIndexStatus, lastSources, st);
    applyIndexStatusToSources(lastIndexStatus, lastSources, st);
    if (!st.running) {
      const peopleRoot = $("#browse-groups")?.querySelector('.browse-branch[data-branch="people"]');
      if (peopleRoot?.dataset.loaded === "1") await fillPeopleBranch();
    }
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
    browseView === "folders"
      ? "folders"
      : browseView === "people"
        ? "people"
        : browseView;
  const root = sidebar.querySelector(`.browse-branch[data-branch="${restoreId}"]`);
  if (root) {
    await toggleBrowseBranch(root, true);
    if (browseActiveKey === "all") {
      selectBrowseItem(root);
      if (restoreId === "people" || showSubfoldersChecked()) {
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
    groupBy === "folder" ||
    groupBy === "vfolder"
  ) {
    // Large groups exceed the default 200; truncation breaks cross-branch AND
    // (e.g. China ∩ 2025) and drops older bursts from Clusters/Regroup scope.
    q += `&limit=10000`;
    if (groupBy === "folder" || groupBy === "location" || groupBy === "date") {
      q += `&include_subfolders=${showSubfoldersChecked() ? "true" : "false"}`;
    }
    if (groupBy === "person" && key === "all") {
      q += `&labeled=${showLabeledImagesChecked() ? "true" : "false"}`;
    }
  }
  return api(`/api/browse/photos?${q}`);
}

async function loadAllRemovedPhotos() {
  const gen = ++browseLoadGen;
  browseSelectionByGroup = {};
  syncBrowseActiveFromSelection();
  browseSelected.clear();
  browseAnchorKey = null;
  const results = $("#browse-results");
  if (results) results.innerHTML = `<p class="muted">Loading…</p>`;
  updateBrowseSelectionCount();
  const items = await api(`/api/browse/photos?group_by=remove&key=all&limit=500`);
  if (gen !== browseLoadGen) return;
  browseItems = filterByPhotoVisibility(items || []);
  browseItemsFull = browseItems.slice();
  browseSearchActive = false;
  renderBrowseResults();
  refreshScopedWorkspaceViews();
}

/** OR within each groupBy, AND across groupBy branches. */
async function loadBrowsePhotosFromSelection(selectionByGroup) {
  const gen = ++browseLoadGen;
  browseSelectionByGroup = cloneBrowseSelection(selectionByGroup);
  syncBrowseActiveFromSelection();
  browseSelected.clear();
  browseAnchorKey = null;
  const results = $("#browse-results");
  results.innerHTML = `<p class="muted">Loading…</p>`;
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
    return;
  }
  for (const [, keys] of participating) {
    if (!keys.length) {
      if (gen !== browseLoadGen) return;
      browseItems = [];
      browseItemsFull = [];
      browseSearchActive = false;
      results.innerHTML = `<p class="muted">Turn on “Show nested” to include nested items.</p>`;
      updateBrowseSelectionCount();
      return;
    }
  }

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

function openEditPropertiesModal() {
  const photos = (isDupesWorkspace() ? selectedDupePhotos() : selectedBrowsePhotos()).filter(
    (p) => p.id != null
  );
  if (!photos.length) return;
  const dlg = $("#edit-properties");
  const form = $("#edit-properties-form");
  const err = $("#edit-properties-error");
  const count = $("#edit-properties-count");
  err.hidden = true;
  err.textContent = "";
  count.textContent = `${photos.length} photo${photos.length === 1 ? "" : "s"} selected`;
  const dates = [...new Set(photos.map((p) => takenAtToInputValue(p.taken_at)).filter(Boolean))];
  form.elements.namedItem("taken_at").value = dates.length === 1 ? dates[0] : "";
  const places = [...new Set(photos.map((p) => (p.place_label || "").trim()).filter(Boolean))];
  form.elements.namedItem("place_label").value = places.length === 1 ? places[0] : "";
  syncEditPersonControls(peopleSelectionEditContext());
  dlg.showModal();
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
  const peopleRoot = peopleBranchContainer();
  if (peopleRoot?.dataset.loaded === "1") await fillPeopleBranch(peopleRoot);
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
  return browseView === "folders"
    ? "folder"
    : browseView === "people"
      ? "person"
      : browseView;
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
      results.innerHTML = `<p class="muted">Turn on “Show nested” to include nested items.</p>`;
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
    Object.prototype.hasOwnProperty.call(next, "location") || browseView === "location";
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
    (browseView === "date" && dateEdited) ||
    (browseView === "location" && placeEdited);
  if (!treeAffecting) {
    await reloadBrowseGallery();
    // Places/Dates trees are lazy-cached; refresh or invalidate after edits
    // even when browsing Folders (or another view).
    const ids = [];
    if (dateEdited) ids.push("date");
    if (placeEdited) ids.push("location");
    if (ids.length) await refreshExpandedBranches(ids);
    photoMapLoaded = false;
    photoMapBounds = null;
    return;
  }
  const saved = cloneBrowseSelection();
  await loadBrowseView({ skipRestore: true });
  const target =
    placeEdited && placeEditNav === "follow"
      ? await selectionAfterPlaceEdit(saved, resolvedPlace)
      : saved;
  if (!browseSelectionParticipates(target)) {
    refreshScopedWorkspaceViews();
    return;
  }
  const surviving = await restoreBrowseTreeSelection(target);
  const hasKeys = Object.values(surviving).some((k) => k?.length);
  if (!hasKeys) {
    browseSelectionByGroup = {};
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
    refreshScopedWorkspaceViews();
    return;
  }
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
  if (select) {
    select.innerHTML = `<option value="">Choose…</option>`;
    try {
      const folders = await api(`/api/virtual-folders${removeQuery() ? `?${removeQuery().slice(1)}` : ""}`);
      for (const f of folders || []) {
        const opt = document.createElement("option");
        opt.value = String(f.id);
        opt.textContent = `${f.name} (${f.count ?? 0})`;
        select.appendChild(opt);
      }
    } catch (ex) {
      if (err) {
        err.textContent = ex.message || "Failed to load folders";
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
  const folderId = activeVirtualFolderId();
  if (folderId == null) return;
  const ids = selectedBrowsePhotos()
    .filter((p) => p.id != null)
    .map((p) => p.id);
  if (!ids.length) return;
  try {
    await api(`/api/virtual-folders/${folderId}/members`, {
      method: "DELETE",
      body: JSON.stringify({ photo_ids: ids }),
    });
    browseSelected.clear();
    browseAnchorKey = null;
    await reloadBrowseGallery();
    await refreshExpandedBranches(["folders"]);
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
      const created = await api("/api/virtual-folders", {
        method: "POST",
        body: JSON.stringify({ name: newName }),
      });
      folderId = created.id;
    }
    if (!folderId) {
      throw new Error("Choose an existing folder or enter a new name");
    }
    await api(`/api/virtual-folders/${folderId}/members`, {
      method: "POST",
      body: JSON.stringify({ photo_ids: addToFolderPhotoIds }),
    });
    $("#add-to-folder")?.close();
    await refreshExpandedBranches(["folders"]);
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
  const ids = (isDupesWorkspace() ? selectedDupePhotos() : selectedBrowsePhotos())
    .filter((p) => p.id != null)
    .map((p) => p.id);
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
  try {
    const res = await api("/api/photos/properties", {
      method: "POST",
      body: JSON.stringify({ photo_ids: ids, properties: props }),
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

$("#btn-browse-remove")?.addEventListener("click", async () => {
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
    if (removingAll && !confirm(`Remove all ${n} photo${n === 1 ? "" : "s"}?`)) return;
    await removePhotoIds(ids);
    dupeSelected.clear();
    loadDupes();
    refreshStats();
    return;
  }
  const ids = selectedBrowsePhotos()
    .filter((p) => p.id != null && !p.culled)
    .map((p) => p.id);
  if (!ids.length) return;
  const n = ids.length;
  const removableVisible = browseDisplayItems().filter((p) => p.id != null && !p.culled);
  const removingAll =
    removableVisible.length > 0 &&
    removableVisible.every((p) => browseSelected.has(browseItemKey(p)));
  if (removingAll && !confirm(`Remove all ${n} photo${n === 1 ? "" : "s"}?`)) return;
  await removePhotoIds(ids);
  browseSelected.clear();
  browseAnchorKey = null;
  refreshStats();
  await reloadBrowseGallery();
});

$("#btn-browse-restore")?.addEventListener("click", async () => {
  if (isDupesWorkspace()) {
    const ids = selectedDupePhotos()
      .filter((p) => p.id != null && p.culled)
      .map((p) => p.id);
    if (!ids.length) return;
    await restorePhotoIds(ids);
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
  browseSelected.clear();
  browseAnchorKey = null;
  refreshStats();
  await reloadBrowseGallery();
});

$("#btn-browse-delete")?.addEventListener("click", async () => {
  const selected = isDupesWorkspace() ? selectedDupePhotos() : selectedBrowsePhotos();
  const ids = selected.filter((p) => p.id != null && p.culled).map((p) => p.id);
  if (!ids.length) return;
  const n = ids.length;
  if (
    !confirm(
      `Delete ${n} selected file${n === 1 ? "" : "s"}?\n\nFiles are moved to the system trash (not permanently erased). They will be removed from the index.`
    )
  ) {
    return;
  }
  const res = await deletePhotoIds(ids);
  if (res?.failed) {
    alert(
      `Deleted ${res.deleted || 0} from the index. ${res.failed} file${res.failed === 1 ? "" : "s"} could not be moved to trash (kept in Removed).`
    );
  }
  if (isDupesWorkspace()) {
    dupeSelected.clear();
    loadDupes();
    refreshStats();
    return;
  }
  browseSelected.clear();
  browseAnchorKey = null;
  refreshStats();
  await reloadBrowseGallery();
});

async function ensureBrowseBranchLoaded(groupBy) {
  const branchId = browseViewForGroupBy(groupBy);
  const root = $("#browse-groups")?.querySelector(
    `.browse-branch[data-branch="${CSS.escape(branchId)}"]`
  );
  if (!root) return null;
  if (root.dataset.loaded !== "1") await toggleBrowseBranch(root, true);
  else {
    const sub = root.nextElementSibling;
    if (sub?.classList.contains("browse-subtree") && sub.hidden) {
      await toggleBrowseBranch(root, true);
    }
  }
  return root;
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
async function restoreBrowseTreeSelection(selectionByGroup) {
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
      const root = await ensureBrowseBranchLoaded(groupBy);
      if (root) {
        root.classList.add("selected");
        selectedEls.push(root);
        surviving[groupBy] = ["all"];
      }
      continue;
    }
    await ensureBrowseBranchLoaded(groupBy);
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
  // Folders/dates/places: nested gallery scope. People: labeled filter.
  const saved = cloneBrowseSelection();
  const hadPerson = Array.isArray(saved.person);
  const peopleRoot = $("#browse-groups")?.querySelector('.browse-branch[data-branch="people"]');
  if (peopleRoot?.dataset.loaded === "1") await fillPeopleBranch();
  if (hadPerson) {
    const surviving = await restoreBrowseTreeSelection(saved);
    if (Object.values(surviving).some((k) => k?.length)) {
      await loadBrowsePhotosFromSelection(surviving);
    } else {
      clearBrowseGallery();
    }
    refreshScopedWorkspaceViews();
    return;
  }
  const selected = [...($("#browse-groups")?.querySelectorAll(".browse-item.selected") || [])];
  if (selected.length) {
    await activateSelectedBrowseTree(selected[0]);
    return;
  }
  if (browseSelectionParticipates()) await reloadBrowseGallery();
});

async function applyPhotoVisibility(mode) {
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

document.querySelectorAll(".visibility-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    applyPhotoVisibility(btn.dataset.visibility);
  });
});

$("#browse-show-face")?.addEventListener("click", () => {
  syncBrowseFrameVisibility();
});

$("#browse-show-months")?.addEventListener("click", () => {
  if (GALLERY_WORKSPACE_VIEWS.has(browseGalleryView)) renderBrowseResults();
});

$("#lightbox-show-frames")?.addEventListener("click", () => {
  drawLightboxFaceBoxes();
});

$("#lightbox-orientation")?.addEventListener("change", () => {
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

$("#lightbox-place")?.addEventListener("change", () => {
  if (placeSuggestPending) return;
  saveLightboxEdits({ showErrors: true });
});
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
  } else if (e.key === "Escape" && placeSuggestPending) {
    e.preventDefault();
    e.stopPropagation();
    cancelPlaceSuggestion();
  }
});

$("#lightbox-remove")?.addEventListener("click", () => {
  removeLightboxPhoto();
});
$("#lightbox-restore")?.addEventListener("click", () => {
  restoreLightboxPhoto();
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
  const selected = selectedBrowsePhotos();
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
      $("#lightbox-vqa-prompt")?.focus();
      return;
    }
    runLightboxVqa(task);
  });
});

$("#lightbox-vqa-ask")?.addEventListener("click", () => {
  runLightboxVqa("freeform");
});

$("#lightbox-vqa-prompt")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
    e.preventDefault();
    runLightboxVqa("freeform");
  }
});

$("#lightbox-prev")?.addEventListener("click", () => lightboxNavigateTo(lightboxIndex - 1));
$("#lightbox-next")?.addEventListener("click", () => lightboxNavigateTo(lightboxIndex + 1));
$("#lightbox")?.addEventListener("cancel", (e) => {
  if (placeSuggestPending) {
    e.preventDefault();
    cancelPlaceSuggestion();
  }
});
$("#lightbox")?.addEventListener("keydown", (e) => {
  const tag = (e.target?.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select") return;
  if (e.key === "ArrowLeft") { e.preventDefault(); lightboxNavigateTo(lightboxIndex - 1); }
  else if (e.key === "ArrowRight") { e.preventDefault(); lightboxNavigateTo(lightboxIndex + 1); }
});

$("#lightbox")?.addEventListener("close", () => {
  cancelPlaceSuggestion();
  saveLightboxEdits();
  flushLightboxRecluster();
});

/* Draggable & resizable dialogs with localStorage persistence */
function initDraggableDialog(dialog) {
  const key = "dlg-" + dialog.id;
  const handle = dialog.querySelector(".dialog-head");
  if (!handle) return;

  function save() {
    try {
      const r = dialog.getBoundingClientRect();
      localStorage.setItem(key, JSON.stringify({
        x: Math.round(r.left), y: Math.round(r.top),
        w: Math.round(r.width), h: Math.round(r.height),
      }));
    } catch (_) {}
  }

  function restore() {
    try {
      const s = JSON.parse(localStorage.getItem(key));
      if (!s) return;
      dialog.style.margin = "0";
      dialog.style.position = "fixed";
      dialog.style.left = Math.max(0, Math.min(s.x, innerWidth - 80)) + "px";
      dialog.style.top = Math.max(0, Math.min(s.y, innerHeight - 60)) + "px";
      if (s.w) dialog.style.width = s.w + "px";
      if (s.h) dialog.style.height = s.h + "px";
    } catch (_) {}
  }

  handle.addEventListener("mousedown", (e) => {
    if (e.target.closest("button, input, select, a")) return;
    e.preventDefault();
    const rect = dialog.getBoundingClientRect();
    const sx = e.clientX, sy = e.clientY;
    const sl = rect.left, st = rect.top;
    const onMove = (ev) => {
      dialog.style.margin = "0";
      dialog.style.position = "fixed";
      dialog.style.left = (sl + ev.clientX - sx) + "px";
      dialog.style.top = (st + ev.clientY - sy) + "px";
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      save();
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });

  new ResizeObserver(() => { if (dialog.open) save(); }).observe(dialog);
  dialog.addEventListener("close", save);

  const origShow = dialog.showModal.bind(dialog);
  dialog.showModal = function () { origShow(); restore(); };
}

document.querySelectorAll(".draggable-dialog").forEach(initDraggableDialog);

loadBrowsePrefs();
bindPressPrefs();
bindLayoutSplitters();
bindBrowseTimeline();
loadBrowseView();
loadAppSettings();
try {
  const saved = localStorage.getItem(TAB_STORAGE_KEY);
  if (saved === "sources" || saved === "browse" || saved === "cards" || !saved) {
    activateWorkspaceView(browseGalleryView || "thumbs");
  } else if (VALID_WORKSPACE_VIEWS.has(saved)) {
    activateWorkspaceView(saved);
  } else if (saved === "search") {
    activateWorkspaceView(browseGalleryView || "thumbs");
  } else {
    activateWorkspaceView("thumbs");
  }
} catch (_) {
  activateWorkspaceView("thumbs");
}
