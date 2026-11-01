/** Read a copied SessionStore; all recap writes die with the temporary directory. */
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import {ShepherdClaudeStore} from './claude_sqlite_store.mjs';
import {claudeResumeStore} from './claude_sdk_options.mjs';

const p = JSON.parse(await readFile(process.argv[2], 'utf8'));
const sdk = await import(new URL('sdk.mjs', pathToFileURL(p.sdkRoot + '/')).href);
const store = new ShepherdClaudeStore(p.dbPath, p.sessionId);
const key = {projectKey:'shepherd', sessionId:p.sessionId};
if (!(await store.load(key)).length) throw new Error('Recap source is empty');
let ok = false, attempted = false, text = '';
for await (const message of sdk.query({prompt:p.prompt, options:{
  cwd:p.cwd, resume:p.sessionId, sessionStore:claudeResumeStore(store), sessionStoreFlush:'eager',
  pathToClaudeCodeExecutable:p.claudeBinary, env:p.env,
  model:p.model || undefined, effort:p.effort || undefined,
  systemPrompt:p.systemPrompt || undefined, maxTurns:1,
  tools:[], mcpServers:{}, settingSources:[], permissionMode:'dontAsk',
  canUseTool:async () => { attempted = true; return {behavior:'deny', message:'Recap 禁止工具'}; },
}})) {
  if (message.type === 'assistant') {
    for (const part of message.message?.content || []) {
      if (part.type === 'tool_use') attempted = true;
      if (part.type === 'text') text += part.text;
    }
  }
  if (message.type === 'system' && message.subtype === 'mirror_error') throw new Error('Recap store failed');
  if (message.type === 'result') ok = message.subtype === 'success' && !message.is_error;
}
process.stdout.write(JSON.stringify({type:'recap', ok:ok && !attempted,
  text:ok && !attempted ? text : ''}) + '\n');
