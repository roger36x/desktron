// Offline contract test: the real Desk adapter, a recording upstream tool.
import assert from 'node:assert/strict';
import {mkdtempSync, mkdirSync, writeFileSync, readFileSync, readdirSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

// Desk owns deadlines; shrink them so the idle watchdog is observable offline.
process.env.DESKTRON_PI_SUBAGENT_IDLE_MS='1500';
process.env.DESKTRON_PI_SUBAGENT_TOTAL_MS='1800000';
const {default:children,IDLE_MS,TOTAL_MS,stalled}=await import('../../lib/engine_adapters/pi_children.ts');
assert.equal(IDLE_MS,1500);assert.equal(TOTAL_MS,1800000);

const root=mkdtempSync(join(tmpdir(),'desk-child-timeout-'));
try {
  mkdirSync(join(root,'src/api'),{recursive:true});
  writeFileSync(join(root,'src/api/capability-ceiling.js'),
    'export const registerSubagentCapabilityCeiling=()=>({dispose(){}});');
  writeFileSync(join(root,'src/api/control-channel.js'),
    'export const requestAsyncStop=()=>{throw new Error("unexpected cancellation in timeout contract");};');
  writeFileSync(join(root,'index.js'),`export default function(pi){
    pi.registerTool({name:'subagent',execute:async(...args)=>globalThis.recordChild(...args)});
  }`);
  Object.assign(process.env,{DESKTRON_PI_SUBAGENTS_ENTRY:root,
    DESKTRON_PI_SUBAGENTS_STORE:join(root,'store'),DESKTRON_SESSION_ID:'parent',
    DESKTRON_PI_SUBAGENTS_AGENT_DIR:root});
  const calls=[];
  globalThis.recordChild=async(id,input)=>{
    calls.push(input);
    return {content:[{type:'text',text:'answer'}],details:{asyncId:id,results:[{exitCode:0}]}};
  };
  let tool;
  const registered=[];
  await children({on(){},events:{on(){},emit(_name,request){
    registered.push(request.name);
    request.result={ok:true,registration:{dispose(){}}};
  }},registerTool(value){tool=value;},getThinkingLevel(){return 'off';}});
  assert.equal(registered.length,3);
  assert.ok(registered.every(name=>/^desk-(?:scout|reviewer|oracle)--desktron-[a-f0-9]{32}$/.test(name)));
  assert.equal(Object.hasOwn(tool.parameters.properties,'required'),true);
  const ctx={cwd:root,model:{provider:'fixture',id:'model'},
    modelRegistry:{getAvailable:()=>[{provider:'fixture',id:'model'}]}};
  const task={agent:'desk-scout',task:'Inspect code.',required:false};
  const run=(id,params)=>tool.execute(id,params,new AbortController().signal,()=>{},ctx);
  // The calling model no longer chooses a deadline.
  assert.equal(Object.hasOwn(tool.parameters.properties,'timeoutMs'),false);
  let n=0;
  for(const mode of ['sync','async'])for(const parallel of [false,true]){
    for(const timeoutMs of [undefined,1,5000,7200000,2147483647]){
      const id='test-'+n++;
      const params={mode,required:false,...(parallel?{tasks:[task,task,task]}:task),
        ...(timeoutMs===undefined?{}:{timeoutMs})};
      const before=calls.length;
      const result=await run(id,params);
      const launches=calls.slice(before);
      assert.equal(launches.length,parallel&&mode==='async'?3:1);
      for(const input of launches){
        assert.equal(input.timeoutMs,TOTAL_MS,'upstream always receives the Desk total deadline');
        assert.equal(Object.hasOwn(input,'workflowScript'),false);
        assert.equal(Boolean(input.workflow),parallel&&mode==='sync');
        if(input.workflow){
          assert.equal(readFileSync(input.workflow,'utf8'),'return await runs.all(args.tasks);\n');
          assert.equal(input.args.tasks.length,3);
          assert.equal(input.maxSubagentSpawnsPerRun,3);
        }
      }
      const launchMeta=JSON.parse(readFileSync(join(root,'store/parent/_calls',result.details.run_id,'launch.json')));
      assert.equal(launchMeta.timeoutMs,TOTAL_MS);
      assert.equal(launchMeta.idleTimeoutMs,IDLE_MS);
      assert.equal(launchMeta.requestedTimeoutMs,timeoutMs);
      assert.deepEqual(await run(id,params),result);
      assert.equal(calls.length,before+launches.length,'duplicate must not launch again');
      await assert.rejects(run(id,{...params,timeoutMs:timeoutMs===5000?6000:5000}),/不能变更任务/);
    }
  }
  const gwCtx={cwd:root,model:{provider:'example-gw',id:'gpt-fixture'},
    modelRegistry:{getAvailable:()=>[{provider:'example-gw',id:'gpt-fixture'}]}};
  const beforeGw=calls.length;
  const unthrottled=await tool.execute('gw-sync-triple',{mode:'sync',tasks:[task,task,task]},
    new AbortController().signal,()=>{},gwCtx);
  const gwLaunches=calls.slice(beforeGw);
  assert.equal(gwLaunches.length,1);
  assert.equal(Object.hasOwn(gwLaunches[0],'workflowScript'),false);
  assert.equal(readFileSync(gwLaunches[0].workflow,'utf8'),'return await runs.all(args.tasks);\n');
  assert.equal(gwLaunches[0].args.tasks.length,3);
  assert.equal(gwLaunches[0].maxSubagentSpawnsPerRun,3);
  assert.equal(unthrottled.details.runs.length,1);
  const unthrottledMeta=JSON.parse(readFileSync(join(root,'store/parent/_calls',unthrottled.details.run_id,'launch.json')));
  assert.equal(unthrottledMeta.children[0].agent,'desk-scout+desk-scout+desk-scout');
  assert.match(unthrottled.content[0].text,/desk-scout\+desk-scout\+desk-scout/);
  globalThis.recordChild=async()=>{const error=new Error('账号额度不足：synthetic detail');error.code='QUOTA';error.upstreamCode=402;throw error;};
  const thrown=await run('error-thrown',{mode:'sync',...task});
  assert.equal(thrown.isError,true);
  assert.equal(thrown.details.runs[0].error_code,'QUOTA');
  assert.equal(thrown.details.runs[0].error_kind,'quota');
  assert.equal(thrown.details.runs[0].upstream_code,402);
  let meta=JSON.parse(readFileSync(join(root,'store/parent/_calls',thrown.details.run_id,'launch.json')));
  assert.equal(meta.children[0].error_code,'QUOTA');
  globalThis.recordChild=async()=>({isError:false,content:[{type:'text',text:'partial'}],details:{results:[{exitCode:1,error:{code:'MODEL',message:'Model not found'}}]}});
  const nested=await run('error-nested',{mode:'sync',...task});
  assert.equal(nested.isError,true);
  assert.equal(nested.details.runs[0].error_code,'MODEL');
  assert.equal(nested.details.runs[0].exit_code,1);
  meta=JSON.parse(readFileSync(join(root,'store/parent/_calls',nested.details.run_id,'launch.json')));
  assert.equal(meta.children[0].error_kind,'model');
  globalThis.recordChild=async()=>{throw new Error('Rejected: a subagent call is already in progress. Issue exactly ONE subagent call per turn.');};
  const concurrent=await run('error-concurrent',{mode:'sync',...task});
  assert.equal(concurrent.isError,true);
  assert.equal(concurrent.details.runs[0].error_code,'CONCURRENT_CALL');
  // Legacy timeoutMs values are ignored instead of shortening or rejecting work.
  for(const timeoutMs of [null,0,-1,1.5,'5000']){
    const before=calls.length;
    globalThis.recordChild=async(id,input)=>{calls.push(input);return {content:[{type:'text',text:'ok'}],details:{results:[{exitCode:0}]}};};
    const ok=await run('legacy-'+n++,{mode:'sync',...task,timeoutMs});
    assert.equal(ok.isError,false);assert.equal(calls.at(-1).timeoutMs,TOTAL_MS);assert.equal(calls.length,before+1);
  }

  // Liveness rule: queue/tool/retry phases are alive; settled children are ignored.
  const t0=1_000_000;
  assert.equal(stalled([],t0,t0+IDLE_MS),false);
  assert.equal(stalled([],t0,t0+IDLE_MS+1),true,'never started');
  assert.equal(stalled([{phase:'model',progress_at:t0,updated:t0+IDLE_MS}],t0,t0+IDLE_MS+1),true,'waiting heartbeat is not progress');
  for(const phase of ['capacity','tool','retry'])assert.equal(stalled([{phase,progress_at:t0}],t0,t0+10*IDLE_MS),false);
  assert.equal(stalled([{phase:'settled',progress_at:t0},{phase:'model',progress_at:t0+10*IDLE_MS}],t0,t0+10*IDLE_MS+1),false);
  assert.equal(stalled([{phase:'settled',progress_at:t0},{phase:'model',progress_at:t0}],t0,t0+IDLE_MS+1),true);

  // Sync watchdog: a child that keeps progressing survives past IDLE_MS; one that
  // stops progressing is aborted through the tool signal and reported IDLE_TIMEOUT
  // with its partial output, without being mistaken for a parent interrupt.
  // Fork children write their session under parent/forks/, not native/<index>/.
  const observe=(rid,progress,fork)=>{const d=join(root,'store/parent','child-'+rid);mkdirSync(d,{recursive:true});
    writeFileSync(join(d,'state.json'),JSON.stringify({callId:rid,runtime:'pi-subagents-v2',status:'running',phase:'model',
      nativeSessionFile:join(root,'store/parent/_calls',rid,fork?'parent/forks/f.jsonl':'native/0/s.jsonl'),started:Date.now(),progress_at:progress}));};
  globalThis.recordChild=async(id,input,signal)=>{
    const rid=input.sessionDir.replaceAll('\\', '/').split('/_calls/')[1].split('/')[0];
    // The progressing child outlives IDLE_MS twice over; the stalled one waits
    // for the watchdog (bounded, so a missing abort fails instead of hanging).
    const alive=id.startsWith('alive');
    const until=Date.now()+(alive?IDLE_MS*2:IDLE_MS*20);
    while(Date.now()<until){
      observe(rid,alive?Date.now():Date.now()-IDLE_MS*2,id.includes('fork'));
      if(signal.aborted)return {isError:false,content:[{type:'text',text:'partial findings'}],details:{results:[{exitCode:0,interrupted:true}]}};
      await new Promise(r=>setTimeout(r,50));
    }
    return {content:[{type:'text',text:'finished'}],details:{results:[{exitCode:0}]}};
  };
  const alive=await run('alive-sync',{mode:'sync',...task});
  assert.equal(alive.isError,false,'progressing child must not be stopped');
  const aliveFork=await run('alive-fork-sync',{mode:'sync',tasks:[task,task,task]});
  assert.equal(aliveFork.isError,false,'progressing fork child must not look never-started');
  const stuck=await run('stuck-sync',{mode:'sync',...task});
  assert.equal(stuck.isError,true);
  assert.equal(stuck.details.runs[0].error_code,'IDLE_TIMEOUT');
  assert.match(stuck.details.runs[0].error,/无进展/);
  assert.match(JSON.stringify(stuck.content),/partial findings/);
  meta=JSON.parse(readFileSync(join(root,'store/parent/_calls',stuck.details.run_id,'launch.json')));
  assert.equal(meta.children[0].watchdog.reason,'idle');
  assert.equal(meta.outcome,'failed');
  const {default:observer}=await import('../../lib/engine_adapters/pi_subagent_observer.ts');
  for(const suffix of ['--desk-fixture','--desktron-fixture','']){
    const launch=join(root,'observer-'+(suffix||'plain'),'parent','_calls','run');
    const file=join(launch,'native','s.jsonl');
    mkdirSync(join(launch,'native'),{recursive:true});
    writeFileSync(join(launch,'launch.json'),JSON.stringify({sessionID:'parent',id:'run'}));
    const hooks=new Map();
    observer({on(name,handler){hooks.set(name,handler);}});
    hooks.get('session_start')({}, {sessionManager:{getSessionFile:()=>file,getSessionId:()=> 'observed-child'}});
    hooks.get('before_agent_start')({prompt:'fixture',systemPrompt:'<active_agent name="desk-scout'+suffix+'">'});
    const state=JSON.parse(readFileSync(join(launch,'..','..','observed-child','state.json'),'utf8'));
    assert.equal(state.kind,'desk-scout','observer accepts both old and new suffixes');
  }
  console.log('timeout contract and observer role suffix compatibility passed');
}finally{
  delete globalThis.recordChild;
  rmSync(root,{recursive:true,force:true});
}
