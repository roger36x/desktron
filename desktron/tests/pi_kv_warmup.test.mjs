import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { stripTypeScriptTypes, syncBuiltinESMExports } from 'node:module';

const sourcePath = new URL('../lib/engine_adapters/pi_kv_warmup.ts', import.meta.url);
const source = fs.readFileSync(sourcePath, 'utf8');
const moduleUrl = 'data:text/javascript;base64,' + Buffer.from(stripTypeScriptTypes(source)).toString('base64');
const { default: install, claimWindow } = await import(moduleUrl);
const tick = () => new Promise(resolve => setImmediate(resolve));
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

function setup(t, env = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'desk-warmup-test-'));
  const previous = { ...process.env };
  for (const key of Object.keys(process.env)) if (key.startsWith('PI_KV_WARM') || key.startsWith('PI_OLLAMA')) delete process.env[key];
  Object.assign(process.env, { PI_KV_WARMUP: '1', PI_KV_WARM_PROVIDER: 'ollama-remote',
    PI_KV_WARM_MODEL: 'qwen3.8:27b', ...env });
  t.mock.method(os, 'homedir', () => root);
  syncBuiltinESMExports();
  const hooks = {};
  const requests = [];
  const logs = [];
  t.mock.method(process.stderr, 'write', value => { logs.push(String(value)); return true; });
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    requests.push({ url: String(url), ...options });
    return new Response('', { status: 200 });
  });
  const ctx = {
    model: { provider: 'ollama-remote', id: 'qwen3.8:27b', api: 'openai-completions', baseUrl: 'http://model.invalid/v1' },
    isIdle: () => true, hasPendingMessages: () => false,
    getSystemPrompt: () => 'CURRENT_SYSTEM_PREFIX',
    modelRegistry: { getApiKeyAndHeaders: async () => ({ ok: true, apiKey: 'TEST_SECRET' }) },
  };
  const register = () => {
    const registered = {};
    install({ registerFlag() {}, getFlag() {}, on: (event, fn) => { registered[event] = fn; } });
    return registered;
  };
  Object.assign(hooks, register());
  t.after(async () => {
    hooks.session_shutdown({}, ctx);
    await tick();
    for (const key of Object.keys(process.env)) if (!(key in previous)) delete process.env[key];
    Object.assign(process.env, previous);
    t.mock.restoreAll();
    syncBuiltinESMExports();
    fs.rmSync(root, { recursive: true, force: true });
  });
  return { root, ctx, hooks, requests, logs, register };
}

test('disabled by default, legacy Ollama provider alone does not enable warmup', async t => {
  const x = setup(t, { PI_KV_WARMUP: '', PI_OLLAMA_PROVIDER: 'ollama-remote' });
  x.hooks.session_start({}, x.ctx);
  await tick();
  assert.equal(x.requests.length, 0);
  assert.deepEqual(fs.readdirSync(x.root), []);
});

test('requires exact provider, model and supported API; never touches auth for others', async t => {
  const x = setup(t);
  x.ctx.modelRegistry.getApiKeyAndHeaders = () => { assert.fail('must not resolve other provider auth'); };
  for (const patch of [{ provider: 'example-gw' }, { id: 'another-qwen' }, { api: 'anthropic-messages' }]) {
    x.hooks.session_start({}, { ...x.ctx, model: { ...x.ctx.model, ...patch } });
  }
  await tick();
  assert.equal(x.requests.length, 0);
});

test('only current system prefix is sent; old transcript cache and URL override are ignored', async t => {
  const x = setup(t, { PI_KV_WARM_BASE_URL: 'https://wrong.invalid/v1' });
  const cache = path.join(x.root, '.pi/agent/cache');
  fs.mkdirSync(cache, { recursive: true });
  const old = path.join(cache, 'kv-warmup-body.json');
  fs.writeFileSync(old, JSON.stringify({ messages: [{ role: 'user', content: 'OLD_PRIVATE_MESSAGE' }] }));
  const before = fs.readFileSync(old, 'utf8');
  assert.equal(x.hooks.session_start({}, x.ctx), undefined, 'hook never waits for network');
  await tick();
  assert.equal(x.requests.length, 1);
  assert.equal(x.requests[0].url, 'http://model.invalid/v1/chat/completions');
  assert.equal(x.requests[0].redirect, 'error');
  assert.deepEqual(JSON.parse(x.requests[0].body), { model: 'qwen3.8:27b', messages: [
    { role: 'system', content: 'CURRENT_SYSTEM_PREFIX' }, { role: 'user', content: 'hi' },
  ], max_tokens: 1, stream: false });
  assert.equal(fs.readFileSync(old, 'utf8'), before);
  assert(!x.logs.join('').includes('TEST_SECRET'));
  const markers = fs.readdirSync(path.join(cache, 'desk-kv-warmup-attempts-v2'), { recursive: true });
  assert(markers.every(name => !name.includes('CURRENT_SYSTEM_PREFIX')));
});

test('busy, queued, empty and oversized prefixes do not create requests', async t => {
  const x = setup(t);
  for (const patch of [{ isIdle: () => false }, { hasPendingMessages: () => true },
    { getSystemPrompt: () => '' }, { getSystemPrompt: () => '中'.repeat(22000) }]) {
    x.hooks.session_start({}, { ...x.ctx, ...patch });
  }
  await tick();
  assert.equal(x.requests.length, 0);
});

