const $ = (sel) => document.querySelector(sel);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
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

function orientationStyle(p) {
  const d = orientationDeg(p);
  return d ? `transform:rotate(${d}deg)` : "";
}

function setLightboxOrientation(deg) {
  const img = $("#lightbox-img");
  const stage = img?.closest(".lightbox-stage") || img;
  if (!stage) return;
  const d = orientationDeg(deg);
  stage.style.transform = d ? `rotate(${d}deg)` : "";
}

async function refreshStats() {
  const s = await api("/api/stats");
  $("#stats").textContent =
    `${s.photos} photos · ${s.embedded} embedded · ${s.with_place ?? s.with_gps} places · ${s.groups} groups · ${s.replicas} replicas`;
}

function renderResults(items) {
  const root = $("#results");
  searchItems = items || [];
  searchSelected.clear();
  updateSearchSelectionUi();
  if (!items.length) {
    root.innerHTML = `<p class="muted">No results.</p>`;
    return;
  }
  root.innerHTML = items
    .map(
      (p) => `
      <article class="card${p.culled ? " removed" : ""}" data-id="${p.id}" data-orientation="${orientationDeg(p)}">
        ${p.culled ? `<span class="removed-badge">REMOVE</span>` : ""}
        <img loading="lazy" src="/api/thumb/${p.id}" alt="" style="${orientationStyle(p)}" />
        <div class="meta">
          <strong title="${p.rel_path || p.path}">${basename(p.path)}</strong>
          ${p.score != null ? `score ${p.score.toFixed(3)} · ` : ""}
          ${p.distance_km != null ? `${p.distance_km} km · ` : ""}
          ${p.taken_at ? p.taken_at.slice(0, 10) : "undated"}
          ${p.place_label ? ` · ${p.place_label}` : ""}
        </div>
      </article>`
    )
    .join("");
  root.querySelectorAll(".card").forEach((el) => {
    el.addEventListener("click", (e) => {
      if (e.detail > 1) return;
      const id = Number(el.dataset.id);
      if (searchSelected.has(id)) searchSelected.delete(id);
      else searchSelected.add(id);
      el.classList.toggle("selected", searchSelected.has(id));
      updateSearchSelectionUi();
    });
    el.addEventListener("dblclick", (e) => {
      e.preventDefault();
      const id = Number(el.dataset.id);
      const idx = searchItems.findIndex((x) => x.id === id);
      const photo = idx >= 0 ? searchItems[idx] : null;
      const group = searchItems.map((p) => ({
        src: `/api/original/${p.id}`,
        orientation: orientationDeg(p),
        photo: p,
      }));
      openLightbox({
        src: `/api/original/${id}`,
        orientation: el.dataset.orientation,
        photo,
        group,
        groupIndex: idx,
      });
    });
  });
}

let searchItems = [];
let searchSelected = new Set();

