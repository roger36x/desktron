import { createHash, createHmac, randomBytes as nodeRandomBytes } from 'node:crypto';

const SCHEMA_VERSION = 1;
const MAX_DOCUMENT_BYTES = 64 * 1024;
const MAX_JSON_DEPTH = 64;
const MAX_JSON_NODES = 20_000;
const MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;
const CLOCK_SKEW_MS = 5 * 60 * 1000;
const ID = /^[A-Za-z0-9._:-]{1,160}$/u;
const ROUTE = /^[A-Za-z0-9._:-]{1,64}$/u;
const SHA256 = /^[0-9a-f]{64}$/u;
const AFFINITY = /^aff_v1_[A-Za-z0-9_-]{22}$/u;
const LEVELS = ['off', 'low', 'high'];
const FEATURE_KEYS = ['text', 'images', 'tools', 'streaming', 'cancel'];
const LIMIT_KEYS = ['context_window', 'max_output_tokens', 'max_body_bytes', 'image_limit'];
const TIMEOUT_KEYS = ['first_byte_ms', 'idle_ms', 'total_ms'];
const RETRY_KEYS = ['max_attempts', 'max_delay_ms'];
const AFFINITY_REASONS = new Set(['new_session', 'fork', 'ttl_expired', 'rebind']);
const TRUSTED = new WeakSet();
const TRUST_ASSERTIONS = new WeakSet();

export class ProviderContractError extends Error {
  constructor(code, message) { super(`Provider runtime contract [${code}]: ${message}`); this.name = 'ProviderContractError'; this.code = code; }
}
const fail = (code, message) => { throw new ProviderContractError(code, message); };
const isObject = value => {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
};

function exactKeys(value, keys, label) {
  if (!isObject(value)) fail('SCHEMA', `${label} must be a plain object`);
  const expected = new Set(keys);
  for (const key of Object.keys(value)) if (!expected.has(key)) fail('SCHEMA', `${label} contains unsupported field ${key}`);
  for (const key of keys) if (!Object.hasOwn(value, key)) fail('SCHEMA', `${label} is missing ${key}`);
  return value;
}
function safeInteger(value, label, { min = 0, max = Number.MAX_SAFE_INTEGER } = {}) {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < min || value > max) {
    fail('SCHEMA', `${label} must be an integer between ${min} and ${max}`);
  }
  return value;
}
function safeId(value, label, pattern = ID) {
  if (typeof value !== 'string' || !pattern.test(value)) fail('SCHEMA', `${label} is invalid`);
  return value;
}
function safeBoolean(value, label) {
  if (typeof value !== 'boolean') fail('SCHEMA', `${label} must be boolean`);
  return value;
}
function canonical(value) {
  if (value === null || typeof value === 'boolean' || typeof value === 'string') return JSON.stringify(value);
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) fail('SCHEMA', 'non-finite number');
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (!isObject(value)) fail('SCHEMA', 'unsupported canonical value');
  return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
}
const digest = value => createHash('sha256').update(canonical(value)).digest('hex');
function deepFreeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.freeze(value);
    for (const item of Object.values(value)) deepFreeze(item);
  }
  return value;
}

