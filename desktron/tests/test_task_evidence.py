import copy
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import task_evidence
import task_ledger as ledger
import tasks

SPEC = {'criteria': [{'id': 'report', 'kind': 'artifact', 'summary': '报告可读取'}]}
REPORT = [{'criterion': 'report', 'result': 'passed', 'summary': '报告已检查', 'ref': 'artifacts/report.md'}]


def setup(tmp_path, spec=SPEC, sid='s1'):
    root, data = str(tmp_path / 'ws'), str(tmp_path / 'data')
    os.makedirs(Path(root) / 'domains' / 'alpha', exist_ok=True)
    item, row, error = ledger.apply(data, root, 'alpha',
        {'intent': 'create', 'title': '任务', 'contract': spec}, {'id': sid})
    assert not error
    return root, data, item['t'], row


def update(root, data, tid, **change):
    row = ledger.get(data, 'alpha', tid)
    return ledger.apply(data, root, 'alpha', {'task_id': tid, **change},
        {'id': 's1'}, expected_revision=row['revision'])


def test_missing_evidence_rejects_complete_and_cannot_expand_scope(tmp_path):
    root, data, tid, row = setup(tmp_path)
    assert update(root, data, tid, intent='complete', done='已经完成')[2]
    assert ledger.get(data, 'alpha', tid) == row
    changed = copy.deepcopy(SPEC)
    changed['criteria'].append({'id': 'extra', 'kind': 'acceptance', 'summary': '长期观察'})
    assert update(root, data, tid, intent='progress', contract=changed)[2]
    assert ledger.get(data, 'alpha', tid) == row
    assert ledger.apply(data, root, 'alpha', {'intent':'progress','task_id':tid}, {'id':'s1'})[2]


def test_malformed_contract_does_not_create_task(tmp_path):
    root, data, tid, _ = setup(tmp_path)
    for spec in ({'criteria': []}, {'criteria': [{'id': 'x', 'kind': [], 'summary': 'bad'}]}):
        assert ledger.apply(data, root, 'alpha', {'intent':'create','title':'bad','contract':spec})[2]
    assert len(tasks.read_tasks(tasks.tasks_md_path('alpha', root))) == 1


def test_explicit_clear_and_evidence_review_close_without_model(tmp_path):
    root, data, tid, _ = setup(tmp_path)
    assert not update(root, data, tid, intent='progress', done='报告已交付', next='待检查', evidence=REPORT)[2]
    item, row, error = update(root, data, tid, intent='review')
    assert not error and item['state'] == 'doing'
    assert any('下一步' in x for x in row['review']['missing'])
    assert not update(root, data, tid, intent='progress', next='')[2]
    item, row, error = update(root, data, tid, intent='review')
    assert not error and item['state'] == 'done'
    assert row['review']['outcome'] == 'complete'
    assert update(root, data, tid, intent='review')[1]['revision'] == row['revision']


def test_only_this_turn_and_once_per_evidence_state(tmp_path):
    root, data, tid, row = setup(tmp_path)
    stamp = row['activity']['s1']
    assert ledger.review_turn(data, root, 'alpha', 's1', stamp+1, 1) == []
    assert ledger.review_turn(data, root, 'alpha', 's1', stamp-1, 1, stamp-1) == []
    assert ledger.review_turn(data, root, 'alpha', 'other', 1, 1) == []
    result = ledger.review_turn(data, root, 'alpha', 's1', stamp, 1, stamp)
    assert result[0]['review']['outcome'] == 'pending'
    hint = ledger.consume_review_hint(data, root, 'alpha', 's1')
    assert tid in hint and 'report' in hint
    assert ledger.consume_review_hint(data, root, 'alpha', 's1') == ''
    assert not update(root, data, tid, intent='progress')[2]
    update(root, data, tid, intent='review')
    assert ledger.consume_review_hint(data, root, 'alpha', 's1') == ''
    update(root, data, tid, intent='progress', done='有新结果')
    update(root, data, tid, intent='review')
    assert ledger.consume_review_hint(data, root, 'alpha', 's1')


def test_stale_review_and_user_edits_never_close(tmp_path):
    root, data, tid, row = setup(tmp_path)
    update(root, data, tid, intent='progress', done='报告交付', evidence=REPORT)
    ledger.ui_override(data, 'alpha', tid, state='doing')
    before = ledger.get(data, 'alpha', tid)
    assert ledger.apply(data, root, 'alpha', {'intent':'review','task_id':tid},
        {'id':'s1'}, expected_revision=row['revision'])[2]
    item, after, error = update(root, data, tid, intent='review')
    assert not error and item['state'] == 'doing' and after['source'] == 'user'
    assert any('用户' in x for x in after['review']['missing'])
    assert before['evidence'] == after['evidence']
    update(root, data, tid, intent='progress', done='仅改措辞')
    assert update(root, data, tid, intent='review')[0]['state'] == 'doing'
    update(root, data, tid, intent='progress', evidence=REPORT)
    assert update(root, data, tid, intent='review')[0]['state'] == 'done'


