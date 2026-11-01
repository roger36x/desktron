"""Local trial policy. Disabling execution preserves all trial evidence."""
import os
from pathlib import Path
from durable_json import read, write
from .store import Conflict

TRIAL_DEFAULT_ENABLED = True


def data_root(data_home):
    return os.path.join(os.fspath(data_home), 'experiments', 'shepherd-unified')


def policy(data_home):
    path = Path(data_home) / 'shepherd-trial-policy.json'
    try:
        value = read(str(path)) if path.exists() else {}
        if not isinstance(value, dict) or set(value) - {'create', 'execute'}:
            raise ValueError('invalid policy')
        result = {k: value.get(k, TRIAL_DEFAULT_ENABLED) for k in ('create', 'execute')}
        if any(type(v) is not bool for v in result.values()): raise ValueError('invalid policy')
        return result
    except Exception:
        return {'create': False, 'execute': False, 'error': '试用策略无法读取，执行已暂停'}


def set_policy(data_home, *, create, execute):
    if type(create) is not bool or type(execute) is not bool:
        raise Conflict('Trial policy requires booleans')
    path = str(Path(data_home) / 'shepherd-trial-policy.json')
    value = read(path)
    value.clear()
    value.update(create=create, execute=execute)
    write(path, value)
    return dict(value)
