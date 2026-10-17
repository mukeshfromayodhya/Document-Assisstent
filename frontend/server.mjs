// Static file server + allow-listed proxy to the FastAPI backend. Zero dependencies (Node 18+).
import http from 'node:http';
import https from 'node:https';
import { readFile } from 'node:fs/promises';
import { existsSync, readFileSync } from 'node:fs';
import { extname, join, normalize, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('.', import.meta.url));
const pub = join(root, 'public');
const envFile = join(root, '.env');
if (existsSync(envFile)) {
  for (const line of readFileSync(envFile, 'utf8').split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$/);
    if (m && !(m[1] in process.env)) process.env[m[1]] = m[2].replace(/^(["'])(.*)\1$/, '$2');
  }
}
const BACKEND = new URL(process.env.BACKEND_URL || 'http://127.0.0.1:8000');
const TOKEN = process.env.API_ACCESS_TOKEN || '';
const HOST = process.env.FRONTEND_HOST || '127.0.0.1';
const PORT = Number(process.env.FRONTEND_PORT || 4173);
const lib = BACKEND.protocol === 'https:' ? https : http;
const MAX_UPLOAD = 10 * 1024 * 1024 + 64 * 1024;
const MAX_JSON = 1024 * 1024;

const ROUTES = [
  ['GET', /^\/auth\/session$/],
  ['POST', /^\/auth\/(guest|signup|login|logout)$/],
  ['GET', /^\/chats$/],
  ['PUT', /^\/chats\/[\w.-]{1,120}$/],
  ['DELETE', /^\/chats\/[\w.-]{1,120}$/],
  ['POST', /^\/documents\/(upload|search|ask)$/],
  ['GET', /^\/documents\/[a-f0-9]{64}\/status$/],
  ['GET', /^\/documents\/[a-f0-9]{64}\/chunks\/\d+$/],
];
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.ico': 'image/x-icon' };
const SECURITY = {
  'Content-Security-Policy': "default-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'X-Frame-Options': 'DENY',
};

function sendJson(res, status, body) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', ...SECURITY });
  res.end(JSON.stringify(body));
}

function proxy(req, res, path) {
  const limit = path === '/documents/upload' ? MAX_UPLOAD : MAX_JSON;
  if (Number(req.headers['content-length'] || 0) > limit) return sendJson(res, 413, { detail: 'The request is too large.' });
  const headers = {};
  for (const h of ['content-type', 'content-length', 'cookie', 'accept']) if (req.headers[h]) headers[h] = req.headers[h];
  if (TOKEN) headers.authorization = `Bearer ${TOKEN}`;
  const upstream = lib.request({ hostname: BACKEND.hostname, port: BACKEND.port || undefined, path, method: req.method, headers }, (r) => {
    const out = { 'Cache-Control': 'no-store', ...SECURITY };
    for (const h of ['content-type', 'set-cookie', 'retry-after', 'www-authenticate']) if (r.headers[h]) out[h] = r.headers[h];
    res.writeHead(r.statusCode || 502, out);
    r.pipe(res);
  });
  upstream.on('error', () => { if (!res.headersSent) sendJson(res, 502, { detail: 'The backend is not reachable. Start it and try again.' }); else res.destroy(); });
  let seen = 0;
  req.on('data', (chunk) => { seen += chunk.length; if (seen > limit) { upstream.destroy(); if (!res.headersSent) sendJson(res, 413, { detail: 'The request is too large.' }); } });
  req.pipe(upstream);
}

async function serveStatic(req, res, pathname) {
  if (req.method !== 'GET' && req.method !== 'HEAD') return sendJson(res, 405, { detail: 'Method not allowed.' });
  let rel;
  try { rel = decodeURIComponent(pathname); } catch { return sendJson(res, 400, { detail: 'Bad path.' }); }
  if (rel === '/') rel = '/index.html';
  const file = normalize(join(pub, rel));
  if (!file.startsWith(pub + sep) || !MIME[extname(file)]) return sendJson(res, 404, { detail: 'Not found.' });
  try {
    const data = await readFile(file);
    res.writeHead(200, { 'Content-Type': MIME[extname(file)], 'Cache-Control': 'no-cache', ...SECURITY });
    res.end(req.method === 'HEAD' ? undefined : data);
  } catch { sendJson(res, 404, { detail: 'Not found.' }); }
}

http.createServer((req, res) => {
  const url = new URL(req.url || '/', 'http://localhost');
  if (url.pathname.startsWith('/api/')) {
    const path = url.pathname.slice(4);
    if (!ROUTES.some(([m, re]) => m === req.method && re.test(path))) return sendJson(res, 404, { detail: 'Route not available.' });
    const origin = req.headers.origin;
    if (req.method !== 'GET' && origin) {
      let same = false;
      try { same = new URL(origin).host === req.headers.host; } catch { /* invalid origin */ }
      if (!same) return sendJson(res, 403, { detail: 'Cross-origin requests are not allowed.' });
    }
    return proxy(req, res, path + url.search);
  }
  serveStatic(req, res, url.pathname);
}).listen(PORT, HOST, () => console.log(`Document Assistant UI: http://${HOST}:${PORT}  (backend ${BACKEND.origin})`));