function parseLimits(raw, label = 'limits') {
  const row = exactKeys(raw, LIMIT_KEYS, label);
  const limits = {
    context_window: safeInteger(row.context_window, `${label}.context_window`, { min: 1024, max: 4_000_000 }),
    max_output_tokens: safeInteger(row.max_output_tokens, `${label}.max_output_tokens`, { min: 1, max: 1_000_000 }),
    max_body_bytes: safeInteger(row.max_body_bytes, `${label}.max_body_bytes`, { min: 1024, max: 1024 ** 3 }),
    image_limit: safeInteger(row.image_limit, `${label}.image_limit`, { min: 0, max: 1024 }),
  };
  if (limits.max_output_tokens > limits.context_window) fail('SCHEMA', `${label}.max_output_tokens exceeds context_window`);
  return limits;
}
function parseFeatures(raw, label = 'features') {
  const row = exactKeys(raw, FEATURE_KEYS, label);
  return Object.fromEntries(FEATURE_KEYS.map(key => [key, safeBoolean(row[key], `${label}.${key}`)]));
}
function parseTimeouts(raw, label = 'timeout_hints') {
  const row = exactKeys(raw, TIMEOUT_KEYS, label);
  const value = {
    first_byte_ms: safeInteger(row.first_byte_ms, `${label}.first_byte_ms`, { min: 100, max: 60 * 60 * 1000 }),
    idle_ms: safeInteger(row.idle_ms, `${label}.idle_ms`, { min: 100, max: 60 * 60 * 1000 }),
    total_ms: safeInteger(row.total_ms, `${label}.total_ms`, { min: 100, max: 24 * 60 * 60 * 1000 }),
  };
  if (value.first_byte_ms > value.total_ms || value.idle_ms > value.total_ms) fail('SCHEMA', `${label} exceeds total_ms`);
  return value;
}
function parseRetry(raw, label = 'retry_hints') {
  const row = exactKeys(raw, RETRY_KEYS, label);
  return {
    max_attempts: safeInteger(row.max_attempts, `${label}.max_attempts`, { min: 0, max: 10 }),
    max_delay_ms: safeInteger(row.max_delay_ms, `${label}.max_delay_ms`, { min: 0, max: 10 * 60 * 1000 }),
  };
}
function parseThinking(raw, label = 'thinking') {
  const row = exactKeys(raw, LEVELS, label); const result = {};
  for (const level of LEVELS) {
    const item = exactKeys(row[level], ['enabled', 'budget_tokens'], `${label}.${level}`);
    const enabled = safeBoolean(item.enabled, `${label}.${level}.enabled`);
    const budget = safeInteger(item.budget_tokens, `${label}.${level}.budget_tokens`, { min: 0, max: 1_000_000 });
    if (level === 'off' && (enabled || budget !== 0)) fail('SCHEMA', `${label}.off must be disabled with zero budget`);
    if (level !== 'off' && (!enabled || budget < 1)) fail('SCHEMA', `${label}.${level} must be enabled with a positive budget`);
    result[level] = { enabled, budget_tokens: budget };
  }
  if (result.low.budget_tokens > result.high.budget_tokens) fail('SCHEMA', `${label}.low exceeds high`);
  return result;
}
function parseAffinity(raw, label = 'affinity') {
  const row = exactKeys(raw, ['mode', 'scope', 'route_group', 'ttl_ms'], label);
  if (row.mode !== 'session' || row.scope !== 'provider-model-config') fail('SCHEMA', `${label} mode or scope is unsupported`);
  return { mode:'session', scope:'provider-model-config', route_group:safeId(row.route_group, `${label}.route_group`, ROUTE),
    ttl_ms:safeInteger(row.ttl_ms, `${label}.ttl_ms`, { min:1000, max:7 * 24 * 60 * 60 * 1000 }) };
}

