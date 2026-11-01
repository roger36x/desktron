import test from 'node:test';
import assert from 'node:assert/strict';
import { createAppControlBrowserSearch, parseDuckDuckGoSnapshot } from '../lib/web_tools/browser_search.mjs';
import { WebExecutor, failureEvidence } from '../lib/web_tools/core.mjs';
const IP = '93.184.216.34';
const ok = (output, lease_id = 'lease') => ({ status: 'succeeded', output, lease_id });
function page(query = 'one') {
  return { url: `https://duckduckgo.com/?q=${query}`, text: 'normal', nodes: [
    { node_id: 'a', tag: 'article', text: 'Result Full snippet.' },
    { node_id: 'h', parent_id: 'a', tag: 'h2', role: 'heading', name: 'Result' },
    { node_id: 'l', parent_id: 'h', tag: 'a', role: 'link', href: 'https://example.com/a', name: 'Result' },
  ] };
}
function client(pages, calls = []) {
  return {
    async open(url, options) { calls.push(['open']); options.onLease?.('lease'); return ok({}); },
    async inspect(lease, input, options) { calls.push(['inspect', input.max_nodes, options.settleMs]); return ok(pages.shift() || page()); },
    async close() { calls.push(['close']); return ok({}); },
  };
}
const executor = (provider, resolveHost = async () => [{ address: IP, family: 4 }]) => new WebExecutor({ searchProvider: provider, resolveHost });

test('ready results avoid generic settle waits and expose bounded diagnostics', async () => {
  const calls = [];
  const result = await executor(createAppControlBrowserSearch({ client: client([page()], calls) })).search('one', 1);
  assert.deepEqual(calls.map(x => x[0]), ['open', 'inspect', 'close']);
  assert.equal(calls[1][2], 0);
  assert.equal(result.diagnostics.inspections, 1);
  assert.equal(result.diagnostics.cleanup_failed, false);
  for (const [key, value] of Object.entries(result.diagnostics)) {
    if (key.endsWith('_ms')) assert.ok(Number.isInteger(value) && value >= 0);
  }
});

test('incomplete truncated pages re-observe the same lease and expand the bounded snapshot', async () => {
  const calls = [];
  const incomplete = { url: 'https://duckduckgo.com/?q=one', text: 'loading', nodes: [], truncated: true };
  const result = await executor(createAppControlBrowserSearch({ client: client([incomplete, page()], calls) })).search('one', 1);
  assert.equal(result.results.length, 1);
  assert.deepEqual(calls.filter(x => x[0] === 'inspect'), [['inspect', 1200, 0], ['inspect', 4000, 300]]);
  assert.equal(calls.filter(x => x[0] === 'open').length, 1);
});

test('unknown layouts exhaust three observations, remain errors, and retain diagnostics', async () => {
  const calls = [];
  const empty = { url: 'https://duckduckgo.com/?q=one', text: 'loading', nodes: [] };
  const web = executor(createAppControlBrowserSearch({ client: client([empty, empty, empty], calls) }));
  let error;
  try { await web.search('one', 1); } catch (e) { error = e; }
  assert.equal(error.code, 'provider_malformed_response');
  assert.equal(failureEvidence('web_search', 'one', error).diagnostics.inspections, 3);
  assert.equal(calls.at(-1)[0], 'close');
});

test('challenge and foreign-host pages never retry or become empty results', async () => {
  for (const snapshot of [{ ...page(), text: 'Verify you are human' }, { ...page(), url: 'https://evil.example/?q=one' }]) {
    const calls = [];
    await assert.rejects(() => executor(createAppControlBrowserSearch({ client: client([snapshot], calls) })).search('one', 1));
    assert.equal(calls.filter(x => x[0] === 'inspect').length, 1);
    assert.equal(calls.at(-1)[0], 'close');
  }
});

