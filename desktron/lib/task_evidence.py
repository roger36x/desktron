"""Opt-in completion contracts. Evidence is a bounded current snapshot, not a log."""
import hashlib
import json
import os
import re
import subprocess

KINDS = {'commit', 'test', 'deploy', 'acceptance', 'artifact'}


def text(value, limit, name):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'{name} 必须是非空字符串，最多 {limit} 字')
    return value.strip()


def contract(value):
    if not isinstance(value, dict) or set(value) != {'criteria'}:
        raise ValueError('contract 只接受 criteria')
    criteria = value['criteria']
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 8:
        raise ValueError('完成条件须为 1–8 项')
    out, seen = [], set()
    for c in criteria:
        if not isinstance(c, dict) or set(c) != {'id', 'kind', 'summary'}:
            raise ValueError('条件需要 id、kind、summary')
        cid = text(c['id'], 32, '条件 id')
        if not re.fullmatch(r'[a-z][a-z0-9_-]*', cid) or cid in seen:
            raise ValueError('条件 id 非法或重复')
        if c['kind'] not in KINDS:
            raise ValueError('条件 kind 非法')
        seen.add(cid)
        out.append({'id': cid, 'kind': c['kind'], 'summary': text(c['summary'], 160, '条件说明')})
    if sum(c['kind'] == 'commit' for c in out) > 1:
        raise ValueError('试点每项任务只绑定一个代码仓及交付提交')
    return {'criteria': out}


def evidence(value, spec, previous):
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise ValueError('evidence 须为 1–8 项')
    conditions = {c['id']: c for c in spec['criteria']}
    out, seen = dict(previous or {}), set()
    for e in value:
        if not isinstance(e, dict) or set(e) - {'criterion', 'summary', 'ref', 'repo', 'commit', 'subject', 'result'}:
            raise ValueError('证据字段非法')
        cid = e.get('criterion')
        if not isinstance(cid, str) or cid not in conditions or cid in seen:
            raise ValueError('证据必须绑定一个未重复的完成条件')
        seen.add(cid)
        item = {'criterion': cid, 'summary': text(e.get('summary'), 240, '证据摘要'),
                'ref': text(e.get('ref'), 500, '证据引用'), 'verification': 'agent_attested'}
        if conditions[cid]['kind'] == 'commit':
            if 'subject' in e or 'result' in e:
                raise ValueError('commit 证据不接受 subject/result；验证结果由系统生成')
            repo = text(e.get('repo'), 1000, 'repo')
            sha = text(e.get('commit'), 64, 'commit')
            if not os.path.isabs(repo) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', sha):
                raise ValueError('commit 证据需要绝对仓库路径和完整提交 SHA')
            try:
                result = subprocess.run(['git', '-C', repo, 'cat-file', '-t', sha],
                    capture_output=True, text=True, timeout=5)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ValueError('无法验证 Git 提交') from exc
            if result.returncode or result.stdout.strip() != 'commit':
                raise ValueError('Git 提交不存在或不是 commit 对象')
            item.update(repo=repo, commit=sha, verification='git_object', result='passed')
        else:
            if 'repo' in e or 'commit' in e:
                raise ValueError('非 commit 证据用 subject 绑定所验证的提交')
            if not isinstance(e.get('result'), str) or e['result'] not in ('passed', 'failed'):
                raise ValueError('非 commit 证据需要 result: passed 或 failed')
            item['result'] = e['result']
            if 'subject' in e:
                subject = text(e['subject'], 64, 'subject')
                if not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', subject):
                    raise ValueError('subject 必须是完整提交 SHA')
                item['subject'] = subject
        out[cid] = item
    return out


def criterion_status(row):
    """Return authoritative per-criterion UI state without exposing evidence internals."""
    spec = row.get('contract') or {}
    entries = row.get('evidence') or {}
    criteria = spec.get('criteria') if isinstance(spec, dict) else []
    commit_id = next((c['id'] for c in criteria or [] if c.get('kind') == 'commit'), '')
    sha = (entries.get(commit_id) or {}).get('commit')
    review_required = set(row.get('user_review_required') or [])
    user_changed = row.get('source') == 'user'
    out = {}
    for condition in criteria or []:
        cid = condition.get('id')
        evidence_row = entries.get(cid) or {}
        passed = bool(cid and evidence_row.get('result') == 'passed' and not user_changed
                      and cid not in review_required)
        if passed and condition.get('kind') != 'commit' and commit_id:
            passed = bool(sha and evidence_row.get('subject') == sha)
        if cid:
            out[cid] = 'passed' if passed else 'pending'
    return out


def missing(row):
    spec = row.get('contract')
    if not spec:
        return ['未启用完成条件；按原任务人工收口']
    entries = row.get('evidence') or {}
    commit_id = next((c['id'] for c in spec['criteria'] if c['kind'] == 'commit'), '')
    sha = (entries.get(commit_id) or {}).get('commit')
    out = []
    for c in spec['criteria']:
        e = entries.get(c['id'])
        if not e:
            out.append(c['id'] + '：缺少' + c['summary'])
        elif e.get('result') != 'passed':
            out.append(c['id'] + '：验证尚未通过')
        elif c['kind'] != 'commit' and commit_id and (not sha or e.get('subject') != sha):
            out.append(c['id'] + '：证据未绑定当前交付提交')
    if row.get('next'):
        out.append('仍有下一步：' + row['next'])
    if row.get('blocker'):
        out.append('仍有阻碍：' + row['blocker'])
    if not row.get('done'):
        out.append('缺少交付摘要')
    if row.get('source') == 'user' or row.get('user_review_required'):
        out.append('用户已调整任务，需重新确认交付证据' +
                   ('：' + ','.join(row['user_review_required']) if row.get('user_review_required') else ''))
    return out


def fingerprint(row):
    fields = ('contract', 'evidence', 'next', 'blocker', 'source', 'user_review_required')
    raw = json.dumps({**{k: row.get(k) for k in fields}, 'has_summary': bool(row.get('done'))},
                     sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()
