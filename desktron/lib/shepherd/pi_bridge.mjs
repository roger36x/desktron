#!/usr/bin/env node
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { once } from 'node:events';
import {PI_STORE_CONTRACT, verifyPiStore} from './pi_runtime_contract.mjs';

const [sdkRoot, command, sessionFile, sessionDir, cwd, sessionId, payloadFile] = process.argv.slice(2);
if (!sdkRoot || !command || !sessionDir || !cwd || !sessionId)
  throw new Error('invalid Pi bridge arguments');
const pkg = JSON.parse(await readFile(resolve(sdkRoot, 'package.json'), 'utf8'));
const { SessionManager } = await import(pathToFileURL(resolve(sdkRoot, 'dist/core/session-manager.js')).href);
verifyPiStore(SessionManager, cwd);

async function open() {
  const sm = sessionFile === '-'
    ? SessionManager.create(cwd, sessionDir, { id: sessionId })
    : SessionManager.open(sessionFile, sessionDir, cwd);
  if (sm.getSessionId() !== sessionId || resolve(sm.getCwd()) !== resolve(cwd))
    throw new Error('Pi session identity mismatch');
  return sm;
}
function markers(sm) {
  return sm.getEntries().filter(entry => entry.type === 'custom'
    && ['shepherd-round-start', 'shepherd-round-commit', 'shepherd-checkpoint'].includes(entry.customType));
}
function report(sm) {
  const entries = sm.getEntries(), context = sm.buildSessionContext();
  return { storeContract: PI_STORE_CONTRACT, sdkVersion: pkg.version, sessionId: sm.getSessionId(), cwd: sm.getCwd(),
    sessionFile: sm.getSessionFile() || '', entries: entries.length,
    leafId: sm.getLeafId() || '', markers: markers(sm),
    messages: context.messages.length, roles: context.messages.map(message => message.role) };
}
const payload = payloadFile ? JSON.parse(await readFile(payloadFile, 'utf8')) : {};
const sm = await open();
if (command === 'bootstrap') {
  if (sm.getEntries().length === 0) {
    sm.appendMessage({role:'assistant',content:[],api:'shepherd-bootstrap',provider:'shepherd',
      model:'bootstrap',stopReason:'aborted',timestamp:0,usage:{input:0,output:0,cacheRead:0,
      cacheWrite:0,totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}});
    sm.appendCustomEntry('shepherd-bootstrap', {codec: payload.codec});
  }
  console.log(JSON.stringify(report(sm)));
} else if (command === 'append') {
  for (const round of payload.rounds || []) {
    const done = markers(sm).find(entry => entry.customType === 'shepherd-round-commit'
      && entry.data?.roundId === round.roundId);
    if (done) {
      if (done.data?.entryHash !== round.entryHash || done.data?.codec !== payload.codec)
        throw new Error('Pi round marker conflict');
      continue;
    }
    sm.appendCustomEntry('shepherd-round-start', { roundId: round.roundId,
      entryHash: round.entryHash, codec: payload.codec });
    for (const message of round.messages || []) {
      if (message.type === 'custom' && ['shepherd-custom-tool-v1', 'shepherd-reasoning-v2',
          'shepherd-tool-call-ids-v1'].includes(message.customType)) {
        sm.appendCustomEntry(message.customType, message.data);
      } else sm.appendMessage(message);
    }
    sm.appendCustomEntry('shepherd-round-commit', { roundId: round.roundId,
      entryHash: round.entryHash, codec: payload.codec });
  }
  console.log(JSON.stringify(report(sm)));
} else if (command === 'checkpoint') {
  if (sm.getEntries().length) throw new Error('Pi checkpoint requires empty session');
  sm.appendMessage({ role:'assistant', content:[{type:'text',text:''}], api:'shepherd-import',
    provider:'shepherd', model:'history', stopReason:'stop', timestamp:0,
    usage:{input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,
      cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}} });
  const anchor = sm.appendCustomEntry('shepherd-checkpoint', { checkpointId: payload.checkpointId,
    coversThroughOffset: payload.coversThroughOffset, coversHash: payload.coversHash, codec: payload.codec });
  sm.appendCompaction(payload.summary, anchor, payload.estimatedTokens,
    { shepherd: { checkpointId: payload.checkpointId, coversThroughOffset: payload.coversThroughOffset,
      coversHash: payload.coversHash, codec: payload.codec } }, true);
  console.log(JSON.stringify(report(sm)));
} else if (command === 'resolve-batch') {
  // Read once; only requested source entries cross the process boundary. Status
  // reports build model context and copy all markers, neither is needed here.
  const entries = sm.getEntries();
  if (!Array.isArray(payload.queries)) throw new Error('invalid source batch');
  const boundaries = new Map();
  for (let index = 0; index < entries.length; index++) {
    const entry = entries[index];
    if (entry.type !== 'custom' || !['shepherd-round-start', 'shepherd-round-commit'].includes(entry.customType)) continue;
    const id = entry.data?.roundId;
    if (!boundaries.has(id)) boundaries.set(id, {starts: [], commits: []});
    boundaries.get(id)[entry.customType === 'shepherd-round-start' ? 'starts' : 'commits'].push(index);
  }
  function sourceEntries(query) {
    if (!Object.hasOwn(query, 'roundId')) {
      const {start, end} = query;
      if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end <= start || end > entries.length)
        throw new Error('Pi source range invalid');
      return entries.slice(start, end);
    }
    const {starts = [], commits = []} = boundaries.get(query.roundId) || {};
    if (starts.length !== 1 || commits.length !== 1 || starts[0] >= commits[0])
      throw new Error('Pi round boundary missing or ambiguous');
    const start = entries[starts[0]], commit = entries[commits[0]];
    if (start.data?.entryHash !== commit.data?.entryHash
        || start.data?.codec !== payload.codec || commit.data?.codec !== payload.codec)
      throw new Error('Pi round marker mismatch');
    return entries.slice(starts[0], commits[0] + 1);
  }
  // Frame one round at a time and respect pipe backpressure. Do not construct
  // one batch-sized JSON string on either side of the bridge.
  for (const query of payload.queries) {
    const frame = {storeContract: PI_STORE_CONTRACT, sdkVersion: pkg.version, sessionId: sm.getSessionId(), sourceEntries: sourceEntries(query)};
    if (!process.stdout.write(JSON.stringify(frame) + '\n')) await once(process.stdout, 'drain');
  }
} else if (command === 'resolve-range') {
  const entries = sm.getEntries(), start = Number(payload.start), end = Number(payload.end);
  if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end <= start || end > entries.length)
    throw new Error('Pi source range invalid');
  console.log(JSON.stringify({...report(sm), sourceEntries: entries.slice(start, end)}));
} else if (command === 'resolve') {
  const entries = sm.getEntries(), roundId = String(payload.roundId || '');
  const starts = entries.map((entry, index) => [entry, index]).filter(([entry]) =>
    entry.type === 'custom' && entry.customType === 'shepherd-round-start'
    && entry.data?.roundId === roundId);
  const commits = entries.map((entry, index) => [entry, index]).filter(([entry]) =>
    entry.type === 'custom' && entry.customType === 'shepherd-round-commit'
    && entry.data?.roundId === roundId);
  if (starts.length !== 1 || commits.length !== 1 || starts[0][1] >= commits[0][1])
    throw new Error('Pi round boundary missing or ambiguous');
  const start = starts[0], commit = commits[0];
  if (start[0].data?.entryHash !== commit[0].data?.entryHash
      || start[0].data?.codec !== payload.codec || commit[0].data?.codec !== payload.codec)
    throw new Error('Pi round marker mismatch');
  console.log(JSON.stringify({...report(sm), roundId, entryHash: commit[0].data?.entryHash,
    sourceEntries: entries.slice(start[1], commit[1] + 1)}));
} else if (command === 'verify') {
  console.log(JSON.stringify(report(sm)));
} else throw new Error('unsupported Pi bridge command');
