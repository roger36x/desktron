import test from 'node:test';
import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import {
  acceptCapabilities, affinityNeedsRotation, applyRuntimeSafety, buildRequestContext,
  capabilitiesDocumentSha256, classifyRuntimeUpdate, createPinnedSignatureTrustAssertion,
  createProviderRuntimeContract, createSameOriginTrustAssertion, parseCapabilitiesJson,
  validateSessionSnapshot,
} from '../lib/provider_runtime_contract.mjs';

const NOW = 1_800_000_000_000;
const KEY = Buffer.alloc(32, 9);
const base = () => ({
  schema_version:1, provider:'office-qwen', model:'qwen3.8-27b',
  config_revision:7, config_version:'qwen-prod-v7', generated_at_ms:NOW-60_000,
  expires_at_ms:NOW+3_600_000, lifecycle:'active',
  limits:{context_window:32768,max_output_tokens:4096,max_body_bytes:16*1024*1024,image_limit:0},
  thinking:{off:{enabled:false,budget_tokens:0},low:{enabled:true,budget_tokens:1024},high:{enabled:true,budget_tokens:4096}},
  features:{text:true,images:false,tools:true,streaming:true,cancel:true},
  timeout_hints:{first_byte_ms:600_000,idle_ms:120_000,total_ms:1_800_000},
  retry_hints:{max_attempts:1,max_delay_ms:60_000},
  affinity:{mode:'session',scope:'provider-model-config',route_group:'qwen-tp4',ttl_ms:1_800_000},
});
const trustFor = (raw, patch={}) => createSameOriginTrustAssertion({
  mode:'same-origin-tls', configured_url:'https://qwen.example.internal/v1',
  response_url:'https://qwen.example.internal/v1/capabilities', redirected:false,
  tls_verified:true, authenticated_request:true, document_sha256:capabilitiesDocumentSha256(raw),
  expected_provider:'office-qwen', expected_model:'qwen3.8-27b', now_ms:NOW, ...patch,
});
const ceiling = () => ({
  limits:{context_window:65536,max_output_tokens:8192,max_body_bytes:32*1024*1024,image_limit:8},
  features:{text:true,images:true,tools:true,streaming:true,cancel:true},
  timeout_hints:{first_byte_ms:900_000,idle_ms:180_000,total_ms:2_100_000},
  retry_hints:{max_attempts:2,max_delay_ms:90_000},
});
const mutate=(value,fn)=>{const copy=structuredClone(value);fn(copy);return copy;};
const rng = seed => size => Buffer.alloc(size, seed);
const runtime = seed => createProviderRuntimeContract({randomBytes:rng(seed)});
const accepted = (raw=base()) => acceptCapabilities(raw,trustFor(raw));
const snapshot = (level='low', seed=1) => runtime(seed).createSessionSnapshot(
  accepted(),{thinking:level},ceiling(),{captured_at_ms:NOW,integrity_key:KEY});

function signedTrust(raw, patch={}) { return createPinnedSignatureTrustAssertion({
  mode:'pinned-signature',signature_verified:true,key_source:'release',key_id:'qwen-v1',expected_key_id:'qwen-v1',
  audience:'desk-qwen',expected_audience:'desk-qwen',issuer:'desk-control',expected_issuer:'desk-control',
  document_sha256:capabilitiesDocumentSha256(raw),expected_provider:'office-qwen',expected_model:'qwen3.8-27b',now_ms:NOW,...patch,
});}
function canonical(value) {
  if (value === null || typeof value === 'boolean' || typeof value === 'string') return JSON.stringify(value);
  if (typeof value === 'number') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
}
function reseal(snapshotValue, key = KEY) {
  const body = structuredClone(snapshotValue); delete body.snapshot_hmac_sha256;
  body.snapshot_hmac_sha256 = createHmac('sha256', key).update(canonical(body)).digest('hex');
  return body;
}