function semanticBody(capability) {
  return { schema_version:capability.schema_version, provider:capability.provider, model:capability.model,
    config_version:capability.config_version, lifecycle:capability.lifecycle, limits:capability.limits,
    thinking:capability.thinking, features:capability.features, timeout_hints:capability.timeout_hints,
    retry_hints:capability.retry_hints, affinity:capability.affinity };
}
function normalizeCapabilities(raw) {
  const root = exactKeys(raw, ['schema_version', 'provider', 'model', 'config_revision', 'config_version',
    'generated_at_ms', 'expires_at_ms', 'lifecycle', 'limits', 'thinking', 'features',
    'timeout_hints', 'retry_hints', 'affinity'], 'capabilities');
  if (root.schema_version !== SCHEMA_VERSION) fail('SCHEMA_VERSION', 'unsupported schema_version');
  if (!['active', 'retired'].includes(root.lifecycle)) fail('SCHEMA', 'lifecycle is invalid');
  const normalized = {
    schema_version:SCHEMA_VERSION, provider:safeId(root.provider, 'provider'), model:safeId(root.model, 'model'),
    config_revision:safeInteger(root.config_revision, 'config_revision', { min:1 }),
    config_version:safeId(root.config_version, 'config_version'),
    generated_at_ms:safeInteger(root.generated_at_ms, 'generated_at_ms', { min:1, max:10 ** 15 }),
    expires_at_ms:safeInteger(root.expires_at_ms, 'expires_at_ms', { min:1, max:10 ** 15 }),
    lifecycle:root.lifecycle, limits:parseLimits(root.limits), thinking:parseThinking(root.thinking),
    features:parseFeatures(root.features), timeout_hints:parseTimeouts(root.timeout_hints),
    retry_hints:parseRetry(root.retry_hints), affinity:parseAffinity(root.affinity),
  };
  return { ...normalized, semantic_sha256:digest(semanticBody(normalized)), document_sha256:digest(normalized) };
}
function checkFresh(capability, now, label = 'capabilities') {
  if (capability.generated_at_ms > now + CLOCK_SKEW_MS || capability.expires_at_ms <= now
      || capability.expires_at_ms <= capability.generated_at_ms
      || capability.expires_at_ms - capability.generated_at_ms > MAX_AGE_MS) {
    fail('EXPIRED', `${label} is expired or outside the freshness window`);
  }
}
function brandTrustAssertion(details, keys) {
  const trust = exactKeys(details, keys, 'trust');
  const branded = deepFreeze(structuredClone(trust));
  TRUST_ASSERTIONS.add(branded);
  return branded;
}
export function createSameOriginTrustAssertion(details) {
  return brandTrustAssertion(details, ['mode', 'configured_url', 'response_url', 'redirected', 'tls_verified',
    'authenticated_request', 'document_sha256', 'expected_provider', 'expected_model', 'now_ms']);
}
export function createPinnedSignatureTrustAssertion(details) {
  return brandTrustAssertion(details, ['mode', 'signature_verified', 'key_source', 'key_id', 'expected_key_id',
    'audience', 'expected_audience', 'issuer', 'expected_issuer', 'document_sha256',
    'expected_provider', 'expected_model', 'now_ms']);
}
function assertTrust(trust, cap, documentSha) {
  if (!isObject(trust) || !TRUST_ASSERTIONS.has(trust)) fail('TRUST', 'trusted verifier assertion is required');
  if (trust.expected_provider !== cap.provider || trust.expected_model !== cap.model) fail('ROUTE_MISMATCH', 'provider/model trust mismatch');
  if (trust.document_sha256 !== documentSha) fail('TRUST', 'trust assertion does not bind this document');
  const now = safeInteger(trust.now_ms, 'trust.now_ms', { min:1, max:10 ** 15 });
  if (trust.mode === 'same-origin-tls') {
    exactKeys(trust, ['mode', 'configured_url', 'response_url', 'redirected', 'tls_verified',
      'authenticated_request', 'document_sha256', 'expected_provider', 'expected_model', 'now_ms'], 'trust');
    let configured; let response;
    try { configured = new URL(trust.configured_url); response = new URL(trust.response_url); }
    catch { fail('TRUST', 'trust URL is invalid'); }
    if (configured.protocol !== 'https:' || configured.origin !== response.origin || trust.redirected !== false
        || trust.tls_verified !== true || trust.authenticated_request !== true) fail('TRUST', 'same-origin TLS trust failed');
  } else if (trust.mode === 'pinned-signature') {
    exactKeys(trust, ['mode', 'signature_verified', 'key_source', 'key_id', 'expected_key_id',
      'audience', 'expected_audience', 'issuer', 'expected_issuer', 'document_sha256',
      'expected_provider', 'expected_model', 'now_ms'], 'trust');
    if (trust.signature_verified !== true || trust.key_source !== 'release' || trust.key_id !== trust.expected_key_id
        || trust.audience !== trust.expected_audience || trust.issuer !== trust.expected_issuer) fail('TRUST', 'pinned signature trust failed');
  } else fail('TRUST', 'unsupported trust mode');
  return now;
}

