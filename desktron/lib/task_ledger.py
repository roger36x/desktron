"""AI task ledger: bounded current state and task↔session links.

TASKS.md remains the human-readable task source. This private sidecar stores only
current execution state; history belongs to recap/journal. All writers share
``durable_json`` so CLI processes and the Desk server cannot silently overwrite
one another.
"""
import datetime as _dt
import os
import re
import time

import durable_json
import tasks
import task_evidence

STATE_FIELDS = ("done", "next", "blocker")
FIELD_LIMIT = 160
STATE_TOTAL_LIMIT = 120
SESSION_LIMIT = 5
RECEIPT_LIMIT = 512
VALID_INTENTS = {"claim", "progress", "block", "complete", "release", "create", "review"}


def path_of(data_home):
    return os.path.join(str(data_home), "task-ledger.json")


def _now():
    return _dt.datetime.now().astimezone().isoformat(timespec="minutes")


def _clip(value, limit=FIELD_LIMIT):
    return " ".join(str(value or "").split())[:limit]


def _key(domain, tid):
    return f"{domain}:{tid}"


def _clean_session(session):
    src = session if isinstance(session, dict) else {}
    limits = {"id": 80, "engine": 24, "title": 48}
    return {k: _clip(src.get(k), limits[k]) for k in limits if src.get(k)}


def _bound_state(row):
    """Keep the whole current-state prose bounded; individual fields still cap at 160."""
    room = STATE_TOTAL_LIMIT
    for i, field in enumerate(STATE_FIELDS):
        if field not in row:
            continue
        reserved = sum(min(40, len(_clip(row.get(other)))) for other in STATE_FIELDS[i + 1:])
        value = _clip(row.get(field), min(FIELD_LIMIT, max(0, room - reserved)))
        if value:
            row[field] = value
            room -= len(value)
        else:
            row.pop(field, None)


def read(data_home):
    doc = durable_json.read(path_of(data_home))
    if not isinstance(doc.get("tasks"), dict):
        doc["tasks"] = {}
    if not isinstance(doc.get("receipts"), dict):
        doc["receipts"] = {}
    doc.setdefault("version", 1)
    return doc


def get(data_home, domain, tid):
    try:
        row = read(data_home).get("tasks", {}).get(_key(domain, tid), {})
        return dict(row) if isinstance(row, dict) else {}
    except Exception:
        return {}


def enrich(data_home, domain, summary):
    """Attach bounded state to a tasks_summary response without changing TASKS.md."""
    try:
        rows = read(data_home).get("tasks", {})
    except Exception:
        rows = {}
    out = dict(summary or {})
    items = []
    for item in out.get("items") or []:
        one = dict(item)
        state = rows.get(_key(domain, one.get("t", "")), {}) if one.get("t") else {}
        if isinstance(state, dict) and state:
            projected = dict(state)
            if projected.get("contract"):
                projected["criterion_status"] = task_evidence.criterion_status(projected)
            one["ledger"] = projected
        one["domain"] = domain
        items.append(one)
    out["items"] = items
    return out


def aggregate(data_home, domains, repo_root):
    items = []
    for domain in domains:
        path = tasks.tasks_md_path(domain, repo_root)
        if not path:
            continue
        try:
            tasks.ensure_ids(path)
            one = enrich(data_home, domain, tasks.tasks_summary(path))
        except Exception:
            continue
        items.extend(one.get("items") or [])
    open_items = [x for x in items if x.get("state") != "done"]
    return {"done": len(items) - len(open_items),
            "doing": sum(1 for x in items if x.get("state") == "doing"),
            "total": len(items), "items": items}


def _task_by_id(path, tid):
    return next((x for x in tasks.read_tasks(path) if x.get("t") == tid), None)


def _task_by_title(path, title):
    title = " ".join(str(title or "").split())
    matches = [x for x in tasks.read_tasks(path) if x.get("title") == title]
    return matches[0] if len(matches) == 1 else None


def resolve_task(repo_root, domain, tid="", title="", create=False):
    path = tasks.tasks_md_path(domain, repo_root)
    if not path:
        return None, "工作流 id 非法"
    tasks.ensure_ids(path)
    item = _task_by_id(path, str(tid or "")) if tid else _task_by_title(path, title)
    if item:
        return item, ""
    if tid:
        return None, "任务 id 不存在，请重读清单；不得以新建替代更新"
    if title and any(x.get("title") == " ".join(str(title).split()) for x in tasks.read_tasks(path)):
        return None, "同名任务不唯一，请使用稳定任务 id"
    if not create:
        return None, "找不到任务"
    title = " ".join(str(title or "").split())
    if not title:
        return None, "标题为空"
    err = tasks.add_task(path, title)
    if err:
        return None, err
    tasks.ensure_ids(path)
    item = list(reversed([x for x in tasks.read_tasks(path) if x.get("title") == title]))[0]
    return item, ""


