import { createHash } from 'node:crypto';
import { execFile } from 'node:child_process';
import { lookup as dnsLookup } from 'node:dns/promises';
import http from 'node:http';
import https from 'node:https';
import { BlockList, isIP } from 'node:net';
import { promisify } from 'node:util';
import { brotliDecompress, gunzip, inflate } from 'node:zlib';
import { enforcePublicSearchEgress, isSensitiveQueryKey } from './egress_policy.mjs';
import {
  DDG_SEARCH_ENDPOINT, DDG_SEARCH_LIMIT_MAX, DDG_SEARCH_PROVIDER, createDuckDuckGoSearchProvider,
} from './duckduckgo_search.mjs';
import {
  assertSearchProvider, SEARCH_TRANSPORT_DIRECT_HTTP, SEARCH_TRANSPORT_BROWSER_APP_CONTROL,
} from './search_provider.mjs';

export { DDG_SEARCH_ENDPOINT, DDG_SEARCH_PROVIDER };

export const WEB_SCHEMA_VERSION = '1.1';
export const WEB_TOOL_VERSION = '1.1.1';
export const FETCH_PROVIDER = 'desk-public-fetch';
export const SEARCH_LIMIT_MAX = DDG_SEARCH_LIMIT_MAX;
export const FETCH_OUTPUT_MAX_BYTES = 50_000;
export const FETCH_RESPONSE_MAX_BYTES = 1_048_576;
export const FETCH_DECODED_MAX_BYTES = 2_097_152;
export const MAX_COMPRESSION_RATIO = 40;
export const MAX_REDIRECTS = 3;
export const MAX_CRAWL_DEPTH = 2;
export const FETCH_OUTPUT_MAX_LINES = 2000;
export const MAX_SESSION_TOOL_CALLS = 20;
export const MAX_CONCURRENT_TOOL_CALLS = 2;
export const MAX_SESSION_DECODED_BYTES = 8 * 1024 * 1024;
const USER_AGENT = 'Desktron-WebTools/1.0 (+public-readonly)';
const DEFAULT_TIMEOUT_MS = 10_000;
const ROBOTS_MAX_BYTES = 131_072;
const SEARCH_MAX_BYTES = 1_048_576;
const HTML_TYPES = new Set(['text/html', 'application/xhtml+xml', 'text/plain']);
const REDIRECT_CODES = new Set([301, 302, 303, 307, 308]);
const SEARCH_HEADER_ALLOWLIST = new Set(['accept', 'accept-language', 'content-type']);
const SEARCH_BROWSER_RECOMMENDABLE_ERRORS = new Set(['dns_failed', 'dns_empty', 'timeout', 'network_error']);
const FAKE_IP_CACHE_MS = 30_000;
const TRUSTED_DNS_CACHE_MAX_MS = 60_000;
const TRUSTED_DNS_TIMEOUT_MS = 5_000;
const TRUSTED_DNS_MAX_BYTES = 65_536;
const TRUSTED_DOH_HOST = 'cloudflare-dns.com';
const TRUSTED_DOH_ADDRESSES = ['1.1.1.1', '1.0.0.1'];
const SEARCH_CHALLENGE_COOLDOWN_MS = 5 * 60_000;
const SEARCH_RATE_LIMIT_COOLDOWN_MS = 60_000;
const execFileAsync = promisify(execFile);
let fakeIpEnvironmentCache = { expiresAt: 0, value: false };
const trustedDnsCache = new Map();

const blocked4 = new BlockList();
for (const [base, bits] of [
  ['0.0.0.0', 8], ['10.0.0.0', 8], ['100.64.0.0', 10], ['127.0.0.0', 8],
  ['169.254.0.0', 16], ['172.16.0.0', 12], ['192.0.0.0', 24], ['192.0.2.0', 24],
  ['192.88.99.0', 24], ['192.168.0.0', 16], ['198.18.0.0', 15], ['198.51.100.0', 24], ['203.0.113.0', 24],
  ['224.0.0.0', 4], ['240.0.0.0', 4], ['255.255.255.255', 32],
]) blocked4.addSubnet(base, bits, 'ipv4');
const blocked6 = new BlockList();
for (const [base, bits] of [
  ['::', 96], ['::ffff:0:0', 96], ['64:ff9b::', 96], ['64:ff9b:1::', 48], ['100::', 64],
  ['2001::', 23], ['2001:db8::', 32], ['2002::', 16], ['3fff::', 20], ['5f00::', 16],
  ['fc00::', 7], ['fe80::', 10], ['fec0::', 10], ['ff00::', 8],
]) blocked6.addSubnet(base, bits, 'ipv6');

export class WebToolError extends Error {
  constructor(code, message, retryable = false) {
    super(message || code);
    this.name = 'WebToolError';
    this.code = code;
    this.retryable = retryable;
  }
}

class AsyncGate {
  constructor(limit) {
    this.limit = limit;
    this.active = 0;
    this.waiters = [];
  }

  async acquire(signal) {
    if (signal?.aborted) throw new WebToolError('aborted', '请求已取消');
    if (this.active < this.limit) { this.active += 1; return; }
    await new Promise((resolve, reject) => {
      const waiter = { resolve, reject, signal, settled: false, abort: null };
      waiter.abort = () => {
        if (waiter.settled) return;
        waiter.settled = true;
        const index = this.waiters.indexOf(waiter);
        if (index >= 0) this.waiters.splice(index, 1);
        reject(new WebToolError('aborted', '请求已取消'));
      };
      signal?.addEventListener('abort', waiter.abort, { once: true });
      this.waiters.push(waiter);
    });
  }

  release() {
    while (this.waiters.length) {
      const waiter = this.waiters.shift();
      if (waiter.settled) continue;
      waiter.settled = true;
      waiter.signal?.removeEventListener('abort', waiter.abort);
      waiter.resolve();
      return;
    }
    this.active = Math.max(0, this.active - 1);
  }
}

function isFakeIpAddress(address) {
  const value = normalizedHostname(address);
  if (isIP(value) !== 4) return false;
  const octets = value.split('.').map(Number);
  return octets[0] === 198 && (octets[1] === 18 || octets[1] === 19);
}

