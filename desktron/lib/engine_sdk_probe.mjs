/** Disposable, credential-free SDK checks. Never submits a user/model prompt. */
import {readFile} from 'node:fs/promises';
import {writeFileSync, renameSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
import {join} from 'node:path';
import {CapabilityMismatch, requireMethods, verifyPiSession, verifyPiStore} from './shepherd/pi_runtime_contract.mjs';
import {configurePiHttp, loadPiFactory} from './shepherd/pi_sdk.mjs';

const started = performance.now();
function phase(name) {
  // Disposable side channel: fixed phase IDs + monotonic time only. Never
  // serialize paths, native errors, credentials, prompts or SDK settings.
  const target = process.env.DESKTRON_PROTOCOL_PROGRESS;
  if (!target) return;
  try {
    writeFileSync(target + '.tmp', JSON.stringify({phase:name,
      elapsed_ms:Math.round(performance.now() - started)}), {encoding:'utf8', mode:0o600});
    renameSync(target + '.tmp', target);
  } catch { /* Diagnostics must not change capability admission. */ }
}
const [engine, root, binary, mode] = process.argv.slice(2);
phase('package');
const contract = `desk-${engine}-sdk-v1`;
const pkg = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'));
const load = name => import(pathToFileURL(join(root, name)).href);
let missing = [];
try {
if (engine === 'pi') {
  phase('store_import');
  const sdk = await load('dist/core/session-manager.js');
  try {
    phase('store');
    verifyPiStore(sdk.SessionManager, process.cwd());
    if (mode !== 'storage') {
      phase('imports');
      const [factory, cli, rpc, config, output] = await Promise.all([
        loadPiFactory(root), load('dist/cli/args.js'), load('dist/modes/rpc/rpc-mode.js'),
        load('dist/config.js'), load('dist/core/output-guard.js'),
      ]);
      phase('exports');
      requireMethods(factory, ['createAgentSessionServices', 'createAgentSessionFromServices'], 'SDK');
      requireMethods(rpc, ['runRpcMode'], 'RPC');
      requireMethods(config, ['getAgentDir'], 'Config');
      requireMethods(output, ['writeRawStdout'], 'Output');
      requireMethods(cli, ['parseArgs'], 'CLI');
      if (!Array.isArray(cli.parseArgs(['--no-tools']).diagnostics)) throw new CapabilityMismatch('CLI.parseArgs');
      phase('settings');
      const {SettingsManager} = await load('dist/core/settings-manager.js');
      requireMethods(SettingsManager, ['create', 'inMemory'], 'SettingsManager');
      const settings = SettingsManager.inMemory({});
      requireMethods(settings, ['getGlobalSettings', 'getCompactionReserveTokens', 'applyOverrides'], 'SettingsManager');
      settings.applyOverrides({compaction:{reserveTokens:12345}});
      if (settings.getCompactionReserveTokens() !== 12345) throw new CapabilityMismatch('SettingsManager.compaction');
      phase('http');
      await configurePiHttp(root, settings);
      phase('services');
      const services = await factory.createAgentSessionServices({cwd:process.cwd(),
        agentDir:process.cwd(), settingsManager:settings,
        resourceLoaderOptions:{noExtensions:true,noSkills:true,noPromptTemplates:true,
          noThemes:true,noContextFiles:true}});
      phase('session');
      const {session} = await factory.createAgentSessionFromServices({services,
        sessionManager:sdk.SessionManager.inMemory(process.cwd()), noTools:'all'});
      try {verifyPiSession(session);}
      finally {phase('dispose'); await session?.dispose?.();}
    }
  } catch (error) {
    if (!(error instanceof CapabilityMismatch)) throw error;
    missing = [error.message];
  }
} else if (engine === 'claude') {
  phase('imports');
  const sdk = await load('sdk.mjs');
  // SDK readback must actually consume the supplied store, not a user transcript.
  let loaded = false;
  const id = 'b3ff4ce0-9295-47b4-836d-6b98f8a8a01f';
  const entries = [{type:'user', uuid:'80d2cbf8-2e2e-438f-bc8f-34e695e6a0e2', parentUuid:null,
    sessionId:id, cwd:process.cwd(), timestamp:'2026-01-01T00:00:00Z',
    message:{role:'user', content:'desk protocol probe'}}];
  const store = {async load(key) {if (key.sessionId !== id) throw new Error('store identity');
      loaded = true; return entries;}, async append() {}, async listSubkeys() {return [];}};
  phase('store');
  requireMethods(sdk, ['query', 'getSessionMessages'], 'SDK');
  const messages = await sdk.getSessionMessages(id, {dir:process.cwd(), sessionStore:store});
  if (!loaded || messages.length !== 1) missing.push('SDK.SessionStore.readback');
  if (mode !== 'storage') {
    let endInput;
    async function* emptyInput() {await new Promise(resolve => {endInput = resolve;});}
    const query = sdk.query({prompt:emptyInput(), options:{cwd:process.cwd(),
      pathToClaudeCodeExecutable:binary, settingSources:[], tools:[],
      permissionMode:'dontAsk', persistSession:true,
      // An explicit synthetic credential prevents native keychain fallback;
      // any accidental API request can only fail against this loopback port.
      env:{...process.env,ANTHROPIC_API_KEY:'desk-protocol-probe',
        ANTHROPIC_BASE_URL:'http://127.0.0.1:1'},
      sessionStore:store, sessionStoreFlush:'eager', includePartialMessages:true,
      canUseTool:async () => ({behavior:'deny', message:'protocol probe'})}});
    try {
      requireMethods(query, ['initializationResult', 'interrupt', 'close'], 'Query');
      phase('claude_init');
      const init = await query.initializationResult();
      if (!Array.isArray(init.commands) || !Array.isArray(init.models))
        missing.push('CLI.initialize');
      await query.interrupt();
    } finally {phase('dispose'); endInput?.(); query.close();}
  }
} else throw new Error('unknown SDK engine');
} catch (error) {
  if (error instanceof CapabilityMismatch) missing = [error.message];
  else { console.error('SDK protocol probe failed'); process.exitCode = 1; }
}
if (!process.exitCode && missing.length === 0) phase('done');
console.log(JSON.stringify({available:!process.exitCode && missing.length === 0, contract,
  sdk_version:pkg.version, missing_capabilities:missing}));
