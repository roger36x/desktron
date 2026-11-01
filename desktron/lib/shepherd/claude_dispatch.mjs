/** Fixed, per-dispatch facts only. No prompt, stderr, URL or credential is emitted. */
const stages=new Set(['start_received','store_begin','store_ready','input_queued',
  'input_consumed','first_native_event','provider_response_started','provider_retry']);

export class ClaudeDispatch {
  constructor(dispatchId, emit=row=>process.stdout.write(JSON.stringify(row)+'\n')) {
    this.id=typeof dispatchId==='string' && /^[0-9a-f-]{36}$/.test(dispatchId) ? dispatchId : '';
    this.emit=emit;this.seen=new Set();this.stderrSeen=new Set();
  }
  phase(stage) {
    if(!this.id || !stages.has(stage))return;
    // Every api_retry is a fresh native progress pulse. Structural stages and
    // response-start remain one-shot diagnostics.
    if(stage!=='provider_retry' && this.seen.has(stage))return;
    this.seen.add(stage);
    try {this.emit({type:'claude_dispatch',dispatch_id:this.id,stage});} catch {}
  }
  native(message={}) {
    // Initialization can run before prepared/start. Do not report it as a
    // response to an input the SDK has not taken yet.
    if(!this.seen.has('input_consumed'))return;
    this.phase('first_native_event');
    if(message.type==='stream_event' && message.event?.type==='message_start'
        && message.event.message?.role==='assistant' && message.event.message?.id)
      this.phase('provider_response_started');
    if(message.type==='system' && message.subtype==='api_retry')
      this.phase('provider_retry');
  }
  stderr(chunk) {
    if(!this.id)return;
    // Heuristic categories, not a verified authentication/network diagnosis.
    // Retain neither chunks nor free text; at most four notices per attempt.
    const text=String(chunk);
    const stage=/\b(?:401|403)\b|authentication_error|invalid[_ ]api[_ ]key|OAuth.*(?:expired|revoked)/i.test(text)
      ? 'stderr_auth' : /\b429\b|rate[_ -]?limit/i.test(text) ? 'stderr_rate_limit'
      : /ECONN|ENOTFOUND|ETIMEDOUT|fetch failed|connection (?:reset|refused)|network error/i.test(text)
      ? 'stderr_network' : 'stderr_other';
    if(this.stderrSeen.has(stage))return;
    this.stderrSeen.add(stage);
    try {this.emit({type:'claude_dispatch',dispatch_id:this.id,stage});} catch {}
  }
}