for (const event of ['before_agent_start', 'before_provider_request', 'model_select', 'session_shutdown']) {
  test(event + ' cancels warmup without adding a request or changing prompt', async t => {
    const x = setup(t);
    let signal;
    t.mock.method(globalThis, 'fetch', (_url, options) => { signal = options.signal; return new Promise(() => {}); });
    x.hooks.session_start({}, x.ctx);
    await tick();
    const payload = { systemPrompt: 'UNCHANGED', payload: { messages: [{ role: 'user', content: 'PRIVATE' }] } };
    assert.equal(x.hooks[event](payload, x.ctx), undefined);
    assert.equal(signal.aborted, true);
    assert.equal(payload.systemPrompt, 'UNCHANGED');
    await tick();
  });
}

test('authentication timeout cannot later dispatch a request', async t => {
  const x = setup(t, { PI_KV_WARM_TIMEOUT_MS: '100' });
  let complete;
  x.ctx.modelRegistry.getApiKeyAndHeaders = () => new Promise(resolve => { complete = resolve; });
  x.hooks.session_start({}, x.ctx);
  await delay(140);
  complete({ ok: true, apiKey: 'LATE_SECRET' });
  await tick();
  assert.equal(x.requests.length, 0);
  assert(x.logs.some(line => line.includes('cancelled_or_timeout')));
  x.ctx.modelRegistry.getApiKeyAndHeaders = async () => ({ ok: true });
  x.hooks.session_start({}, x.ctx);
  await tick();
  assert.equal(x.requests.length, 1, 'timeout cleared process-local pending state');
});

test('fetch timeout is bounded even if transport fails to settle', async t => {
  const x = setup(t, { PI_KV_WARM_TIMEOUT_MS: '100' });
  let signal;
  t.mock.method(globalThis, 'fetch', (_url, options) => { signal = options.signal; return new Promise(() => {}); });
  x.hooks.session_start({}, x.ctx);
  await delay(140);
  assert(signal.aborted);
  assert(x.logs.some(line => line.includes('cancelled_or_timeout')));
});

test('real turn cancels pending authentication and late completion cannot send', async t => {
  const x = setup(t);
  let complete;
  x.ctx.modelRegistry.getApiKeyAndHeaders = () => new Promise(resolve => { complete = resolve; });
  x.hooks.session_start({}, x.ctx);
  x.hooks.before_agent_start({}, x.ctx);
  complete({ ok: true });
  await tick();
  assert.equal(x.requests.length, 0);
});

test('context and authentication failures do not escape the optional hook', async t => {
  const x = setup(t);
  assert.doesNotThrow(() => x.hooks.session_start({}, {
    ...x.ctx, getSystemPrompt() { throw new Error('PRIVATE_CONTEXT'); },
  }));
  x.ctx.modelRegistry.getApiKeyAndHeaders = async () => ({ ok: false, error: 'PRIVATE_AUTH_ERROR' });
  x.hooks.session_start({}, x.ctx);
  await tick();
  assert.equal(x.requests.length, 0);
  assert(!x.logs.join('').includes('PRIVATE_'));
});

test('failure consumes shared cooldown; logs never echo exception secrets', async t => {
  const x = setup(t);
  let calls = 0;
  t.mock.method(globalThis, 'fetch', async () => { calls++; throw new Error('SECRET_URL_AND_KEY'); });
  x.hooks.session_start({}, x.ctx);
  await tick();
  x.register().session_start({}, x.ctx);
  await tick();
  assert.equal(calls, 1);
  assert(!x.logs.join('').includes('SECRET_URL_AND_KEY'));
});

test('401 and unsupported endpoint are skipped without affecting real work', async t => {
  const x = setup(t);
  t.mock.method(globalThis, 'fetch', async () => new Response('', { status: 401 }));
  x.hooks.session_start({}, x.ctx);
  await tick();
  assert(x.logs.some(line => line.includes('http_401')));
  x.ctx.model.baseUrl = 'https://user:secret@wrong.invalid/v1';
  x.hooks.session_start({}, x.ctx);
  await tick();
  assert(x.logs.some(line => line.includes('skip_unsupported_endpoint')));
});

test('shared windows deduplicate, recover after crash window and bound marker retention', t => {
  const { root } = setup(t);
  assert(claimWindow(root, 'same-endpoint-model', 1000, 100));
  assert(!claimWindow(root, 'same-endpoint-model', 1099, 100));
  assert(claimWindow(root, 'different-model', 1000, 100));
  assert(claimWindow(root, 'same-endpoint-model', 1100, 100));
  assert(claimWindow(root, 'same-endpoint-model', 1300, 100));
  for (const name of fs.readdirSync(root)) assert(fs.readdirSync(path.join(root, name)).length <= 2);
});

test('independent processes race for one warmup slot', async t => {
  const { root } = setup(t);
  const code = `const {claimWindow}=await import(${JSON.stringify(moduleUrl)}); console.log(claimWindow(process.argv[1], 'shared', 1000, 100));`;
  const results = await Promise.all(Array.from({ length: 4 }, () => new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ['--input-type=module', '-e', code, root], { stdio: ['ignore', 'pipe', 'pipe'] });
    let output = '';
    child.stdout.on('data', data => { output += data; });
    child.on('error', reject);
    child.on('exit', code => code === 0 ? resolve(output.trim()) : reject(new Error('child failed')));
  })));
  assert.equal(results.filter(value => value === 'true').length, 1);
});
