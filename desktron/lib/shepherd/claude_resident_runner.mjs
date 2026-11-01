#!/usr/bin/env node
import {verifyFallback} from './fallback_receipt.mjs';
/** Keep one Claude Agent SDK streaming query alive across committed Shepherd turns. */
import { readFile, unlink, writeFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import { createInterface } from 'node:readline';
import { randomUUID } from 'node:crypto';
import {bootstrap as unifiedBootstrap} from './claude_unified_bootstrap.mjs';
import { ShepherdClaudeMemoryStore, claudeStoreTransitionRevision } from './claude_memory_store.mjs';
import {claudeExtraArgs, claudeTurnEnv, claudeBashEnvBody, claudeDisallowedTools,
  claudeFilesystemOptions, claudeProtectedToolDecision,
  isClaudeTaskNotificationResult, claudeResumeStore} from './claude_sdk_options.mjs';
import {claudeBinaryVersion,verifyClaudeProcessVersion} from './claude_binary_version.mjs';
import {ClaudeDispatch} from './claude_dispatch.mjs';

const bootstrap = JSON.parse(await readFile(process.argv[2], 'utf8'));
let initialEntries=bootstrap.entries || [];
delete bootstrap.entries; // Do not retain the parsed bootstrap tree beside the Store owner.
const BRIDGE_PROTOCOL_VERSION = 4;
if (bootstrap.bridgeProtocolVersion !== BRIDGE_PROTOCOL_VERSION)
  throw new Error('unsupported resident Claude Shepherd bridge protocol');
const expectedBashPolicy = {codec:'claude-2.1.268-v1', mode:'reset_each_call'};
if (bootstrap.bashCwdPolicy?.codec !== expectedBashPolicy.codec
    || bootstrap.bashCwdPolicy?.mode !== expectedBashPolicy.mode)
  throw new Error('Claude Shepherd Bash cwd policy/version mismatch');

const sdk = await import(new URL('sdk.mjs', pathToFileURL(bootstrap.sdkRoot + '/')).href);
const input = createInterface({input: process.stdin, crlfDelay: Infinity});
const store = new ShepherdClaudeMemoryStore(initialEntries, bootstrap.sessionId, input);
initialEntries=null;
const key = {projectKey:'shepherd', sessionId:bootstrap.sessionId};
const pending = new Map();
const transientParentEnv = new Set([
  'CLAUDECODE', 'CLAUDE_AGENT_SDK', 'CLAUDE_AGENT_SDK_CLIENT_APP',
  'CLAUDE_AGENT_SDK_VERSION', 'CLAUDE_CODE_CHILD_SESSION',
  'CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING', 'CLAUDE_CODE_ENTRYPOINT',
  'CLAUDE_CODE_EXECPATH', 'CLAUDE_CODE_MESSAGING_SOCKET',
  'CLAUDE_CODE_MESSAGING_TOKEN', 'CLAUDE_CODE_SESSION_ID',
  'CLAUDE_CONFIG_DIR', 'CLAUDE_EFFORT', 'CLAUDE_PID',
]);
function transientEnvKey(name) {
  return transientParentEnv.has(name) || [
    'CLAUDE_CODE_BRIDGE_', 'CLAUDE_CODE_HOST_', 'CLAUDE_CODE_MESSAGING_',
    'CLAUDE_CODE_RELAUNCH_', 'CLAUDE_CODE_SDK_', 'CLAUDE_CODE_SESSION_',
  ].some(prefix => name.startsWith(prefix));
}
function stableParentEnv() {
  return Object.fromEntries(Object.entries(process.env).filter(([name]) => !transientEnvKey(name)));
}
const claudeVersion=claudeBinaryVersion(bootstrap.claudeBinary,stableParentEnv());

class AsyncQueue {
  constructor() { this.items=[]; this.waiters=[]; this.closed=false; }
  push(value) {
    if (this.closed) throw new Error('Claude resident input is closed');
    const resolve=this.waiters.shift();
    if (resolve) resolve({value,done:false}); else this.items.push(value);
  }
  close() {
    this.closed=true;
    for (const resolve of this.waiters.splice(0)) resolve({value:undefined,done:true});
  }
  [Symbol.asyncIterator]() { return this; }
  next() {
    if (this.items.length) return Promise.resolve({value:this.items.shift(),done:false});
    if (this.closed) return Promise.resolve({value:undefined,done:true});
    return new Promise(resolve=>this.waiters.push(resolve));
  }
  return() { this.close(); return Promise.resolve({value:undefined,done:true}); }
}

const turns = new AsyncQueue();
async function* userInput() {
  for await (const item of turns) {
    item.diagnostics.phase('input_consumed');
    yield item.frame;  // Taken by the SDK; not proof of a CLI/Provider write.
  }
}
let activeQuery = null;
let queryPump = null;
let turn = null;
let closing = false;
let bashEnvPath = '';
let commandHandler = null;
const bufferedCommands = [];

input.on('line',line=>{
  let message; try {message=JSON.parse(line);} catch {return;}
  if (commandHandler) commandHandler(message);
  else bufferedCommands.push(message);
});

function write(value) {
  process.stdout.write(JSON.stringify(value) + '\n');
}
function denyPending(message='控制通道已关闭') {
  for (const resolve of pending.values()) resolve({behavior:'deny', message});
  pending.clear();
}
async function cleanupBashEnv() {
  if (bashEnvPath) await unlink(bashEnvPath).catch(()=>{});
}
async function boundedQueryReturn() {
  if (!activeQuery?.return) return;
  let timer;
  const limit=new Promise(resolve=>{timer=setTimeout(resolve,2000);timer.unref?.();});
  await Promise.race([Promise.resolve(activeQuery.return()).catch(()=>{}),limit]);
  clearTimeout(timer);
}
function fail(error) {
  if (closing) return;
  closing=true;
  denyPending(); turns.close();
  process.stderr.write((error?.stack || String(error)) + '\n');
  void (async()=>{await boundedQueryReturn();await cleanupBashEnv();process.exit(1);})();
}
async function shutdown() {
  if (closing) return;
  closing=true; denyPending(); turns.close();
  await boundedQueryReturn();
  await cleanupBashEnv();
  input.close();
  process.exit(0);
}

function resetRetryBudget(current) {
  current.retryBudgetMs = Math.min(60*60*1000,
    Math.max(60*1000, Number(bootstrap.retryBudgetMs) || 60*60*1000));
  current.retryWaitStartedAt=0; current.retryWaitAccumulatedMs=0;
  current.retryBudgetTimer=null; current.retryBudgetExpired=false;
}
function pauseRetryBudget(current) {
  if (current.retryWaitStartedAt)
    current.retryWaitAccumulatedMs += Date.now() - current.retryWaitStartedAt;
  current.retryWaitStartedAt=0;
  if (current.retryBudgetTimer) clearTimeout(current.retryBudgetTimer);
  current.retryBudgetTimer=null;
}
function retryBudgetRemaining(current) {
  const active=current.retryWaitStartedAt ? Date.now()-current.retryWaitStartedAt : 0;
  return Math.max(0,current.retryBudgetMs-current.retryWaitAccumulatedMs-active);
}
function armRetryBudget(current) {
  if (current.retryWaitStartedAt || current.retryBudgetExpired) return;
  const remaining=retryBudgetRemaining(current);
  current.retryWaitStartedAt=Date.now();
  current.retryBudgetTimer=setTimeout(()=>{
    current.retryBudgetExpired=true;
    write({type:'retry_budget_expired',dispatch_id:current.diagnostics.id,
      retryBudgetMs:current.retryBudgetMs});
    void activeQuery?.interrupt().catch(()=>{});
  },remaining);
}

function userFrame(payload) {
  const frame=payload.userFrame || {type:'user',message:{role:'user',content:payload.prompt},
    parent_tool_use_id:null,session_id:bootstrap.sessionId};
  const message={...(frame.message || {role:'user',content:payload.prompt})};
  let content=Array.isArray(message.content)
    ? [...message.content] : [{type:'text',text:String(message.content || '')}];
  if (payload.groundingText) {
    if (!payload.groundingUuid) throw new Error('Claude Shepherd grounding UUID missing');
    content=[{type:'text',text:`<system-reminder data-desktron-grounding="${payload.groundingUuid}">\n${payload.groundingText}\n</system-reminder>`},...content];
  }
  return {...frame,...(payload.groundingUuid ? {uuid:payload.groundingUuid} : {}),
    message:{...message,content},origin:{kind:'human'}};
}

async function settle(current) {
  pauseRetryBudget(current);
  if (!current.terminal) throw new Error('Claude turn ended without a native result');
  if (current.retryBudgetExpired) {
    current.terminal='failed_recoverable';
    current.resultError=`Claude retry budget exhausted after ${Math.round(current.retryBudgetMs/60000)} minutes`;
  }
  if (current.mirrorError) throw new Error('Claude SessionStore mirror failed: '
    +JSON.stringify(current.mirrorError));
  const afterSize=store.size();
  store.seal();
  if (afterSize <= current.beforeSize)
    throw new Error('Claude turn produced no durable SessionStore entries');
  const suffix=store.slice(current.beforeSize);
  if (suffix.some(entry=>entry.type==='system' && entry.subtype==='compact_boundary')) {
    const checkpoint=store.checkpointEntries();
    if (checkpoint) write({type:'checkpoint',payload:checkpoint});
  }
  if (bootstrap.fallbackReceipt && current.terminal==='completed'
      && !suffix.some(e=>e.type==='system' && e.subtype==='compact_boundary')) {
    write({type:'checkpoint',payload:store.slice()});
  }
  const terminalEntry=suffix.find(entry=>entry.uuid===current.terminalUuid);
  if ((current.terminalUuid && (!terminalEntry || terminalEntry.type!=='assistant'))
      || (current.terminal==='completed' && !current.terminalUuid))
    throw new Error('Claude parent assistant terminal missing from SessionStore: '
      +JSON.stringify({terminal:current.terminal,tail:suffix.slice(-12).map(entry=>({
        type:entry.type,subtype:entry.subtype,summary:entry.isCompactSummary}))}));
  write({type:'result',terminalState:current.terminal,error:current.resultError,
    retryBudgetExpired:current.retryBudgetExpired,startSeq:current.beforeSize,
    endSeq:afterSize,terminalUuid:current.terminalUuid,
    groundingUuid:current.payload.groundingUuid || '',nativeRevision:store.revision(),
    transitionRevision:claudeStoreTransitionRevision(
      current.beforeRevision,store.payloadSlice(current.beforeSize))});
  turn=null;
}

async function receiveNative(message) {
  if (message.type==='system' && message.subtype==='init'
      && message.claude_code_version!==claudeVersion)
    throw new Error(`Claude executable/init version mismatch: ${claudeVersion} != ${message.claude_code_version || '<missing>'}`);
  const current=turn;
  if (!current) {
    if (message.type==='system' && ['init','status'].includes(message.subtype)) return;
    throw new Error('Claude resident emitted output outside an active turn');
  }
  if (isClaudeTaskNotificationResult(message,bootstrap.sessionId,current.terminalUuid)) return;
  current.diagnostics.native(message);
  if (message.type==='system' && message.subtype==='mirror_error')
    current.mirrorError=message.error || {message:'unknown mirror error'};
  if (message.type==='system' && message.subtype==='api_retry') armRetryBudget(current);
  if (message.type==='assistant') {
    if (!message.isApiErrorMessage) pauseRetryBudget(current);
    if (!message.parent_tool_use_id && message.session_id===bootstrap.sessionId && message.uuid)
      current.terminalUuid=message.uuid;
  }
  const event=message.type==='system' && message.subtype==='api_retry'
    ? {...message,retry_watchdog:true,retry_budget_remaining_ms:retryBudgetRemaining(current)}
    : message;
  write({type:'event',event});
  if (message.type!=='result') return;
  if (message.session_id!==bootstrap.sessionId)
    throw new Error('Claude parent result session mismatch');
  current.terminal=['aborted_streaming','aborted_tools'].includes(message.terminal_reason)
    ? 'interrupted_confirmed'
    : message.subtype==='success' && !message.is_error ? 'completed' : 'failed_recoverable';
  if (message.subtype!=='success')
    current.resultError=(message.errors || []).join('; ') || message.subtype;
  await settle(current);
}

async function pumpQuery() {
  try {
    for await (const message of activeQuery) await receiveNative(message);
    if (!closing) throw new Error('Claude resident query ended unexpectedly');
  } catch (error) {
    const terminalExit=error?.errorClass==='error_result'
      || (error?.errorClass==='process_exited_nonzero' && error?.exitCode===1);
    if (turn && ['failed_recoverable','interrupted_confirmed'].includes(turn.terminal)
        && terminalExit) await settle(turn);
    else if (!closing) fail(error);
  }
}

async function prepareTurn(payload, options) {
  if (turn) throw new Error('Claude resident received an overlapping turn');
  store.healthy(); // Idle writes poison the cache; never admit them as a committed prefix.
  const current={payload,beforeSize:store.size(),beforeRevision:store.revision(),
    diagnostics:new ClaudeDispatch(payload.dispatchId),
    terminal:'',terminalUuid:'',mirrorError:false,resultError:''};
  resetRetryBudget(current); turn=current;
  store.beginPredispatch();
  if (!queryPump) {
    activeQuery=sdk.query({prompt:userInput(),options});
    await activeQuery.initializationResult();
    await verifyClaudeProcessVersion(activeQuery,claudeVersion);
    queryPump=pumpQuery();
  }
  const dispatchFence=new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error('Claude Shepherd dispatch fence timeout')),30000);
    current.start=()=>{clearTimeout(timer);resolve();};
    current.cancelBeforeStart=()=>{clearTimeout(timer);reject(new Error('Claude Shepherd cancelled before dispatch'));};
  });
  write({type:'prepared',bridgeProtocolVersion:BRIDGE_PROTOCOL_VERSION,
    fallbackReceipt:verifyFallback(store.slice(),bootstrap.fallbackReceipt),
    startSeq:current.beforeSize,storeSize:current.beforeSize,
    parentUuid:store.lastUuid() || '',nativeRevision:store.revision(),claudeVersion,
    groundingUuid:payload.groundingUuid || ''});
  await dispatchFence;
  current.diagnostics.phase('start_received');
  current.diagnostics.phase('store_begin');
  await store.beginTurn();
  current.diagnostics.phase('store_ready');
  if (current.cancelled) throw new Error('Claude Shepherd cancelled before dispatch');
  current.diagnostics.phase('input_queued');
  turns.push({frame:userFrame(payload),diagnostics:current.diagnostics});
}