test('same-origin trust is bound to the exact canonical document',()=>{
  const raw=base(), first=acceptCapabilities(raw,trustFor(raw));
  assert.match(first.document_sha256,/^[0-9a-f]{64}$/);assert.match(first.semantic_sha256,/^[0-9a-f]{64}$/);
  assert.equal(Object.isFrozen(first),true);
  const changed=mutate(raw,x=>{x.limits.context_window=16384;});
  assert.throws(()=>acceptCapabilities(changed,trustFor(raw)),error=>error.code==='TRUST');
});

test('same-origin trust rejects forged assertions, redirects, origin mismatch, missing auth and route mismatch',()=>{
  const raw=base();
  assert.throws(()=>acceptCapabilities(raw,{mode:'same-origin-tls',tls_verified:true,authenticated_request:true,
    document_sha256:capabilitiesDocumentSha256(raw),expected_provider:'office-qwen',expected_model:'qwen3.8-27b',now_ms:NOW}),error=>error.code==='TRUST');
  for(const patch of [{redirected:true},{response_url:'https://other.example/v1/capabilities'},
    {authenticated_request:false},{expected_model:'other'}]) {
    assert.throws(()=>acceptCapabilities(raw,trustFor(raw,patch)),/trust|mismatch/iu);
  }
});

test('pinned signature trust binds document, release key, kid, audience and issuer',()=>{
  const raw=base();assert.equal(acceptCapabilities(raw,signedTrust(raw)).provider,'office-qwen');
  for(const patch of [{key_source:'document'},{key_id:'other'},{audience:'other'},{issuer:'other'}])
    assert.throws(()=>acceptCapabilities(raw,signedTrust(raw,patch)),error=>error.code==='TRUST');
});

test('dynamic capabilities cannot expand authentication or protocol surface',()=>{
  for(const [field,value] of [['endpoint','https://attacker'],['headers',{authorization:'x'}],['auth','jwt'],
    ['request_template','{{prompt}}'],['tool_protocol','arbitrary'],['retry_policy',{forever:true}]]) {
    const raw={...base(),[field]:value};assert.throws(()=>acceptCapabilities(raw,trustFor(raw)),/unsupported field/u);
  }
});

test('strict JSON rejects duplicates, non-finite, oversized and deeply nested documents',()=>{
  const raw=base(), text=JSON.stringify(raw), trust=trustFor(raw);
  assert.throws(()=>parseCapabilitiesJson(text.replace('"provider":"office-qwen"','"provider":"office-qwen","provider":"other"'),trust),/duplicate/u);
  assert.throws(()=>parseCapabilitiesJson(text.replace('32768','1e999'),trust),/non-finite/u);
  assert.throws(()=>parseCapabilitiesJson(' '.repeat(70*1024),trust),/oversized/u);
  assert.throws(()=>parseCapabilitiesJson('['.repeat(70)+'0'+']'.repeat(70),trust),/nesting/u);
});

test('programmatic input rejects inherited required fields and non-plain objects',()=>{
  const raw=base(), inherited=Object.create(raw);inherited.schema_version=1;
  assert.throws(()=>acceptCapabilities(inherited,trustFor(raw)),/plain object/u);
});

test('freshness, numeric and thinking limits fail closed',()=>{
  for(const raw of [mutate(base(),x=>{x.expires_at_ms=NOW;}),mutate(base(),x=>{x.expires_at_ms=NOW+8*86400_000;}),
    mutate(base(),x=>{x.generated_at_ms=NOW+10*60_000;})]) assert.throws(()=>acceptCapabilities(raw,trustFor(raw)),error=>error.code==='EXPIRED');
  for(const raw of [mutate(base(),x=>{x.limits.context_window=true;}),mutate(base(),x=>{x.features.tools=1;}),
    mutate(base(),x=>{x.thinking.off.budget_tokens=1;}),mutate(base(),x=>{x.thinking.low.budget_tokens=5000;})])
    assert.throws(()=>acceptCapabilities(raw,trustFor(raw)));
});

