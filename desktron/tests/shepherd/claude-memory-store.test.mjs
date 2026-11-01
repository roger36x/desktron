import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {EventEmitter} from 'node:events';
import {ShepherdClaudeMemoryStore,claudeStoreTransitionRevision} from '../../lib/shepherd/claude_memory_store.mjs';

const input=new EventEmitter(),key={sessionId:'session',projectKey:'shepherd'};
const entries=[
  {type:'attachment',uuid:'snapshot',attachment:{type:'prompt_snapshot',systemPrompt:['system']}},
  {type:'user',uuid:'user',parentUuid:'snapshot',message:{role:'user',content:'hello'}},
  {type:'assistant',uuid:'assistant',parentUuid:'user',message:{role:'assistant',content:[{type:'text',text:'world'}]}},
];
const expected=createHash('sha256').update(JSON.stringify(entries)).digest('hex');
const store=new ShepherdClaudeMemoryStore(entries,'session',input);
assert.equal(store.size(),3);assert.equal(store.revision(),expected);
assert.equal(store.promptSnapshot().uuid,'snapshot');
const first=await store.load(key);first[0].attachment.systemPrompt[0]='mutated';
assert.equal((await store.load(key))[0].attachment.systemPrompt[0],'system');
await store.append(key,[entries[1]]);assert.equal(store.size(),3);
// Claude CLI decorates a loaded cross-engine user entry with a derived slug.
// That mirror replay is non-semantic and must not rewrite the archived bytes.
await store.append(key,[{...entries[1],slug:'derived-display-slug'}]);assert.equal(store.size(),3);
assert.equal(store.revision(),expected);
await assert.rejects(()=>store.append(key,[{...entries[1],slug:'derived-display-slug',
  message:{role:'user',content:'different'}}]),/UUID conflict/);
const boundary={type:'system',subtype:'compact_boundary',uuid:'boundary',parentUuid:'assistant'};
const summary={type:'user',uuid:'summary',parentUuid:'boundary',isCompactSummary:true,message:{role:'user',content:'short'}};
await store.append(key,[boundary,summary]);
assert.equal(store.revision(),createHash('sha256').update(JSON.stringify([...entries,boundary,summary])).digest('hex'));
assert.deepEqual(store.checkpointEntries().map(row=>row.uuid),['snapshot','boundary','summary']);
assert.equal(store.lastUuid(),'summary');

// The cross-language receipt hashes the exact JSON.stringify payloads emitted
// by the Bridge; it must not depend on another runtime recreating those bytes.
const receiptEntries=[{cost:1e-7,negativeZero:-0,'10':'ten','2':'two',text:'\ud800'},
                      {cost:1.0,text:'雪'}];
const receiptPayloads=receiptEntries.map(JSON.stringify);
const receiptHash=createHash('sha256').update(
  `claude-store-transition-v1\0${'a'.repeat(64)}\0[${receiptPayloads.join(',')}]`).digest('hex');
assert.equal(claudeStoreTransitionRevision('a'.repeat(64),receiptPayloads),receiptHash);
assert.equal(receiptPayloads[0],'{"2":"two","10":"ten","cost":1e-7,"negativeZero":0,"text":"\\ud800"}');

// A lost archive acknowledgement poisons this host. Retrying the same UUID may
// not be treated as an already-durable success.
const lostInput=new EventEmitter(),lost=new ShepherdClaudeMemoryStore([], 'session', lostInput,{ackTimeoutMs:5});
lost.recording=true;const frames=[],write=process.stdout.write;
process.stdout.write=value=>{frames.push(String(value));return true;};
await assert.rejects(()=>lost.append(key,[entries[1]]),/acknowledgement timeout/);
await assert.rejects(()=>lost.append(key,[entries[1]]),/acknowledgement timeout/);
process.stdout.write=write;
assert.equal(frames.length,1);

// Cancellation while an archive write awaits acknowledgement must reject
// immediately and permanently poison the host rather than waiting for timeout.
const cancelInput=new EventEmitter();
const cancelled=new ShepherdClaudeMemoryStore([], 'session',cancelInput,{ackTimeoutMs:30000});
cancelled.recording=true;process.stdout.write=()=>true;
const waiting=cancelled.append(key,[entries[1]]);
queueMicrotask(()=>cancelInput.emit('line',JSON.stringify({type:'cancel'})));
await assert.rejects(()=>waiting,/acknowledgement cancelled/);
await assert.rejects(()=>cancelled.append(key,[entries[2]]),/acknowledgement cancelled/);
process.stdout.write=write;

const broken=new ShepherdClaudeMemoryStore([], 'session',new EventEmitter());broken.recording=true;
let attempts=0;process.stdout.write=()=>{attempts++;throw Object.assign(new Error('broken pipe'),{code:'EPIPE'});};
await assert.rejects(()=>broken.append(key,[entries[2]]),/broken pipe/);
await assert.rejects(()=>broken.append(key,[entries[2]]),/broken pipe/);
process.stdout.write=write;assert.equal(attempts,1);
// A committed resident prefix must never acquire unarchived idle writes.
for (const added of [[{type:'queue-operation',operation:'enqueue'}],
    [{...entries[1],message:{role:'user',content:'changed'}}],
    [entries[1],{type:'assistant',uuid:'late'}]]) {
  const idle=new ShepherdClaudeMemoryStore(entries,'session',new EventEmitter());
  idle.seal();
  await idle.append(key,[entries[1]]); // identical replay is harmless
  await idle.append(key,[{...entries[1],slug:'derived-display-slug'}]);
  await assert.rejects(()=>idle.append(key,added),/outside an active turn/);
  assert.equal(idle.size(),3);assert.equal(idle.revision(),expected);
  assert.throws(()=>idle.beginTurn(),/outside an active turn/);
}
const resumedInput=new EventEmitter();
const resumed=new ShepherdClaudeMemoryStore(entries,'session',resumedInput);
resumed.seal();resumed.beginPredispatch();
process.stdout.write=value=>{
  const frame=JSON.parse(value);
  assert.deepEqual(frame.entryPayloads,frame.entries.map(JSON.stringify));
  queueMicrotask(()=>resumedInput.emit('line',JSON.stringify({type:'archive_ack',id:frame.id})));
  return true;
};
try {
  await resumed.append(key,[boundary]);
  assert.equal(resumed.size(),3); // pre-dispatch writes remain outside the trusted prefix
  const begin=resumed.beginTurn();
  assert.throws(()=>resumed.seal(),/unacknowledged/);
  await begin;resumed.seal();
} finally {process.stdout.write=write;}
assert.equal(resumed.size(),4);
console.log(JSON.stringify({ok:true,size:store.size()}));