function updateSearchSelectionUi() {
  const n = searchSelected.size;
  const countEl = $("#search-selection-count");
  if (countEl) countEl.textContent = n ? `${n} selected` : "";
  const selectAll = $("#btn-search-select-all");
  if (selectAll) {
    selectAll.hidden = !searchItems.length;
    if (searchItems.length) {
      const allSelected =
        searchItems.length > 0 && searchItems.every((p) => searchSelected.has(p.id));
      selectAll.textContent = allSelected ? "Deselect all" : "Select all";
    }
  }
  const removeBtn = $("#btn-search-remove");
  const restoreBtn = $("#btn-search-restore");
  const activeIds = [...searchSelected].filter((id) => {
    const p = searchItems.find((x) => x.id === id);
    return p && !p.culled;
  });
  const removedIds = [...searchSelected].filter((id) => {
    const p = searchItems.find((x) => x.id === id);
    return p && p.culled;
  });
  if (removeBtn) {
    removeBtn.disabled = activeIds.length === 0;
    removeBtn.textContent = activeIds.length
      ? `Remove selected (${activeIds.length})`
      : "Remove selected";
  }
  if (restoreBtn) {
    restoreBtn.disabled = removedIds.length === 0;
    restoreBtn.textContent = removedIds.length
      ? `Restore selected (${removedIds.length})`
      : "Restore selected";
  }
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

async function runSearch(e) {
  e.preventDefault();
  const params = new URLSearchParams();
  const q = $("#q").value.trim();
  const mode = $("#mode").value;
  params.set("mode", mode);
  if (q) params.set("q", q);
  for (const id of ["place", "album", "camera", "taken_from", "taken_to"]) {
    const v = $(`#${id}`).value.trim();
    if (v) params.set(id, v);
  }
  if ($("#has_gps").checked) params.set("has_gps", "true");
  if ($("#show_remove").checked) params.set("include_culled", "true");
  $("#results").innerHTML = `<p class="muted">Searching…</p>`;
  try {
    const items = await api(`/api/search?${params}`);
    renderResults(items);
  } catch (err) {
    $("#results").innerHTML = `<p class="muted">${err.message}</p>`;
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
  parts.push(formatSourceStage("indexed", indexed, listed > 0 && indexed >= listed));
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
  { id: "faces", label: "Face-scan", slow: false },
  { id: "embed", label: "Embedding", slow: true },
];

function stageOrder(phase) {
  if (phase === "starting" || phase === "scan" || phase === "list" || phase === "index") {
    return "index";
  }
  if (phase === "geocode" || phase === "grayscale" || phase === "embed") {
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
  const activeIdx = pipeline.findIndex((s) => s.id === active);
  const pendingEmbed = pendingEmbedCount(st);
  const pendingFaces = pendingFacesCount();
  const faceRunning = !!faceSt?.running && faceSt.phase !== "suspended";
  const facePaused = faceSt?.phase === "suspended" || (!faceSt?.running && pendingFaces > 0);

  const items = INDEX_STAGES.map((s) => {
    let cls = "";
    let count = "";
    if (s.id === "faces") {
      if (pendingFaces > 0) count = toGoLabel(pendingFaces);
      if (faceRunning) cls = "is-active";
      else if (facePaused) cls = "is-paused";
    } else if (s.id === "embed") {
      if (pendingEmbed > 0) count = toGoLabel(pendingEmbed);
      if (running && active === "embed") cls = "is-active";
      else if (pendingEmbed > 0 && (suspended || !running)) cls = "is-paused";
      else if (pendingEmbed > 0) cls = "is-waiting";
      else if (running && activeIdx >= 0 && pipeline.findIndex((p) => p.id === s.id) < activeIdx) {
        cls = "is-done";
      }
    } else if (running && s.id === active) {
      cls = "is-active";
      count = toGoLabel(remainingCount(st));
    } else if (running && activeIdx >= 0 && pipeline.findIndex((p) => p.id === s.id) < activeIdx) {
      cls = "is-done";
    } else if (!running && suspended && s.slow) {
      cls = "is-paused";
    }
    return `<li class="${cls}" data-stage="${s.id}"><span class="stage-label">${s.label}</span>${
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
          <button type="button" class="source-toggle ${faceOn ? "is-on" : ""}" data-face-toggle="${s.id}" ${enabled ? "" : "disabled"}>Face-scan</button>
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
  // Stage chips carry remaining work; only name the active archive here.
  const parts = [];
  if (fst?.running && fst.phase !== "suspended") {
    const name = archiveDisplayName(fst.source_id, sources);
    if (name) parts.push(name);
  }
  if (st?.running && st.phase !== "suspended") {
    const name = archiveDisplayName(st.source_id, sources);
    if (name) parts.push(name);
  }
  return [...new Set(parts)].join(" · ");
}

async function pollIndex() {
  const st = await api("/api/index/status");
  lastIndexStatus = st;
  const el = $("#index-status");
  el.textContent = indexStatusText(st, lastSources, lastFacesStatus);
  applyIndexStatusToSources(st, lastSources, lastFacesStatus);
  if (!st.running) refreshStats();
}

let dupeSelected = new Set();
let dupePhotosById = new Map();

function dupeGroupSelectionState(groupEl) {
  const members = [...groupEl.querySelectorAll(".dupe-members [data-id]")];
  const selectedCount = members.filter((el) => dupeSelected.has(Number(el.dataset.id))).length;
  const nonPrimary = members.filter(
    (el) => el.dataset.culled !== "1" && el.dataset.keeper !== "1"
  );
  return { selectedCount, nonPrimaryCount: nonPrimary.length, nonPrimary };
}

function syncDupeSelectToggleLabel(btn, selectedCount, nonPrimaryCount) {
  if (selectedCount > 0) {
    btn.textContent = "Unselect all";
    btn.disabled = false;
  } else {
    btn.textContent = nonPrimaryCount
      ? `Select non-primary (${nonPrimaryCount})`
      : "Select non-primary";
    btn.disabled = nonPrimaryCount === 0;
  }
}

function syncDupeToolbar() {
  const n = dupeSelected.size;
  const removeBtn = $("#btn-remove-selected");
  const restoreBtn = $("#btn-restore-selected");
  if (!removeBtn) return;
  removeBtn.disabled = n === 0;
  removeBtn.textContent = n ? `Remove selected (${n})` : "Remove selected";
  restoreBtn.disabled = n === 0;

  let totalNonPrimary = 0;
  document.querySelectorAll("#dupe-list .dupe-group").forEach((group) => {
    const state = dupeGroupSelectionState(group);
    totalNonPrimary += state.nonPrimaryCount;
    const btn = group.querySelector(".dupe-select-toggle");
    if (btn) syncDupeSelectToggleLabel(btn, state.selectedCount, state.nonPrimaryCount);
  });
  const allBtn = $("#btn-dupe-select-toggle");
  if (allBtn) {
    const hasGroups = !!document.querySelector("#dupe-list .dupe-group");
    allBtn.hidden = !hasGroups;
    if (hasGroups) syncDupeSelectToggleLabel(allBtn, n, totalNonPrimary);
  }
}

function selectDupeNonPrimary(scopeEl) {
  scopeEl.querySelectorAll(".dupe-members [data-id]").forEach((el) => {
    if (el.dataset.culled === "1" || el.dataset.keeper === "1") return;
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

function renderDupeMember(m) {
  const removed = !!m.culled;
  const isKeeper = !!m.is_keeper && !removed;
  const selected = dupeSelected.has(m.id);
  const path = photoPath(m);
  const classes = [
    "card",
    isKeeper ? "keeper" : "",
    removed ? "removed" : "",
    selected ? "selected" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return `
    <article class="${classes}" data-id="${m.id}" data-keeper="${isKeeper ? 1 : 0}" data-culled="${removed ? 1 : 0}" data-orientation="${orientationDeg(m)}">
      ${removed ? `<span class="removed-badge">REMOVE</span>` : ""}
      ${isKeeper ? `<span class="tag">PRIMARY</span>` : ""}
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

const DUPE_INCLUDE_REMOVED_KEY = "fotoman.dupeIncludeRemoved";

function dupeIncludeRemoved() {
  return !!$("#dupe-include-removed")?.checked;
}

function loadDupeIncludeRemovedPref() {
  const el = $("#dupe-include-removed");
  if (!el) return;
  try {
    el.checked = localStorage.getItem(DUPE_INCLUDE_REMOVED_KEY) === "1";
  } catch (_) {
    /* ignore */
  }
}

function saveDupeIncludeRemovedPref() {
  try {
    localStorage.setItem(DUPE_INCLUDE_REMOVED_KEY, dupeIncludeRemoved() ? "1" : "0");
  } catch (_) {
    /* ignore */
  }
}

async function loadDupes() {
  const includeRemoved = dupeIncludeRemoved();
  const q = includeRemoved ? "?include_culled=true" : "";
  const groups = await api(`/api/duplicates${q}`);
  const root = $("#dupe-list");
  const liveIds = new Set();
  dupePhotosById = new Map();
  groups.forEach((g) =>
    (g.members || []).forEach((m) => {
      liveIds.add(m.id);
      dupePhotosById.set(m.id, m);
    })
  );
  dupeSelected = new Set([...dupeSelected].filter((id) => liveIds.has(id)));
  syncDupeToolbar();

  if (!groups.length) {
    root.innerHTML = `<p class="muted">No groups yet. Index photos, then run Find bundles.</p>`;
    syncDupeToolbar();
    return;
  }
  root.innerHTML = groups
    .map((g) => {
      const active = (g.members || []).filter((m) => !m.culled);
      const nonPrimary = active.filter((m) => !m.is_keeper).length;
      const groupSelected = (g.members || []).filter((m) => dupeSelected.has(m.id)).length;
      const toggleLabel = groupSelected
        ? "Unselect all"
        : `Select non-primary${nonPrimary ? ` (${nonPrimary})` : ""}`;
      return `
      <div class="dupe-group" data-group="${g.id}">
        <div class="dupe-group-head">
          <h3>${g.kind}</h3>
          <button type="button" class="dupe-select-toggle" data-group="${g.id}" ${
            groupSelected || nonPrimary ? "" : "disabled"
          }>${toggleLabel}</button>
        </div>
        <div class="dupe-members">
          ${(g.members || []).map((m) => renderDupeMember(m)).join("")}
        </div>
      </div>`;
    })
    .join("");

  root.querySelectorAll(".dupe-members [data-id]").forEach((el) => {
    let clickTimer = null;
    el.addEventListener("click", (e) => {
      e.preventDefault();
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
        return { src: `/api/original/${mid}`, orientation: m.dataset.orientation, photo: dupePhotosById.get(mid) || null };
      });
      const groupIndex = memberEls.findIndex((m) => Number(m.dataset.id) === id);
      openLightbox({
        src: `/api/original/${id}`,
        orientation: el.dataset.orientation,
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
      else selectDupeNonPrimary(group);
      syncDupeToolbar();
    });
  });

  syncDupeToolbar();
}

const TAB_STORAGE_KEY = "fotoman.activeTab";
const VALID_TABS = new Set(["sources", "browse", "map", "search", "dupes"]);

function activateTab(tab) {
  if (!VALID_TABS.has(tab)) tab = "sources";
  const btn = document.querySelector(`.tab[data-tab="${tab}"]`);
  const panel = $(`#tab-${tab}`);
  if (!btn || !panel) return;
  document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
  document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
  btn.classList.add("active");
  panel.classList.add("active");
  try {
    localStorage.setItem(TAB_STORAGE_KEY, tab);
  } catch (_) { /* ignore quota / private mode */ }
  if (tab === "sources") {
    loadSources();
    pollFacesStatus();
  }
  if (tab === "dupes") loadDupes();
  if (tab === "browse") loadBrowseView();
  if (tab === "map") loadMap();
  if (tab === "search") {
    const q = $("#q");
    if (q) q.focus();
  }
}

document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => activateTab(btn.dataset.tab));
});

let photoMap = null;
let photoMapCluster = null;
let photoMapLoaded = false;

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
  setTimeout(() => photoMap.invalidateSize(), 50);
  return photoMap;
}

async function loadMap(force = false) {
  ensurePhotoMap();
  const status = $("#map-status");
  if (photoMapLoaded && !force) {
    setTimeout(() => photoMap.invalidateSize(), 50);
    return;
  }
  status.textContent = "Loading pins…";
  try {
    const includeCulled = $("#map-show-remove")?.checked;
    const q = includeCulled ? "?include_culled=true" : "";
    const photos = await api(`/api/map/photos${q}`);
    photoMapCluster.clearLayers();
    if (!photos.length) {
      status.textContent = "No photos with GPS yet.";
      photoMapLoaded = true;
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
          photo: p,
        });
      });
      markers.push(marker);
    }
    photoMapCluster.addLayers(markers);
    if (bounds.length) {
      photoMap.fitBounds(bounds, { padding: [36, 36], maxZoom: 12 });
    }
    status.textContent = `${photos.length} photo${photos.length === 1 ? "" : "s"} on map`;
    photoMapLoaded = true;
    setTimeout(() => photoMap.invalidateSize(), 50);
  } catch (err) {
    status.textContent = err.message;
  }
}

