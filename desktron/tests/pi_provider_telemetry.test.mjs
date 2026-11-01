import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { stripTypeScriptTypes } from 'node:module';

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'desk-provider-telemetry-'));
const source = fs.readFileSync(new URL('../lib/engine_adapters/pi_provider_telemetry.ts', import.meta.url), 'utf8');
const moduleUrl = 'data:text/javascript;base64,' + Buffer.from(stripTypeScriptTypes(source)).toString('base64');
const { default: install, fingerprint, summarizePayload,
  providerTraceIdentity } = await import(moduleUrl);

test.after(() => { fs.rmSync(root, { recursive: true, force: true }); });

test('provider summary is deterministic, domain-separated and contains no raw material', () => {
  const payload = {
    model: 'gpt-test', instructions: 'SYSTEM_CANARY', prompt_cache_key: 'CACHE_CANARY',
    input: [
      { role: 'developer', content: 'DEVELOPER_CANARY' },
      { role: 'user', content: 'HISTORY_CANARY' },
      { type: 'reasoning', encrypted_content: 'REASONING_CANARY' },
      { role: 'user', content: 'TAIL_CANARY' },
    ],
    tools: [{ type: 'function', name: 'TOOL_CANARY', parameters: { secret: 'ARG_CANARY' } }],
    reasoning: { effort: 'high' }, store: false,
  };
  const model = { provider: 'openai', id: 'gpt-test', api: 'openai-responses' };
  const epoch = '12345678-1234-4123-8123-123456789abc';
  const first = summarizePayload(payload, model, 'SESSION_CANARY', epoch, 1);
  const again = summarizePayload(payload, model, 'SESSION_CANARY', epoch, 1);
  assert.deepEqual(first, again);
  const text = JSON.stringify(first);
  for (const marker of ['SYSTEM_CANARY', 'DEVELOPER_CANARY', 'CACHE_CANARY', 'HISTORY_CANARY',
    'REASONING_CANARY', 'TAIL_CANARY', 'TOOL_CANARY', 'ARG_CANARY', 'SESSION_CANARY']) {
    assert.doesNotMatch(text, new RegExp(marker));
  }
  for (const [name, value] of Object.entries(first)) {
    if (name.endsWith('_fp')) assert.match(String(value), /^[0-9a-f]{32}$/);
  }
  assert.notEqual(first.system_segment_fp, first.tools_segment_fp);
  assert.equal(first.change_reason, 'new_conversation');
});

test('request changes distinguish cache/session/model lifecycle without raw ids', () => {
  const epoch = '12345678-1234-4123-8123-123456789abc';
  const model = { provider: 'openai', id: 'gpt-test', api: 'openai-responses' };
  const base = summarizePayload({ input: [{ role:'user', content:'one' }], prompt_cache_key:'cache-a' },
    model, 'session-a', epoch, 1);
  const stable = summarizePayload({ input: [{ role:'user', content:'two' }], prompt_cache_key:'cache-a' },
    model, 'session-a', epoch, 2, base);
  assert.equal(stable.session_changed, false);
  assert.equal(stable.cache_key_changed, false);
  assert.equal(stable.change_reason, 'new_provider_request');
  const changed = summarizePayload({ input: [{ role:'user', content:'two' }], prompt_cache_key:'cache-b' },
    model, 'session-b', epoch, 3, stable);
  assert.equal(changed.session_changed, true);
  assert.equal(changed.cache_key_changed, true);
  assert.equal(changed.change_reason, 'unknown');
  const routed = summarizePayload({ input: [{ role:'user', content:'two' }] },
    { ...model, id:'gpt-other' }, 'session-b', epoch, 4, changed);
  assert.equal(routed.change_reason, 'model_route_change');
});

test('fingerprints require a private key and field domain', () => {
  assert.match(fingerprint('session_fp', 'same'), /^[0-9a-f]{32}$/);
  assert.notEqual(fingerprint('session_fp', 'same'), fingerprint('cache_key_fp', 'same'));
  assert.equal(fingerprint('INVALID DOMAIN', 'same'), undefined);
});

test('extension emits ordered request summaries without inferring transport or continuation', async () => {
  const hooks = {};
  install({ on(name, handler) { hooks[name] = handler; } });
  const messages = [];
  const context = {
    model: { provider:'openai', id:'gpt-test', api:'openai-responses' },
    sessionManager: { getSessionId: () => 'RAW_SESSION' },
    ui: { notify: message => messages.push(message) },
  };
  await hooks.session_start({}, context);
  const firstPayload = { input:[{role:'user',content:'RAW_FIRST'}], prompt_cache_key:'RAW_CACHE' };
  hooks.before_provider_request({ payload:firstPayload }, context);
  assert.equal(providerTraceIdentity(firstPayload).request_seq, 1);
  hooks.turn_end({ message:{ role:'assistant', usage:{ input:10, cacheRead:8, totalTokens:12 } } }, context);
  const secondPayload = { input:[{role:'user',content:'RAW_SECOND'}], prompt_cache_key:'RAW_CACHE' };
  hooks.before_provider_request({ payload:secondPayload }, context);
  assert.equal(providerTraceIdentity(secondPayload).request_seq, 2);
  const rows = messages.map(message => JSON.parse(message.slice('DESKTRON_PROVIDER_TRACE:'.length)));
  assert.deepEqual(rows.map(row => row.request_seq), [1, 1, 2]);
  assert.equal(rows[1].kind, 'usage');
  assert.deepEqual([rows[1].input, rows[1].cache_read, rows[1].total], [10, 8, 12]);
  assert.ok(rows.filter(row => row.kind === 'request').every(row => row.transport === 'unknown'));
  assert.ok(rows.every(row => !('continuation_used' in row)));
  assert.doesNotMatch(messages.join('\n'), /RAW_SESSION|RAW_FIRST|RAW_SECOND|RAW_CACHE/);
});

test('cyclic payload is fail-open and does not consume a request sequence', async () => {
  const hooks = {}; const messages = [];
  install({ on(name, handler) { hooks[name] = handler; } });
  const context = { model:{provider:'openai',id:'gpt',api:'openai-responses'},
    sessionManager:{getSessionId:()=> 'session'}, ui:{notify:m=>messages.push(m)} };
  await hooks.session_start({}, context);
  const cyclic = { role:'user' }; cyclic.self = cyclic;
  hooks.before_provider_request({ payload:{ input:[cyclic] } }, context);
  assert.equal(messages.length, 0);
  const valid = { input:[{role:'user',content:'ok'}] };
  hooks.before_provider_request({ payload:valid }, context);
  assert.equal(providerTraceIdentity(valid).request_seq, 1);
});

test('notification failure does not consume sequence state', async () => {
  const hooks = {}; let failed = true; const messages = [];
  install({ on(name, handler) { hooks[name] = handler; } });
  const context = { model:{provider:'openai',id:'gpt',api:'openai-responses'},
    sessionManager:{getSessionId:()=> 'session'}, ui:{notify:m=>{ if(failed) throw Error('down'); messages.push(m); }} };
  await hooks.session_start({}, context);
  hooks.before_provider_request({ payload:{input:[{role:'user',content:'first'}]} }, context);
  failed = false;
  const valid = {input:[{role:'user',content:'second'}]};
  hooks.before_provider_request({ payload:valid }, context);
  assert.equal(providerTraceIdentity(valid).request_seq, 1);
  assert.equal(JSON.parse(messages[0].slice('DESKTRON_PROVIDER_TRACE:'.length)).request_seq, 1);
});