function fakeDnsUsesInterface(output, interfaceName) {
  return String(output).split(/(?=^resolver #\d+)/m).some(block =>
    new RegExp(`^\\s*if_index\\s*:.*\\(${interfaceName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\)`, 'm').test(block) &&
    /^\s*nameserver\[\d+\]\s*:\s*198\.(?:18|19)\./m.test(block));
}

export function detectLocalFakeIpProxy(address, routeOutput, dnsOutput) {
  if (!isFakeIpAddress(address)) return false;
  const interfaceName = /^\s*interface\s*:\s*(utun\d+)\s*$/m.exec(String(routeOutput))?.[1];
  return Boolean(interfaceName && fakeDnsUsesInterface(dnsOutput, interfaceName));
}

async function defaultConfirmLocalFakeIpProxy(address, signal) {
  if (process.platform !== 'darwin' || !isFakeIpAddress(address)) return false;
  if (signal?.aborted) throw new WebToolError('aborted', '请求已取消');
  const now = Date.now();
  if (fakeIpEnvironmentCache.expiresAt > now) return fakeIpEnvironmentCache.value;
  let value = false;
  try {
    const commandOptions = { encoding: 'utf8', maxBuffer: 512 * 1024, timeout: 2_000, signal };
    const [route, dns] = await Promise.all([
      execFileAsync('/sbin/route', ['-n', 'get', address], commandOptions),
      execFileAsync('/usr/sbin/scutil', ['--dns'], commandOptions),
    ]);
    value = detectLocalFakeIpProxy(address, route.stdout, dns.stdout);
  } catch (error) {
    if (signal?.aborted || error?.name === 'AbortError') throw new WebToolError('aborted', '请求已取消');
  }
  fakeIpEnvironmentCache = { expiresAt: now + FAKE_IP_CACHE_MS, value };
  return value;
}

export function requestTrustedDns(hostname, type, address, signal) {
  return new Promise((resolve, reject) => {
    const url = new URL(`https://${TRUSTED_DOH_HOST}/dns-query`);
    url.searchParams.set('name', hostname);
    url.searchParams.set('type', type);
    let settled = false;
    let size = 0;
    let deadline;
    let request;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      if (deadline) clearTimeout(deadline);
      signal?.removeEventListener('abort', abort);
      if (error) reject(error); else resolve(value);
    };
    request = https.request(url, {
      method: 'GET', agent: false,
      headers: { accept: 'application/dns-json', 'user-agent': USER_AGENT },
      lookup: (_hostname, options, callback) => {
        const row = { address, family: 4 };
        if (options?.all) callback(null, [row]); else callback(null, row.address, row.family);
      },
    }, response => {
      if (response.statusCode !== 200) {
        response.resume();
        finish(new WebToolError('trusted_dns_failed', '可信公网 DNS 查询失败', true));
        return;
      }
      const chunks = [];
      response.on('data', chunk => {
        size += chunk.length;
        if (size > TRUSTED_DNS_MAX_BYTES) {
          response.destroy();
          finish(new WebToolError('trusted_dns_failed', '可信公网 DNS 响应超过上限'));
        } else chunks.push(chunk);
      });
      response.on('end', () => {
        try { finish(null, JSON.parse(Buffer.concat(chunks).toString('utf8'))); }
        catch { finish(new WebToolError('trusted_dns_failed', '可信公网 DNS 响应无效')); }
      });
      response.on('error', () => finish(new WebToolError('trusted_dns_failed', '可信公网 DNS 读取失败', true)));
    });
    deadline = setTimeout(() => request.destroy(new WebToolError('trusted_dns_failed', '可信公网 DNS 查询超时', true)),
      TRUSTED_DNS_TIMEOUT_MS);
    deadline.unref?.();
    request.on('error', error => finish(error instanceof WebToolError ? error :
      new WebToolError('trusted_dns_failed', '可信公网 DNS 请求失败', true)));
    const abort = () => request.destroy(new WebToolError('aborted', '请求已取消'));
    if (signal?.aborted) abort(); else signal?.addEventListener('abort', abort, { once: true });
    request.end();
  });
}

export async function queryTrustedDns(hostname, type, signal) {
  let lastError;
  for (const address of TRUSTED_DOH_ADDRESSES) {
    try { return await requestTrustedDns(hostname, type, address, signal); }
    catch (error) {
      if (error?.code === 'aborted') throw error;
      lastError = error;
    }
  }
  throw lastError || new WebToolError('trusted_dns_failed', '可信公网 DNS 查询失败', true);
}

export function parseTrustedDnsResponse(hostname, type, payload) {
  const queryType = type === 'A' ? 1 : type === 'AAAA' ? 28 : 0;
  const queriedName = normalizedHostname(hostname);
  if (!queryType || !payload || Number(payload.Status) !== 0 || payload.TC === true || !Array.isArray(payload.Question) ||
      payload.Question.length !== 1 || normalizedHostname(payload.Question[0]?.name) !== queriedName ||
      Number(payload.Question[0]?.type) !== queryType) {
    throw new WebToolError('trusted_dns_failed', '可信公网 DNS 响应与查询不匹配');
  }
  const answers = Array.isArray(payload.Answer) ? payload.Answer : [];
  const reachableNames = new Set([queriedName]);
  let changed = true;
  while (changed) {
    changed = false;
    for (const row of answers) {
      if (Number(row?.type) !== 5 || !reachableNames.has(normalizedHostname(row?.name))) continue;
      const target = normalizedHostname(row?.data);
      if (target && !reachableNames.has(target)) { reachableNames.add(target); changed = true; }
    }
  }
  const records = answers.flatMap(row => {
    if (Number(row?.type) !== queryType || !reachableNames.has(normalizedHostname(row?.name))) return [];
    const address = normalizedHostname(row?.data);
    const family = isIP(address);
    const ttl = Number(row?.TTL ?? row?.ttl);
    return family === (queryType === 1 ? 4 : 6)
      ? [{ address, family, ttl: Number.isFinite(ttl) && ttl > 0 ? ttl : 1 }] : [];
  });
  return [...new Map(records.map(row => [`${row.family}:${row.address}`, row])).values()];
}

async function defaultResolveTrustedHost(hostname, signal) {
  if (signal?.aborted) throw new WebToolError('aborted', '请求已取消');
  const cached = trustedDnsCache.get(hostname);
  if (cached?.expiresAt > Date.now()) return cached.addresses.map(({ address, family }) => ({ address, family }));
  const settled = await Promise.allSettled([
    queryTrustedDns(hostname, 'A', signal), queryTrustedDns(hostname, 'AAAA', signal),
  ]);
  const aborted = settled.find(row => row.status === 'rejected' && row.reason?.code === 'aborted');
  if (signal?.aborted || aborted) throw new WebToolError('aborted', '请求已取消');
  if (settled.every(row => row.status === 'rejected')) {
    throw new WebToolError('trusted_dns_failed', '可信公网 DNS 查询失败', true);
  }
  const records = settled.flatMap((row, index) => {
    if (row.status !== 'fulfilled') return [];
    return parseTrustedDnsResponse(hostname, index === 0 ? 'A' : 'AAAA', row.value);
  });
  if (!records.length) throw new WebToolError('dns_empty', '公网域名没有可用地址', true);
  const ttlSeconds = Math.max(1, Math.min(TRUSTED_DNS_CACHE_MAX_MS / 1000, ...records.map(row => row.ttl)));
  trustedDnsCache.set(hostname, { expiresAt: Date.now() + ttlSeconds * 1000, addresses: records });
  return records.map(({ address, family }) => ({ address, family }));
}

function boundedInteger(raw, fallback, minimum, maximum) {
  const value = Number(raw);
  return Number.isSafeInteger(value) ? Math.max(minimum, Math.min(value, maximum)) : fallback;
}

function cleanText(value, limit) {
  return String(value ?? '').replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, '').trim().slice(0, limit);
}

function sha256(value) {
  return 'sha256:' + createHash('sha256').update(value).digest('hex');
}

function citationId(kind, ...parts) {
  return `${kind}:${createHash('sha256').update(parts.join('\0')).digest('hex').slice(0, 32)}`;
}