test('query mismatch re-observes but never accepts previous-query results', async () => {
  const calls = [];
  const wrong = page('previous');
  const web = executor(createAppControlBrowserSearch({ client: client([wrong, wrong, wrong], calls) }));
  await assert.rejects(() => web.search('one', 1), e => e.code === 'provider_navigation_mismatch');
  assert.equal(calls.filter(x => x[0] === 'open').length, 1);
});

test('settled empty results succeed while partial unsettled results receive a bounded recheck', async () => {
  const calls = [];
  const empty = { url: 'https://duckduckgo.com/?q=one', text: 'No results found', nodes: [], settled: true };
  assert.equal((await executor(createAppControlBrowserSearch({ client: client([empty], calls) })).search('one', 5)).result_count, 0);
  assert.equal(calls.filter(x => x[0] === 'inspect').length, 1);
  const partialCalls = [];
  const provider = createAppControlBrowserSearch({ client: client([page(), { ...page(), settled: true }], partialCalls) });
  assert.equal((await executor(provider).search('one', 5)).result_count, 1);
  assert.equal(partialCalls.filter(x => x[0] === 'inspect').length, 2);
});

test('cleanup failure fails the search rather than silently returning results', async () => {
  const mock = client([page()]);
  let yielded = 0;
  mock.close = async () => { throw new Error('failed'); };
  mock.yield = async () => { yielded++; };
  let error;
  try { await executor(createAppControlBrowserSearch({ client: mock })).search('one', 1); } catch (e) { error = e; }
  assert.equal(error.code, 'browser_cleanup_failed');
  assert.equal(failureEvidence('web_search', 'one', error).diagnostics.cleanup_failed, true);
  assert.equal(yielded, 1);
});

test('lease received before interrupted open wait is still cleaned up', async () => {
  let closed = '';
  const mock = {
    async open(_url, options) { options.onLease('early-lease'); throw Object.assign(new Error('cancelled'), { code: 'aborted' }); },
    async close(lease) { closed = lease; return ok({}); },
  };
  await assert.rejects(() => executor(createAppControlBrowserSearch({ client: mock })).search('one', 1), e => e.code === 'aborted');
  assert.equal(closed, 'early-lease');
});

test('role-free h2 and article-scoped h3 variants retain snippets, not navigation h3', () => {
  const snapshot = page();
  delete snapshot.nodes[1].role;
  assert.equal(parseDuckDuckGoSnapshot(snapshot)[0].snippet, 'Full snippet.');
  snapshot.nodes[1].tag = 'h3';
  assert.equal(parseDuckDuckGoSnapshot(snapshot)[0].title, 'Result');
  assert.throws(() => parseDuckDuckGoSnapshot({ ...snapshot, nodes: [{ node_id: 'n', tag: 'h3', name: 'Nav' }] }), e => e.code === 'provider_malformed_response');
});

test('parser rejects credentialed, HTTP, and non-default-port search pages', () => {
  for (const url of ['http://duckduckgo.com/?q=one', 'https://user@duckduckgo.com/?q=one', 'https://duckduckgo.com:444/?q=one']) {
    assert.throws(() => parseDuckDuckGoSnapshot({ ...page(), url }), e => e.code === 'provider_redirected');
  }
});

test('result DNS checks have bounded concurrency, stable ranking and per-call origin reuse', async () => {
  let active = 0, peak = 0, resolutions = 0;
  const provider = { id: 'fixture', transport: 'app_control_browser', limitMax: 10,
    async search() { return { results: [0, 1, 2, 3, 4].map(i => ({ title: `T${i}`, raw_url: `https://h${i === 4 ? 0 : i}.example/${i}` })) }; } };
  const web = executor(provider, async host => {
    active++; peak = Math.max(peak, active); resolutions++;
    await new Promise(resolve => setTimeout(resolve, host === 'h0.example' ? 20 : 2));
    active--; return [{ address: IP, family: 4 }];
  });
  const result = await web.search('one', 5);
  assert.ok(peak <= 3 && peak > 1);
  assert.equal(resolutions, 4);
  assert.deepEqual(result.results.map(x => x.title), ['T0', 'T1', 'T2', 'T3', 'T4']);
  await web.search('one', 5);
  assert.equal(resolutions, 8);
});

