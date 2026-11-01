import io
import json
import os
import sys
import threading
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

_argv = sys.argv
sys.argv = ["ui_server.py", "/tmp/frontend", "7999", "7998", "/tmp/workspace"]
try:
    from task import api
finally:
    sys.argv = _argv
import tasks


def test_user_change_and_review_share_transaction(tmp_path):
    import task_ledger
    repo, data = str(tmp_path/'ws'), str(tmp_path/'data')
    item, row, error = task_ledger.apply(data, repo, 'alpha',
        {'intent':'create', 'title':'review race', 'done':'报告完成',
         'contract':{'criteria':[{'id':'report','kind':'artifact','summary':'报告'}]},
         'evidence':[{'criterion':'report','result':'passed','summary':'已检查','ref':'artifact.md'}]}, {'id':'s1'})
    assert not error
    writing, release, reviewed, review_started = (threading.Event() for _ in range(4))
    actual = tasks.update_task
    def held_write(*args, **kwargs):
        writing.set()
        assert release.wait(3)
        return actual(*args, **kwargs)
    result = []
    def review():
        review_started.set()
        result.append(task_ledger.apply(data, repo, 'alpha', {'intent':'review','task_id':item['t']},
            {'id':'s1'}, expected_revision=row['revision']))
        reviewed.set()
    h = Handler(json.dumps({'op':'set','line':item['line'],'expect':item['title'],'state':'doing'}).encode())
    with mock.patch.object(api, 'REPO_ROOT', repo), mock.patch.object(api, 'DATA_HOME', data), \
         mock.patch.object(tasks, 'update_task', side_effect=held_write):
        writer = threading.Thread(target=lambda: api.handle(h, '/workbench/domains/alpha/tasks', '', 'POST'))
        writer.start()
        try:
            assert writing.wait(3)
            reader = threading.Thread(target=review)
            reader.start()
            assert review_started.wait(3)
            assert not reviewed.wait(0.05)
        finally:
            release.set()
            writer.join(3)
        reader.join(3)
    assert h.code == 200
    assert result[0][2] and '已被其他操作更新' in result[0][2]
    assert task_ledger.get(data, 'alpha', item['t'])['source'] == 'user'
    assert tasks.read_tasks(tasks.tasks_md_path('alpha', repo))[0]['state'] == 'doing'


def test_failed_user_fence_leaves_task_file_unchanged(tmp_path):
    repo, data = str(tmp_path/'ws'), str(tmp_path/'data')
    path = tasks.tasks_md_path('alpha', repo)
    tasks.add_task(path, 'task')
    tasks.ensure_ids(path)
    item = tasks.read_tasks(path)[0]
    h = Handler(json.dumps({'op':'set','line':item['line'],'expect':'task','state':'done'}).encode())
    with mock.patch.object(api, 'REPO_ROOT', repo), mock.patch.object(api, 'DATA_HOME', data), \
         mock.patch.object(api.task_ledger, 'ui_override', side_effect=OSError('fixture')):
        api.handle(h, '/workbench/domains/alpha/tasks', '', 'POST')
    assert h.code == 500 and '任务未修改' in h.result['error']
    assert tasks.read_tasks(path)[0]['state'] != 'done'


class Handler:
    def __init__(self, body=b""):
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)
        self.result = None
        self.code = None

    def _send_json(self, value, code=200):
        self.result, self.code = value, code
        return value


def test_get_enriches_task_with_domain_and_ledger(tmp_path):
    repo, data = str(tmp_path / "ws"), str(tmp_path / "data")
    path = tasks.tasks_md_path("alpha", repo)
    tasks.add_task(path, "任务甲")
    with mock.patch.object(api, "REPO_ROOT", repo), mock.patch.object(api, "DATA_HOME", data):
        h = Handler()
        api.handle(h, "/workbench/domains/alpha/tasks", "", "GET")
        assert h.code == 200
        assert h.result["items"][0]["domain"] == "alpha"


def test_ledger_operation_uses_current_session_payload(tmp_path):
    repo, data = str(tmp_path / "ws"), str(tmp_path / "data")
    body = json.dumps({"op": "ledger", "change": {"intent": "create", "title": "任务甲",
                       "next": "继续实现"}, "session": {"id": "s1", "engine": "pi"},
                       "receipt": "pi/s1/1"}).encode()
    with mock.patch.object(api, "REPO_ROOT", repo), mock.patch.object(api, "DATA_HOME", data):
        h = Handler(body)
        api.handle(h, "/workbench/domains/alpha/tasks", "", "POST")
        assert h.code == 200 and h.result["ok"]
        assert h.result["task"]["state"] == "doing"
        assert h.result["ledger"]["sessions"][0]["id"] == "s1"
        h = Handler()
        api.handle(h, "/workbench/domains/alpha/tasks", "", "GET")
        assert h.result["items"][0]["ledger"]["next"] == "继续实现"
        assert "auto_summary" not in h.result["items"][0]


def test_get_exposes_bounded_contract_and_evidence_for_task_detail(tmp_path):
    repo, data = str(tmp_path / "ws"), str(tmp_path / "data")
    change = {"intent": "create", "title": "任务甲",
              "contract": {"criteria": [{"id": "accept", "kind": "acceptance", "summary": "用户验收"}]},
              "evidence": [{"criterion": "accept", "result": "passed", "summary": "已验收", "ref": "manual"}]}
    body = json.dumps({"op": "ledger", "change": change}).encode()
    with mock.patch.object(api, "REPO_ROOT", repo), mock.patch.object(api, "DATA_HOME", data):
        h = Handler(body); api.handle(h, "/workbench/domains/alpha/tasks", "", "POST")
        assert h.code == 200
        h = Handler(); api.handle(h, "/workbench/domains/alpha/tasks", "", "GET")
        ledger = h.result["items"][0]["ledger"]
        assert ledger["contract"]["criteria"][0]["summary"] == "用户验收"
        assert ledger["evidence"]["accept"]["result"] == "passed"
        assert ledger["criterion_status"] == {"accept": "passed"}


def test_revision_conflict_is_409(tmp_path):
    repo, data = str(tmp_path / "ws"), str(tmp_path / "data")
    create = {"op": "ledger", "change": {"intent": "create", "title": "任务甲"}}
    with mock.patch.object(api, "REPO_ROOT", repo), mock.patch.object(api, "DATA_HOME", data):
        h = Handler(json.dumps(create).encode()); api.handle(h, "/workbench/domains/alpha/tasks", "", "POST")
        tid = h.result["task"]["t"]
        stale = {"op": "ledger", "change": {"intent": "progress", "task_id": tid}, "revision": 0}
        h = Handler(json.dumps(stale).encode()); api.handle(h, "/workbench/domains/alpha/tasks", "", "POST")
        assert h.code == 409
        assert "已被其他操作更新" in h.result["error"]