def test_criterion_status_matches_authoritative_completion_rules(tmp_path):
    root, data, tid, _ = setup(tmp_path)
    _, row, error = update(root, data, tid, intent='progress', done='报告已交付', evidence=REPORT)
    assert not error
    assert task_evidence.criterion_status(row) == {'report': 'passed'}
    ledger.ui_override(data, 'alpha', tid, state='doing')
    changed = ledger.get(data, 'alpha', tid)
    assert task_evidence.criterion_status(changed) == {'report': 'pending'}
    _, confirmed, error = update(root, data, tid, intent='progress', evidence=REPORT)
    assert not error
    assert task_evidence.criterion_status(confirmed) == {'report': 'passed'}


def test_failed_evidence_and_stale_hint_never_count_as_completion(tmp_path):
    root, data, tid, _ = setup(tmp_path)
    failure = [{**REPORT[0], 'result': 'failed'}]
    update(root, data, tid, intent='progress', done='报告已检查但有缺陷', evidence=failure)
    _, row, _ = update(root, data, tid, intent='review')
    assert row['review']['missing'] == ['report：验证尚未通过']
    update(root, data, tid, intent='progress', evidence=REPORT)
    assert ledger.consume_review_hint(data, root, 'alpha', 's1') == ''
    assert update(root, data, tid, intent='review')[0]['state'] == 'done'


def test_recap_cannot_supply_or_weaken_structured_evidence(tmp_path):
    root, data, tid, row = setup(tmp_path)
    for change in ({'intent':'complete','done':'done','confidence':'high'},
                   {'intent':'progress','evidence':REPORT}, {'intent':'progress','next':''}):
        assert ledger.apply(data, root, 'alpha', {'task_id':tid, **change}, {'id':'s1'},
            receipt='recap-test', expected_revision=row['revision'])[2]
    assert ledger.get(data, 'alpha', tid) == row


def test_commit_exists_and_new_commit_invalidates_other_evidence(tmp_path):
    repo = tmp_path / 'code'
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git','-C',str(repo),*args], text=True).strip()
    git('init', '-q')
    git('-c','user.name=test','-c','user.email=test@invalid','commit','--allow-empty','-qm','first')
    sha1 = git('rev-parse','HEAD')
    git('-c','user.name=test','-c','user.email=test@invalid','commit','--allow-empty','-qm','second')
    sha2 = git('rev-parse','HEAD')
    spec = {'criteria': [{'id':'code','kind':'commit','summary':'代码提交'},
                         {'id':'tests','kind':'test','summary':'测试通过'},
                         {'id':'deploy','kind':'deploy','summary':'双端部署验收'}]}
    root, data, tid, _ = setup(tmp_path, spec)
    def code(sha):
        return {'criterion':'code','summary':'修复','ref':sha,'repo':str(repo),'commit':sha}
    assert update(root, data, tid, intent='progress', evidence=[code('0'*40)])[2]
    assert not update(root, data, tid, intent='progress', done='修复已提交', evidence=[code(sha1),
        {'criterion':'tests','result':'passed','summary':'测试通过','ref':'/tmp/test.log','subject':sha1}])[2]
    _, row, _ = update(root, data, tid, intent='review')
    assert row['review']['missing'] == ['deploy：缺少双端部署验收']
    update(root, data, tid, intent='progress', evidence=[code(sha2)])
    _, row, _ = update(root, data, tid, intent='review')
    assert any('tests' in x and '提交' in x for x in row['review']['missing'])
    assert row['evidence']['code']['verification'] == 'git_object'
    assert row['evidence']['tests']['verification'] == 'agent_attested'
    # A second session can attach deployment evidence to the same original task.
    all_pass = [{'criterion':c,'result':'passed','summary':'已验证','ref':'/tmp/'+c,'subject':sha2}
                for c in ('tests','deploy')]
    item, row, error = ledger.apply(data, root, 'alpha',
        {'intent':'progress','task_id':tid,'evidence':all_pass}, {'id':'publisher'},
        expected_revision=row['revision'])
    assert not error
    results = ledger.review_turn(data, root, 'alpha', 'publisher', row['activity']['publisher'], 2)
    assert results[0]['state'] == 'done'


def test_cli_roundtrip_files_and_empty_fields(tmp_path, capsys):
    cli = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'bin' / 'desk-task'), run_name='test')
    spec, ev = tmp_path/'contract.json', tmp_path/'evidence.json'
    spec.write_text(json.dumps(SPEC)); ev.write_text(json.dumps(REPORT))
    sent = []
    def post(path, body):
        sent.append(body)
        return {'task':{'t':'t1'},'ledger':{'revision':2}}
    cli['main'].__globals__['_post'] = post
    assert cli['main'](['--domain','alpha','create','--title','试点','--contract-file',str(spec)]) == 0
    assert sent[-1]['change']['contract'] == SPEC
    assert 'next' not in sent[-1]['change']
    assert cli['main'](['--domain','alpha','progress','--task-id','t1','--revision','1',
        '--next','','--blocker','','--evidence-file',str(ev)]) == 0
    assert sent[-1]['change']['next'] == sent[-1]['change']['blocker'] == ''
    assert sent[-1]['change']['evidence'] == REPORT