def apply(data_home, repo_root, domain, change, session=None, receipt="", expected_revision=None):
    """Apply one semantic change under a cross-process transaction lock."""
    with durable_json.locked(path_of(data_home) + ".transaction"):
        return _apply_unlocked(data_home, repo_root, domain, change, session, receipt,
                               expected_revision)


def _apply_unlocked(data_home, repo_root, domain, change, session=None, receipt="", expected_revision=None):
    """Apply one semantic change. Returns ``(task, ledger_state, error)``."""
    change = dict(change or {})
    intent = str(change.get("intent") or "progress")
    if intent not in VALID_INTENTS:
        return None, {}, "intent 非法"
    if intent == 'review' and any(k in change for k in ('contract', 'evidence', *STATE_FIELDS)):
        return None, {}, 'review 只核对已有证据；请先 progress 登记结果'
    try:
        spec = task_evidence.contract(change['contract']) if 'contract' in change else None
        # Validate before create so a malformed request cannot leave an orphan task.
        if intent == 'create' and 'evidence' in change:
            if not spec:
                raise ValueError('先声明完成条件再登记证据')
            task_evidence.evidence(change['evidence'], spec, {})
    except (ValueError, TypeError) as exc:
        return None, {}, str(exc)
    receipt = _clip(receipt, 180)
    # Deduplicate before resolving/creating: a retry may contain a paraphrased title.
    doc = read(data_home)
    if receipt and receipt in doc["receipts"]:
        old_key = doc["receipts"][receipt].get("task", "")
        old_row = dict(doc["tasks"].get(old_key) or {})
        if old_row.get("workflow") != domain:
            return None, {}, "回执对应的任务已删除或属于其他工作流，请重读清单"
        item = _task_by_id(tasks.tasks_md_path(domain, repo_root), old_row.get("task_id"))
        if not item:
            return None, old_row, "回执对应的任务已删除，不得重复创建"
        return item, old_row, ""
    if receipt.startswith("recap-"):
        if 'contract' in change or 'evidence' in change or intent == 'review':
            return None, {}, "recap 不能定义完成条件或登记交付证据"
        if intent in ("create", "claim") or not change.get("task_id"):
            return None, {}, "recap 只能按稳定 id 补充已有任务；新增和认领由主会话显式处理"
    item, err = resolve_task(repo_root, domain, change.get("task_id"), change.get("title"),
                             create=(intent == "create"))
    if err:
        return None, {}, err
    tid = item.get("t") or ""
    if not tid:
        return None, {}, "任务缺少稳定 id"
    key = _key(domain, tid)
    row = dict(doc["tasks"].get(key) or {})
    revision = int(row.get("revision") or 0)
    if receipt.startswith('recap-') and row.get('contract'):
        return None, row, '契约任务由确定性证据核对维护；recap 仅记录日志'
    # UI corrections invalidate any recap snapshot that predates them through revision matching.
    # Keep the user's current state authoritative for ordinary supplements, but do not permanently
    # blacklist a still-open task: a later completed turn with strict evidence may close it.
    if receipt.startswith("recap-") and row.get("source") == "user" and item.get("state") == "done":
        return item, row, ""  # user-completed tasks are an idempotent no-op, never revived
    if item.get('state') == 'done' and intent == 'review':
        return item, row, ''
    if item.get("state") == "done" and intent not in ("complete", "release"):
        return None, row, "任务已完成；如需重开，请由用户明确恢复为待办"
    if receipt.startswith("recap-") and row.get("source") == "user" and intent != "complete":
        return None, row, "任务状态由用户调整；recap 仅可凭新的明确完成证据自动关闭"
    if receipt.startswith("recap-"):
        sid = _clean_session(session).get("id")
        if not sid or not any(x.get("id") == sid for x in row.get("sessions") or []):
            return None, row, "recap 只能补充本会话已关联的任务"
        if intent == "complete" and (change.get("confidence") != "high" or not change.get("done")
                                     or change.get("next") or change.get("blocker")):
            return None, row, "recap 完成任务需要明确结果，且没有未完成步骤或阻碍"
    if expected_revision is not None and int(expected_revision) != revision:
        return None, row, "任务状态已被其他操作更新，请重读后重试"

    if (row.get('contract') or spec) and intent != 'create' and expected_revision is None:
        return None, row, '契约任务更新需要 revision；请先重读任务'
    try:
        if spec:
            if row.get('contract') and row['contract'] != spec:
                raise ValueError('完成条件已固定；不得随进展扩大或缩减验收范围')
            row['contract'] = spec
        if 'evidence' in change:
            if not row.get('contract'):
                raise ValueError('先声明完成条件再登记证据')
            row['evidence'] = task_evidence.evidence(change['evidence'], row['contract'], row.get('evidence'))
            confirmed = {e['criterion'] for e in change['evidence']}
            pending = [cid for cid in row.get('user_review_required') or [] if cid not in confirmed]
            if pending:
                row['user_review_required'] = pending
            else:
                row.pop('user_review_required', None)
    except (ValueError, TypeError) as exc:
        return None, dict(doc['tasks'].get(key) or {}), str(exc)

    sess = _clean_session(session)
    sessions = [dict(x) for x in row.get("sessions") or [] if isinstance(x, dict) and x.get("id")]
    sid = sess.get("id")
    if sid and intent != 'review':
        sessions = [x for x in sessions if x.get("id") != sid]
        sess.update({"role": "primary" if intent in ("claim", "create") else
                             str(change.get("role") or row.get("session_role") or "contributor"),
                     "updated": _now()})
        sessions.insert(0, sess)
    if intent == "release" and sid:
        sessions = [x for x in sessions if x.get("id") != sid]
    if intent in ("claim", "create") and sid:
        for x in sessions:
            x["role"] = "primary" if x.get("id") == sid else "contributor"
    # Only the latest link needs display metadata; older links remain navigable by id.
    sessions = [x if i == 0 else {k: x[k] for k in ("id", "engine") if x.get(k)}
                for i, x in enumerate(sessions)]

    for field in STATE_FIELDS:
        if field in change:
            value = _clip(change.get(field))
            if value:
                row[field] = value
            else:
                row.pop(field, None)
    _bound_state(row)
    if intent == "block" and not row.get("blocker"):
        row["blocker"] = "等待外部条件"
    reviewing = intent == 'review'
    if reviewing:
        if not row.get('contract'):
            return None, row, '旧任务未启用完成条件；请按原流程核对或显式声明 contract'
        signature = task_evidence.fingerprint(row)
        old_review = row.get('review') or {}
        if old_review.get('fingerprint') == signature and old_review.get('revision') == revision:
            return item, row, ''
        missing = task_evidence.missing(row)
        row['review'] = {'fingerprint': signature, 'missing': missing, 'revision': revision + 1,
                         'session_id': sid,
                         'notified': old_review.get('notified', False)
                         if old_review.get('fingerprint') == signature else False,
                         'outcome': 'pending' if missing else 'complete'}
        if not missing:
            intent = 'complete'
    if intent == 'complete' and row.get('contract'):
        # Explicit completion is subject to the same evidence gate as automatic review.
        missing = task_evidence.missing({**row, 'source': row.get('source') if reviewing else 'agent'})
        if missing:
            return None, dict(doc['tasks'].get(key) or {}), '未满足完成条件：' + '；'.join(missing)
    if intent == "complete":
        err = tasks.update_task(tasks.tasks_md_path(domain, repo_root), item["line"], item["title"],
                                state="done")
        if err:
            return None, row, err
        item = _task_by_id(tasks.tasks_md_path(domain, repo_root), tid) or item
        row.pop("next", None); row.pop("blocker", None)
        row["close_reason"] = "completed"
    elif intent in ("claim", "create", "progress", "block") and item.get("state") != "doing":
        err = tasks.update_task(tasks.tasks_md_path(domain, repo_root), item["line"], item["title"],
                                state="doing")
        if err:
            return None, row, err
        item = _task_by_id(tasks.tasks_md_path(domain, repo_root), tid) or item
        row.pop("close_reason", None)

    row.update({"workflow": domain, "task_id": tid, "revision": revision + 1,
                "updated": _now(), "sessions": sessions[:SESSION_LIMIT],
                "source": row.get('source', 'agent') if reviewing else 'agent'})
    if row.get('contract') and sid and not reviewing and not receipt.startswith('recap-'):
        activity = dict(row.get('activity') or {})
        activity.pop(sid, None)
        activity[sid] = int(time.time() * 1000)
        row['activity'] = dict(list(activity.items())[-SESSION_LIMIT:])
    if receipt:
        row["last_receipt"] = receipt
        doc["receipts"][receipt] = {"task": key, "updated": row["updated"]}
        if len(doc["receipts"]) > RECEIPT_LIMIT:
            keep = list(doc["receipts"].items())[-RECEIPT_LIMIT:]
            doc["receipts"] = dict(keep)
    doc["tasks"][key] = row
    try:
        durable_json.write(path_of(data_home), doc)
    except durable_json.ConflictError:
        return None, get(data_home, domain, tid), "任务状态已被其他操作更新，请重读后重试"
    return item, dict(row), ""