test('untrusted or expired accepted capabilities cannot create a snapshot',()=>{
  const fake={...accepted()};assert.throws(()=>runtime(1).createSessionSnapshot(fake,{thinking:'low'},ceiling(),
    {captured_at_ms:NOW,integrity_key:KEY}),error=>error.code==='UNTRUSTED');
  const cap=accepted();assert.throws(()=>runtime(1).createSessionSnapshot(cap,{thinking:'low'},ceiling(),
    {captured_at_ms:NOW+4_000_000,integrity_key:KEY}),error=>error.code==='EXPIRED');
});

test('snapshot freezes selection, takes smaller limits and persists affinity TTL',()=>{
  const local=ceiling();local.limits.context_window=16384;local.limits.max_output_tokens=2048;local.features.tools=false;
  const fixed=runtime(2).createSessionSnapshot(accepted(),{thinking:'low'},local,{captured_at_ms:NOW,integrity_key:KEY});
  assert.deepEqual(fixed.limits,{context_window:16384,max_output_tokens:2048,max_body_bytes:16*1024*1024,image_limit:0});
  assert.equal(fixed.features.tools,false);assert.deepEqual(fixed.thinking,{level:'low',enabled:true,budget_tokens:1024});
  assert.equal(fixed.affinity.ttl_ms,1_800_000);assert.equal(fixed.affinity.expires_at_ms,NOW+1_800_000);
  assert.equal(Object.isFrozen(fixed),true);
});

test('snapshot HMAC and cross-field validation reject persisted privilege escalation',()=>{
  const fixed=snapshot();
  assert.throws(()=>validateSessionSnapshot(mutate(fixed,x=>{x.limits.context_window=65536;}),KEY),error=>error.code==='INTEGRITY');
  assert.throws(()=>validateSessionSnapshot(fixed,Buffer.alloc(32,8)),error=>error.code==='INTEGRITY');
  const tampered=mutate(fixed,x=>{x.features.images=true;});
  assert.throws(()=>validateSessionSnapshot(tampered,KEY),error=>error.code==='INTEGRITY');
});

test('resume restores the sealed snapshot without refreshing capabilities',()=>{
  const fixed=snapshot('high');const restored=validateSessionSnapshot(JSON.parse(JSON.stringify(fixed)),KEY);
  assert.deepEqual(restored,fixed);assert.equal(restored.config_revision,7);
});

test('semantic hash ignores envelope refresh while document hash changes',()=>{
  const one=accepted();const raw=mutate(base(),x=>{x.generated_at_ms+=1000;x.expires_at_ms+=1000;x.config_revision=8;});
  const two=accepted(raw);assert.equal(one.semantic_sha256,two.semantic_sha256);assert.notEqual(one.document_sha256,two.document_sha256);
  assert.deepEqual(classifyRuntimeUpdate(snapshot(),KEY,two,NOW),{kind:'same',requires_new_snapshot:false,immediate_safety:false});
});

test('runtime safety shrinks limits and reserves output plus thinking without mutating snapshot',()=>{
  const fixed=snapshot('low');const currentRaw=mutate(base(),x=>{x.config_revision=8;x.config_version='safe-v8';x.limits.context_window=8192;});
  const current=accepted(currentRaw);const ready=applyRuntimeSafety(fixed,KEY,current,ceiling(),{now_ms:NOW,context_tokens:3000});
  assert.equal(ready.admission.required_tokens,3000+4096+1024);assert.equal(ready.admission.status,'ready');
  assert.equal(applyRuntimeSafety(fixed,KEY,current,ceiling(),{now_ms:NOW,context_tokens:4000}).admission.status,'compact_or_block');
  const noTools=accepted(mutate(currentRaw,x=>{x.config_version='no-tools-v8';x.features.tools=false;}));
  assert.equal(applyRuntimeSafety(fixed,KEY,noTools,ceiling(),{now_ms:NOW,context_tokens:1}).admission.status,'rebind_or_block');
});

