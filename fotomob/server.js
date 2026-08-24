import http from 'http';
import path from 'path';
import express from 'express';
import { fileURLToPath } from 'url';
import { createFotomobHandlers } from './main.js';

const rootDir = path.dirname(fileURLToPath(import.meta.url));
const publicDir = path.join(rootDir, 'public');
const prefix = '/fotomob';
const port = Number(process.env.PORT) || 3001;
const host = process.env.HOST || '127.0.0.1';

const app = express();
const fotomob = createFotomobHandlers({ basePath: prefix });

app.use(prefix, (req, res, next) => {
  if (fotomob.isApiRequest(req)) {
    fotomob.handleRequest(req, res);
  } else {
    next();
  }
});

app.get(`${prefix}/sw.js`, (_req, res) => {
  res.set('Cache-Control', 'no-cache');
  res.set('Service-Worker-Allowed', prefix);
  res.sendFile(path.join(publicDir, 'sw.js'));
});

app.use(
  prefix,
  express.static(publicDir, {
    index: 'index.html',
    redirect: false,
    setHeaders(res, filePath) {
      if (/\.(html|js|css|webmanifest)$/.test(filePath)) {
        res.setHeader('Cache-Control', 'no-cache');
      }
    },
  })
);

app.get(prefix, (_req, res) => {
  res.redirect(301, `${prefix}/`);
});

app.get(new RegExp(`^${prefix}/`), (_req, res) => {
  res.sendFile(path.join(publicDir, 'index.html'));
});

const server = http.createServer(app);
const sockets = fotomob.attachWebSocket(server);

server.on('upgrade', (req, socket, head) => {
  const pathname = new URL(req.url || '/', 'http://localhost').pathname;
  if (pathname === fotomob.wsPath) {
    sockets.provider.handleUpgrade(req, socket, head, (ws) => {
      sockets.provider.emit('connection', ws, req);
    });
  } else if (pathname === fotomob.deviceWsPath) {
    sockets.device.handleUpgrade(req, socket, head, (ws) => {
      sockets.device.emit('connection', ws, req);
    });
  } else {
    socket.destroy();
  }
});

server.listen(port, host, () => {
  console.log(`[fotomob] listening on http://${host}:${port}${prefix}`);
  console.log(`[fotomob] ws ${fotomob.wsPath}, ${fotomob.deviceWsPath}`);
});