export function capabilitiesDocumentSha256(raw) { return normalizeCapabilities(raw).document_sha256; }
export function acceptCapabilities(raw, trust) {
  const normalized = normalizeCapabilities(raw);
  const now = assertTrust(trust, normalized, normalized.document_sha256); checkFresh(normalized, now);
  const accepted = deepFreeze(normalized); TRUSTED.add(accepted); return accepted;
}
function trustedCapabilities(value, now, { fresh = true } = {}) {
  if (!isObject(value) || !TRUSTED.has(value)) fail('UNTRUSTED', 'capabilities were not accepted by this runtime');
  if (fresh) checkFresh(value, now);
  return value;
}

class StrictJsonParser {
  constructor(text) { this.text=text; this.at=0; this.nodes=0; }
  white() { while (/\s/u.test(this.text[this.at] ?? '')) this.at++; }
  value(depth=0) {
    if (depth > MAX_JSON_DEPTH || ++this.nodes > MAX_JSON_NODES) fail('SCHEMA', 'JSON nesting or node limit exceeded');
    this.white(); const ch=this.text[this.at];
    if (ch === '{') return this.object(depth+1); if (ch === '[') return this.array(depth+1); if (ch === '"') return this.string();
    for (const [token,value] of [['true',true],['false',false],['null',null]]) if (this.text.startsWith(token,this.at)) { this.at+=token.length; return value; }
    const match=this.text.slice(this.at).match(/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/u);
    if (!match) fail('SCHEMA','invalid JSON'); this.at+=match[0].length; const value=Number(match[0]);
    if (!Number.isFinite(value)) fail('SCHEMA','non-finite JSON number'); return value;
  }
  string() { const start=this.at++; while (this.at<this.text.length) { const ch=this.text[this.at++];
    if (ch==='"') { try { return JSON.parse(this.text.slice(start,this.at)); } catch { fail('SCHEMA','invalid JSON string'); } }
    if (ch==='\\') this.at++; else if (ch<' ') fail('SCHEMA','invalid JSON string'); } fail('SCHEMA','unterminated JSON string'); }
  object(depth) { this.at++; const out=Object.create(null), keys=new Set(); this.white(); if(this.text[this.at]==='}') {this.at++;return out;}
    for(;;){this.white();if(this.text[this.at]!== '"')fail('SCHEMA','invalid JSON object key');const key=this.string();if(keys.has(key))fail('SCHEMA',`duplicate JSON key ${key}`);keys.add(key);
      this.white();if(this.text[this.at++]!==':')fail('SCHEMA','invalid JSON object');out[key]=this.value(depth);this.white();const next=this.text[this.at++];if(next==='}')return out;if(next!==',')fail('SCHEMA','invalid JSON object');}}
  array(depth){this.at++;const out=[];this.white();if(this.text[this.at]===']'){this.at++;return out;}for(;;){out.push(this.value(depth));this.white();const next=this.text[this.at++];if(next===']')return out;if(next!==',')fail('SCHEMA','invalid JSON array');}}
  parse(){const value=this.value();this.white();if(this.at!==this.text.length)fail('SCHEMA','trailing JSON data');return value;}
}
export function parseCapabilitiesJson(text, trust) {
  if(typeof text!=='string'||Buffer.byteLength(text)>MAX_DOCUMENT_BYTES)fail('SCHEMA','capabilities JSON is oversized');
  return acceptCapabilities(new StrictJsonParser(text).parse(),trust);
}

function parseCeiling(raw,label='local_ceiling'){const row=exactKeys(raw,['limits','features','timeout_hints','retry_hints'],label);return{
  limits:parseLimits(row.limits,`${label}.limits`),features:parseFeatures(row.features,`${label}.features`),
  timeout_hints:parseTimeouts(row.timeout_hints,`${label}.timeout_hints`),retry_hints:parseRetry(row.retry_hints,`${label}.retry_hints`) };}
