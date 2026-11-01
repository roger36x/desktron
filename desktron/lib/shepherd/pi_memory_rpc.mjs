import {verifyFallback} from './fallback_receipt.mjs';
import {verifyPiStore, verifyPiSession} from './pi_runtime_contract.mjs';
import {configurePiHttp, loadPiFactory} from './pi_sdk.mjs';
import {fitCompactionBudget} from './pi_compaction_budget.mjs';
/** Shepherd persists native events while Pi owns the in-memory context and tool loop. */
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';
import { performance } from 'node:perf_hooks';

const startupStarted = performance.now();
let startupPrevious = startupStarted;
const startupTiming = {};
const startupMark = stage => {
  const now = performance.now();
  startupTiming[stage] = {elapsed_ms: now - startupStarted, duration_ms: now - startupPrevious};
  startupPrevious = now;
};

const [root, bootstrap, ...args] = process.argv.slice(2);
const load = p => import(pathToFileURL(join(root, 'dist', p)).href);
try {
  const [{ SessionManager }, { parseArgs }, { getAgentDir }, sdk, { runRpcMode }] = await Promise.all([
    load('core/session-manager.js'), load('cli/args.js'), load('config.js'),
    loadPiFactory(root), load('modes/rpc/rpc-mode.js'),
  ]);
  startupMark('sdk_import');
  const parsed = parseArgs(args);
  const errors = parsed.diagnostics.filter(x => x.type === 'error');
  if (errors.length) throw new Error(errors.map(x => x.message).join('; '));
  const state = JSON.parse(readFileSync(bootstrap, 'utf8'));
  verifyPiStore(SessionManager, process.cwd());
  const sessionManager = SessionManager.inMemory(process.cwd(), { id: state.id }, state.entries);
  for (const message of state.messages) {
    if (message.type==='custom') sessionManager.appendCustomEntry(message.customType,message.data);
    else sessionManager.appendMessage(message);
  }
  if (state.compact && state.fallbackReceipt) {
    // A bounded fallback is one synthetic turn. Give Pi a visible, explicitly
    // attributed retained boundary so session.compact() can summarize that turn
    // instead of rejecting it as too small. This is native custom-message state,
    // not an invented assistant response or checkpoint.
    sessionManager.appendCustomMessageEntry('shepherd-fallback-boundary',
      'Verified bounded fallback retention boundary.',false);
  }
  startupMark('history_restore');
  const { SettingsManager } = await load('core/settings-manager.js');
  const localSettings = SettingsManager.create(process.cwd(), getAgentDir());
  const settingsManager = SettingsManager.inMemory(localSettings.getGlobalSettings());
  startupMark('settings_load');
  // Match the Pi CLI startup (main.js): apply the proxy policy and install the
  // proxy-aware undici dispatcher before any provider request. Without this the
  // Shepherd host connected directly while `pi` itself used the proxy; when direct
  // egress to the provider is blocked every turn ended with "Connection error."
  // (2026-10-04, openai/gpt-6.1-sol). A required proxy that cannot be applied fails loudly.
  await configurePiHttp(root, settingsManager);
  startupMark('http_setup');
  const services = await sdk.createAgentSessionServices({
    cwd: process.cwd(), agentDir: getAgentDir(), settingsManager,
    modelRuntimeSignal: AbortSignal.timeout(15000),
    extensionFlagValues: parsed.unknownFlags,
    resourceLoaderOptions: {
      noExtensions: true, additionalExtensionPaths: parsed.extensions,
      additionalSkillPaths: parsed.skills, noSkills: parsed.noSkills,
      noPromptTemplates: true, noThemes: true, noContextFiles: parsed.noContextFiles,
      appendSystemPrompt: parsed.appendSystemPrompt,
    },
  });
  startupMark('services_create');
  const extensionErrors = services.resourceLoader.getExtensions().errors;
  if (extensionErrors.length) throw new Error('Pi extension initialization failed: ' + extensionErrors.map(x => x.error).join('; '));
  // Resolve against the user's configured providers, including Desk's explicit extensions.
  let model;
  if (parsed.model) {
    const models = services.modelRuntime.getModels();
    model = models.find(m => `${m.provider}/${m.id}` === parsed.model ||
      (m.id === parsed.model && (!parsed.provider || m.provider === parsed.provider)));
    if (!model) throw new Error('Pi model is unavailable: ' + parsed.model);
  }
  const { session } = await sdk.createAgentSessionFromServices({
    services, sessionManager, model, thinkingLevel: parsed.thinking,
    noTools: parsed.noTools ? 'all' : undefined,
  });
  startupMark('session_create');
  if (state.compact && state.fallbackReceipt) {
    // Session creation reapplies the live model's resolved settings. Override
    // afterward: the verified fallback is one bounded synthetic turn, so Pi's
    // ordinary 20k retained-tail budget would reject it as "Nothing to compact".
    // Model overrides outrank the ordinary setting, so fence the exact live key too.
    const modelKey=`${session.model.provider}/${session.model.id}`;
    settingsManager.applyOverrides({compaction:{keepRecentTokens:1,
      modelOverrides:{[modelKey]:{keepRecentTokens:1}}}});
    if (settingsManager.getCompactionKeepRecentTokens(session.model)!==1)
      throw new Error('Pi fallback compaction settings were not applied');
  }
  verifyPiSession(session);
  const { writeRawStdout } = await load('core/output-guard.js');
  const emit = value => writeRawStdout(JSON.stringify(value)+'\n');
  startupMark('ready_validate');
  emit({type:'protocol_ready',contract:'desk-pi-execute-v1',sessionId:state.id,
    startup_timing:startupTiming});
  if (state.fallbackReceipt) emit({type:'fallback_prepared',
    fallbackReceipt:verifyFallback(session.messages,state.fallbackReceipt)});
  let forceCheckpoint=!!state.fallbackReceipt;
  // Storage interception only: native settings and compaction scheduling are unchanged.
  if (typeof sessionManager._persist !== 'function') throw new Error('Pi journal hook unavailable');
  let compacted=false;
  sessionManager._persist = entry => {
    if (entry.type==='compaction') compacted=true;
    emit({type:'archive',entries:[entry]});
  };
  const checkpoint = () => {
    if (compacted) {
      const entries=sessionManager.getEntries();
      const latest=entries.findLast(e=>e.type==='compaction');
      const at=entries.findIndex(e=>e.id===latest.firstKeptEntryId);
      if (at<0) throw new Error('Pi checkpoint retained tail missing');
      emit({type:'checkpoint',payload:[sessionManager.getHeader(),...entries.slice(at)]});
      compacted=false;forceCheckpoint=false;
    } else if (forceCheckpoint) {
      emit({type:'checkpoint',payload:[sessionManager.getHeader(),...sessionManager.getEntries()]});
      forceCheckpoint=false;
    }
  };
  session.subscribe(event => { if (event.type==='agent_end') checkpoint(); });
  if (state.compact) {
    const {createInterface}=await import('node:readline');
    const input=createInterface({input:process.stdin,crlfDelay:Infinity});
    let cancelled=false;
    let compactEnd;
    const started=new Promise(resolve => {
      input.on('line', line => {
        const command=JSON.parse(line);
        if (command.type==='abort') {
          cancelled=true;
          session.abortCompaction();
        } else if (command.type!=='prompt') {
          throw new Error('Unsupported manual compaction command');
        }
        resolve();
      });
    });
    const unsubscribe=session.subscribe(event => {
      // compact() creates its abort controller after an await. Retain an early
      // cancellation until the native start event, without inventing a terminal.
      if (event.type==='compaction_start' && cancelled) session.abortCompaction();
      if (event.type==='compaction_end') compactEnd=event;
      if (event.type==='compaction_start' || event.type==='compaction_end') emit(event);
    });
    try {
      await started;
      // The native RPC loop normally supplies the UI transport. Maintenance
      // uses the same notification contract even though it has no tool loop.
      await session.bindExtensions({mode:'rpc', uiContext:{
        ...session.extensionRunner.getUIContext(),
        notify(message, notifyType) {
          emit({type:'extension_ui_request', method:'notify', message, notifyType});
        },
      }});
      if (cancelled) {
        emit({type:'memory_compact_done',status:'aborted'});
      } else {
        let failed=false;
        try {
          await session.compact();
        } catch (error) {
          // Only a native compaction_end proves failure/cancellation. Startup,
          // export and transport exceptions remain unresolved, never replayable.
          if (compacted || !compactEnd || (!compactEnd.aborted && !compactEnd.errorMessage)) throw error;
          failed=!compactEnd.aborted;
        }
        if (compactEnd?.aborted === true) {
          emit({type:'memory_compact_done',status:'aborted'});
        } else if (failed) {
          emit({type:'memory_compact_done',status:'failed'});
        } else {
          checkpoint();
          emit({type:'memory_compact_done',status:'completed'});
        }
      }
    } finally {
      unsubscribe();
      input.close();
      await session.dispose();
    }
  } else {
  const unsupported = async () => { throw new Error('Shepherd owns session history; native session replacement is unavailable'); };
  const host = {
    session, setRebindSession() {}, newSession: unsupported, fork: unsupported,
    switchSession: unsupported,
    async dispose() { await session.dispose(); },
  };
  // Session-scoped compaction threshold for a resident host. The pinned Pi runtime compacts
  // when context > contextWindow - reserveTokens and re-reads the settings object
  // at every check, so rewriting reserveTokens in this private in-memory manager
  // changes the next check without a restart. `desk_compaction` is a Desk control
  // frame consumed here, before Pi's RPC reader; every other line passes through
  // unchanged and in order. Handling is synchronous, so a threshold sent ahead of
  // a prompt always applies before that prompt is parsed.
  //
  // Without a manual tier, Pi's fixed native budget is fitted to the live window
  // (pi_compaction_budget.mjs) so a small route keeps output room after
  // compaction instead of degrading to one-token length stops.
  const modelKey = `${session.model.provider}/${session.model.id}`;
  const nativeReserve = settingsManager.getCompactionReserveTokens(session.model);
  const nativeKeep = settingsManager.getCompactionKeepRecentTokens(session.model);
  const applyCompaction = frame => {
    const threshold = Number.isSafeInteger(frame.threshold) && frame.threshold > 0 ? frame.threshold : 0;
    const minHeadroom = Number.isSafeInteger(frame.minHeadroom) && frame.minHeadroom > 0
      ? frame.minHeadroom : 0;
    const model = session.model;
    const contextWindow = model?.contextWindow > 0 ? model.contextWindow : 0;
    let reserveTokens = nativeReserve, status = 'native';
    if (threshold) {
      // reserveTokens also caps the summary request (0.8 * reserve); without a
      // model output ceiling a large reserve would become an invalid max_tokens.
      // A saved tier stays inert when the live route cannot retain enough tail
      // and summary headroom (the live model can differ from the rendered catalog).
      const headroom = contextWindow - threshold;
      if (!contextWindow) status = 'no_context_window';
      else if (!(model.maxTokens > 0)) status = 'no_output_limit';
      else if (headroom < minHeadroom || headroom <= nativeReserve) status = 'above_window';
      else { reserveTokens = headroom; status = 'applied'; }
    }
    let keepRecentTokens = nativeKeep;
    if (status !== 'applied') {
      const fitted = fitCompactionBudget(contextWindow, reserveTokens, keepRecentTokens);
      ({reserveTokens, keepRecentTokens} = fitted);
      if (fitted.scaled) status = status === 'native' ? 'fitted' : status;
    }
    const budget = {reserveTokens, keepRecentTokens};
    settingsManager.applyOverrides({compaction: {...budget, modelOverrides:{[modelKey]:budget}}});
    emit({type:'desk_compaction', status, threshold, contextWindow,
      reserveTokens: settingsManager.getCompactionReserveTokens(session.model),
      keepRecentTokens: settingsManager.getCompactionKeepRecentTokens(session.model)});
  };
  const { PassThrough } = await import('node:stream');
  const { StringDecoder } = await import('node:string_decoder');
  const rpcInput = new PassThrough();
  const realInput = process.stdin;
  Object.defineProperty(process, 'stdin', {configurable: true, enumerable: true, get: () => rpcInput});
  const decoder = new StringDecoder('utf8');
  let pendingText = '';
  const route = line => {
    // Control frames are tiny; never JSON-parse large prompt lines (images) twice.
    if (line.length < 4096 && line.includes('"desk_compaction"')) {
      let frame = null;
      try { frame = JSON.parse(line); } catch {}
      if (frame?.type === 'desk_compaction') {
        try { applyCompaction(frame); }
        catch (error) { emit({type:'desk_compaction', status:'error', error:String(error?.message || error)}); }
        return;
      }
    }
    if (!rpcInput.write(line + '\n')) {
      realInput.pause();
      rpcInput.once('drain', () => realInput.resume());
    }
  };
  realInput.on('data', chunk => {
    pendingText += decoder.write(chunk);
    let at;
    while ((at = pendingText.indexOf('\n')) >= 0) {
      const line = pendingText.slice(0, at);
      pendingText = pendingText.slice(at + 1);
      route(line.endsWith('\r') ? line.slice(0, -1) : line);
    }
  });
  realInput.on('end', () => {
    pendingText += decoder.end();
    if (pendingText) route(pendingText);
    pendingText = '';
    rpcInput.end();
  });
  await runRpcMode(host);
  }
} catch (error) {
  process.stderr.write(`Shepherd Pi startup failed: ${error.message}\n`);
  process.exitCode = 1;
}
