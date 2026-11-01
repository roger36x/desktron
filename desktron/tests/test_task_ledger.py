import json
import os
import sys
import runpy
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import task_ledger
import tasks


def test_desk_task_all_includes_completed_for_duplicate_check(capsys):
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'bin' / 'desk-task'), run_name='test_desk_task')
    module['main'].__globals__['_get'] = lambda _: {'items': [
        {'t':'a','state':'done','title':'已完成事项'}, {'t':'b','state':'doing','title':'未完成事项'}]}
    assert module['main'](['--domain','alpha','list']) == 0
    text = capsys.readouterr().out
    assert '已完成事项' not in text and '未完成事项' in text
    assert module['main'](['--domain','alpha','list','--all']) == 0
    text = capsys.readouterr().out
    assert '已完成事项' in text and '未完成事项' in text


def _repo(tmp_path):
    root = str(tmp_path / "workspace")
    os.makedirs(os.path.join(root, "domains", "alpha"))
    os.makedirs(os.path.join(root, "domains", "beta"))
    return root, str(tmp_path / "data")


def test_recap_cannot_create_or_claim_or_use_title(tmp_path):
    root, data = _repo(tmp_path)
    for intent in ('create', 'claim', 'progress'):
        _, _, err = task_ledger.apply(data, root, 'alpha',
            {'intent': intent, 'title': '换一种说法的新任务'}, {'id': 's1'}, 'recap-'+intent)
        assert err
    assert tasks.read_tasks(tasks.tasks_md_path('alpha', root)) == []


def test_retried_receipt_with_new_title_cannot_create_or_resurrect(tmp_path):
    root, data = _repo(tmp_path)
    item, _, err = task_ledger.apply(data, root, 'alpha',
        {'intent':'create','title':'原任务'}, receipt='stable-receipt')
    assert not err
    again, _, err = task_ledger.apply(data, root, 'alpha',
        {'intent':'create','title':'改写标题'}, receipt='stable-receipt')
    assert not err and again['t'] == item['t']
    assert len(tasks.read_tasks(tasks.tasks_md_path('alpha', root))) == 1
    assert not tasks.update_task(tasks.tasks_md_path('alpha', root), item['line'], item['title'], delete=True)
    assert task_ledger.apply(data, root, 'alpha',
        {'intent':'create','title':'再次改写'}, receipt='stable-receipt')[2]
    assert not tasks.read_tasks(tasks.tasks_md_path('alpha', root))


def test_completed_title_and_stale_id_never_create_new_task(tmp_path):
    root, data = _repo(tmp_path)
    item, _, _ = task_ledger.apply(data, root, 'alpha', {'intent':'create','title':'修复布局'})
    task_ledger.apply(data, root, 'alpha', {'intent':'complete','task_id':item['t']})
    for change in ({'intent':'create','title':'修复布局'},
                   {'intent':'create','task_id':'missing','title':'另一标题'},
                   {'intent':'progress','task_id':item['t'],'next':'又开始'}):
        assert task_ledger.apply(data, root, 'alpha', change)[2]
    rows = tasks.read_tasks(tasks.tasks_md_path('alpha', root))
    assert len(rows) == 1 and rows[0]['state'] == 'done'


def test_recap_only_updates_linked_task_and_requires_completion_evidence(tmp_path):
    root, data = _repo(tmp_path)
    item, _, _ = task_ledger.apply(data, root, 'alpha', {'intent':'create','title':'目标'}, {'id':'owner'})
    change = {'intent':'progress','task_id':item['t'],'next':'验证'}
    assert task_ledger.apply(data, root, 'alpha', change, {'id':'other'}, 'recap-other')[2]
    assert not task_ledger.apply(data, root, 'alpha', change, {'id':'owner'}, 'recap-owner')[2]
    change.update(intent='complete', confidence='high', done='源码已修复')
    assert task_ledger.apply(data, root, 'alpha', change, {'id':'owner'}, 'recap-done')[2]
    change.pop('next')
    assert not task_ledger.apply(data, root, 'alpha', change, {'id':'owner'}, 'recap-done-ok')[2]