function normalizedHostname(hostname) {
  return String(hostname || '').toLowerCase().replace(/^\[/, '').replace(/\]$/, '').replace(/\.$/, '');
}

export function isBlockedIp(address) {
  const value = normalizedHostname(address);
  const family = isIP(value);
  if (!family) return true;
  try { return (family === 4 ? blocked4 : blocked6).check(value, family === 4 ? 'ipv4' : 'ipv6'); }
  catch { return true; }
}

// 组织内网域名由部署方声明(逗号分隔),例如 DESKTRON_INTERNAL_DOMAINS=corp.example,intra.example。
// 这些域名下的地址不经网页工具外取。
export function internalDomains() {
  return String(process.env.DESKTRON_INTERNAL_DOMAINS || '').split(',')
    .map((d) => d.trim().toLowerCase().replace(/^\.+|\.+$/g, '')).filter(Boolean);
}

function blockedHostname(hostname) {
  const host = normalizedHostname(hostname);
  if (!host) return 'missing_host';
  if (host === 'localhost' || host.endsWith('.localhost') || host.endsWith('.local') ||
      host.endsWith('.internal') || host.endsWith('.home.arpa')) return 'local_host';
  if (host === 'metadata.google.internal' || host === 'metadata.aws.internal') return 'metadata_host';
  if (internalDomains().some((d) => host === d || host.endsWith('.' + d))) return 'internal_host';
  if (isIP(host) && isBlockedIp(host)) return 'blocked_ip';
  return undefined;
}

function sensitiveQuerySegments(url) {
  return String(url.search || '').slice(1).split(/([&;])/).map((part, index) => {
    if (index % 2) return { separator: part };
    const equals = part.indexOf('=');
    const rawKey = equals < 0 ? part : part.slice(0, equals);
    return { part, rawKey, sensitive: isSensitiveQueryKey(rawKey) };
  });
}

function parsePublicUrl(raw) {
  if (typeof raw !== 'string' || !raw.trim()) throw new WebToolError('empty_url', 'URL 不能为空');
  if (/[\u0000-\u001f\u007f\\]/.test(raw)) throw new WebToolError('invalid_characters', 'URL 含不允许的字符');
  let url;
  try { url = new URL(raw); } catch { throw new WebToolError('invalid_url', 'URL 格式无效'); }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') throw new WebToolError('unsupported_protocol', '只允许 HTTP(S) URL');
  if (url.username || url.password) throw new WebToolError('userinfo_not_allowed', 'URL 不允许包含 userinfo');
  if (sensitiveQuerySegments(url).some(row => row.sensitive && row.part.includes('=') && !/=%5Bredacted%5D$/i.test(row.part))) {
    throw new WebToolError('sensitive_query_not_allowed', 'URL 不允许包含凭证型查询参数');
  }
  const reason = blockedHostname(url.hostname);
  if (reason) throw new WebToolError(reason, 'URL 目标不属于可访问的公网地址');
  if (url.port) throw new WebToolError('nonstandard_port_not_allowed', '公开网页工具只允许标准 HTTP(S) 端口');
  return url;
}

export function sanitizeUrl(raw) {
  try {
    const url = new URL(raw);
    url.username = '';
    url.password = '';
    url.hash = '';
    const segments = sensitiveQuerySegments(url);
    if (segments.some(row => row.sensitive)) {
      url.search = '?' + segments.map(row => row.separator || (row.sensitive ? `${row.rawKey}=%5BREDACTED%5D` : row.part)).join('');
    }
    return url.toString();
  } catch { return '[invalid-url]'; }
}

function provenanceKey(raw) {
  const url = parsePublicUrl(raw);
  url.hash = '';
  return url.toString();
}

export function canonicalEvidenceUrl(raw) {
  return sanitizeUrl(parsePublicUrl(raw).toString());
}

async function defaultResolveHost(hostname, signal) {
  if (signal?.aborted) throw new WebToolError('aborted', '请求已取消');
  try { return await dnsLookup(hostname, { all: true, verbatim: true }); }
  catch { throw new WebToolError('dns_failed', '公网域名解析失败', true); }
}

export async function resolvePublicTarget(raw, options = {}) {
  const url = parsePublicUrl(raw);
  const host = normalizedHostname(url.hostname);
  let addresses;
  if (isIP(host)) addresses = [{ address: host, family: isIP(host) }];
  else addresses = await (options.resolveHost || defaultResolveHost)(host, options.signal);
  if (!Array.isArray(addresses) || !addresses.length) throw new WebToolError('dns_empty', '公网域名没有可用地址', true);
  const normalized = addresses.map(row => ({ address: normalizedHostname(row.address), family: Number(row.family) }));
  if (normalized.some(row => ![4, 6].includes(row.family) || isIP(row.address) !== row.family)) {
    throw new WebToolError('dns_invalid', '域名解析返回无效地址');
  }
  const blocked = normalized.filter(row => isBlockedIp(row.address));
  if (!blocked.length) return { url, addresses: normalized };
  if (!normalized.every(row => isFakeIpAddress(row.address))) {
    throw new WebToolError('dns_blocked_ip', '域名解析到了受保护地址');
  }
  const confirmLocalFakeIpProxy = options.confirmLocalFakeIpProxy || defaultConfirmLocalFakeIpProxy;
  if (!await confirmLocalFakeIpProxy(normalized[0].address, options.signal)) {
    throw new WebToolError('dns_blocked_ip', '域名解析到了受保护地址');
  }
  const resolveTrustedHost = options.resolveTrustedHost || defaultResolveTrustedHost;
  const trusted = await resolveTrustedHost(host, options.signal);
  if (!Array.isArray(trusted) || !trusted.length) throw new WebToolError('dns_empty', '公网域名没有可用地址', true);
  const approved = trusted.map(row => ({ address: normalizedHostname(row.address), family: Number(row.family) }));
  if (approved.some(row => ![4, 6].includes(row.family) || isIP(row.address) !== row.family)) {
    throw new WebToolError('dns_invalid', '可信公网 DNS 返回无效地址');
  }
  if (approved.some(row => isBlockedIp(row.address))) {
    throw new WebToolError('dns_blocked_ip', '可信公网 DNS 解析到了受保护地址');
  }
  return { url, addresses: approved };
}

function headerValue(headers, name) {
  const value = headers[String(name).toLowerCase()];
  return Array.isArray(value) ? value.join(', ') : String(value || '');
}

const gunzipAsync = promisify(gunzip);
const inflateAsync = promisify(inflate);
const brotliAsync = promisify(brotliDecompress);

