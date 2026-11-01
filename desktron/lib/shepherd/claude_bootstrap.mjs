#!/usr/bin/env node
/** Create and verify the first-request static prompt snapshot for a new Binding. */
import http from 'node:http';
import { createHash } from 'node:crypto';
import { mkdir, readFile, rm } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import { ShepherdClaudeStore } from './claude_sqlite_store.mjs';

const payload = JSON.parse(await readFile(process.argv[2], 'utf8'));
if (payload.promptSnapshotPolicy !== 'claude-static-v1')
  throw new Error('unsupported Claude prompt snapshot policy');
if (typeof payload.systemPrompt !== 'string' || !payload.systemPrompt)
  throw new Error('Claude static system prompt missing');
if (createHash('sha256').update(payload.systemPrompt).digest('hex') !== payload.promptSnapshotSha256)
  throw new Error('Claude static system prompt digest mismatch');
const sdk = await import(new URL('sdk.mjs', pathToFileURL(payload.sdkRoot + '/')).href);
const transient = new Set(['CLAUDECODE','CLAUDE_AGENT_SDK','CLAUDE_AGENT_SDK_CLIENT_APP',
  'CLAUDE_AGENT_SDK_VERSION','CLAUDE_CODE_CHILD_SESSION',
  'CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING','CLAUDE_CODE_ENTRYPOINT',
  'CLAUDE_CODE_EXECPATH','CLAUDE_CODE_MESSAGING_SOCKET','CLAUDE_CODE_MESSAGING_TOKEN',
  'CLAUDE_CODE_SESSION_ID','CLAUDE_CONFIG_DIR','CLAUDE_EFFORT','CLAUDE_PID']);
const stableEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
  !transient.has(key) && !['CLAUDE_CODE_BRIDGE_','CLAUDE_CODE_HOST_',
    'CLAUDE_CODE_MESSAGING_','CLAUDE_CODE_RELAUNCH_','CLAUDE_CODE_SDK_',
    'CLAUDE_CODE_SESSION_'].some(prefix => key.startsWith(prefix))));
const store = new ShepherdClaudeStore(payload.dbPath, payload.sessionId);
const key = {projectKey:'shepherd', sessionId:payload.sessionId};
const exactSnapshot = entry => entry?.type === 'attachment'
  && entry?.attachment?.type === 'prompt_snapshot'
  && Array.isArray(entry.attachment.systemPrompt)
  && entry.attachment.systemPrompt.length === 1
  && entry.attachment.systemPrompt[0] === payload.systemPrompt;
const completedBootstrap = entries => entries.some(entry => entry?.type === 'assistant'
  && entry?.message?.id === 'msg_shepherd_bootstrap'
  && entry?.message?.role === 'assistant'
  && entry.message.stop_reason === 'end_turn');
let existing = await store.load(key);
const snapshots = existing.filter(entry => entry?.type === 'attachment'
  && entry?.attachment?.type === 'prompt_snapshot');
if (snapshots.length) {
  if (!snapshots.every(exactSnapshot) || !completedBootstrap(existing))
    throw new Error('Claude static prompt snapshot mismatch');
  process.stdout.write(JSON.stringify({type:'bootstrap', reused:true,
    nativeRevision:store.revision()}) + '\n');
  process.exit(0);
}
// A policy-marked Binding must snapshot before portable history is imported.
// Existing legacy Bindings have no policy marker and never enter this program.
if (existing.length) throw new Error('Claude prompt snapshot must precede history import');
const config = payload.dbPath + '.bootstrap-config';
await mkdir(config, {recursive:true, mode:0o700});
const server = http.createServer((request, response) => {
  let raw = ''; request.on('data', chunk => raw += chunk); request.on('end', () => {
    if (request.url.includes('count_tokens')) {
      const body = '{"input_tokens":1}';
      response.writeHead(200, {'content-type':'application/json','content-length':body.length});
      response.end(body); return;
    }
    const requestBody = JSON.parse(raw || '{}'), id = 'msg_shepherd_bootstrap';
    const events = [
      {type:'message_start',message:{id,type:'message',role:'assistant',content:[],model:requestBody.model,
       stop_reason:null,stop_sequence:null,usage:{input_tokens:1,output_tokens:0}}},
      {type:'content_block_start',index:0,content_block:{type:'text',text:''}},
      {type:'content_block_delta',index:0,delta:{type:'text_delta',text:'Shepherd session initialized.'}},
      {type:'content_block_stop',index:0},
      {type:'message_delta',delta:{stop_reason:'end_turn',stop_sequence:null},usage:{output_tokens:1}},
      {type:'message_stop'},
    ];
    const body = events.map(event => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join('');
    response.writeHead(200, {'content-type':'text/event-stream','content-length':Buffer.byteLength(body)});
    response.end(body);
  });
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
try {
  const env = {...stableEnv, CLAUDE_CONFIG_DIR:config,
    ANTHROPIC_BASE_URL:`http://127.0.0.1:${server.address().port}`,
    ANTHROPIC_API_KEY:'shepherd-local-bootstrap', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC:'1'};
  const options = {cwd:payload.cwd, env, sessionId:payload.sessionId, sessionStore:store,
    sessionStoreFlush:'eager', tools:[], permissionMode:'dontAsk', settingSources:[],
    pathToClaudeCodeExecutable:payload.claudeBinary, maxTurns:1,
    systemPrompt:{type:'custom',prompt:payload.systemPrompt,snapshot:true}};
  for await (const _message of sdk.query({
    prompt:'Shepherd native bootstrap. No user request.', options})) {}
} finally {
  await new Promise(resolve => server.close(resolve));
  await rm(config, {recursive:true, force:true});
}
existing = await store.load(key);
if (!existing.some(exactSnapshot) || !completedBootstrap(existing))
  throw new Error('Claude static prompt snapshot was not persisted');
process.stdout.write(JSON.stringify({type:'bootstrap', reused:false,
  nativeRevision:store.revision()}) + '\n');