await unifiedBootstrap(store,key,bootstrap,sdk,stableParentEnv);
store.seal();
bashEnvPath=`${bootstrap.dbPath}.bash-env-${randomUUID()}`;
await writeFile(bashEnvPath,claudeBashEnvBody,{encoding:'utf8',mode:0o600,flag:'wx'});
const runtimeEnv={...stableParentEnv(),...(bootstrap.env || {}),...claudeTurnEnv,
  CLAUDE_CODE_RETRY_WATCHDOG:'1',CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR:'1',
  BASH_ENV:bashEnvPath,CLAUDE_ENV_FILE:bashEnvPath};
const extraArgs=claudeExtraArgs(bootstrap.thinkingDisplay,bootstrap.autocompact);
const options={
  cwd:bootstrap.cwd,resume:bootstrap.sessionId,sessionStore:claudeResumeStore(store),
  sessionStoreFlush:'eager',includePartialMessages:true,persistSession:true,
  loadTimeoutMs:5000,pathToClaudeCodeExecutable:bootstrap.claudeBinary,
  permissionMode:bootstrap.permissionMode || 'default',model:bootstrap.model || undefined,
  effort:bootstrap.effort || undefined,maxTurns:bootstrap.maxTurns || undefined,
  env:runtimeEnv,settingSources:['project'],strictMcpConfig:true,
  disallowedTools:claudeDisallowedTools,
  ...claudeFilesystemOptions(bootstrap.cwd,bootstrap.protectedPaths,bootstrap.allowedPaths),
  stderr:chunk=>turn?.diagnostics.stderr(chunk),
  ...(extraArgs ? {extraArgs} : {}),
};
if (bootstrap.permissionMode==='bypassPermissions') options.allowDangerouslySkipPermissions=true;
if (bootstrap.systemPrompt) options.systemPrompt={type:'custom',prompt:bootstrap.systemPrompt,snapshot:true};
if (bootstrap.interactivePermissions) options.canUseTool=async (tool,toolInput,context)=>{
  if (!turn || turn.cancelled) return {behavior:'deny',message:'用户已中断'};
  const protectedDecision=await claudeProtectedToolDecision(
    toolInput,context,bootstrap.cwd,bootstrap.protectedPaths,bootstrap.allowedPaths,tool);
  if (protectedDecision) return protectedDecision;
  const id=context.toolUseID || randomUUID();
  write({type:'permission',id,tool,input:toolInput,title:context.title || '',
    description:context.description || ''});
  const decision=await new Promise(resolve=>pending.set(id,resolve));
  return decision.behavior==='allow'
    ? {...decision,updatedInput:decision.updatedInput || toolInput} : decision;
};

commandHandler=message=>{
  if (message.type==='turn') {
    void prepareTurn(message.payload || {},options).catch(fail);
  } else if (message.type==='start') {
    if(!turn?.start){fail(new Error('Claude Shepherd start received before prepared'));return;}
    const start=turn.start; delete turn.start; delete turn.cancelBeforeStart; start();
  } else if (message.type==='cancel') {
    if (turn) turn.cancelled=true;
    denyPending('用户已中断');
    if (turn?.cancelBeforeStart) turn.cancelBeforeStart();
    else void activeQuery?.interrupt().catch(fail);
  } else if (message.type==='shutdown') {
    void shutdown();
  } else if (message.type==='permission_result' && pending.has(message.id)) {
    pending.get(message.id)(message.allow
      ? {behavior:'allow',updatedInput:message.updatedInput || undefined}
      : {behavior:'deny',message:message.message || '用户拒绝了该操作'});
    pending.delete(message.id);
  }
};
for (const message of bufferedCommands.splice(0)) commandHandler(message);
input.on('close',()=>{
  if (!closing) void shutdown();
});
