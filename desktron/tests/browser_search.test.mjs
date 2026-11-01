import test from 'node:test';
import assert from 'node:assert/strict';
import { createAppControlBrowserSearch, parseDuckDuckGoSnapshot } from '../lib/web_tools/browser_search.mjs';
import { WebExecutor, failureEvidence } from '../lib/web_tools/core.mjs';

const PUBLIC_IP = '93.184.216.34';

function snapshot(query = 'one', target = 'https://example.com/docs?x=1') {
  const redirect = `https://duckduckgo.com/l/?uddg=${encodeURIComponent(target)}`;
  return {
    url: `https://duckduckgo.com/?q=${query}`,
    title: `${query} at DuckDuckGo`, text: 'normal result page', nodes: [
      { node_id: 'n1', parent_id: '', role: 'heading', tag: 'h2', name: `Title ${query}` },
      { node_id: 'n2', parent_id: 'n1', role: 'link', tag: 'a', name: `Title ${query}`, href: redirect },
      { node_id: 'n3', parent_id: '', role: 'link', tag: 'a', name: 'example.com/docs', href: target },
      { node_id: 'n4', parent_id: '', role: 'paragraph', tag: 'p', name: `Snippet for ${query}`, text: `Snippet for ${query}` },
    ],
  };
}

function succeeded(output, leaseId = 'lease-1') {
  return { status: 'succeeded', lease_id: leaseId, output };
}

function executor(provider, resolveHost = async () => [{ address: PUBLIC_IP, family: 4 }]) {
  return new WebExecutor({ searchProvider: provider, resolveHost });
}

test('DDG browser snapshot extracts real links and rejects unsafe provider pages', () => {
  assert.deepEqual(parseDuckDuckGoSnapshot(snapshot(), 1), [{
    title: 'Title one', raw_url: 'https://example.com/docs?x=1', snippet: 'Snippet for one', published_at: null,
  }]);
  assert.throws(() => parseDuckDuckGoSnapshot({ ...snapshot(), url: 'https://127.0.0.1/' }),
    error => error.code === 'provider_redirected');
  assert.throws(() => parseDuckDuckGoSnapshot({ ...snapshot(), text: 'Verify you are human' }),
    error => error.code === 'provider_challenge');
});

test('DDG browser snapshot skips sponsored tracking links before applying limit', () => {
  const real = snapshot();
  const nodes = [
    { node_id: 'a1', parent_id: '', role: 'heading', tag: 'h2', name: 'Sponsored result' },
    { node_id: 'a2', parent_id: 'a1', role: 'link', tag: 'a', name: 'Sponsored result',
      href: `https://duckduckgo.com/y.js?ad_domain=ads.example&uddg=${encodeURIComponent('https://advertiser.example/')}` },
    ...real.nodes,
  ];
  assert.deepEqual(parseDuckDuckGoSnapshot({ ...real, nodes }, 1), [{
    title: 'Title one', raw_url: 'https://example.com/docs?x=1',
    snippet: 'Snippet for one', published_at: null,
  }]);
});

test('DDG browser snapshot treats an all-sponsored page as an empty result set', () => {
  const page = snapshot();
  page.nodes = [
    { node_id: 'a1', parent_id: '', role: 'heading', tag: 'h2', name: 'Offer' },
    { node_id: 'a2', parent_id: 'a1', role: 'link', tag: 'a', name: 'Offer', href: 'https://advertiser.example/' },
    { node_id: 'a3', parent_id: '', role: 'text', tag: 'span', name: 'Sponsored', text: 'Sponsored' },
  ];
  assert.deepEqual(parseDuckDuckGoSnapshot(page, 5), []);
});

test('browser provider creates and destroys one isolated tab per search', async () => {
  const calls = [];
  const client = {
    async open(url, options) {
      calls.push(['open', url, options.allowedMainFrameHosts]);
      return succeeded({ url }, 'lease-7');
    },
    async inspect(lease) { calls.push(['inspect', lease]); return succeeded(snapshot('one'), lease); },
    async close(lease) { calls.push(['close', lease]); return succeeded({}, lease); },
  };
  const result = await executor(createAppControlBrowserSearch({ client })).search('one', 1);
  assert.equal(result.transport, 'app_control_browser');
  assert.equal(result.http_status, null);
  assert.equal(result.results[0].url, 'https://example.com/docs?x=1');
  assert.deepEqual(calls.map(row => row[0]), ['open', 'inspect', 'close']);
  assert.deepEqual(calls[0][2], ['duckduckgo.com']);
});

test('blank and stale-query snapshots fail explicitly and clean up the isolated tab', async () => {
  for (const page of [{ ...snapshot(), url: 'about:blank', nodes: [], settled: false }, snapshot('old')]) {
    const calls = [];
    const provider = createAppControlBrowserSearch({ client: {
      async open(url) { calls.push('open'); return succeeded({ url }, 'lease-nav'); },
      async inspect() { calls.push('inspect'); return succeeded(page, 'lease-nav'); },
      async close() { calls.push('close'); return succeeded({}, 'lease-nav'); },
    } });
    const expectedCode = page.url === 'about:blank' ? 'provider_redirected' : 'provider_navigation_mismatch';
    await assert.rejects(() => provider.search('one', 1), error => error.code === expectedCode);
    assert.deepEqual(calls, page.url === 'about:blank'
      ? ['open', 'inspect', 'close'] : ['open', 'inspect', 'inspect', 'inspect', 'close']);
  }
});

