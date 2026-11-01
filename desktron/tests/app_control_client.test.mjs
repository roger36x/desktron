import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { AppControlClient, createAppControlClientFromEnv } from '../lib/web_tools/app_control_client.mjs';

async function withLocalServer(handler, task) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'desk-app-control-'));
  const socket = path.join(dir, 'control.sock');
  const server = http.createServer(handler);
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    if (process.platform === 'win32') server.listen(0, '127.0.0.1', resolve);
    else server.listen(socket, resolve);
  });
  const address = process.platform === 'win32' ? `tcp:127.0.0.1:${server.address().port}` : socket;
  try { return await task(address); }
  finally {
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

test('App Control client sends session capability and fixed browser constraints over its local channel', async () => {
  const seen = [];
  await withLocalServer((request, response) => {
    const chunks = [];
    request.on('data', chunk => chunks.push(chunk));
    request.on('end', () => {
      const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      seen.push({ path: request.url, capability: request.headers['x-desk-app-control-session'], body });
      response.setHeader('content-type', 'application/json');
      response.end(JSON.stringify({ id: 'op-1', status: 'succeeded', lease_id: 'lease-1', output: { url: body.url } }));
    });
  }, async socketPath => {
    const client = new AppControlClient({ socketPath, sessionId: 'session-1', capability: 'cap-1' });
    const result = await client.open('https://duckduckgo.com/?q=one', {
      allowedMainFrameHosts: ['duckduckgo.com'], intent: 'test intent', timeoutMs: 2000,
    });
    assert.equal(result.status, 'succeeded');
  });
  assert.equal(seen[0].path, '/workbench/app-control/open');
  assert.equal(seen[0].capability, 'cap-1');
  assert.deepEqual(seen[0].body.allowed_main_frame_hosts, ['duckduckgo.com']);
  assert.equal(seen[0].body.isolated, true);
  assert.equal(seen[0].body.session_id, 'session-1');
});

test('missing or unreachable browser channel becomes stable browser_unavailable', async () => {
  assert.equal(createAppControlClientFromEnv({}), null);
  const client = new AppControlClient({
    socketPath: path.join(os.tmpdir(), `missing-${process.pid}.sock`), sessionId: 'session-1', capability: 'cap-1',
  });
  await assert.rejects(() => client.open('https://duckduckgo.com/', { timeoutMs: 1000 }),
    error => error.code === 'browser_unavailable' && error.retryable === true);
});

test('inspection settle budget is explicit and bounded without changing the general default', async () => {
  const seen = [];
  await withLocalServer((request, response) => {
    let data = '';
    request.on('data', chunk => { data += chunk; });
    request.on('end', () => {
      seen.push(JSON.parse(data).settle_ms);
      response.end(JSON.stringify({ status: 'succeeded', output: {} }));
    });
  }, async socketPath => {
    const client = new AppControlClient({ socketPath, sessionId: 's', capability: 'c' });
    await client.inspect('l', {}, { settleMs: 0 });
    await client.inspect('l', {}, { settleMs: 300 });
    await client.inspect('l', {});
    await client.inspect('l', {}, { settleMs: 9000 });
  });
  assert.deepEqual(seen, [0, 300, 5000, 5000]);
});

test('open reports the lease before waiting and malformed pending operations fail explicitly', async () => {
  let reported = '';
  await withLocalServer((request, response) => {
    response.end(JSON.stringify(request.url.includes('/wait?')
      ? { id: 'op', status: 'failed' }
      : { id: 'op', lease_id: 'early', status: 'queued' }));
  }, async socketPath => {
    const client = new AppControlClient({ socketPath, sessionId: 's', capability: 'c' });
    const op = await client.open('https://duckduckgo.com/', { onLease: lease => { reported = lease; } });
    assert.equal(reported, 'early');
    assert.equal(op.status, 'failed');
    await assert.rejects(() => client.wait({ status: 'queued' }), e => e.code === 'browser_protocol_error');
  });
});

test('cancellation before creation response still hands the lease back for cleanup', async () => {
  let reported = '';
  const controller = new AbortController();
  await withLocalServer((request, response) => {
    controller.abort();
    setTimeout(() => response.end(JSON.stringify({ id: 'op', lease_id: 'created', status: 'queued' })), 20);
  }, async socketPath => {
    const client = new AppControlClient({ socketPath, sessionId: 's', capability: 'c' });
    await assert.rejects(() => client.open('https://duckduckgo.com/', {
      signal: controller.signal, onLease: lease => { reported = lease; }, timeoutMs: 500,
    }), e => e.code === 'aborted');
    assert.equal(reported, 'created');
  });
});

test('submission and wait share the deadline without a one-second minimum inflation', async () => {
  await withLocalServer((request, response) => {
    setTimeout(() => response.end(JSON.stringify(request.url.includes('/wait?')
      ? { id: 'op', status: 'succeeded' } : { id: 'op', lease_id: 'created', status: 'queued' })), 140);
  }, async socketPath => {
    const client = new AppControlClient({ socketPath, sessionId: 's', capability: 'c' });
    const started = performance.now();
    await assert.rejects(() => client.open('https://duckduckgo.com/', { timeoutMs: 220 }));
    assert.ok(performance.now() - started < 400);
  });
});

test('absolute request deadline also bounds continuously streaming responses', async () => {
  await withLocalServer((request, response) => {
    const timer = setInterval(() => response.write(' '), 30);
    response.on('close', () => clearInterval(timer));
  }, async socketPath => {
    const client = new AppControlClient({ socketPath, sessionId: 's', capability: 'c' });
    const started = Date.now();
    await assert.rejects(() => client.request('/test', {}, { timeoutMs: 1000 }));
    assert.ok(Date.now() - started < 2000);
  });
});
