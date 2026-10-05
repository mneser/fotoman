# Fotomob

Public relay + PWA. Fotoman at home opens an outbound WebSocket; phones browse shared collections here. No photo library is stored on this host.

Root collections get a share link and QR. Nested sub-folders do not, unless `FOTOMOB_SUBFOLDER_SHARE=1` is set on both Fotoman and Fotomob.

```bash
cd fotomob
npm install
PORT=3001 HOST=127.0.0.1 npm start
```

Routes are under `/fotomob` (API, static files, `/fotomob/provider`, `/fotomob/device`). Put TLS in front and proxy that path to port 3001. In Fotoman, set the Fotomob URL to your public address. The default is a hosted relay at `https://colligator.com/fotomob`.