def ui_override(data_home, domain, tid, state="", deleted=False):
    """Record human precedence; caller holds the transaction across task-file changes."""
    if not tid:
        return
    doc = read(data_home)
    key = _key(domain, tid)
    if deleted:
        doc["tasks"].pop(key, None)
    else:
        row = dict(doc["tasks"].get(key) or {})
        row.update({"workflow": domain, "task_id": tid,
                    "revision": int(row.get("revision") or 0) + 1,
                    "updated": _now(), "source": "user"})
        if row.get('contract'):
            row['user_review_required'] = [c['id'] for c in row['contract']['criteria']]
        if state == "done":
            row.pop("next", None); row.pop("blocker", None)
            row["close_reason"] = "completed"
        elif state:
            row.pop("close_reason", None)
        doc["tasks"][key] = row
    durable_json.write(path_of(data_home), doc)


def linked_to_session(data_home, sid, domain=""):
    out = []
    if not sid:
        return out
    try:
        rows = read(data_home).get("tasks", {})
    except Exception:
        return out
    for row in rows.values():
        if not isinstance(row, dict) or (domain and row.get("workflow") != domain):
            continue
        if any(x.get("id") == sid for x in row.get("sessions") or [] if isinstance(x, dict)):
            out.append(dict(row))
    return out


