"""Common admission and live rejection recovery: no provider-specific replay."""
from unittest import mock

import pytest

from shepherd.context_admission import admit, context_error, RejectionTracker
from shepherd.harness import ExecutionHarness
from shepherd.service import Service
from shepherd.store import Conflict, SourceVerificationError
from shepherd.unified_runners import UnifiedRunner
from test_shepherd_unified_storage import ENGINES, JournalRunner


@pytest.mark.parametrize('error', ['context_length_exceeded', 'contextWindowExceeded',
    'maximum context length is 123', 'prompt is too long', '上下文超限',
    {'codexErrorInfo': {'contextWindowExceeded': {}}},
    'Your input exceeds the context window of this model',
    'The input token count (100) exceeds the maximum number of tokens (80)',
    'the request exceeds the available context size', 'CONTEXT'])
def test_native_context_errors(error):
    assert context_error(error)


@pytest.mark.parametrize('error', ['serverOverloaded', '429 quota exhausted', '401 auth',
    'connection reset', 'compaction failed', 'request timed out', 'output limit',
    'rate limit: context_length_exceeded', 'quota: maximum context length'])
def test_other_failures_are_not_context_errors(error):
    assert not context_error(error)


def test_admission_source_rules():
    base=dict(window=32000,current=1000,history=100,has_history=True,
              has_checkpoint=True,required=True)
    assert not admit(**base).reason
    assert admit(**{**base,'has_checkpoint':False}).reason=='no_compatible_checkpoint'
    assert not admit(**{**base,'has_checkpoint':False,'has_history':False}).reason
    assert admit(**{**base,'history':30000}).reason=='context_over_budget'
    assert admit(**base,learned=1000,rejected=True).reason=='context_rejected'
    assert admit(**{**base,'window':0,'required':False,'has_checkpoint':False}).history_budget==16384
    with pytest.raises(Conflict,match='窗口未知'):admit(**{**base,'window':0})
    with pytest.raises(Conflict,match='当前输入'):admit(**{**base,'current':32000})


def reject(runner, error='context_length_exceeded'):
    if runner.connector.engine == 'codex':
        runner.on_event('turn/completed',{'turn':{'id':'native','status':'failed',
            'error':{'message':error}}})
    elif runner.connector.engine=='claude':
        runner.on_event({'type':'result','subtype':'error_during_execution',
            'is_error':True,'errors':[error]})
    else:
        runner.on_event({'type':'compaction_end','reason':'overflow','aborted':False,
            'errorMessage':error})


class RejectingRunner(UnifiedRunner):
    def __init__(self, connector, *, failures=1, progress=False, error='context_length_exceeded'):
        super().__init__(connector, options={'model':'fixture','_context_window':32000})
        self.calls=0;self.failures=failures;self.progress=progress;self.error=error
        self.budgets=[]

    def native(self, prompt, rounds, checkpoint, dispatch, locate):
        kwargs={}
        if self.connector.engine=='claude':
            kwargs['native_anchor']=self._claude_native_anchor({
                'parentUuid':'','nativeRevision':'a'*64,'startSeq':0,'storeSize':0,
                'bridgeProtocolVersion':3,'claudeVersion':'2.1.283'})
        dispatch(**kwargs)
        self.calls+=1
        self.budgets.append(self.connector.admission['history_tokens'])
        if self.calls<=self.failures:
            if self.progress:self.context_tracker.progress=True
            reject(self,self.error)
            return 'failed_recoverable'
        return 'completed'

    _claude=native
    _pi=native
    _codex=native


def setup(tmp_path, engine):
    service=Service(tmp_path,storage='unified');service.create('admit',engine,tmp_path)
    active=service.active('admit');c=service.connector(active['binding']);lease=active['lease']
    public=[{'type':'message','role':'assistant','content':[
        {'type':'output_text','text':'prior decision '+('history '*3000)}]}]
    ExecutionHarness(service.coordinator,c,JournalRunner(c,public)).run('admit',
        lease_id=lease['lease_id'],generation=lease['generation'],prompt='prior',
        input_public=[{'type':'message','role':'user','content':[{'type':'input_text','text':'prior'}]}])
    return service,c,lease


