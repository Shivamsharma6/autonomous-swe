// Disposable UI-only preview. Never forwards requests to the real control plane.
// Run: node tests/web/preview.mjs, then open http://127.0.0.1:4173.
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname } from 'node:path';
import { createHash } from 'node:crypto';
import { fixture, runs } from './fixtures.mjs';

const root = new URL('../../apps/web/', import.meta.url);
const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  res.setHeader('Cache-Control', 'no-store');
  if (url.pathname.startsWith('/api/') || url.pathname === '/health/ready') {
    res.setHeader('Content-Type', 'application/json');
    if (url.pathname.endsWith('/onboard')) {
      res.end(JSON.stringify({ project_id: runs[0].project_id, repository_id: runs[0].repository_id, name: 'Checkout service', source_path: 'checkout-service', default_branch: 'main', baseline_commit: runs[0].baseline_commit }));
      return;
    }
    res.end(JSON.stringify(fixture(req.url)));
    return;
  }
  try {
    const relative = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (relative.includes('..')) throw new Error('Invalid path');
    const body = await readFile(new URL(relative, root));
    res.setHeader('Content-Type', ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' })[extname(relative)] || 'text/plain');
    res.end(body);
  } catch (_) { res.writeHead(404); res.end('Not found'); }
});
server.on('upgrade', (req, socket) => {
  const accept = createHash('sha1').update(req.headers['sec-websocket-key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
  socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\nSec-WebSocket-Protocol: ${req.headers['sec-websocket-protocol']}\r\n\r\n`);
  socket.on('error', () => {});
  socket.on('data', data => { if ((data[0] & 0x0f) === 8) socket.end(Buffer.from([0x88, 0])); });
});
server.listen(4173, '127.0.0.1', () => console.log('Disposable UI fixture: http://127.0.0.1:4173'));
