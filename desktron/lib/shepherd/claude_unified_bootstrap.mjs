import http from 'node:http';
import {mkdir} from 'node:fs/promises';
export async function bootstrap(store, key, payload, sdk, stableParentEnv) {
  const snapshot = store.promptSnapshot();
  if (snapshot) {
    if (JSON.stringify(snapshot.attachment.systemPrompt)!==JSON.stringify([payload.systemPrompt]))
      throw new Error('Unified Claude checkpoint prompt mismatch');
    return;
  }
  const saved = await store.load(key);
  store.reset();
  // Build the immutable prompt snapshot before linking imported messages.
  // Session metadata can follow the final message and has no UUID.
  const config = payload.dbPath + '.bootstrap-config';
  await mkdir(config, {recursive: true, mode: 0o700});
  const server = http.createServer((request, response) => {
    let raw = '';
    request.on('data', chunk => raw += chunk);
    request.on('end', () => {
      if (request.url.includes('count_tokens')) {
        const body = '{"input_tokens":1}';
        response.writeHead(200, {'content-type':'application/json', 'content-length':body.length});
        response.end(body); return;
      }
      const requestBody = JSON.parse(raw || '{}'), id = 'msg_shepherd_bootstrap';
      const events = [
        {type:'message_start', message:{id,type:'message',role:'assistant',content:[],model:requestBody.model,
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
    const env = {...stableParentEnv(), CLAUDE_CONFIG_DIR: config,
      ANTHROPIC_BASE_URL: `http://127.0.0.1:${server.address().port}`,
      ANTHROPIC_API_KEY: 'shepherd-local-bootstrap', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1'};
    const bootstrapOptions = {
      cwd:payload.cwd, env, sessionId:payload.sessionId, sessionStore:store,
      sessionStoreFlush:'eager', tools:[], permissionMode:'dontAsk', settingSources:[],
      pathToClaudeCodeExecutable:payload.claudeBinary, maxTurns:1};
    if (payload.systemPrompt) bootstrapOptions.systemPrompt = {
      type:'custom', prompt:payload.systemPrompt, snapshot:true};
    for await (const _message of sdk.query({
      prompt:'Shepherd native bootstrap. No user request.', options:bootstrapOptions})) {}
  } finally { server.close(); }
  if (!(await store.load(key)).some(e=>e.type==='attachment' && e.attachment?.type==='prompt_snapshot'))
    throw new Error('Claude private bootstrap snapshot missing');
  let parent=store.lastUuid();
  for (const entry of saved) {entry.parentUuid=parent;parent=entry.uuid;}
  await store.append(key,saved);
}