$("#map-show-remove")?.addEventListener("change", () => loadMap(true));

$("#search-form")?.addEventListener("submit", runSearch);

$("#btn-search-select-all")?.addEventListener("click", () => {
  if (!searchItems.length) return;
  const allSelected = searchItems.every((p) => searchSelected.has(p.id));
  if (allSelected) searchSelected.clear();
  else searchItems.forEach((p) => searchSelected.add(p.id));
  $("#results")
    ?.querySelectorAll(".card[data-id]")
    .forEach((el) => {
      el.classList.toggle("selected", searchSelected.has(Number(el.dataset.id)));
    });
  updateSearchSelectionUi();
});

$("#btn-search-remove")?.addEventListener("click", async () => {
  const ids = [...searchSelected].filter((id) => {
    const p = searchItems.find((x) => x.id === id);
    return p && !p.culled;
  });
  if (!ids.length) return;
  await removePhotoIds(ids);
  searchSelected.clear();
  refreshStats();
  $("#search-form")?.requestSubmit();
});

$("#btn-search-restore")?.addEventListener("click", async () => {
  const ids = [...searchSelected].filter((id) => {
    const p = searchItems.find((x) => x.id === id);
    return p && p.culled;
  });
  if (!ids.length) return;
  await restorePhotoIds(ids);
  searchSelected.clear();
  refreshStats();
  $("#search-form")?.requestSubmit();
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

$("#btn-rebuild")?.addEventListener("click", async () => {
  const exact = $("#find-exact")?.checked;
  const formatted = $("#find-formatted")?.checked;
  const variation = Number($("#variation")?.value);
  const threshold = 1 - variation * 0.02;
  const near = Number.isFinite(variation) && variation > 0;
  if (!exact && !formatted && !near) {
    alert("Pick at least one find mode: Duplicate, Formatted, or Variation above 0.");
    return;
  }
  const list = $("#dupe-list");
  if (list) list.innerHTML = `<p class="muted">Clustering…</p>`;
  dupeSelected.clear();
  syncDupeToolbar();
  await api("/api/duplicates/rebuild", {
    method: "POST",
    body: JSON.stringify({
      threshold: near ? threshold : undefined,
      exact,
      formatted,
      near,
    }),
  });
  loadDupes();
  refreshStats();
});

$("#btn-dupe-select-toggle")?.addEventListener("click", () => {
  const root = $("#dupe-list");
  if (!root) return;
  if (dupeSelected.size > 0) unselectDupeAll(root);
  else selectDupeNonPrimary(root);
  syncDupeToolbar();
});

$("#dupe-include-removed")?.addEventListener("change", () => {
  saveDupeIncludeRemovedPref();
  loadDupes();
});

$("#btn-remove-selected")?.addEventListener("click", async () => {
  const ids = [...dupeSelected];
  if (!ids.length) return;
  await removePhotoIds(ids);
  dupeSelected.clear();
  loadDupes();
  refreshStats();
});

$("#btn-restore-selected")?.addEventListener("click", async () => {
  const ids = [...dupeSelected];
  if (!ids.length) return;
  await restorePhotoIds(ids);
  dupeSelected.clear();
  loadDupes();
  refreshStats();
});

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

const placeInput = $("#place");
const placeList = $("#place-suggest");
let placeActive = -1;

async function refreshPlaceSuggest() {
  const q = placeInput.value.trim();
  placeActive = -1;
  if (q.length < 1) {
    placeList.hidden = true;
    placeList.innerHTML = "";
    return;
  }
  const items = await api(`/api/places/suggest?q=${encodeURIComponent(q)}`);
  if (!items.length) {
    placeList.hidden = true;
    placeList.innerHTML = "";
    return;
  }
  placeList.innerHTML = items
    .map(
      (it, i) =>
        `<li data-i="${i}" data-label="${it.label.replace(/"/g, "&quot;")}">
          <span>${it.label}</span>
          <span class="kind">${it.kind} · ${it.count}</span>
        </li>`
    )
    .join("");
  placeList.hidden = false;
  placeList.querySelectorAll("li").forEach((li) => {
    li.addEventListener("mousedown", (e) => {
      e.preventDefault();
      placeInput.value = li.dataset.label;
      placeList.hidden = true;
    });
  });
}

placeInput?.addEventListener("input", debounce(refreshPlaceSuggest, 180));
placeInput?.addEventListener("keydown", (e) => {
  if (!placeList) return;
  const items = [...placeList.querySelectorAll("li")];
  if (placeList.hidden || !items.length) return;
  if (e.key === "ArrowDown") {
    e.preventDefault();
    placeActive = (placeActive + 1) % items.length;
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    placeActive = (placeActive - 1 + items.length) % items.length;
  } else if (e.key === "Enter" && placeActive >= 0) {
    e.preventDefault();
    placeInput.value = items[placeActive].dataset.label;
    placeList.hidden = true;
    return;
  } else if (e.key === "Escape") {
    placeList.hidden = true;
    return;
  } else {
    return;
  }
  items.forEach((li, i) => li.classList.toggle("active", i === placeActive));
});
placeInput?.addEventListener("blur", () => setTimeout(() => {
  if (placeList) placeList.hidden = true;
}, 120));

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
let browseGalleryView = "thumbs";
let browseItems = [];
let browseSelected = new Set();
let browseSort = { key: "taken_at", dir: "desc" };

const BROWSE_VIEW_KEY = "fotoman.browseView";
const BROWSE_GALLERY_KEY = "fotoman.browseGalleryView";
const VALID_BROWSE_VIEWS = new Set([
  "folders",
  "date",
  "camera",
  "location",
  "people",
  "bundles",
  "remove",
]);
const VALID_GALLERY_VIEWS = new Set(["thumbs", "cards", "details"]);

function loadBrowsePrefs() {
  try {
    const view = localStorage.getItem(BROWSE_VIEW_KEY);
    if (view && VALID_BROWSE_VIEWS.has(view)) browseView = view;
    const gallery = localStorage.getItem(BROWSE_GALLERY_KEY);
    if (gallery && VALID_GALLERY_VIEWS.has(gallery)) browseGalleryView = gallery;
  } catch (_) {
    /* ignore */
  }
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
  document.querySelectorAll(".browse-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === browseView);
  });
  document.querySelectorAll(".browse-view-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.galleryView === browseGalleryView);
  });
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

function sortedBrowseItems() {
  const { key, dir } = browseSort;
  const mult = dir === "asc" ? 1 : -1;
  return [...browseItems].sort((a, b) => {
    const av = browseSortValue(a, key);
    const bv = browseSortValue(b, key);
    if (av < bv) return -1 * mult;
    if (av > bv) return 1 * mult;
    return (basename(a.path) || "").localeCompare(basename(b.path) || "");
  });
}

function browseVisibleKeys() {
  return browseItems.map(browseItemKey);
}

function allBrowseVisibleSelected() {
  const keys = browseVisibleKeys();
  return keys.length > 0 && keys.every((k) => browseSelected.has(k));
}

function updateBrowseSelectionCount() {
  const el = $("#browse-selection-count");
  if (!el) return;
  const n = browseSelected.size;
  el.textContent = n ? `${n} selected` : "";
  const btn = $("#btn-browse-select-all");
  if (!btn) return;
  const hasItems = browseItems.length > 0;
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

function syncBrowseRemoveButtons() {
  const selected = selectedBrowsePhotos();
  const activeIds = selected.filter((p) => p.id != null && !p.culled).map((p) => p.id);
  const removedIds = selected.filter((p) => p.id != null && p.culled).map((p) => p.id);
  const indexedIds = selected.filter((p) => p.id != null).map((p) => p.id);
  const removeBtn = $("#btn-browse-remove");
  const restoreBtn = $("#btn-browse-restore");
  const editBtn = $("#btn-browse-edit");
  if (removeBtn) {
    removeBtn.disabled = activeIds.length === 0;
    removeBtn.textContent = activeIds.length
      ? `Remove selected (${activeIds.length})`
      : "Remove selected";
  }
  if (restoreBtn) {
    restoreBtn.disabled = removedIds.length === 0;
    restoreBtn.textContent = removedIds.length
      ? `Restore selected (${removedIds.length})`
      : "Restore selected";
  }
  if (editBtn) {
    editBtn.disabled = indexedIds.length === 0;
    editBtn.textContent = indexedIds.length
      ? `Edit properties (${indexedIds.length})`
      : "Edit properties";
  }
}

function toggleBrowseSelectAll() {
  if (!browseItems.length) return;
  if (allBrowseVisibleSelected()) {
    browseSelected.clear();
  } else {
    browseVisibleKeys().forEach((k) => browseSelected.add(k));
  }
  renderBrowseResults();
}

const FACE_COLORS = [
  "#e4572e",
  "#17bebb",
  "#ffc914",
  "#76b041",
  "#4b8bbe",
  "#9b5de5",
  "#f15bb5",
  "#00bbf9",
  "#fee440",
  "#00f5d4",
];

let lightboxFaces = [];
let lightboxFallbackFace = null;
let lightboxFaceLoadGen = 0;

function faceColor(key) {
  const n = Number(key);
  const i = Number.isFinite(n) ? Math.abs(Math.trunc(n)) : 0;
  return FACE_COLORS[i % FACE_COLORS.length];
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
}

function clearLightboxInfo() {
  lightboxFaceLoadGen += 1;
  const info = $("#lightbox-info");
  const meta = $("#lightbox-meta");
  const list = $("#lightbox-face-list");
  const framesOpt = $("#lightbox-frames-opt");
  if (meta) meta.innerHTML = "";
  if (list) {
    list.innerHTML = "";
    list.hidden = true;
  }
  if (framesOpt) framesOpt.hidden = true;
  if (info) info.hidden = true;
  lightboxFaces = [];
  lightboxFallbackFace = null;
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
  if (p.lat != null && p.lon != null) {
    rows.push(["GPS", `${Number(p.lat).toFixed(5)}, ${Number(p.lon).toFixed(5)}`]);
  }
  if (p.score != null) rows.push(["Score", Number(p.score).toFixed(3)]);
  if (p.distance_km != null) rows.push(["Distance", `${p.distance_km} km`]);
  return rows;
}

function renderLightboxFaceList(faces) {
  const list = $("#lightbox-face-list");
  if (!list) return;
  const labeled = (faces || []).filter((f) => f.labeled);
  if (!labeled.length) {
    list.innerHTML = "";
    list.hidden = true;
    return;
  }
  const seen = new Set();
  const items = [];
  for (const f of labeled) {
    const key = f.person_id ?? f.id;
    if (seen.has(key)) continue;
    seen.add(key);
    const color = faceColor(key);
    items.push(
      `<li><span class="face-dot" style="background:${color}"></span>${escapeHtml(f.display_name)}</li>`
    );
  }
  list.innerHTML = items.join("");
  list.hidden = false;
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

function drawLightboxFaceBoxes() {
  const layer = $("#lightbox-faces");
  if (!layer) return;
  const showFrames = $("#lightbox-show-frames")?.checked;
  if (!showFrames) {
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
  layer.hidden = false;
  layer.innerHTML = faces
    .map((f) => {
      if (f.bbox_w == null) return "";
      const color = faceColor(f.person_id ?? f.id ?? 0);
      return `<span class="face-box" style="left:${f.bbox_x * 100}%;top:${f.bbox_y * 100}%;width:${f.bbox_w * 100}%;height:${f.bbox_h * 100}%;border-color:${color}"></span>`;
    })
    .join("");
}

function syncLightboxFramesOpt() {
  const opt = $("#lightbox-frames-opt");
  if (!opt) return;
  const has =
    lightboxFaces.length > 0 ||
    (lightboxFallbackFace && lightboxFallbackFace.bbox_w != null);
  opt.hidden = !has;
  if (!has) {
    const cb = $("#lightbox-show-frames");
    if (cb) cb.checked = false;
  }
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
  if (faces.some((f) => f.labeled)) {
    const info = $("#lightbox-info");
    if (info) info.hidden = false;
  }
  syncLightboxFramesOpt();
  renderLightboxFaceList(faces);
  drawLightboxFaceBoxes();
}

let lightboxGroup = [];
let lightboxIndex = -1;

function lightboxNavigateTo(idx) {
  if (idx < 0 || idx >= lightboxGroup.length) return;
  lightboxIndex = idx;
  const entry = lightboxGroup[idx];
  const photo = entry.photo || null;
  const src = entry.src || "";
  clearLightboxFaces();
  clearLightboxInfo();
  lightboxFallbackFace = entry.face || null;
  setLightboxOrientation(entry.orientation ?? photo?.orientation);
  renderLightboxMeta(photo);
  if (photo) $("#lightbox-info").hidden = false;
  const framesCb = $("#lightbox-show-frames");
  if (framesCb) framesCb.checked = false;
  syncLightboxFramesOpt();
  const img = $("#lightbox-img");
  img.onload = null;
  img.src = src;
  const afterLoad = () => { drawLightboxFaceBoxes(); };
  if (img.complete && img.naturalWidth) afterLoad();
  else img.onload = afterLoad;
  loadLightboxFaces(photo);
  syncLightboxNav();
}

function syncLightboxNav() {
  const prev = $("#lightbox-prev");
  const next = $("#lightbox-next");
  if (prev) prev.hidden = lightboxGroup.length < 2 || lightboxIndex <= 0;
  if (next) next.hidden = lightboxGroup.length < 2 || lightboxIndex >= lightboxGroup.length - 1;
}

function openLightbox({ src, face = null, orientation = 0, photo = null, group = [], groupIndex = -1 } = {}) {
  lightboxGroup = group;
  lightboxIndex = groupIndex;
  const img = $("#lightbox-img");
  clearLightboxFaces();
  clearLightboxInfo();
  lightboxFallbackFace = face;
  setLightboxOrientation(orientation ?? photo?.orientation);
  renderLightboxMeta(photo);
  if (photo) $("#lightbox-info").hidden = false;
  const framesCb = $("#lightbox-show-frames");
  if (framesCb) framesCb.checked = false;
  syncLightboxFramesOpt();
  img.onload = null;
  img.src = src || "";
  const afterLoad = () => {
    drawLightboxFaceBoxes();
  };
  if (img.complete && img.naturalWidth) afterLoad();
  else img.onload = afterLoad;
  loadLightboxFaces(photo);
  syncLightboxNav();
  $("#lightbox").showModal();
}

function openBrowseLightbox(original, face, orientation, photo, group = [], groupIndex = -1) {
  openLightbox({ src: original, face, orientation, photo, group, groupIndex });
}

function syncBrowseFrameVisibility() {
  const root = $("#browse-results");
  if (!root) return;
  const show = $("#browse-show-frame")?.checked !== false;
  root.classList.toggle("hide-face-frames", browseView === "people" && !show);
}

function bindBrowseItemInteractions(root) {
  root.querySelectorAll("[data-browse-key]").forEach((el) => {
    el.addEventListener("click", (e) => {
      if (e.target.closest("th")) return;
      if (e.detail > 1) return;
      const key = el.dataset.browseKey;
      if (browseSelected.has(key)) browseSelected.delete(key);
      else browseSelected.add(key);
      el.classList.toggle("selected", browseSelected.has(key));
      updateBrowseSelectionCount();
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
        return { src: m.original, orientation: orientationDeg(p), photo: p };
      });
      openBrowseLightbox(el.dataset.original, face, el.dataset.orientation, photo, group, idx);
    });
  });
}

function showRemoveBin() {
  return $("#browse-show-remove")?.checked || false;
}

function removeQuery() {
  return showRemoveBin() ? "&include_culled=true" : "";
}

function faceBoxAttrs(p) {
  if (p.bbox_w == null || p.bbox_h == null) return "";
  return ` data-bbox-x="${p.bbox_x}" data-bbox-y="${p.bbox_y}" data-bbox-w="${p.bbox_w}" data-bbox-h="${p.bbox_h}"`;
}

function faceBoxOverlay(p) {
  if (p.bbox_w == null || p.bbox_h == null) return "";
  return `<span class="face-box" style="left:${p.bbox_x * 100}%;top:${p.bbox_y * 100}%;width:${p.bbox_w * 100}%;height:${p.bbox_h * 100}%"></span>`;
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

function renderBrowseThumbs(items) {
  return items
    .map((p) => {
      const media = browseMediaUrls(p);
      const key = browseItemKey(p);
      const removed = !!p.culled;
      const sel = browseSelected.has(key) ? " selected" : "";
      const hasFace = p.bbox_w != null ? " has-face" : "";
      const path = photoPath(p);
      return `
      <article class="card${removed ? " removed" : ""}${sel}" data-browse-key="${key}" data-original="${media.original}" data-orientation="${orientationDeg(p)}"${faceBoxAttrs(p)}>
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
    })
    .join("");
}

function renderBrowseCards(items) {
  return `<div class="browse-thumbs">${items
    .map((p) => {
      const media = browseMediaUrls(p);
      const key = browseItemKey(p);
      const removed = !!p.culled;
      const sel = browseSelected.has(key) ? " selected" : "";
      const hasFace = p.bbox_w != null ? " has-face" : "";
      const name = basename(p.path);
      const path = photoPath(p);
      const rest = browseItemDetailBits(p).filter((b) => b !== path);
      return `
      <article class="browse-thumb${removed ? " removed" : ""}${sel}" data-browse-key="${key}" data-original="${media.original}" data-orientation="${orientationDeg(p)}"${faceBoxAttrs(p)}>
        <div class="browse-thumb-media${hasFace}">
          ${browseMediaInner(p, media)}
          ${removed ? `<span class="removed-badge">REMOVE</span>` : ""}
        </div>
        <div class="browse-thumb-detail">
          <strong>${name}</strong>
          ${path ? `<span class="card-hover-path">${path}</span>` : ""}
          <span>${rest.join(" · ")}</span>
        </div>
      </article>`;
    })
    .join("")}</div>`;
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
  const rows = items
    .map((p) => {
      const media = browseMediaUrls(p);
      const key = browseItemKey(p);
      const removed = !!p.culled;
      const sel = browseSelected.has(key) ? " selected" : "";
      const path = photoPath(p);
      return `
      <tr class="${removed ? "removed " : ""}${sel}" data-browse-key="${key}" data-original="${media.original}" data-orientation="${orientationDeg(p)}"${faceBoxAttrs(p)}>
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
    })
    .join("");
  return `<div class="browse-details-wrap"><table class="browse-details">
    <thead><tr>${head}</tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}

function renderBrowseResults() {
  const root = $("#browse-results");
  root.className =
    browseGalleryView === "thumbs"
      ? "grid"
      : browseGalleryView === "cards"
        ? "browse-results-cards"
        : "browse-results-details";
  if (!browseItems.length) {
    root.innerHTML =
      browseView === "remove"
        ? `<p class="muted">Remove bin is empty.</p>`
        : `<p class="muted">No photos in this group.</p>`;
    updateBrowseSelectionCount();
    return;
  }
  const items = sortedBrowseItems();
  if (browseGalleryView === "cards") root.innerHTML = renderBrowseCards(items);
  else if (browseGalleryView === "details") root.innerHTML = renderBrowseDetails(items);
  else root.innerHTML = renderBrowseThumbs(items);

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
}

function renderBrowseTree(nodes, container, groupBy, depth = 0) {
  nodes.forEach((node) => {
    const isFile = !!node.is_file;
    const item = document.createElement("div");
    item.className =
      "browse-item" +
      (node.children ? " has-children" : "") +
      (isFile ? " is-file" : "");
    item.dataset.path = node.path;
    item.style.paddingLeft = `${depth * 0.8 + 0.4}rem`;
    const countHtml = isFile ? "" : `<span class="browse-item-count">${node.count}</span>`;
    item.innerHTML = `<span class="browse-item-name">${node.children ? "▸ " : ""}${node.name}</span>
      ${countHtml}`;
    item.addEventListener("click", (e) => {
      e.stopPropagation();
      // Clear selection only within this tree root's sidebar
      const root = container.closest("#browse-groups") || container;
      root.querySelectorAll(".browse-item").forEach((i) => i.classList.remove("selected"));
      item.classList.add("selected");
      if (node.children) {
        const arrow = item.querySelector(".browse-item-name");
        const sub = item.nextElementSibling;
        if (sub && sub.classList.contains("browse-subtree") && !sub.hidden) {
          sub.hidden = true;
          arrow.textContent = `▸ ${node.name}`;
        } else if (sub && sub.classList.contains("browse-subtree")) {
          sub.hidden = false;
          arrow.textContent = `▾ ${node.name}`;
        }
      }
      loadBrowsePhotos(groupBy, node.path);
    });
    container.appendChild(item);
    if (node.children) {
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
  sidebar.querySelectorAll(".browse-item").forEach((i) => i.classList.remove("selected"));
  item.classList.add("selected");
  let el = item.parentElement;
  while (el && el !== sidebar) {
    if (el.classList.contains("browse-subtree")) {
      el.hidden = false;
      const parentItem = el.previousElementSibling;
      const nameEl = parentItem?.querySelector?.(".browse-item-name");
      if (nameEl && nameEl.textContent.startsWith("▸ ")) {
        nameEl.textContent = `▾ ${nameEl.textContent.slice(2)}`;
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
      input.title = `Person ${updated.id}`;
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
          await renderPeopleList();
          const kept = $("#browse-groups")?.querySelector(
            `.person-label[data-person="${updated.id}"]`
          );
          const item = kept?.closest(".browse-item");
          if (item) {
            $("#browse-groups")
              .querySelectorAll(".browse-item")
              .forEach((i) => i.classList.remove("selected"));
            item.classList.add("selected");
          }
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

async function renderPeopleList() {
  const sidebar = $("#browse-groups");
  const people = await api("/api/browse/people");
  sidebar.innerHTML = "";
  if (!people.length) {
    sidebar.innerHTML = `<p class="muted">No people yet. Run Faces Go on an archive.</p>`;
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
    const faceSrc = p.cover_face_id
      ? `/api/faces/${p.cover_face_id}/thumb`
      : "";
    item.innerHTML = `
      ${faceSrc ? `<img class="person-face" src="${faceSrc}" alt="" loading="lazy" draggable="false" />` : `<span class="person-face person-face-empty"></span>`}
      <input class="person-label" type="text" value="${esc(p.display_name)}"
        data-person="${p.id}" title="Person ${p.id}" aria-label="Person name" />
      <span class="browse-item-count">${p.photo_count}</span>`;
    item.addEventListener("click", (e) => {
      if (e.target.closest(".person-label")) return;
      sidebar.querySelectorAll(".browse-item").forEach((i) => i.classList.remove("selected"));
      item.classList.add("selected");
      loadBrowsePhotos("person", String(p.id));
    });
    bindPersonLabel(item.querySelector(".person-label"));
    sidebar.appendChild(item);
  });
}

async function pollFacesStatus() {
  try {
    const st = await api("/api/faces/status");
    lastFacesStatus = st;
    const bar = $("#index-status");
    if (bar) bar.textContent = indexStatusText(lastIndexStatus, lastSources, st);
    applyIndexStatusToSources(lastIndexStatus, lastSources, st);
    if (!st.running && browseView === "people") renderPeopleList();
  } catch {
    /* ignore */
  }
}

async function loadBrowseView() {
  const sidebar = $("#browse-groups");
  const results = $("#browse-results");
  const filesOpt = $("#browse-files-opt");
  const frameOpt = $("#browse-frame-opt");
  const removeOpt = $("#browse-remove-opt");
  sidebar.innerHTML = `<p class="muted">Loading…</p>`;
  browseItems = [];
  browseSelected.clear();
  results.innerHTML = "";
  updateBrowseSelectionCount();
  filesOpt.hidden = browseView !== "folders";
  if (frameOpt) frameOpt.hidden = browseView !== "people";
  if (removeOpt) removeOpt.hidden = browseView === "remove";
  syncBrowseFrameVisibility();

  if (browseView === "remove") {
    sidebar.innerHTML = `<p class="muted">Photos in the remove bin.</p>`;
    await loadBrowsePhotos("remove", "all");
    return;
  }

  if (browseView === "folders") {
    const showFiles = $("#browse-show-files").checked;
    const tree = await api(
      `/api/browse/tree?include_files=${showFiles ? "true" : "false"}${removeQuery()}`
    );
    sidebar.innerHTML = "";
    if (!tree.length) {
      sidebar.innerHTML = `<p class="muted">No archives or photos found.</p>`;
      return;
    }
    renderBrowseTree(tree, sidebar, "folder");
  } else if (browseView === "date") {
    const tree = await api(`/api/browse/date-tree?${removeQuery().slice(1)}`);
    sidebar.innerHTML = "";
    if (!tree.length) {
      sidebar.innerHTML = `<p class="muted">No dated photos found.</p>`;
      return;
    }
    renderBrowseTree(tree, sidebar, "date");
  } else if (browseView === "location") {
    const tree = await api(`/api/browse/location-tree?${removeQuery().slice(1)}`);
    sidebar.innerHTML = "";
    if (!tree.length) {
      sidebar.innerHTML = `<p class="muted">No places. Index GPS or folder-named places first.</p>`;
      return;
    }
    renderBrowseTree(tree, sidebar, "location");
  } else if (browseView === "people") {
    await renderPeopleList();
  } else if (browseView === "bundles") {
    const bundles = await api(`/api/browse/bundles?${removeQuery().slice(1)}`);
    sidebar.innerHTML = "";
    if (!bundles.length) {
      sidebar.innerHTML = `<p class="muted">No bundles. Run Find bundles first.</p>`;
      return;
    }
    bundles.forEach((b) => {
      const item = document.createElement("div");
      item.className = "browse-item";
      item.innerHTML = `<span class="browse-item-name">${b.kind} #${b.id}</span>
        <span class="browse-item-count">${b.member_count}</span>`;
      item.addEventListener("click", () => {
        sidebar.querySelectorAll(".browse-item").forEach((i) => i.classList.remove("selected"));
        item.classList.add("selected");
        loadBrowsePhotos("bundle", String(b.id));
      });
      sidebar.appendChild(item);
    });
  } else {
    const groups = await api(`/api/browse/groups?group_by=${browseView}${removeQuery()}`);
    sidebar.innerHTML = "";
    if (!groups.length) {
      sidebar.innerHTML = `<p class="muted">No data for this view.</p>`;
      return;
    }
    groups.forEach((g) => {
      const item = document.createElement("div");
      item.className = "browse-item";
      item.innerHTML = `<span class="browse-item-name">${g.key}</span>
        <span class="browse-item-count">${g.count}</span>`;
      item.addEventListener("click", () => {
        sidebar.querySelectorAll(".browse-item").forEach((i) => i.classList.remove("selected"));
        item.classList.add("selected");
        loadBrowsePhotos(browseView, g.key);
      });
      sidebar.appendChild(item);
    });
  }
}

async function loadBrowsePhotos(groupBy, key) {
  browseActiveKey = key;
  browseSelected.clear();
  const results = $("#browse-results");
  results.innerHTML = `<p class="muted">Loading…</p>`;
  updateBrowseSelectionCount();
  let q = `group_by=${encodeURIComponent(groupBy)}&key=${encodeURIComponent(key)}${removeQuery()}`;
  if (groupBy === "remove") {
    q = `group_by=remove&key=all&limit=500`;
  } else if (groupBy === "folder") {
    const showSub = $("#browse-show-files")?.checked;
    q += `&include_subfolders=${showSub ? "true" : "false"}`;
  }
  browseItems = await api(`/api/browse/photos?${q}`);
  renderBrowseResults();
}

document.querySelectorAll(".browse-view-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".browse-view-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    browseGalleryView = btn.dataset.galleryView;
    saveBrowseGalleryPref();
    renderBrowseResults();
  });
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

function collectEditProperties(form) {
  /** Build extendible properties payload from named form fields. */
  const props = {};
  const taken = form.elements.namedItem("taken_at");
  if (taken && taken.value) props.taken_at = taken.value;
  const place = form.elements.namedItem("place_label");
  if (place && place.value.trim()) props.place_label = place.value.trim();
  const orient = form.elements.namedItem("orientation");
  if (orient && orient.value !== "") props.orientation = Number(orient.value);
  return props;
}

function openEditPropertiesModal() {
  const photos = selectedBrowsePhotos().filter((p) => p.id != null);
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
  form.elements.namedItem("orientation").value = "";
  dlg.showModal();
}

function browseGroupBy() {
  return browseView === "folders"
    ? "folder"
    : browseView === "bundles"
      ? "bundle"
      : browseView === "people"
        ? "person"
        : browseView;
}

async function reloadBrowseGallery() {
  if (browseView === "remove") {
    await loadBrowsePhotos("remove", "all");
  } else if (browseActiveKey) {
    await loadBrowsePhotos(browseGroupBy(), browseActiveKey);
  } else {
    renderBrowseResults();
  }
}

async function reloadBrowseAfterPropertyEdit(props) {
  const treeAffecting =
    (browseView === "date" && "taken_at" in props) ||
    (browseView === "location" && "place_label" in props);
  if (!treeAffecting) {
    await reloadBrowseGallery();
    return;
  }
  const key = browseActiveKey;
  await loadBrowseView();
  if (!key) return;
  if (selectBrowseTreePath(key)) {
    await loadBrowsePhotos(browseGroupBy(), key);
  } else {
    browseActiveKey = null;
    browseItems = [];
    browseSelected.clear();
    const results = $("#browse-results");
    if (results) results.innerHTML = `<p class="muted">This group no longer has photos.</p>`;
    updateBrowseSelectionCount();
  }
}

$("#btn-browse-edit")?.addEventListener("click", () => {
  openEditPropertiesModal();
});

$("#btn-edit-properties-cancel")?.addEventListener("click", () => {
  $("#edit-properties")?.close();
});

$("#edit-properties-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  const ids = selectedBrowsePhotos()
    .filter((p) => p.id != null)
    .map((p) => p.id);
  if (!ids.length) return;
  const props = collectEditProperties(form);
  const err = $("#edit-properties-error");
  const saveBtn = $("#btn-edit-properties-save");
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
    $("#edit-properties")?.close();
    await reloadBrowseAfterPropertyEdit(props);
  } catch (ex) {
    err.textContent = ex.message || "Update failed";
    err.hidden = false;
  } finally {
    saveBtn.disabled = false;
  }
});

$("#btn-browse-remove")?.addEventListener("click", async () => {
  const ids = selectedBrowsePhotos()
    .filter((p) => p.id != null && !p.culled)
    .map((p) => p.id);
  if (!ids.length) return;
  await removePhotoIds(ids);
  browseSelected.clear();
  refreshStats();
  await reloadBrowseGallery();
});

$("#btn-browse-restore")?.addEventListener("click", async () => {
  const ids = selectedBrowsePhotos()
    .filter((p) => p.id != null && p.culled)
    .map((p) => p.id);
  if (!ids.length) return;
  await restorePhotoIds(ids);
  browseSelected.clear();
  refreshStats();
  await reloadBrowseGallery();
});

document.querySelectorAll(".browse-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".browse-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    browseView = btn.dataset.view;
    browseActiveKey = null;
    saveBrowseViewPref();
    loadBrowseView();
  });
});

$("#browse-show-files")?.addEventListener("change", async () => {
  if (browseView !== "folders") return;
  const key = browseActiveKey;
  await loadBrowseView();
  if (key) loadBrowsePhotos("folder", key);
});

$("#browse-show-remove")?.addEventListener("change", () => {
  if (browseView === "remove") return;
  loadBrowseView();
  if (browseActiveKey) {
    const by =
      browseView === "folders"
        ? "folder"
        : browseView === "bundles"
          ? "bundle"
          : browseView === "people"
            ? "person"
            : browseView;
    loadBrowsePhotos(by, browseActiveKey);
  }
});

$("#browse-show-frame")?.addEventListener("change", () => {
  syncBrowseFrameVisibility();
});

$("#lightbox-show-frames")?.addEventListener("change", () => {
  drawLightboxFaceBoxes();
});

$("#lightbox-prev")?.addEventListener("click", () => lightboxNavigateTo(lightboxIndex - 1));
$("#lightbox-next")?.addEventListener("click", () => lightboxNavigateTo(lightboxIndex + 1));
$("#lightbox")?.addEventListener("keydown", (e) => {
  if (e.key === "ArrowLeft") { e.preventDefault(); lightboxNavigateTo(lightboxIndex - 1); }
  else if (e.key === "ArrowRight") { e.preventDefault(); lightboxNavigateTo(lightboxIndex + 1); }
});

loadDupeIncludeRemovedPref();
try {
  activateTab(localStorage.getItem(TAB_STORAGE_KEY) || "sources");
} catch (_) {
  activateTab("sources");
}
