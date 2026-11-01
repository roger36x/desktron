const INTERNAL_QUERY_RE = /\b(?:localhost|home\.arpa|metadata\.(?:google|aws)\.internal|(?:[\w-]+\.)+(?:local|internal))\b/i;
function mentionsInternalDomain(text) {
  const lower = String(text || '').toLowerCase();
  return String(process.env.DESKTRON_INTERNAL_DOMAINS || '').split(',')
    .map((d) => d.trim().toLowerCase().replace(/^\.+|\.+$/g, '')).filter(Boolean)
    .some((d) => new RegExp('(?:^|[^\\w.-])(?:[\\w-]+\\.)*' + d.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\b').test(lower));
}
const PRIVATE_IP_QUERY_RE = /\b(?:(?:0|10|127)(?:\.\d{1,3}){3}|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])(?:\.\d{1,3}){2}|169\.254(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|192\.(?:0\.0|0\.2|168\.\d{1,3})(?:\.\d{1,3})|198\.(?:(?:18|19)\.\d{1,3}|51\.100)\.\d{1,3}|203\.0\.113\.\d{1,3}|(?:22[4-9]|23\d|24\d|25[0-5])(?:\.\d{1,3}){3})\b|(?:\[?(?:::|::1|::ffff:|64:ff9b(?::1)?:|100:|2001:db8:|2002:|(?:fc|fd|fe[89ab]|fec)[0-9a-f]*:)[0-9a-f:.]*\]?)/i;
const BEARER_QUERY_RE = /\bbearer\s+[a-z0-9._~+\/-]{12,}/i;
const EMAIL_QUERY_RE = /\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/i;
const PHONE_QUERY_RE = /(?<![\d-])(?:\+\d{1,3}[ ().-]*)?(?:\d[ ().-]*){8,14}\d(?![\d-])/;
const SECRET_PROSE_RE = /\b(?:password|passwd|passphrase|api[ _-]?key|access[ _-]?key|client[ _-]?secret|private[ _-]?key|auth(?:entication)?[ _-]?token|oauth[ _-]?token)(?:\s+(?:is|was|equals?|为|是))?\s*[:=]?\s+\S{8,}/i;
const CONFIDENTIAL_MARKER_RE = /\b(?:confidential|internal[ _-]?only|company[ _-]?confidential|proprietary|do[ _-]?not[ _-]?(?:share|distribute)|not[ _-]?for[ _-]?(?:external|public)[ _-]?(?:use|distribution)?)\b|(?:机密|内部资料|仅限内部|禁止外传|不得外传)/i;
const KNOWN_TOKEN_RE = /\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})\b|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b/;
const HIGH_ENTROPY_TOKEN_RE = /\b(?=[A-Za-z0-9_~+\/-]{32,}\b)(?=[A-Za-z0-9_~+\/-]*[A-Za-z])(?=[A-Za-z0-9_~+\/-]*\d)[A-Za-z0-9_~+\/-]+\b/;
const MALFORMED_INLINE_PERCENT_RE = /(?:^|\S)%(?![0-9a-f]{2})(?=\S)/i;
const SENSITIVE_KEY_WORDS = new Set(['token', 'secret', 'credential', 'password', 'passwd', 'signature', 'jwt', 'assertion']);
const SENSITIVE_KEY_EXACT = new Set(['token', 'secret', 'key', 'credential', 'private', 'code', 'assertion', 'ticket',
  'authorization', 'auth', 'jwt', 'samlresponse', 'session', 'sig', 'signature', 'password', 'passwd', 'apikey',
  'oauthverifier', 'googleaccessid']);
const SENSITIVE_KEY_CONTEXT = new Set(['access', 'refresh', 'auth', 'oauth', 'api', 'client', 'private', 'session',
  'relay', 'security', 'googleaccess', 'x', 'amz', 'goog']);
const SENSITIVE_KEY_SUFFIX = new Set(['id', 'key', 'code', 'ticket', 'verifier']);

function decodeValidPercentRuns(value) {
  return value.replace(/(?:%[0-9a-f]{2})+/gi, run => {
    try { return decodeURIComponent(run); }
    catch {
      return run.replace(/%([0-9a-f]{2})/gi, (_match, hex) =>
        String.fromCharCode(Number.parseInt(hex, 16)));
    }
  });
}

export function decodeForInspection(raw) {
  let value = String(raw).replace(/\+/g, ' ');
  for (let attempt = 0; attempt < 3; attempt++) {
    const decoded = decodeValidPercentRuns(value);
    if (decoded === value) break;
    value = decoded;
  }
  return value;
}

export function isSensitiveQueryKey(raw) {
  const decoded = decodeForInspection(raw);
  const compact = decoded.toLowerCase().replace(/[^a-z0-9]/g, '');
  if (SENSITIVE_KEY_EXACT.has(compact)) return true;
  const words = decoded.replace(/([a-z0-9])([A-Z])/g, '$1 $2').toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
  if (words.some(word => SENSITIVE_KEY_WORDS.has(word))) return true;
  return words.some(word => SENSITIVE_KEY_CONTEXT.has(word)) && words.some(word => SENSITIVE_KEY_SUFFIX.has(word));
}

function containsCredential(text) {
  if (BEARER_QUERY_RE.test(text) || SECRET_PROSE_RE.test(text) || KNOWN_TOKEN_RE.test(text) || HIGH_ENTROPY_TOKEN_RE.test(text)) return true;
  for (const match of String(text).matchAll(/(?:^|[\s?&;,])([^\s?&;,=:]{1,80})\s*[:=]\s*([^\s&;,]+)/g)) {
    if (isSensitiveQueryKey(match[1]) && match[2]) return true;
  }
  return false;
}

export function inspectPublicSearchQuery(raw) {
  const source = String(raw);
  const text = decodeForInspection(source);
  const categories = [];
  if (MALFORMED_INLINE_PERCENT_RE.test(source)) categories.push('malformed_encoding');
  if (INTERNAL_QUERY_RE.test(text) || mentionsInternalDomain(text) || CONFIDENTIAL_MARKER_RE.test(text)) categories.push('internal_data');
  if (PRIVATE_IP_QUERY_RE.test(text)) categories.push('private_network');
  if (containsCredential(text)) categories.push('credential');
  if (EMAIL_QUERY_RE.test(text)) categories.push('email');
  if (PHONE_QUERY_RE.test(text)) categories.push('phone');
  return { allowed: categories.length === 0, categories };
}

export function enforcePublicSearchEgress(query) {
  return inspectPublicSearchQuery(query);
}