export async function decodeContent(bytes, encoding, options = {}) {
  const maxDecodedBytes = boundedInteger(options.maxDecodedBytes, FETCH_DECODED_MAX_BYTES, 1024, FETCH_DECODED_MAX_BYTES);
  const maxRatio = boundedInteger(options.maxRatio, MAX_COMPRESSION_RATIO, 1, MAX_COMPRESSION_RATIO);
  const kind = String(encoding || 'identity').trim().toLowerCase();
  let decoded;
  try {
    if (!kind || kind === 'identity') decoded = bytes;
    else if (kind === 'gzip' || kind === 'x-gzip') decoded = await gunzipAsync(bytes, { maxOutputLength: maxDecodedBytes + 1 });
    else if (kind === 'deflate') decoded = await inflateAsync(bytes, { maxOutputLength: maxDecodedBytes + 1 });
    else if (kind === 'br') decoded = await brotliAsync(bytes, { maxOutputLength: maxDecodedBytes + 1 });
    else throw new WebToolError('unsupported_content_encoding', '响应使用了不支持的压缩格式');
  } catch (error) {
    if (error instanceof WebToolError) throw error;
    throw new WebToolError('decompression_failed', '响应解压失败或超过上限');
  }
  if (decoded.byteLength > maxDecodedBytes) throw new WebToolError('decoded_response_too_large', '解压后的响应超过上限');
  if (bytes.byteLength && decoded.byteLength / bytes.byteLength > maxRatio) {
    throw new WebToolError('compression_ratio_exceeded', '响应压缩比超过上限');
  }
  return decoded;
}

async function defaultRequest({ target, method = 'GET', headers = {}, body, signal, timeoutMs, maxCompressedBytes }) {
  return await new Promise((resolve, reject) => {
    const client = target.url.protocol === 'https:' ? https : http;
    const addresses = target.addresses;
    const lookup = (_hostname, lookupOptions, callback) => {
      const rows = addresses.map(row => ({ address: row.address, family: row.family }));
      if (lookupOptions?.all) callback(null, rows);
      else callback(null, rows[0].address, rows[0].family);
    };
    let settled = false;
    const deadline = setTimeout(() => request.destroy(new WebToolError('timeout', '网络请求超时', true)), timeoutMs);
    deadline.unref?.();
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(deadline);
      signal?.removeEventListener('abort', abort);
      if (error) reject(error); else resolve(value);
    };
    const request = client.request(target.url, { method, headers, lookup, agent: false }, response => {
      const chunks = [];
      let size = 0;
      const length = Number(response.headers['content-length']);
      if (Number.isFinite(length) && length > maxCompressedBytes) {
        response.destroy();
        finish(new WebToolError('response_too_large', '响应超过压缩大小上限'));
        return;
      }
      response.on('data', chunk => {
        size += chunk.length;
        if (size > maxCompressedBytes) {
          response.destroy();
          finish(new WebToolError('response_too_large', '响应超过压缩大小上限'));
          return;
        }
        chunks.push(chunk);
      });
      response.on('end', () => finish(null, {
        status: Number(response.statusCode || 0),
        headers: Object.fromEntries(Object.entries(response.headers).map(([key, value]) => [key.toLowerCase(), value])),
        body: Buffer.concat(chunks),
      }));
      response.on('error', error => finish(error instanceof WebToolError ? error : new WebToolError('network_error', '网络读取失败', true)));
    });
    request.setTimeout(timeoutMs, () => request.destroy(new WebToolError('timeout', '网络请求超时', true)));
    request.on('error', error => finish(error instanceof WebToolError ? error : new WebToolError('network_error', '网络请求失败', true)));
    const abort = () => request.destroy(new WebToolError('aborted', '请求已取消'));
    if (signal?.aborted) abort(); else signal?.addEventListener('abort', abort, { once: true });
    if (body) request.write(body);
    request.end();
  });
}

async function safeRequest(rawUrl, options = {}) {
  const timeoutMs = boundedInteger(options.timeoutMs, DEFAULT_TIMEOUT_MS, 1000, 30_000);
  const maxCompressedBytes = boundedInteger(options.maxCompressedBytes, FETCH_RESPONSE_MAX_BYTES, 1024, FETCH_RESPONSE_MAX_BYTES);
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (options.signal?.aborted) abort(); else options.signal?.addEventListener('abort', abort, { once: true });
  try {
    const target = await resolvePublicTarget(rawUrl, { ...options, signal: controller.signal });
    if (controller.signal.aborted) throw new WebToolError('aborted', '请求已取消');
    const response = await (options.request || defaultRequest)({
      target,
      method: options.method || 'GET',
      headers: options.headers || {},
      body: options.body,
      signal: controller.signal,
      timeoutMs,
      maxCompressedBytes,
    });
    if (controller.signal.aborted) throw new WebToolError('aborted', '请求已取消');
    if (!response || !Number.isSafeInteger(response.status) || !response.headers || !Buffer.isBuffer(response.body)) {
      throw new WebToolError('invalid_transport_response', '网络传输返回了无效响应');
    }
    if (response.body.byteLength > maxCompressedBytes) throw new WebToolError('response_too_large', '响应超过压缩大小上限');
    let decoded;
    try { decoded = await decodeContent(response.body, headerValue(response.headers, 'content-encoding'), options); }
    catch (error) {
      if (error instanceof WebToolError) {
        error.httpStatus = response.status;
        error.contentType = contentType(response.headers) || null;
      }
      throw error;
    }
    if (controller.signal.aborted) throw new WebToolError('aborted', '请求已取消');
    options.consumeBytes?.(decoded.byteLength);
    return { ...response, decoded, target };
  } finally {
    options.signal?.removeEventListener('abort', abort);
  }
}

function contentType(headers) {
  return headerValue(headers, 'content-type').split(';', 1)[0].trim().toLowerCase();
}

function assertUtf8Charset(headers) {
  const raw = headerValue(headers, 'content-type');
  const match = /;\s*charset\s*=\s*["']?([^;"'\s]+)/i.exec(raw);
  if (match && !/^utf-?8$/i.test(match[1])) throw new WebToolError('unsupported_charset', '页面字符集暂不支持');
}

function annotateFailure(error, chain, current, status = null, type = null) {
  if (error instanceof WebToolError) {
    if (!Array.isArray(error.redirectChain)) error.redirectChain = [...chain];
    if (error.finalUrl === undefined) error.finalUrl = sanitizeUrl(current);
    if (error.httpStatus === undefined) error.httpStatus = status;
    if (error.contentType === undefined) error.contentType = type;
  }
  return error;
}

function responseFailure(error, response) {
  return annotateFailure(error, response?.redirectChain || [], response?.finalUrl || '',
    response?.status ?? null, response?.headers ? contentType(response.headers) || null : null);
}

function recommendBrowser(error) {
  error.browserRecommended = true;
  return error;
}

async function requestFollowing(rawUrl, options = {}) {
  let current = parsePublicUrl(rawUrl).toString();
  const chain = [];
  for (let redirects = 0; redirects <= (options.maxRedirects ?? MAX_REDIRECTS); redirects++) {
    try { if (options.validateHop) await options.validateHop(current, redirects); }
    catch (error) { throw annotateFailure(error, chain, current); }
    let response;
    try { response = await safeRequest(current, options); }
    catch (error) {
      throw annotateFailure(error, chain, current);
    }
    chain.push({ url: sanitizeUrl(current), status: response.status });
    if (!REDIRECT_CODES.has(response.status)) return { ...response, finalUrl: current, redirectChain: chain };
    const location = headerValue(response.headers, 'location');
    if (!location) throw annotateFailure(new WebToolError('redirect_without_location', '重定向缺少 Location'), chain, current, response.status);
    if (redirects >= (options.maxRedirects ?? MAX_REDIRECTS)) {
      throw annotateFailure(new WebToolError('too_many_redirects', '重定向次数超过上限'), chain, current, response.status);
    }
    let next;
    try { next = new URL(location, current).toString(); }
    catch { throw annotateFailure(new WebToolError('invalid_redirect', '重定向 URL 无效'), chain, current, response.status); }
    let parsed;
    try { parsed = parsePublicUrl(next); }
    catch (error) { throw annotateFailure(error, chain, next, response.status); }
    if (new URL(current).protocol === 'https:' && parsed.protocol === 'http:') {
      throw annotateFailure(new WebToolError('https_downgrade_redirect', '拒绝 HTTPS 降级重定向'), chain, next, response.status);
    }
    current = parsed.toString();
  }
  throw new WebToolError('too_many_redirects', '重定向次数超过上限');
}

function decodeEntities(text) {
  const named = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' };
  return String(text).replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (_match, entity) => {
    const value = String(entity).toLowerCase();
    try {
      if (value.startsWith('#x')) return String.fromCodePoint(Number.parseInt(value.slice(2), 16));
      if (value.startsWith('#')) return String.fromCodePoint(Number.parseInt(value.slice(1), 10));
    } catch { return ' '; }
    return named[value] ?? ' ';
  });
}