def run(service,c,lease,runner,prompt='continue'):
    return ExecutionHarness(service.coordinator,c,runner).run('admit',
        lease_id=lease['lease_id'],generation=lease['generation'],prompt=prompt,
        attempt_id='request',input_public=[{'type':'message','role':'user','content':[
            {'type':'input_text','text':prompt}]}])


@pytest.mark.parametrize('engine', ENGINES)
def test_rejection_commits_then_rebuilds_once_and_persists_learning(tmp_path,engine):
    service,c,lease=setup(tmp_path,engine);runner=RejectingRunner(c)
    result=run(service,c,lease,runner)
    assert result['terminal_state']=='completed'
    assert runner.calls==2 and runner.budgets[1]<runner.budgets[0]
    assert c.archive.read(c.binding,'admit:2')['terminal']=='failed_recoverable'
    assert c.archive.read(c.binding,'admit:3')['terminal']=='completed'
    rebuilt=Service(tmp_path,storage='unified').connector(c.binding)
    rebuilt.context({'model':'fixture','_context_window':32000,'_require_context_budget':True})
    assert rebuilt.admission['reason']=='context_rejected'
    assert rebuilt.admission['history_tokens']<=runner.budgets[0]//2
    rebuilt.context({'model':'other','_context_window':64000,'_require_context_budget':True})
    assert rebuilt.admission['reason']!='context_rejected'


@pytest.mark.parametrize('engine', ENGINES)
@pytest.mark.parametrize('model', ['', 'fixture'])
@pytest.mark.parametrize('has_history', [False, True])
def test_unknown_window_retains_native_admission(tmp_path,engine,model,has_history):
    if has_history:
        service,c,lease=setup(tmp_path,engine)
    else:
        service=Service(tmp_path,storage='unified');service.create('admit',engine,tmp_path)
        active=service.active('admit');c=service.connector(active['binding']);lease=active['lease']
    runner=RejectingRunner(c,failures=0)
    runner.options={'model':model} if model else {}  # No injected window or compaction threshold.
    result=run(service,c,lease,runner)
    assert result['terminal_state']=='completed' and runner.calls==1
    assert bool(runner.degraded_context)==has_history
    assert c.admission['reason']==('no_compatible_checkpoint' if has_history else '')
    assert c.admission['history_tokens']<=16384


@pytest.mark.parametrize('engine', ENGINES)
@pytest.mark.parametrize('model', ['', 'fixture'])
def test_unknown_window_still_learns_rejection_and_rebuilds(tmp_path,engine,model):
    service,c,lease=setup(tmp_path,engine);runner=RejectingRunner(c)
    runner.options={'model':model} if model else {}
    result=run(service,c,lease,runner)
    assert result['terminal_state']=='completed'
    assert runner.calls==2 and runner.budgets[1]<runner.budgets[0]
    rebuilt=Service(tmp_path,storage='unified').connector(c.binding)
    rebuilt.context({**runner.options,'_context_admission':True})
    assert rebuilt.admission['reason']=='context_rejected'
    assert rebuilt.admission['history_tokens']<=runner.budgets[0]//2


@pytest.mark.parametrize('engine', ENGINES)
@pytest.mark.parametrize('case', ['progress','auth','exhausted','compact','cancel'])
def test_replay_is_bounded_and_requires_no_progress(tmp_path,engine,case):
    service,c,lease=setup(tmp_path,engine)
    runner=RejectingRunner(c,failures=5,progress=case=='progress',
        error='401 unauthorized' if case=='auth' else 'context_length_exceeded')
    if case=='cancel':
        original=runner.native
        def cancelled(*args):
            terminal=original(*args)
            runner.cancelled.set()
            return terminal
        method='_claude' if engine=='claude' else '_pi' if engine=='pi' else '_codex'
        setattr(runner,method,cancelled)
    result=run(service,c,lease,runner,'/compact' if case=='compact' else 'continue')
    assert result['terminal_state']=='failed_recoverable'
    assert runner.calls==(2 if case=='exhausted' else 1)