const minFields=(...args)=>{const keys=args.pop();return Object.fromEntries(keys.map(key=>[key,Math.min(...args.map(value=>value[key]))]));};
const andFeatures=(...values)=>Object.fromEntries(FEATURE_KEYS.map(key=>[key,values.every(value=>value[key])]));
function integrityKey(value){const key=Buffer.from(value??[]);if(key.length!==32)fail('INTEGRITY','snapshot integrity key must be 32 bytes');return key;}
function affinityId(random){const bytes=random(16);if(!Buffer.isBuffer(bytes)||bytes.length!==16)fail('RNG','affinity RNG must return 16 bytes');return`aff_v1_${bytes.toString('base64url')}`;}
function snapshotSeal(body,key){return createHmac('sha256',integrityKey(key)).update(canonical(body)).digest('hex');}

function snapshotBody(cap,selection,ceiling,options,random,generation=1,reason='new_session'){
  const captured=safeInteger(options.captured_at_ms??Date.now(),'captured_at_ms',{min:1,max:10**15});checkFresh(cap,captured);
  if(cap.lifecycle!=='active')fail('RETIRED','cannot snapshot retired capabilities');
  const selected=exactKeys(selection,['thinking'],'selection');if(!LEVELS.includes(selected.thinking))fail('SCHEMA','selection.thinking is invalid');
  const thinking=cap.thinking[selected.thinking];const ttl=cap.affinity.ttl_ms;
  return{schema_version:SCHEMA_VERSION,provider:cap.provider,model:cap.model,config_revision:cap.config_revision,config_version:cap.config_version,
    semantic_sha256:cap.semantic_sha256,document_sha256:cap.document_sha256,capabilities_generated_at_ms:cap.generated_at_ms,
    capabilities_expires_at_ms:cap.expires_at_ms,captured_at_ms:captured,
    declared_lifecycle:cap.lifecycle,declared_limits:cap.limits,limits:minFields(cap.limits,ceiling.limits,LIMIT_KEYS),declared_thinking:cap.thinking,
    thinking:{level:selected.thinking,enabled:thinking.enabled,budget_tokens:thinking.budget_tokens},declared_features:cap.features,
    features:andFeatures(cap.features,ceiling.features),declared_timeout_hints:cap.timeout_hints,timeout_hints:minFields(cap.timeout_hints,ceiling.timeout_hints,TIMEOUT_KEYS),
    declared_retry_hints:cap.retry_hints,retry_hints:minFields(cap.retry_hints,ceiling.retry_hints,RETRY_KEYS),declared_affinity:cap.affinity,
    affinity:{id:affinityId(random),generation,scope:cap.affinity.scope,route_group:cap.affinity.route_group,ttl_ms:ttl,
      issued_at_ms:captured,expires_at_ms:Math.min(captured+ttl,cap.expires_at_ms),last_rotation_reason:reason}};
}