test('downgrade is immediate, upgrade waits, version reuse with changed semantics is rejected',()=>{
  const fixed=snapshot();const down=accepted(mutate(base(),x=>{x.config_revision=8;x.config_version='safe-v8';x.limits.context_window=16384;}));
  assert.deepEqual(classifyRuntimeUpdate(fixed,KEY,down,NOW),{kind:'downgrade',requires_new_snapshot:true,immediate_safety:true});
  const up=accepted(mutate(base(),x=>{x.config_revision=8;x.config_version='up-v8';x.limits.context_window=65536;}));
  assert.equal(classifyRuntimeUpdate(fixed,KEY,up,NOW).kind,'upgrade');
  const reused=accepted(mutate(base(),x=>{x.config_revision=8;x.limits.context_window=16384;}));
  assert.throws(()=>classifyRuntimeUpdate(fixed,KEY,reused,NOW),error=>error.code==='VERSION_REUSE');
  const old=accepted(mutate(base(),x=>{x.config_revision=6;x.config_version='old';}));
  assert.throws(()=>classifyRuntimeUpdate(fixed,KEY,old,NOW),error=>error.code==='REVISION_ROLLBACK');
});

test('retirement is explicit and stable',()=>{
  const retired=accepted(mutate(base(),x=>{x.config_revision=8;x.config_version='retired-v8';x.lifecycle='retired';}));
  assert.deepEqual(classifyRuntimeUpdate(snapshot(),KEY,retired,NOW),{kind:'retired',requires_new_snapshot:true,immediate_safety:true});
});

test('fork preserves semantics but rotates opaque affinity; TTL blocks stale requests',()=>{
  const fixed=snapshot('low',1), fork=runtime(3).forkSessionSnapshot(fixed,KEY,{rotated_at_ms:NOW+100});
  assert.equal(fork.semantic_sha256,fixed.semantic_sha256);assert.notEqual(fork.affinity.id,fixed.affinity.id);
  assert.equal(fork.affinity.generation,2);assert.equal(fork.affinity.last_rotation_reason,'fork');
  assert.equal(affinityNeedsRotation(fixed,KEY,NOW+1_799_999),false);assert.equal(affinityNeedsRotation(fixed,KEY,NOW+1_800_000),true);
  assert.throws(()=>buildRequestContext(fixed,KEY,{now_ms:NOW+1_800_000}),error=>error.code==='AFFINITY_EXPIRED');
});

test('capability expiry caps affinity and prevents snapshot resurrection',()=>{
  const raw=mutate(base(),x=>{x.expires_at_ms=NOW+600_000;x.affinity.ttl_ms=1_800_000;});const cap=accepted(raw);
  const fixed=runtime(5).createSessionSnapshot(cap,{thinking:'low'},ceiling(),{captured_at_ms:NOW,integrity_key:KEY});
  assert.equal(fixed.affinity.expires_at_ms,NOW+600_000);assert.equal(affinityNeedsRotation(fixed,KEY,NOW+600_000),true);
  assert.throws(()=>buildRequestContext(fixed,KEY,{now_ms:NOW+600_000}),error=>error.code==='EXPIRED');
  assert.throws(()=>applyRuntimeSafety(fixed,KEY,cap,ceiling(),{now_ms:NOW+600_000,context_tokens:1}),error=>error.code==='EXPIRED');
  assert.throws(()=>runtime(5).forkSessionSnapshot(fixed,KEY,{rotated_at_ms:NOW+600_000}),error=>error.code==='EXPIRED');
  assert.throws(()=>runtime(5).forkSessionSnapshot(fixed,KEY,{rotated_at_ms:NOW-600_000}),error=>error.code==='AFFINITY_EXPIRED');
});