def test_ledger_state_does_not_inject_machine_prose_into_task_memo_view(tmp_path):
    root, data = _repo(tmp_path)
    item, row, _ = task_ledger.apply(data, root, 'alpha',
        {'intent':'create','title':'目标','done':'已修复。更多流水。','next':'部署后验收。其他。'}, {'id':'s'})
    result = task_ledger.enrich(data, 'alpha', {'items':[item]})['items'][0]
    assert result['ledger']['done'] == row['done']
    assert result['ledger']['next'] == row['next']
    assert 'auto_summary' not in result
    assert not result.get('note')


def test_long_progress_never_erases_next_or_blocker():
    row = {'done': '进展'*150, 'next': '待验收'*100, 'blocker':'等授权'*100}
    task_ledger._bound_state(row)
    assert all(row.get(k) for k in task_ledger.STATE_FIELDS)
    assert sum(len(row[k]) for k in task_ledger.STATE_FIELDS) <= task_ledger.STATE_TOTAL_LIMIT


def test_create_claim_progress_and_complete_are_bounded(tmp_path):
    root, data = _repo(tmp_path)
    item, row, err = task_ledger.apply(
        data, root, "alpha", {"intent": "create", "title": "实现任务账本",
                              "done": "已完成 " * 100, "next": "补测试"},
        {"id": "s1", "engine": "pi", "title": "任务设计"}, "pi/s1/1")
    assert not err and item["state"] == "doing"
    assert len(row["done"]) <= task_ledger.FIELD_LIMIT
    assert row["sessions"][0]["role"] == "primary"
    assert row["revision"] == 1

    item, row2, err = task_ledger.apply(
        data, root, "alpha", {"intent": "complete", "task_id": item["t"], "done": "测试通过"},
        {"id": "s1", "engine": "pi"}, "pi/s1/2", expected_revision=1)
    assert not err and item["state"] == "done"
    assert row2["close_reason"] == "completed"
    assert "next" not in row2


def test_receipt_is_idempotent_and_revision_conflicts(tmp_path):
    root, data = _repo(tmp_path)
    first, row, err = task_ledger.apply(data, root, "alpha",
        {"intent": "create", "title": "同一事件"}, {"id": "s1"}, "r1")
    assert not err
    _, same, err = task_ledger.apply(data, root, "alpha",
        {"intent": "progress", "task_id": first["t"], "next": "不应覆盖"}, {"id": "s1"}, "r1")
    assert not err and same["revision"] == row["revision"] and "next" not in same
    _, current, err = task_ledger.apply(data, root, "alpha",
        {"intent": "progress", "task_id": first["t"], "next": "继续"}, {"id": "s1"}, "r2",
        expected_revision=0)
    assert "已被其他操作更新" in err and current["revision"] == 1


def test_session_links_are_many_to_many_and_bounded(tmp_path):
    root, data = _repo(tmp_path)
    item, _, _ = task_ledger.apply(data, root, "alpha", {"intent": "create", "title": "并行任务"},
                                    {"id": "primary"}, "r0")
    for i in range(8):
        _, row, err = task_ledger.apply(data, root, "alpha",
            {"intent": "progress", "task_id": item["t"], "done": f"贡献 {i}"},
            {"id": f"s{i}", "engine": "codex"}, f"r{i+1}")
        assert not err
    assert len(row["sessions"]) == task_ledger.SESSION_LIMIT
    assert task_ledger.linked_to_session(data, "s7", "alpha")[0]["task_id"] == item["t"]


def test_aggregate_keeps_domain_and_ledger(tmp_path):
    root, data = _repo(tmp_path)
    item, _, _ = task_ledger.apply(data, root, "alpha", {"intent": "create", "title": "甲任务",
                                                         "next": "下一步"}, {"id": "s1"}, "r1")
    tasks.add_task(tasks.tasks_md_path("beta", root), "乙任务")
    out = task_ledger.aggregate(data, ["alpha", "beta"], root)
    assert out["total"] == 2
    by_title = {x["title"]: x for x in out["items"]}
    assert by_title["甲任务"]["domain"] == "alpha"
    assert by_title["甲任务"]["ledger"]["next"] == "下一步"
    assert by_title["乙任务"]["domain"] == "beta"


