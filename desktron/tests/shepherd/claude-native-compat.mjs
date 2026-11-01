#!/usr/bin/env node
/** Real pinned Claude CLI + SDK compatibility probe against a local fake model. */
import http from 'node:http';
import { randomUUID } from 'node:crypto';
import { mkdir, rm, writeFile } from 'node:fs/promises';

const [sdkRoot, claudeBinary, cwd] = process.argv.slice(2);
if (!sdkRoot || !claudeBinary || !cwd) throw new Error('sdkRoot, claudeBinary and cwd required');
const sdk = await import(new URL('file://' + sdkRoot.replaceAll(' ', '%20') + '/sdk.mjs'));
await rm(cwd, {recursive:true, force:true}); await mkdir(cwd, {recursive:true});
const png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=';
await writeFile(cwd + '/probe.png', Buffer.from(png, 'base64'));
const requests = [];
const server = http.createServer((request, response) => {
  let raw = ''; request.on('data', chunk => raw += chunk); request.on('end', () => {
    if (request.url.includes('count_tokens')) {
      const body = '{"input_tokens":10}';
      response.writeHead(200, {'content-type':'application/json','content-length':body.length});
      response.end(body); return;
    }
    const payload = JSON.parse(raw || '{}');
    requests.push(structuredClone(payload));
    const serialized = JSON.stringify(payload.messages || []);
    const tool = serialized.includes('READ_IMAGE_PROBE') && !serialized.includes('tool_result');
    const id = 'msg_' + Math.random().toString(16).slice(2);
    const events = [{type:'message_start',message:{id,type:'message',role:'assistant',content:[],
      model:payload.model,stop_reason:null,stop_sequence:null,usage:{input_tokens:10,output_tokens:0}}}];
    if (tool) events.push(
      {type:'content_block_start',index:0,content_block:{type:'tool_use',id:'toolu_image',name:'Read',input:{}}},
      {type:'content_block_delta',index:0,delta:{type:'input_json_delta',partial_json:JSON.stringify({file_path:cwd+'/probe.png'})}},
      {type:'content_block_stop',index:0},
      {type:'message_delta',delta:{stop_reason:'tool_use',stop_sequence:null},usage:{output_tokens:2}});
    else events.push(
      {type:'content_block_start',index:0,content_block:{type:'text',text:''}},
      {type:'content_block_delta',index:0,delta:{type:'text_delta',text:'ok'}},
      {type:'content_block_stop',index:0},
      {type:'message_delta',delta:{stop_reason:'end_turn',stop_sequence:null},usage:{output_tokens:2}});
    events.push({type:'message_stop'});
    const body = events.map(event => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join('');
    response.writeHead(200, {'content-type':'text/event-stream','content-length':Buffer.byteLength(body)});
    response.end(body);
  });
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
try {
  const inner = new sdk.InMemorySessionStore(); const log = [];
  const store = {append:async (key, entries) => { log.push(...structuredClone(entries)); return inner.append(key, entries); },
    load:key => inner.load(key), listSessions:prefix => inner.listSessions(prefix),
    listSubkeys:key => inner.listSubkeys?.(key) ?? []};
  const sid = randomUUID();
  const env = {...process.env, ANTHROPIC_BASE_URL:`http://127.0.0.1:${server.address().port}`,
    ANTHROPIC_API_KEY:'synthetic-test-only', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC:'1'};
  const base = {cwd, sessionStore:store, sessionStoreFlush:'eager', pathToClaudeCodeExecutable:claudeBinary,
    settingSources:[], env, maxTurns:3, permissionMode:'bypassPermissions', allowDangerouslySkipPermissions:true};
  const run = async (prompt, first=false, extra={}) => {
    for await (const _event of sdk.query({prompt, options:{...base, ...extra,
      ...(first ? {sessionId:sid} : {resume:sid})}})) {}
  };
  const staticMarker = 'STATIC_STYLE_SENTINEL';
  await run('seed one', true, {systemPrompt:{type:'custom',prompt:staticMarker,snapshot:true}, tools:[]});
  const groundingUuid = randomUUID(), dynamicMarker = 'DYNAMIC_GROUNDING_SENTINEL';
  async function* groundedFrame() {
    yield {type:'user', uuid:groundingUuid, session_id:sid, parent_tool_use_id:null,
      origin:{kind:'human'}, message:{role:'user',content:[
        {type:'text',text:`<system-reminder data-desktron-grounding="${groundingUuid}">\n${dynamicMarker}\n</system-reminder>`},
        {type:'text',text:'REAL_USER_SENTINEL'}]}};
  }
  await run(groundedFrame(), false, {systemPrompt:{type:'custom',prompt:staticMarker,snapshot:true}});
  await run('seed two');
  const compactAt = log.length; await run('/compact'); const compact = log.slice(compactAt);
  const imageAt = log.length; await run('READ_IMAGE_PROBE'); const image = log.slice(imageAt);
  const boundary = compact.find(row => row.type === 'system' && row.subtype === 'compact_boundary');
  const summary = compact.find(row => row.type === 'user' && row.isCompactSummary === true);
  const imageCallRow = image.find(row => (row.message?.content || []).some(block =>
    block.type === 'tool_use' && block.id === 'toolu_image'));
  const imageResultRow = image.find(row => (row.message?.content || []).some(block =>
    block.type === 'tool_result' && Array.isArray(block.content)
    && block.content.some(item => item.type === 'image')));
  const snapshot = log.find(row => row.type === 'attachment'
    && row.attachment?.type === 'prompt_snapshot');
  const groundingRow = log.find(row => row.uuid === groundingUuid);
  const groundingRequest = requests.find(payload => JSON.stringify(payload.messages || [])
    .includes('REAL_USER_SENTINEL'));
  console.log(JSON.stringify({sid, boundary, summary, imageCallRow, imageResultRow,
    promptSnapshotHasStatic:JSON.stringify(snapshot?.attachment?.systemPrompt || '').includes(staticMarker),
    groundingRow,
    groundingRequestHasDynamic:JSON.stringify(groundingRequest?.messages || []).includes(dynamicMarker),
    groundingRequestHasUser:JSON.stringify(groundingRequest?.messages || []).includes('REAL_USER_SENTINEL'),
    groundingRequestToolCount:Array.isArray(groundingRequest?.tools) ? groundingRequest.tools.length : -1}));
} finally { server.close(); }
