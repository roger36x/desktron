"""Presentation facts only: no retries, permission decisions or execution state."""
import copy
import hashlib
import re
import time


def safe_text(value, limit=4096):
    text = str(value or '')
    text = re.sub(r'(?i)(bearer\s+)[\w.~/+\-=]+', r'\1[redacted]', text)
    text = re.sub(r'(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)'
                  r'[\s"\x27]*[:=][\s"\x27]*)[^\s"\x27&,;]+', r'\1[redacted]', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]{12,}', '[redacted]', text)
    return text[:limit] + ('…' if len(text) > limit else '')


def failure(value, *, source='engine', code='UNKNOWN'):
    raw = value if isinstance(value, dict) else {'message': str(value or '')}
    native_code = raw.get('code') or raw.get('codexErrorInfo') or code
    if isinstance(native_code, dict):
        native_code = next(iter(native_code), code)
    result = {'source': source, 'code': safe_text(native_code, 100),
              'message': safe_text(str(raw.get('message') or '').strip() or '引擎报告了一个错误')}
    return result


TURN_STAGES = {'turn', 'reconnect', 'compact', 'compaction', 'native_pre_dispatch'}


def prepare(sid, payload, generation=0):
    p = copy.deepcopy(payload)
    stage = p.setdefault('stage', 'engine')
    p.setdefault('state', 'error')
    p.setdefault('scope', 'turn' if stage in TURN_STAGES else 'environment')
    p.setdefault('generation', generation if p['scope'] == 'turn' else 0)
    p.setdefault('observedAt', int(time.time() * 1000))
    p['label'] = safe_text(p.get('label') or '')
    p['details'] = [safe_text(x) for x in p.get('details') or []]
    identity = (sid, p['scope'], stage, p['generation'], p.get('operationID', ''),
                p.get('engine', ''), p.get('provider', ''),
                p.get('startedAt', p['observedAt']) if p['state'] in ('warning', 'error') else 0,
                p['label'] if p['state'] in ('warning', 'error') else '')
    p.setdefault('issueID', f'turn:{sid}:{p["generation"]}' if stage == 'turn' else
                 hashlib.sha256(repr(identity).encode()).hexdigest()[:24])
    return p


def issue_key(p):
    if p.get('stage') == 'auth' and p.get('engine'):
        return 'auth:' + p['engine'] + ':' + str(p.get('provider') or '')
    return p.get('stage', 'engine')


def current_source(row, engine=''):
    selected = row.get('selected_engine') or row.get('engine') or engine
    opts = (row.get('engine_opts') or {}).get(selected) or row.get('opts') or {}
    model = str(opts.get('model') or row.get('model') or '')
    provider = (model.split('/', 1)[0] if '/' in model else '') if selected == 'pi' else selected
    return selected, provider


def relevant(p, row, engine=''):
    selected, provider = current_source(row, engine)
    # Old auth facts cannot be attributed after a Shepherd switch. Keep the
    # diagnostic in storage, but never offer a login action against a guessed engine.
    if p.get('stage') == 'auth' and not p.get('engine') and row.get('shepherd_mode'):
        return False
    if p.get('engine') and selected and p['engine'] != selected:
        return False
    if p.get('stage') == 'auth' and p.get('provider') and provider:
        return p['provider'] == provider
    return True


def visible(row, engine='', fallback=None):
    issues = row.get('environment_issues') or {}
    if 'environment_issues' not in row and isinstance(row.get('environment'), dict):
        issues = {'legacy': row['environment']}
    candidates = [p for p in issues.values() if relevant(p, row, engine)]
    out = max(candidates, key=lambda p: (
        bool(p.get('action')) or p.get('stage') in ('permission', 'recovery'),
        p.get('state') == 'error', p.get('scope') != 'turn', int(p.get('generation') or 0)),
        default=fallback)
    return dict(out, issueRevision=int(row.get('environment_revision') or 0)) if out else None


def _legacy_compaction_permission(issue, incoming):
    """Recognize only the old Pi bug that persisted compact errors as permission."""
    if incoming.get('stage') != 'compaction' or incoming.get('state') != 'ready':
        return False
    error = issue.get('error') if isinstance(issue.get('error'), dict) else {}
    message = str(error.get('message') or '').strip()
    if (issue.get('stage') != 'permission'
            or error.get('code') != 'PERMISSION_EXTENSION_ERROR'
            or not message.startswith('Compaction summary failed:')):
        return False
    if not issue.get('engine') or issue.get('engine') != incoming.get('engine'):
        return False
    cutoff = int(incoming.get('resolvedThrough') or 0)
    observed = int(issue.get('startedAt', issue.get('observedAt', 0)) or 0)
    return bool(cutoff and observed <= cutoff)


def has_legacy_compaction_permission(row, incoming):
    issues = row.get('environment_issues') or {}
    if 'environment_issues' not in row and isinstance(row.get('environment'), dict):
        issues = {'legacy': row['environment']}
    return any(_legacy_compaction_permission(issue, incoming) for issue in issues.values())


def project(row, incoming, engine=''):
    """Independent auth sources; successful observations fence delayed failures."""
    issues = copy.deepcopy(row.get('environment_issues') or {})
    old = row.get('environment')
    if 'environment_issues' not in row and isinstance(old, dict) and old.get('state') in ('warning', 'error'):
        issues[issue_key(old)] = copy.deepcopy(old)
    if incoming.get('state') == 'ready' and incoming.get('stage') == 'compaction':
        issues = {key: value for key, value in issues.items()
                  if not _legacy_compaction_permission(value, incoming)}
    key = issue_key(incoming)
    if incoming['state'] in ('warning', 'error'):
        resolved = (row.get('environment_resolved') or {}).get(key, 0)
        started = int(incoming.get('startedAt', incoming.get('observedAt', 0)))
        prior = issues.get(key) or {}
        newer = incoming['stage'] != 'auth' or started >= int(prior.get('startedAt', prior.get('observedAt', 0)))
        if newer and (not resolved or started > resolved):
            issues[key] = incoming
    elif incoming['state'] == 'ready':
        prior = issues.get(key)
        cutoff = incoming.get('resolvedThrough')
        if cutoff is not None and incoming['stage'] == 'auth':
            resolved = row.setdefault('environment_resolved', {})
            resolved[key] = max(int(resolved.get(key, 0)), int(cutoff))
        if (prior is None or (int(prior.get('startedAt', prior.get('observedAt', 0))) <= int(cutoff)
                             if cutoff is not None else
                             int(prior.get('generation') or 0) <= int(incoming.get('generation') or 0))):
            issues.pop(key, None)
    row['environment_issues'] = issues
    row['environment_revision'] = int(row.get('environment_revision') or 0) + 1
    fallback = incoming if relevant(incoming, row, engine) and incoming['state'] not in ('error', 'warning') else {
        'state':'ready', 'stage':'environment', 'label':'', 'clear':True}
    out = visible(row, engine, fallback)
    if out and out.get('state') in ('warning', 'error'):
        row['environment'] = copy.deepcopy(out)
    else:
        row.pop('environment', None)
    return out
