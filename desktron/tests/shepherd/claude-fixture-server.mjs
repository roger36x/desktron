#!/usr/bin/env node
import http from 'node:http';
const requestedPaths = new Set();
const server = http.createServer((request, response) => {
  let raw = '';
  request.on('data', chunk => raw += chunk);
  request.on('end', () => {
    if (request.url.includes('count_tokens')) {
      const body = '{"input_tokens":10}';
      response.writeHead(200, {'content-type':'application/json','content-length':body.length});
      response.end(body); return;
    }
    const payload = JSON.parse(raw || '{}');
    const last = (payload.messages || []).at(-1) || {};
    const source = Array.isArray(last.content) ? last.content.map(x => x.text || '').join('') : String(last.content || '');
    const id = 'msg_' + Math.random().toString(16).slice(2);
    const messages = payload.messages || [];
    const textOf = message => Array.isArray(message.content)
      ? message.content.map(x => x.text || '').join('') : String(message.content || '');
    // Native hooks may append context after the actual user message.
    const requestAt = messages.findLastIndex(message => textOf(message).includes('permission-fixture:'));
    const permissionPath = requestAt < 0 ? null : textOf(messages[requestAt]).match(/permission-fixture:([^\s<]+)/)?.[1];
    const protectedAt = messages.findLastIndex(message => /protected-(?:read|bash)-fixture:/.test(textOf(message)));
    const protectedMatch = protectedAt < 0 ? null
      : textOf(messages[protectedAt]).match(/protected-(read|bash)-fixture:([^\s<]+)/);
    const toolStart = Math.max(requestAt, protectedAt);
    const toolFinished = messages.slice(toolStart + 1).some(message =>
      Array.isArray(message.content) && message.content.some(block => block.type === 'tool_result'));
    const text = source.includes('Shepherd native bootstrap') ? 'Shepherd session initialized.'
      : protectedMatch && toolFinished ? 'continued-after-denial' : 'runner-ok';
    const events = [
      {type:'message_start',message:{id,type:'message',role:'assistant',content:[],model:payload.model,
       stop_reason:null,stop_sequence:null,usage:{input_tokens:10,output_tokens:0}}},
      {type:'content_block_start',index:0,content_block:{type:'text',text:''}},
      {type:'content_block_delta',index:0,delta:{type:'text_delta',text}},
      {type:'content_block_stop',index:0},
      {type:'message_delta',delta:{stop_reason:'end_turn',stop_sequence:null},usage:{output_tokens:2}},
      {type:'message_stop'},
    ];
    if (permissionPath && !toolFinished && !requestedPaths.has(permissionPath)) {
      requestedPaths.add(permissionPath);
      const tool = (payload.tools || []).some(x => x.name === 'write') ? 'write' : 'Write';
      const toolInput = tool === 'write' ? {path:permissionPath,content:'original'}
        : {file_path:permissionPath,content:'original'};
      events.splice(1, 4,
        {type:'content_block_start',index:0,content_block:{type:'tool_use',id:'toolu_shepherd',name:tool,input:{}}},
        {type:'content_block_delta',index:0,delta:{type:'input_json_delta',partial_json:JSON.stringify(toolInput)}},
        {type:'content_block_stop',index:0},
        {type:'message_delta',delta:{stop_reason:'tool_use',stop_sequence:null},usage:{output_tokens:10}});
    } else if (protectedMatch && !toolFinished) {
      const [, kind, protectedPath] = protectedMatch;
      const tool = kind === 'read' ? 'Read' : 'Bash';
      const toolInput = kind === 'read' ? {file_path:protectedPath}
        : {command:`cat ${JSON.stringify(protectedPath)}`};
      events.splice(1, 4,
        {type:'content_block_start',index:0,content_block:{type:'tool_use',id:`toolu_protected_${kind}`,name:tool,input:{}}},
        {type:'content_block_delta',index:0,delta:{type:'input_json_delta',partial_json:JSON.stringify(toolInput)}},
        {type:'content_block_stop',index:0},
        {type:'message_delta',delta:{stop_reason:'tool_use',stop_sequence:null},usage:{output_tokens:10}});
    }
    const body = events.map(event => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join('');
    response.writeHead(200, {'content-type':'text/event-stream','content-length':Buffer.byteLength(body)});
    response.end(body);
  });
});
server.listen(0, '127.0.0.1', () => console.log(JSON.stringify({port:server.address().port})));
process.on('SIGTERM', () => server.close(() => process.exit(0)));
