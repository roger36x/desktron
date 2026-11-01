#!/usr/bin/env node
import {verifyFallback} from './fallback_receipt.mjs';
/** Execute one real Claude Code turn against a Shepherd SessionStore. */
import { readFile, unlink, writeFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import { createInterface } from 'node:readline';
import { randomUUID } from 'node:crypto';
import { ShepherdClaudeStore } from './claude_sqlite_store.mjs';
import {bootstrap as unifiedBootstrap} from './claude_unified_bootstrap.mjs';
import { ShepherdClaudeMemoryStore, claudeStoreTransitionRevision } from './claude_memory_store.mjs';
import {claudeExtraArgs, claudeTurnEnv, claudeBashEnvBody, claudeDisallowedTools,
  claudeFilesystemOptions, claudeProtectedToolDecision,
  isClaudeTaskNotificationResult, claudeResumeStore} from './claude_sdk_options.mjs';
import {claudeBinaryVersion,verifyClaudeProcessVersion} from './claude_binary_version.mjs';
import {ClaudeDispatch} from './claude_dispatch.mjs';

const payload = JSON.parse(await readFile(process.argv[2], 'utf8'));
const diagnostics = new ClaudeDispatch(payload.dispatchId);
const BRIDGE_PROTOCOL_VERSION = 3;
const hostProtocol = payload.bridgeProtocolVersion ?? 1;
if (![1, 2, BRIDGE_PROTOCOL_VERSION].includes(hostProtocol))
  throw new Error('unsupported Claude Shepherd bridge protocol');
const expectedBashPolicy = {codec:'claude-2.1.268-v1', mode:'reset_each_call'};
// A v1 host has no declaration, but this bridge still enforces the safe Bash
// environment below. An explicit contradictory declaration is never ignored.
if (payload.bashCwdPolicy !== undefined
    && (payload.bashCwdPolicy?.codec !== expectedBashPolicy.codec
      || payload.bashCwdPolicy?.mode !== expectedBashPolicy.mode))
  throw new Error('Claude Shepherd Bash cwd policy/version mismatch');
const sdk = await import(new URL('sdk.mjs', pathToFileURL(payload.sdkRoot + '/')).href);
const input = createInterface({input: process.stdin, crlfDelay: Infinity});
const store = payload.unified
  ? new ShepherdClaudeMemoryStore(payload.entries, payload.sessionId, input)
  : new ShepherdClaudeStore(payload.dbPath, payload.sessionId);
const key = {projectKey: 'shepherd', sessionId: payload.sessionId};
const pending = new Map();
const transientParentEnv = new Set([
  'CLAUDECODE', 'CLAUDE_AGENT_SDK', 'CLAUDE_AGENT_SDK_CLIENT_APP',
  'CLAUDE_AGENT_SDK_VERSION', 'CLAUDE_CODE_CHILD_SESSION',
  'CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING',
  'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_EXECPATH',
  'CLAUDE_CODE_MESSAGING_SOCKET', 'CLAUDE_CODE_MESSAGING_TOKEN',
  'CLAUDE_CODE_SESSION_ID', 'CLAUDE_CONFIG_DIR', 'CLAUDE_EFFORT', 'CLAUDE_PID',
]);
function transientEnvKey(key) {
  return transientParentEnv.has(key) || [
    'CLAUDE_CODE_BRIDGE_', 'CLAUDE_CODE_HOST_', 'CLAUDE_CODE_MESSAGING_',
    'CLAUDE_CODE_RELAUNCH_', 'CLAUDE_CODE_SDK_', 'CLAUDE_CODE_SESSION_',
  ].some(prefix => key.startsWith(prefix));
}
function stableParentEnv() {
  return Object.fromEntries(Object.entries(process.env).filter(([key]) => !transientEnvKey(key)));
}
let activeQuery, cancelled = false, preparedSent = false, dispatchFenceReject = null;
const retryBudgetMs = Math.min(60 * 60 * 1000,
  Math.max(60 * 1000, Number(payload.retryBudgetMs) || 60 * 60 * 1000));
let retryWaitStartedAt = 0, retryWaitAccumulatedMs = 0;
let retryBudgetTimer = null, retryBudgetExpired = false;
function pauseRetryBudget() {
  if (retryWaitStartedAt) retryWaitAccumulatedMs += Date.now() - retryWaitStartedAt;
  retryWaitStartedAt = 0;
  if (retryBudgetTimer) clearTimeout(retryBudgetTimer);
  retryBudgetTimer = null;
}
function retryBudgetRemaining() {
  const active = retryWaitStartedAt ? Date.now() - retryWaitStartedAt : 0;
  return Math.max(0, retryBudgetMs - retryWaitAccumulatedMs - active);
}
function armRetryBudget() {
  if (retryWaitStartedAt || retryBudgetExpired) return;
  const remaining = retryBudgetRemaining();
  retryWaitStartedAt = Date.now();
  retryBudgetTimer = setTimeout(() => {
    retryBudgetExpired = true;
    process.stdout.write(JSON.stringify({type:'retry_budget_expired',
      dispatch_id:diagnostics.id, retryBudgetMs}) + '\n');
    void activeQuery?.interrupt().catch(() => {});
  }, remaining);
}
function denyPending() {
  for (const resolve of pending.values()) resolve({behavior:'deny', message:'控制通道已关闭'});
  pending.clear();
}
input.on('close', () => {
  cancelled=true;denyPending();
  if(dispatchFenceReject)dispatchFenceReject(new Error('Claude Shepherd host disconnected before dispatch'));
  // Losing the host must not silently approve or leave a tool waiting forever.
  if (activeQuery) void activeQuery.interrupt().catch(() => {});
});
input.on('line', line => {
  try {
    const message = JSON.parse(line);
    if (message.type === 'start' && !preparedSent) {
      cancelled = true;
      void activeQuery?.interrupt().catch(() => {});
    }
    if (message.type === 'cancel') {
      cancelled = true;
      for (const resolve of pending.values()) resolve({behavior:'deny', message:'用户已中断'});
      pending.clear();
      void activeQuery?.interrupt().catch(() => {});
    }
    if (message.type === 'permission_result' && pending.has(message.id)) {
      pending.get(message.id)(message.allow
        ? {behavior:'allow', updatedInput:message.updatedInput || undefined}
        : {behavior:'deny', message:message.message || '用户拒绝了该操作'});
      pending.delete(message.id);
    }
  } catch {}
});

async function verifyPromptSnapshot() {
  if (!payload.promptSnapshotPolicy) return; // Explicit legacy compatibility.
  if (payload.promptSnapshotPolicy !== 'claude-static-v1')
    throw new Error('unsupported Claude prompt snapshot policy');
  const existing = await store.load(key);
  const exact = existing.some(entry => entry?.type === 'attachment'
    && entry?.attachment?.type === 'prompt_snapshot'
    && Array.isArray(entry.attachment.systemPrompt)
    && entry.attachment.systemPrompt.length === 1
    && entry.attachment.systemPrompt[0] === payload.systemPrompt);
  if (!exact) throw new Error('Claude static prompt snapshot missing or mismatched');
}

if (payload.unified) {
  await unifiedBootstrap(store,key,payload,sdk,stableParentEnv);
  store.seal();
} else await verifyPromptSnapshot();
const before = await store.load(key);
const beforeRevision=store.revision();
const claudeVersion=hostProtocol >= 3
  ? claudeBinaryVersion(payload.claudeBinary,stableParentEnv()) : '';
const parentUuid=[...before].reverse().find(entry=>typeof entry?.uuid==='string')?.uuid || '';
let terminal = '', terminalUuid = '', mirrorError = false, resultError = '';
let compactStatus;
let failedCompactCommand = false;
// Claude's ordinary policy rejects server-directed waits above 60 seconds.
// The watchdog keeps the same native request alive and reports progress every
// 30 seconds; Shepherd adds its own one-hour ceiling below instead of replaying
// the public turn from outside the SDK.
const bashEnvPath = `${payload.dbPath}.bash-env-${randomUUID()}`;
await writeFile(bashEnvPath,claudeBashEnvBody,{encoding:'utf8',mode:0o600,flag:'wx'});
const runtimeEnv = {...stableParentEnv(), ...(payload.env || {}), ...claudeTurnEnv,
  CLAUDE_CODE_RETRY_WATCHDOG:'1',
  // The pinned Claude CLI otherwise keeps Bash's `cd` across tool calls and
  // stamps later parent records with that foreign cwd.
  CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR:'1',
  BASH_ENV:bashEnvPath,CLAUDE_ENV_FILE:bashEnvPath};
const extraArgs=claudeExtraArgs(payload.thinkingDisplay,payload.autocompact);
const options = {
  cwd: payload.cwd, resume: payload.sessionId, sessionStore: claudeResumeStore(store),
  sessionStoreFlush: 'eager', settingSources:['project'], strictMcpConfig:true,
  includePartialMessages: true,
  persistSession: true, loadTimeoutMs: 5000,
  pathToClaudeCodeExecutable: payload.claudeBinary,
  permissionMode: payload.permissionMode || 'default',
  model: payload.model || undefined, effort: payload.effort || undefined,
  maxTurns: payload.maxTurns || undefined, env: runtimeEnv,
  stderr:chunk=>diagnostics.stderr(chunk),
  // Current private store deliberately does not support subagent subpaths.
  disallowedTools:claudeDisallowedTools,
  ...claudeFilesystemOptions(payload.cwd,payload.protectedPaths,payload.allowedPaths),
  // Pinned CLI 2.1.268 without an explicit display leaves API thinking blocks
  // empty (signature only) for current models, in print and interactive modes
  // alike unless showThinkingSummaries is set. Match the native Claude adapter:
  // request summaries unless the payload explicitly chooses otherwise.
  ...(extraArgs ? {extraArgs} : {}),
};
if (payload.permissionMode === 'bypassPermissions') options.allowDangerouslySkipPermissions = true;
if (payload.systemPrompt) options.systemPrompt = {
  type:'custom', prompt:payload.systemPrompt, snapshot:true};
if (payload.interactivePermissions) options.canUseTool = async (tool, toolInput, context) => {
  if (cancelled) return {behavior:'deny', message:'用户已中断'};
  const protectedDecision=await claudeProtectedToolDecision(
    toolInput,context,payload.cwd,payload.protectedPaths,payload.allowedPaths,tool);
  if (protectedDecision) return protectedDecision;
  const id = context.toolUseID || randomUUID();
  process.stdout.write(JSON.stringify({type:'permission', id, tool, input:toolInput,
    title:context.title || '', description:context.description || ''}) + '\n');
  const decision = await new Promise(resolve => pending.set(id, resolve));
  return decision.behavior === 'allow'
    ? {...decision, updatedInput:decision.updatedInput || toolInput} : decision;
};
let releaseUserInput;
const userInputFence=new Promise(resolve=>{releaseUserInput=resolve;});
async function* userInput() {
  await userInputFence;
  const frame = payload.userFrame || {type:'user', message:{role:'user', content:payload.prompt},
    parent_tool_use_id:null, session_id:payload.sessionId};
  const message = {...(frame.message || {role:'user', content:payload.prompt})};
  let content = Array.isArray(message.content)
    ? [...message.content] : [{type:'text', text:String(message.content || '')}];
  if (payload.groundingText) {
    if (!payload.groundingUuid) throw new Error('Claude Shepherd grounding UUID missing');
    content = [{type:'text', text:`<system-reminder data-desktron-grounding="${payload.groundingUuid}">\n${payload.groundingText}\n</system-reminder>`}, ...content];
  }
  diagnostics.phase('input_consumed');
  yield {...frame, ...(payload.groundingUuid ? {uuid:payload.groundingUuid} : {}),
    message:{...message, content}, origin:{kind:'human'}};
}
try {
  if (payload.unified) store.beginPredispatch();
  activeQuery = sdk.query({prompt: userInput(), options});
  if (cancelled) void activeQuery.interrupt().catch(() => {});
  if (hostProtocol >= 3) {
    await activeQuery.initializationResult();
    await verifyClaudeProcessVersion(activeQuery,claudeVersion);
  }
  if (cancelled) throw new Error('Claude Shepherd cancelled before dispatch');
  const dispatchFence=new Promise((resolve, reject) => {
    let settled=false;
    const finish=(callback,value)=>{
      if(settled)return;settled=true;clearTimeout(timer);input.off('line',ready);
      dispatchFenceReject=null;callback(value);
    };
    const timer=setTimeout(
      ()=>finish(reject,new Error('Claude Shepherd dispatch fence timeout')),30000);
    const ready=line=>{
      try {
        const message=JSON.parse(line);
        if(message.type==='start')finish(resolve);
        else if(message.type==='cancel')
          finish(reject,new Error('Claude Shepherd cancelled before dispatch'));
      } catch {}
    };
    dispatchFenceReject=error=>finish(reject,error);
    input.on('line',ready);
    if(cancelled)dispatchFenceReject(new Error('Claude Shepherd cancelled before dispatch'));
  });
  process.stdout.write(JSON.stringify({type:'prepared', bridgeProtocolVersion:BRIDGE_PROTOCOL_VERSION,
    fallbackReceipt:verifyFallback(before,payload.fallbackReceipt), parentUuid,
    startSeq:before.length,storeSize:before.length,nativeRevision:store.revision(),claudeVersion,
    groundingUuid:payload.groundingUuid || ''}) + '\n');
  preparedSent=true;
  await dispatchFence;
  diagnostics.phase('start_received');
  diagnostics.phase('store_begin');
  if (payload.unified) await store.beginTurn();
  diagnostics.phase('store_ready');
  if (cancelled) throw new Error('Claude Shepherd cancelled before dispatch');
  diagnostics.phase('input_queued');
  releaseUserInput();
  for await (const message of activeQuery) {
    if (isClaudeTaskNotificationResult(message,payload.sessionId,terminalUuid)) continue;
    diagnostics.native(message);
    if (claudeVersion && message.type === 'system' && message.subtype === 'init'
        && message.claude_code_version !== claudeVersion)
      throw new Error(`Claude executable/init version mismatch: ${claudeVersion} != ${message.claude_code_version || '<missing>'}`);
    if (payload.unified && payload.prompt === '/compact'
        && message.type === 'system' && message.subtype === 'status'
        && message.session_id === payload.sessionId && message.compact_result) {
      compactStatus = message;
    }
    if (message.type === 'system' && message.subtype === 'mirror_error') mirrorError = true;
    if (message.type === 'system' && message.subtype === 'api_retry') armRetryBudget();
    if (message.type === 'assistant') {
      if (!message.isApiErrorMessage) pauseRetryBudget();
      if (!message.parent_tool_use_id && message.session_id === payload.sessionId && message.uuid)
        terminalUuid = message.uuid;
    }
    const event = message.type === 'system' && message.subtype === 'api_retry'
      ? {...message, retry_watchdog:true, retry_budget_remaining_ms:retryBudgetRemaining()}
      : message;
    process.stdout.write(JSON.stringify({type: 'event', event}) + '\n');
    if (message.type === 'result') {
      pauseRetryBudget();
      if (message.session_id !== payload.sessionId) throw new Error('Claude parent result session mismatch');
      terminal = ['aborted_streaming', 'aborted_tools'].includes(message.terminal_reason) ? 'interrupted_confirmed'
        : message.subtype === 'success' && !message.is_error ? 'completed' : 'failed_recoverable';
      if (message.subtype !== 'success') resultError = (message.errors || []).join('; ') || message.subtype;
      // A successful local-command dispatch is not successful compaction.
      // The pinned CLI reports cancelled summaries as compact_result=failed;
      // preserve that fact rather than guessing cancellation from error text.
      if (message.local_command === 'compact' && compactStatus?.compact_result === 'failed') {
        terminal = 'failed_recoverable';
        failedCompactCommand = true;
        resultError = compactStatus.compact_error || 'Native compaction failed';
      }
    }
  }
} catch (error) {
  // The pinned CLI exits 1 after an aborted/error result; the SDK then throws
  // despite already yielding that terminal result after its eager store flush.
  // A crash without a result or any mirror/storage failure remains fatal.
  const terminalExit = error?.errorClass === 'error_result'
    || (error?.errorClass === 'process_exited_nonzero' && error?.exitCode === 1);
  if (!['failed_recoverable', 'interrupted_confirmed'].includes(terminal) || !terminalExit) throw error;
} finally {
  pauseRetryBudget(); denyPending();
  await unlink(bashEnvPath).catch(() => {});
}
if (!terminal) throw new Error('Claude turn ended without a native result');
if (retryBudgetExpired) {
  terminal = 'failed_recoverable';
  resultError = `Claude retry budget exhausted after ${Math.round(retryBudgetMs / 60000)} minutes`;
}
if (mirrorError) throw new Error('Claude SessionStore mirror failed');
const after = await store.load(key);
if (after.length <= before.length) throw new Error('Claude turn produced no durable SessionStore entries');
const suffix = after.slice(before.length);
if (payload.unified && suffix.some(e=>e.type==='system' && e.subtype==='compact_boundary')) {
  const at=after.findLastIndex(e=>e.type==='system' && e.subtype==='compact_boundary');
  const snapshots=after.filter(e=>e.type==='attachment' && e.attachment?.type==='prompt_snapshot');
  process.stdout.write(JSON.stringify({type:'checkpoint',payload:[...snapshots.slice(-1),...after.slice(at)]})+'\n');
}
if (payload.unified && payload.fallbackReceipt && terminal==='completed'
    && !suffix.some(e=>e.type==='system' && e.subtype==='compact_boundary')) {
  process.stdout.write(JSON.stringify({type:'checkpoint',payload:after})+'\n');
}
const terminalEntry = suffix.find(entry => entry.uuid === terminalUuid);
const nativeManualCompact = payload.unified && payload.prompt === '/compact'
  && terminal === 'completed'
  && suffix.some(entry => entry.type === 'system' && entry.subtype === 'compact_boundary')
  && suffix.some(entry => entry.type === 'user' && entry.isCompactSummary === true);
const nativeFailedCompact = failedCompactCommand && terminal === 'failed_recoverable'
  && suffix.some(entry => entry.type === 'system' && entry.subtype === 'local_command'
    && typeof entry.version === 'string' && entry.version.length > 0 && entry.sessionId === payload.sessionId
    && typeof entry.content === 'string');
if (nativeFailedCompact) terminalUuid = ''; // Local command output is not an assistant message.
if (!nativeManualCompact && !nativeFailedCompact && ((terminalUuid && (!terminalEntry || terminalEntry.type !== 'assistant'))
    || (terminal === 'completed' && !terminalUuid)))
  throw new Error('Claude parent assistant terminal missing from SessionStore: '+JSON.stringify({terminal,tail:suffix.slice(-12).map(e=>({type:e.type,subtype:e.subtype,summary:e.isCompactSummary}))}));
process.stdout.write(JSON.stringify({type: 'result', terminalState: terminal,
  error: resultError, retryBudgetExpired, startSeq: before.length, endSeq: after.length,
  terminalUuid, groundingUuid:payload.groundingUuid || '',
  nativeRevision:store.revision(),
  transitionRevision:claudeStoreTransitionRevision(beforeRevision,store.payloadSlice(before.length))}) + '\n');
input.close();