export function createProviderRuntimeContract({ randomBytes = nodeRandomBytes } = {}) {
  const createSnapshot=(cap,selection,localCeiling,options={})=>{const now=options.captured_at_ms??Date.now();trustedCapabilities(cap,now);const ceiling=parseCeiling(localCeiling);
    const body=snapshotBody(cap,selection,ceiling,options,randomBytes);return validateSessionSnapshot({...body,snapshot_hmac_sha256:snapshotSeal(body,options.integrity_key)},options.integrity_key);};
  const forkSnapshot=(snapshot,key,options={})=>{const fixed=validateSessionSnapshot(snapshot,key);const at=safeInteger(options.rotated_at_ms??Date.now(),'rotated_at_ms',{min:1,max:10**15});
    ensureSnapshotUsable(fixed,at);const body={...structuredClone(fixed)};delete body.snapshot_hmac_sha256;body.affinity={...body.affinity,id:affinityId(randomBytes),generation:body.affinity.generation+1,
      issued_at_ms:at,expires_at_ms:Math.min(at+body.affinity.ttl_ms,body.capabilities_expires_at_ms),last_rotation_reason:'fork'};return validateSessionSnapshot({...body,snapshot_hmac_sha256:snapshotSeal(body,key)},key);};
  const rebindSnapshot=(snapshot,key,cap,selection,ceiling,options={})=>{const old=validateSessionSnapshot(snapshot,key);const now=options.captured_at_ms??Date.now();ensureSnapshotUsable(old,now);trustedCapabilities(cap,now);
    if(cap.config_revision<old.config_revision) fail('REVISION_ROLLBACK','config_revision must be monotonic');
    if(cap.provider!==old.provider||cap.model!==old.model){
      const allow=options.allow_route_change===true;if(!allow)fail('ROUTE_MISMATCH','rebind route change requires explicit allowance');
    }
    if(cap.provider===old.provider&&cap.model===old.model&&cap.config_version===old.config_version&&cap.semantic_sha256!==old.semantic_sha256)fail('VERSION_REUSE','config_version changed semantics');
    const body=snapshotBody(cap,selection,parseCeiling(ceiling),options,randomBytes,old.affinity.generation+1,'rebind');return validateSessionSnapshot({...body,snapshot_hmac_sha256:snapshotSeal(body,key)},key);};
  return deepFreeze({createSessionSnapshot:createSnapshot,forkSessionSnapshot:forkSnapshot,rebindSessionSnapshot:rebindSnapshot});
}
const DEFAULT_RUNTIME=createProviderRuntimeContract();
export const createSessionSnapshot=(...args)=>DEFAULT_RUNTIME.createSessionSnapshot(...args);
export const forkSessionSnapshot=(...args)=>DEFAULT_RUNTIME.forkSessionSnapshot(...args);
export const rebindSessionSnapshot=(...args)=>DEFAULT_RUNTIME.rebindSessionSnapshot(...args);

