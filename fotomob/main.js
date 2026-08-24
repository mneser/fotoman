import { WebSocketServer } from 'ws';

const MAX_PAYLOAD = 80 * 1024 * 1024;
const META_TIMEOUT_MS = 12_000;
const SEARCH_TIMEOUT_MS = 30_000;
const THUMB_TIMEOUT_MS = 20_000;
const ORIGINAL_TIMEOUT_MS = 60_000;

function envFlag(name, fallback = false) {
  const raw = String(process.env[name] ?? '').trim().toLowerCase();
  if (['1', 'true', 'yes', 'on'].includes(raw)) return true;
  if (['0', 'false', 'no', 'off'].includes(raw)) return false;
  return fallback;
}

function stripShareFields(node) {
  const out = { ...node, children: (node.children || []).map(stripShareFields) };
  delete out.share_code;
  delete out.share_url;
  return out;
}

function applySubfolderSharePolicy(nodes, allowNested) {
  if (allowNested) return nodes;
  return (nodes || []).map((n) => ({
    ...n,
    children: (n.children || []).map(stripShareFields),
  }));
}

/**
 * Fotomob: mobile gallery over a WebSocket tunnel to Fotoman providers.
 * Standalone relay. nginx should proxy /fotomob here.
 */
export function createFotomobHandlers({ basePath = '/fotomob' } = {}) {
  const prefix = basePath.replace(/\/$/, '') || '/fotomob';
  const wsPath = `${prefix}/provider`;
  const deviceWsPath = `${prefix}/device`;

  /** @type {Map<string, { name: string, socket: import('ws').WebSocket, pending: Map<string, any>, nextId: number }>} */
  const providers = new Map();
  /** @type {Map<string, { pair: string, socket: import('ws').WebSocket, provider: string | null, pending: Map<string, any> }>} */
  const devices = new Map();

  function safeName(name) {
    const n = String(name || '').trim().slice(0, 64);
    return n || 'fotoman';
  }

  function sendJson(res, status, payload) {
    res.writeHead(status, {
      'Content-Type': 'application/json',
      'Cache-Control': 'no-cache',
    });
    res.end(JSON.stringify(payload));
  }

  function queryOf(req) {
    return new URL(req.url || '/', 'http://localhost').searchParams;
  }

  function normalizePath(req) {
    const raw = (req.url || '/').split('?')[0];
    let urlPath = raw === '' ? '/' : raw;
    if (urlPath === prefix || urlPath.startsWith(`${prefix}/`)) {
      urlPath = urlPath.slice(prefix.length) || '/';
    }
    return urlPath;
  }

  function dropProvider(name, socket) {
    const cur = providers.get(name);
    if (cur && (!socket || cur.socket === socket)) {
      providers.delete(name);
      for (const [, wait] of cur.pending) {
        clearTimeout(wait.timer);
        const err = new Error('Fotoman disconnected');
        err.status = 503;
        wait.reject(err);
      }
    }
  }

  function providerCall(name, method, params = {}, timeoutMs = META_TIMEOUT_MS) {
    return new Promise((resolve, reject) => {
      const p = providers.get(name);
      if (!p || p.socket.readyState !== 1) {
        const err = new Error(name ? `Fotoman “${name}” is not connected` : 'Fotoman is not connected');
        err.status = 503;
        reject(err);
        return;
      }
      const id = String(p.nextId++);
      const timer = setTimeout(() => {
        p.pending.delete(id);
        const err = new Error('Fotoman timed out');
        err.status = 504;
        reject(err);
      }, timeoutMs);
      p.pending.set(id, { resolve, reject, timer });
      p.socket.send(JSON.stringify({ id, op: 'req', method, params }));
    });
  }

  function stampProvider(nodes, provider) {
    return (nodes || []).map((n) => ({
      ...n,
      provider,
      children: stampProvider(n.children || [], provider),
    }));
  }

  function shareTokens(q) {
    const parts = [];
    const add = (value) => {
      String(value || '')
        .split(/[,\s]+/)
        .forEach((t) => {
          const s = t.trim();
          if (s) parts.push(s);
        });
    };
    q.getAll('share').forEach(add);
    return [...new Set(parts)];
  }

  function shareParam(q) {
    return shareTokens(q).join(',');
  }

  function albumPhoto(p, provider, share) {
    const id = Number(p.id);
    const q = new URLSearchParams({ provider });
    if (share) q.set('share', share);
    const qs = q.toString();
    const personIds = Array.isArray(p.person_ids)
      ? p.person_ids.map((x) => Number(x)).filter(Number.isFinite)
      : [];
    const lat = Number(p.lat);
    const lon = Number(p.lon);
    return {
      id,
      provider,
      taken_at: p.taken_at || null,
      place_label: p.place_label || p.city || p.venue || null,
      city: p.city || null,
      region: p.region || null,
      country: p.country || null,
      lat: Number.isFinite(lat) ? lat : null,
      lon: Number.isFinite(lon) ? lon : null,
      person_ids: personIds,
      thumb: `/thumb/${id}?${qs}`,
      original: `/original/${id}?${qs}`,
    };
  }

  function connectedNames() {
    return [...providers.keys()].sort((a, b) => a.localeCompare(b));
  }

  function resolveProviders(q) {
    const wanted = safeName(q.get('provider') || '');
    if (q.get('provider')) {
      return providers.has(wanted) ? [wanted] : [];
    }
    return connectedNames();
  }

  function isApiRequest(req) {
    const urlPath = normalizePath(req);
    if (
      urlPath === '/api/status' ||
      urlPath === '/api/tree' ||
      urlPath === '/api/album' ||
      urlPath === '/api/people' ||
      urlPath === '/api/search' ||
      urlPath === '/api/device/status' ||
      urlPath === '/api/device/put'
    ) {
      return true;
    }
    if (
      /^\/thumb\/\d+$/.test(urlPath) ||
      /^\/original\/\d+$/.test(urlPath) ||
      /^\/face\/\d+$/.test(urlPath)
    ) {
      return true;
    }
    return false;
  }

  function handleStatus(_req, res) {
    const names = connectedNames();
    sendJson(res, 200, { live: names.length > 0, providers: names });
  }

  async function handleTree(req, res) {
    const names = connectedNames();
    if (!names.length) {
      sendJson(res, 503, { error: 'No Fotoman providers connected', live: false });
      return;
    }
    const share = shareParam(queryOf(req));
    const results = await Promise.all(
      names.map(async (name) => {
        try {
          const msg = await providerCall(name, 'tree', { share });
          if (!msg.share_ok) return { kind: 'empty' };
          const children = applySubfolderSharePolicy(
            stampProvider(msg.body || [], name),
            envFlag('FOTOMOB_SUBFOLDER_SHARE'),
          );
          if (!children.length) return { kind: 'empty' };
          const count = children.reduce((n, c) => n + (Number(c.count) || 0), 0);
          return {
            kind: 'tree',
            node: {
              id: name,
              name,
              provider: name,
              kind: 'provider',
              count,
              children,
            },
          };
        } catch {
          return { kind: 'error' };
        }
      })
    );
    const trees = results.filter((r) => r.kind === 'tree').map((r) => r.node);
    if (trees.length) {
      sendJson(res, 200, trees);
      return;
    }
    if (results.some((r) => r.kind === 'empty')) {
      sendJson(res, 200, []);
      return;
    }
    sendJson(res, 504, { error: 'Fotoman timed out', live: false });
  }

  async function handleAlbum(req, res) {
    const q = queryOf(req);
    const provider = safeName(q.get('provider') || '');
    const key = String(
      q.get('collection_id') || q.get('collection') || q.get('folder_id') || q.get('vfolder') || ''
    ).trim();
    if (!provider || !providers.has(provider)) {
      sendJson(res, 503, { error: 'Fotoman provider not connected', live: false });
      return;
    }
    if (!key) {
      sendJson(res, 400, { error: 'Collection required' });
      return;
    }
    const share = shareParam(q);
    const params = { top_k: 10000, include_subfolders: true, share };
    if (/^\d+$/.test(key)) params.collection_id = Number(key);
    else params.collection = key;
    try {
      const msg = await providerCall(provider, 'album', params, 20_000);
      const photos = Array.isArray(msg.body) ? msg.body.map((p) => albumPhoto(p, provider, share)) : [];
      sendJson(res, 200, {
        provider,
        folder_id: params.collection_id || key,
        count: photos.length,
        photos,
        live: true,
      });
    } catch (err) {
      sendJson(res, err.status || 502, { error: err.message || 'Tunnel error' });
    }
  }

  async function handlePeople(req, res) {
    const q = queryOf(req);
    const names = resolveProviders(q);
    if (!names.length) {
      sendJson(res, 503, { error: 'Fotoman provider not connected', live: false });
      return;
    }
    const labeled = q.get('labeled') == null ? true : q.get('labeled') !== 'false';
    const share = shareParam(q);
    const groups = (
      await Promise.all(
        names.map(async (name) => {
          try {
            const msg = await providerCall(name, 'people', { labeled, share });
            const people = Array.isArray(msg.body)
              ? msg.body.map((p) => {
                  const cover = Number(p.cover_face_id);
                  return {
                    id: Number(p.id),
                    provider: name,
                    name: p.name || null,
                    display_name: p.display_name || p.name || `Person ${p.id}`,
                    photo_count: Number(p.photo_count) || 0,
                    cover_face_id: Number.isFinite(cover) && cover > 0 ? cover : null,
                  };
                })
              : [];
            return { provider: name, people };
          } catch {
            return null;
          }
        })
      )
    ).filter(Boolean);
    if (!groups.length) {
      sendJson(res, 504, { error: 'Fotoman timed out', live: false });
      return;
    }
    sendJson(res, 200, { live: true, providers: names, groups });
  }

  async function handleSearch(req, res) {
    const q = queryOf(req);
    const names = resolveProviders(q);
    if (!names.length) {
      sendJson(res, 503, { error: 'Fotoman provider not connected', live: false });
      return;
    }
    const text = String(q.get('q') || '').trim();
    const mode = String(q.get('mode') || '').trim() || (text ? 'visual' : 'meta');
    const personId = q.get('person_id') ? Number(q.get('person_id')) : undefined;
    const params = {
      q: text,
      mode,
      top_k: Math.min(200, Math.max(1, Number(q.get('top_k')) || 80)),
      taken_from: q.get('taken_from') || undefined,
      taken_to: q.get('taken_to') || undefined,
      place: q.get('place') || undefined,
      person_id: personId,
      collection_id: q.get('collection_id') ? Number(q.get('collection_id')) : undefined,
      collection: q.get('collection') || undefined,
      include_subfolders: true,
      share: shareParam(q),
    };
    if (params.person_id != null && !Number.isFinite(params.person_id)) {
      sendJson(res, 400, { error: 'Invalid person' });
      return;
    }
    const results = await Promise.all(
      names.map(async (name) => {
        try {
          const msg = await providerCall(name, 'search', params, SEARCH_TIMEOUT_MS);
          return { ok: true, photos: Array.isArray(msg.body) ? msg.body : [] };
        } catch {
          return { ok: false, photos: [] };
        }
      })
    );
    if (!results.some((r) => r.ok)) {
      sendJson(res, 504, { error: 'Fotoman timed out', live: false });
      return;
    }
    const photos = [];
    for (let i = 0; i < names.length; i++) {
      for (const p of results[i].photos) photos.push(albumPhoto(p, names[i], params.share));
    }
    sendJson(res, 200, { live: true, count: photos.length, photos, mode });
  }

  async function imageFromTunnel(method, provider, id, share, res, idKey = 'photo_id', timeoutMs = THUMB_TIMEOUT_MS) {
    try {
      const msg = await providerCall(provider, method, { [idKey]: id, share }, timeoutMs);
      const buf = Buffer.from(msg.body || '', 'base64');
      res.writeHead(200, {
        'Content-Type': msg.contentType || 'image/jpeg',
        'Cache-Control': 'public, max-age=31536000, immutable',
      });
      res.end(buf);
    } catch (err) {
      res.writeHead(err.status || 502).end();
    }
  }

  function readJson(req, limit = MAX_PAYLOAD) {
    return new Promise((resolve, reject) => {
      const chunks = [];
      let n = 0;
      req.on('data', (c) => {
        n += c.length;
        if (n > limit) {
          const err = new Error('Payload too large');
          err.status = 413;
          reject(err);
          req.destroy();
          return;
        }
        chunks.push(c);
      });
      req.on('end', () => {
        try {
          resolve(JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}'));
        } catch {
          const err = new Error('Invalid JSON');
          err.status = 400;
          reject(err);
        }
      });
      req.on('error', reject);
    });
  }

  async function handleDeviceStatus(req, res) {
    const q = queryOf(req);
    const pairs = [
      ...new Set(
        q
          .getAll('pair')
          .flatMap((p) => String(p || '').split(','))
          .map((p) => p.trim())
          .filter(Boolean)
      ),
    ];
    const names = connectedNames();
    const found = await Promise.all(
      pairs.map(async (pair) => {
        const hits = await notifyProviders('device_lookup', { pair }, 8000);
        const hit = hits[0];
        const body = (hit && hit.msg && hit.msg.body) || {};
        return {
          pair,
          online: Boolean(hit),
          provider: hit ? hit.name : '',
          label: String(body.label || body.fotoman_name || '').trim(),
        };
      })
    );
    sendJson(res, 200, { live: names.length > 0, providers: names, pairs: found });
  }

  async function handleDevicePut(req, res) {
    let body;
    try {
      body = await readJson(req);
    } catch (err) {
      sendJson(res, err.status || 400, { error: err.message || 'Invalid JSON' });
      return;
    }
    const pair = String(body.pair || '').trim();
    const file = body.file;
    const folder = String(body.folder || '').trim();
    const finalize = Boolean(body.finalize);
    if (!pair || !file || typeof file !== 'object') {
      sendJson(res, 400, { error: 'pair and file required' });
      return;
    }
    try {
      const hits = await notifyProviders(
        'device_put',
        { pair, file, folder, queue_scan: finalize },
        120000
      );
      const hit = hits[0];
      if (!hit) {
        const status = connectedNames().length ? 404 : 503;
        sendJson(res, status, {
          error: status === 404 ? 'Unknown pair code' : 'Fotoman is not connected',
        });
        return;
      }
      sendJson(res, 200, { ok: true, provider: hit.name, ...(hit.msg.body || {}) });
    } catch (err) {
      sendJson(res, err.status || 502, { error: err.message || 'Upload failed' });
    }
  }

  function handleRequest(req, res) {
    const urlPath = normalizePath(req);
    if (req.method === 'GET' && urlPath === '/api/status') {
      handleStatus(req, res);
      return;
    }
    if (req.method === 'GET' && urlPath === '/api/tree') {
      handleTree(req, res);
      return;
    }
    if (req.method === 'GET' && urlPath === '/api/album') {
      handleAlbum(req, res);
      return;
    }
    if (req.method === 'GET' && urlPath === '/api/people') {
      handlePeople(req, res);
      return;
    }
    if (req.method === 'GET' && urlPath === '/api/search') {
      handleSearch(req, res);
      return;
    }
    if (req.method === 'GET' && urlPath === '/api/device/status') {
      handleDeviceStatus(req, res);
      return;
    }
    if (req.method === 'POST' && urlPath === '/api/device/put') {
      handleDevicePut(req, res);
      return;
    }
    const thumb = urlPath.match(/^\/thumb\/(\d+)$/);
    if (req.method === 'GET' && thumb) {
      const q = queryOf(req);
      imageFromTunnel('thumb', safeName(q.get('provider') || ''), Number(thumb[1]), shareParam(q), res);
      return;
    }
    const original = urlPath.match(/^\/original\/(\d+)$/);
    if (req.method === 'GET' && original) {
      const q = queryOf(req);
      imageFromTunnel(
        'original',
        safeName(q.get('provider') || ''),
        Number(original[1]),
        shareParam(q),
        res,
        'photo_id',
        ORIGINAL_TIMEOUT_MS
      );
      return;
    }
    const face = urlPath.match(/^\/face\/(\d+)$/);
    if (req.method === 'GET' && face) {
      const q = queryOf(req);
      imageFromTunnel(
        'face',
        safeName(q.get('provider') || ''),
        Number(face[1]),
        shareParam(q),
        res,
        'face_id'
      );
      return;
    }
    res.writeHead(404).end('Not found');
  }

  function dropDevice(pair, socket) {
    const cur = devices.get(pair);
    if (cur && (!socket || cur.socket === socket)) {
      devices.delete(pair);
      for (const [, wait] of cur.pending) {
        clearTimeout(wait.timer);
        const err = new Error('Phone disconnected');
        err.status = 503;
        wait.reject(err);
      }
      if (cur.provider && providers.has(cur.provider)) {
        providerCall(cur.provider, 'device_bye', { pair }).catch(() => {});
      }
    }
  }

  function notifyProviders(method, params, timeoutMs = 15000) {
    const names = connectedNames();
    return Promise.all(
      names.map((name) =>
        providerCall(name, method, params, timeoutMs)
          .then((msg) => ({ name, msg }))
          .catch(() => null)
      )
    ).then((rows) => rows.filter(Boolean));
  }

  function onProviderConnection(socket) {
    let name = null;
    socket.isAlive = true;
    socket.on('pong', () => {
      socket.isAlive = true;
    });
    const beat = setInterval(() => {
      if (socket.readyState !== 1) {
        clearInterval(beat);
        return;
      }
      if (!socket.isAlive) {
        socket.terminate();
        return;
      }
      socket.isAlive = false;
      try {
        socket.ping();
      } catch {
        socket.terminate();
      }
    }, 20_000);
    socket.on('message', (raw) => {
      let msg;
      try {
        msg = JSON.parse(String(raw));
      } catch {
        return;
      }
      if (msg.op === 'hello') {
        name = safeName(msg.name);
        const prev = providers.get(name);
        if (prev && prev.socket !== socket) {
          try {
            prev.socket.close();
          } catch {
            /* ignore */
          }
          dropProvider(name, prev.socket);
        }
        providers.set(name, { name, socket, pending: new Map(), nextId: 1 });
        console.log('[fotomob] provider connected: %s', name);
        return;
      }
      if (msg.op === 'devreq') {
        const pair = String(msg.pair || '').trim();
        const dev = devices.get(pair);
        if (!dev || dev.socket.readyState !== 1) {
          socket.send(
            JSON.stringify({
              id: msg.id,
              op: 'devres',
              ok: false,
              status: 503,
              error: 'Phone is not connected',
            })
          );
          return;
        }
        const id = String(msg.id);
        const timer = setTimeout(() => {
          dev.pending.delete(id);
          socket.send(
            JSON.stringify({
              id,
              op: 'devres',
              ok: false,
              status: 504,
              error: 'Phone timed out',
            })
          );
        }, 120000);
        dev.pending.set(id, { timer, provider: name });
        dev.socket.send(
          JSON.stringify({
            id,
            op: 'req',
            method: msg.method,
            params: msg.params || {},
          })
        );
        return;
      }
      if (msg.op !== 'res' || msg.id == null || !name) return;
      const p = providers.get(name);
      if (!p) return;
      const wait = p.pending.get(String(msg.id));
      if (!wait) return;
      p.pending.delete(String(msg.id));
      clearTimeout(wait.timer);
      if (!msg.ok) {
        const err = new Error(msg.error || 'Fotoman error');
        err.status = msg.status || 500;
        wait.reject(err);
        return;
      }
      wait.resolve(msg);
    });
    socket.on('close', () => {
      clearInterval(beat);
      if (name) {
        dropProvider(name, socket);
        console.log('[fotomob] provider disconnected: %s', name);
      }
    });
  }

  function onDeviceConnection(socket) {
    let pair = null;
    socket.on('message', async (raw) => {
      let msg;
      try {
        msg = JSON.parse(String(raw));
      } catch {
        return;
      }
      if (msg.op === 'hello') {
        pair = String(msg.pair || '').trim();
        if (!pair) {
          socket.close();
          return;
        }
        const prev = devices.get(pair);
        if (prev && prev.socket !== socket) {
          try {
            prev.socket.close();
          } catch {
            /* ignore */
          }
          dropDevice(pair, prev.socket);
        }
        const label = String(msg.label || '').trim();
        const hits = await notifyProviders('device_hello', { pair, label });
        const ok = hits.find((h) => h.msg && h.msg.ok);
        if (!ok) {
          socket.send(JSON.stringify({ op: 'error', error: 'Unknown pair code' }));
          socket.close();
          return;
        }
        devices.set(pair, {
          pair,
          socket,
          provider: ok.name,
          pending: new Map(),
        });
        socket.send(JSON.stringify({ op: 'ready', provider: ok.name, body: ok.msg.body || {} }));
        console.log('[fotomob] device paired: %s → %s', pair, ok.name);
        return;
      }
      if (msg.op === 'catalog') {
        if (!pair) return;
        const dev = devices.get(pair);
        const files = Array.isArray(msg.files) ? msg.files : [];
        const target = (dev && dev.provider) || '';
        try {
          if (target && providers.has(target)) {
            await providerCall(target, 'device_catalog', { pair, files }, 120000);
          } else {
            await notifyProviders('device_catalog', { pair, files }, 120000);
          }
          socket.send(JSON.stringify({ op: 'catalog_ok', count: files.length }));
        } catch (err) {
          socket.send(JSON.stringify({ op: 'error', error: err.message || 'Catalog failed' }));
        }
        return;
      }
      if (msg.op !== 'res' || msg.id == null || !pair) return;
      const dev = devices.get(pair);
      if (!dev) return;
      const wait = dev.pending.get(String(msg.id));
      if (!wait) return;
      dev.pending.delete(String(msg.id));
      clearTimeout(wait.timer);
      const p = providers.get(wait.provider);
      if (p && p.socket.readyState === 1) {
        p.socket.send(
          JSON.stringify({
            id: msg.id,
            op: 'devres',
            ok: !!msg.ok,
            status: msg.status,
            error: msg.error,
            contentType: msg.contentType,
            body: msg.body,
          })
        );
      }
    });
    socket.on('close', () => {
      if (pair) {
        dropDevice(pair, socket);
        console.log('[fotomob] device disconnected: %s', pair);
      }
    });
  }

  function attachWebSocket(_httpServer) {
    const wssProvider = new WebSocketServer({ noServer: true, maxPayload: MAX_PAYLOAD });
    const wssDevice = new WebSocketServer({ noServer: true, maxPayload: MAX_PAYLOAD });
    wssProvider.on('connection', onProviderConnection);
    wssDevice.on('connection', onDeviceConnection);
    console.log('[fotomob] ready on %s and %s', wsPath, deviceWsPath);
    return { provider: wssProvider, device: wssDevice };
  }

  return { handleRequest, attachWebSocket, wsPath, deviceWsPath, isApiRequest, prefix };
}
