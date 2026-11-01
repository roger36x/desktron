import {EventEmitter} from 'node:events';
import {ShepherdClaudeMemoryStore} from '../lib/shepherd/claude_memory_store.mjs';

const count=Number(process.argv[2]||4000), bytes=Number(process.argv[3]||2048);
const blob='x'.repeat(bytes), entries=[];
for(let i=0;i<count;i++)entries.push({type:i%2?'assistant':'user',uuid:`u-${i}`,parentUuid:i?`u-${i-1}`:null,
  message:{role:i%2?'assistant':'user',content:[{type:'text',text:blob+i}]}});
if(global.gc)global.gc();const before=process.memoryUsage();
const store=new ShepherdClaudeMemoryStore(entries,'session',new EventEmitter());
if(global.gc)global.gc();const stored=process.memoryUsage();
for(let i=0;i<10;i++){store.size();store.revision();store.slice(Math.max(0,count-4));}
if(global.gc)global.gc();const after=process.memoryUsage();
console.log(JSON.stringify({count,bytes,size:store.size(),
  heap_delta_mb:+((stored.heapUsed-before.heapUsed)/(1<<20)).toFixed(2),
  rss_delta_mb:+((stored.rss-before.rss)/(1<<20)).toFixed(2),
  post_ops_heap_mb:+((after.heapUsed-stored.heapUsed)/(1<<20)).toFixed(2)}));