export function validateSessionSnapshot(raw,key){const row=exactKeys(raw,['schema_version','provider','model','config_revision','config_version','semantic_sha256','document_sha256',
  'capabilities_generated_at_ms','capabilities_expires_at_ms','captured_at_ms','declared_lifecycle','declared_limits','limits','declared_thinking','thinking','declared_features','features',
  'declared_timeout_hints','timeout_hints','declared_retry_hints','retry_hints','declared_affinity','affinity','snapshot_hmac_sha256'],'snapshot');const seal=row.snapshot_hmac_sha256;const body={...row};delete body.snapshot_hmac_sha256;
  if(!SHA256.test(seal)||snapshotSeal(body,key)!==seal)fail('INTEGRITY','snapshot HMAC mismatch');if(row.schema_version!==SCHEMA_VERSION)fail('SCHEMA_VERSION','snapshot schema is unsupported');
  safeId(row.provider,'snapshot.provider');safeId(row.model,'snapshot.model');safeId(row.config_version,'snapshot.config_version');safeInteger(row.config_revision,'snapshot.config_revision',{min:1});
  const generated=safeInteger(row.capabilities_generated_at_ms,'snapshot.capabilities_generated_at_ms',{min:1,max:10**15});const captured=safeInteger(row.captured_at_ms,'snapshot.captured_at_ms',{min:1,max:10**15});const capExpires=safeInteger(row.capabilities_expires_at_ms,'snapshot.capabilities_expires_at_ms',{min:1,max:10**15});
  if(generated>captured+CLOCK_SKEW_MS||capExpires<=captured||capExpires<=generated||capExpires-generated>MAX_AGE_MS)fail('INTEGRITY','snapshot capability expiry is invalid');
  if(row.declared_lifecycle!=='active')fail('RETIRED','snapshot lifecycle is not active');
  const declared=parseLimits(row.declared_limits,'snapshot.declared_limits');const limits=parseLimits(row.limits,'snapshot.limits');for(const name of LIMIT_KEYS)if(limits[name]>declared[name])fail('ESCALATION',`snapshot ${name} exceeds declared capability`);
  const declaredThinking=parseThinking(row.declared_thinking,'snapshot.declared_thinking');const thinking=exactKeys(row.thinking,['level','enabled','budget_tokens'],'snapshot.thinking');
  if(!LEVELS.includes(thinking.level)||thinking.enabled!==declaredThinking[thinking.level].enabled||thinking.budget_tokens!==declaredThinking[thinking.level].budget_tokens)fail('ESCALATION','snapshot thinking exceeds declared capability');
  const declaredFeatures=parseFeatures(row.declared_features,'snapshot.declared_features'),features=parseFeatures(row.features,'snapshot.features');for(const name of FEATURE_KEYS)if(features[name]&&!declaredFeatures[name])fail('ESCALATION',`snapshot ${name} feature exceeds declared capability`);
  const declaredTimeouts=parseTimeouts(row.declared_timeout_hints,'snapshot.declared_timeout_hints'),timeouts=parseTimeouts(row.timeout_hints,'snapshot.timeout_hints');for(const name of TIMEOUT_KEYS)if(timeouts[name]>declaredTimeouts[name])fail('ESCALATION',`snapshot ${name} timeout exceeds declared capability`);
  const declaredRetry=parseRetry(row.declared_retry_hints,'snapshot.declared_retry_hints'),retry=parseRetry(row.retry_hints,'snapshot.retry_hints');for(const name of RETRY_KEYS)if(retry[name]>declaredRetry[name])fail('ESCALATION',`snapshot ${name} retry exceeds declared capability`);
  const declaredAffinity=parseAffinity(row.declared_affinity,'snapshot.declared_affinity');const affinity=exactKeys(row.affinity,['id','generation','scope','route_group','ttl_ms','issued_at_ms','expires_at_ms','last_rotation_reason'],'snapshot.affinity');
  if(!AFFINITY.test(affinity.id)||affinity.scope!==declaredAffinity.scope||affinity.route_group!==declaredAffinity.route_group||affinity.ttl_ms!==declaredAffinity.ttl_ms||!AFFINITY_REASONS.has(affinity.last_rotation_reason))fail('ESCALATION','snapshot affinity is invalid');
  safeInteger(affinity.generation,'snapshot.affinity.generation',{min:1});const issued=safeInteger(affinity.issued_at_ms,'snapshot.affinity.issued_at_ms',{min:1,max:10**15});const affinityExpires=safeInteger(affinity.expires_at_ms,'snapshot.affinity.expires_at_ms',{min:1,max:10**15});if(affinityExpires<=issued||affinityExpires>issued+affinity.ttl_ms||affinityExpires>capExpires)fail('INTEGRITY','snapshot affinity expiry is invalid');
  const capabilityDocument={schema_version:SCHEMA_VERSION,provider:row.provider,model:row.model,config_revision:row.config_revision,config_version:row.config_version,
    generated_at_ms:generated,expires_at_ms:capExpires,lifecycle:row.declared_lifecycle,limits:declared,thinking:declaredThinking,
    features:declaredFeatures,timeout_hints:declaredTimeouts,retry_hints:declaredRetry,affinity:declaredAffinity};
  const semantic=digest(semanticBody(capabilityDocument));const document=digest(capabilityDocument);
  if(row.semantic_sha256!==semantic||row.document_sha256!==document)fail('INTEGRITY','snapshot manifest hash is invalid');
  return deepFreeze(structuredClone(row));}

