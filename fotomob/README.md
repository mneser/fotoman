# Fotomob

Public relay + PWA. Fotoman at home opens an outbound WebSocket; phones browse shared collections here. No photo library is stored on this host.

Root collections get a share link and QR. Nested sub-folders do not, unless `FOTOMOB_SUBFOLDER_SHARE=1` is set on both Fotoman and Fotomob.

Listens on `http://127.0.0.1:3001` with every route under `/fotomob` (API, static, `/fotomob/provider`, `/fotomob/device`). nginx on colligator.com terminates TLS and proxies `/fotomob` here; everything else still goes to colligator.

```bash
cd fotomob
npm install
PORT=3001 HOST=127.0.0.1 npm start
```

On the VPS, see [deploy/DEPLOY.md](deploy/DEPLOY.md) (rsync + pm2), `deploy/nginx-colligator.com.conf`, and `deploy/ecosystem.config.cjs`.
