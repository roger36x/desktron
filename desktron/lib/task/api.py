"""任务领域路由:工作区级 TASKS.md 行级增改删、任务备忘(tasks.py 之上的 HTTP 面)。
拆分自 ui_server._workbench;owns() 按路径认领,
handle(h) 内 h 为 Handler 实例,行为与原 if 链一致。"""
import os, json, urllib.parse
from deskcfg import REPO_ROOT, DATA_HOME
import tasks
import task_ledger
import durable_json


def owns(p, method):
    return p.startswith("/workbench/domains/") and p.endswith("/tasks") and method in ("GET", "POST")


def handle(h, p, q, method):
    if p.startswith("/workbench/domains/") and p.endswith("/tasks") and method in ("GET", "POST"):
        # 工作区级任务清单(domains/<id>/TASKS.md):GET 返回 {done,doing,total,items};
        # POST 行级增改删 {op:add|set|del, title?, line?, expect?, state?}。面板角标、清单 UI、
        # 容器子区进度共用;agent 侧不走这里(直接 Edit cwd 的 TASKS.md,同一份文件)。
        # id 支持「容器:子区」形态(自定义子区的清单落 domains/<容器>/<子区>/)。
        did = urllib.parse.unquote(p.split("/")[3])
        path = tasks.tasks_md_path(did, REPO_ROOT)
        if not path:
            return h._send_json({"error": "bad id"}, 400)
        if method == "GET":
            # 存量任务多数没有 t: 注记,读的时候补齐一次。id 是这套界面里唯一稳定的把手——
            # 行号会随增删漂移,标题会改,只有它拿出去还指着同一条。补齐是幂等的,补完不再写。
            try:
                tasks.ensure_ids(path)
            except Exception:
                pass   # 清单不可写(只读挂载之类)不该让读也失败,退回没有 id 的那一档
            return h._send_json(task_ledger.enrich(DATA_HOME, did, tasks.tasks_summary(path)))
        length = int(h.headers.get("Content-Length", 0))
        try:
            body = json.loads(h.rfile.read(length) or b"{}")
            assert isinstance(body, dict)
        except Exception:
            return h._send_json({"error": "invalid json"}, 400)
        op = body.get("op")
        state = body.get("state")
        if state is not None and state not in ("todo", "doing", "done"):
            return h._send_json({"error": f"state 非法:{state}"}, 422)
        if op == "memo-set":
            # 工作流页就地编辑备忘:无绑定先建篇,再整体写回正文(首行=任务标题由前端组装)
            import notepad_store
            try:
                line = int(body.get("line"))
            except Exception:
                return h._send_json({"error": "需要 line(行号)"}, 400)
            annot, err = tasks.task_line_annot(path, line, body.get("expect"))
            if err:
                return h._send_json({"error": err}, 409)
            nid = annot.get("note", "")
            try:
                if not nid or not notepad_store.get_note(REPO_ROOT, nid):
                    nid = notepad_store.ensure_task_note(REPO_ROOT, body.get("wfName") or did, body.get("expect"))
                    err = tasks.task_annot_set(path, line, body.get("expect"), "note", nid)
                    if err:
                        return h._send_json({"error": err}, 409)
                err = notepad_store.set_note_content(REPO_ROOT, nid, str(body.get("text") or ""))
                if err:
                    return h._send_json({"error": err}, 422)
            except Exception as e:
                return h._send_json({"error": f"备忘录存储失败:{str(e)[:160]}"}, 500)
            return h._send_json({"ok": True, "note": nid})
        if op in ("memo-open", "memo-get"):
            # Desk 备忘录应用(notepad,domains/<域>/notepad.json):任务备忘 = 「任务」文件夹一篇,
            # note: 注记绑定。memo-get 取正文文本做页内预览;memo-open 无绑定(或备忘被删)则建篇,
            # 返回备忘 id 由前端打开 notepad 页并 #note= 定位。
            import notepad_store
            try:
                line = int(body.get("line"))
            except Exception:
                return h._send_json({"error": "需要 line(行号)"}, 400)
            annot, err = tasks.task_line_annot(path, line, body.get("expect"))
            if err:
                return h._send_json({"error": err}, 409)
            nid = annot.get("note", "")
            try:
                if op == "memo-get":
                    n = notepad_store.get_note(REPO_ROOT, nid)
                    return h._send_json({"ok": True, "note": nid, "text": (n or {}).get("content", "")})
                if not nid or not notepad_store.get_note(REPO_ROOT, nid):
                    nid = notepad_store.ensure_task_note(REPO_ROOT, body.get("wfName") or did, body.get("expect"))
                    err = tasks.task_annot_set(path, line, body.get("expect"), "note", nid)
                    if err:
                        return h._send_json({"error": err}, 409)
            except Exception as e:
                return h._send_json({"error": f"备忘录存储失败:{str(e)[:160]}"}, 500)
            return h._send_json({"ok": True, "note": nid})
        if op in ("note-get", "note-set", "note-asset"):
            # 任务备忘(tasknotes/<tid>.md):get 只读(无 id 不落盘);set/asset 首写时补打 t: id。
            try:
                line = int(body.get("line"))
            except Exception:
                return h._send_json({"error": "需要 line(行号)"}, 400)
            tid, err = tasks.task_ensure_id(path, line, body.get("expect"), create=(op != "note-get"))
            if err:
                return h._send_json({"error": err}, 409)
            if op == "note-get":
                return h._send_json({"ok": True, "tid": tid,
                                        "text": tasks.task_note_read(path, tid) if tid else ""})
            if op == "note-set":
                err = tasks.task_note_write(path, tid, str(body.get("text") or ""))
                if err:
                    return h._send_json({"error": err}, 422)
                return h._send_json({"ok": True, "tid": tid})
            import base64 as _b64n
            try:
                data = _b64n.b64decode(body.get("data_b64") or "")
                assert data
            except Exception:
                return h._send_json({"error": "data_b64 缺失或非法"}, 422)
            if len(data) > 5 * 1024 * 1024:
                return h._send_json({"error": "文件超过 5MB"}, 422)
            rel, err = tasks.task_note_asset(path, tid, body.get("name"), data)
            if err:
                return h._send_json({"error": err}, 422)
            wsrel = os.path.relpath(os.path.join(os.path.dirname(path), "tasknotes", rel.replace("/", os.sep)), REPO_ROOT)
            return h._send_json({"ok": True, "tid": tid, "rel": rel,
                                    "src": "/workbench/view?raw=1&path=" + urllib.parse.quote(wsrel.replace(os.sep, "/"))})
        if op == "ledger":
            change = body.get("change") if isinstance(body.get("change"), dict) else {}
            item, ledger, err = task_ledger.apply(
                DATA_HOME, REPO_ROOT, did, change,
                session=body.get("session"), receipt=body.get("receipt"),
                expected_revision=body.get("revision"))
            if err:
                return h._send_json({"error": err, "ledger": ledger}, 409)
            return h._send_json({"ok": True, "task": item, "ledger": ledger})
        if op == "add":
            err = tasks.add_task(path, body.get("title"))
        elif op == "annot":
            # 行尾白名单注记的通用入口(当前放行 note: 与 due:)。备忘那几个 op 各自封了一套
            # 「找不到就建篇再绑」的流程,与这里不是一回事;这里只管把一个值写上去或清掉。
            try:
                line = int(body.get("line"))
            except Exception:
                return h._send_json({"error": "需要 line(行号)"}, 400)
            err = tasks.task_annot_set(path, line, body.get("expect"),
                                       str(body.get("key") or ""), body.get("value"))
        elif op == "task-id":
            # 任务稳定 id(t: 注记,缺则生成):节点关联要指向一条任务,行号会漂、标题会改,只有它稳
            try:
                line = int(body.get("line"))
            except Exception:
                return h._send_json({"error": "需要 line(行号)"}, 400)
            tid, err = tasks.task_ensure_id(path, line, body.get("expect"))
            if err:
                return h._send_json({"error": err}, 409)
            return h._send_json({"ok": True, "id": tid})
        elif op in ("set", "del"):
            try:
                line = int(body.get("line"))
            except Exception:
                return h._send_json({"error": "需要 line(行号)"}, 400)
            # Human state and its revision must commit before an automatic reviewer
            # can observe either file. Share the ledger's cross-process lock.
            with durable_json.locked(task_ledger.path_of(DATA_HOME) + '.transaction'):
                tid, tid_err = tasks.task_ensure_id(path, line, body.get("expect"))
                if tid_err:
                    return h._send_json({"error": tid_err}, 409)
                if op == 'set':
                    try:
                        # Fence human intent first. If the ledger is unwritable,
                        # leave TASKS.md unchanged rather than let old evidence close it.
                        task_ledger.ui_override(DATA_HOME, did, tid, state=state or '')
                    except Exception as exc:
                        return h._send_json({'error': '账本写入失败，任务未修改：' + str(exc)[:120]}, 500)
                err = tasks.update_task(path, line, body.get("expect"),
                                        state=state, title=body.get("title"),
                                        delete=(op == "del"),
                                        flow=body.get("flow"), fcur=body.get("fcur"),
                                        fdone=body.get("fdone"), nl=body.get("nl"))
                if not err and op == 'del':
                    try:
                        task_ledger.ui_override(DATA_HOME, did, tid, state=state or "", deleted=(op == "del"))
                    except Exception as exc:
                        return h._send_json({'error': '任务已修改，但账本写入失败，请重试：' + str(exc)[:120]}, 500)
        else:
            return h._send_json({"error": "未知 op(add/set/del/annot/task-id)"}, 400)
        if err:
            return h._send_json({"error": err}, 409)   # 冲突/校验失败:前端重拉全量
        return h._send_json({"ok": True, **task_ledger.enrich(
            DATA_HOME, did, tasks.tasks_summary(path))})
    return h._send_json({"error": "unknown"}, 404)
