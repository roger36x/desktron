import http from 'node:http';
import { pathToFileURL } from 'node:url';
import { ShepherdClaudeStore } from '../../lib/shepherd/claude_sqlite_store.mjs';

const [sdkRoot, dbPath, sessionId, cwd, claudeBinary] = process.argv.slice(2);
const sdk = await import(new URL('sdk.mjs', pathToFileURL(sdkRoot + '/')).href);
const store = new ShepherdClaudeStore(dbPath, sessionId);
const requests = [];
let toolCalls = 0;
const server = http.createServer((req, res) => {
  let raw = '';
  req.on('data', chunk => raw += chunk);
  req.on('end', () => {
    const body = JSON.parse(raw || '{}');
    if (req.url.includes('count_tokens')) {
      const out = '{"input_tokens":10}';
      res.writeHead(200, {'content-type': 'application/json', 'content-length': out.length});
      res.end(out); return;
    }
    requests.push(body);
    toolCalls += (body.messages || []).flatMap(message => Array.isArray(message.content) ? message.content : [])
      .filter(block => block.type === 'tool_use').length;
    const text = 'connector-resume-ok', id = 'msg_connector';
    const events = [
      {type:'message_start', message:{id,type:'message',role:'assistant',content:[],model:body.model,
        stop_reason:null,stop_sequence:null,usage:{input_tokens:10,output_tokens:0}}},
      {type:'content_block_start',index:0,content_block:{type:'text',text:''}},
      {type:'content_block_delta',index:0,delta:{type:'text_delta',text}},
      {type:'content_block_stop',index:0},
      {type:'message_delta',delta:{stop_reason:'end_turn',stop_sequence:null},usage:{output_tokens:2}},
      {type:'message_stop'},
    ];
    const out = events.map(event => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join('');
    res.writeHead(200, {'content-type':'text/event-stream','content-length':Buffer.byteLength(out)});
    res.end(out);
  });
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const env = {...process.env, CLAUDECODE:'', ANTHROPIC_BASE_URL:`http://127.0.0.1:${server.address().port}`,
  ANTHROPIC_API_KEY:'synthetic-test-only', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC:'1',
  CLAUDE_CONFIG_DIR: cwd + '/.claude-test'};
try {
  for await (const _message of sdk.query({prompt:'current-user-marker', options:{cwd, env,
    resume:sessionId, sessionStore:store, sessionStoreFlush:'eager', tools:[], permissionMode:'dontAsk',
    pathToClaudeCodeExecutable:claudeBinary}})) {}
  const messages = requests.findLast(request => Array.isArray(request.messages))?.messages || [];
  const flat = JSON.stringify(messages);
  console.log(JSON.stringify({ok: flat.includes('current-user-marker'),
    hasSeedUser: flat.includes('seed-user'), hasSeedAssistant: flat.includes('seed-assistant'),
    hasToolResult: flat.includes('historical-tool-result'),
    hasCheckpoint: flat.includes('portable-checkpoint-marker'),
    roles: messages.map(message => message.role), toolCalls}));
} finally {
  server.close();
}
