import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import https from 'node:https';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { stripTypeScriptTypes } from 'node:module';

const sourcePath = new URL('../lib/engine_adapters/pi_web.ts', import.meta.url);
const coreUrl = new URL('../lib/web_tools/core.mjs', import.meta.url).href;
const browserUrl = new URL('../lib/web_tools/browser_search.mjs', import.meta.url).href;
const original = fs.readFileSync(sourcePath, 'utf8').replace(/\r\n/g, '\n');
assert.match(original, /from '\.\.\/web_tools\/core\.mjs'/);
assert(!original.includes('pi_secret_bridge'));
assert(!original.includes('process.env'));
assert(!original.includes('node:http'));
assert(!original.includes('open.feedcoopapi.com'));

const rawSource = original
  .replace(/import type \{ ExtensionAPI \} from '@earendil-works\/pi-coding-agent';\n/, '')
  .replace(/import \{ Text \} from '@earendil-works\/pi-tui';\n/, 'class Text { constructor(text) { this.text = text; } }\n')
  .replace("from '../web_tools/core.mjs';", `from '${coreUrl}';`)
  .replace("from '../web_tools/browser_search.mjs';", `from '${browserUrl}';`);
const moduleUrl = 'data:text/javascript;base64,' + Buffer.from(stripTypeScriptTypes(rawSource)).toString('base64');
const mod = await import(moduleUrl);
const PUBLIC_IP = '93.184.216.34';

function loadExtension() {
  const tools = [], handlers = {};
  const names = ['DESKTRON_SESSION_ID', 'DESKTRON_APP_CONTROL_SESSION_CAPABILITY', 'DESKTRON_APP_CONTROL_SOCKET'];
  const saved = Object.fromEntries(names.map(name => [name, process.env[name]]));
  for (const name of names) delete process.env[name];
  try {
    mod.default({
      registerTool(tool) { tools.push(tool); },
      on(name, handler) { handlers[name] = handler; },
    });
  } finally {
    for (const name of names) {
      if (saved[name] === undefined) delete process.env[name]; else process.env[name] = saved[name];
    }
  }
  return { tools, handlers };
}

async function withHttpsFixture(run) {
  const originalRequest = https.request;
  const calls = [];
  https.request = (url, options, callback) => {
    const request = new EventEmitter();
    request.setTimeout = () => {};
    request.write = () => {};
    request.destroy = error => { if (error) queueMicrotask(() => request.emit('error', error)); };
    request.end = () => {
      calls.push({ url: url.toString(), options });
      const response = new PassThrough();
      response.statusCode = url.pathname === '/robots.txt' ? 404 : 200;
      response.headers = { 'content-type': url.pathname === '/robots.txt' ? 'text/plain' : 'text/html; charset=utf-8' };
      callback(response);
      response.end(url.pathname === '/robots.txt' ? '' : '<title>Real core</title><p>evidence</p>');
    };
    return request;
  };
  try { return await run(calls); } finally { https.request = originalRequest; }
}

function branch(messages) {
  return { sessionManager: { getBranch: () => messages } };
}

test('Pi layer is thin and registers stable tools and lifecycle hooks', () => {
  const { tools, handlers } = loadExtension();
  assert.deepEqual(tools.map(tool => tool.name), ['web_search', 'web_fetch']);
  assert.deepEqual(Object.keys(handlers).sort(), ['before_agent_start', 'session_start']);
  assert.match(tools[0].description, /disposable anonymous background App Control tab/);
  assert.match(tools[1].description, /user input, web_search, or a fetched page link/);
});

test('Pi layer uses real core and rebuilds provenance from only the current branch', async () => {
  await withHttpsFixture(async calls => {
    const { handlers, tools } = loadExtension();
    const fetchTool = tools.find(tool => tool.name === 'web_fetch');
    const oldUrl = `https://${PUBLIC_IP}/old`;
    const newUrl = `https://${PUBLIC_IP}/new`;
    await handlers.session_start({}, branch([
      { type: 'message', message: { role: 'user', content: `read ${oldUrl}` } },
    ]));
    const old = await fetchTool.execute('old', { url: oldUrl });
    assert.equal(old.details.status, 'success');
    assert.equal(old.details.title, 'Real core');
    assert.match(old.details.content_hash, /^sha256:/);

    await handlers.before_agent_start({ prompt: 'continue' }, branch([
      { type: 'message', message: { role: 'user', content: [{ type: 'text', text: `read ${newUrl}` }] } },
    ]));
    assert.equal((await fetchTool.execute('gone', { url: oldUrl })).details.failure_reason, 'url_provenance_required');
    assert.equal((await fetchTool.execute('new', { url: newUrl })).details.status, 'success');
    assert.equal(calls.length, 4);
    assert(calls.every(call => !('cookie' in call.options.headers) && !('authorization' in call.options.headers)));
  });
});

test('Pi layer returns real structured search evidence and top-level untrusted warning', async () => {
  await withHttpsFixture(async calls => {
    const { tools } = loadExtension();
    const search = tools.find(tool => tool.name === 'web_search');
    const searchResult = await search.execute('1', { query: 'public weather', limit: 2 });
    assert.equal(searchResult.details.status, 'error');
    assert.equal(searchResult.details.failure_reason, 'browser_unavailable');
    assert.equal(searchResult.details.transport, 'app_control_browser');
    const envelope = JSON.parse(searchResult.content[0].text);
    assert.match(envelope.security_notice, /UNTRUSTED/);
    assert.equal(envelope.evidence.query, 'public weather');
    assert(calls.every(call => !('cookie' in call.options.headers) && !('authorization' in call.options.headers)));
  });
  const { tools } = loadExtension();
  const search = tools.find(tool => tool.name === 'web_search');
  const fetchTool = tools.find(tool => tool.name === 'web_fetch');
  const sensitive = await search.execute('2', { query: 'password=very-sensitive-value' });
  assert.equal(sensitive.details.failure_reason, 'query_may_contain_sensitive_data');
  assert.equal(sensitive.details.query, '[BLOCKED_SENSITIVE_QUERY]');
  const fetchFailure = await fetchTool.execute('3', { url: `https://${PUBLIC_IP}/not-authorized` });
  assert.equal(fetchFailure.details.tool, 'web_fetch');
  assert.equal(fetchFailure.details.failure_reason, 'url_provenance_required');
});