@pytest.mark.parametrize('engine', ENGINES)
def test_unconfirmed_exception_never_replays_or_learns(tmp_path,engine):
    service,c,lease=setup(tmp_path,engine);runner=RejectingRunner(c)
    def uncertain(*args):
        kwargs={}
        if engine=='claude':
            kwargs['native_anchor']=runner._claude_native_anchor({
                'parentUuid':'','nativeRevision':'a'*64,'startSeq':0,'storeSize':0,
                'bridgeProtocolVersion':3,'claudeVersion':'2.1.283'})
        args[3](**kwargs)
        reject(runner)
        raise Conflict('transport lost')
    method='_claude' if engine=='claude' else '_pi' if engine=='pi' else '_codex'
    with mock.patch.object(runner,method,side_effect=uncertain):
        with pytest.raises(Conflict,match='transport lost'):run(service,c,lease,runner)
    assert c.archive.context_limit(c.admission['context_key']) is None
    assert service.coordinator.state('admit')['inflight']['state']=='needs_reconciliation'


@pytest.mark.parametrize('engine', ENGINES)
def test_learning_tampering_is_blocked(tmp_path,engine):
    service,c,lease=setup(tmp_path,engine);runner=RejectingRunner(c,progress=True)
    run(service,c,lease,runner)
    with c.archive.db() as db:db.execute('UPDATE context_limits SET history_tokens=999999')
    with pytest.raises(Conflict,match='evidence mismatch'):
        c.context({'model':'fixture','_context_window':32000})


@pytest.mark.parametrize('engine', ENGINES)
def test_missing_native_checkpoint_keeps_verified_history_when_it_fits(tmp_path,engine):
    service,c,lease=setup(tmp_path,engine)
    original=c.encode
    encoded=[]
    def observe(row):
        result=original(row)
        encoded.extend(result)
        return result
    with mock.patch.object(c,'encode',side_effect=observe):
        _,checkpoint,rounds=c.context({'model':'fixture','_context_window':32000,
            '_context_admission':True,'_require_context_budget':True})
    assert checkpoint is None and rounds
    assert 'history history history history history' in str(encoded)
    assert c.admission['reason']==''


@pytest.mark.parametrize('engine', ENGINES)
def test_normal_prose_cannot_trigger_recovery(engine):
    tracker=RejectionTracker(engine)
    if engine == 'codex':
        tracker.observe('item/completed',{'item':{'type':'agentMessage','text':'context_length_exceeded'}})
    elif engine=='claude':
        tracker.observe({'type':'assistant','message':{'content':[{'type':'text','text':'context_length_exceeded'}]}})
    else:
        tracker.observe({'type':'message_end','message':{'role':'assistant','content':[
            {'type':'text','text':'context_length_exceeded'}],'stopReason':'stop'}})
    assert tracker.progress and not tracker.rejected


@pytest.mark.parametrize('engine', ENGINES)
def test_failed_rebuild_returns_committed_failure_not_prompt_not_started(tmp_path,engine):
    service,c,lease=setup(tmp_path,engine);runner=RejectingRunner(c)
    original=c.context
    calls=[]
    def context(options):
        calls.append(options)
        if len(calls)>1:raise Conflict('no space for bounded history')
        return original(options)
    with mock.patch.object(c,'context',side_effect=context):
        result=run(service,c,lease,runner)
    assert result['terminal_state']=='failed_recoverable'
    assert runner.calls==1 and runner.context_retry_error=='no space for bounded history'
    assert len(service.coordinator.state('admit')['turns'])==2