test('HMAC-valid inconsistent snapshots fail closed',()=>{
  const fixed=snapshot();
  assert.throws(()=>validateSessionSnapshot(reseal(mutate(fixed,x=>{x.timeout_hints.total_ms=x.declared_timeout_hints.total_ms+1;})),KEY),error=>error.code==='SCHEMA'||error.code==='ESCALATION');
  assert.throws(()=>validateSessionSnapshot(reseal(mutate(fixed,x=>{x.capabilities_expires_at_ms=x.captured_at_ms;})),KEY),error=>error.code==='INTEGRITY');
  assert.throws(()=>validateSessionSnapshot(reseal(mutate(fixed,x=>{x.provider='other';})),KEY),error=>error.code==='INTEGRITY');
  assert.throws(()=>validateSessionSnapshot(reseal(mutate(fixed,x=>{x.document_sha256='0'.repeat(64);})),KEY),error=>error.code==='INTEGRITY');
});

test('rebind rejects rollback and route change unless explicitly allowed',()=>{
  const fixed=snapshot();
  const rollback=accepted(mutate(base(),x=>{x.config_revision=6;x.config_version='old-v6';}));
  assert.throws(()=>runtime(1).rebindSessionSnapshot(fixed,KEY,rollback,{thinking:'low'},ceiling(),{captured_at_ms:NOW,integrity_key:KEY}),error=>error.code==='REVISION_ROLLBACK');
  const routeRaw=mutate(base(),x=>{x.model='other-model';x.config_revision=8;x.config_version='other-v8';});
  const route=acceptCapabilities(routeRaw,trustFor(routeRaw,{expected_model:'other-model'}));
  assert.throws(()=>runtime(1).rebindSessionSnapshot(fixed,KEY,route,{thinking:'low'},ceiling(),{captured_at_ms:NOW,integrity_key:KEY}),error=>error.code==='ROUTE_MISMATCH');
  const routeRollbackRaw=mutate(base(),x=>{x.model='other-model';x.config_revision=6;x.config_version='other-v6';});
  const routeRollback=acceptCapabilities(routeRollbackRaw,trustFor(routeRollbackRaw,{expected_model:'other-model'}));
  assert.throws(()=>runtime(1).rebindSessionSnapshot(fixed,KEY,routeRollback,{thinking:'low'},ceiling(),{captured_at_ms:NOW,integrity_key:KEY,allow_route_change:true}),error=>error.code==='REVISION_ROLLBACK');
});

test('rebind atomically updates route/config and rotates affinity generation',()=>{
  const fixed=snapshot();const raw=mutate(base(),x=>{x.model='qwen3.8-27b-max';x.config_revision=8;x.config_version='max-v8';x.affinity.route_group='qwen-max-tp4';});
  const cap=acceptCapabilities(raw,trustFor(raw,{expected_model:'qwen3.8-27b-max'}));
  const rebound=runtime(4).rebindSessionSnapshot(fixed,KEY,cap,{thinking:'high'},ceiling(),{captured_at_ms:NOW+100,integrity_key:KEY,allow_route_change:true});
  assert.equal(rebound.model,'qwen3.8-27b-max');assert.equal(rebound.config_version,'max-v8');
  assert.equal(rebound.affinity.route_group,'qwen-max-tp4');assert.equal(rebound.affinity.generation,2);assert.notEqual(rebound.affinity.id,fixed.affinity.id);
});

test('request context is non-semantic version and affinity metadata only',()=>{
  const context=buildRequestContext(snapshot(),KEY,{now_ms:NOW});
  assert.deepEqual(Object.keys(context).sort(),['affinity_generation','affinity_id','affinity_scope','config_revision','config_version','document_sha256','route_group','schema_version','semantic_sha256']);
  assert.doesNotMatch(JSON.stringify(context),/jwt|token|authorization|employee|email|prompt|tool|header/iu);
});

test('production affinity entropy cannot be supplied by request or session data',()=>{
  const fixed=snapshot();assert.match(fixed.affinity.id,/^aff_v1_[A-Za-z0-9_-]{22}$/);assert.doesNotMatch(fixed.affinity.id,/office|qwen|session|user/iu);
  const deterministic=runtime(6).createSessionSnapshot(accepted(),{thinking:'off'},ceiling(),{captured_at_ms:NOW,integrity_key:KEY});
  assert.equal(deterministic.affinity.id,'aff_v1_BgYGBgYGBgYGBgYGBgYGBg');
});