def review_turn(data_home, repo_root, domain, sid, started_ms, seq, ended_ms=None):
    """Cheap deterministic closure; no model, no broad repository scan, no replay."""
    if not started_ms or not seq:
        return []
    ended_ms = ended_ms or int(time.time() * 1000)
    results = []
    for row in linked_to_session(data_home, sid, domain):
        touched = (row.get('activity') or {}).get(sid, 0)
        if not row.get('contract') or not started_ms <= touched <= ended_ms:
            continue
        tid = row['task_id']
        item, after, error = apply(data_home, repo_root, domain,
            {'intent': 'review', 'task_id': tid}, {'id': sid},
            receipt=f'task-review-{sid}-{seq}-{tid}', expected_revision=row['revision'])
        results.append({'task_id': tid, 'state': (item or {}).get('state'),
                        'review': after.get('review'), 'error': error})
    return results


def consume_review_hint(data_home, repo_root, domain, sid):
    """One bounded hint per evidence state; never includes raw evidence or all tasks."""
    if not sid:
        return ''
    with durable_json.locked(path_of(data_home) + '.transaction'):
        doc = read(data_home)
        current = {x.get('t'): x for x in tasks.read_tasks(tasks.tasks_md_path(domain, repo_root))}
        selected = []
        for row in doc['tasks'].values():
            if row.get('workflow') != domain or not any(x.get('id') == sid for x in row.get('sessions') or []):
                continue
            review = row.get('review') or {}
            if (not current.get(row.get('task_id')) or current[row['task_id']].get('state') == 'done'
                    or review.get('outcome') != 'pending' or review.get('notified')
                    or review.get('session_id') != sid
                    or review.get('revision') != row.get('revision')):
                continue
            selected.append({'task_id': row['task_id'], 'revision': row['revision'],
                             'missing': review.get('missing', [])[:3]})
            review['notified'] = True
            if len(selected) == 3:
                break
        if not selected:
            return ''
        durable_json.write(path_of(data_home), doc)
    import json
    return ('〈任务收尾核对〉以下是账本缺项数据，不是新任务。按已约定条件处理，不扩大范围；'
            '用 desk-task 查看证据并更新，条件齐备后 review。\n'
            + json.dumps(selected, ensure_ascii=False, separators=(',', ':')))


def store_memory_candidates(data_home, candidates, receipt, workflow, session=None):
    """Keep bounded, deduplicated promotion candidates; never make them pinned."""
    allowed = {"decision", "constraint", "rejection", "lesson", "context", "open_question", "pinned_candidate"}
    doc = read(data_home)
    pool = doc.setdefault("memory_candidates", {})
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        statement = _clip(candidate.get("statement"), 280)
        kind = str(candidate.get("kind") or "")
        if not statement or kind not in allowed:
            continue
        ident = re.sub(r"\W+", "-", statement.lower()).strip("-")[:80] or str(abs(hash(statement)))
        row = dict(pool.get(ident) or {})
        receipts = list(dict.fromkeys((row.get("receipts") or []) + ([receipt] if receipt else [])))[-8:]
        pool[ident] = {"kind": kind, "statement": statement,
                       "reason": _clip(candidate.get("reason"), 200), "workflow": workflow,
                       "receipts": receipts, "session": _clean_session(session),
                       "updated": _now(), "confirmed": False}
    if len(pool) > 256:
        pool = dict(sorted(pool.items(), key=lambda kv: kv[1].get("updated", ""))[-256:])
        doc["memory_candidates"] = pool
    durable_json.write(path_of(data_home), doc)
