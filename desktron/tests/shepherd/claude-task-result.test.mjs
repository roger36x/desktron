import assert from 'node:assert/strict';
import {isClaudeTaskNotificationResult as notification, claudeTurnEnv,
  claudeDisallowedTools} from '../../lib/shepherd/claude_sdk_options.mjs';
const result={type:'result',session_id:'s',origin:{kind:'task-notification'},
  subtype:'success',is_error:false,num_turns:0,result:''};
assert.equal(notification(result,'s'),true);
assert.equal(notification({...result,origin:{kind:'human'}},'s'),false);
assert.equal(notification({...result,origin:undefined},'s'),false);
assert.equal(notification({type:'system',subtype:'task_notification'},'s'),false);
for (const patch of [{session_id:'foreign'},{subtype:'error_during_execution'},
    {is_error:true},{num_turns:1},{num_turns:undefined},{result:'autonomous answer'},
    {terminal_reason:'aborted_tools'}]) {
  assert.throws(()=>notification({...result,...patch},'s'),/verified no-op/);
}
assert.throws(()=>notification(result,'s','prior-assistant'),/verified no-op/);
assert.equal({...{CLAUDE_CODE_DISABLE_BACKGROUND_TASKS:'0'},...claudeTurnEnv}
  .CLAUDE_CODE_DISABLE_BACKGROUND_TASKS,'1');
assert.equal(claudeTurnEnv.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC,'1');
assert.deepEqual(claudeDisallowedTools,['Agent','Task','Monitor']);
console.log('Claude task notification result boundary OK');
