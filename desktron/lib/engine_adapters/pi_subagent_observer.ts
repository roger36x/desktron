/** Passive child telemetry for both upstream foreground and detached runners. */
import {existsSync,readFileSync,mkdirSync,writeFileSync,renameSync,appendFileSync} from 'node:fs';
import {dirname,join} from 'node:path';
import {randomUUID} from 'node:crypto';

export default function observer(pi:any){
  let row:any,dir:string,queued=false,softStalled=false,control:any;
  const runningTools=new Set<string>();
  const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  const save=()=>{if(!row)return;
    const state=join(dir,'state.json');
    if(existsSync(state))try{if(JSON.parse(readFileSync(state,'utf8')).status==='done')return;}catch{}
    row.updated=Date.now();const tmp=join(dir,randomUUID()+'.tmp');
    writeFileSync(tmp,JSON.stringify(row),{mode:0o600});renameSync(tmp,join(dir,'state.json'));};
  const bounded=(value:any)=>Number.isInteger(value)&&value>=0&&value<=3600000;
  const publicCode=(value:any)=>typeof value==='string'&&/^[A-Z_]{2,40}$/.test(value)?value:'';
  const safeText=(value:any,limit=300)=>String(value ?? '').replace(/[\u0000-\u001f\u007f]+/g,' ').trim().slice(0,limit);
  const classifyText=(text:string)=>{
    const s=text.toLowerCase();
    if(/quota|insufficient.{0,20}(credit|balance)|额度不足/.test(s))return 'QUOTA';
    if(/context_length_exceeded|context.{0,30}(exceed|limit)|token.{0,20}limit|上下文超限/.test(s))return 'CONTEXT';
    if(/timed? ?out|timeout|超时/.test(s))return 'TIMEOUT';
    if(/rate.?limit|限流|429/.test(s))return 'RATE_LIMIT';
    if(/overload|busy|capacity|503|529|繁忙|排队/.test(s))return 'OVERLOADED';
    if(/auth|unauthorized|forbidden|401|403|登录|无权/.test(s))return 'AUTH';
    if(/abort|cancel|取消|中断/.test(s))return 'ABORTED';
    return 'SUBAGENT_ERROR';
  };
  const recordError=(value:any)=>{
    if(!value||typeof value!=='object')value={message:value};
    const code=publicCode(value.code)||classifyText(safeText(value.message ?? value.error));
    row.error_code=code;row.error_kind=code.toLowerCase();
    row.error=safeText(value.message ?? value.error) || '子代理失败';
    if(Number.isSafeInteger(value.upstreamCode))row.upstream_code=value.upstreamCode;
  };
  const accept=(channel:string,value:any)=>{
    if(!value||typeof value!=='object')return false;
    if(channel==='QUEUE'){
      if(!['queue_begin','request_wait_in_queue','queue_end','request_dequeue'].includes(value.phase))return false;
      if(value.position!==undefined&&(!Number.isInteger(value.position)||value.position<0))return false;
    }
    if(channel==='WAIT'){
      if(!['request_start','progress','waiting'].includes(value.kind))return false;
      if(value.kind==='waiting'&&(!bounded(value.elapsed_ms)||!bounded(value.remaining_ms)
        ||value.transport_age_ms!==undefined&&value.transport_age_ms!==null
          &&!bounded(value.transport_age_ms)))return false;
    }
    if(channel==='RETRY'){
      const sampling=value.sampling===true,code=String(value.code||'').toUpperCase();
      if(!Number.isInteger(value.attempt)||value.attempt<1||!bounded(value.delay_ms)||!bounded(value.remaining_ms))return false;
      if(sampling){if(!['UPSTREAM','NETWORK','TIMEOUT','TRUNCATED'].includes(code)
        ||value.status!==undefined&&value.status!==null&&!Number.isInteger(value.status))return false;}
      else if(![429,503,529].includes(value.status))return false;
    }
    const {request_epoch:epoch,request_seq:request,notice_seq:notice}=value;
    const sequenced=epoch!==undefined||request!==undefined||notice!==undefined;
    const start=channel==='WAIT'&&value.kind==='request_start';
    if(!sequenced)return !control&&!start;
    if(!uuid.test(epoch)||!Number.isInteger(request)||request<1||!Number.isInteger(notice)||notice<1)return false;
    if(start){
      if(notice!==1||control&&(control.epoch!==epoch||request<=control.request))return false;
    }else if(!control||control.epoch!==epoch||request!==control.request||notice<=control.notice)return false;
    control={epoch,request,notice};return true;
  };
  // Desk liveness reads progress_at: only real progress moves it. Transport
  // waiting heartbeats keep `updated` fresh but never count as progress.
  const beat=()=>{if(row)row.progress_at=Date.now();};
  const modelState=(label='处理中')=>{queued=false;row.phase='model';row.last=label;};
  const projectedState=(label='处理中')=>{
    queued=false;
    if(softStalled){row.phase='stalled';row.last='长时间无新进展';}
    else if(runningTools.size){row.phase='tool';row.last='工具执行中';}
    else modelState(label);
  };
  const progressState=(label='处理中')=>{softStalled=false;beat();projectedState(label);};
  const messageProgress=(message:any)=>{
    if(message?.role!=='assistant'||['error','aborted'].includes(message.stopReason)||message.errorMessage)return false;
    const content=message.content;
    if(typeof content==='string')return content.trim().length>0;
    return Array.isArray(content)&&content.some((block:any)=>block&&typeof block==='object'&&(
      block.type==='toolCall'||typeof block.text==='string'&&block.text.trim().length>0||
      typeof block.thinking==='string'&&block.thinking.trim().length>0));
  };
  pi.on('session_start',(_event:any,ctx:any)=>{
    const file=ctx.sessionManager.getSessionFile();if(!file)throw new Error('Desk child session file unavailable');
    let parent=dirname(file),launch:any;
    for(let n=0;n<12;n++){
      const path=join(parent,'launch.json');
      if(existsSync(path)){launch=JSON.parse(readFileSync(path,'utf8'));break;}
      const next=dirname(parent);if(next===parent)break;parent=next;
    }
    if(!launch?.sessionID)throw new Error('Desk child launch identity unavailable');
    const id=ctx.sessionManager.getSessionId();
    if(!/^[\w-]+$/.test(id))throw new Error('Desk child identity invalid');
    dir=join(dirname(dirname(parent)),id);mkdirSync(dir,{recursive:true,mode:0o700});
    row={id,sessionID:launch.sessionID,toolCallId:launch.toolCallId,callId:launch.id,
      runtime:'pi-subagents-v2',mode:launch.mode,pid:process.ppid,workerPid:process.pid,
      nativeSessionFile:file,model:ctx.model?`${ctx.model.provider}/${ctx.model.id}`:undefined,status:'running',started:Date.now(),updated:Date.now(),phase:'model',last:'处理中'};
    beat();
    save();
  });
  for(const type of ['message_start','message_update','message_end','tool_execution_start','tool_execution_end']){
    pi.on(type,(event:any)=>{
      if(!row)return;
      let value=event;
      if(type==='message_update'){
        const d=event.assistantMessageEvent;
        if(!['text_delta','thinking_delta','toolcall_delta'].includes(d?.type)||
          typeof d.delta!=='string'||!d.delta.length)return;
        value={type,assistantMessageEvent:{type:d.type,delta:d.delta,contentIndex:d.contentIndex}};
        progressState('正在输出');
      }else if(type==='message_start')value={type};
      else if(type==='message_end'&&messageProgress(event.message))progressState('正在输出');
      if(type==='tool_execution_start'){
        runningTools.add(String(event.toolCallId||'unknown'));
        softStalled=false;queued=false;beat();row.phase='tool';row.last='调用 '+event.toolName;
      }
      if(type==='tool_execution_end'){
        runningTools.delete(String(event.toolCallId||'unknown'));
        if(event.isError)recordError((event.result||{}).error ?? (event.result||{}).content ?? event.result ?? event);
        progressState();
      }
      appendFileSync(join(dir,'events.jsonl'),JSON.stringify(value)+'\n',{mode:0o600});save();
    });
  }
  pi.on('before_agent_start',(event:any)=>{if(row){row.task=event.prompt;
    row.kind=/active_agent name="([^"]+)"/.exec(event.systemPrompt || '')?.[1]?.split(/--(?:desktron|desk)-/)[0];save();}});
  // A settled child is not stalled while its siblings or the workflow finish.
  pi.on('agent_end',()=>{if(row){beat();row.phase='settled';row.last='已完成';save();}});
  // The parent/upstream lifecycle, not this hook, decides the terminal outcome.
}
