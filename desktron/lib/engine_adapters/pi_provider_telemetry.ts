import { createHmac, randomBytes, randomUUID } from 'node:crypto';
import type { ExtensionAPI } from '@earendil-works/pi-coding-agent';

const PREFIX = 'DESKTRON_PROVIDER_TRACE:';
const SAFE = /^[A-Za-z0-9._:/+\-]{1,160}$/;
const FP = /^[0-9a-f]{32}$/;
// A process-local key keeps request material unlinkable after a Resident restart and never
// exposes a secret through environment variables, files, logs or provider request fields.
const PROCESS_KEY = randomBytes(32);
const FINGERPRINT_EPOCH = createHmac('sha256', PROCESS_KEY).update('desk-ttft-epoch-v1').digest('hex').slice(0, 32);
const IDENTITIES = new WeakMap<object, { request_epoch: string; request_seq: number }>();

const MAX_CANONICAL_BYTES = 16 * 1024 * 1024;
const MAX_CANONICAL_NODES = 200_000;
function assertBounded(value: unknown, state = { nodes:0, bytes:0, seen:new Set<object>() }, depth = 0): void {
  if (++state.nodes > MAX_CANONICAL_NODES || depth > 64) throw new Error('telemetry payload too complex');
  if (typeof value === 'string') state.bytes += Buffer.byteLength(value) * 6 + 2;
  else if (value === null || typeof value === 'boolean' || typeof value === 'number') state.bytes += 32;
  else if (Array.isArray(value)) {
    if (state.seen.has(value)) throw new Error('cyclic telemetry payload');
    state.seen.add(value); state.bytes += value.length + 2;
    for (const item of value) assertBounded(item, state, depth + 1);
    state.seen.delete(value);
  } else if (typeof value === 'object') {
    const row = value as Record<string, unknown>;
    if (state.seen.has(row)) throw new Error('cyclic telemetry payload');
    state.seen.add(row);
    for (const name of Object.keys(row)) {
      state.bytes += Buffer.byteLength(name) * 6 + 4;
      if (row[name] !== undefined) assertBounded(row[name], state, depth + 1);
    }
    state.seen.delete(row);
  } else state.bytes += Buffer.byteLength(String(value)) * 6 + 2;
  if (state.bytes > MAX_CANONICAL_BYTES) throw new Error('telemetry payload too large');
}
function canonical(value: unknown): string {
  if (value === null || typeof value === 'boolean' || typeof value === 'string') return JSON.stringify(value);
  if (typeof value === 'number') return Number.isFinite(value) ? JSON.stringify(value) : 'null';
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (typeof value === 'object') {
    const row = value as Record<string, unknown>;
    return `{${Object.keys(row).sort().filter(k => row[k] !== undefined)
      .map(k => `${JSON.stringify(k)}:${canonical(row[k])}`).join(',')}}`;
  }
  return JSON.stringify(String(value));
}

export function fingerprint(field: string, value: unknown): string | undefined {
  if (!/^[a-z][a-z0-9_]{0,47}$/.test(field)) return undefined;
  assertBounded(value);
  return createHmac('sha256', PROCESS_KEY).update(`desk-ttft-v1\0${field}\0${canonical(value)}`)
    .digest('hex').slice(0, 32);
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}

function splitPayload(payload: unknown) {
  const row = record(payload);
  const rawItems = Array.isArray(row.input) ? row.input : Array.isArray(row.messages) ? row.messages : [];
  const systemItems: unknown[] = [];
  const reasoningItems: unknown[] = [];
  const ordinary: unknown[] = [];
  for (const item of rawItems) {
    const value = record(item);
    const role = String(value.role || '');
    const type = String(value.type || '');
    if (role === 'system' || role === 'developer') systemItems.push(item);
    else if (type.includes('reasoning')) reasoningItems.push(item);
    else ordinary.push(item);
  }
  return {
    system: { instructions: row.instructions ?? row.system ?? null, items: systemItems },
    tools: row.tools ?? [],
    history: ordinary.slice(0, -1),
    reasoning: { config: row.reasoning ?? row.reasoning_effort ?? null, items: reasoningItems },
    tail: ordinary.length ? ordinary.at(-1) : null,
  };
}