def test_user_override_beats_recap_supplement(tmp_path):
    root, data = _repo(tmp_path)
    item, row, err = task_ledger.apply(data, root, "alpha",
        {"intent": "create", "title": "用户优先"}, {"id": "s1"}, "agent-1")
    assert not err
    path = tasks.tasks_md_path("alpha", root)
    current = next(x for x in tasks.read_tasks(path) if x["t"] == item["t"])
    assert tasks.update_task(path, current["line"], current["title"], state="done") == ""
    task_ledger.ui_override(data, "alpha", item["t"], state="done")
    _, after, err = task_ledger.apply(data, root, "alpha",
        {"intent": "progress", "task_id": item["t"], "next": "不应复活"},
        {"id": "s1"}, "recap-s1-2")
    assert not err and after["source"] == "user" and "next" not in after
    assert next(x for x in tasks.read_tasks(path) if x["t"] == item["t"])["state"] == "done"


def test_ui_override_invalidates_old_recap_but_not_fresh_completion(tmp_path):
    root, data = _repo(tmp_path)
    item, row, err = task_ledger.apply(data, root, "alpha",
        {"intent": "create", "title": "完成后自动关闭"}, {"id": "s1"}, "agent-1")
    assert not err
    old_revision = row["revision"]
    task_ledger.ui_override(data, "alpha", item["t"], state="doing")
    current = task_ledger.get(data, "alpha", item["t"])
    assert current["source"] == "user" and current["revision"] == old_revision + 1
    _, _, err = task_ledger.apply(data, root, "alpha",
        {"intent": "progress", "task_id": item["t"], "next": "机器不得覆盖用户状态"},
        {"id": "s1"}, "recap-fresh-progress", expected_revision=current["revision"])
    assert "仅可凭新的明确完成证据" in err

    complete = {"intent": "complete", "task_id": item["t"],
                "done": "交付与验收均已完成", "confidence": "high"}
    _, _, err = task_ledger.apply(data, root, "alpha", complete, {"id": "s1"},
                                  "recap-stale", expected_revision=old_revision)
    assert "已被其他操作更新" in err
    assert next(x for x in tasks.read_tasks(tasks.tasks_md_path("alpha", root))
                if x["t"] == item["t"])["state"] == "doing"

    closed, after, err = task_ledger.apply(data, root, "alpha", complete, {"id": "s1"},
                                           "recap-fresh", expected_revision=current["revision"])
    assert not err and closed["state"] == "done"
    assert after["source"] == "agent" and after["close_reason"] == "completed"


def test_task_snapshot_stays_under_one_kib_after_one_hundred_updates(tmp_path):
    root, data = _repo(tmp_path)
    item, row, err = task_ledger.apply(data, root, "alpha",
        {"intent": "create", "title": "长期任务", "done": "完" * 160,
         "next": "下" * 160, "blocker": "阻" * 160},
        {"id": "s0", "engine": "codex", "title": "会话" * 60}, "r0")
    assert not err
    for i in range(100):
        item, row, err = task_ledger.apply(data, root, "alpha",
            {"intent": "progress", "task_id": item["t"], "done": "完" * 160,
             "next": "下" * 160, "blocker": "阻" * 160},
            {"id": f"s{i}", "engine": "codex", "title": "会话" * 60}, f"r{i + 1}")
        assert not err
    assert len(json.dumps(row, ensure_ascii=False).encode()) < 1024
    assert len(row["sessions"]) == task_ledger.SESSION_LIMIT


def test_memory_candidates_are_unconfirmed_and_deduplicated(tmp_path):
    root, data = _repo(tmp_path)
    candidate = {"kind": "pinned_candidate", "statement": "以后不要自动重启", "reason": "用户纠正"}
    task_ledger.store_memory_candidates(data, [candidate], "r1", "alpha", {"id": "s1"})
    task_ledger.store_memory_candidates(data, [candidate], "r2", "alpha", {"id": "s1"})
    doc = json.load(open(task_ledger.path_of(data), encoding="utf-8"))
    rows = list(doc["memory_candidates"].values())
    assert len(rows) == 1
    assert rows[0]["confirmed"] is False
    assert rows[0]["receipts"] == ["r1", "r2"]