@pytest.mark.parametrize('engine', ENGINES)
def test_tools_forbid_replay_even_when_terminal_says_context_error(engine):
    tracker=RejectionTracker(engine)
    if engine == 'codex':
        tracker.observe('item/started',{'item':{'type':'commandExecution','id':'tool-1'}})
        tracker.observe('turn/completed',{'turn':{'error':{'message':'context_length_exceeded'}}})
    elif engine=='claude':
        tracker.observe({'type':'stream_event','event':{'type':'content_block_start',
            'content_block':{'type':'tool_use','name':'Bash'}}})
        tracker.observe({'type':'result','is_error':True,'errors':['prompt is too long']})
    else:
        tracker.observe({'type':'tool_execution_start','toolCallId':'tool'})
        tracker.observe({'type':'compaction_end','errorMessage':'context_length_exceeded'})
    assert tracker.rejected and not tracker.retryable('failed_recoverable')


@pytest.mark.parametrize('engine', ['codex'])
def test_injected_codex_history_is_not_current_turn_progress(engine):
    tracker=RejectionTracker(engine)
    tracker.observe('rawResponseItem/completed',{'item':{'type':'message','role':'developer',
        'content':[{'type':'input_text','text':'injected instructions'}]}})
    tracker.observe('rawResponseItem/completed',{'item':{'type':'message','role':'assistant',
        'content':[{'type':'output_text','text':'historical answer'}]}})
    tracker.observe('item/completed',{'item':{'type':'userMessage','id':'user-1',
        'content':[{'type':'text','text':'current request'}]}})
    tracker.observe('turn/completed',{'turn':{'error':{'message':'context_length_exceeded'}}})
    assert tracker.rejected and tracker.retryable('failed_recoverable')
    tracker.observe('item/completed',{'item':{'type':'agentMessage','id':'answer-1',
        'text':'partial answer'}})
    assert tracker.progress and not tracker.retryable('failed_recoverable')


def test_claude_placeholder_is_not_model_progress():
    tracker=RejectionTracker('claude')
    tracker.observe({'type':'assistant','message':{'model':'<synthetic>',
        'stop_reason':'stop_sequence','content':[{'type':'text','text':'No response requested.'}]}})
    assert not tracker.progress
    tracker.observe({'type':'assistant','message':{'model':'real',
        'stop_reason':'stop_sequence','content':[{'type':'text','text':'No response requested.'}]}})
    assert tracker.progress


@pytest.mark.parametrize('engine', ENGINES)
def test_new_session_rejection_does_not_poison_future_history_budget(tmp_path,engine):
    service=Service(tmp_path,storage='unified');service.create('admit',engine,tmp_path)
    active=service.active('admit');c=service.connector(active['binding']);lease=active['lease']
    runner=RejectingRunner(c)
    result=run(service,c,lease,runner)
    assert result['terminal_state']=='failed_recoverable' and runner.calls==1
    assert c.archive.context_limit(c.admission['context_key']) is None


def test_native_hooks_are_side_effects_not_replayable_progress():
    tracker=RejectionTracker('claude')
    tracker.observe({'type':'system','subtype':'hook_started'})
    tracker.observe({'type':'result','is_error':True,'errors':['prompt is too long']})
    assert tracker.rejected and not tracker.retryable('failed_recoverable')


@pytest.mark.parametrize('engine', ENGINES)
def test_presentation_shape_errors_do_not_break_the_observer(engine):
    tracker=RejectionTracker(engine)
    if engine == 'codex':
        tracker.observe('turn/completed',{'turn':'bad'})
    elif engine=='pi':
        tracker.observe({'type':'message_update','assistantMessageEvent':'bad'})
        tracker.observe({'type':'message_end','message':'bad'})
    else:
        tracker.observe({'type':'assistant','message':'bad'})
        tracker.observe({'type':'stream_event','event':'bad'})
    assert not tracker.retryable('failed_recoverable')