export function extractReadable(input, type = '') {
  if (type === 'text/plain') return { title: '', text: String(input).replace(/\s+/g, ' ').trim(), links: [] };
  const html = String(input);
  const title = decodeEntities((/<title[^>]*>([\s\S]*?)<\/title>/i.exec(html)?.[1] || '').replace(/\s+/g, ' ').trim());
  const links = [...html.matchAll(/<a\b[^>]*?\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/gi)]
    .map(match => decodeEntities(match[1] ?? match[2] ?? match[3] ?? ''));
  let text = html
    .replace(/<script\b[\s\S]*?<\/script>/gi, ' ')
    .replace(/<style\b[\s\S]*?<\/style>/gi, ' ')
    .replace(/<noscript\b[\s\S]*?<\/noscript>/gi, ' ')
    .replace(/<!--([\s\S]*?)-->/g, ' ')
    .replace(/<\/(p|div|section|article|h[1-6]|li|tr|blockquote)>/gi, '\n')
    .replace(/<br\s*\/?\s*>/gi, '\n')
    .replace(/<[^>]+>/g, ' ');
  text = decodeEntities(text).replace(/[ \t\f\v]+/g, ' ').replace(/\n\s+/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
  return { title, text, links };
}

function truncateContent(text, maxBytes, maxLines = FETCH_OUTPUT_MAX_LINES) {
  const source = String(text);
  const lines = source.split('\n');
  let output = lines.slice(0, maxLines).join('\n');
  const totalBytes = Buffer.byteLength(source);
  if (Buffer.byteLength(output) > maxBytes) {
    output = Buffer.from(output).subarray(0, maxBytes).toString('utf8');
    while (Buffer.byteLength(output) > maxBytes) output = output.slice(0, -1);
  }
  return { text: output, truncated: output !== source, outputBytes: Buffer.byteLength(output), totalBytes,
    outputLines: output ? output.split('\n').length : 0, totalLines: lines.length };
}

function normalizeRobotsOctets(value) {
  return String(value).replace(/%([0-9a-f]{2})/gi, (match, hex) => {
    const byte = Number.parseInt(hex, 16);
    const char = String.fromCharCode(byte);
    return /[A-Za-z0-9._~-]/.test(char) ? char : '%' + hex.toUpperCase();
  });
}

function robotsMatch(path, pattern) {
  path = normalizeRobotsOctets(path);
  pattern = normalizeRobotsOctets(pattern);
  const anchored = pattern.endsWith('$');
  const source = (anchored ? pattern.slice(0, -1) : pattern)
    .replace(/[.+?^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*');
  try { return new RegExp('^' + source + (anchored ? '$' : '')).test(path); }
  catch { return false; }
}

function robotsAllowed(path, body) {
  const groups = [];
  let current;
  for (const raw of String(body).split(/\r?\n/)) {
    const line = raw.replace(/#.*/, '').trim();
    if (!line) continue;
    const match = /^([a-z-]+)\s*:\s*(.*)$/i.exec(line);
    if (!match) continue;
    const key = match[1].toLowerCase(), value = match[2].trim();
    if (key === 'user-agent') {
      if (!current || current.rules.length) { current = { agents: [], rules: [] }; groups.push(current); }
      current.agents.push(value.toLowerCase());
    } else if ((key === 'allow' || key === 'disallow') && current) current.rules.push({ type: key, path: value });
  }
  const ua = USER_AGENT.toLowerCase();
  const candidates = groups.map(group => ({ group, specificity: Math.max(-1, ...group.agents.map(agent =>
    agent === '*' ? 0 : ua.startsWith(agent) ? agent.length : -1)) })).filter(item => item.specificity >= 0);
  const specificity = Math.max(-1, ...candidates.map(item => item.specificity));
  let best;
  for (const { group } of candidates.filter(item => item.specificity === specificity)) {
    for (const rule of group.rules) {
      const specificity = rule.path.replace(/[\*$]/g, '').length;
      const bestSpecificity = best ? best.path.replace(/[\*$]/g, '').length : -1;
      if (rule.path && robotsMatch(path, rule.path) && (specificity > bestSpecificity ||
          specificity === bestSpecificity && rule.type === 'allow')) best = rule;
    }
  }
  return best?.type !== 'disallow';
}

function discoveredLinks(links, finalUrl) {
  const output = [];
  const seen = new Set();
  for (const raw of links) {
    try {
      const absolute = canonicalEvidenceUrl(new URL(raw, finalUrl).toString());
      if (!seen.has(absolute)) { seen.add(absolute); output.push(absolute); }
    } catch {}
    if (output.length >= 100) break;
  }
  return output;
}

function publicFailure(error) {
  if (error instanceof WebToolError) return { code: error.code, message: error.message, retryable: Boolean(error.retryable) };
  return { code: 'internal_error', message: 'Web 工具执行失败', retryable: false };
}

function asWebToolError(error, fallbackCode = 'provider_malformed_response', fallbackMessage = '搜索服务响应结构无效') {
  if (error instanceof WebToolError) return error;
  if (error && typeof error.code === 'string') {
    const converted = new WebToolError(error.code, error.message || fallbackMessage, Boolean(error.retryable));
    converted.browserRecommended = Boolean(error.browserRecommended);
    if (error.searchDiagnostics) converted.searchDiagnostics = error.searchDiagnostics;
    return converted;
  }
  return new WebToolError(fallbackCode, fallbackMessage);
}

function searchDiagnostics(value) {
  if (!value || typeof value !== 'object') return {};
  const out = {};
  for (const key of ['open_ms', 'inspect_ms', 'parse_ms', 'cleanup_ms', 'provider_ms',
    'validate_urls_ms', 'inspections', 'snapshot_nodes']) {
    if (Number.isFinite(value[key]) && value[key] >= 0) out[key] = Math.round(value[key]);
  }
  for (const key of ['snapshot_truncated', 'cleanup_failed', 'operation_pending', 'open_outcome_unknown']) {
    if (typeof value[key] === 'boolean') out[key] = value[key];
  }
  return Object.keys(out).length ? { diagnostics: out } : {};
}

export function failureEvidence(tool, input, error, now = () => new Date()) {
  const failure = publicFailure(error);
  const timestamp = now().toISOString();
  return {
    schema_version: WEB_SCHEMA_VERSION,
    tool_version: WEB_TOOL_VERSION,
    tool,
    provider: tool === 'web_search' ? (error?.provider || DDG_SEARCH_PROVIDER) : FETCH_PROVIDER,
    transport: error?.transport || SEARCH_TRANSPORT_DIRECT_HTTP,
    content_trust: 'untrusted_web_content',
    browser_recommended: Boolean(error?.browserRecommended),
    status: 'error',
    requested_at: timestamp,
    ...(tool === 'web_search'
      ? { searched_at: timestamp,
          query: failure.code === 'query_may_contain_sensitive_data' ? '[BLOCKED_SENSITIVE_QUERY]' : cleanText(input, 1000),
          result_count: 0, results: [] }
      : { fetched_at: timestamp, url: sanitizeUrl(String(input || '')), final_url: error?.finalUrl || null,
          title: '', content_type: error?.contentType ?? null, http_status: error?.httpStatus ?? null, redirect_chain: error?.redirectChain || [],
          content_hash: null, quoted_text: '', truncated: false }),
    failure_reason: failure.code,
    failure,
    ...(tool === 'web_search' ? searchDiagnostics(error?.searchDiagnostics) : {}),
  };
}

export class WebProvenance {
  constructor() { this.sources = new Map(); }
  authorizeUrl(raw, source) {
    try { this.sources.set(provenanceKey(raw), { ...source }); } catch {}
  }
  authorizeUserText(text) {
    for (const match of String(text || '').matchAll(/https?:\/\/[^\s<>"']+/gi)) {
      const raw = match[0].replace(/[),.;!?\]}]+$/g, '');
      this.authorizeUrl(raw, { kind: 'user_input', depth: 0 });
    }
  }
  authorizeSearch(result) {
    for (const item of result?.results || []) this.authorizeUrl(item.url, {
      kind: 'search_result', query: result.query, citation_id: item.citation?.id, depth: 0,
    });
  }
  authorizeFetch(result) {
    const parentDepth = Number(result?.provenance?.depth || 0);
    if (parentDepth >= MAX_CRAWL_DEPTH) return;
    for (const url of result?.discovered_links || []) this.authorizeUrl(url, {
      kind: 'page_link', parent_url: result.final_url, citation_id: result.citation?.id,
      depth: parentDepth + 1,
    });
  }
  restoreToolResult(toolName, details) {
    if (toolName === 'web_search' && details?.status === 'success') this.authorizeSearch(details);
    if (toolName === 'web_fetch' && details?.status === 'success') this.authorizeFetch(details);
  }
  sourceFor(raw) {
    try { return this.sources.get(provenanceKey(raw)); } catch { return undefined; }
  }
}

export class WebExecutor {
  constructor(options = {}) {
    this.now = options.now || (() => new Date());
    this.provenance = options.provenance || new WebProvenance();
    this.searchProvider = assertSearchProvider(options.searchProvider || createDuckDuckGoSearchProvider());
    this.sessionCalls = 0;
    this.callGate = new AsyncGate(MAX_CONCURRENT_TOOL_CALLS);
    this.searchGate = new AsyncGate(1);
    this.clock = options.clock || Date.now;
    this.searchBlockedUntil = 0;
    this.searchBlockedError = null;
    this.sessionDecodedBytes = 0;
    const upstreamConsume = options.consumeBytes;
    this.options = { ...options, consumeBytes: count => {
      this.sessionDecodedBytes += Number(count || 0);
      if (this.sessionDecodedBytes > MAX_SESSION_DECODED_BYTES) {
        throw new WebToolError('session_byte_budget_exceeded', '本会话网页读取总量超过上限');
      }
      upstreamConsume?.(count);
    } };
  }

  async _execute(signal, task) {
    this.sessionCalls += 1;
    if (this.sessionCalls > MAX_SESSION_TOOL_CALLS) throw new WebToolError('session_call_budget_exceeded', '本会话网页工具调用次数超过上限');
    const controller = new AbortController();
    const timeout = boundedInteger(this.options.operationTimeoutMs, DEFAULT_TIMEOUT_MS, 1000, 30_000);
    let timer;
    let rejectCancellation;
    const cancellation = new Promise((_resolve, reject) => { rejectCancellation = reject; });
    const cancel = () => {
      rejectCancellation(new WebToolError('aborted', '请求已取消'));
      controller.abort();
    };
    if (signal?.aborted) cancel(); else signal?.addEventListener('abort', cancel, { once: true });
    const deadline = new Promise((_resolve, reject) => {
      // Queueing is part of the operation budget. A busy session cannot stretch a nominal
      // ten-second web call into many serial ten-second waits.
      timer = setTimeout(() => {
        reject(new WebToolError('timeout', '网页工具执行超时', true));
        controller.abort();
      }, timeout);
    });
    let operationFinished = false;
    const operation = (async () => {
      try {
        await this.callGate.acquire(controller.signal);
        try { return await task(controller.signal); }
        finally {
          // Keep the permit until the underlying task, including cleanup, stops.
          this.callGate.release();
        }
      } finally { operationFinished = true; }
    })();
    try { return await Promise.race([operation, deadline, cancellation]); }
    catch (error) {
      error.operationPending = !operationFinished;
      throw error;
    }
    finally {
      if (timer) clearTimeout(timer);
      signal?.removeEventListener('abort', cancel);
    }
  }

  async _robots(url, signal) {
    const parsed = new URL(url);
    if (parsed.pathname === '/robots.txt') return;
    const robotsUrl = `${parsed.protocol}//${parsed.host}/robots.txt`;
    const response = await requestFollowing(robotsUrl, {
      ...this.options, signal, maxRedirects: 2, maxCompressedBytes: ROBOTS_MAX_BYTES,
      maxDecodedBytes: ROBOTS_MAX_BYTES, headers: { 'user-agent': USER_AGENT, accept: 'text/plain' },
    });
    if (response.status === 404 || response.status === 410) return;
    if (response.status < 200 || response.status >= 300) {
      throw responseFailure(new WebToolError('robots_unavailable', 'robots.txt 暂不可用', true), response);
    }
    if (!robotsAllowed((parsed.pathname || '/') + parsed.search, response.decoded.toString('utf8'))) {
      throw responseFailure(new WebToolError('robots_disallow', 'robots.txt 不允许抓取该路径'), response);
    }
  }

  async fetch(rawUrl, options = {}) {
    return this._execute(options.signal, signal => this._fetch(rawUrl, { ...options, signal }));
  }

  async _fetch(rawUrl, options = {}) {
    const requestedAt = this.now().toISOString();
    const provenance = this.provenance.sourceFor(rawUrl);
    if (!provenance) throw new WebToolError('url_provenance_required', 'URL 必须来自用户输入、web_search 结果或已读取页面链接');
    if (Number(provenance.depth || 0) > MAX_CRAWL_DEPTH) throw new WebToolError('crawl_depth_exceeded', '网页抓取深度超过上限');
    const initialUrl = parsePublicUrl(rawUrl).toString();
    await this._robots(initialUrl, options.signal);
    const response = await requestFollowing(initialUrl, {
      ...this.options, signal: options.signal,
      validateHop: async (url, index) => { if (index > 0) await this._robots(url, options.signal); },
      maxCompressedBytes: boundedInteger(options.maxResponseBytes, FETCH_RESPONSE_MAX_BYTES, 1024, FETCH_RESPONSE_MAX_BYTES),
      maxDecodedBytes: FETCH_DECODED_MAX_BYTES,
      headers: { 'user-agent': USER_AGENT, accept: 'text/html, application/xhtml+xml, text/plain;q=0.9' },
    });
    if (response.status < 200 || response.status >= 300) {
      const error = annotateFailure(new WebToolError(`http_${response.status}`, '网页返回非成功状态', response.status >= 500 || response.status === 429),
        response.redirectChain, response.finalUrl, response.status);
      throw ([403, 429].includes(response.status) ? recommendBrowser(error) : error);
    }
    const type = contentType(response.headers);
    if (!HTML_TYPES.has(type)) throw responseFailure(new WebToolError('unsupported_content_type', '网页内容类型暂不支持'), response);
    try { assertUtf8Charset(response.headers); }
    catch (error) { throw responseFailure(error, response); }
    const sourceText = response.decoded.toString('utf8');
    const readable = extractReadable(sourceText, type);
    if (!readable.text && type !== 'text/plain' && /<script\b|\bid=["'](?:root|app|__next)["']/i.test(sourceText)) {
      throw responseFailure(recommendBrowser(new WebToolError('client_render_required', '页面需要浏览器执行 JavaScript 后读取')), response);
    }
    const limited = truncateContent(readable.text, boundedInteger(options.maxOutputBytes, FETCH_OUTPUT_MAX_BYTES, 1024, FETCH_OUTPUT_MAX_BYTES));
    const finalUrl = sanitizeUrl(response.finalUrl);
    const fetchedAt = this.now().toISOString();
    const result = {
      schema_version: WEB_SCHEMA_VERSION,
      tool_version: WEB_TOOL_VERSION,
      tool: 'web_fetch',
      provider: FETCH_PROVIDER,
      transport: SEARCH_TRANSPORT_DIRECT_HTTP,
      content_trust: 'untrusted_web_content',
      browser_recommended: false,
      status: 'success',
      requested_at: requestedAt,
      fetched_at: fetchedAt,
      url: sanitizeUrl(initialUrl),
      final_url: finalUrl,
      title: cleanText(readable.title, 500),
      content_type: type,
      http_status: response.status,
      redirect_chain: response.redirectChain,
      content_hash: sha256(response.decoded),
      content_bytes: response.decoded.byteLength,
      quoted_text: limited.text,
      truncated: limited.truncated,
      truncation: limited.truncated ? { output_bytes: limited.outputBytes, total_bytes: limited.totalBytes,
        output_lines: limited.outputLines, total_lines: limited.totalLines } : null,
      provenance,
      discovered_links: discoveredLinks(readable.links, response.finalUrl),
      failure_reason: null,
      citation: { id: citationId('web-fetch', finalUrl, sha256(response.decoded), WEB_TOOL_VERSION), url: finalUrl,
        title: cleanText(readable.title, 500), retrieved_at: fetchedAt },
    };
    this.provenance.authorizeFetch(result);
    return result;
  }

  async search(query, limit = 5, options = {}) {
    try { return await this._execute(options.signal, async signal => {
      await this.searchGate.acquire(signal);
      try {
        return await this._search(query, limit, { ...options, signal });
      } catch (error) {
        error.provider ||= this.searchProvider.id;
        error.transport ||= this.searchProvider.transport;
        throw error;
      } finally { this.searchGate.release(); }
    }); } catch (error) {
      // Outer cancellation/deadline can win before the provider's catch/cleanup.
      error.provider ||= this.searchProvider.id;
      error.transport ||= this.searchProvider.transport;
      if (error.operationPending) error.searchDiagnostics = { ...error.searchDiagnostics, operation_pending: true };
      throw error;
    }
  }

  async _search(query, limit = 5, options = {}) {
    const provider = this.searchProvider;
    const text = cleanText(query, 1000);
    if (!text) {
      const error = new WebToolError('empty_query', '搜索词不能为空');
      error.provider = provider.id;
      throw error;
    }
    const egress = enforcePublicSearchEgress(text);
    if (!egress.allowed) {
      const error = new WebToolError('query_may_contain_sensitive_data', '搜索词未通过公网外发策略');
      error.egressCategories = egress.categories;
      error.provider = provider.id;
      throw error;
    }
    const count = boundedInteger(limit, 5, 1, provider.limitMax);
    const requestedAt = this.now().toISOString();
    if (this.searchBlockedUntil > this.clock() && this.searchBlockedError) {
      const cached = new WebToolError(this.searchBlockedError.code, this.searchBlockedError.message,
        this.searchBlockedError.retryable);
      cached.browserRecommended = this.searchBlockedError.browserRecommended;
      cached.provider = provider.id;
      cached.transport = provider.transport;
      throw cached;
    }
    if (provider.transport === SEARCH_TRANSPORT_BROWSER_APP_CONTROL) {
      return await this._searchBrowser(provider, text, count, requestedAt, options.signal);
    }
    let providerRequest;
    try { providerRequest = provider.buildRequest(text, count); }
    catch (error) {
      const converted = asWebToolError(error);
      converted.provider = provider.id;
      throw converted;
    }
    if (String(providerRequest.method || 'POST').toUpperCase() !== 'POST') {
      throw new WebToolError('provider_method_not_allowed', '搜索 provider 只允许 POST');
    }
    const providerHeaders = Object.fromEntries(Object.entries(providerRequest.headers || {}).map(([name, value]) => [name.toLowerCase(), value]));
    if (Object.keys(providerHeaders).some(name => name !== 'content-length' && !SEARCH_HEADER_ALLOWLIST.has(name))) {
      throw new WebToolError('provider_header_not_allowed', '搜索 provider 包含不允许的请求头');
    }
    if (typeof providerRequest.body !== 'string' && !Buffer.isBuffer(providerRequest.body)) {
      throw new WebToolError('provider_body_required', '搜索 provider 必须提供固定编码的请求体');
    }
    delete providerHeaders['content-length'];
    const headers = { ...providerHeaders, 'content-length': String(Buffer.byteLength(providerRequest.body)), 'user-agent': USER_AGENT };
    let response;
    try {
      response = await requestFollowing(providerRequest.url, {
        ...this.options, signal: options.signal, method: 'POST',
        maxRedirects: providerRequest.maxRedirects ?? 0,
        maxCompressedBytes: SEARCH_MAX_BYTES, maxDecodedBytes: SEARCH_MAX_BYTES,
        headers, body: providerRequest.body,
      });
    } catch (error) {
      const converted = asWebToolError(error, 'network_error', '搜索网络请求失败');
      converted.provider = provider.id;
      if (SEARCH_BROWSER_RECOMMENDABLE_ERRORS.has(converted.code)) converted.browserRecommended = true;
      throw converted;
    }
    try { assertUtf8Charset(response.headers); }
    catch (error) {
      const converted = responseFailure(error, response);
      converted.provider = provider.id;
      converted.browserRecommended = true;
      throw converted;
    }
    let normalized;
    try {
      normalized = provider.parseResponse({
        status: response.status,
        headers: response.headers,
        contentType: contentType(response.headers),
        body: response.decoded,
        finalUrl: response.finalUrl,
      }, count);
    } catch (error) {
      const converted = responseFailure(asWebToolError(error), response);
      converted.provider = provider.id;
      const cooldown = converted.code === 'provider_challenge' ? SEARCH_CHALLENGE_COOLDOWN_MS
        : converted.code === 'provider_rate_limited' ? SEARCH_RATE_LIMIT_COOLDOWN_MS : 0;
      if (cooldown) {
        this.searchBlockedUntil = this.clock() + cooldown;
        this.searchBlockedError = {
          code: converted.code, message: converted.message, retryable: converted.retryable,
          browserRecommended: converted.browserRecommended,
        };
      }
      throw converted;
    }
    if (!normalized || !Array.isArray(normalized.results)) {
      const error = responseFailure(new WebToolError('provider_malformed_response', '搜索服务响应结构无效'), response);
      error.provider = provider.id;
      throw error;
    }
    const results = [];
    let rejectedUrls = 0;
    for (const row of normalized.results) {
      const rawUrl = cleanText(row?.raw_url ?? row?.url, 4096);
      const title = cleanText(row?.title, 500);
      if (!rawUrl || !title) continue;
      let url;
      try { url = canonicalEvidenceUrl(rawUrl); }
      catch { rejectedUrls += 1; continue; }
      const snippet = cleanText(row?.snippet, 2000);
      const contentHash = sha256(Buffer.from(`${title}\n${url}\n${snippet}`));
      results.push({
        rank: results.length + 1, title, url, snippet,
        published_at: cleanText(row?.published_at, 80) || null,
        content_hash: contentHash,
        citation: { id: `web-search:${contentHash.slice(7, 39)}`, url, title, retrieved_at: this.now().toISOString() },
      });
      if (results.length >= count) break;
    }
    if (!results.length && normalized.results.length) {
      const message = rejectedUrls ? '搜索服务只返回了不安全或无效 URL' : '搜索服务只返回了不完整结果';
      const error = responseFailure(new WebToolError('provider_malformed_response', message), response);
      error.provider = provider.id;
      error.browserRecommended = !rejectedUrls;
      throw error;
    }
    const result = {
      schema_version: WEB_SCHEMA_VERSION,
      tool_version: WEB_TOOL_VERSION,
      tool: 'web_search',
      provider: provider.id,
      transport: provider.transport,
      content_trust: 'untrusted_web_content',
      browser_recommended: false,
      status: 'success',
      requested_at: requestedAt,
      searched_at: this.now().toISOString(),
      query: text,
      result_count: results.length,
      results,
      content_hash: typeof provider.responseHash === 'function' ? provider.responseHash({ body: response.decoded }) : sha256(response.decoded),
      http_status: response.status,
      content_type: contentType(response.headers),
      redirect_chain: response.redirectChain,
      provider_request_id: cleanText(normalized.provider_request_id, 200) || null,
      failure_reason: null,
    };
    this.provenance.authorizeSearch(result);
    return result;
  }

  async _searchBrowser(provider, text, count, requestedAt, signal) {
    let normalized;
    try {
      normalized = await provider.search(text, count, { signal });
    } catch (error) {
      const converted = asWebToolError(error, 'browser_operation_failed', '后台浏览器搜索失败');
      converted.provider = provider.id;
      converted.transport = provider.transport;
      const cooldown = converted.code === 'provider_challenge' ? SEARCH_CHALLENGE_COOLDOWN_MS : 0;
      if (cooldown) {
        this.searchBlockedUntil = this.clock() + cooldown;
        this.searchBlockedError = {
          code: converted.code, message: converted.message, retryable: converted.retryable,
          browserRecommended: false,
        };
      }
      throw converted;
    }
    if (!normalized || !Array.isArray(normalized.results)) {
      const error = new WebToolError('provider_malformed_response', '浏览器搜索服务响应结构无效');
      error.provider = provider.id;
      error.transport = provider.transport;
      throw error;
    }
    const validateStarted = performance.now();
    // Bound fan-out, retain provider order, and share same-origin DNS checks only
    // within this observation. Fetch always resolves and pins the socket afresh.
    const candidates = normalized.results.slice(0, SEARCH_LIMIT_MAX);
    const checked = new Array(candidates.length);
    const origins = new Map();
    let next = 0;
    let rejectedUrls = 0;
    const worker = async () => {
      while (next < candidates.length && !signal?.aborted) {
        const index = next++;
        const row = candidates[index];
        const rawUrl = cleanText(row?.raw_url ?? row?.url, 4096);
        const title = cleanText(row?.title, 500);
        if (!rawUrl || !title) continue;
        let url;
        try {
          const parsed = parsePublicUrl(rawUrl);
          const origin = parsed.origin;
          if (!origins.has(origin)) origins.set(origin, resolvePublicTarget(rawUrl, { ...this.options, signal }));
          await origins.get(origin);
          url = canonicalEvidenceUrl(parsed.toString());
        } catch { rejectedUrls += 1; continue; }
        const snippet = cleanText(row?.snippet, 2000);
        const contentHash = sha256(Buffer.from(`${title}\n${url}\n${snippet}`));
        checked[index] = {
          title, url, snippet, published_at: cleanText(row?.published_at, 80) || null,
          content_hash: contentHash,
          citation: { id: `web-search:${contentHash.slice(7, 39)}`, url, title, retrieved_at: this.now().toISOString() },
        };
      }
    };
    await Promise.all(Array.from({ length: Math.min(3, candidates.length) }, worker));
    if (signal?.aborted) throw new WebToolError('aborted', '请求已取消');
    const results = checked.filter(Boolean).slice(0, count).map((row, index) => ({ rank: index + 1, ...row }));
    const diagnostics = searchDiagnostics({ ...normalized.diagnostics,
      validate_urls_ms: performance.now() - validateStarted });
    if (!results.length && normalized.results.length) {
      const error = new WebToolError('provider_malformed_response', rejectedUrls
        ? '浏览器搜索只返回了不安全或无效 URL' : '浏览器搜索只返回了不完整结果');
      error.provider = provider.id;
      error.transport = provider.transport;
      throw error;
    }
    const result = {
      schema_version: WEB_SCHEMA_VERSION,
      tool_version: WEB_TOOL_VERSION,
      tool: 'web_search',
      provider: provider.id,
      transport: provider.transport,
      content_trust: 'untrusted_web_content',
      browser_recommended: false,
      status: 'success',
      requested_at: requestedAt,
      searched_at: this.now().toISOString(),
      query: text,
      result_count: results.length,
      results,
      content_hash: normalized.content_hash || sha256(Buffer.from(JSON.stringify(results))),
      ...diagnostics,
      http_status: null,
      content_type: 'text/html',
      redirect_chain: [],
      provider_request_id: null,
      failure_reason: null,
    };
    this.provenance.authorizeSearch(result);
    return result;
  }
}