test('unstable or truncated empty pages cannot exhaust retries into false empty success', async () => {
  for (const flags of [{ settled: false }, { settled: true, truncated: true }]) {
    const empty = { url: 'https://duckduckgo.com/?q=one', text: 'No results found', nodes: [], ...flags };
    await assert.rejects(() => executor(createAppControlBrowserSearch({ client: client([empty, empty, empty]) })).search('one', 5),
      e => e.code === 'provider_results_not_ready');
  }
});

test('outer cancellation preserves browser attribution and reports pending operation honestly', async () => {
  let release, entered;
  const started = new Promise(resolve => { entered = resolve; });
  const provider = { id: 'browser-fixture', transport: 'app_control_browser', limitMax: 10,
    async search() { entered(); await new Promise(resolve => { release = resolve; }); return { results: [] }; } };
  const web = executor(provider);
  const controller = new AbortController();
  const running = web.search('one', 1, { signal: controller.signal });
  await started;
  controller.abort();
  let error;
  try { await running; } catch (e) { error = e; }
  const result = failureEvidence('web_search', 'one', error);
  assert.equal(result.provider, 'browser-fixture');
  assert.equal(result.transport, 'app_control_browser');
  assert.equal(result.diagnostics.operation_pending, true);
  release();
});

test('fetch re-resolves DNS independently of browser search origin reuse', async () => {
  let reads = 0;
  const provider = { id: 'fixture', transport: 'app_control_browser', limitMax: 10,
    async search() { return { results: [{ title: 'T', raw_url: 'https://rebind.example/a' }] }; } };
  const web = executor(provider, async () => [{ address: ++reads === 1 ? IP : '127.0.0.1', family: 4 }]);
  const result = await web.search('one', 1);
  await assert.rejects(() => web.fetch(result.results[0].url), e => e.code === 'dns_blocked_ip');
  assert.ok(reads > 1);
});

test('lost creation receipt is explicit and never triggers a second open', async () => {
  let opens = 0;
  const mock = { async open() { opens++; throw Object.assign(new Error('lost receipt'), { code: 'browser_unavailable' }); } };
  let error;
  try { await executor(createAppControlBrowserSearch({ client: mock })).search('one', 1); } catch (e) { error = e; }
  assert.equal(opens, 1);
  assert.equal(failureEvidence('web_search', 'one', error).diagnostics.open_outcome_unknown, true);
});

test('diagnostics whitelist cannot include arbitrary text from providers', async () => {
  const provider = { id: 'fixture', transport: 'app_control_browser', limitMax: 10,
    async search() { return { results: [], diagnostics: { open_ms: 'credential', query: 'secret', inspect_ms: 3 } }; } };
  const result = await executor(provider).search('one', 1);
  assert.equal(result.diagnostics.inspect_ms, 3);
  assert.equal(result.diagnostics.query, undefined);
  assert.equal(result.diagnostics.open_ms, undefined);
});

test('mixed public/private answers are excluded and cancellation grants no provenance', async () => {
  const provider = { id: 'fixture', transport: 'app_control_browser', limitMax: 10,
    async search() { return { results: [{ title: 'T', raw_url: 'https://mixed.example/a' }] }; } };
  const web = executor(provider, async () => [{ address: IP, family: 4 }, { address: '127.0.0.1', family: 4 }]);
  await assert.rejects(() => web.search('one', 1), e => e.code === 'provider_malformed_response');
  assert.equal(web.provenance.sourceFor('https://mixed.example/a'), undefined);
  const controller = new AbortController();
  const cancelled = executor(provider, async () => { controller.abort(); return [{ address: IP, family: 4 }]; });
  await assert.rejects(() => cancelled.search('one', 1, { signal: controller.signal }), e => e.code === 'aborted');
  assert.equal(cancelled.provenance.sourceFor('https://mixed.example/a'), undefined);
});