function ensureSnapshotUsable(fixed,now){safeInteger(now,'now_ms',{min:1,max:10**15});if(now<fixed.affinity.issued_at_ms-CLOCK_SKEW_MS)fail('AFFINITY_EXPIRED','affinity is not yet valid');if(now>=fixed.capabilities_expires_at_ms)fail('EXPIRED','snapshot capabilities expired');if(now>=fixed.affinity.expires_at_ms)fail('AFFINITY_EXPIRED','affinity must rotate before request');}
export function affinityNeedsRotation(snapshot,key,now=Date.now()){const fixed=validateSessionSnapshot(snapshot,key);return now>=fixed.affinity.expires_at_ms||now>=fixed.capabilities_expires_at_ms;}
export function applyRuntimeSafety(snapshot,key,currentCapabilities,currentSafetyCeiling,usage={}){const fixed=validateSessionSnapshot(snapshot,key);const now=usage.now_ms??Date.now();ensureSnapshotUsable(fixed,now);const current=trustedCapabilities(currentCapabilities,now);if(fixed.provider!==current.provider||fixed.model!==current.model)fail('ROUTE_MISMATCH','runtime safety route mismatch');
  const safety=parseCeiling(currentSafetyCeiling,'current_safety_ceiling');const limits=minFields(fixed.limits,current.limits,safety.limits,LIMIT_KEYS);const features=andFeatures(fixed.features,current.features,safety.features);
  const selected=fixed.thinking.level;const thinkingBudget=Math.min(fixed.thinking.budget_tokens,current.thinking[selected].enabled?current.thinking[selected].budget_tokens:0);
  const context=safeInteger(usage.context_tokens??0,'usage.context_tokens',{min:0,max:10**9});const required=context+limits.max_output_tokens+thinkingBudget;
  const semanticBlocked=Object.entries(fixed.features).some(([name,enabled])=>enabled&&!features[name])||fixed.thinking.enabled&&thinkingBudget===0;
  return deepFreeze({limits,features,thinking_budget_tokens:thinkingBudget,timeout_hints:minFields(fixed.timeout_hints,current.timeout_hints,safety.timeout_hints,TIMEOUT_KEYS),
    retry_hints:minFields(fixed.retry_hints,current.retry_hints,safety.retry_hints,RETRY_KEYS),admission:{status:semanticBlocked?'rebind_or_block':required>limits.context_window?'compact_or_block':'ready',
      context_tokens:context,reserved_output_tokens:limits.max_output_tokens,reserved_thinking_tokens:thinkingBudget,required_tokens:required,silent_truncation_allowed:false}});}

export function classifyRuntimeUpdate(snapshot,key,currentCapabilities,now=Date.now()){const fixed=validateSessionSnapshot(snapshot,key);const current=trustedCapabilities(currentCapabilities,now);if(fixed.provider!==current.provider||fixed.model!==current.model)fail('ROUTE_MISMATCH','runtime update route mismatch');if(current.config_revision<fixed.config_revision)fail('REVISION_ROLLBACK','config_revision must be monotonic');
  if(current.lifecycle==='retired')return deepFreeze({kind:'retired',requires_new_snapshot:true,immediate_safety:true});if(current.semantic_sha256===fixed.semantic_sha256)return deepFreeze({kind:'same',requires_new_snapshot:false,immediate_safety:false});if(current.config_version===fixed.config_version)fail('VERSION_REUSE','config_version changed semantics');
  let lower=false,higher=false;for(const name of LIMIT_KEYS){lower||=current.limits[name]<fixed.declared_limits[name];higher||=current.limits[name]>fixed.declared_limits[name];}for(const name of FEATURE_KEYS){lower||=fixed.declared_features[name]&&!current.features[name];higher||=!fixed.declared_features[name]&&current.features[name];}
  const selected=fixed.thinking.level;lower||=!current.thinking[selected].enabled||current.thinking[selected].budget_tokens<fixed.thinking.budget_tokens;higher||=current.thinking[selected].budget_tokens>fixed.thinking.budget_tokens;
  return deepFreeze({kind:lower&&higher?'changed':lower?'downgrade':higher?'upgrade':'changed',requires_new_snapshot:true,immediate_safety:lower});}

export function buildRequestContext(snapshot,key,{now_ms=Date.now()}={}){const fixed=validateSessionSnapshot(snapshot,key);ensureSnapshotUsable(fixed,now_ms);return deepFreeze({schema_version:SCHEMA_VERSION,
  config_revision:fixed.config_revision,config_version:fixed.config_version,semantic_sha256:fixed.semantic_sha256,document_sha256:fixed.document_sha256,
  affinity_id:fixed.affinity.id,affinity_generation:fixed.affinity.generation,affinity_scope:fixed.affinity.scope,route_group:fixed.affinity.route_group});}