export function summarizePayload(payload: unknown, model: { provider?: string; id?: string; api?: string },
  sessionId: string, requestEpoch: string, requestSeq: number, previous?: Record<string, string>) {
  const segments = splitPayload(payload);
  const row = record(payload);
  const result: Record<string, unknown> = {
    kind: 'request', request_epoch: requestEpoch, request_seq: requestSeq,
    provider: SAFE.test(String(model.provider || '')) ? model.provider : 'unknown',
    model: SAFE.test(String(model.id || '')) ? model.id : 'unknown',
    transport: 'unknown', fingerprint_epoch_fp: FINGERPRINT_EPOCH,
    request_epoch_fp: fingerprint('request_epoch_fp', requestEpoch),
  };
  for (const [name, value] of Object.entries({
    session_fp: sessionId,
    cache_key_fp: typeof row.prompt_cache_key === 'string' ? row.prompt_cache_key : undefined,
    system_segment_fp: segments.system, tools_segment_fp: segments.tools,
    history_segment_fp: segments.history, reasoning_segment_fp: segments.reasoning,
    tail_segment_fp: segments.tail,
  })) {
    if (value === undefined) continue;
    const digest = fingerprint(name, value);
    if (digest && FP.test(digest)) result[name] = digest;
  }
  result.session_changed = !!previous?.session_fp && previous.session_fp !== result.session_fp;
  result.cache_key_changed = !!previous?.cache_key_fp && previous.cache_key_fp !== result.cache_key_fp;
  result.change_reason = requestSeq === 1 ? 'new_conversation'
    : previous?.model !== result.model || previous?.provider !== result.provider ? 'model_route_change'
    : result.session_changed || result.cache_key_changed ? 'unknown' : 'new_provider_request';
  return result;
}

export function providerTraceIdentity(payload: unknown) {
  return payload && typeof payload === 'object' ? IDENTITIES.get(payload as object) : undefined;
}

export default function registerProviderTelemetry(pi: ExtensionAPI) {
  let notify: (payload: object) => void = () => {};
  let epoch = randomUUID();
  let requestSeq = 0;
  let previous: Record<string, string> | undefined;
  let active: { request_epoch:string; request_seq:number } | undefined;
  pi.on('session_start', async (_event, ctx) => {
    epoch = randomUUID(); requestSeq = 0; previous = undefined; active = undefined;
    notify = payload => ctx.ui.notify(`${PREFIX}${JSON.stringify(payload)}`, 'info');
  });
  pi.on('session_shutdown', async () => { notify = () => {}; previous = undefined; active = undefined; });
  pi.on('before_provider_request', (event, ctx) => {
    const model = ctx.model;
    if (!model) return;
    try {
      const nextSeq = requestSeq + 1;
      const next = summarizePayload(event.payload, model, ctx.sessionManager.getSessionId(),
        epoch, nextSeq, previous);
      notify(next);
      requestSeq = nextSeq; active = { request_epoch:epoch, request_seq:requestSeq };
      if (event.payload && typeof event.payload === 'object') IDENTITIES.set(event.payload as object, active);
      previous = Object.fromEntries(Object.entries(next).filter(([, value]) => typeof value === 'string')) as Record<string, string>;
    } catch { /* observation must not affect inference or consume sequence numbers */ }
  });
  pi.on('turn_end', event => {
    if (!active || event.message.role !== 'assistant') return;
    const usage = event.message.usage;
    if (!usage) return;
    const row = {kind:'usage', ...active, input:Number(usage.input || 0),
      cache_read:Number(usage.cacheRead || 0), total:Number(usage.totalTokens || 0)};
    try { notify(row); } catch { /* observation only */ }
    active = undefined;
  });
}