test('browser result URLs require public DNS before provenance authorization', async () => {
  const client = {
    async open(url) { return succeeded({ url }, 'lease-private'); },
    async inspect(lease) { return succeeded(snapshot('one', 'https://private.example/path'), lease); },
    async close(lease) { return succeeded({}, lease); },
  };
  const web = executor(createAppControlBrowserSearch({ client }),
    async () => [{ address: '127.0.0.1', family: 4 }]);
  await assert.rejects(() => web.search('one', 1), error => error.code === 'provider_malformed_response');
  assert.equal(web.provenance.sourceFor('https://private.example/path'), undefined);
});

test('browser search failures preserve evidence and still clean up created tabs', async () => {
  const calls = [];
  const provider = createAppControlBrowserSearch({ client: {
    async open(url) { calls.push('open'); return succeeded({ url }, 'lease-fail'); },
    async inspect() { calls.push('inspect'); throw Object.assign(new Error('offline'), { code: 'browser_unavailable', retryable: true }); },
    async close() { calls.push('close'); return succeeded({}, 'lease-fail'); },
  } });
  const web = executor(provider);
  let error;
  try { await web.search('public query', 1); } catch (caught) { error = caught; }
  const evidence = failureEvidence('web_search', 'public query', error);
  assert.equal(evidence.failure_reason, 'browser_unavailable');
  assert.equal(evidence.provider, 'duckduckgo-browser');
  assert.equal(evidence.transport, 'app_control_browser');
  assert.equal(evidence.browser_recommended, false);
  assert.deepEqual(calls, ['open', 'inspect', 'close']);
});

// Mirrors the live DDG snapshot shape: each result is an <article> whose text holds
// host, URL breadcrumb, title, optional date and the full snippet; the snippet itself
// has no node of its own, only bold query fragments (<b>).
function articleResult(id, { host, url, title, body, bold = [] }) {
  const article = `${id}a`;
  const parsed = new URL(url);
  const crumb = [`${parsed.protocol}//${parsed.host}`, ...parsed.pathname.split('/').filter(Boolean)].join(' › ');
  return [
    { node_id: article, parent_id: 'main', role: 'article', tag: 'article',
      text: `${host} ${crumb} ${title} ${body}` },
    { node_id: `${id}p`, parent_id: article, role: 'paragraph', tag: 'p', text: host },
    { node_id: `${id}u`, parent_id: article, role: 'link', tag: 'a', href: url, text: crumb },
    { node_id: `${id}h`, parent_id: article, role: 'heading', tag: 'h2', name: title },
    { node_id: `${id}l`, parent_id: `${id}h`, role: 'link', tag: 'a', name: title, href: url },
    ...bold.map((text, at) => ({ node_id: `${id}b${at}`, parent_id: article, role: '', tag: 'b', text })),
  ];
}

function liveSnapshot(nodes) {
  return { url: 'https://duckduckgo.com/?q=volc', title: 'volc at DuckDuckGo', text: 'normal', nodes };
}

test('DDG browser snapshot reads the full snippet and date from the result article', () => {
  const nodes = [
    ...articleResult('r1', { host: 'ark.example.com', url: 'https://ark.example.com/docs/pricing',
      title: '模型价格 - 方舟文档', body: '2026年10月7日本文介绍各类模型的计费公式及单价。', bold: ['方舟', '价格'] }),
    ...articleResult('r2', { host: 'docs.example.com', url: 'https://docs.example.com/p/1',
      title: 'docs.example.com', body: '2 天前 不同模型服务支持的能力及单价各不相同。', bold: ['模型'] }),
    ...articleResult('r3', { host: 'video.example.com', url: 'https://video.example.com/watch',
      title: 'Video guide', body: '1:43 Oct 7, 2026 Step by step walkthrough.', bold: ['guide'] }),
  ];
  assert.deepEqual(parseDuckDuckGoSnapshot(liveSnapshot(nodes), 5), [
    { title: '模型价格 - 方舟文档', raw_url: 'https://ark.example.com/docs/pricing',
      snippet: '本文介绍各类模型的计费公式及单价。', published_at: '2026-10-07' },
    { title: 'docs.example.com', raw_url: 'https://docs.example.com/p/1',
      snippet: '不同模型服务支持的能力及单价各不相同。', published_at: '2 天前' },
    { title: 'Video guide', raw_url: 'https://video.example.com/watch',
      snippet: 'Step by step walkthrough.', published_at: '2026-10-07' },
  ]);
});

test('DDG browser snapshot drops DuckDuckGo modules and duplicate URLs before applying limit', () => {
  const nodes = [
    { node_id: 'v1', parent_id: 'main', role: 'heading', tag: 'h2', name: 'volc视频' },
    { node_id: 'v2', parent_id: 'v1', role: 'link', tag: 'a', name: 'volc视频',
      href: 'https://duckduckgo.com/?q=volc&ia=videos&iax=videos' },
    { node_id: 'v3', parent_id: 'main', role: 'heading', tag: 'h3', name: 'A video card' },
    ...articleResult('r1', { host: 'a.example.com', url: 'https://a.example.com/x', title: 'First', body: 'one' }),
    ...articleResult('r2', { host: 'a.example.com', url: 'https://a.example.com/x', title: 'First again', body: 'dup' }),
    ...articleResult('r3', { host: 'b.example.com', url: 'https://b.example.com/y', title: 'Second', body: 'two' }),
  ];
  assert.deepEqual(parseDuckDuckGoSnapshot(liveSnapshot(nodes), 2).map(row => [row.raw_url, row.snippet]), [
    ['https://a.example.com/x', 'one'], ['https://b.example.com/y', 'two'],
  ]);
});
