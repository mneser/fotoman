# Deploy Fotomob to colligator.com

Live layout on the VPS:

| Piece | Path / port |
|-------|-------------|
| Fotomob app | `/root/fotomob` → `127.0.0.1:3001` |
| Colligator (everything else) | `/root/colligator` → `127.0.0.1:3000` |
| nginx | TLS + path split; see `nginx-colligator.com.conf` |
| process manager | pm2 apps `fotomob` and `colligator` (`ecosystem.config.cjs`) |

Public URL: `https://colligator.com/fotomob`

## Redeploy (code only)

From this repo on your machine:

```bash
rsync -az --exclude node_modules --exclude '.git' \
  /path/to/fotoman/fotomob/ root@colligator.com:/root/fotomob/

ssh root@colligator.com 'cd /root/fotomob && npm install --omit=dev && pm2 restart fotomob --update-env'
```

Smoke-check:

```bash
curl -sS https://colligator.com/fotomob/api/status
# {"live":true,"providers":["…"]} once Fotoman at home is connected
```

Bump `?v=` on `app.js` / `styles.css` / shell cache in `public/` when the PWA must invalidate the service worker after a UI change.

`FOTOMOB_SUBFOLDER_SHARE` defaults to off (also set in `ecosystem.config.cjs`). Nested collection folders then have no share link or QR; set it to `1` on Fotoman and Fotomob to restore per-subfolder codes.

## pm2 cwd (important)

`fotomob` must run with **cwd `/root/fotomob`**. If cwd is `/root`, Node cannot resolve `express` / `ws` and the process crash-loops.

Prefer starting from the ecosystem file:

```bash
pm2 delete fotomob   # only if cwd is wrong
pm2 start /root/fotomob/deploy/ecosystem.config.cjs --only fotomob
pm2 save
```

Do not start with a bare `pm2 start server.js` from another directory.

## First-time / nginx

Already done on colligator.com. If rebuilding elsewhere:

1. Copy tree to `/root/fotomob`, `npm install --omit=dev`
2. Install site: `cp deploy/nginx-colligator.com.conf /etc/nginx/sites-available/colligator.com` → enable, `nginx -t`, reload
3. `pm2 start deploy/ecosystem.config.cjs` (both apps) and `pm2 save` / `pm2 startup` as needed

Colligator stays on 3000; only `/fotomob` is proxied to 3001. After a fotomob restart, home Fotoman must reconnect its provider WebSocket (restart Fotoman yourself; do not auto-restart it from deploy scripts).
